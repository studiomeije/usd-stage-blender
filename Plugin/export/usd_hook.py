"""Scoped USDHook capture of the exporter's prim map.

Blender's USD exporter exposes ``export_context.get_prim_map()`` to
``on_export`` hooks: an exact mapping from exported USD prim paths to the
Blender IDs that produced them. Capturing it during export lets the MaterialX
rewrite resolve material prims to Blender materials directly instead of
guessing by name.

The hook class is registered only for the duration of the plugin's own
``wm.usd_export`` call so it never runs for unrelated USD exports.
"""

from __future__ import annotations

from contextlib import contextmanager

_captured: dict = {"materials": None, "mesh_materials": None}


def consume_captured_material_map() -> dict | None:
    """Return and clear {material prim path: material name} from the last export.

    Consuming prevents a stale map from a previous export leaking into a
    postprocess run on an unrelated stage.
    """
    materials = _captured.get("materials")
    _captured["materials"] = None
    return materials


def consume_captured_mesh_material_map() -> dict | None:
    """Return and clear {mesh prim path: Blender material names its faces use}.

    The exporter records one Blender material per material prim, so two
    materials whose names reduce to the same prim name leave no trace of the
    one that lost. Keeping the Blender side per mesh is what detects that
    collapse; see ``materials.rewrite._require_distinct_material_prims``.
    """
    mesh_materials = _captured.get("mesh_materials")
    _captured["mesh_materials"] = None
    return mesh_materials


def _used_material_names(mesh) -> list:
    """Distinct Blender material names the mesh's faces actually reference.

    Slots no face uses are left out: the exporter writes no material prim for
    them, so counting them would report a collision that never happened.
    """
    slots = list(getattr(mesh, "materials", None) or ())
    polygons = getattr(mesh, "polygons", None)
    if not slots or not polygons:
        return []
    if len(slots) == 1:
        return [slots[0].name] if slots[0] is not None else []

    # foreach_get fills the buffer in C; a Python loop over the faces of a
    # dense mesh is slow enough to be felt inside the exporter's own hook.
    indices = [0] * len(polygons)
    try:
        polygons.foreach_get("material_index", indices)
    except Exception:
        indices = [getattr(polygon, "material_index", 0) for polygon in polygons]

    names: list = []
    for index in sorted(set(indices)):
        if not 0 <= index < len(slots):
            continue
        material = slots[index]
        if material is not None and material.name not in names:
            names.append(material.name)
    return names


@contextmanager
def capture_prim_map():
    """Register a USDHook around a ``wm.usd_export`` call and capture its prim map."""
    import bpy

    _captured["materials"] = None
    _captured["mesh_materials"] = None

    class USDSTAGE_USD_prim_map_hook(bpy.types.USDHook):
        bl_idname = "usdstage_prim_map_capture"
        bl_label = "USD Stage for Blender prim map capture"

        @staticmethod
        def on_export(export_context):
            try:
                prim_map = export_context.get_prim_map()
            except Exception:
                return True
            materials: dict[str, str] = {}
            mesh_materials: dict[str, list] = {}
            # Two independent scans: a prim path that reported both a mesh
            # and a material would otherwise lose whichever kind came second.
            for prim_path, ids in prim_map.items():
                path = str(prim_path)
                for id_datablock in ids:
                    if isinstance(id_datablock, bpy.types.Material):
                        materials[path] = id_datablock.name
                        break
                for id_datablock in ids:
                    if isinstance(id_datablock, bpy.types.Mesh):
                        mesh_materials[path] = _used_material_names(id_datablock)
                        break
            _captured["materials"] = materials
            _captured["mesh_materials"] = mesh_materials
            return True

    bpy.utils.register_class(USDSTAGE_USD_prim_map_hook)
    try:
        yield
    finally:
        try:
            bpy.utils.unregister_class(USDSTAGE_USD_prim_map_hook)
        except Exception:
            pass
