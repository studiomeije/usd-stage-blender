"""
Command registry for the USD Stage for Blender headless API.

Each command module exposes a ``handle(args)`` function that receives a dict
and returns a JSON-serialisable dict.
"""

from . import (
    version,
    scene_info,
    list_objects,
    list_materials,
    validate,
    settings_get,
    settings_set,
    settings_list,
    export,
    bake_export,
    support_bundle,
    preferences_get,
    preferences_set,
)

REGISTRY = {
    "version": version.handle,
    "info": scene_info.handle,
    "list_objects": list_objects.handle,
    "list_materials": list_materials.handle,
    "validate": validate.handle,
    "settings_get": settings_get.handle,
    "settings_set": settings_set.handle,
    "settings_list": settings_list.handle,
    "export": export.handle,
    "bake_export": bake_export.handle,
    "support_bundle": support_bundle.handle,
    "preferences_get": preferences_get.handle,
    "preferences_set": preferences_set.handle,
}
