"""
USD Stage for Blender Add-on

A Blender extension to export scenes to Reality Composer Pro format.
"""

_needs_reload = "bpy" in locals()

try:
    import bpy as _bpy  # type: ignore
    # Sanity-check: some environments may have a stub `bpy` module installed.
    from bpy.props import StringProperty as _StringProperty  # noqa: F401
except Exception:  # Allows importing non-Blender utilities from this package.
    bpy = None
else:
    bpy = _bpy


MINIMUM_BLENDER_VERSION = (5, 2, 0)


def require_supported_blender_version() -> tuple[int, int, int]:
    """Fail closed unless Blender 5.2 or newer is running.

    The extension manifest prevents normal installation in older Blender
    versions, but command-line and source-tree execution can bypass that UI
    check. Keep a runtime gate as the authoritative safety boundary before any
    classes are registered or an export mutates scene/output state.
    """
    if bpy is None:
        raise RuntimeError("USD Stage for Blender must be run inside Blender (bpy not available).")

    app = getattr(bpy, "app", None)
    version = tuple(int(part) for part in getattr(app, "version", (0, 0, 0))[:3])
    if version < MINIMUM_BLENDER_VERSION:
        required = ".".join(str(part) for part in MINIMUM_BLENDER_VERSION)
        detected = ".".join(str(part) for part in version)
        raise RuntimeError(
            f"USD Stage for Blender requires Blender {required} or newer; "
            f"detected Blender {detected}."
        )
    return version


if bpy is not None:
    from . import prefs as prefs_module
    from . import ui as ui_module
    from . import ops as ops_module
else:
    prefs_module = None
    ui_module = None
    ops_module = None

if bpy is not None and _needs_reload:
    import importlib
    prefs_module = importlib.reload(prefs_module)
    ui_module = importlib.reload(ui_module)
    ops_module = importlib.reload(ops_module)


def register():
    """Register the add-on's classes and properties."""
    require_supported_blender_version()

    prefs_module.register()
    ops_module.register()
    ui_module.register()

    # The MaterialX nodedef manifest ships with the extension. Loading it here
    # reports a damaged install when the add-on is enabled, not at first export.
    from .manifest.materialx_nodes import load_manifest

    try:
        load_manifest()
    except Exception as exc:
        print(f"Warning: {exc}")


def unregister():
    """Unregister the add-on's classes and properties."""
    if bpy is None:
        return

    ui_module.unregister()
    ops_module.unregister()
    prefs_module.unregister()


if __name__ == "__main__":
    register()
