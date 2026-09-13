"""settings_set command — modify export settings in a .blend file."""

from __future__ import annotations

from ._settings_common import (
    INTERNAL_KEYS,
    apply_setting_updates_transactionally,
    get_settings,
    prepare_setting_update,
    setting_value_issue,
)
from ..errors import CommandError


def handle(args: dict) -> dict:
    settings_dict = args.get("settings", {})
    save = args.get("save", False)
    dry_run = args.get("dry_run", False)

    if not settings_dict:
        raise ValueError("No settings provided. Pass 'settings': {'key': value, ...}")

    settings = get_settings()
    prop_defs = {prop.identifier: prop for prop in settings.bl_rna.properties}

    # Validate every key and value without touching the live PropertyGroup.
    to_apply = []
    invalid_keys = []
    invalid_values = []
    for key, value in settings_dict.items():
        if key in INTERNAL_KEYS:
            invalid_keys.append(
                setting_value_issue(key, value, "internal setting")
            )
            continue
        prop = prop_defs.get(key)
        if prop is None:
            invalid_keys.append(
                setting_value_issue(key, value, "unknown setting")
            )
            continue
        try:
            to_apply.append(
                prepare_setting_update(
                    prop,
                    value,
                    key=key,
                    source="settings_set",
                )
            )
        except Exception as exc:
            invalid_values.append(setting_value_issue(key, value, exc))

    if invalid_keys:
        raise CommandError(
            "Invalid setting key.",
            code="INVALID_SETTING_OVERRIDE",
            details=invalid_keys + invalid_values,
        )
    if invalid_values:
        raise CommandError(
            "Invalid setting value.",
            code="INVALID_SETTING_VALUE",
            details=invalid_values,
        )

    if dry_run:
        return {
            "valid": True,
            "would_update": [update.key for update in to_apply],
        }

    assignment_errors = apply_setting_updates_transactionally(settings, to_apply)
    if assignment_errors:
        raise CommandError(
            "Invalid setting value.",
            code="INVALID_SETTING_VALUE",
            details=assignment_errors,
        )
    updated = [update.key for update in to_apply]

    saved = False
    warnings = []
    if save:
        import bpy
        if bpy.data.filepath:
            status = bpy.ops.wm.save_mainfile()
            saved = 'FINISHED' in status
            if not saved:
                raise CommandError(
                    "Settings were applied but the .blend could not be saved.",
                    code="SETTINGS_SAVE_FAILED",
                    details={"status": sorted(status), "updated": updated},
                )
        else:
            raise CommandError(
                "Cannot save: the .blend has no filepath.",
                code="SETTINGS_SAVE_FAILED",
                details={"updated": updated},
            )
    else:
        # Every command runs in a short-lived `blender --background` worker that
        # exits immediately after this returns. Without --save the writes above
        # die with that process, so reporting them as "updated" alone reads as
        # success for a change that never reached the file.
        warnings.append(
            "Settings were applied to a temporary Blender session and NOT "
            "written to the .blend. Re-run with --save to persist them."
        )

    result = {"updated": updated, "saved": saved}
    if warnings:
        result["warnings"] = warnings
    return result
