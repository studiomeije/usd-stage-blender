"""Which Blender objects an export covers, and how one is identified.

Both answers are needed before any export machinery is reachable - animation
staging, variant planning and selection closure all ask them - and neither
touches ``bpy``.  Keeping them here lets the callers that only need these two
facts stay importable outside Blender, and keeps one definition of "exportable"
for every caller to agree on.
"""

from __future__ import annotations

#: Object types the RealityKit delivery contract never exports. RealityKit's
#: cross-platform renderer profile cannot consume these ordinary USD schemas,
#: and the native exporter's flags for them are hard-disabled too, so selection
#: filtering must never admit them.
NEVER_EXPORTED_TYPES = frozenset(
    {"LIGHT", "CAMERA", "CURVE", "CURVES", "POINTCLOUD", "VOLUME"}
)


def rna_identity(value) -> int:
    """Stable Blender datablock identity, with a test-double fallback."""
    try:
        return int(value.as_pointer())
    except Exception:
        return id(value)


def is_exportable_object(obj, settings) -> bool:
    """Whether an object belongs in the export scope.

    Meshes always export: the USD exporter's mesh flag is hard-coded on
    (``_build_export_kwargs``), so no setting can drop one. Armatures follow
    ``export_armatures``. Empties are real USD Xforms and collection-instance
    roots; any other Blender object class is out of scope, so selected-only
    validation cannot succeed on a selection that would publish only the
    synthetic export root.
    """
    object_type = str(getattr(obj, "type", ""))
    if object_type == "MESH":
        return True
    if object_type == "ARMATURE":
        return bool(getattr(settings, "export_armatures", True))
    if object_type in NEVER_EXPORTED_TYPES:
        return False
    return object_type == "EMPTY"


def view_layer_objects(context, settings=None) -> list:
    """The scene objects Blender's USD exporter writes, in scene order.

    Blender leaves out objects in collections excluded from the view layer,
    and follows ``evaluation_mode``: ``RENDER`` skips objects and collections
    disabled in renders, ``VIEWPORT`` those disabled in viewports. Hiding with
    the outliner's eye does not leave anything out. The export scope does the
    same. Collection-instance prototypes hidden this way still
    reach the processing scope through their instancer.
    """
    scene_objects = list(context.scene.objects)
    if settings is None:
        settings = getattr(getattr(context, "scene", None), "usd_stage_export_settings", None)
    viewport = str(getattr(settings, "evaluation_mode", "RENDER")) == "VIEWPORT"
    hidden_flag = "hide_viewport" if viewport else "hide_render"
    view_layer = getattr(context, "view_layer", None)
    root = getattr(view_layer, "layer_collection", None)
    if root is None:
        layer_objects = getattr(view_layer, "objects", None)
        if layer_objects is None:
            return scene_objects
        in_layer = {rna_identity(obj) for obj in layer_objects}
        return [obj for obj in scene_objects if rna_identity(obj) in in_layer]

    visible: set[int] = set()
    stack = [root]
    while stack:
        layer_collection = stack.pop()
        collection = layer_collection.collection
        # A collection's hide flag hides its whole subtree, but ``exclude``
        # does not: a child included under an excluded parent still exports
        # its objects, and variant staging relies on exactly that.
        if bool(getattr(collection, hidden_flag, False)):
            continue
        if not bool(getattr(layer_collection, "exclude", False)):
            for obj in collection.objects:
                if not bool(getattr(obj, hidden_flag, False)):
                    visible.add(rna_identity(obj))
        stack.extend(layer_collection.children)
    return [obj for obj in scene_objects if rna_identity(obj) in visible]
