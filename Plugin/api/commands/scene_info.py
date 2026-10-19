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
    file will export without exporting it, so a set naming a deleted
    collection appears here with a null collection instead of being hidden.
    """
    return [
        {
            "name": variant_set.name,
            "default_variant": variant_set.default_variant,
            "variants": [
                {
                    "name": variant.name,
                    "collection": (
                        variant.collection.name if variant.collection else None
                    ),
                }
                for variant in variant_set.variants
            ],
        }
        for variant_set in getattr(scene, "usd_stage_variant_sets", None) or ()
    ]
