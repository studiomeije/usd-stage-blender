"""
Build the RealityKit node groups shipped in Plugin/assets/nodegroups.blend.

Used by scripts/build_nodegroups.py and scripts/check_nodegroup_parity.py; the
extension itself only loads the saved library.
"""

from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import bpy

from Plugin.nodes import metadata
from Plugin.nodes.metadata import RK_NODE_VERSION

from .handlers import get_preview_builder
from .preview import (
    PreviewHelpers,
    _basic_io,
    _output_kind,
    _link_mix_inputs,
    _connect_output,
    _new_mix_node,
    _build_float_mix_preview,
    _get_input_socket,
    _build_simple_shader_preview,
    _build_environment_radiance_preview,
    _build_view_direction_preview,
    _build_camera_position_preview,
    _build_normal_map_preview,
    _build_vector_math_preview,
    _build_ramp_preview,
    _build_split_preview,
    _build_combine3_preview,
    _build_swizzle_preview,
    _build_value_passthrough_preview,
    _build_map_range_preview,
    _build_clamp_preview,
    _build_smoothstep_preview,
    _build_if_preview,
    _build_logic_preview,
    _build_blend_preview,
    _build_inside_outside_preview,
    _build_switch_preview,
    _build_math_preview,
    _build_passthrough_preview,
)


INTERFACE_SOCKET_TYPES = {
    "NodeSocketFloat",
    "NodeSocketInt",
    "NodeSocketBool",
    "NodeSocketVector",
    "NodeSocketColor",
    "NodeSocketMenu",
    "NodeSocketShader",
    "NodeSocketBundle",
    "NodeSocketClosure",
}

MATH_NODE_OPS = {
    "absval": "ABSOLUTE",
    "acos": "ARCCOSINE",
    "asin": "ARCSINE",
    "atan2": "ARCTAN2",
    "ceil": "CEIL",
    "cos": "COSINE",
    "exp": "EXPONENT",
    "floor": "FLOOR",
    "fractional": "FRACT",
    "ln": "LOGARITHM",
    "max": "MAXIMUM",
    "min": "MINIMUM",
    "modulo": "MODULO",
    "power": "POWER",
    "safepower": "POWER",
    "round": "ROUND",
    "sign": "SIGN",
    "sin": "SINE",
    "sqrt": "SQRT",
    "step": "GREATER_THAN",
    "tan": "TANGENT",
    "add": "ADD",
    "subtract": "SUBTRACT",
    "multiply": "MULTIPLY",
    "divide": "DIVIDE",
    "oneminus": "SUBTRACT",
}

VECTOR_NODE_OPS = {
    "crossproduct": "CROSS_PRODUCT",
    "dot": "DOT_PRODUCT",
    "dotproduct": "DOT_PRODUCT",
    "normalize": "NORMALIZE",
    "magnitude": "LENGTH",
}

BLEND_MODE_MAP = {
    "burn": "BURN",
    "dodge": "DODGE",
    "overlay": "OVERLAY",
    "screen": "SCREEN",
    "plus": "ADD",
    "minus": "SUBTRACT",
    "difference": "DIFFERENCE",
}


def ensure_nodegroups() -> Dict[str, bpy.types.NodeTree]:
    """Create or update NodeGroups defined by the catalog."""
    groups: Dict[str, bpy.types.NodeTree] = {}
    for entry in metadata.get_node_catalog():
        group = _ensure_group(entry)
        if group:
            groups[entry["id"]] = group
    return groups


def get_nodegroup(node_id: str) -> Optional[bpy.types.NodeTree]:
    """Return the NodeGroup for a catalog entry."""
    entry = metadata.find_entry(node_id)
    if not entry:
        return None
    group_name = entry["group_name"]
    return bpy.data.node_groups.get(group_name)


def save_nodegroup_library(path: Path) -> None:
    """Save the catalog NodeGroups into a .blend library."""
    groups = set(ensure_nodegroups().values())
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.data.libraries.write(str(path), groups)


def _ensure_group(entry: Dict[str, object]) -> bpy.types.NodeTree:
    group_name = entry["group_name"]
    group = bpy.data.node_groups.get(group_name)

    if not group:
        group = bpy.data.node_groups.new(group_name, "ShaderNodeTree")

    needs_rebuild = _needs_rebuild(group, entry)
    _apply_metadata(group, entry)

    if needs_rebuild:
        _build_group(group, entry)

    return group


def _needs_rebuild(group: bpy.types.NodeTree, entry: Dict[str, object]) -> bool:
    current_version = group.get("rk_version")
    current_id = group.get("rk_id")

    if current_version and current_version != RK_NODE_VERSION:
        return True
    if current_id and current_id != entry["id"]:
        return True
    if group.nodes:
        return False
    return True


def _apply_metadata(group: bpy.types.NodeTree, entry: Dict[str, object]) -> None:
    group["rk_id"] = entry["id"]
    group["rk_node_id"] = entry.get("export_id")
    group["rk_version"] = RK_NODE_VERSION
    group["rk_label"] = entry.get("label")


def _build_group(group: bpy.types.NodeTree, entry: Dict[str, object]) -> None:
    io = entry.get("io", {})
    inputs = _io_to_socket_defs(io.get("inputs", []))
    outputs = _io_to_socket_defs(io.get("outputs", []))

    _ensure_group_inputs(group, inputs)
    _ensure_group_outputs(group, outputs)

    group.nodes.clear()

    node_id = entry.get("export_id") or entry.get("id")

    preview_helpers = PreviewHelpers(
        basic_io=_basic_io,
        output_kind=_output_kind,
        link_mix_inputs=_link_mix_inputs,
        connect_output=_connect_output,
        new_mix_node=_new_mix_node,
        build_float_mix_preview=_build_float_mix_preview,
        get_input_socket=_get_input_socket,
    )

    preview_builder = get_preview_builder(entry, node_id)
    if preview_builder:
        preview_builder(group, entry, preview_helpers)
        return

    if node_id in {"realitykit_occlusion", "realitykit_shadowreceiver"}:
        _build_simple_shader_preview(group, (0.2, 0.2, 0.2, 1.0))
        return
    if node_id == "realitykit_environment_radiance":
        _build_environment_radiance_preview(group)
        return
    if node_id == "realitykit_viewdirection":
        _build_view_direction_preview(group)
        return
    if node_id == "realitykit_cameraposition":
        _build_camera_position_preview(group)
        return
    if node_id == "normal_map_decode":
        _build_normal_map_preview(group)
        return
    if node_id in {"realitykit_reflect", "realitykit_refract"}:
        _build_vector_math_preview(group, entry, "REFLECT" if node_id.endswith("reflect") else "REFRACT")
        return

    if node_id in {"ramplr", "ramptb"}:
        _build_ramp_preview(group, entry, axis="x" if node_id == "ramplr" else "y")
        return
    if node_id in {"splitlr", "splittb"}:
        _build_split_preview(group, entry, axis="x" if node_id == "splitlr" else "y")
        return
    if node_id == "combine3":
        _build_combine3_preview(group)
        return
    if node_id == "swizzle":
        _build_swizzle_preview(group)
        return
    if node_id in {"constant", "convert"}:
        _build_value_passthrough_preview(group, input_name="value" if node_id == "constant" else "in")
        return
    if node_id == "remap":
        _build_map_range_preview(group)
        return
    if node_id == "clamp":
        _build_clamp_preview(group)
        return
    if node_id == "smoothstep":
        _build_smoothstep_preview(group)
        return

    if node_id in {"ifequal", "ifgreater", "ifgreatereq"}:
        _build_if_preview(group, entry, node_id)
        return
    if node_id in {"and", "or", "xor", "not"}:
        _build_logic_preview(group, entry, node_id)
        return

    if node_id in BLEND_MODE_MAP:
        _build_blend_preview(group, entry, BLEND_MODE_MAP[node_id])
        return
    if node_id in {"inside", "outside"}:
        _build_inside_outside_preview(group, entry, node_id == "inside")
        return
    if node_id == "switch":
        _build_switch_preview(group, entry)
        return

    if node_id in {"add", "subtract", "multiply", "divide"} and _output_kind(entry) != "float":
        vector_op = {
            "add": "ADD",
            "subtract": "SUBTRACT",
            "multiply": "MULTIPLY",
            "divide": "DIVIDE",
        }[node_id]
        _build_vector_math_preview(group, entry, vector_op)
        return

    if node_id in MATH_NODE_OPS:
        _build_math_preview(group, entry, MATH_NODE_OPS[node_id], node_id)
        return
    if node_id in VECTOR_NODE_OPS:
        _build_vector_math_preview(group, entry, VECTOR_NODE_OPS[node_id])
        return

    _build_passthrough_preview(group, outputs)


def _io_to_socket_defs(items: Iterable[Dict[str, object]]) -> List[Tuple[str, str, object]]:
    socket_defs: List[Tuple[str, str, object]] = []
    for item in items:
        socket_defs.append((item.get("name"), item.get("socket_type"), item.get("default")))
    return socket_defs


def _iter_interface_items(item_tree):
    for item in item_tree:
        yield item
        if hasattr(item, "items"):
            yield from _iter_interface_items(item.items)


def _get_interface_socket(node_tree, name: str, in_out: str):
    interface = getattr(node_tree, "interface", None)
    if not interface:
        return None
    item_tree = getattr(interface, "items_tree", None)
    if not item_tree:
        return None
    try:
        for item in _iter_interface_items(item_tree):
            if getattr(item, "item_type", None) != 'SOCKET':
                continue
            if getattr(item, "in_out", None) != in_out:
                continue
            if getattr(item, "name", None) == name:
                return item
    except TypeError:
        return None
    return None


def _ensure_interface_socket(node_tree, name: str, in_out: str, socket_type: str, default=None):
    interface = getattr(node_tree, "interface", None)
    if not interface:
        return None
    socket = _get_interface_socket(node_tree, name, in_out)
    if not socket:
        socket = interface.new_socket(
            name,
            description="",
            in_out=in_out,
            socket_type=socket_type,
        )
    if default is not None and hasattr(socket, "default_value"):
        socket.default_value = default
    return socket


def _ensure_group_inputs(node_tree, input_defs):
    for name, socket_type, default in input_defs:
        socket_type = _resolve_socket_type(socket_type)
        default = _coerce_default_for_socket(socket_type, default)
        if getattr(node_tree, "interface", None):
            _ensure_interface_socket(node_tree, name, "INPUT", socket_type, default)
        else:
            if name not in node_tree.inputs:
                socket = node_tree.inputs.new(socket_type, name)
            else:
                socket = node_tree.inputs[name]
            if default is not None and hasattr(socket, "default_value"):
                socket.default_value = default


def _ensure_group_outputs(node_tree, output_defs):
    for name, socket_type, _default in output_defs:
        socket_type = _resolve_socket_type(socket_type)
        if getattr(node_tree, "interface", None):
            _ensure_interface_socket(node_tree, name, "OUTPUT", socket_type)
        else:
            if name not in node_tree.outputs:
                node_tree.outputs.new(socket_type, name)


def _resolve_socket_type(socket_type: str) -> str:
    if not socket_type:
        return "NodeSocketFloat"
    if socket_type in INTERFACE_SOCKET_TYPES and hasattr(bpy.types, socket_type):
        return socket_type
    return "NodeSocketFloat"


def _coerce_default_for_socket(socket_type: str, default):
    if default is None:
        return None
    if socket_type == "NodeSocketFloat":
        if isinstance(default, (int, float, bool)):
            return float(default)
        if isinstance(default, (list, tuple)) and default:
            try:
                return float(default[0])
            except (TypeError, ValueError):
                return None
        if isinstance(default, str):
            try:
                return float(default)
            except ValueError:
                return None
        return None
    if socket_type == "NodeSocketInt":
        if isinstance(default, bool):
            return int(default)
        if isinstance(default, (int, float)):
            return int(default)
        if isinstance(default, (list, tuple)) and default:
            try:
                return int(default[0])
            except (TypeError, ValueError):
                return None
        if isinstance(default, str):
            try:
                return int(default)
            except ValueError:
                return None
        return None
    if socket_type == "NodeSocketBool":
        if isinstance(default, bool):
            return default
        if isinstance(default, (int, float)):
            return bool(default)
        if isinstance(default, str):
            return default.strip().lower() in {"true", "1", "yes"}
        return None
    if socket_type == "NodeSocketString":
        return str(default)
    if socket_type == "NodeSocketVector":
        if isinstance(default, (list, tuple)):
            if len(default) >= 3:
                return tuple(float(v) for v in default[:3])
            if len(default) == 2:
                return tuple(float(v) for v in default[:2]) + (0.0,)
        return None
    if socket_type == "NodeSocketColor":
        if isinstance(default, (list, tuple)):
            values = [float(v) for v in default]
            if len(values) == 3:
                values.append(1.0)
            if len(values) >= 4:
                return tuple(values[:4])
        return None
    return None
