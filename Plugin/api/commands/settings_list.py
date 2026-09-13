"""settings_list command — list all available setting keys with schema info."""

from __future__ import annotations


def handle(args: dict) -> list:
    """Return schema for all export settings.

    This command works even without a .blend file loaded — it inspects the
    PropertyGroup class definition rather than a live instance.
    """
    from ._settings_common import (
        SETTING_GROUPS,
        INTERNAL_KEYS,
    )

    # We need the PropertyGroup registered to inspect bl_rna.
    # In background mode the addon should already be loaded by the runner.
    import bpy
    settings = bpy.context.scene.usd_stage_export_settings

    # Reverse-map key → group
    key_to_group = {}
    for group_name, keys in SETTING_GROUPS.items():
        for key in keys:
            key_to_group[key] = group_name

    results = []
    for prop in settings.bl_rna.properties:
        key = prop.identifier
        if key in INTERNAL_KEYS:
            continue

        entry = {
            "key": key,
            "type": prop.type,
            "description": prop.description or "",
            "group": key_to_group.get(key, "other"),
        }

        if prop.type == "ENUM":
            entry["values"] = [item.identifier for item in prop.enum_items]
            entry["default"] = prop.default
        elif prop.type == "BOOLEAN":
            entry["default"] = prop.default
        elif prop.type == "INT":
            entry["default"] = prop.default
            entry["min"] = prop.hard_min
            entry["max"] = prop.hard_max
        elif prop.type == "FLOAT":
            entry["default"] = prop.default
            entry["min"] = prop.hard_min
            entry["max"] = prop.hard_max
        elif prop.type == "STRING":
            entry["default"] = prop.default

        results.append(entry)

    return results
