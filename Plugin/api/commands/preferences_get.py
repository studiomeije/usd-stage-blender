"""preferences_get command — read addon-level preferences."""

from __future__ import annotations

_PREF_KEYS = (
    "usdzip_path",
)


def handle(args: dict) -> dict:
    import bpy
    from ... import prefs as addon_prefs

    prefs = addon_prefs.get_preferences(bpy.context)
    if prefs is None:
        raise RuntimeError("USD Stage for Blender add-on preferences not available.")

    return {key: getattr(prefs, key, None) for key in _PREF_KEYS}
