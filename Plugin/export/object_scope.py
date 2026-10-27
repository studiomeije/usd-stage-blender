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
