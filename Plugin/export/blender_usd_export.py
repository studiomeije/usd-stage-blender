"""
Blender USD export wrapper

Uses Blender's native USD exporter to create initial USD file,
which will then be post-processed for RealityKit compatibility.
"""

from __future__ import annotations

import json
import os
import bpy
import hashlib
import re
import secrets
import shutil
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import require_supported_blender_version
from ..apple_contract import (
    REALITYKIT_METERS_PER_UNIT,
    REALITYKIT_SCENE_UNITS,
    REALITYKIT_UP_AXIS,
    REALITYKIT_USD_EXPORT_FORWARD_AXIS,
)
from . import animation_export
from . import usd_hook
from .staging_namespace import (
    forget_native_texture_copies,
    record_native_texture_copies,
    snapshot_texture_directory,
)
from .sidecar_manifest import (
    OWNED_SIDECAR_DIRECTORIES,
    SIDECAR_MANIFEST_DIRECTORY,
    SIDECAR_MANIFEST_SCHEMA_VERSION,
    SidecarManifestError,
    canonical_output_identity as _canonical_output_identity,
    output_sidecar_manifest_path,
    read_output_sidecar_manifest,
    validate_manifest_destination,
    validate_sidecar_relative_path,
)


_VALID_USD_EXPORT_NGON_METHODS = {"BEAUTY", "CLIP"}

# Properties whose names/semantics define the Blender 5.2 USD boundary used by
# this add-on. We inspect the live operator before invoking it and fail closed
# if Blender changes the contract; silently dropping an option can change the
# exported asset (notably the texture-copy behavior).
_REQUIRED_USD_EXPORT_PROPERTIES = frozenset(
    {
        "export_textures_mode",
        "generate_preview_surface",
        "generate_materialx_network",
        "root_prim_path",
    }
)


def _ngon_method_for_usd_export(value: str) -> str:
    """Map UI n-gon method to Blender USD exporter enum values."""
    if value is None:
        return value
    value = str(value).strip()
    if not value:
        return value
    if value in _VALID_USD_EXPORT_NGON_METHODS:
        return value
    if value == "EAR_CLIP":
        return "CLIP"
    upper = value.upper()
    if upper in _VALID_USD_EXPORT_NGON_METHODS:
        return upper
    if upper == "EAR_CLIP":
        return "CLIP"
    return value


_STAGING_ATTEMPT_SUFFIX = re.compile(r".+\.[0-9a-f]{32}$")


def _absolute_output_path(path: str | Path) -> Path:
    """Anchor an output path to the working directory if it is relative.

    Every consumer of a staging path compares it against another path that has
    already been resolved, so the two spellings of one destination must not
    diverge. They did: ``get_export_staging_dir`` built its directory from the
    caller's raw path while ``create_texture_staging_state`` resolved the same
    output to an absolute path, and a relative ``-o`` put those on opposite
    sides of a relative/absolute comparison.
    """
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else Path.cwd() / candidate


def get_export_staging_dir(
    final_path: str | Path,
    *,
    attempt_id: str | None = None,
) -> Path:
    """Return a unique, attempt-scoped intermediate directory.

    The old stem-only path let ``scene.usda`` and ``scene.usdc`` share one
    directory, and a concurrent reset could delete another process's in-flight
    export. The complete output filename plus a random attempt token makes the
    directory an ownership handle rather than global mutable state.

    The result is always absolute. Texture staging resolves the staging USD to
    an absolute path (``create_texture_staging_state``) and then authors baked
    textures relative to it. A relative staging directory - which is what a
    relative ``-o`` produced - made that a relative-against-absolute
    comparison, so the authored asset path came out as a run of ``../``
    segments long enough to walk past the filesystem root and clamp there:
    ``/.usdstage_temp/…``. The bake then died reporting its own freshly
    written texture missing. The panel never saw it because it always passes an
    absolute output path.
    """
    final_path = _absolute_output_path(final_path)
    token = attempt_id or secrets.token_hex(16)
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError(f"Invalid export staging attempt id: {token!r}")
    output_key = _portable_staging_output_name(final_path.name)
    return final_path.parent / ".usdstage_temp" / f"{output_key}.{token}"


def create_export_staging_dir(
    final_path: str | Path,
    diagnostics=None,
) -> Path:
    """Allocate and create one unique export-attempt directory."""
    final_path = Path(final_path)
    last_error = None
    for _attempt in range(16):
        staging_dir = get_export_staging_dir(final_path)
        temp_root = staging_dir.parent
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            if temp_root.is_symlink():
                raise RuntimeError(
                    f"Refusing symlinked export staging root: {temp_root}"
                )
            temp_root.mkdir(exist_ok=True)
            _validate_export_staging_dir(staging_dir, create=False)
            staging_dir.mkdir()
            return _validate_export_staging_dir(staging_dir, create=False)
        except FileExistsError as exc:
            # A cryptographic token collision or pre-created unowned path must
            # never be reset/reused. Generate a new ownership handle instead.
            last_error = exc
            continue
        except Exception as exc:
            if diagnostics:
                diagnostics.add_warning(
                    f"Failed to create export staging directory '{staging_dir}': {exc}"
                )
            raise
    raise RuntimeError(
        f"Could not allocate a unique export staging directory for '{final_path}'."
    ) from last_error


def _portable_staging_output_name(name: str) -> str:
    normalized = unicodedata.normalize("NFC", str(name or "scene.usd"))
    portable = "".join(
        character
        if (character.isalnum() or character in {"-", "_", "."})
        else "_"
        for character in normalized
    ) or "scene.usd"
    if portable != normalized:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        portable = f"{portable}-{digest}"
    return portable


def _validate_export_staging_dir(staging_dir: str | Path, *, create: bool = False) -> Path:
    """Reject staging roots that can redirect writes or recursive deletion.

    Both ``.usdstage_temp`` and its per-output child are managed paths. A
    symlink at either level could otherwise redirect ``mkdir``, ``rmtree`` or
    the native USD exporter outside the selected output directory.
    """
    staging_dir = Path(staging_dir)
    temp_root = staging_dir.parent
    output_parent = temp_root.parent
    if (
        temp_root.name != ".usdstage_temp"
        or not _STAGING_ATTEMPT_SUFFIX.fullmatch(staging_dir.name)
    ):
        raise RuntimeError(f"Refusing unsafe export staging directory: {staging_dir}")

    if create:
        output_parent.mkdir(parents=True, exist_ok=True)
    if temp_root.is_symlink():
        raise RuntimeError(f"Refusing symlinked export staging root: {temp_root}")
    if create:
        temp_root.mkdir(exist_ok=True)
    if not temp_root.exists():
        return staging_dir
    if not temp_root.is_dir():
        raise RuntimeError(f"Export staging root is not a directory: {temp_root}")
    if temp_root.resolve().parent != output_parent.resolve():
        raise RuntimeError(f"Export staging root escapes the output directory: {temp_root}")

    if staging_dir.is_symlink():
        raise RuntimeError(f"Refusing symlinked per-output staging directory: {staging_dir}")
    if create:
        staging_dir.mkdir(exist_ok=True)
    if staging_dir.exists():
        if not staging_dir.is_dir():
            raise RuntimeError(f"Per-output staging path is not a directory: {staging_dir}")
        if staging_dir.resolve().parent != temp_root.resolve():
            raise RuntimeError(f"Per-output staging directory escapes its root: {staging_dir}")
    return staging_dir


def _ensure_export_staging_dir(staging_dir: str | Path) -> Path:
    return _validate_export_staging_dir(staging_dir, create=True)


def _validate_staging_matches_final(
    staging_dir: str | Path,
    final_path: str | Path,
) -> Path:
    staging_dir = Path(staging_dir)
    # Normalize both sides the same way get_export_staging_dir does, or a
    # relative output path fails its own ownership check.
    final_path = _absolute_output_path(final_path)
    expected_root = final_path.parent / ".usdstage_temp"
    staged_output_key, separator, _attempt_token = staging_dir.name.rpartition(".")
    expected_output_key = _portable_staging_output_name(final_path.name)
    if (
        staging_dir.parent != expected_root
        or separator != "."
        or staged_output_key != expected_output_key
    ):
        raise RuntimeError(
            f"Export staging directory '{staging_dir}' does not belong to '{final_path}'."
        )
    return _validate_export_staging_dir(staging_dir, create=False)


#: Blender's USD exporter never rescales geometry to match the scene's unit
#: scale - ``convert_scene_units`` only *declares* ``metersPerUnit``. Measured
#: on Blender 5.2: a 2-unit cube exported from ``scale_length`` 1.0 and 0.01
#: produced identical points (``max_x = 1.0``) under ``METERS``, under
#: ``CUSTOM``/1.0, and under ``CENTIMETERS`` (which only changed the declared
#: ``metersPerUnit`` to 0.01).
#:
#: The Apple spatial contract pins ``metersPerUnit`` to 1.0, so a scene at any
#: other unit scale would export geometry whose numbers are Blender units while
#: the stage claims they are metres. A centimetre-scale scene reads "2 cm" in
#: the sidebar and lands in RealityKit 100x oversized, with nothing to indicate
#: it happened.
#:
#: Refusing is the honest behaviour until the exporter can compensate. Baking
#: the scale into a root transform is the real fix, but it has to be validated
#: against the skeletal and animated fixtures before it can be trusted, so it
#: is deliberately not attempted here.
_SUPPORTED_SCENE_UNIT_SCALE = 1.0
_SCENE_UNIT_SCALE_TOLERANCE = 1e-6


def _require_supported_scene_unit_scale(context) -> None:
    """Refuse a scene whose unit scale the Apple contract cannot represent."""
    scene = getattr(context, "scene", None)
    unit_settings = getattr(scene, "unit_settings", None)
    scale_length = getattr(unit_settings, "scale_length", None)
    if scale_length is None:
        return
    try:
        scale_length = float(scale_length)
    except (TypeError, ValueError):
        return
    if abs(scale_length - _SUPPORTED_SCENE_UNIT_SCALE) <= _SCENE_UNIT_SCALE_TOLERANCE:
        return

    raise RuntimeError(
        "Scene unit scale is "
        f"{scale_length:g}, but the RealityKit export contract fixes "
        "metersPerUnit at 1.0 and Blender's USD exporter does not rescale "
        "geometry to match. Exporting would author your Blender units as "
        f"metres, making the asset {1.0 / scale_length:g}x the size you see in "
        "Blender. Set Scene Properties > Units > Unit Scale to 1.0 (and apply "
        "object scale where needed) before exporting."
    )


def export_blender_scene(
    context,
    settings,
    final_path: str,
    diagnostics=None,
    *,
    reset_staging: bool = True,
    staging_dir: str | Path | None = None,
) -> Optional[str]:
    """Export Blender scene to USD using Blender's native exporter

    Args:
        context: Blender context
        settings: Export settings
        final_path: Final output path
        reset_staging: Wipe and recreate the staging dir before exporting. The
            bake-export flow bakes textures into the staging dir *before* calling
            this, so it resets the dir itself beforehand and passes False here to
            avoid deleting those freshly baked textures.
        staging_dir: Exact attempt directory allocated by a multi-phase caller.
            Ordinary exports omit it and receive a fresh private attempt.

    Returns:
        Path to exported USD file (temporary if USDZ is requested)
    """
    require_supported_blender_version()
    _require_supported_scene_unit_scale(context)

    export_format = getattr(settings, "export_format", "USDA")

    # Stage all exports in an export-specific temp directory. This prevents
    # Blender's USD exporter from resolving relative texture paths against an
    # existing destination `textures/` directory and reusing stale sidecars from
    # previous exports.
    allocated_here = staging_dir is None
    temp_dir = (
        Path(staging_dir)
        if staging_dir is not None
        else create_export_staging_dir(final_path, diagnostics)
    )
    _validate_staging_matches_final(temp_dir, final_path)
    if reset_staging and not allocated_here:
        _reset_export_staging_dir(temp_dir, diagnostics)
    elif not allocated_here:
        _ensure_export_staging_dir(temp_dir)
    temp_ext = ".usdc" if export_format == "USDZ" else Path(final_path).suffix
    if not temp_ext:
        temp_ext = ".usdc" if export_format == "USDC" else ".usda"
    temp_usd = temp_dir / f"{Path(final_path).stem}{temp_ext}"
    output_path = str(temp_usd)

    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Get export settings
    root_prim_name = getattr(settings, "root_prim_name", "") or "Scene"
    root_prim_path = root_prim_name if root_prim_name.startswith("/") else f"/{root_prim_name}"

    # Configure Blender USD export
    # Note: This uses Blender's built-in USD exporter
    # We'll use the operator directly

    # Save current selection
    original_selection = [obj for obj in context.selected_objects]
    original_active = context.active_object
    animation_state = None
    export_completed = False

    try:
        export_kwargs = _build_export_kwargs(
            settings,
            output_path=output_path,
            root_prim_path=root_prim_path,
        )

        # Orientation and units are fixed by the Apple spatial contract in
        # ``_build_export_kwargs``; they are not scene settings.

        # If exporting animations, ensure all actions are serialized into a single NLA
        # track so downstream tools (Reality Composer Pro) can clip the timeline.
        animation_state = animation_export.prepare_animation_export(context, settings, diagnostics)

        # Record exactly which texture files the native exporter creates. With
        # export_textures_mode='NEW' it copies packed/generated images to
        # <staging>/textures/<basename>; texture staging then supersedes each
        # with a content-addressed copy and must delete the flat original.
        # Those originals cannot be recognised by path shape - a user's own
        # authoritative texture can sit at exactly textures/<name>.png - so the
        # only sound signal is what appeared while this operator ran.
        textures_before = snapshot_texture_directory(temp_dir)

        # The prim-map hook records exactly which Blender material produced
        # each USD material prim for the MaterialX rewrite.
        with usd_hook.capture_prim_map():
            _invoke_usd_export(
                bpy.ops.wm.usd_export,
                export_kwargs,
                diagnostics=diagnostics,
            )

        record_native_texture_copies(
            temp_dir, textures_before, snapshot_texture_directory(temp_dir)
        )

        if not os.path.exists(output_path):
            raise RuntimeError(f"USD export failed: {output_path} not created")

        export_completed = True
        return output_path

    finally:
        if animation_state is not None:
            animation_export.restore_animation_export(animation_state)

        # Restore selection
        try:
            for obj in context.view_layer.objects:
                obj.select_set(False)
        except Exception:
            pass
        for obj in original_selection:
            try:
                obj.select_set(True)
            except Exception:
                pass
        if original_active:
            try:
                context.view_layer.objects.active = original_active
            except Exception:
                pass
        if not export_completed:
            cleanup_export_staging_dir(temp_usd, diagnostics)


def publish_unpacked_export(staged_usd_path: str | Path, final_path: str | Path, diagnostics=None) -> None:
    """Transactionally publish a staged USD and its exact sidecar closure.

    Every replacement is copied into a same-filesystem transaction directory
    first. Existing files are backed up without removing them, replacements
    use atomic ``os.replace``, and any exception (including ``TimeoutError`` or
    cancellation) rolls committed paths back before it escapes.
    """
    staged_usd = Path(staged_usd_path)
    final = Path(final_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.is_symlink():
        raise RuntimeError(f"Refusing to replace symlinked export destination: {final}")
    if not staged_usd.is_file():
        raise RuntimeError(f"Staged USD export does not exist: {staged_usd}")

    with _output_publication_lock(final):
        _publish_unpacked_export_locked(staged_usd, final, diagnostics)


def _publish_unpacked_export_locked(
    staged_usd: Path,
    final: Path,
    diagnostics=None,
) -> None:
    """Publish while holding the exact final-output process lock."""

    _recover_abandoned_publication_transactions(final, diagnostics)
    staged_dir = staged_usd.parent
    sidecars = _collect_staged_sidecars(staged_dir, final.parent)
    old_owned = _read_output_sidecar_entries(final, diagnostics)
    other_owned = _sidecars_owned_by_other_outputs(final)
    sidecar_actions, published_sidecars = _plan_sidecar_publication(
        sidecars,
        final,
        old_owned,
        other_owned,
    )

    new_entries = {
        path.relative_to(final.parent).as_posix()
        for path in published_sidecars
    }
    stale_entries = sorted(old_owned - new_entries - other_owned)

    transaction_dir = _create_publication_transaction_dir(final)
    try:
        prepared_root = transaction_dir / "prepared"
        prepared_sidecars = []
        for index, (source, destination, role) in enumerate(sidecar_actions):
            prepared = prepared_root / f"sidecar-{index:05d}"
            _copy_publication_file(source, prepared)
            prepared_sidecars.append(_PublicationAction(destination, prepared, role))

        prepared_final = prepared_root / f"export{final.suffix}"
        _copy_publication_file(staged_usd, prepared_final)

        transition_entries = sorted(old_owned | new_entries)
        prepared_transition_manifest = None
        if transition_entries:
            prepared_transition_manifest = prepared_root / "transition-manifest.json"
            _write_manifest_entries(
                prepared_transition_manifest,
                final,
                transition_entries,
            )

        prepared_final_manifest = None
        if new_entries:
            prepared_final_manifest = prepared_root / "final-manifest.json"
            _write_manifest_entries(
                prepared_final_manifest,
                final,
                sorted(new_entries),
            )

        _execute_root_last_publication(
            sidecar_actions=prepared_sidecars,
            prepared_final=prepared_final,
            final_path=final,
            prepared_transition_manifest=prepared_transition_manifest,
            prepared_final_manifest=prepared_final_manifest,
            transaction_dir=transaction_dir,
            diagnostics=diagnostics,
        )
    finally:
        _cleanup_publication_transaction_dir(transaction_dir, diagnostics)

    _remove_stale_sidecars_after_commit(final, stale_entries, diagnostics)
    if diagnostics:
        diagnostics.add_generated_file("export", str(final), source=str(staged_usd))
        for path in published_sidecars:
            diagnostics.add_generated_file("sidecar_asset", str(path))
        if published_sidecars:
            diagnostics.add_generated_file(
                "sidecar_manifest",
                str(_output_sidecar_manifest_path(final)),
            )
        for entry in stale_entries:
            diagnostics.add_generated_file(
                "removed_stale_sidecar",
                str(final.parent / entry),
            )

    cleanup_export_staging_dir(staged_usd, diagnostics)


def cleanup_export_staging_dir(staged_path: str | Path, diagnostics=None) -> None:
    """Remove an export staging directory if the path is inside `.usdstage_temp`."""
    staged_path = Path(staged_path)
    target_dir = staged_path.parent
    if target_dir.parent.name != ".usdstage_temp":
        return
    temp_root = target_dir.parent
    # The attempt is over, so any unconsumed native-texture record is dead.
    # Texture staging pops it on the success path; this covers the failure one,
    # where it would otherwise outlive its directory.
    forget_native_texture_copies(target_dir)
    try:
        _validate_export_staging_dir(target_dir, create=False)
    except RuntimeError as exc:
        if diagnostics:
            diagnostics.add_warning(str(exc))
        return

    try:
        if target_dir.exists():
            shutil.rmtree(target_dir)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(
                f"Failed to remove export staging directory '{target_dir}': {exc}"
            )
        return

    if temp_root.name == ".usdstage_temp":
        try:
            temp_root.rmdir()
        except OSError:
            pass


def remove_export_staging_dir(
    final_path: str | Path,
    diagnostics=None,
    *,
    staging_dir: str | Path | None = None,
) -> None:
    """Guarantee the per-export staging tree for *final_path* is gone.

    ``cleanup_export_staging_dir`` only runs on the success path (inside
    publish/pack), so any early return or exception between staging and publish
    leaves its attempt directory below ``.usdstage_temp`` in the user's
    export directory.
    This is safe to call from a ``finally`` after every export attempt - success
    OR failure - so the staging tree never lingers. Idempotent and best-effort:
    a missing dir, an already-cleaned tree, or an rmtree error are all swallowed.
    """
    if staging_dir is None:
        # A final filename does not identify one directory: several exports
        # may be in flight, and deleting every matching child could remove
        # another process's staging tree.
        if diagnostics:
            diagnostics.add_warning(
                f"Skipped staging cleanup for '{final_path}' because no exact attempt directory was provided."
            )
        return
    staging_dir = Path(staging_dir)
    try:
        _validate_staging_matches_final(staging_dir, final_path)
    except RuntimeError as exc:
        if diagnostics:
            diagnostics.add_warning(str(exc))
        return
    temp_root = staging_dir.parent
    if staging_dir.exists():
        # Let rmtree raise so a genuine failure (locked file, permissions) is
        # surfaced as a warning rather than silently swallowed - matching
        # cleanup_export_staging_dir. The caller still wraps this in try/except,
        # so a warning here never masks the original export error.
        try:
            shutil.rmtree(staging_dir)
        except Exception as exc:
            if diagnostics:
                diagnostics.add_warning(
                    f"Failed to remove export staging directory '{staging_dir}': {exc}"
                )
            return
    # Drop the now-empty ".usdstage_temp" root too (no-op while other
    # exports still have staging dirs inside it).
    try:
        temp_root.rmdir()
    except OSError:
        pass


def _reset_export_staging_dir(staging_dir: str | Path, diagnostics=None) -> None:
    """Create an empty per-export staging directory."""
    staging_dir = Path(staging_dir)

    try:
        _validate_export_staging_dir(staging_dir, create=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        _ensure_export_staging_dir(staging_dir)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(
                f"Failed to reset export staging directory '{staging_dir}': {exc}"
            )
        raise RuntimeError(
            f"Failed to reset export staging directory '{staging_dir}': {exc}"
        ) from exc


_SIDECAR_MANIFEST_DIRECTORY = SIDECAR_MANIFEST_DIRECTORY
_SIDECAR_MANIFEST_SCHEMA_VERSION = SIDECAR_MANIFEST_SCHEMA_VERSION
_OWNED_SIDECAR_DIRECTORIES = OWNED_SIDECAR_DIRECTORIES
_PUBLICATION_TRANSACTION_DIRECTORY = ".usdstage_publish"
_PUBLICATION_LOCK_DIRECTORY = "locks"
_PUBLICATION_OWNER_MARKER = ".owner.json"


@contextmanager
def _output_publication_lock(final_path: Path):
    """Fail closed when another process is publishing the same final output."""
    transaction_root = final_path.parent / _PUBLICATION_TRANSACTION_DIRECTORY
    if transaction_root.is_symlink():
        raise RuntimeError(
            f"Refusing symlinked publication transaction root: {transaction_root}"
        )
    transaction_root.mkdir(exist_ok=True)
    if (
        not transaction_root.is_dir()
        or transaction_root.resolve().parent != final_path.parent.resolve()
    ):
        raise RuntimeError(f"Unsafe publication transaction root: {transaction_root}")

    lock_root = transaction_root / _PUBLICATION_LOCK_DIRECTORY
    if lock_root.is_symlink():
        raise RuntimeError(f"Refusing symlinked publication lock root: {lock_root}")
    lock_root.mkdir(exist_ok=True)
    if not lock_root.is_dir() or lock_root.resolve().parent != transaction_root.resolve():
        raise RuntimeError(f"Unsafe publication lock root: {lock_root}")

    lock_key = hashlib.sha256(
        _canonical_output_identity(final_path).encode("utf-8")
    ).hexdigest()
    lock_path = lock_root / f"{lock_key}.lock"
    if lock_path.is_symlink():
        raise RuntimeError(f"Refusing symlinked publication lock: {lock_path}")
    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, open_flags, 0o600)
    locked = False
    try:
        try:
            _lock_publication_descriptor(descriptor)
            locked = True
        except (BlockingIOError, OSError) as exc:
            raise RuntimeError(
                f"Another export is already publishing '{final_path}'."
            ) from exc
        yield
    finally:
        if locked:
            _unlock_publication_descriptor(descriptor)
        os.close(descriptor)


def _lock_publication_descriptor(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except ImportError:
        pass

    import msvcrt

    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)


def _unlock_publication_descriptor(descriptor: int) -> None:
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    except ImportError:
        pass

    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@dataclass(frozen=True)
class _PublicationAction:
    destination: Path
    prepared_source: Path | None
    role: str


def _collect_staged_sidecars(staged_dir: Path, output_parent: Path):
    sidecars = []
    if staged_dir.is_symlink():
        raise RuntimeError(f"Refusing symlinked export staging directory: {staged_dir}")
    for dirname in sorted(_OWNED_SIDECAR_DIRECTORIES):
        source_root = staged_dir / dirname
        if source_root.is_symlink():
            raise RuntimeError(f"Refusing symlinked staged sidecar directory: {source_root}")
        if not source_root.exists():
            continue
        if not source_root.is_dir():
            raise RuntimeError(f"Staged sidecar root is not a directory: {source_root}")
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise RuntimeError(f"Refusing symlinked staged sidecar: {source}")
            if not source.is_file():
                continue
            relative = Path(dirname) / source.relative_to(source_root)
            destination = _safe_sidecar_destination(output_parent, relative)
            sidecars.append((source, destination, relative.as_posix()))
    return sidecars


def _safe_sidecar_destination(output_parent: Path, relative: Path) -> Path:
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or relative.parts[0] not in _OWNED_SIDECAR_DIRECTORIES
    ):
        raise RuntimeError(f"Refusing unsafe sidecar path: {relative}")

    root = output_parent / relative.parts[0]
    if root.is_symlink():
        raise RuntimeError(f"Refusing symlinked sidecar destination root: {root}")
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"Sidecar destination root is not a directory: {root}")

    current = root
    for part in relative.parts[1:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(
                f"Refusing symlinked sidecar destination directory: {current}"
            )
        if current.exists() and not current.is_dir():
            raise RuntimeError(
                f"Sidecar destination parent is not a directory: {current}"
            )

    destination = output_parent / relative
    if destination.is_symlink():
        raise RuntimeError(f"Refusing symlinked sidecar destination: {destination}")
    resolved_root = root.resolve()
    resolved_destination = destination.resolve()
    if not resolved_destination.is_relative_to(resolved_root):
        raise RuntimeError(f"Sidecar destination escapes its managed root: {destination}")
    return destination


def _validate_manifest_destination(final_path: Path) -> Path:
    return validate_manifest_destination(final_path)


def _read_output_sidecar_entries(final_path: Path, diagnostics=None) -> set[str]:
    try:
        manifest = read_output_sidecar_manifest(final_path)
    except SidecarManifestError as exc:
        if diagnostics:
            diagnostics.add_warning(str(exc))
        raise RuntimeError(str(exc)) from exc
    if manifest is None:
        return set()
    return {entry.as_posix() for entry in manifest.sidecars}


def _plan_sidecar_publication(sidecars, final_path: Path, old_owned, other_owned):
    actions = []
    published = []
    for source, destination, relative in sidecars:
        published.append(destination)
        exists = destination.exists() or destination.is_symlink()
        if not exists:
            if relative in other_owned:
                raise RuntimeError(
                    f"Sidecar path '{relative}' is owned by another output but is missing; refusing to replace its contract."
                )
            actions.append((source, destination, "sidecar_asset"))
            continue
        if destination.is_symlink() or not destination.is_file():
            raise RuntimeError(f"Refusing non-file sidecar collision: {destination}")

        owned_by_current = relative in old_owned
        owned_by_other = relative in other_owned
        if not owned_by_current and not owned_by_other:
            raise RuntimeError(
                f"Refusing to overwrite unowned sidecar collision: {destination}"
            )

        identical = _files_are_identical(source, destination)
        if not identical:
            raise RuntimeError(
                f"Immutable sidecar collision has different bytes: {destination}"
            )
        # Identical content-addressed files are safe to share across manifests.
        # They are already fully installed, so no publication action is needed.
        continue
    return actions, published


def _files_are_identical(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as lhs, second.open("rb") as rhs:
            while True:
                left = lhs.read(1024 * 1024)
                right = rhs.read(1024 * 1024)
                if left != right:
                    return False
                if not left:
                    return True
    except OSError:
        return False


def _create_publication_transaction_dir(final_path: Path) -> Path:
    root = final_path.parent / _PUBLICATION_TRANSACTION_DIRECTORY
    if root.is_symlink():
        raise RuntimeError(f"Refusing symlinked publication transaction root: {root}")
    root.mkdir(exist_ok=True)
    if not root.is_dir() or root.resolve().parent != final_path.parent.resolve():
        raise RuntimeError(f"Unsafe publication transaction root: {root}")
    prefix = f"{final_path.name}."
    transaction_dir = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    _write_manifest_entries(
        transaction_dir / _PUBLICATION_OWNER_MARKER,
        final_path,
        (),
    )
    return transaction_dir


def _recover_abandoned_publication_transactions(
    final_path: Path,
    diagnostics=None,
) -> None:
    """Remove hard-exit debris while the exact output lock is held."""
    root = final_path.parent / _PUBLICATION_TRANSACTION_DIRECTORY
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Unsafe publication transaction root: {root}")
    for candidate in sorted(root.iterdir()):
        if candidate.name == _PUBLICATION_LOCK_DIRECTORY:
            continue
        marker = candidate / _PUBLICATION_OWNER_MARKER
        try:
            if (
                candidate.is_symlink()
                or not candidate.is_dir()
                or marker.is_symlink()
            ):
                continue
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if (
                payload.get("schema_version") != _SIDECAR_MANIFEST_SCHEMA_VERSION
                or payload.get("output") != _canonical_output_identity(final_path)
                or payload.get("sidecars") != []
            ):
                continue
            if candidate.resolve().parent != root.resolve():
                continue
            shutil.rmtree(candidate)
            if diagnostics:
                diagnostics.add_warning(
                    f"Recovered abandoned publication transaction '{candidate}'."
                )
        except Exception as exc:
            if diagnostics:
                diagnostics.add_warning(
                    f"Could not recover abandoned publication transaction '{candidate}': {exc}"
                )


def _write_manifest_entries(path: Path, final_path: Path, entries) -> None:
    payload = {
        "schema_version": _SIDECAR_MANIFEST_SCHEMA_VERSION,
        "output": _canonical_output_identity(final_path),
        "sidecars": sorted(set(entries)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_publication_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f"Publication source is not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _replace_publication_file(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def _unlink_publication_file(path: Path) -> None:
    path.unlink()


def _validate_publication_action_destination(destination: Path, output_parent: Path) -> None:
    if destination.is_symlink():
        raise RuntimeError(f"Refusing symlinked publication destination: {destination}")
    lexical_output = output_parent.absolute()
    lexical_destination = destination.absolute()
    try:
        relative_parent = lexical_destination.parent.relative_to(lexical_output)
    except ValueError as exc:
        raise RuntimeError(
            f"Publication destination escapes output directory: {destination}"
        ) from exc
    current = lexical_output
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(
                f"Refusing symlinked publication destination directory: {current}"
            )
    resolved_parent = destination.parent.resolve()
    resolved_output = output_parent.resolve()
    if resolved_parent != resolved_output and not resolved_parent.is_relative_to(resolved_output):
        raise RuntimeError(f"Publication destination escapes output directory: {destination}")


def _publication_phase_checkpoint(_phase: str) -> None:
    """No-op fault point used by hard-exit crash-coherence tests."""


def _execute_root_last_publication(
    *,
    sidecar_actions: list[_PublicationAction],
    prepared_final: Path,
    final_path: Path,
    prepared_transition_manifest: Path | None,
    prepared_final_manifest: Path | None,
    transaction_dir: Path,
    diagnostics=None,
) -> None:
    """Install immutable sidecars, then atomically switch the root USD.

    A transition manifest protects both generations across the only externally
    forced termination available to Blender's watchdog (``os._exit``). At every
    checkpoint either the old root and old sidecars or the new root and all new
    sidecars form a complete artifact. A hard exit may leak an unused immutable
    generation, but cannot corrupt the previously published asset.
    """
    output_parent = transaction_dir.parent.parent
    backup_root = transaction_dir / "backups"
    manifest_path = _validate_manifest_destination(final_path)
    _validate_publication_action_destination(final_path, output_parent)
    if final_path.exists() and not final_path.is_file():
        raise RuntimeError(f"Refusing non-file export destination: {final_path}")

    old_final_backup = None
    if final_path.exists():
        old_final_backup = backup_root / f"old-export{final_path.suffix}"
        _copy_publication_file(final_path, old_final_backup)
    old_manifest_backup = None
    if manifest_path.exists():
        old_manifest_backup = backup_root / "old-sidecar-manifest.json"
        _copy_publication_file(manifest_path, old_manifest_backup)

    installed_sidecars = []
    root_swapped = False
    manifest_touched = False
    try:
        # Claim both generations before installing any new file. If the process
        # is killed mid-sidecar loop, a retry can distinguish the partially
        # installed immutable generation from an unowned user collision.
        if prepared_transition_manifest is not None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            _validate_publication_action_destination(manifest_path, output_parent)
            _replace_publication_file(prepared_transition_manifest, manifest_path)
            manifest_touched = True

        _publication_phase_checkpoint("after_transition_manifest")

        for action in sidecar_actions:
            destination = action.destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            _validate_publication_action_destination(destination, output_parent)
            if destination.exists() or destination.is_symlink():
                raise RuntimeError(
                    f"Immutable sidecar destination appeared during publication: {destination}"
                )
            _replace_publication_file(action.prepared_source, destination)
            installed_sidecars.append(destination)

        _publication_phase_checkpoint("after_sidecars")

        _replace_publication_file(prepared_final, final_path)
        root_swapped = True

        _publication_phase_checkpoint("after_root")

        if prepared_final_manifest is not None:
            _replace_publication_file(prepared_final_manifest, manifest_path)
            manifest_touched = True
        elif manifest_path.exists():
            _unlink_publication_file(manifest_path)
            manifest_touched = True

        _publication_phase_checkpoint("after_final_manifest")
    except BaseException as original_exc:
        rollback_errors = []
        if root_swapped:
            try:
                if old_final_backup is not None:
                    _replace_publication_file(old_final_backup, final_path)
                elif final_path.exists() or final_path.is_symlink():
                    _unlink_publication_file(final_path)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{final_path}: {rollback_exc}")
        if manifest_touched:
            try:
                if old_manifest_backup is not None:
                    _replace_publication_file(old_manifest_backup, manifest_path)
                elif manifest_path.exists() or manifest_path.is_symlink():
                    _unlink_publication_file(manifest_path)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{manifest_path}: {rollback_exc}")
        for destination in reversed(installed_sidecars):
            try:
                if destination.exists() or destination.is_symlink():
                    _unlink_publication_file(destination)
            except BaseException as rollback_exc:
                rollback_errors.append(f"{destination}: {rollback_exc}")
        if diagnostics:
            diagnostics.add_warning(
                f"Rolled back failed unpacked USD publication: {original_exc}"
            )
        if rollback_errors:
            raise RuntimeError(
                "Publication failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from original_exc
        raise


def _remove_stale_sidecars_after_commit(final_path: Path, entries, diagnostics=None) -> None:
    """Best-effort cleanup after the new root and final manifest are durable."""
    emptied: list[Path] = []
    for entry in entries:
        try:
            path = _safe_sidecar_destination(final_path.parent, Path(entry))
            if path.is_file() and not path.is_symlink():
                path.unlink()
                emptied.append(path.parent)
        except Exception as exc:
            if diagnostics:
                diagnostics.add_warning(f"Failed to remove stale sidecar '{entry}': {exc}")
    _remove_emptied_sidecar_generations(final_path, emptied, diagnostics)


def _remove_emptied_sidecar_generations(
    final_path: Path, directories, diagnostics=None
) -> None:
    """Drop generation directories the stale-sidecar sweep just emptied.

    Every export publishes its sidecars under a fresh
    ``textures/<output>/<generation>/`` so a crash leaves either the old root
    with its old generation or the new root with its new one, never a mixture.
    The stale-sidecar sweep removes the superseded generation's files but not
    its directory, so without this pass every re-export to the same path would
    leave one empty directory behind, often inside an ``.rkassets`` bundle.

    Only directories strictly below an owned sidecar root are considered, only
    while genuinely empty, and never the root itself - so a concurrent export's
    live generation and any user file are both untouched.
    """
    output_parent = final_path.parent
    roots = {output_parent / name for name in _OWNED_SIDECAR_DIRECTORIES}
    for directory in sorted(set(directories), key=lambda path: len(path.parts), reverse=True):
        current = directory
        while current not in roots and current != output_parent:
            if current.is_symlink() or not current.is_dir():
                break
            try:
                if any(current.iterdir()):
                    break
                current.rmdir()
            except OSError as exc:
                if diagnostics:
                    diagnostics.add_warning(
                        f"Failed to remove emptied sidecar directory '{current}': {exc}"
                    )
                break
            current = current.parent


def _cleanup_publication_transaction_dir(transaction_dir: Path, diagnostics=None) -> None:
    root = transaction_dir.parent
    try:
        if (
            root.name != _PUBLICATION_TRANSACTION_DIRECTORY
            or root.is_symlink()
            or transaction_dir.is_symlink()
            or transaction_dir.resolve().parent != root.resolve()
        ):
            raise RuntimeError(f"Refusing unsafe transaction cleanup: {transaction_dir}")
        if transaction_dir.exists():
            shutil.rmtree(transaction_dir)
        root.rmdir()
    except OSError:
        # The root legitimately remains while another export transaction uses
        # it. A leftover transaction child is reported below only on rmtree
        # failure.
        if transaction_dir.exists() and diagnostics:
            diagnostics.add_warning(
                f"Failed to remove publication transaction directory '{transaction_dir}'."
            )
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(str(exc))


def _output_sidecar_manifest_path(final_path: Path) -> Path:
    """Return a manifest path uniquely tied to the complete output filename."""
    return output_sidecar_manifest_path(final_path)


def _sidecars_owned_by_other_outputs(final_path: Path) -> set[str]:
    """Return paths protected by another output's ownership manifest."""
    manifest_directory = final_path.parent / _SIDECAR_MANIFEST_DIRECTORY
    current_manifest = _output_sidecar_manifest_path(final_path)
    protected = set()
    if manifest_directory.is_symlink():
        raise RuntimeError(
            f"Refusing symlinked sidecar manifest directory: {manifest_directory}"
        )
    if not manifest_directory.is_dir():
        return protected

    for manifest_path in manifest_directory.glob("*.json"):
        if manifest_path == current_manifest:
            continue
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError(f"Invalid sidecar ownership manifest: {manifest_path}")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"Could not read sidecar ownership manifest '{manifest_path}': {exc}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _SIDECAR_MANIFEST_SCHEMA_VERSION
            or payload.get("output") != manifest_path.stem
            or not isinstance(payload.get("sidecars"), list)
        ):
            raise RuntimeError(f"Invalid sidecar ownership manifest: {manifest_path}")
        entries = set()
        for entry in payload["sidecars"]:
            try:
                relative = validate_sidecar_relative_path(entry)
            except SidecarManifestError as exc:
                raise RuntimeError(str(exc)) from exc
            relative_path = relative.as_posix()
            if relative_path in entries:
                raise RuntimeError(
                    f"Duplicate sidecar ownership entry: {manifest_path}"
                )
            entries.add(relative_path)
        protected.update(entries)
    return protected


def _build_export_kwargs(settings, *, output_path: str, root_prim_path: str) -> dict:
    """Build arguments using only the Blender 5.2 ``wm.usd_export`` API.

    Blender's Preview Surface network is generated because it carries the
    material assignments; the material rewrite then deletes it from every
    translated material. Native MaterialX is disabled because the rewrite
    authors the RealityKit MaterialX network. Regular external images use ``KEEP`` so Blender does not copy
    them into a native ``textures`` sidecar. Packed/generated images require
    ``NEW``: Blender 5.2's ``KEEP`` mode still materializes those pixels but can
    author a broken bare asset path. We never unpack/repack user datablocks.
    """
    export_custom_properties = bool(getattr(settings, "export_custom_properties", True))
    return {
        'filepath': output_path,
        'check_existing': False,
        'filter_glob': '*.usd;*.usda;*.usdc',
        'selected_objects_only': bool(getattr(settings, "selected_objects_only", False)),
        'export_animation': bool(getattr(settings, "export_animation", False)),
        # Literals below are fixed export policy, not user settings.
        'incremental_frames': 0,
        # RealityKit/RCP3 do not import Blender's raw curve, point-cloud, or
        # hair schemas. These are policy constants, not user settings: keeping
        # them false prevents a doomed export before composed-stage preflight.
        'export_hair': False,
        'export_uvmaps': True,
        'rename_uvmaps': True,
        'export_mesh_colors': True,
        'export_normals': True,
        'export_materials': True,
        'export_subdivision': getattr(settings, "export_subdivision", 'BEST_MATCH'),
        'export_armatures': bool(getattr(settings, "export_armatures", True)),
        'only_deform_bones': bool(getattr(settings, "only_deform_bones", False)),
        'export_shapekeys': bool(getattr(settings, "export_shapekeys", True)),
        'use_instancing': bool(getattr(settings, "use_instancing", True)),
        'evaluation_mode': getattr(settings, "evaluation_mode", 'RENDER'),
        'generate_preview_surface': True,
        'generate_materialx_network': False,
        'convert_orientation': True,
        'export_global_forward_selection': REALITYKIT_USD_EXPORT_FORWARD_AXIS,
        'export_global_up_selection': REALITYKIT_UP_AXIS,
        'export_textures_mode': _native_texture_export_mode(),
        'overwrite_textures': False,
        'relative_paths': True,
        # Translate, rotate, scale, on their own Xform above each mesh: the
        # layout the post-process reads object names and transforms from.
        'xform_op_mode': 'TRS',
        'root_prim_path': root_prim_path,
        'export_custom_properties': export_custom_properties,
        'custom_properties_namespace': getattr(settings, "custom_properties_namespace", "userProperties")
        if export_custom_properties
        else "",
        'accessibility_label': "",
        'accessibility_description': "",
        'author_blender_name': bool(getattr(settings, "author_blender_name", True))
        if export_custom_properties
        else False,
        'allow_unicode': bool(getattr(settings, "allow_unicode", True)),
        'convert_world_material': False,
        'export_meshes': True,
        'export_lights': False,
        'export_cameras': False,
        'export_curves': False,
        'export_points': False,
        'export_volumes': False,
        'triangulate_meshes': bool(getattr(settings, "triangulate_meshes", False)),
        'quad_method': getattr(settings, "quad_method", 'SHORTEST_DIAGONAL'),
        'ngon_method': _ngon_method_for_usd_export(getattr(settings, "ngon_method", "BEAUTY")),
        'merge_parent_xform': False,
        'convert_scene_units': REALITYKIT_SCENE_UNITS,
        'meters_per_unit': REALITYKIT_METERS_PER_UNIT,
    }


def _native_texture_export_mode() -> str:
    """Choose the safe Blender 5.2 texture mode without mutating images."""
    images = getattr(getattr(bpy, "data", None), "images", ())
    for image in images:
        if getattr(image, "packed_file", None):
            return "NEW"
        if str(getattr(image, "source", "")).upper() == "GENERATED":
            return "NEW"
    return "KEEP"


def _validate_export_operator_contract(operator, kwargs: dict) -> None:
    """Require the live USD operator to support every argument we depend on."""
    try:
        valid_props = {prop.identifier for prop in operator.get_rna_type().properties}
    except Exception as exc:
        raise RuntimeError(
            "Unable to inspect Blender 5.2 USD export operator properties."
        ) from exc

    missing_contract = sorted(_REQUIRED_USD_EXPORT_PROPERTIES - valid_props)
    unsupported = sorted(set(kwargs) - valid_props)
    if missing_contract or unsupported:
        details = []
        if missing_contract:
            details.append(f"missing required properties: {', '.join(missing_contract)}")
        if unsupported:
            details.append(f"unsupported arguments: {', '.join(unsupported)}")
        raise RuntimeError(
            "Incompatible Blender USD export operator contract (" + "; ".join(details) + ")."
        )


def _invoke_usd_export(operator, kwargs: dict, diagnostics=None) -> set[str]:
    """Invoke Blender's USD exporter and turn reports into actionable failures."""
    reports_before = _read_window_manager_reports()
    try:
        _validate_export_operator_contract(operator, kwargs)
        result = set(operator(**kwargs) or ())
    except Exception as exc:
        reports = _new_operator_reports(reports_before, _read_window_manager_reports())
        report_detail = "; ".join(message for _levels, message in reports)
        suffix = f" Reports: {report_detail}" if report_detail else ""
        message = f"Blender 5.2 USD export operator failed: {exc}{suffix}"
        if diagnostics:
            diagnostics.add_error(message)
        raise RuntimeError(message) from exc

    reports = _new_operator_reports(reports_before, _read_window_manager_reports())
    report_errors = []
    for levels, message in reports:
        if any(level.startswith("ERROR") for level in levels):
            report_errors.append(message)
        elif "WARNING" in levels and diagnostics:
            diagnostics.add_warning(f"Blender USD export: {message}")

    if report_errors:
        message = "Blender 5.2 USD export operator reported errors: " + "; ".join(report_errors)
        if diagnostics:
            diagnostics.add_error(message)
        raise RuntimeError(message)

    if "FINISHED" not in result:
        status = ", ".join(sorted(result)) if result else "no status"
        message = f"Blender 5.2 USD export operator did not finish (reported: {status})."
        if diagnostics:
            diagnostics.add_error(message)
        raise RuntimeError(message)
    return result


def _read_window_manager_reports() -> tuple[tuple[frozenset[str], str], ...]:
    """Snapshot Blender 5.2 WindowManager reports without clearing them."""
    try:
        reports = bpy.context.window_manager.reports
    except Exception:
        return ()

    snapshot = []
    for report in reports:
        try:
            raw_levels = report.type
            if isinstance(raw_levels, str):
                raw_levels = {raw_levels}
            levels = frozenset(str(level) for level in raw_levels)
            message = str(report.message)
        except Exception:
            continue
        snapshot.append((levels, message))
    return tuple(snapshot)


def _new_operator_reports(before, after):
    """Return reports appended by one operator call."""
    if len(after) >= len(before) and after[:len(before)] == before:
        return after[len(before):]
    return after
