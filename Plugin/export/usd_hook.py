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

_captured: dict = {"materials": None}


def consume_captured_material_map() -> dict | None:
    """Return and clear {material prim path: material name} from the last export.

    Consuming prevents a stale map from a previous export leaking into a
    postprocess run on an unrelated stage.
    """
    materials = _captured.get("materials")
    _captured["materials"] = None
    return materials


@contextmanager
def capture_prim_map():
    """Register a USDHook around a ``wm.usd_export`` call and capture its prim map."""
    import bpy

    _captured["materials"] = None

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
            for prim_path, ids in prim_map.items():
                for id_datablock in ids:
                    if isinstance(id_datablock, bpy.types.Material):
                        materials[str(prim_path)] = id_datablock.name
                        break
            _captured["materials"] = materials
            return True

    bpy.utils.register_class(USDSTAGE_USD_prim_map_hook)
    try:
        yield
    finally:
        try:
            bpy.utils.unregister_class(USDSTAGE_USD_prim_map_hook)
        except Exception:
            pass
