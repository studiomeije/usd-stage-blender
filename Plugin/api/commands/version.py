"""version command — return plugin, Blender, and Python versions."""

from __future__ import annotations


def handle(args: dict) -> dict:
    import bpy
    import sys

    plugin_version = "unknown"
    try:
        from ...core.version import get_version
        plugin_version = get_version()
    except Exception:
        pass

    return {
        "plugin": plugin_version,
        "blender": ".".join(str(v) for v in bpy.app.version),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    }
