"""preferences_set command — modify add-on preferences."""

from __future__ import annotations

from ..errors import CommandError

_PREF_KEYS = {
    "usdzip_path",
}


def handle(args: dict) -> dict:
    import bpy
    from ... import prefs as addon_prefs

    settings_dict = args.get("settings", {})
    if not settings_dict:
        raise CommandError(
            "No preferences provided. Pass key=value pairs.",
            code="INVALID_ARGUMENTS",
        )

    prefs = addon_prefs.get_preferences(bpy.context)
    if prefs is None:
        raise RuntimeError("USD Stage for Blender add-on preferences are not available.")

    updated = []
    for key, value in settings_dict.items():
        if key not in _PREF_KEYS:
            raise CommandError(
                f"Unknown preference key: '{key}'. Available: {sorted(_PREF_KEYS)}",
                code="UNKNOWN_PREFERENCE_KEY",
                details={"key": key, "available": sorted(_PREF_KEYS)},
            )

        try:
            setattr(prefs, key, value)
            updated.append(key)
        except Exception as exc:
            raise CommandError(
                f"Failed to set '{key}': {exc}",
                code="INVALID_PREFERENCE_VALUE",
                details={"key": key},
            ) from exc

    # Each CLI call is a fresh Blender process; without an explicit userpref
    # save the change would die with this process.
    bpy.ops.wm.save_userpref()

    return {"updated": updated}
