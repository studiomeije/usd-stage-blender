"""
Shader Editor add-menu integration for RealityKit nodes.
"""

import re

import bpy

from ..nodes import metadata as rk_metadata

MENU_IDNAME = "USDSTAGE_MT_shader_nodes"

_section_menus = []


def _add_node_item(layout, entry):
    props = layout.operator("usdstage.add_rk_node", text=entry["label"])
    props.rk_node_id = entry["id"]
    if entry["id"] in {"rk_pbr", "rk_unlit"}:
        props.auto_connect = True


def _sections():
    """Catalog entries grouped by section, sections in catalog order."""
    grouped = {}
    for entry in rk_metadata.get_node_catalog():
        grouped.setdefault(entry.get("section") or "Misc", []).append(entry)
    return grouped


def _section_menu_idname(section: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", section).strip("_").lower() or "misc"
    return f"{MENU_IDNAME}_{slug}"


def _make_section_menu(section: str):
    def draw(self, _context):
        for entry in _sections().get(section, ()):
            _add_node_item(self.layout, entry)

    return type(
        _section_menu_idname(section),
        (bpy.types.Menu,),
        {"bl_idname": _section_menu_idname(section), "bl_label": section, "draw": draw},
    )


class USDSTAGE_MT_shader_nodes(bpy.types.Menu):
    """RealityKit node menu for the Shader Editor, one submenu per section."""

    bl_label = "RealityKit Nodes"
    bl_idname = MENU_IDNAME

    def draw(self, _context):
        layout = self.layout
        sections = _sections()
        if not sections:
            layout.label(text="No RealityKit nodes found")
            return
        for section in sections:
            layout.menu(_section_menu_idname(section))


def _draw_add_menu(self, context):
    layout = self.layout
    layout.separator()
    if getattr(context, "is_menu_search", False):
        for entries in _sections().values():
            for entry in entries:
                _add_node_item(layout, entry)
    else:
        layout.menu(MENU_IDNAME)


def register():
    """Register the RealityKit Nodes menu in the Shader Editor."""
    if bpy.app.background:
        return
    for section in _sections():
        menu = _make_section_menu(section)
        bpy.utils.register_class(menu)
        _section_menus.append(menu)
    bpy.utils.register_class(USDSTAGE_MT_shader_nodes)
    bpy.types.NODE_MT_shader_node_add_all.append(_draw_add_menu)


def unregister():
    """Unregister the RealityKit Nodes menu."""
    if bpy.app.background:
        return
    try:
        bpy.types.NODE_MT_shader_node_add_all.remove(_draw_add_menu)
    except Exception:
        pass
    for menu in (USDSTAGE_MT_shader_nodes, *reversed(_section_menus)):
        try:
            bpy.utils.unregister_class(menu)
        except Exception:
            pass
    _section_menus.clear()
