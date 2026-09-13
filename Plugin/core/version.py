"""Single source of truth for add-on identity: blender_manifest.toml."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "blender_manifest.toml"


@lru_cache(maxsize=1)
def get_manifest_info() -> dict:
    """Return the parsed extension manifest (id, name, version, ...)."""
    with _MANIFEST_PATH.open("rb") as fh:
        return tomllib.load(fh)


def get_version() -> str:
    """Return the extension version string, e.g. ``"1.0.0"``."""
    return str(get_manifest_info().get("version", "unknown"))
