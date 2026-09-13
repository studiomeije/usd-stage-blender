"""
Shader Editor panel for RealityKit compatibility status.
"""

import bpy
from bpy.types import Panel

from ..ops.validation_operators import (
    bake_profile_label,
    export_settings,
    get_active_material,
    validate_active_material,
)
from . import brand
from .draw_utils import draw_issue_list


class USDSTAGE_PT_shader_validation(Panel):
    """RealityKit compatibility status panel."""
    bl_label = "RealityKit Compatibility"
    bl_idname = "USDSTAGE_PT_shader_validation"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "USD Stage"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return space and space.type == 'NODE_EDITOR' and space.tree_type == 'ShaderNodeTree'

    def draw_header(self, context):
        brand.draw_header_icon(self.layout, 'NODE_MATERIAL')

    def draw(self, context):
        layout = self.layout
        material = get_active_material(context)
        if not material:
            layout.label(text="No active material", icon='INFO')
            return

        result = validate_active_material(context, material)
        settings = export_settings(context)
        if getattr(settings, "clamp_specular_tint", False):
            layout.label(text="Clamp Overbright Specular Tint is on", icon='INFO')
        bake_label = bake_profile_label(context)
        if bake_label:
            # Baking renders the node graph to textures, so nodes that direct
            # translation refuses still export; list them for Translate Materials.
            layout.label(text="Bakes on export", icon='CHECKMARK')
            layout.label(text=f"Profile: {bake_label}")
        elif result["errors"]:
            status = layout.row()
            status.alert = True
            status.label(text="Incompatible material", icon='ERROR')
        elif result["warnings"]:
            layout.label(text="Compatible with warnings", icon='INFO')
        else:
            layout.label(text="Compatible", icon='CHECKMARK')

        def _issue_text(issue):
            return f"{issue['node_name']}: {issue['message']}"

        draw_issue_list(
            layout, result["errors"],
            title="Refused by Translate Materials" if bake_label else "Errors",
            icon='INFO' if bake_label else 'ERROR',
            alert=not bake_label,
            max_items=6,
            format_item=_issue_text,
        )
        draw_issue_list(
            layout, result["warnings"],
            title="Warnings", icon='INFO', max_items=6,
            format_item=_issue_text,
        )

        layout.separator()
        layout.operator("usdstage.validate_material", icon='CHECKMARK')
        layout.operator("usdstage.select_offending_nodes", icon='RESTRICT_SELECT_OFF')


def register():
    """Register shader editor panels."""
    bpy.utils.register_class(USDSTAGE_PT_shader_validation)


def unregister():
    """Unregister shader editor panels."""
    bpy.utils.unregister_class(USDSTAGE_PT_shader_validation)
