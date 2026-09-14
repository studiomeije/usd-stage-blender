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
    }
