"""list_materials command — return materials in the file."""

from __future__ import annotations


def handle(args: dict) -> list:
    import bpy

    include_unused = args.get("unused", False)
    scene = bpy.context.scene

    # Collect materials used by scene objects
    scene_materials = set()
    for obj in scene.objects:
        for slot in getattr(obj, "material_slots", []):
            if slot.material:
                scene_materials.add(slot.material.name)

    results = []
    for mat in bpy.data.materials:
        in_scene = mat.name in scene_materials
        if not in_scene and not include_unused:
            continue

        entry = {
            "name": mat.name,
            "users": mat.users,
            "node_count": len(mat.node_tree.nodes),
        }
        results.append(entry)

    return results
