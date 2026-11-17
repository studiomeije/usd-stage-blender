"""Where an image texture's pixels live on disk, and staging the ones that do not.

An Image Texture node names a Blender image, and the export needs a file it
can copy. Most images already are one. A packed, generated or edited image is
not, so its current pixels are saved to a temporary file for the length of the
export: :func:`begin_image_staging_session` opens that directory and
:func:`cleanup_image_staging_session` removes it, whatever happened between.

This module only reads Blender images and writes those temporary files; the
material extractor decides what to do with the paths it returns.
"""

import hashlib
import os
import re
import shutil
import tempfile
from array import array
from pathlib import Path
from typing import Any, Dict, Optional

#: The open staging session: images already staged, by cache key, and the
#: directory they were written to. Empty and None outside a session.
_STAGED_IMAGE_CACHE: Dict[Any, str] = {}
_STAGED_IMAGE_DIR: Optional[Path] = None
_STAGED_IMAGE_DIR_OWNED = False

_FORMAT_TO_EXTENSION = {
    "AVIF": ".avif",
    "PNG": ".png",
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "TIFF": ".tif",
    "TIF": ".tif",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "OPEN_EXR": ".exr",
    "OPEN_EXR_MULTILAYER": ".exr",
    "HDR": ".hdr",
    "BMP": ".bmp",
    "WEBP": ".webp",
}

_EXTENSION_TO_FORMAT = {
    ".avif": "AVIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".tga": "TARGA",
    ".exr": "OPEN_EXR",
    ".hdr": "HDR",
    ".bmp": "BMP",
    ".webp": "WEBP",
}


#: Blender reports ``Image.depth`` as total bits per pixel across the file's
#: channels. ``Image.channels`` is the internal pixel-buffer width and is
#: almost always 4, so it cannot answer whether the file carries alpha.
#: Depths that two different layouts share (16 is 16-bit gray or 8-bit
#: gray+alpha; 32 is 8-bit RGBA or one float channel) are left out and
#: resolved from the file header instead.
_IMAGE_DEPTH_CHANNELS = {
    8: 1,
    24: 3,
    48: 3,
    96: 3,
    64: 4,
    128: 4,
}


def _image_source_channels(image) -> Optional[int]:
    """Return the source file's channel count, or None when unknown."""
    if not image:
        return None
    try:
        depth = int(getattr(image, "depth", 0) or 0)
    except (TypeError, ValueError):
        return None
    return _IMAGE_DEPTH_CHANNELS.get(depth)


def _image_source_alpha(image, texture_path: Optional[str]) -> Dict[str, Any]:
    """Describe the source file's channel count and usable alpha.

    Reader selection never depends on this; only a consumer asking for the
    alpha channel does. When the count cannot be established the result is
    empty and the alpha read stays permitted.
    """
    channels = _image_source_channels(image)
    if channels is None and texture_path:
        channels = _file_channel_count(texture_path)
    if channels is None:
        return {}
    alpha_mode = str(getattr(image, "alpha_mode", "") or "").upper()
    return {
        "source_channels": channels,
        # Blender's alpha_mode NONE means "ignore the file's alpha and treat
        # the image as opaque", so it removes the channel from play.
        "source_has_alpha": channels in (2, 4) and alpha_mode != "NONE",
    }


def _file_channel_count(path: str) -> Optional[int]:
    """Read a channel count from an image file header, best effort."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 26:
        # IHDR colour type: 0 gray, 2 RGB, 4 gray+alpha, 6 RGBA. Type 3
        # (palette) can carry alpha in a tRNS chunk, so it stays unknown.
        return {0: 1, 2: 3, 4: 2, 6: 4}.get(header[25])
    return None


def _resolve_image_path(image) -> Optional[str]:
    """Resolve a Blender image path to an absolute filesystem path."""
    if not image:
        return None

    filepath = image.filepath or image.filepath_raw or ""
    is_dirty = bool(getattr(image, "is_dirty", False))
    packed = getattr(image, "packed_file", None)
    source = (getattr(image, "source", "") or "").upper()

    if is_dirty and source in {"TILED", "SEQUENCE", "MOVIE"}:
        raise ValueError(
            f"Dirty {source.lower()} image '{getattr(image, 'name', 'Image')}' must be "
            "baked to a single current frame before RealityKit export"
        )

    try:
        import bpy
        if filepath:
            filepath = bpy.path.abspath(filepath)
    except Exception:
        pass

    if filepath:
        try:
            filepath = str(Path(filepath).resolve())
        except Exception:
            filepath = os.path.normpath(filepath)

    # A Blender datablock pointer alone is not an image identity. Blender may
    # reuse it after a reload, and a dirty image can become a clean file-backed
    # image without changing the pointer. Include the resolved source state so
    # only an exact image/file version can reuse a staged snapshot.
    cache_key = _image_cache_key(image, filepath)
    cached_path = _STAGED_IMAGE_CACHE.get(cache_key)
    if (
        cached_path
        and not is_dirty
        and packed is None
        and source != "GENERATED"
        and _is_path_on_disk(cached_path)
    ):
        return cached_path

    # Current Blender pixels and packed bytes are authoritative. Never return
    # an external file first for dirty/packed/generated images.
    if is_dirty or packed is not None or source == "GENERATED":
        return _stage_image_to_temp(
            image,
            filepath,
            force_refresh=is_dirty,
            refresh_packed=packed is not None and not is_dirty,
        )

    if filepath and _is_path_on_disk(filepath) and not _is_temp_path(filepath):
        return filepath

    # Fallback: stage packed or generated images to a temp directory.
    return _stage_image_to_temp(image, filepath)


def _is_path_on_disk(path: str) -> bool:
    """Return True if the path exists on disk."""
    try:
        return Path(path).is_file()
    except Exception:
        return False


def _is_temp_path(path: str) -> bool:
    """Return True if the path points into a temporary directory."""
    lowered = path.replace("\\", "/").lower()
    if "usd_textures_tmp" in lowered:
        return True

    temp_root = Path(tempfile.gettempdir())
    try:
        return Path(path).resolve().is_relative_to(temp_root.resolve())
    except Exception:
        return lowered.startswith(str(temp_root).replace("\\", "/").lower())


def _stage_image_to_temp(
    image,
    filepath: Optional[str],
    force_refresh: bool = False,
    refresh_packed: bool = False,
) -> Optional[str]:
    """Stage image data to a temp file so it can be copied into the export."""
    cache_key = _image_cache_key(image, filepath)
    if not force_refresh and not refresh_packed and cache_key in _STAGED_IMAGE_CACHE:
        cached_path = _STAGED_IMAGE_CACHE[cache_key]
        if _is_path_on_disk(cached_path):
            return cached_path
        _STAGED_IMAGE_CACHE.pop(cache_key, None)

    staging_dir = _get_staging_dir()
    source = (getattr(image, "source", "") or "").upper()
    if force_refresh or source == "GENERATED":
        extension = ".exr" if bool(getattr(image, "is_float", False)) else ".png"
    else:
        extension = _guess_image_extension(image, filepath)
    basename = _sanitize_texture_name(Path(filepath).stem if filepath else image.name)
    destination_key = _image_destination_key(image, filepath)
    digest = hashlib.sha256(repr(destination_key).encode("utf-8")).hexdigest()[:12]
    dest_path = staging_dir / f"{basename}_{digest}{extension}"

    if force_refresh:
        if _save_current_image_snapshot_to_path(image, dest_path):
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        raise ValueError(
            f"Unable to snapshot current pixels for dirty image "
            f"'{getattr(image, 'name', 'Image')}'; refusing stale packed or disk bytes"
        )

    if source == "GENERATED":
        if _save_current_image_snapshot_to_path(image, dest_path):
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        raise ValueError(
            f"Unable to snapshot generated image '{getattr(image, 'name', 'Image')}'; "
            "refusing non-current fallback bytes"
        )

    packed = getattr(image, "packed_file", None)
    if packed:
        packed_data = getattr(packed, "data", None)
        if not packed_data:
            raise ValueError(
                f"Packed image '{getattr(image, 'name', 'Image')}' has no readable bytes; "
                "refusing stale external-file fallback"
            )
        try:
            dest_path.write_bytes(packed_data)
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        except Exception as exc:
            raise ValueError(
                f"Unable to stage authoritative packed bytes for image "
                f"'{getattr(image, 'name', 'Image')}'; refusing stale external-file fallback"
            ) from exc

    if filepath and _is_path_on_disk(filepath):
        try:
            shutil.copy2(filepath, dest_path)
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        except Exception:
            pass

    if _save_image_to_path(image, dest_path):
        _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
        return str(dest_path)

    return None


def _save_current_image_snapshot_to_path(image, dest_path: Path) -> bool:
    """Save current in-memory pixels without mutating the source datablock."""
    try:
        import bpy

        width, height = (int(value) for value in image.size[:2])
        if width <= 0 or height <= 0:
            return False
        source_pixels = getattr(image, "pixels", None)
        if source_pixels is None:
            return False

        values = array('f', [0.0]) * len(source_pixels)
        source_pixels.foreach_get(values)
        snapshot = bpy.data.images.new(
            name=f"__USDStage_{getattr(image, 'name', 'Image')}",
            width=width,
            height=height,
            alpha=True,
            float_buffer=bool(getattr(image, "is_float", False)),
        )
        try:
            # Blender 5.2 can clear a newly assigned pixel buffer when its
            # colorspace is set afterward, even when assigning the same name.
            # Configure metadata before copying the authoritative pixels.
            try:
                snapshot.colorspace_settings.name = image.colorspace_settings.name
            except Exception:
                pass
            try:
                snapshot.alpha_mode = image.alpha_mode
            except Exception:
                pass
            snapshot.pixels.foreach_set(values)
            snapshot.update()
            snapshot.filepath_raw = str(dest_path)
            snapshot.file_format = _EXTENSION_TO_FORMAT.get(dest_path.suffix.lower(), "PNG")
            snapshot.save()
            return dest_path.is_file()
        finally:
            bpy.data.images.remove(snapshot)
    except Exception:
        return False


def _image_cache_key(image, resolved_filepath: Optional[str] = None):
    """Return an export-local key for the current image source state.

    ``Image.as_pointer()`` identifies a live datablock, not its pixels. The
    same pointer survives dirty-to-clean transitions and reloads, and may be
    reused after a datablock is removed. File identity and mutable Blender
    source state therefore participate in the key as well.
    """
    pointer = None
    if hasattr(image, "as_pointer"):
        try:
            pointer = int(image.as_pointer())
        except Exception:
            pass
    if pointer is None:
        pointer = id(image)

    filepath = str(
        resolved_filepath
        or getattr(image, "filepath", "")
        or getattr(image, "filepath_raw", "")
        or ""
    )
    file_state = _file_state_fingerprint(filepath)
    size = getattr(image, "size", ()) or ()
    try:
        dimensions = tuple(int(value) for value in size[:2])
    except Exception:
        dimensions = ()
    packed = getattr(image, "packed_file", None)
    library = getattr(image, "library", None)
    library_path = str(getattr(library, "filepath", "") or "")

    return (
        pointer,
        str(getattr(image, "name", "") or ""),
        str(getattr(image, "source", "") or "").upper(),
        bool(getattr(image, "is_dirty", False)),
        bool(packed is not None),
        str(getattr(image, "filepath_raw", "") or ""),
        filepath,
        file_state,
        dimensions,
        bool(getattr(image, "is_float", False)),
        str(getattr(image, "file_format", "") or ""),
        library_path,
    )


def _file_state_fingerprint(filepath: str):
    """Return enough filesystem identity to invalidate a reloaded source."""
    if not filepath:
        return None
    try:
        stat_result = Path(filepath).stat()
    except (OSError, ValueError):
        return None
    return (
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _image_destination_key(image, resolved_filepath: Optional[str]):
    """Return a stable filename identity separate from cache invalidation.

    Pointer and file-stat state belong in the cache key, but putting them in a
    staged filename would make otherwise identical exports nondeterministic.
    A cache miss always rewrites this file, so a stable image/source identity
    remains safe across reloads and dirty-state transitions.
    """
    size = getattr(image, "size", ()) or ()
    try:
        dimensions = tuple(int(value) for value in size[:2])
    except Exception:
        dimensions = ()
    library = getattr(image, "library", None)
    return (
        str(getattr(image, "name", "") or ""),
        str(getattr(image, "source", "") or "").upper(),
        str(getattr(image, "filepath_raw", "") or resolved_filepath or ""),
        str(getattr(library, "filepath", "") or ""),
        dimensions,
        bool(getattr(image, "is_float", False)),
        str(getattr(image, "file_format", "") or ""),
    )


def begin_image_staging_session(diagnostics=None) -> Path:
    """Start a unique image-staging session for one export.

    Any abandoned previous session is removed first. A unique directory avoids
    reusing files left by an earlier export in the same long-lived Blender
    process, while clearing the cache prevents datablock-pointer reuse.
    """
    global _STAGED_IMAGE_DIR, _STAGED_IMAGE_DIR_OWNED
    cleanup_image_staging_session(diagnostics)
    base_dir = Path(tempfile.gettempdir()) / "usdstage_textures"
    base_dir.mkdir(parents=True, exist_ok=True)
    _STAGED_IMAGE_DIR = Path(
        tempfile.mkdtemp(prefix=f"export_{os.getpid()}_", dir=str(base_dir))
    )
    _STAGED_IMAGE_DIR_OWNED = True
    _STAGED_IMAGE_CACHE.clear()
    return _STAGED_IMAGE_DIR


def cleanup_image_staging_session(diagnostics=None) -> bool:
    """Clear image cache state and remove the current owned temp directory."""
    global _STAGED_IMAGE_DIR, _STAGED_IMAGE_DIR_OWNED
    staging_dir = _STAGED_IMAGE_DIR
    owned = _STAGED_IMAGE_DIR_OWNED
    _STAGED_IMAGE_CACHE.clear()
    _STAGED_IMAGE_DIR = None
    _STAGED_IMAGE_DIR_OWNED = False

    if staging_dir is None or not owned:
        return True
    try:
        shutil.rmtree(staging_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if diagnostics and hasattr(diagnostics, "add_warning"):
            diagnostics.add_warning(
                f"Failed to remove temporary image staging directory "
                f"'{staging_dir}': {exc}"
            )
        return False

    try:
        staging_dir.parent.rmdir()
    except OSError:
        pass
    return not staging_dir.exists()


def _get_staging_dir() -> Path:
    """Return the staging directory for temporary textures."""
    if _STAGED_IMAGE_DIR is None:
        return begin_image_staging_session()
    _STAGED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return _STAGED_IMAGE_DIR


def _sanitize_texture_name(name: str) -> str:
    """Sanitize a filename stem for staging."""
    if not name:
        return "image"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if sanitized[0].isdigit():
        sanitized = f"img_{sanitized}"
    return sanitized


def _guess_image_extension(image, filepath: Optional[str]) -> str:
    """Return a best-effort extension for the image."""
    if filepath:
        ext = Path(filepath).suffix.lower()
        if ext in _EXTENSION_TO_FORMAT:
            return ext
    fmt = getattr(image, "file_format", None)
    if fmt:
        return _FORMAT_TO_EXTENSION.get(str(fmt).upper(), ".png")
    return ".png"


def _save_image_to_path(image, dest_path: Path) -> bool:
    """Attempt to save an image to disk without mutating the original path."""
    try:
        orig_path = image.filepath_raw
        orig_format = image.file_format
    except Exception:
        orig_path = None
        orig_format = None

    try:
        fmt = _EXTENSION_TO_FORMAT.get(dest_path.suffix.lower(), "PNG")
        image.filepath_raw = str(dest_path)
        if fmt:
            image.file_format = fmt
        image.save()
        return dest_path.exists()
    except Exception:
        try:
            image.save_render(str(dest_path))
            return dest_path.exists()
        except Exception:
            return False
    finally:
        try:
            if orig_path is not None:
                image.filepath_raw = orig_path
            if orig_format is not None:
                image.file_format = orig_format
        except Exception:
            pass
