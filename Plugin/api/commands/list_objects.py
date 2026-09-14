"""list_objects command — return objects in the scene."""

from __future__ import annotations


def handle(args: dict) -> list:
    import bpy

    scene = bpy.context.scene
    type_filter = args.get("type")  # str or list of str
    selected_only = args.get("selected", False)

    if isinstance(type_filter, str):
        type_filter = [type_filter.upper()]
    elif isinstance(type_filter, list):
        type_filter = [t.upper() for t in type_filter]

    results = []
    for obj in scene.objects:
        if type_filter and obj.type not in type_filter:
            continue
        if selected_only and not obj.select_get():
            continue

        entry = {
            "name": obj.name,
            "type": obj.type,
            "visible": obj.visible_get(),
            "selected": obj.select_get(),
            "materials": [
                slot.material.name
                for slot in getattr(obj, "material_slots", [])
                if slot.material
            ],
        }

        if obj.type == "MESH" and obj.data:
            entry["vertices"] = len(obj.data.vertices)
        elif obj.type == "LIGHT" and obj.data:
            entry["light_type"] = obj.data.type

        results.append(entry)

    return results
