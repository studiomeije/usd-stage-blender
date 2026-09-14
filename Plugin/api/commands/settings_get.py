"""settings_get command — read export settings from a .blend file."""

from __future__ import annotations

from ._settings_common import SETTING_GROUPS, INTERNAL_KEYS, get_settings
from ..errors import CommandError


def handle(args: dict) -> dict:
    keys = args.get("keys")
    group = args.get("group", "all")

    settings = get_settings()

    # Determine which keys to return
    if keys:
        valid_keys = {prop.identifier for prop in settings.bl_rna.properties} - set(INTERNAL_KEYS)
        unknown = sorted(set(keys) - valid_keys)
        if unknown:
            raise CommandError(
                f"Unknown setting key(s): {', '.join(unknown)}. Use 'settings list' to see valid keys.",
                code="UNKNOWN_SETTING_KEY",
                details={"keys": unknown},
            )
        allowed = set(keys)
    elif group and group != "all":
        allowed = SETTING_GROUPS.get(group)
        if allowed is None:
            raise CommandError(
                f"Unknown group: '{group}'. Available: {sorted(SETTING_GROUPS.keys())}",
                code="UNKNOWN_SETTING_GROUP",
                details={"group": group, "available": sorted(SETTING_GROUPS.keys())},
            )
    else:
        allowed = None  # all keys

    result = {}
    for prop in settings.bl_rna.properties:
        key = prop.identifier
        if key in INTERNAL_KEYS:
            continue
        if allowed is not None and key not in allowed:
            continue
        try:
            result[key] = getattr(settings, key)
        except Exception:
            continue

    return result
