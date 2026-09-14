"""
USD texture staging utilities.

Ensures referenced assets are copied and paths are made relative for USDZ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import os
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .staging_namespace import (
    output_sidecar_namespace,
    take_native_texture_copies,
)
from .usd_utils import Sdf


TEXTURE_EXTENSIONS = {
    ".avif",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".exr",
    ".hdr",
    ".ktx",
    ".ktx2",
    ".tga",
    ".bmp",
    ".gif",
    ".dds",
    ".webp",
}

_ORIGINAL_FORMAT_BY_EXTENSION = {
    ".avif": "AVIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".tga": "TARGA",
    ".bmp": "BMP",
    ".webp": "WEBP",
}

# Quality for lossy output formats (AVIF/JPEG/WebP) written via imbuf.
_LOSSY_TEXTURE_QUALITY = 90

# Image.file_format enum names whose ImBuf file_type identifier differs.
_IMBUF_FILE_TYPE_BY_FORMAT = {
    "TARGA": "TGA",
}

# Keep the package contract deliberately narrower than Blender's import
# support. These formats are the formats USD Stage for Blender may copy through without
# transcoding for RealityKit/Reality Composer Pro. EXR is intentionally kept
# byte-for-byte: Xcode 27's realitytool and macOS 27 RealityKit both load an
# EXR-backed ShaderGraphMaterial, while converting it to PNG destroys its float
# range. Other recognized image formats are inputs only and must be converted
# to PNG (or to an explicitly requested supported format) before packaging.
#
# AVIF is not here on purpose. The USDZ specification allows it and the file
# Blender writes is valid (ImageIO and Pillow both open it), but Reality
# Composer Pro 3 refuses it on import with "Unhandled
# image format", loose beside a .usdc and inside a .usdz alike. An AVIF source
# is therefore an input that gets converted to PNG.
_APPLE_TEXTURE_OUTPUT_EXTENSIONS = {
    ".exr",
    ".jpg",
    ".jpeg",
    ".png",
}

_NON_FILE_ASSET_SCHEMES = {
    "anon",
    "blob",
    "data",
    "http",
    "https",
    "mem",
}

_CONTENT_HASH_SUFFIX = re.compile(r"^(?P<stem>.+)-[0-9a-f]{64}$")
_MAX_CONTENT_STEM_UTF8_BYTES = 120

# Windows refuses a path longer than MAX_PATH unless both the process manifest
# and the machine's LongPathsEnabled policy opt in, neither of which we can
# assume on an artist's machine. Measured failure, Blender 5.2 on Windows 11:
#
#   [WinError 3] ...\textures\Export.usdc\<32 hex>\
#     Export-StonePaving_low_DefaultMaterial_BaseColor.png
#     -> ...-<64 hex>.png
#
# The copy landed at 215 characters and succeeded; appending the content digest
# took the same path to 280 and os.replace failed. The output directory was an
# unremarkable C:\Users\<name>\Desktop\<folder>, so this is not a pathological
# case - the staging scheme itself spends 118 characters before the texture's
# own name, and the digest spends 65 more.
#
# Windows only. Budgeting every platform to MAX_PATH was the first attempt and
# it was wrong: this repository's own evaluation exports stage 248 characters
# deep on macOS, so a global budget left 8 bytes for the stem and would have
# renamed textures in exports that work today. POSIX keeps the flat cap.
_MAX_STAGED_PATH_CHARS = 259  # MAX_PATH (260) less the terminating NUL
_MIN_CONTENT_STEM_CHARS = 8
_PATH_LENGTH_IS_CAPPED = os.name == "nt"


class _TextureStagingError(RuntimeError):
    """Raised when a texture cannot satisfy the self-contained contract."""


@dataclass
class TextureStagingState:
    usd_path: Path
    usd_dir: Path
    sidecar_namespace: Path
    textures_dir: Path
    texture_override: object
    output_name_prefix: str
    seen_sources: dict = field(default_factory=dict)
    seen_names: dict = field(default_factory=dict)
    seen_fingerprints: dict = field(default_factory=dict)
    superseded_paths: set[Path] = field(default_factory=set)
    # Files Blender's own exporter wrote into <staging>/textures during this
    # attempt. Empty when the native exporter did not run, in which case
    # nothing outside the generation directory is ours to remove.
    native_texture_copies: frozenset[Path] = frozenset()


def create_texture_staging_state(
    usd_path: str | Path,
    settings,
    diagnostics=None,
) -> TextureStagingState:
    # Keep every path calculation in the same canonical namespace as
    # ``usd_assets``.  On macOS, /tmp and /var are symlink aliases for paths
    # below /private; mixing the authored alias with resolved destination paths
    # produces relative references that escape the export tree.
    usd_path = Path(usd_path).resolve()
    usd_dir = usd_path.parent
    sidecar_namespace = output_sidecar_namespace(usd_path)
    return TextureStagingState(
        usd_path=usd_path,
        usd_dir=usd_dir,
        sidecar_namespace=sidecar_namespace,
        textures_dir=usd_dir / "textures" / sidecar_namespace,
        texture_override=_texture_override_settings(settings, diagnostics),
        output_name_prefix=_texture_name_prefix(str(usd_path)),
        # Consumed destructively: one export attempt, one staging pass.
        native_texture_copies=take_native_texture_copies(usd_dir),
    )


def stage_layer_texture_asset(
    authored_path: str,
    resolved_path: str,
    *,
    authoring_layer,
    destination_layer_path: str | Path,
    state: TextureStagingState,
    diagnostics=None,
):
    """Stage one authored layer opinion without composing or overriding it.

    ``Sdf.Layer.Traverse`` includes inactive variant and instance-prototype
    specs. Callers read from the source layer and assign this return value only
    to the export-owned destination layer copy, preserving every authored
    opinion and its original resolver anchor.
    """
    authored_path = str(authored_path or "")
    resolved_path = str(resolved_path or "")
    display_path = authored_path or resolved_path
    if not display_path or not (
        _is_texture_path(authored_path) or _is_texture_path(resolved_path)
    ):
        return None, False

    try:
        source_path = _resolve_layer_texture_source(
            authored_path,
            resolved_path,
            authoring_layer,
            state.usd_dir,
        )
        destination = _stage_texture_source(
            source_path,
            authored_path=authored_path,
            usd_attribute=str(
                getattr(authoring_layer, "identifier", destination_layer_path)
            ),
            state=state,
            diagnostics=diagnostics,
        )
    except _TextureStagingError as exc:
        if diagnostics:
            diagnostics.add_texture_failed(display_path, str(exc))
        raise

    destination_base = Path(destination_layer_path).resolve().parent
    relative_path = Path(
        os.path.relpath(destination, start=destination_base)
    ).as_posix()
    return Sdf.AssetPath(relative_path), True


def finish_texture_staging(
    state: TextureStagingState,
    *,
    stage=None,
    diagnostics=None,
) -> None:
    if stage is not None:
        _remove_unreferenced_superseded_textures(
            stage,
            state.usd_dir,
            state.superseded_paths,
            diagnostics,
        )


def remove_unreferenced_bake_outputs(
    usd_path: str | Path,
    staging_root: str | Path,
    baked_paths,
    diagnostics=None,
) -> tuple[Path, ...]:
    """Remove only bake-source files superseded by final staged references.

    A bake job owns its unique staging directory, but the generic texture
    staging API may also operate beside user-authored source images. Keep this
    cleanup explicit to the bake worker and require every deletion candidate
    to stay below that worker's ``textures`` directory.
    """

    if Sdf is None:
        return ()
    try:
        from pxr import Usd
    except ImportError:
        return ()

    usd_path = Path(usd_path).resolve()
    staging_root = Path(staging_root).resolve()
    if usd_path.parent != staging_root:
        raise _TextureStagingError(
            "bake texture cleanup requires the USD layer inside its owned "
            "staging directory"
        )
    textures_root = staging_root / "textures"
    if textures_root.is_symlink():
        raise _TextureStagingError(
            f"refusing symlinked bake texture directory: {textures_root}"
        )

    stage = Usd.Stage.Open(str(usd_path))
    if stage is None:
        raise _TextureStagingError(
            f"cannot reopen USD stage for bake texture cleanup: {usd_path}"
        )
    referenced_paths = _referenced_texture_paths(stage, staging_root)
    removed = []
    for raw_path in sorted({str(value or "") for value in baked_paths}):
        if not raw_path or raw_path.startswith("//"):
            continue
        candidate = Path(raw_path).resolve()
        try:
            candidate.relative_to(textures_root.resolve())
        except ValueError:
            continue
        if (
            candidate in referenced_paths
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            continue
        candidate.unlink()
        removed.append(candidate)
        if diagnostics:
            diagnostics.add_generated_file(
                "removed_superseded_bake_texture",
                str(candidate),
            )
    return tuple(removed)


def _stage_texture_source(
    source_path: Path,
    *,
    authored_path: str,
    usd_attribute: str,
    state: TextureStagingState,
    diagnostics=None,
) -> Path:
    if not source_path.name:
        raise _TextureStagingError("Texture asset has no filename.")
    if not source_path.is_file():
        message = f"Texture file not found: {source_path}"
        if diagnostics:
            diagnostics.add_texture_failed(str(source_path), message)
        raise _TextureStagingError(message)
    try:
        if source_path.stat().st_size <= 0:
            message = f"Texture file is empty: {source_path}"
            if diagnostics:
                diagnostics.add_texture_failed(str(source_path), message)
            raise _TextureStagingError(message)
    except OSError as exc:
        message = f"Could not inspect texture '{source_path}': {exc}"
        if diagnostics:
            diagnostics.add_texture_failed(str(source_path), message)
        raise _TextureStagingError(message) from exc

    # The dependency pipeline runs twice: source localization, then a final
    # closure pass after material/animation authoring. Never feed a first-pass
    # lossy output through the encoder again. Exact-generation containment and
    # a full filename/bytes hash match are both required before reuse.
    if _is_current_generation_texture(source_path, state.textures_dir):
        return source_path

    source_extension = source_path.suffix.lower()
    if source_extension == ".hdr":
        message = (
            f"Texture '{source_path}' uses Radiance HDR. Automatic conversion to PNG "
            "would destroy HDR/float fidelity; convert it to OpenEXR before export."
        )
        if diagnostics:
            diagnostics.add_texture_failed(str(source_path), message)
        raise _TextureStagingError(message)
    if source_extension == ".exr" and state.texture_override and diagnostics:
        diagnostics.add_warning(
            f"Ignored texture format/resize override for OpenEXR texture '{source_path}' "
            "to preserve HDR/float fidelity."
        )

    effective_override = _effective_texture_override(
        source_path,
        state.texture_override,
    )
    source_key = _source_key(source_path, effective_override)
    existing = state.seen_sources.get(source_key)
    if existing is not None:
        return existing

    fingerprint = _source_fingerprint(source_path, effective_override)
    deduped_dest = (
        state.seen_fingerprints.get(fingerprint) if fingerprint else None
    )
    if deduped_dest is not None:
        state.seen_sources[source_key] = deduped_dest
        return deduped_dest

    destination_source = _destination_source_path(
        source_path,
        effective_override,
        state.output_name_prefix,
    )
    dest_name = _unique_destination_name(
        destination_source,
        state.seen_names,
        diagnostics,
        "texture",
    )
    dest_path = state.textures_dir / dest_name

    try:
        state.textures_dir.mkdir(parents=True, exist_ok=True)
        converted = False
        conversion_artifact_type = None
        if effective_override:
            converted = _convert_texture_atomically(
                source_path,
                dest_path,
                effective_override,
                diagnostics,
            )
            conversion_artifact_type = "texture_override"
            if not converted:
                fallback_override = _texture_fallback_override(effective_override)
                if fallback_override:
                    fallback_dest_source = _destination_source_path(
                        source_path,
                        fallback_override,
                        state.output_name_prefix,
                    )
                    fallback_dest_name = _unique_destination_name(
                        fallback_dest_source,
                        state.seen_names,
                        diagnostics,
                        "texture",
                    )
                    fallback_dest_path = state.textures_dir / fallback_dest_name
                    converted = _convert_texture_atomically(
                        source_path,
                        fallback_dest_path,
                        fallback_override,
                        diagnostics,
                    )
                    if converted:
                        dest_path = fallback_dest_path
                        conversion_artifact_type = "texture_override_fallback"
                        if diagnostics:
                            diagnostics.add_warning(
                                f"Saved resized PNG fallback for texture '{source_path}' because AVIF conversion failed."
                            )
            if not converted:
                if not _is_supported_apple_texture(source_path):
                    message = (
                        f"Texture '{source_path}' uses unsupported package format "
                        f"'{source_path.suffix or '<none>'}' and could not be transcoded to PNG."
                    )
                    if diagnostics:
                        diagnostics.add_texture_failed(str(source_path), message)
                    raise _TextureStagingError(message)
                fallback_source = _destination_source_path(
                    source_path,
                    None,
                    state.output_name_prefix,
                )
                dest_name = _unique_destination_name(
                    fallback_source,
                    state.seen_names,
                    diagnostics,
                    "texture",
                )
                dest_path = state.textures_dir / dest_name
                if source_path.resolve() != dest_path.resolve():
                    shutil.copy2(source_path, dest_path)
        elif source_path.resolve() != dest_path.resolve():
            shutil.copy2(source_path, dest_path)

        dest_path = _finalize_content_addressed_texture(
            dest_path,
            state.textures_dir,
        )
        _mark_superseded_export_texture(
            source_path,
            dest_path,
            state.textures_dir,
            state.superseded_paths,
            state.native_texture_copies,
        )
        if converted and diagnostics:
            diagnostics.add_texture_converted(str(source_path))
            diagnostics.add_generated_file(
                conversion_artifact_type,
                str(dest_path),
                source=str(source_path),
                usd_attribute=usd_attribute,
                authored_path=authored_path,
            )
        state.seen_sources[source_key] = dest_path
        if fingerprint:
            state.seen_fingerprints[fingerprint] = dest_path
        if diagnostics:
            diagnostics.add_texture_copied(
                str(source_path),
                str(dest_path),
                usd_attribute=usd_attribute,
                authored_path=authored_path,
            )
        return dest_path
    except _TextureStagingError:
        raise
    except Exception as exc:
        message = f"Failed to stage texture '{source_path}': {exc}"
        hint = _long_path_hint(state.textures_dir)
        if hint:
            message = f"{message} {hint}"
        if diagnostics:
            diagnostics.add_texture_failed(str(source_path), message)
        raise _TextureStagingError(message) from exc


def _resolve_layer_texture_source(
    authored_path: str,
    resolved_path: str,
    authoring_layer,
    fallback_dir: Path,
) -> Path:
    if resolved_path:
        resolved_candidate = _local_asset_path(resolved_path)
        if resolved_candidate is not None:
            return _absolute_path(resolved_candidate, fallback_dir)

    if not authored_path:
        raise _TextureStagingError("Texture asset has no authored or resolved path.")
    authored_candidate = _local_asset_path(authored_path)
    if authored_candidate is None:
        scheme = _asset_scheme(authored_path) or "non-file"
        raise _TextureStagingError(
            f"Remote or non-file texture URL '{authored_path}' ({scheme}) cannot be included in a self-contained export."
        )
    if authored_candidate.is_absolute():
        return _absolute_path(authored_candidate, fallback_dir)

    if authoring_layer is not None:
        try:
            computed = authoring_layer.ComputeAbsolutePath(
                _normalize_file_url(authored_path)
            )
        except Exception:
            computed = ""
        if computed:
            candidate = _local_asset_path(str(computed))
            if candidate is not None:
                return _absolute_path(candidate, fallback_dir)
        layer_path = _layer_filesystem_path(authoring_layer)
        if layer_path is not None:
            return _absolute_path(authored_candidate, layer_path.parent)
    return _absolute_path(authored_candidate, fallback_dir)


def _asset_value_paths(asset_value) -> tuple[str, str]:
    """Return authored and resolver-produced paths without conflating them."""
    if isinstance(asset_value, Sdf.AssetPath):
        return str(asset_value.path or ""), str(asset_value.resolvedPath or "")
    if asset_value:
        return str(asset_value), ""
    return "", ""


def _layer_filesystem_path(layer) -> Path | None:
    for attribute in ("realPath", "resolvedPath", "identifier"):
        value = getattr(layer, attribute, None)
        if not value:
            continue
        candidate = _local_asset_path(str(value))
        if candidate is not None and "[" not in str(candidate):
            return _absolute_path(candidate, Path.cwd())
    return None


def _local_asset_path(asset_path: str) -> Path | None:
    if not asset_path:
        return None
    scheme = _asset_scheme(asset_path)
    if scheme in _NON_FILE_ASSET_SCHEMES:
        return None
    if scheme and scheme != "file":
        return None
    return Path(_normalize_file_url(asset_path))


def _asset_scheme(asset_path: str) -> str:
    value = str(asset_path or "")
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    # A Windows drive path is not a URI scheme. This does no harm on macOS and
    # keeps support bundles/tests portable.
    if len(scheme) == 1 and len(value) >= 2 and value[1] == ":":
        return ""
    return scheme


def _normalize_file_url(asset_path: str) -> str:
    if _asset_scheme(asset_path) != "file":
        return asset_path
    parsed = urlparse(asset_path)
    path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    return path


def _absolute_path(path: Path, base_dir: Path) -> Path:
    candidate = path if path.is_absolute() else base_dir / path
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _effective_texture_override(source_path: Path, texture_override):
    # OpenEXR is the fidelity-preserving Apple package format for float/HDR
    # images. Never send it through the 8-bit PNG or lossy AVIF/JPEG override
    # path, even when a global texture override is enabled.
    if source_path.suffix.lower() == ".exr":
        return None
    if _is_supported_apple_texture(source_path):
        return texture_override if texture_override and _can_convert_texture(source_path) else None

    if texture_override:
        requested_format = str(texture_override.get("file_format") or "").upper()
        if requested_format in {"AVIF", "PNG"}:
            return texture_override
        return {
            "file_format": "PNG",
            "extension": ".png",
            "resolution": max(0, int(texture_override.get("resolution") or 0)),
        }
    return {
        "file_format": "PNG",
        "extension": ".png",
        "resolution": 0,
    }


def require_safe_texture_alpha_staging_policy(
    source_path: str | Path,
    *,
    alpha_mode: str | None,
    has_premultiplied_alpha: bool,
    settings,
    diagnostics=None,
) -> None:
    """Reject Blender 5.2 AVIF encoding for premultiplied base color.

    Material extraction is the authoritative source of alpha semantics; USD
    texture staging cannot recover those semantics from an asset path alone.
    Blender 5.2's AVIF writer does not preserve the premultiplied-alpha
    relationship required by the MaterialX ``hasPremultipliedAlpha`` input, so
    an encode or resize to AVIF would make the graph disagree with its pixels.

    Only a texture override that writes AVIF is refused, and an Original
    override on an AVIF source counts as one. Without an override the source
    is converted to PNG like any format outside the package contract, and PNG
    copy and resize are permitted. This function only validates policy and
    never changes the graph metadata.
    """
    normalized_alpha_mode = str(alpha_mode or "").strip().upper()
    is_premultiplied = bool(has_premultiplied_alpha) or normalized_alpha_mode in {
        "PREMUL",
        "PREMULTIPLIED",
    }
    if not is_premultiplied:
        return

    source_path = Path(source_path)
    texture_override = _texture_override_settings(settings, diagnostics)
    effective_override = _effective_texture_override(source_path, texture_override)
    if not effective_override:
        return

    output_format = str(effective_override.get("file_format") or "").upper()
    if output_format == "ORIGINAL":
        output_format = _original_file_format_for_source(source_path) or ""
    if output_format != "AVIF":
        return

    raise _TextureStagingError(
        f"Premultiplied base-color texture '{source_path}' cannot be encoded "
        "or resized as AVIF safely by Blender 5.2. Select PNG for the export "
        "texture format, or disable the unsafe AVIF/resolution override."
    )


def _is_supported_apple_texture(path: Path) -> bool:
    return path.suffix.lower() in _APPLE_TEXTURE_OUTPUT_EXTENSIONS


def _is_texture_path(asset_path: str) -> bool:
    if not asset_path:
        return False
    parsed = urlparse(str(asset_path))
    suffix = Path(parsed.path or str(asset_path)).suffix.lower()
    return suffix in TEXTURE_EXTENSIONS


def _texture_override_settings(settings, diagnostics=None):
    if not bool(getattr(settings, "export_texture_settings_enabled", False)):
        return None

    try:
        from . import bake_textures

        image_format = bake_textures._resolve_bake_image_format(settings, diagnostics)
        resolution = bake_textures._resolve_texture_override_resolution(settings)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(f"Export texture overrides disabled: {exc}")
        return None

    if image_format["file_format"] == "ORIGINAL" and int(resolution) <= 0:
        return None

    return {
        "file_format": image_format["file_format"],
        "extension": image_format["extension"],
        "resolution": int(resolution),
    }


def _texture_fallback_override(texture_override):
    file_format = str(texture_override.get("file_format") or "").upper()
    if file_format != "AVIF":
        return None
    resolution = int(texture_override.get("resolution") or 0)
    return {
        "file_format": "PNG",
        "extension": ".png",
        # A zero resolution preserves native dimensions. AVIF encoding can be
        # unavailable even when the user selected Original size; that must not
        # disable the safe PNG fallback.
        "resolution": max(0, resolution),
    }


def _texture_name_prefix(usd_path: str) -> str:
    prefix = _safe_filename_stem(Path(usd_path).stem)
    return prefix or "scene"


def _safe_filename_stem(name: str) -> str:
    cleaned = []
    previous_separator = False
    for char in str(name or ""):
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
            previous_separator = False
        elif not previous_separator:
            cleaned.append("-")
            previous_separator = True
    return "".join(cleaned).strip("-_")


def _destination_source_path(source_path: Path, texture_override, output_name_prefix: str = "") -> Path:
    texture_override = texture_override or {}
    extension = texture_override.get("extension") or source_path.suffix
    stem = source_path.stem
    prefix = _safe_filename_stem(output_name_prefix)
    if prefix and not stem.startswith(f"{prefix}-"):
        stem = f"{prefix}-{stem}"
    return source_path.with_name(f"{stem}{extension}")


def _source_key(source_path: Path, texture_override) -> tuple[str, tuple | None]:
    try:
        resolved = str(source_path.resolve())
    except Exception:
        resolved = str(source_path)
    return resolved, _override_key(texture_override)


def _source_fingerprint(source_path: Path, texture_override) -> tuple[str, tuple | None] | None:
    digest = _file_digest(source_path)
    if not digest:
        return None
    return digest, _override_key(texture_override)


def _override_key(texture_override) -> tuple | None:
    if not texture_override:
        return None
    return (
        texture_override.get("file_format"),
        texture_override.get("extension"),
        int(texture_override.get("resolution") or 0),
    )


def _file_digest(path: Path) -> str | None:
    try:
        digest = hashlib.sha1()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _finalize_content_addressed_texture(dest_path: Path, textures_dir: Path) -> Path:
    """Give a staged texture an immutable name derived from its final bytes.

    Publication installs sidecars before atomically switching the root USD. A
    stable semantic filename could therefore be observed with old bytes after
    a hard process exit. Embedding the complete SHA-256 digest makes each leaf
    immutable while retaining a readable stem for inspection and diagnostics.
    """
    if dest_path.is_symlink() or not dest_path.is_file():
        raise _TextureStagingError(
            f"Staged texture is not a regular file: {dest_path}"
        )
    try:
        if dest_path.stat().st_size <= 0:
            raise _TextureStagingError(f"Staged texture is empty: {dest_path}")
        resolved_dir = textures_dir.resolve()
        if dest_path.resolve().parent != resolved_dir:
            raise _TextureStagingError(
                f"Staged texture escapes its generation directory: {dest_path}"
            )
    except OSError as exc:
        raise _TextureStagingError(
            f"Could not inspect staged texture '{dest_path}': {exc}"
        ) from exc

    digest = _sha256_file_digest(dest_path)
    if digest is None:
        raise _TextureStagingError(
            f"Could not content-address staged texture: {dest_path}"
        )

    # The dependency pipeline stages the same bytes under different source
    # names across its two passes (e.g. Blender's native texture copy first,
    # the packed-image snapshot the MaterialX pass references second), and
    # ``seen_fingerprints`` only spans one staging state. The digest suffix
    # identifies content, not provenance, so any generation file already
    # holding these bytes is the canonical destination; without this reuse
    # the package ships the same payload twice.
    extension = dest_path.suffix.lower()
    for candidate in sorted(textures_dir.glob(f"*-{digest}{extension}")):
        if candidate.name == dest_path.name:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if _files_have_identical_bytes(dest_path, candidate):
            dest_path.unlink()
            return candidate

    semantic_stem = unicodedata.normalize("NFC", dest_path.stem)
    previous_digest = _CONTENT_HASH_SUFFIX.fullmatch(semantic_stem)
    if previous_digest:
        semantic_stem = previous_digest.group("stem")
    semantic_stem = _truncate_utf8_stem(
        semantic_stem,
        _content_stem_budget(dest_path.parent, digest, extension),
    )
    addressed_path = dest_path.with_name(
        f"{semantic_stem}-{digest}{extension}"
    )
    if addressed_path == dest_path:
        return dest_path

    if addressed_path.exists() or addressed_path.is_symlink():
        if (
            addressed_path.is_symlink()
            or not addressed_path.is_file()
            or not _files_have_identical_bytes(dest_path, addressed_path)
        ):
            raise _TextureStagingError(
                f"Content-addressed texture collision: {addressed_path}"
            )
        dest_path.unlink()
        return addressed_path

    os.replace(dest_path, addressed_path)
    return addressed_path


def _is_current_generation_texture(source_path: Path, textures_dir: Path) -> bool:
    if (
        source_path.is_symlink()
        or not source_path.is_file()
        or source_path.suffix.lower() not in _APPLE_TEXTURE_OUTPUT_EXTENSIONS
    ):
        return False
    try:
        if source_path.resolve().parent != textures_dir.resolve():
            return False
    except OSError:
        return False
    match = _CONTENT_HASH_SUFFIX.fullmatch(
        unicodedata.normalize("NFC", source_path.stem)
    )
    if not match:
        return False
    digest = _sha256_file_digest(source_path)
    return digest is not None and source_path.stem.endswith(f"-{digest}")


def _sha256_file_digest(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _files_have_identical_bytes(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        with first.open("rb") as left, second.open("rb") as right:
            while True:
                left_chunk = left.read(1024 * 1024)
                right_chunk = right.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def _long_path_hint(textures_dir: Path) -> str:
    """Name MAX_PATH as the likely cause when the staging path is near it.

    A Windows failure here surfaces as ``[WinError 3] The system cannot find
    the path specified``, naming a path that plainly exists a directory up.
    Nothing about that points at the length, so say it.
    """
    if not _PATH_LENGTH_IS_CAPPED:
        return ""
    # The shortest content-addressed name we could possibly write.
    shortest_name = _MIN_CONTENT_STEM_CHARS + 1 + 64 + len(".png")
    if len(str(textures_dir)) + len(os.sep) + shortest_name <= _MAX_STAGED_PATH_CHARS:
        return ""
    return (
        f"The staging path is {len(str(textures_dir))} characters before the "
        f"texture's own name, which leaves too little of the Windows "
        f"{_MAX_STAGED_PATH_CHARS + 1}-character path limit to write it. "
        "Export to a shorter directory, or shorten the output filename."
    )


def _content_stem_budget(directory: Path, digest: str, extension: str) -> int:
    """UTF-8 bytes the semantic stem may spend before the path breaks Windows.

    The stem is the only part of a content-addressed texture name we are free to
    shorten: the digest is the identity and the directory is the caller's. So
    the budget is whatever MAX_PATH has left once the directory, the separator,
    the ``-`` joint, the digest and the extension are paid for.

    Clamped rather than raised on. A directory long enough to leave no room is
    already unusable on Windows, but it is perfectly legal on macOS and Linux,
    and refusing it here would break exports that work today. Ask for the
    shortest usable stem instead and let the filesystem have the final say -
    ``_stage_texture_source`` explains the failure when it comes.
    """
    if not _PATH_LENGTH_IS_CAPPED:
        return _MAX_CONTENT_STEM_UTF8_BYTES
    fixed = (
        len(str(directory))
        + len(os.sep)
        + 1  # the "-" joining stem and digest
        + len(digest)
        + len(extension)
    )
    available = _MAX_STAGED_PATH_CHARS - fixed
    return max(_MIN_CONTENT_STEM_CHARS, min(_MAX_CONTENT_STEM_UTF8_BYTES, available))


def _truncate_utf8_stem(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value or "texture"
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated.rstrip("-_.") or "texture"


def _can_convert_texture(source_path: Path) -> bool:
    return source_path.suffix.lower() in TEXTURE_EXTENSIONS


def _validate_transcoded_texture(dest_path: Path, diagnostics=None) -> bool:
    """Require nonempty output and decode it when a Blender backend is usable."""
    try:
        if not dest_path.is_file() or dest_path.stat().st_size <= 0:
            if diagnostics:
                diagnostics.add_warning(f"Converted texture '{dest_path}' is empty or missing.")
            return False
    except OSError as exc:
        if diagnostics:
            diagnostics.add_warning(f"Could not inspect converted texture '{dest_path}': {exc}")
        return False

    attempted_decode = False
    try:
        import imbuf

        load = getattr(imbuf, "load", None)
        if callable(load):
            attempted_decode = True
            probe = None
            try:
                probe = load(str(dest_path))
                size = tuple(getattr(probe, "size", ()) or ())
                if len(size) >= 2 and int(size[0]) > 0 and int(size[1]) > 0:
                    return True
            except Exception:
                pass
            finally:
                if probe is not None:
                    try:
                        probe.free()
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        import bpy

        images = getattr(getattr(bpy, "data", None), "images", None)
        load = getattr(images, "load", None)
        if callable(load):
            attempted_decode = True
            image = None
            try:
                image = load(str(dest_path), check_existing=False)
                size = tuple(getattr(image, "size", ()) or ())
                if len(size) >= 2 and int(size[0]) > 0 and int(size[1]) > 0:
                    return True
            except Exception:
                pass
            finally:
                if image is not None:
                    try:
                        images.remove(image)
                    except Exception:
                        pass
    except Exception:
        pass

    if attempted_decode:
        if diagnostics:
            diagnostics.add_warning(f"Converted texture '{dest_path}' could not be decoded after writing.")
        return False
    # Plain-Python tests and non-Blender tooling may have no image decoder. The
    # mandatory nonempty check still prevents zero-byte artifacts; Blender
    # exports take the decode path above.
    return True


def _convert_texture_atomically(
    source_path: Path,
    dest_path: Path,
    texture_override,
    diagnostics=None,
) -> bool:
    """Convert through a sibling temporary file, then atomically publish it.

    A texture already emitted into the export staging directory can be both the
    source and final destination of a resize/format override. Encoding directly
    to that path lets a failed encoder truncate it, and the normal failure
    cleanup can then unlink the only valid source. Keep the source untouched
    until a complete, decodable output is ready for ``os.replace``.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{dest_path.stem}.convert-",
        suffix=dest_path.suffix,
        dir=dest_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    # Blender/ImBuf encoders expect to create or fully rewrite the destination.
    temporary_path.unlink(missing_ok=True)
    try:
        converted = _convert_texture(
            source_path,
            temporary_path,
            texture_override,
            diagnostics,
        )
        if not converted or not _validate_transcoded_texture(temporary_path, diagnostics):
            return False
        os.replace(temporary_path, dest_path)
        return True
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(
                f"Could not publish converted texture '{dest_path}' atomically: {exc}"
            )
        return False
    finally:
        _remove_failed_conversion_output(temporary_path)


def _convert_texture(source_path: Path, dest_path: Path, texture_override, diagnostics=None) -> bool:
    file_format = str(texture_override.get("file_format") or "").upper()
    if file_format == "ORIGINAL":
        file_format = _original_file_format_for_source(source_path)
        if not file_format:
            if diagnostics:
                diagnostics.add_warning(
                    f"Could not resize texture '{source_path}' in its original format; copied the original instead."
                )
            return False
        texture_override = dict(texture_override)
        texture_override["file_format"] = file_format

    if _convert_texture_with_imbuf(source_path, dest_path, texture_override, diagnostics):
        return True

    try:
        import bpy
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(f"Could not convert texture '{source_path}': Blender image API unavailable ({exc}).")
        return False
    return _convert_texture_in_current_process(source_path, dest_path, texture_override, bpy, diagnostics)


def _original_file_format_for_source(source_path: Path) -> str | None:
    return _ORIGINAL_FORMAT_BY_EXTENSION.get(source_path.suffix.lower())


def _convert_texture_in_current_process(
    source_path: Path,
    dest_path: Path,
    texture_override,
    bpy,
    diagnostics=None,
) -> bool:
    image = None
    try:
        image = bpy.data.images.load(str(source_path), check_existing=False)
        _scale_image_to_max_resolution(image, int(texture_override.get("resolution") or 0))
        image.filepath_raw = str(dest_path)
        image.file_format = texture_override["file_format"]
        image.save()
        return dest_path.exists()
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(
                f"Could not convert texture '{source_path}' to '{dest_path.suffix}'; copied the original instead ({exc})."
            )
        return False
    finally:
        if image is not None:
            try:
                if getattr(image, "users", 0) == 0:
                    bpy.data.images.remove(image)
            except Exception:
                pass


def _convert_texture_with_imbuf(
    source_path: Path,
    dest_path: Path,
    texture_override,
    diagnostics=None,
) -> bool:
    """Convert/resize a texture with the imbuf API (format chosen by extension)."""
    try:
        import imbuf
    except Exception:
        return False

    file_format = str(texture_override.get("file_format") or "").upper()
    ibuf = None
    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        ibuf = imbuf.load(str(source_path))
        _scale_imbuf_to_max_resolution(ibuf, int(texture_override.get("resolution") or 0))
        # imbuf.write() encodes according to the buffer's file_type, not the
        # destination extension.
        ibuf.file_type = _IMBUF_FILE_TYPE_BY_FORMAT.get(file_format, file_format)
        ibuf.quality = _LOSSY_TEXTURE_QUALITY
        imbuf.write(ibuf, filepath=str(dest_path))
        return dest_path.exists()
    except Exception as exc:
        _remove_failed_conversion_output(dest_path)
        if diagnostics:
            diagnostics.add_warning(
                f"Could not convert texture '{source_path}' to '{dest_path.suffix}' via imbuf ({exc})."
            )
        return False
    finally:
        if ibuf is not None:
            try:
                ibuf.free()
            except Exception:
                pass


def _scale_imbuf_to_max_resolution(ibuf, max_resolution: int) -> None:
    if max_resolution <= 0:
        return
    width, height = ibuf.size
    largest = max(width, height)
    if largest <= max_resolution:
        return
    scale = max_resolution / largest
    ibuf.resize(
        (max(1, round(width * scale)), max(1, round(height * scale))),
        method='BILINEAR',
    )


def _remove_failed_conversion_output(dest_path: Path) -> None:
    try:
        if dest_path.exists():
            dest_path.unlink()
    except Exception:
        pass


def _scale_image_to_max_resolution(image, resolution: int) -> None:
    if resolution <= 0:
        return
    try:
        width, height = int(image.size[0]), int(image.size[1])
    except Exception:
        return
    if width <= 0 or height <= 0:
        return
    max_dimension = max(width, height)
    if max_dimension <= resolution:
        return
    scale = resolution / float(max_dimension)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    image.scale(target_width, target_height)


def _mark_superseded_export_texture(
    source_path: Path,
    dest_path: Path,
    textures_dir: Path,
    superseded_paths: set[Path],
    native_texture_copies: frozenset[Path] = frozenset(),
) -> None:
    """Record a staging-owned source that ``dest_path`` has replaced.

    Two kinds of source qualify:

    * anything already inside this export's generation directory, and
    * a file the native USD exporter wrote into ``<staging>/textures`` during
      this attempt, as captured by ``record_native_texture_copies``.

    The second set is passed in rather than inferred from the path: a user's
    own authoritative texture can legitimately sit at ``textures/<name>.png``
    next to its USD and is byte-for-byte indistinguishable from a copy Blender
    just wrote. Deleting on path shape alone would destroy real source assets,
    so an attempt that never ran the native exporter supersedes nothing outside
    its generation directory.
    """
    try:
        source_resolved = source_path.resolve()
        dest_resolved = dest_path.resolve()
        textures_resolved = textures_dir.resolve()
    except Exception:
        return
    if source_resolved == dest_resolved:
        return

    if source_resolved not in native_texture_copies:
        try:
            source_resolved.relative_to(textures_resolved)
        except ValueError:
            return
    superseded_paths.add(source_resolved)


def _remove_unreferenced_superseded_textures(
    stage,
    usd_dir: Path,
    superseded_paths: set[Path],
    diagnostics=None,
) -> None:
    """Delete only the *superseded* copies, never everything unreferenced.

    A generation directory carries one unreferenced duplicate per image: the
    same texture reaches staging both as the original and as the copy Blender's
    own exporter wrote, and the two are not byte-identical because Blender
    re-encodes, so content fingerprinting cannot merge them. It is wasted bytes,
    not a correctness problem.

    Do not "fix" that by pruning whatever ``_referenced_texture_paths`` omits.
    That function walks ``stage.Traverse()`` and reads ``attr.Get()`` with no
    time code, so it misses three classes of live reference: a texture used only
    by a **non-selected variant** (preflight iterates up to
    ``MAX_VARIANT_COMBINATIONS`` = 256, so variants are real here), one
    referenced only at a **time sample**, and anything under an **inactive prim
    or unloaded payload**. Pruning on that basis trades wasted bytes for an
    asset whose variants have no textures.

    The two safe routes, in preference order: stop creating the duplicate by
    reusing the already-staged destination when the source is one of the native
    copies recorded by ``staging_namespace.record_native_texture_copies``; or
    harden ``_referenced_texture_paths`` to walk variant combinations, time
    samples and inactive prims first.
    """
    if not superseded_paths:
        return

    referenced_paths = _referenced_texture_paths(stage, usd_dir)
    for path in sorted(superseded_paths):
        if path in referenced_paths or not path.exists():
            continue
        try:
            path.unlink()
            if diagnostics:
                diagnostics.add_generated_file("removed_superseded_texture", str(path))
        except Exception as exc:
            if diagnostics:
                diagnostics.add_warning(f"Failed to remove superseded texture '{path}': {exc}")


def _referenced_texture_paths(stage, usd_dir: Path) -> set[Path]:
    paths = set()
    for prim in stage.Traverse():
        for attr in prim.GetAttributes():
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset:
                continue
            asset_value = attr.Get()
            authored_path, resolved_path = _asset_value_paths(asset_value)
            asset_path = resolved_path or authored_path
            if not asset_path or not _is_texture_path(asset_path):
                continue
            path = _local_asset_path(asset_path)
            if path is not None:
                paths.add(_absolute_path(path, usd_dir))
    return paths


def _unique_destination_name(path: Path, used: dict, diagnostics=None, label: str = "asset") -> str:
    """Return a deterministic name safe on Unicode/case-folding filesystems."""
    name = path.name
    name_key = _destination_name_key(name)
    existing = used.get(name_key)
    if existing is None or existing == path:
        used[name_key] = path
        return name

    stem = path.stem
    suffix = path.suffix
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    candidate = f"{stem}_{digest}{suffix}"
    counter = 1
    candidate_key = _destination_name_key(candidate)
    while candidate_key in used and used[candidate_key] != path:
        candidate = f"{stem}_{digest}_{counter}{suffix}"
        candidate_key = _destination_name_key(candidate)
        counter += 1
    used[candidate_key] = path

    if diagnostics:
        diagnostics.add_warning(
            f"Renamed {label} '{name}' to '{candidate}' to avoid a name collision."
        )

    return candidate


def _destination_name_key(name: str) -> str:
    return unicodedata.normalize("NFC", str(name)).casefold()
