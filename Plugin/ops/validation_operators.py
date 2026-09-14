"""
Operators for RealityKit material validation.
"""

import bpy
from bpy.types import Operator

from ..export_profile import ui_bake_profile_label
from ..nodes import validate as rk_validate


def get_active_material(context):
    """Resolve the active material from the current context."""
    if context.material:
        return context.material
    space = context.space_data
    if space and space.type == 'NODE_EDITOR' and space.tree_type == 'ShaderNodeTree':
        if getattr(space, "id", None) and hasattr(space.id, "node_tree"):
            return space.id
    obj = context.active_object
    if obj:
        return obj.active_material
    return None


def export_settings(context):
    scene = getattr(context, "scene", None)
    return getattr(scene, "usd_stage_export_settings", None)


def validate_active_material(context, material):
    settings = export_settings(context)
    return rk_validate.validate_material(
        material,
        strict=True,
        clamp_specular_tint=bool(getattr(settings, "clamp_specular_tint", False)),
    )


def bake_profile_label(context) -> str | None:
    """The chosen Profile's label when it bakes materials instead of translating them."""
    settings = export_settings(context)
    return ui_bake_profile_label(settings) if settings is not None else None


class USDSTAGE_OT_validate_material(Operator):
    """Validate the active material against RealityKit rules."""
    bl_idname = "usdstage.validate_material"
    bl_label = "Validate RealityKit Material"
    bl_options = {'REGISTER'}

    def execute(self, context):
        material = get_active_material(context)
        if not material:
            self.report({'WARNING'}, "No active material to validate")
            return {'CANCELLED'}

        result = validate_active_material(context, material)
        bake_label = bake_profile_label(context)
        if result["errors"] and bake_label:
            self.report(
                {'INFO'},
                f"'{material.name}' bakes with {bake_label}; "
                f"Translate Materials would refuse {len(result['errors'])} node(s)",
            )
            return {'FINISHED'}
        if result["errors"]:
            self.report({'ERROR'}, f"{len(result['errors'])} errors found in '{material.name}'")
            return {'FINISHED'}
        if result["warnings"]:
            self.report({'WARNING'}, f"{len(result['warnings'])} warnings found in '{material.name}'")
            return {'FINISHED'}

        self.report({'INFO'}, f"'{material.name}' is RealityKit-compatible")
        return {'FINISHED'}


class USDSTAGE_OT_select_offenders(Operator):
    """Select offending nodes in the active material."""
    bl_idname = "usdstage.select_offending_nodes"
    bl_label = "Select Offending Nodes"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        material = get_active_material(context)
        if not material:
            self.report({'WARNING'}, "No active material to inspect")
            return {'CANCELLED'}

        result = validate_active_material(context, material)
        if not result["offending_nodes"]:
            self.report({'INFO'}, "No offending nodes found")
            return {'FINISHED'}

        selected = rk_validate.select_offending_nodes(material, result)
        self.report({'INFO'}, f"Selected {selected} offending nodes")
        return {'FINISHED'}


def register():
    """Register validation operators."""
    bpy.utils.register_class(USDSTAGE_OT_validate_material)
    bpy.utils.register_class(USDSTAGE_OT_select_offenders)


def unregister():
    """Unregister validation operators."""
    bpy.utils.unregister_class(USDSTAGE_OT_select_offenders)
    bpy.utils.unregister_class(USDSTAGE_OT_validate_material)
