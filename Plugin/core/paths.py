"""
Centralized path helpers for the USD Stage for Blender add-on.
"""

from pathlib import Path


def addon_root() -> Path:
    """Return the add-on root directory."""
    return Path(__file__).resolve().parent.parent


def assets_path() -> Path:
    """Return the bundled assets directory."""
    return addon_root() / "assets"


def nodegroups_asset_path() -> Path:
    """Return the bundled nodegroup asset path."""
    return assets_path() / "nodegroups.blend"


def manifest_path() -> Path:
    """Return the bundled MaterialX manifest path."""
    return addon_root() / "manifest" / "rk_nodes_manifest.json"
