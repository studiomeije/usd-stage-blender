"""
Preview builders for RealityKit node groups.
"""

from dataclasses import dataclass
from typing import Dict

import bpy


@dataclass(frozen=True)
class PreviewHelpers:
    basic_io: callable
    output_kind: callable
    link_mix_inputs: callable
    connect_output: callable
    new_mix_node: callable
    build_float_mix_preview: callable
    get_input_socket: callable


def _output_kind(entry: Dict[str, object]) -> str:
    io = entry.get("io", {})
    outputs = io.get("outputs", [])
    if not outputs:
        return "float"
    mtlx_type = outputs[0].get("mtlx_type") or ""
    mtlx_type = mtlx_type.lower()
    if mtlx_type in {"surfaceshader", "volumeshader", "displacementshader", "material"}:
        return "shader"
    if mtlx_type.startswith("color"):
        return "color"
    if mtlx_type in {"half4", "float4", "vector4"}:
        return "color"
    if mtlx_type.startswith("vector") or mtlx_type.endswith("2") or mtlx_type.endswith("3"):
        return "vector"
    if mtlx_type in {"integer", "int"}:
        return "int"
    if mtlx_type in {"boolean", "bool"}:
        return "bool"
    return "float"


def _basic_io(group: bpy.types.NodeTree):
    input_node = group.nodes.new("NodeGroupInput")
    output_node = group.nodes.new("NodeGroupOutput")
    input_node.location = (-300, 0)
    output_node.location = (300, 0)
    return input_node, output_node


def _get_input_socket(input_node, name: str):
    if not input_node:
        return None
    return input_node.outputs.get(name)


def _get_output_socket(output_node, name: str):
    if not output_node:
        return None
    return output_node.inputs.get(name) or (output_node.inputs[0] if output_node.inputs else None)


def _connect_output(group, output_node, output_name: str, source_socket):
    target_socket = _get_output_socket(output_node, output_name)
    if target_socket and source_socket:
        group.links.new(source_socket, target_socket)


def _build_math_preview(group: bpy.types.NodeTree, entry: Dict[str, object], operation: str, node_id: str):
    input_node, output_node = _basic_io(group)
    math_node = group.nodes.new("ShaderNodeMath")
    math_node.operation = operation
    math_node.location = (0, 0)

    if node_id == "oneminus":
        math_node.inputs[0].default_value = 1.0
        input_socket = _get_input_socket(input_node, "in")
        if input_socket:
            group.links.new(input_socket, math_node.inputs[1])
        _connect_output(group, output_node, "out", math_node.outputs[0])
        return

    input_map = {
        "in": 0,
        "in1": 0,
        "value1": 0,
        "fg": 0,
        "low": 1,
        "in2": 1,
        "value2": 1,
        "bg": 1,
        "high": 2,
        "edge": 1,
        "mix": 2,
    }
    for name, idx in input_map.items():
        socket = _get_input_socket(input_node, name)
        if socket:
            group.links.new(socket, math_node.inputs[idx])

    _connect_output(group, output_node, "out", math_node.outputs[0])


def _build_vector_math_preview(group: bpy.types.NodeTree, entry: Dict[str, object], operation: str):
    input_node, output_node = _basic_io(group)
    vec_node = group.nodes.new("ShaderNodeVectorMath")
    vec_node.operation = operation
    vec_node.location = (0, 0)

    for name, idx in [("in", 0), ("in1", 0), ("in2", 1), ("normal", 1)]:
        socket = _get_input_socket(input_node, name)
        if socket:
            group.links.new(socket, vec_node.inputs[idx])

    output_socket = vec_node.outputs.get("Vector") or vec_node.outputs.get("Value") or vec_node.outputs[0]
    _connect_output(group, output_node, "out", output_socket)


def _build_blend_preview(group: bpy.types.NodeTree, entry: Dict[str, object], blend_type: str):
    input_node, output_node = _basic_io(group)
    kind = _output_kind(entry)
    if kind == "float":
        _build_float_blend_preview(group, input_node, output_node, blend_type)
        return

    mix_node = group.nodes.new("ShaderNodeMixRGB")
    mix_node.blend_type = blend_type
    mix_node.location = (0, 0)
    _link_mix_inputs(group, input_node, mix_node, "mix", "fg", "bg")
    _connect_output(group, output_node, "out", mix_node.outputs.get("Color"))


def _build_if_preview(group: bpy.types.NodeTree, entry: Dict[str, object], node_id: str):
    input_node, output_node = _basic_io(group)

    compare_node = group.nodes.new("ShaderNodeMath")
    compare_node.operation = "COMPARE"
    compare_node.location = (-60, 0)
    _link_math_inputs(group, input_node, compare_node, None, "value1", "value2")

    if node_id in {"ifgreater", "ifgreatereq"}:
        compare_node.operation = "GREATER_THAN"

    mix_node = group.nodes.new("ShaderNodeMixRGB")
    mix_node.location = (120, 0)
    if compare_node.outputs:
        group.links.new(compare_node.outputs[0], mix_node.inputs.get("Fac"))

    fg = _get_input_socket(input_node, "in1")
    bg = _get_input_socket(input_node, "in2")
    if fg:
        group.links.new(fg, mix_node.inputs.get("Color2"))
    if bg:
        group.links.new(bg, mix_node.inputs.get("Color1"))

    _connect_output(group, output_node, "out", mix_node.outputs.get("Color"))


def _build_logic_preview(group: bpy.types.NodeTree, entry: Dict[str, object], node_id: str):
    input_node, output_node = _basic_io(group)
    if node_id == "not":
        math_node = group.nodes.new("ShaderNodeMath")
        math_node.operation = "SUBTRACT"
        math_node.inputs[0].default_value = 1.0
        input_socket = _get_input_socket(input_node, "in")
        if input_socket:
            group.links.new(input_socket, math_node.inputs[1])
        _connect_output(group, output_node, "out", math_node.outputs[0])
        return

    math_node = group.nodes.new("ShaderNodeMath")
    math_node.location = (0, 0)
    if node_id == "and":
        math_node.operation = "MULTIPLY"
    elif node_id == "or":
        math_node.operation = "MAXIMUM"
    elif node_id == "xor":
        add_node = group.nodes.new("ShaderNodeMath")
        add_node.operation = "ADD"
        add_node.location = (-120, 0)
        _link_math_inputs(group, input_node, add_node, None, "in1", "in2")

        mod_node = group.nodes.new("ShaderNodeMath")
        mod_node.operation = "MODULO"
        mod_node.location = (80, 0)
        mod_node.inputs[1].default_value = 2.0
        if add_node.outputs:
            group.links.new(add_node.outputs[0], mod_node.inputs[0])
        _connect_output(group, output_node, "out", mod_node.outputs[0])
        return

    _link_math_inputs(group, input_node, math_node, None, "in1", "in2")
    _connect_output(group, output_node, "out", math_node.outputs[0])


def _build_normal_map_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    normal_node = group.nodes.new("ShaderNodeNormalMap")
    normal_node.location = (0, 0)

    in_socket = _get_input_socket(input_node, "in")
    if in_socket:
        group.links.new(in_socket, normal_node.inputs.get("Color"))

    _connect_output(group, output_node, "out", normal_node.outputs.get("Normal"))


def _build_combine3_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    combine = group.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (0, 0)
    for name, idx in [("in1", 0), ("in2", 1), ("in3", 2)]:
        socket = _get_input_socket(input_node, name)
        if socket:
            group.links.new(socket, combine.inputs[idx])
    _connect_output(group, output_node, "out", combine.outputs.get("Vector"))


def _build_swizzle_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    separate = group.nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (0, 0)
    in_socket = _get_input_socket(input_node, "in")
    if in_socket:
        group.links.new(in_socket, separate.inputs.get("Vector"))
    _connect_output(group, output_node, "out", separate.outputs.get("X"))


def _build_ramp_preview(group: bpy.types.NodeTree, entry: Dict[str, object], axis: str):
    input_node, output_node = _basic_io(group)
    mix_node = group.nodes.new("ShaderNodeMixRGB")
    mix_node.location = (120, 0)

    texcoord = _get_input_socket(input_node, "texcoord")
    if texcoord:
        separate = group.nodes.new("ShaderNodeSeparateXYZ")
        separate.location = (-80, 0)
        group.links.new(texcoord, separate.inputs.get("Vector"))
        factor_out = separate.outputs.get("X" if axis == "x" else "Y")
        if factor_out:
            group.links.new(factor_out, mix_node.inputs.get("Fac"))

    left = _get_input_socket(input_node, "valuel") or _get_input_socket(input_node, "valueb")
    right = _get_input_socket(input_node, "valuer") or _get_input_socket(input_node, "valuet")
    if left:
        group.links.new(left, mix_node.inputs.get("Color1"))
    if right:
        group.links.new(right, mix_node.inputs.get("Color2"))

    _connect_output(group, output_node, "out", mix_node.outputs.get("Color"))


def _build_split_preview(group: bpy.types.NodeTree, entry: Dict[str, object], axis: str):
    input_node, output_node = _basic_io(group)
    compare = group.nodes.new("ShaderNodeMath")
    compare.operation = "GREATER_THAN"
    compare.location = (-40, 0)

    texcoord = _get_input_socket(input_node, "texcoord")
    if texcoord:
        separate = group.nodes.new("ShaderNodeSeparateXYZ")
        separate.location = (-200, 0)
        group.links.new(texcoord, separate.inputs.get("Vector"))
        axis_out = separate.outputs.get("X" if axis == "x" else "Y")
        if axis_out:
            group.links.new(axis_out, compare.inputs[0])

    center = _get_input_socket(input_node, "center")
    if center:
        group.links.new(center, compare.inputs[1])

    mix_node = group.nodes.new("ShaderNodeMixRGB")
    mix_node.location = (120, 0)
    if compare.outputs:
        group.links.new(compare.outputs[0], mix_node.inputs.get("Fac"))

    left = _get_input_socket(input_node, "valuel") or _get_input_socket(input_node, "valueb")
    right = _get_input_socket(input_node, "valuer") or _get_input_socket(input_node, "valuet")
    if left:
        group.links.new(left, mix_node.inputs.get("Color1"))
    if right:
        group.links.new(right, mix_node.inputs.get("Color2"))

    _connect_output(group, output_node, "out", mix_node.outputs.get("Color"))


def _build_inside_outside_preview(group: bpy.types.NodeTree, entry: Dict[str, object], inside: bool):
    input_node, output_node = _basic_io(group)
    math_node = group.nodes.new("ShaderNodeMath")
    math_node.operation = "MULTIPLY"
    math_node.location = (0, 0)

    in_socket = _get_input_socket(input_node, "in")
    mask_socket = _get_input_socket(input_node, "mask")
    if not inside and mask_socket:
        invert = group.nodes.new("ShaderNodeMath")
        invert.operation = "SUBTRACT"
        invert.inputs[0].default_value = 1.0
        invert.location = (-120, 0)
        group.links.new(mask_socket, invert.inputs[1])
        mask_socket = invert.outputs[0]

    if in_socket:
        group.links.new(in_socket, math_node.inputs[0])
    if mask_socket:
        group.links.new(mask_socket, math_node.inputs[1])

    _connect_output(group, output_node, "out", math_node.outputs[0])


def _build_switch_preview(group: bpy.types.NodeTree, entry: Dict[str, object]):
    input_node, output_node = _basic_io(group)
    _build_float_mix_preview(group, input_node, output_node, "which", "in1", "in2")


def _build_environment_radiance_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    base_color = _get_input_socket(input_node, "baseColor")
    if base_color:
        _connect_output(group, output_node, "diffuseRadiance", base_color)
        _connect_output(group, output_node, "specularRadiance", base_color)


def _build_view_direction_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    geometry = group.nodes.new("ShaderNodeNewGeometry")
    geometry.location = (0, 0)
    output_socket = geometry.outputs.get("Incoming")
    if output_socket:
        _connect_output(group, output_node, "out", output_socket)


def _build_camera_position_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    geometry = group.nodes.new("ShaderNodeNewGeometry")
    geometry.location = (0, 0)
    output_socket = geometry.outputs.get("Position")
    if output_socket:
        _connect_output(group, output_node, "out", output_socket)


def _build_simple_shader_preview(group: bpy.types.NodeTree, color):
    input_node, output_node = _basic_io(group)
    emission = group.nodes.new("ShaderNodeEmission")
    emission.location = (0, 0)
    emission.inputs.get("Color").default_value = color
    _connect_output(group, output_node, "out", emission.outputs.get("Emission"))


def _build_value_passthrough_preview(group: bpy.types.NodeTree, input_name: str):
    input_node, output_node = _basic_io(group)
    source_socket = _get_input_socket(input_node, input_name)
    _connect_output(group, output_node, "out", source_socket)


def _build_map_range_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    map_node = group.nodes.new("ShaderNodeMapRange")
    map_node.location = (0, 0)

    mapping = {
        "in": "Value",
        "inlow": "From Min",
        "inhigh": "From Max",
        "outlow": "To Min",
        "outhigh": "To Max",
    }
    for input_name, socket_name in mapping.items():
        in_socket = _get_input_socket(input_node, input_name)
        target = map_node.inputs.get(socket_name)
        if in_socket and target:
            group.links.new(in_socket, target)

    _connect_output(group, output_node, "out", map_node.outputs.get("Result") or map_node.outputs[0])


def _build_clamp_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    min_node = group.nodes.new("ShaderNodeMath")
    min_node.operation = "MINIMUM"
    min_node.location = (-60, 0)

    max_node = group.nodes.new("ShaderNodeMath")
    max_node.operation = "MAXIMUM"
    max_node.location = (80, 0)

    in_socket = _get_input_socket(input_node, "in")
    low_socket = _get_input_socket(input_node, "low")
    high_socket = _get_input_socket(input_node, "high")

    if in_socket:
        group.links.new(in_socket, min_node.inputs[0])
        group.links.new(in_socket, max_node.inputs[0])
    if high_socket:
        group.links.new(high_socket, min_node.inputs[1])
    if low_socket:
        group.links.new(low_socket, max_node.inputs[1])

    if min_node.outputs:
        group.links.new(min_node.outputs[0], max_node.inputs[0])
    _connect_output(group, output_node, "out", max_node.outputs[0])


def _build_smoothstep_preview(group: bpy.types.NodeTree):
    input_node, output_node = _basic_io(group)
    map_node = group.nodes.new("ShaderNodeMapRange")
    map_node.location = (0, 0)
    map_node.clamp = True

    if hasattr(map_node, "interpolation_type"):
        try:
            map_node.interpolation_type = 'SMOOTHSTEP'
        except Exception:
            pass

    mapping = {
        "in": "Value",
        "low": "From Min",
        "high": "From Max",
    }
    for input_name, socket_name in mapping.items():
        in_socket = _get_input_socket(input_node, input_name)
        target = map_node.inputs.get(socket_name)
        if in_socket and target:
            group.links.new(in_socket, target)

    _connect_output(group, output_node, "out", map_node.outputs.get("Result") or map_node.outputs[0])


def _build_float_blend_preview(group: bpy.types.NodeTree, input_node, output_node, blend_type: str):
    fg = _get_input_socket(input_node, "fg")
    bg = _get_input_socket(input_node, "bg")

    if blend_type == "ADD":
        math_node = group.nodes.new("ShaderNodeMath")
        math_node.operation = "ADD"
        math_node.location = (0, 0)
        if bg:
            group.links.new(bg, math_node.inputs[0])
        if fg:
            group.links.new(fg, math_node.inputs[1])
        _connect_output(group, output_node, "out", math_node.outputs[0])
        return

    if blend_type == "SUBTRACT":
        math_node = group.nodes.new("ShaderNodeMath")
        math_node.operation = "SUBTRACT"
        math_node.location = (0, 0)
        if bg:
            group.links.new(bg, math_node.inputs[0])
        if fg:
            group.links.new(fg, math_node.inputs[1])
        _connect_output(group, output_node, "out", math_node.outputs[0])
        return

    if blend_type == "DIFFERENCE":
        subtract = group.nodes.new("ShaderNodeMath")
        subtract.operation = "SUBTRACT"
        subtract.location = (-80, 0)
        if bg:
            group.links.new(bg, subtract.inputs[0])
        if fg:
            group.links.new(fg, subtract.inputs[1])

        absolute = group.nodes.new("ShaderNodeMath")
        absolute.operation = "ABSOLUTE"
        absolute.location = (80, 0)
        if subtract.outputs:
            group.links.new(subtract.outputs[0], absolute.inputs[0])
        _connect_output(group, output_node, "out", absolute.outputs[0])
        return

    if blend_type == "SCREEN":
        one_minus_fg = group.nodes.new("ShaderNodeMath")
        one_minus_fg.operation = "SUBTRACT"
        one_minus_fg.inputs[0].default_value = 1.0
        one_minus_fg.location = (-140, 80)
        if fg:
            group.links.new(fg, one_minus_fg.inputs[1])

        one_minus_bg = group.nodes.new("ShaderNodeMath")
        one_minus_bg.operation = "SUBTRACT"
        one_minus_bg.inputs[0].default_value = 1.0
        one_minus_bg.location = (-140, -80)
        if bg:
            group.links.new(bg, one_minus_bg.inputs[1])

        multiply = group.nodes.new("ShaderNodeMath")
        multiply.operation = "MULTIPLY"
        multiply.location = (40, 0)
        if one_minus_fg.outputs:
            group.links.new(one_minus_fg.outputs[0], multiply.inputs[0])
        if one_minus_bg.outputs:
            group.links.new(one_minus_bg.outputs[0], multiply.inputs[1])

        one_minus = group.nodes.new("ShaderNodeMath")
        one_minus.operation = "SUBTRACT"
        one_minus.inputs[0].default_value = 1.0
        one_minus.location = (180, 0)
        if multiply.outputs:
            group.links.new(multiply.outputs[0], one_minus.inputs[1])

        _connect_output(group, output_node, "out", one_minus.outputs[0])
        return

    # Fallback for complex blend modes.
    _build_float_mix_preview(group, input_node, output_node, "mix", "bg", "fg")


def _build_float_mix_preview(group: bpy.types.NodeTree, input_node, output_node, fac_name: str, bg_name: str, fg_name: str):
    fac = _get_input_socket(input_node, fac_name)
    bg = _get_input_socket(input_node, bg_name)
    fg = _get_input_socket(input_node, fg_name)

    mix_node = _new_mix_node(group, "float")
    if mix_node:
        mix_node.location = (0, 0)
        _link_mix_inputs(group, input_node, mix_node, fac_name, fg_name, bg_name)
        _connect_output(group, output_node, "out", mix_node.outputs[0])
        return

    subtract = group.nodes.new("ShaderNodeMath")
    subtract.operation = "SUBTRACT"
    subtract.location = (-120, 0)
    if fg:
        group.links.new(fg, subtract.inputs[0])
    if bg:
        group.links.new(bg, subtract.inputs[1])

    multiply = group.nodes.new("ShaderNodeMath")
    multiply.operation = "MULTIPLY"
    multiply.location = (40, 0)
    if subtract.outputs:
        group.links.new(subtract.outputs[0], multiply.inputs[0])
    if fac:
        group.links.new(fac, multiply.inputs[1])

    add = group.nodes.new("ShaderNodeMath")
    add.operation = "ADD"
    add.location = (180, 0)
    if bg:
        group.links.new(bg, add.inputs[0])
    if multiply.outputs:
        group.links.new(multiply.outputs[0], add.inputs[1])

    _connect_output(group, output_node, "out", add.outputs[0])


def _build_passthrough_preview(group: bpy.types.NodeTree, outputs) -> None:
    input_node, output_node = _basic_io(group)
    for name, _socket_type, _default in outputs:
        if name in input_node.outputs and name in output_node.inputs:
            group.links.new(input_node.outputs[name], output_node.inputs[name])


def _link_mix_inputs(group, input_node, mix_node, fac_name: str, fg_name: str, bg_name: str):
    fac_socket = _get_input_socket(input_node, fac_name)
    fg_socket = _get_input_socket(input_node, fg_name)
    bg_socket = _get_input_socket(input_node, bg_name)

    if fac_socket:
        mix_input = mix_node.inputs.get("Fac") or mix_node.inputs.get("Factor") or mix_node.inputs[0]
        group.links.new(fac_socket, mix_input)
    if bg_socket:
        color1 = mix_node.inputs.get("Color1") or mix_node.inputs.get("A") or mix_node.inputs[1]
        group.links.new(bg_socket, color1)
    if fg_socket:
        color2 = mix_node.inputs.get("Color2") or mix_node.inputs.get("B") or mix_node.inputs[2]
        group.links.new(fg_socket, color2)


def _link_math_inputs(group, input_node, math_node, fac_name: str, in1_name: str, in2_name: str):
    in1 = _get_input_socket(input_node, in1_name)
    in2 = _get_input_socket(input_node, in2_name)
    fac = _get_input_socket(input_node, fac_name) if fac_name else None
    if in1:
        group.links.new(in1, math_node.inputs[0])
    if in2:
        group.links.new(in2, math_node.inputs[1])
    if fac and len(math_node.inputs) > 2:
        group.links.new(fac, math_node.inputs[2])


def _new_mix_node(group: bpy.types.NodeTree, kind: str):
    # ShaderNodeMix has existed since Blender 3.4 and this add-on targets 5.2
    # LTS only, so there is no version to guard against.
    mix_node = group.nodes.new("ShaderNodeMix")
    if kind == "vector":
        mix_node.data_type = 'VECTOR'
    elif kind == "color":
        mix_node.data_type = 'RGBA'
    else:
        mix_node.data_type = 'FLOAT'
    return mix_node
