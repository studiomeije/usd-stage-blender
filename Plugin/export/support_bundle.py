"""
Support bundle creation for USD Stage for Blender.

Bundles are redacted by default and are intended to be safe first-response
artifacts for user bug reports. Source `.blend` files and exported assets are
included only when explicitly requested.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .sidecar_manifest import (
    SidecarManifestError,
    read_output_sidecar_manifest,
    validate_owned_sidecar_files,
)


_TEXT_LOG_LIMIT_LINES = 2000
_TOOL_PROBE_TIMEOUT_SECONDS = 5
_TOOL_OUTPUT_LIMIT = 4096
_APPLE_SDKS = (
    "xros",
    "xrsimulator",
    "macosx",
    "iphoneos",
    "iphonesimulator",
    "appletvos",
    "appletvsimulator",
)
_PATH_KEYS = {
    "filepath",
    "blend_file",
    "export_path",
    "log_path",
    "diagnostics_path",
    "usdzip_path",
    "bake_ibl_filepath",
    "background_job_dir",
}


@dataclass(frozen=True)
class _BoundBundleFile:
    """A required source pinned to the validated regular-file inode."""

    handle: BinaryIO
    source: Path
    archive_path: str


def collect_environment(context=None) -> dict:
    """Collect runtime metadata for diagnostics and support reports."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "process_id": os.getpid(),
    }

    try:
        import bpy

        blender_info = {
            "version": ".".join(str(v) for v in bpy.app.version),
            "version_string": getattr(bpy.app, "version_string", ""),
            "binary_path": getattr(bpy.app, "binary_path", ""),
            "background": bool(getattr(bpy.app, "background", False)),
            "build_branch": _decode_blender_build_value(
                getattr(bpy.app, "build_branch", None)
            ),
            "build_commit_hash": _decode_blender_build_value(
                getattr(bpy.app, "build_hash", None)
            ),
            "build_commit_date": _decode_blender_build_value(
                getattr(bpy.app, "build_commit_date", None)
            ),
            "build_platform": _decode_blender_build_value(
                getattr(bpy.app, "build_platform", None)
            ),
            "build_type": _decode_blender_build_value(
                getattr(bpy.app, "build_type", None)
            ),
        }
        blender_info["gpu"] = _collect_blender_gpu_info(bpy, context or bpy.context)
        info["blender"] = blender_info
        info["blend_file"] = getattr(bpy.data, "filepath", "") or None
    except Exception:
        pass

    info["apple_toolchain"] = _collect_apple_toolchain()

    try:
        from ..core.version import get_manifest_info

        manifest = get_manifest_info()
        info["addon"] = {
            "name": manifest.get("name", "USD Stage for Blender"),
            "version": str(manifest.get("version", "unknown")),
            "blender_required": str(manifest.get("blender_version_min", "")),
        }
    except Exception:
        info["addon"] = {"name": "USD Stage for Blender", "version": "unknown"}

    try:
        from ..core import paths
        from ..manifest import materialx_nodes

        manifest_path = materialx_nodes.get_manifest_path()
        info["resources"] = {
            "addon_root": str(paths.addon_root()),
            "runner_path": str(Path(__file__).resolve().parents[1] / "api" / "runner.py"),
            "manifest_path": str(manifest_path),
            "manifest_exists": manifest_path.exists(),
            "manifest_schema_version": getattr(materialx_nodes, "MANIFEST_SCHEMA_VERSION", None),
            "nodegroups_asset_path": str(paths.nodegroups_asset_path()),
            "nodegroups_asset_exists": paths.nodegroups_asset_path().exists(),
        }
    except Exception as exc:
        info["resources_error"] = str(exc)

    try:
        import bpy
        from .. import prefs as addon_prefs

        prefs = addon_prefs.get_preferences(context or bpy.context)
        if prefs is not None:
            info["preferences"] = {
                "usdzip_path": getattr(prefs, "usdzip_path", ""),
                "last_export_settings_json_valid": _json_is_valid(
                    getattr(prefs, "last_export_settings_json", "")
                ),
                "last_export_paths_json_valid": _json_is_valid(
                    getattr(prefs, "last_export_paths_json", "")
                ),
            }
    except Exception as exc:
        info["preferences_error"] = str(exc)

    return _json_safe(info)


def collect_scene_snapshot(context=None) -> dict:
    """Collect compact scene and export settings metadata."""
    try:
        import bpy

        context = context or bpy.context
        scene = context.scene
        settings = getattr(scene, "usd_stage_export_settings", None)
        materials = set()
        for obj in scene.objects:
            for slot in getattr(obj, "material_slots", []):
                if slot.material:
                    materials.add(slot.material.name)
        snapshot = {
            "file": getattr(bpy.data, "filepath", "") or None,
            "scene": scene.name,
            "frame_range": [scene.frame_start, scene.frame_end],
            "fps": scene.render.fps,
            "unit_system": scene.unit_settings.system,
            "unit_scale": scene.unit_settings.scale_length,
            "object_count": len(scene.objects),
            "selected_object_count": len(getattr(context, "selected_objects", []) or []),
            "material_count": len(materials),
        }
        if settings is not None:
            snapshot["export_settings"] = _serialize_settings(settings)
            snapshot["export_profile"] = _collect_export_profile(settings)
        return _json_safe(snapshot)
    except Exception as exc:
        return {"error": str(exc)}


def collect_validation_snapshot(context=None) -> dict:
    """Collect material validation data without failing bundle creation."""
    try:
        import bpy
        from ..nodes import validate as rk_validate

        context = context or bpy.context
        settings = getattr(
            getattr(context, "scene", None),
            "usd_stage_export_settings",
            None,
        )
        clamp_specular_tint = bool(
            getattr(settings, "clamp_specular_tint", False)
        )
        materials = rk_validate.collect_scene_materials(context)
        entries = []
        total_errors = 0
        total_warnings = 0
        for material in materials:
            result = rk_validate.validate_material(
                material,
                strict=True,
                clamp_specular_tint=clamp_specular_tint,
            )
            total_errors += len(result.get("errors", []))
            total_warnings += len(result.get("warnings", []))
            entries.append({
                "name": result.get("material", material.name),
                "ok": result.get("ok", False),
                "errors": result.get("errors", []),
                "warnings": result.get("warnings", []),
            })
        return _json_safe({
            "ok": total_errors == 0,
            "error_count": total_errors,
            "warning_count": total_warnings,
            "clamp_specular_tint": clamp_specular_tint,
            "materials": entries,
        })
    except Exception as exc:
        return {"error": str(exc)}


def collect_asset_dependency_snapshot(context=None) -> dict:
    """Collect source asset dependencies without failing bundle creation."""
    try:
        from . import asset_preflight

        return asset_preflight.collect_asset_dependency_snapshot(context)
    except Exception as exc:
        return {"error": str(exc)}


def create_support_bundle(
    *,
    context=None,
    blend_file: str | None = None,
    export_path: str | None = None,
    diagnostics_path: str | None = None,
    job_dir: str | None = None,
    bundle_output: str | None = None,
    include_output: bool = False,
    include_blend: bool = False,
    full_log: bool = False,
    redact: bool = True,
) -> dict:
    """Create a redacted support bundle ZIP and return metadata."""
    export = Path(export_path).expanduser() if export_path else None
    blend = Path(blend_file).expanduser() if blend_file else None
    job = Path(job_dir).expanduser() if job_dir else None

    if diagnostics_path:
        diagnostics = Path(diagnostics_path).expanduser()
    elif export:
        diagnostics = export.with_suffix(".diagnostics.json")
    else:
        diagnostics = None

    output_dir = None
    if bundle_output:
        bundle_path = Path(bundle_output).expanduser()
        output_dir = bundle_path.parent
    elif export:
        output_dir = export.parent
    elif blend:
        output_dir = blend.parent
    elif job:
        output_dir = job.parent
    else:
        output_dir = Path.cwd()

    stem = (blend.stem if blend else export.stem if export else "scene") or "scene"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle_name = f"USDStage-support-{stem}-{stamp}"
    bundle_path = (
        Path(bundle_output).expanduser()
        if bundle_output
        else output_dir / f"{bundle_name}.zip"
    )
    bake_export_bundle = _is_bake_export_bundle(job, diagnostics)

    output_manifest = None
    owned_sidecars = ()
    if include_output:
        if export is None:
            raise RuntimeError("An export path is required when including output")
        if export.is_symlink():
            raise RuntimeError(f"Refusing symlinked export output: {export}")
        if not export.exists() or not export.is_file():
            raise RuntimeError(f"Export output is missing or invalid: {export}")
        try:
            output_manifest = read_output_sidecar_manifest(export)
            owned_sidecars = validate_owned_sidecar_files(export, output_manifest)
        except SidecarManifestError as exc:
            raise RuntimeError(
                f"Refusing unsafe export sidecar ownership: {exc}"
            ) from exc

    bundle_inputs = [blend, export, diagnostics, job]
    if job is not None:
        bundle_inputs.extend(
            job / filename for filename in ("status.json", "settings.json", "log.txt")
        )
    if output_manifest is not None:
        bundle_inputs.append(output_manifest.path)
    bundle_inputs.extend(sidecar.path for sidecar in owned_sidecars)
    _validate_bundle_output_path(bundle_path, bundle_inputs, job)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    bound_output_files = _bind_required_output_files(
        export,
        output_manifest,
        owned_sidecars,
    ) if include_output and export else ()

    tokens = _build_redaction_tokens(blend, export)
    files: list[dict[str, str]] = []

    def add_json(zf: zipfile.ZipFile, rel: str, data: Any) -> None:
        payload = _redact_json(data, tokens) if redact else _json_safe(data)
        zf.writestr(f"{bundle_name}/{rel}", json.dumps(payload, indent=2, default=str))
        files.append({"path": rel, "source": "generated"})

    def add_text(zf: zipfile.ZipFile, rel: str, text: str) -> None:
        payload = _redact_text(text, tokens) if redact else text
        zf.writestr(f"{bundle_name}/{rel}", payload)
        files.append({"path": rel, "source": "generated"})

    def add_file(zf: zipfile.ZipFile, source: Path, rel: str, *, redact_file: bool = False) -> None:
        if not source or not source.exists() or not source.is_file():
            return
        if redact_file:
            text = source.read_text(errors="replace")
            if not full_log:
                lines = text.splitlines()
                if len(lines) > _TEXT_LOG_LIMIT_LINES:
                    text = "\n".join(lines[-_TEXT_LOG_LIMIT_LINES:])
            add_text(zf, rel, text)
            files[-1]["source"] = str(source)
            return
        zf.write(source, f"{bundle_name}/{rel}")
        files.append({"path": rel, "source": str(source)})

    def add_bound_file(zf: zipfile.ZipFile, bound: _BoundBundleFile) -> None:
        bound.handle.seek(0)
        with zf.open(f"{bundle_name}/{bound.archive_path}", "w") as member:
            shutil.copyfileobj(bound.handle, member, length=1024 * 1024)
        files.append({"path": bound.archive_path, "source": str(bound.source)})

    try:
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{bundle_path.name}.",
            suffix=".tmp",
            dir=str(bundle_path.parent),
        )
    except Exception:
        _close_bound_files(bound_output_files)
        raise
    temporary_path = Path(temporary_name)
    try:
        temporary_identity = os.fstat(temporary_fd)
        temporary_file = os.fdopen(temporary_fd, "w+b")
        temporary_fd = -1
        with temporary_file:
            with zipfile.ZipFile(temporary_file, "w", zipfile.ZIP_DEFLATED) as zf:
                add_text(
                    zf,
                    "README.txt",
                    "USD Stage for Blender support bundle. Redacted by default. "
                    "Attach this ZIP to a support request with a short description "
                    "of the failure.\n",
                )
                add_json(zf, "environment/version.json", collect_environment(context))
                add_json(zf, "environment/scene-info.json", collect_scene_snapshot(context))
                add_json(zf, "diagnostics/assets.json", collect_asset_dependency_snapshot(context))
                if not bake_export_bundle:
                    add_json(zf, "diagnostics/validate.json", collect_validation_snapshot(context))

                if diagnostics and diagnostics.exists():
                    add_file(
                        zf,
                        diagnostics,
                        "diagnostics/export.diagnostics.json",
                        redact_file=redact,
                    )

                if job and job.exists():
                    add_file(zf, job / "status.json", "job/status.json", redact_file=redact)
                    add_file(
                        zf,
                        job / "settings.json",
                        "job/settings.redacted.json",
                        redact_file=redact,
                    )
                    add_file(zf, job / "log.txt", "job/log.redacted.txt", redact_file=redact)

                for bound in bound_output_files:
                    add_bound_file(zf, bound)

                if include_blend and blend and blend.exists():
                    add_file(zf, blend, f"source/{blend.name}")

                manifest = {
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "bundle": bundle_name,
                    "redacted": redact,
                    "include_output": include_output,
                    "include_blend": include_blend,
                    "sources": _redact_json({
                        "blend_file": str(blend) if blend else None,
                        "export_path": str(export) if export else None,
                        "diagnostics_path": str(diagnostics) if diagnostics else None,
                        "job_dir": str(job) if job else None,
                    }, tokens) if redact else {
                        "blend_file": str(blend) if blend else None,
                        "export_path": str(export) if export else None,
                        "diagnostics_path": str(diagnostics) if diagnostics else None,
                        "job_dir": str(job) if job else None,
                    },
                    "files": files,
                }
                add_json(zf, "manifest.json", manifest)

            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        _validate_temporary_file_identity(temporary_path, temporary_identity)
        # Re-check after bundle construction so a late link swap cannot turn
        # atomic publication into an input overwrite.
        _validate_bundle_output_path(bundle_path, bundle_inputs, job)
        os.replace(temporary_path, bundle_path)
        _fsync_directory(bundle_path.parent)
    except Exception:
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        _close_bound_files(bound_output_files)

    return {
        "support_bundle_path": str(bundle_path),
        "file_count": len(files),
        "redacted": redact,
        "included_output": include_output,
        "included_blend": include_blend,
    }


def _bind_required_output_files(
    export: Path,
    output_manifest,
    owned_sidecars,
) -> tuple[_BoundBundleFile, ...]:
    """Pin validated output files before slow diagnostics collection begins."""
    entries = [(export, f"output/{export.name}")]
    if output_manifest is not None:
        entries.append((
            output_manifest.path,
            f"output/.usdstage_sidecars/{output_manifest.path.name}",
        ))
    entries.extend(
        (sidecar.path, f"output/{sidecar.relative_path.as_posix()}")
        for sidecar in owned_sidecars
    )

    root = export.parent.resolve(strict=True)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd = -1
    if _DIR_FD_SUPPORTED:
        try:
            root_fd = os.open(root, directory_flags)
        except OSError as exc:
            raise RuntimeError(f"Could not bind export directory '{root}': {exc}") from exc

    bound: list[_BoundBundleFile] = []
    try:
        for source, archive_path in entries:
            try:
                relative = source.relative_to(export.parent)
            except ValueError as exc:
                raise RuntimeError(
                    f"Required output source escapes the export directory: {source}"
                ) from exc
            if _DIR_FD_SUPPORTED:
                handle = _open_regular_file_beneath(root_fd, relative, source)
            else:
                handle = _open_regular_file_beneath_by_path(root, relative, source)
            bound.append(_BoundBundleFile(handle, source, archive_path))
    except Exception:
        _close_bound_files(bound)
        raise
    finally:
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
    return tuple(bound)


#: Opening files relative to a pinned directory descriptor is how the bundle
#: refuses a symlink swapped in between the check and the read. Windows has no
#: dir_fd; there the same guarantee is approximated by lstat-ing every
#: component and comparing file identities after the open.
_DIR_FD_SUPPORTED = os.open in getattr(os, "supports_dir_fd", set())


def _open_regular_file_beneath_by_path(
    root: Path,
    relative: Path,
    display_path: Path,
) -> BinaryIO:
    """Path-based twin of ``_open_regular_file_beneath`` for platforms without dir_fd."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"Refusing unsafe required output path: {display_path}")
    try:
        expected = os.lstat(display_path)
    except OSError as exc:
        raise RuntimeError(
            f"Required output source disappeared: {display_path}"
        ) from exc
    if not stat.S_ISREG(expected.st_mode):
        raise RuntimeError(f"Required output source is not a regular file: {display_path}")
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(
                f"Required output source path crosses a symlink or non-directory: {display_path}"
            )
    file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(str(root / relative), file_flags)
    try:
        opened = os.fstat(file_fd)
        again = os.lstat(display_path)
        identities = {
            (expected.st_dev, expected.st_ino),
            (opened.st_dev, opened.st_ino),
            (again.st_dev, again.st_ino),
        }
        if (
            len(identities) != 1
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(again.st_mode)
        ):
            raise RuntimeError(
                f"Required output source changed while bundling: {display_path}"
            )
    except Exception:
        os.close(file_fd)
        raise
    return os.fdopen(file_fd, "rb")


def _open_regular_file_beneath(
    root_fd: int,
    relative: Path,
    display_path: Path,
) -> BinaryIO:
    """Open a required file under a pinned root without following any symlink."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise RuntimeError(f"Refusing unsafe required output path: {display_path}")

    try:
        expected = os.lstat(display_path)
    except OSError as exc:
        raise RuntimeError(
            f"Required output source disappeared: {display_path}"
        ) from exc
    if not stat.S_ISREG(expected.st_mode):
        raise RuntimeError(f"Required output source is not a regular file: {display_path}")

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = os.dup(root_fd)
    file_fd = -1
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=current_fd)
        opened = os.fstat(file_fd)
        current = os.lstat(display_path)
        identities = {
            (expected.st_dev, expected.st_ino),
            (opened.st_dev, opened.st_ino),
            (current.st_dev, current.st_ino),
        }
        if (
            len(identities) != 1
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
        ):
            raise RuntimeError(
                f"Required output source changed while being secured: {display_path}"
            )
        handle = os.fdopen(file_fd, "rb")
        file_fd = -1
        return handle
    except OSError as exc:
        raise RuntimeError(
            f"Could not securely open required output source '{display_path}': {exc}"
        ) from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(current_fd)


def _close_bound_files(bound_files) -> None:
    for bound in bound_files:
        try:
            bound.handle.close()
        except OSError:
            pass


def _validate_temporary_file_identity(path: Path, expected: os.stat_result) -> None:
    """Require the random staging name to still refer to the opened temp inode."""
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise RuntimeError(f"Support bundle temporary file disappeared: {path}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise RuntimeError(f"Support bundle temporary file was replaced: {path}")


def _validate_bundle_output_path(
    bundle_path: Path,
    inputs: list[Path | None],
    job_dir: Path | None,
) -> None:
    """Reject aliases between the destination and every support-bundle input."""
    if bundle_path.is_symlink():
        raise RuntimeError(f"Refusing symlinked support bundle output: {bundle_path}")
    if bundle_path.exists() and not bundle_path.is_file():
        raise RuntimeError(f"Support bundle output is not a file: {bundle_path}")

    resolved_bundle = bundle_path.resolve(strict=False)
    bundle_identity = _canonical_path_identity(bundle_path)
    for source in inputs:
        if source is None:
            continue
        source = Path(source)
        source_identity = _canonical_path_identity(source)
        if (
            resolved_bundle == source.resolve(strict=False)
            or bundle_identity == source_identity
            or bundle_identity[:len(source_identity)] == source_identity
        ):
            raise RuntimeError(
                f"Support bundle output collides with an input: {source}"
            )
        if _path_lexists(bundle_path) and _path_lexists(source):
            try:
                if os.path.samefile(bundle_path, source):
                    raise RuntimeError(
                        f"Support bundle output aliases an input: {source}"
                    )
            except OSError:
                # Canonical path comparison above still covers missing and
                # dangling paths; samefile is an additional hard-link check.
                pass

    if job_dir is not None:
        resolved_job = job_dir.resolve(strict=False)
        job_identity = _canonical_path_identity(job_dir)
        inside_job_identity = bundle_identity[:len(job_identity)] == job_identity
        if (
            resolved_bundle == resolved_job
            or resolved_bundle.is_relative_to(resolved_job)
            or inside_job_identity
        ):
            raise RuntimeError(
                f"Support bundle output cannot be inside the job input directory: {job_dir}"
            )


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _canonical_path_identity(path: Path) -> tuple[str, ...]:
    """Normalize path components for case-insensitive, Unicode-normalized filesystems."""
    return tuple(
        unicodedata.normalize(
            "NFC",
            unicodedata.normalize("NFC", part).casefold(),
        )
        for part in path.resolve(strict=False).parts
    )


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync after the atomically published ZIP."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _serialize_settings(settings) -> dict:
    try:
        from ..api.commands._settings_common import INTERNAL_KEYS
    except Exception:
        INTERNAL_KEYS = {"rna_type", "name"}

    data = {}
    for prop in settings.bl_rna.properties:
        key = prop.identifier
        if key in INTERNAL_KEYS:
            continue
        try:
            data[key] = getattr(settings, key)
        except Exception:
            continue
    return data


def _collect_export_profile(settings) -> dict:
    """Describe whether the active settings match the strict OS 27 profile."""
    try:
        from ..api.commands._settings_common import (
            REALITYKIT_OS27_PROFILE_NAME,
            realitykit_os27_profile_deviations,
        )
        from ..apple_contract import (
            REALITYKIT_FORWARD_AXIS,
            REALITYKIT_METERS_PER_UNIT,
            REALITYKIT_SCENE_UNITS,
            REALITYKIT_UP_AXIS,
        )

        deviations = realitykit_os27_profile_deviations(settings)
        return {
            "name": REALITYKIT_OS27_PROFILE_NAME,
            "spatial_contract": {
                "convert_orientation": True,
                "forward_axis": REALITYKIT_FORWARD_AXIS,
                "up_axis": REALITYKIT_UP_AXIS,
                "scene_units": REALITYKIT_SCENE_UNITS,
                "meters_per_unit": REALITYKIT_METERS_PER_UNIT,
                "relative_paths": True,
                "export_meshes": True,
                "export_uvmaps": True,
                "rename_uvmaps": True,
                "export_normals": True,
            },
            "strict_defaults_active": not deviations,
            "deviations": deviations,
        }
    except Exception as exc:
        return {"name": "REALITYKIT_OS27", "error": str(exc)}


def _collect_blender_gpu_info(bpy, context) -> dict:
    """Collect Blender's selected and active GPU backends when available.

    Blender background processes often have no initialized GPU context.  In
    that case the preference and render-engine values are still useful and we
    intentionally avoid forcing GPU initialization solely for diagnostics.
    """
    result: dict[str, Any] = {}

    try:
        system = context.preferences.system
        result["backend_preference"] = getattr(system, "gpu_backend", None)
    except Exception as exc:
        result["preference_error"] = str(exc)

    try:
        result["render_engine"] = context.scene.render.engine
    except Exception as exc:
        result["render_engine_error"] = str(exc)

    try:
        cycles_addon = context.preferences.addons.get("cycles")
        cycles_preferences = getattr(cycles_addon, "preferences", None)
        if cycles_preferences is not None:
            result["cycles_compute_device_type"] = getattr(
                cycles_preferences,
                "compute_device_type",
                None,
            )
    except Exception as exc:
        result["cycles_error"] = str(exc)

    if bool(getattr(bpy.app, "background", False)):
        result["active_context"] = "unavailable-in-background"
        return _json_safe(result)

    try:
        import gpu

        gpu_platform = getattr(gpu, "platform", None)
        getters = {
            "active_backend": "backend_type_get",
            "active_device": "device_type_get",
            "renderer": "renderer_get",
            "vendor": "vendor_get",
            "driver_version": "version_get",
        }
        for key, getter_name in getters.items():
            getter = getattr(gpu_platform, getter_name, None)
            if callable(getter):
                try:
                    result[key] = getter()
                except Exception as exc:
                    result[f"{key}_error"] = str(exc)
    except Exception as exc:
        result["active_context_error"] = str(exc)

    return _json_safe(result)


def _decode_blender_build_value(value):
    """Normalize Blender's byte-valued build metadata for readable JSON."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_tool_probe(args: list[str]) -> dict:
    """Run a bounded, read-only tool probe and return JSON-safe metadata."""
    command = [str(arg) for arg in args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_TOOL_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return {"available": False, "command": command, "error": "not found"}
    except subprocess.TimeoutExpired:
        return {"available": False, "command": command, "error": "timed out"}
    except Exception as exc:
        return {"available": False, "command": command, "error": str(exc)}

    output = "\n".join(
        part.strip()
        for part in (completed.stdout or "", completed.stderr or "")
        if part.strip()
    )
    if len(output) > _TOOL_OUTPUT_LIMIT:
        output = output[:_TOOL_OUTPUT_LIMIT] + "\n<truncated>"
    return {
        "available": completed.returncode == 0,
        "command": command,
        "exit_code": completed.returncode,
        "output": output,
    }


def _collect_reality_composer_pro() -> dict:
    candidates = (
        Path("/Applications/RealityComposerPro.app"),
        Path("/Applications/Reality Composer Pro.app"),
    )
    for app_path in candidates:
        info_path = app_path / "Contents" / "Info.plist"
        if not info_path.is_file():
            continue
        result: dict[str, Any] = {
            "installed": True,
            "path": str(app_path),
        }
        try:
            with info_path.open("rb") as stream:
                plist = plistlib.load(stream)
            result.update({
                "version": plist.get("CFBundleShortVersionString"),
                "build": plist.get("CFBundleVersion"),
                "platform_version": plist.get("DTPlatformVersion"),
                "minimum_system_version": plist.get("LSMinimumSystemVersion"),
            })
        except Exception as exc:
            result["error"] = str(exc)
        return _json_safe(result)

    return {
        "installed": False,
        "searched_paths": [str(path) for path in candidates],
    }


def _collect_apple_toolchain() -> dict:
    """Capture Xcode, SDK, realitytool, and RCP versions on macOS.

    Support bundles are also generated on Linux and Windows.  Those hosts get
    an explicit not-applicable record and never attempt Apple-only commands.
    """
    if platform.system() != "Darwin":
        return {
            "available": False,
            "reason": "Apple developer tools are available only on macOS",
        }

    xcode = _run_tool_probe(["xcodebuild", "-version"])
    realitytool_path = _run_tool_probe(["xcrun", "--find", "realitytool"])

    sdks = {}
    for sdk in _APPLE_SDKS:
        sdks[sdk] = {
            "version": _run_tool_probe(["xcrun", "--sdk", sdk, "--show-sdk-version"]),
            "path": _run_tool_probe(["xcrun", "--sdk", sdk, "--show-sdk-path"]),
        }

    return _json_safe({
        "available": True,
        "host": {
            "product_version": _run_tool_probe(["sw_vers", "-productVersion"]),
            "build_version": _run_tool_probe(["sw_vers", "-buildVersion"]),
        },
        "developer_dir": _run_tool_probe(["xcode-select", "-p"]),
        "xcode": xcode,
        "realitytool": {
            "path": realitytool_path,
            # realitytool does not expose a --version option. It ships as part
            # of the selected Xcode distribution, so record that bundle's
            # exact version without launching compiler or simulator services.
            "version_source": "selected Xcode distribution",
            "xcode_version": xcode.get("output", ""),
        },
        "reality_composer_pro": _collect_reality_composer_pro(),
        "sdks": sdks,
    })


def _json_is_valid(value: str) -> bool:
    if not value:
        return True
    try:
        json.loads(value)
        return True
    except Exception:
        return False


def _is_bake_export_bundle(job: Path | None, diagnostics: Path | None) -> bool:
    if job:
        settings_path = job / "settings.json"
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        except Exception:
            settings = {}
        export_settings = settings.get("export_settings") or {}
        if "bake_mode" in export_settings:
            return True

    if diagnostics and diagnostics.exists():
        try:
            data = json.loads(diagnostics.read_text(errors="replace"))
        except Exception:
            data = {}
        command = (data.get("export_context") or {}).get("command")
        if command in {"bake_export", "background_bake_export"}:
            return True
    return False


def _build_redaction_tokens(blend: Path | None, export: Path | None) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    home = str(Path.home())
    if home:
        tokens.append((home, "$HOME"))
    if blend and blend.parent:
        tokens.append((str(blend.parent), "$BLEND_DIR"))
    if export and export.parent:
        tokens.append((str(export.parent), "$EXPORT_DIR"))
    return sorted(tokens, key=lambda item: len(item[0]), reverse=True)


def _redact_json(value: Any, tokens: list[tuple[str, str]], key: str | None = None):
    if isinstance(value, dict):
        return {str(k): _redact_json(v, tokens, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json(item, tokens, key) for item in value]
    if isinstance(value, str):
        redacted = _redact_text(value, tokens)
        if key in _PATH_KEYS and redacted:
            return redacted
        return redacted
    return value


def _redact_text(text: str, tokens: list[tuple[str, str]]) -> str:
    redacted = str(text)
    for source, token in tokens:
        variants = [(source, token)]
        escaped_source = json.dumps(source)[1:-1]
        escaped_token = json.dumps(token)[1:-1]
        if escaped_source != source:
            variants.append((escaped_source, escaped_token))
        for candidate, replacement in variants:
            redacted = redacted.replace(candidate, replacement)
    redacted = re.sub(r"(?i)(api[_-]?key|token|secret|password)=\S+", r"\1=<redacted>", redacted)
    return redacted


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
