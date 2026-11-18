"""info command — return scene metadata."""

from __future__ import annotations


def handle(args: dict) -> dict:
    import bpy

    scene = bpy.context.scene
    blend_path = bpy.data.filepath or None

    # Count materials used in scene
    materials = set()
    for obj in scene.objects:
        for slot in getattr(obj, "material_slots", []):
            if slot.material:
                materials.add(slot.material.name)

    return {
        "file": blend_path,
        "scene": scene.name,
        "frame_range": [scene.frame_start, scene.frame_end],
        "fps": scene.render.fps,
        "unit_system": scene.unit_settings.system,
        "unit_scale": scene.unit_settings.scale_length,
        "object_count": len(scene.objects),
        "material_count": len(materials),
        "variant_sets": _variant_sets(scene),
    }


def _variant_sets(scene) -> list:
    """The variant sets this .blend declares, before any export runs.

    Reported as declared rather than as resolved: the point is to show what a
    file will export without exporting it, so a shape variant naming a deleted
    collection, or a finish with no material, appears with a null instead of
    being hidden.
    """
    return [
        _variant_set(variant_set)
        for variant_set in getattr(scene, "usd_stage_variant_sets", None) or ()
    ]


def _name_or_none(datablock):
    return datablock.name if datablock else None


def _variant_set(variant_set) -> dict:
    if getattr(variant_set, "kind", "GEOMETRY") != "MATERIAL":
        return {
            "name": variant_set.name,
            "kind": "GEOMETRY",
            "default_variant": variant_set.default_variant,
            "variants": [
                {"name": variant.name, "collection": _name_or_none(variant.collection)}
                for variant in variant_set.variants
            ],
        }

    entry = {
        "name": variant_set.name,
        "kind": "MATERIAL",
        "default_variant": variant_set.default_variant,
        "target_collection": _name_or_none(getattr(variant_set, "target_collection", None)),
    }
    parent = str(getattr(variant_set, "parent_set", "") or "")
    if parent:
        entry["nested_in"] = parent
    variants = []
    for variant in variant_set.variants:
        item = {"name": variant.name, "material": _name_or_none(getattr(variant, "material", None))}
        offered_on = [
            name.strip()
            for name in str(getattr(variant, "applies_to", "") or "").split(",")
            if name.strip()
        ]
        if parent and offered_on:
            item["offered_on"] = offered_on
        variants.append(item)
    entry["variants"] = variants
    return entry
