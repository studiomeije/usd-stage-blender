"""validate command — check materials for RealityKit compatibility."""

from __future__ import annotations

from ._settings_common import (
    BOOLEAN_FALSE_TOKENS,
    BOOLEAN_TRUE_TOKENS,
    get_settings,
)
from ..errors import CommandError



def _resolve_normalization_policy(args: dict, settings) -> bool:
    """Resolve the explicit opt-in without truthy-string surprises."""
    requested = args.get("clamp_specular_tint")
    if requested is None:
        return bool(getattr(settings, "clamp_specular_tint", False))
    if isinstance(requested, bool):
        return requested
    token = str(requested).strip().casefold()
    if token in BOOLEAN_TRUE_TOKENS:
        return True
    if token in BOOLEAN_FALSE_TOKENS:
        return False
    raise CommandError(
        "Invalid clamp_specular_tint value. "
        f"Allowed true values: {list(BOOLEAN_TRUE_TOKENS)}; "
        f"false values: {list(BOOLEAN_FALSE_TOKENS)}",
        code="INVALID_ARGUMENTS",
    )


def handle(args: dict) -> dict:
    import bpy
    from ...nodes import validate as rk_validate

    material_name = args.get("material")
    only_errors = args.get("only_errors", False)
    settings = get_settings()
    clamp_specular_tint = _resolve_normalization_policy(args, settings)

    if material_name:
        mat = bpy.data.materials.get(material_name)
        if mat is None:
            raise CommandError(
                f"Material not found: '{material_name}'",
                code="MATERIAL_NOT_FOUND",
                details={"material": material_name},
            )
        materials = [mat]
    else:
        materials = rk_validate.collect_scene_materials(bpy.context)

    results = []
    total_errors = 0
    total_warnings = 0

    for mat in materials:
        result = rk_validate.validate_material(
            mat,
            strict=True,
            clamp_specular_tint=clamp_specular_tint,
        )
        entry = {
            "name": result["material"],
            "ok": result["ok"],
            "errors": [
                {
                    "node_name": e.get("node_name", ""),
                    "node_type": e.get("node_type", ""),
                    "message": e.get("message", ""),
                }
                for e in result["errors"]
            ],
        }
        if not only_errors:
            entry["warnings"] = [
                {
                    "node_name": w.get("node_name", ""),
                    "node_type": w.get("node_type", ""),
                    "message": w.get("message", ""),
                }
                for w in result["warnings"]
            ]
            total_warnings += len(result["warnings"])
        total_errors += len(result["errors"])
        results.append(entry)

    all_ok = total_errors == 0
    summary = {
        "ok": all_ok,
        "error_count": total_errors,
        "clamp_specular_tint": clamp_specular_tint,
        "materials": results,
    }
    if not only_errors:
        summary["warning_count"] = total_warnings
    return summary
