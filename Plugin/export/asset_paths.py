"""How an asset path in a USD layer maps to a file on disk, and to a file name.

Two stagers copy files an export depends on - :mod:`usd_assets` for layers
and other dependencies, :mod:`usd_textures` for textures - and both have to
answer the same questions about an authored path: is it a local file at all,
where is it, and what may it be called once copied. They answer them here, so
the two can never disagree about which file a path names.

Nothing here touches the stage or the file system beyond resolving a path.
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .usd_utils import Sdf

#: URI schemes that never name a local file. Any other scheme except
#: ``file`` is not a local file either; these are the ones USD itself writes.
NON_FILE_SCHEMES = frozenset({"anon", "blob", "data", "http", "https", "mem"})


def asset_scheme(asset_path: str) -> str:
    """The lower-case URI scheme of an asset path, or ``""`` for a plain path.

    A Windows drive letter is not a scheme: ``C:\\tex\\a.png`` is a path.
    """
    value = str(asset_path or "")
    scheme = urlparse(value).scheme.lower()
    if len(scheme) == 1 and len(value) >= 2 and value[1] == ":":
        return ""
    return scheme


def normalize_file_url(asset_path: str) -> str:
    """A ``file:`` URL as a file-system path; anything else unchanged.

    The host is kept: ``file://server/share/a.png`` is the network path
    ``//server/share/a.png``, not the local ``/share/a.png``.
    """
    if asset_scheme(asset_path) != "file":
        return asset_path
    parsed = urlparse(asset_path)
    path = url2pathname(unquote(parsed.path))
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        path = f"//{parsed.netloc}{path}"
    return path


def local_asset_path(asset_path: str) -> Path | None:
    """The file an asset path names, or None when it names no local file."""
    if not asset_path:
        return None
    scheme = asset_scheme(asset_path)
    if scheme and scheme != "file":
        return None
    return Path(normalize_file_url(asset_path))


def is_non_file_asset(asset_path: str) -> bool:
    """Whether an authored asset path names something other than a local file."""
    return bool(asset_path) and local_asset_path(str(asset_path)) is None


def absolute_path(path: Path, base_dir: Path) -> Path:
    """``path`` made absolute against ``base_dir`` and resolved where possible."""
    candidate = path if path.is_absolute() else base_dir / path
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def layer_filesystem_path(layer) -> Path | None:
    """The file a layer was read from, or None for an anonymous or packaged one."""
    for attribute in ("realPath", "resolvedPath", "identifier"):
        value = getattr(layer, attribute, None)
        if not value:
            continue
        candidate = local_asset_path(str(value))
        if candidate is not None and "[" not in str(candidate):
            return absolute_path(candidate, Path.cwd())
    return None


def asset_value_paths(asset_value) -> tuple[str, str]:
    """``(authored, resolved)`` paths of an asset value, never conflated."""
    if Sdf is not None and isinstance(asset_value, Sdf.AssetPath):
        return str(asset_value.path or ""), str(asset_value.resolvedPath or "")
    if asset_value:
        return str(asset_value), ""
    return "", ""


def destination_name_key(name: str) -> str:
    """The key two file names collide under on a default Apple file system.

    APFS is case-insensitive and normalization-insensitive by default, so
    ``Color.png``, ``color.png`` and a decomposed ``Café.png`` are one file.
    """
    return unicodedata.normalize("NFC", str(name)).casefold()


def unique_destination_name(path: Path, used: dict, diagnostics=None, label: str = "asset") -> str:
    """A file name for ``path`` that no other source already holds in ``used``.

    ``used`` maps :func:`destination_name_key` to the source path that claimed
    it. The same source always gets the same name; a different source whose
    name collides gets the name plus a short digest of its path, so the result
    is deterministic from one export to the next.
    """
    name = path.name
    name_key = destination_name_key(name)
    existing = used.get(name_key)
    if existing is None or existing == path:
        used[name_key] = path
        return name

    stem, suffix = path.stem, path.suffix
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    candidate = f"{stem}_{digest}{suffix}"
    counter = 1
    candidate_key = destination_name_key(candidate)
    while candidate_key in used and used[candidate_key] != path:
        candidate = f"{stem}_{digest}_{counter}{suffix}"
        candidate_key = destination_name_key(candidate)
        counter += 1
    used[candidate_key] = path

    if diagnostics:
        diagnostics.add_warning(
            f"Renamed {label} '{name}' to '{candidate}' to avoid a name collision."
        )
    return candidate
