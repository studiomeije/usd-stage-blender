"""
Add-on preferences for USD Stage for Blender
"""

from __future__ import annotations

import bpy
import json
from pathlib import Path
from bpy.props import StringProperty
from bpy.types import AddonPreferences

from .api.commands._settings_common import (
    INTERNAL_KEYS,
    REALITYKIT_OS27_PROFILE_NAME,
)


EXPORT_SETTINGS_PAYLOAD_SCHEMA = "usdstage.export-settings"
EXPORT_SETTINGS_PAYLOAD_VERSION = 1
EXPORT_SETTINGS_PROFILE = REALITYKIT_OS27_PROFILE_NAME
EXPORT_SETTINGS_SKIP_KEYS = frozenset({*INTERNAL_KEYS, "filepath"})


class USDStagePreferences(AddonPreferences):
    """Add-on preferences stored in Blender preferences"""
    bl_idname = __package__

    # USD tool paths
    usdzip_path: StringProperty(
        name="USDZ Packager Path",
        description="Path to usdzip tool (optional, will use Python fallback if empty)",
        default="",
        subtype='FILE_PATH',
        maxlen=1024
    )

    last_export_settings_json: StringProperty(
        name="Last Export Settings",
        description="Serialized last used export settings",
        default="",
        options={'HIDDEN'}
    )

    last_export_paths_json: StringProperty(
        name="Last Export Paths",
        description="Per-.blend export path mapping",
        default="",
        options={'HIDDEN'}
    )

    def draw(self, context):
        """Draw preferences UI"""
        layout = self.layout

        # USD tooling
        box = layout.box()
        box.label(text="USD Tooling", icon='SETTINGS')
        box.prop(self, "usdzip_path")
        box.label(text="Leave empty to use built-in Python packager", icon='INFO')



def get_preferences(context=None):
    """Get add-on preferences"""
    if context is None:
        context = bpy.context
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _settings_property_defs(settings) -> dict[str, object]:
    properties = getattr(getattr(settings, "bl_rna", None), "properties", [])
    return {
        prop.identifier: prop
        for prop in properties
        if getattr(prop, "identifier", None)
    }


def _raw_settings_keys(settings) -> set[str]:
    try:
        return {str(key) for key in settings.keys()}
    except Exception:
        return set()


def build_export_settings_payload(settings) -> dict:
    """Build the only persisted export-settings payload accepted."""
    values = {}
    for key in _settings_property_defs(settings):
        if key in EXPORT_SETTINGS_SKIP_KEYS:
            continue
        try:
            values[key] = getattr(settings, key)
        except Exception:
            continue
    return {
        "schema": EXPORT_SETTINGS_PAYLOAD_SCHEMA,
        "version": EXPORT_SETTINGS_PAYLOAD_VERSION,
        "profile": EXPORT_SETTINGS_PROFILE,
        "values": values,
    }


def serialize_export_settings_payload(settings) -> str:
    return json.dumps(
        build_export_settings_payload(settings),
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_export_settings_payload(serialized: str) -> tuple[str, dict | None]:
    if not serialized:
        return "missing", None
    try:
        payload = json.loads(serialized)
    except Exception:
        return "invalid", None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != EXPORT_SETTINGS_PAYLOAD_SCHEMA
        or payload.get("version") != EXPORT_SETTINGS_PAYLOAD_VERSION
        or payload.get("profile") != EXPORT_SETTINGS_PROFILE
        or not isinstance(payload.get("values"), dict)
    ):
        return "invalid", None
    return "current", payload["values"]


def _persisted_value_is_valid(prop, value) -> bool:
    prop_type = getattr(prop, "type", None)
    if prop_type == "BOOLEAN":
        return type(value) is bool
    if prop_type == "INT":
        return type(value) is int
    if prop_type == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if prop_type == "STRING":
        return isinstance(value, str)
    if prop_type == "ENUM":
        if not isinstance(value, str):
            return False
        try:
            valid = {item.identifier for item in prop.enum_items}
        except Exception:
            return True
        return value in valid
    return True


def _apply_export_settings_values(settings, values: dict) -> bool:
    prop_defs = _settings_property_defs(settings)
    expected_keys = {
        key for key in prop_defs if key not in EXPORT_SETTINGS_SKIP_KEYS
    }
    if set(values) != expected_keys:
        return False
    if any(
        not _persisted_value_is_valid(prop_defs[key], value)
        for key, value in values.items()
    ):
        return False

    previous = {}
    for key in values:
        try:
            previous[key] = getattr(settings, key)
        except Exception:
            return False

    try:
        settings.persist_suspended = True
    except Exception:
        pass
    try:
        for key, value in values.items():
            setattr(settings, key, value)
    except Exception:
        for key, value in previous.items():
            try:
                setattr(settings, key, value)
            except Exception:
                pass
        return False
    finally:
        try:
            settings.persist_suspended = False
        except Exception:
            pass
    return True


def persist_export_settings(context, settings, *, remember_path: bool = True) -> bool:
    """Persist current settings under the strict versioned profile."""
    prefs = get_preferences(context) if context is not None else None
    if not prefs:
        return False
    try:
        prefs.last_export_settings_json = serialize_export_settings_payload(settings)
    except Exception:
        return False
    if remember_path and context is not None:
        set_last_export_path(
            context,
            getattr(settings, "filepath", ""),
            getattr(getattr(context, "blend_data", None), "filepath", None),
        )
    return True


def scene_has_saved_settings(settings) -> bool:
    """Whether a scene already stores export settings of its own.

    Blender saves a PropertyGroup value only once it has been set, so a scene
    that never had a public setting changed carries none of those keys.
    """
    return bool(_raw_settings_keys(settings) - EXPORT_SETTINGS_SKIP_KEYS)


def apply_persisted_export_settings(context, settings) -> dict:
    """Seed a scene that has no saved settings with the last-used values.

    A scene that stores its own settings keeps them, so a .blend exports the
    same from the panel as from the command line. The remembered output path
    for the .blend applies whenever the scene has none.
    """
    if getattr(settings, "history_applied", False):
        return {"status": "already_applied"}
    settings.history_applied = True

    if not getattr(settings, "filepath", ""):
        apply_last_export_path(context, settings)

    if scene_has_saved_settings(settings):
        return {"status": "scene_settings"}

    prefs = get_preferences(context)
    if not prefs:
        return {"status": "no_preferences"}

    status, values = _decode_export_settings_payload(
        getattr(prefs, "last_export_settings_json", "")
    )
    if status == "current" and not _apply_export_settings_values(settings, values or {}):
        status = "invalid"
    return {"status": status}


def _blend_key(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except Exception:
        return str(path)


def get_last_export_path(context=None, blend_path: str | Path | None = None) -> str | None:
    prefs = get_preferences(context)
    if not prefs:
        return None
    key = _blend_key(blend_path)
    if key is None:
        if context is None:
            return None
        key = _blend_key(getattr(context.blend_data, "filepath", None))
    if key is None:
        return None
    try:
        data = json.loads(prefs.last_export_paths_json or "{}")
    except Exception:
        data = {}
    return data.get(key)


def set_last_export_path(
    context=None,
    export_path: str | None = None,
    blend_path: str | Path | None = None,
) -> None:
    if not export_path:
        return
    prefs = get_preferences(context)
    if not prefs:
        return
    key = _blend_key(blend_path)
    if key is None and context is not None:
        key = _blend_key(getattr(context.blend_data, "filepath", None))
    if key is None:
        return
    try:
        data = json.loads(prefs.last_export_paths_json or "{}")
    except Exception:
        data = {}
    data[key] = export_path
    try:
        prefs.last_export_paths_json = json.dumps(data)
    except Exception:
        pass


def apply_last_export_path(
    context=None,
    settings=None,
    blend_path: str | Path | None = None,
) -> bool:
    """Apply the last remembered output path for a .blend to export settings."""
    if settings is None:
        return False

    key_path = blend_path
    if key_path is None and context is not None:
        key_path = getattr(getattr(context, "blend_data", None), "filepath", None)

    if not key_path:
        try:
            settings.filepath = ""
        except Exception:
            pass
        return False

    last_path = get_last_export_path(context, key_path)
    if not last_path:
        return False

    try:
        settings.filepath = last_path
    except Exception:
        return False
    return True


def register():
    """Register add-on preferences."""
    bpy.utils.register_class(USDStagePreferences)


def unregister():
    """Unregister add-on preferences."""
    bpy.utils.unregister_class(USDStagePreferences)
