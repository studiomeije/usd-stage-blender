"""
Operators for inserting RealityKit node groups.
"""

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from ..core.paths import nodegroups_asset_path
from ..nodes import metadata as rk_metadata


def _group_matches_entry(group, entry) -> bool:
    """Return whether a group is the current packaged form of a catalog entry."""
    return bool(
        group
        and group.get("rk_id") == entry["id"]
        and group.get("rk_node_id") == entry.get("export_id")
        and group.get("rk_version") == rk_metadata.RK_NODE_VERSION
        and group.nodes
    )


def _load_nodegroup_from_asset(entry):
    """Load a node group from the bundled asset file."""
    asset_path = nodegroups_asset_path()
    if not asset_path.is_file():
        return None

    group_name = entry["group_name"]
    loaded_group = None
    try:
        with bpy.data.libraries.load(str(asset_path), link=False) as (data_from, data_to):
            if group_name not in data_from.node_groups:
                return None
            data_to.node_groups = [group_name]
        loaded_group = data_to.node_groups[0]
    except Exception:
        return None

    if loaded_group is None or loaded_group.name != group_name:
        if loaded_group is not None:
            bpy.data.node_groups.remove(loaded_group, do_unlink=True)
        return None
    if not _group_matches_entry(loaded_group, entry):
        bpy.data.node_groups.remove(loaded_group, do_unlink=True)
        return None
    return loaded_group


def _ensure_active_material(context):
    """The active object's material, created when it has none."""
    obj = context.active_object
    if not obj:
        return None

    material = obj.active_material
    if not material:
        material = bpy.data.materials.new(name="USD Stage Material")
        obj.active_material = material
    return material


def _get_target_node_tree(context):
    """Resolve the node tree where a new group node should be added."""
    space = context.space_data
    if space and space.type == 'NODE_EDITOR' and space.tree_type == 'ShaderNodeTree':
        if space.node_tree:
            return space.node_tree
        if getattr(space, "id", None) and hasattr(space.id, "node_tree"):
            return space.id.node_tree

    material = _ensure_active_material(context)
    if material and material.node_tree:
        return material.node_tree
    return None



def _insert_group_node(context, node_id: str, auto_connect: bool = False):
    """Insert a RealityKit group node into the active material.

    Returns ``(node, None)``, or ``(None, reason)`` when nothing was inserted.
    """
    entry = rk_metadata.find_entry(node_id)
    if not entry:
        return None, f"No RealityKit node '{node_id}' is in the catalog"

    group_name = entry["group_name"]
    group = bpy.data.node_groups.get(group_name)
    if group is not None and not _group_matches_entry(group, entry):
        # A group of that name already in the file is left alone: other nodes
        # may use it.
        return None, (
            f"The node group '{group_name}' in this file is from another version of "
            "the add-on; rename or delete it, then insert again"
        )
    if group is None:
        group = _load_nodegroup_from_asset(entry)
    if not group:
        return None, f"The node group '{group_name}' could not be loaded from the add-on"

    node_tree = _get_target_node_tree(context)
    if not node_tree:
        return None, "Select an object to insert the node group into its material"

    node = node_tree.nodes.new("ShaderNodeGroup")
    node.node_tree = group
    node.label = entry["label"]
    node.location = getattr(context.space_data, "cursor_location", (0.0, 0.0))
    node.select = True
    node_tree.nodes.active = node

    if auto_connect:
        _auto_connect_to_output(node_tree, node)

    return node, None


def _auto_connect_to_output(node_tree, group_node):
    """Connect a group node to the active Material Output if possible."""
    if not node_tree:
        return

    output_nodes = [n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL']
    if not output_nodes:
        return

    active_output = None
    for node in output_nodes:
        if getattr(node, "is_active_output", False):
            active_output = node
            break
    if not active_output:
        active_output = output_nodes[0]

    surface = active_output.inputs.get("Surface")
    if surface is None:
        return
    if surface.is_linked:
        return

    # MaterialX surface definitions use the conventional output name ``out``;
    # older hand-authored groups used ``Shader``.  Only connect a shader or
    # closure socket, and keep the preference order deterministic so a group
    # with multiple outputs cannot accidentally feed a numeric value into the
    # material surface.
    shader_output = None
    for output_name in ("Shader", "out"):
        candidate = group_node.outputs.get(output_name)
        if candidate is None:
            continue
        if getattr(candidate, "type", "") in {"SHADER", "CLOSURE"} or getattr(
            candidate, "bl_idname", ""
        ) in {"NodeSocketShader", "NodeSocketClosure"}:
            shader_output = candidate
            break
    if shader_output is None:
        return
    node_tree.links.new(shader_output, surface)


class USDSTAGE_OT_add_rk_node(Operator):
    """Insert a RealityKit node group from the catalog."""
    bl_idname = "usdstage.add_rk_node"
    bl_label = "Add RealityKit Node"
    bl_options = {'REGISTER', 'UNDO'}

    rk_node_id: StringProperty()
    auto_connect: BoolProperty(default=False)

    def execute(self, context):
        node, reason = _insert_group_node(context, self.rk_node_id, auto_connect=self.auto_connect)
        if not node:
            self.report({'ERROR'}, reason)
            return {'CANCELLED'}
        return {'FINISHED'}


class USDSTAGE_OT_insert_pbr_group(Operator):
    """Insert RealityKit PBR node group"""
    bl_idname = "usdstage.insert_pbr_group"
    bl_label = "Insert RealityKit PBR Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node, reason = _insert_group_node(context, "rk_pbr", auto_connect=True)
        if not node:
            self.report({'ERROR'}, reason)
            return {'CANCELLED'}
        return {'FINISHED'}


class USDSTAGE_OT_insert_unlit_group(Operator):
    """Insert RealityKit Unlit node group"""
    bl_idname = "usdstage.insert_unlit_group"
    bl_label = "Insert RealityKit Unlit Group"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node, reason = _insert_group_node(context, "rk_unlit", auto_connect=True)
        if not node:
            self.report({'ERROR'}, reason)
            return {'CANCELLED'}
        return {'FINISHED'}


def register():
    """Register node group operators."""
    bpy.utils.register_class(USDSTAGE_OT_add_rk_node)
    bpy.utils.register_class(USDSTAGE_OT_insert_pbr_group)
    bpy.utils.register_class(USDSTAGE_OT_insert_unlit_group)


def unregister():
    """Unregister node group operators."""
    bpy.utils.unregister_class(USDSTAGE_OT_insert_unlit_group)
    bpy.utils.unregister_class(USDSTAGE_OT_insert_pbr_group)
    bpy.utils.unregister_class(USDSTAGE_OT_add_rk_node)
