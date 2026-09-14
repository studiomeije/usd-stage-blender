"""Surface readers: Blender input nodes authored as MaterialX geometric readers.

Texture Coordinate, Geometry, Fresnel and Layer Weight used to be refused
outright (and the Geometry node's real type, ``NEW_GEOMETRY``, never matched
the validator's ``GEOMETRY`` entry at all). They now export through the
standard MaterialX readers the platform points at in its own deprecation
notices, and Fresnel / Layer Weight are transcribed from Cycles.

The arithmetic tests evaluate the authored expression trees numerically and
compare them with a Python port of the Cycles functions, so a wrong operand
order or a missing branch fails on values rather than on node names.
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Socket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name


class _Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _Node:
    pass


class _Inputs(dict):
    """Blender's ``node.inputs``: iterable over sockets, ``.get`` by name."""

    def __iter__(self):
        return iter(self.values())


def _reader_node(node_type: str, outputs, inputs=None, name=None):
    node = _Node()
    node.type = node_type
    node.name = name or node_type
    node.outputs = [_Socket(name=n) for n in outputs]
    node.inputs = _Inputs()
    for key, socket in (inputs or {}).items():
        socket.name = key
        node.inputs[key] = socket
    return node


def _link_output(node, output_name: str) -> _Socket:
    socket = next(s for s in node.outputs if s.name == output_name)
    socket.is_linked = True
    return _Socket(linked=True, link=_Link(node, socket))


def _resolve(target: _Socket, expected_type=None):
    return core._resolve_socket_value(target, expected_type=expected_type)


def _material(nodes, output_links):
    """A material whose Principled has the given input sockets linked."""
    principled = _Node()
    principled.type = "BSDF_PRINCIPLED"
    principled.name = "Principled BSDF"
    principled.inputs = _Inputs()
    for input_name, target in output_links.items():
        target.name = input_name
        principled.inputs[input_name] = target
    output = _Node()
    output.type = "OUTPUT_MATERIAL"
    output.name = "Material Output"
    output.is_active_output = True
    output.inputs = _Inputs(
        Surface=_Socket(linked=True, link=_Link(principled, SimpleNamespace(name="BSDF")), name="Surface")
    )
    return SimpleNamespace(
        name="Readers",
        node_tree=SimpleNamespace(nodes=[output, principled, *nodes], links=[]),
    )


# ---------------------------------------------------------------------------
# Numeric evaluation of authored expression trees
# ---------------------------------------------------------------------------

_MANIFEST = load_manifest()


def _evaluate(expr, env):
    """Evaluate an expression tree with reader values supplied by ``env``."""
    kind = expr.get("kind")
    if kind == "constant":
        return expr["value"]
    assert kind == "node", expr
    node_id = expr["node_id"]
    assert node_id in _MANIFEST["nodes"], f"authored a nodedef the manifest lacks: {node_id}"
    ins = {k: _evaluate(v, env) if isinstance(v, dict) else v for k, v in expr["inputs"].items()}
    op = node_id.split("_", 1)[1].rsplit("_", 1)[0]
    if node_id == "ND_realitykit_surface_view_direction":
        return env["view"]
    if node_id.startswith("ND_normalize_vector3"):
        v = ins["in"]; n = math.sqrt(sum(c * c for c in v)) or 1.0
        return tuple(c / n for c in v)
    if node_id == "ND_realitykit_is_front_facing":
        return env["front"]
    if node_id.startswith("ND_convert_boolean_float"):
        return 1.0 if ins["in"] else 0.0
    if node_id.startswith("ND_normal_vector3"):
        return env["normal"]
    if node_id.startswith("ND_dotproduct_vector3"):
        return sum(a * b for a, b in zip(ins["in1"], ins["in2"]))
    if node_id.startswith("ND_combine3_vector3"):
        return (ins["in1"], ins["in2"], ins["in3"])
    table = {
        "absval": lambda: abs(ins["in"]),
        "add": lambda: ins["in1"] + ins["in2"],
        "subtract": lambda: ins["in1"] - ins["in2"],
        "multiply": lambda: ins["in1"] * ins["in2"],
        # The GPU select evaluates both branches; a division in the branch the
        # ifgreater discards must not abort the evaluation here either.
        "divide": lambda: ins["in1"] / ins["in2"] if ins["in2"] != 0.0 else math.nan,
        "sqrt": lambda: math.sqrt(ins["in"]),
        "max": lambda: max(ins["in1"], ins["in2"]),
        "power": lambda: ins["in1"] ** ins["in2"],
        "mix": lambda: ins["mix"] * ins["fg"] + (1.0 - ins["mix"]) * ins["bg"],
        "ifgreater": lambda: ins["in1"] if ins["value1"] > ins["value2"] else ins["in2"],
    }
    assert op in table, node_id
    return table[op]()


def _cycles_fresnel_dielectric_cos(cosi: float, eta: float) -> float:
    c = abs(cosi)
    g = eta * eta - 1.0 + c * c
    if g > 0.0:
        g = math.sqrt(g)
        a = (g - c) / (g + c)
        b = (c * (g + c) - 1.0) / (c * (g - c) + 1.0)
        return 0.5 * a * a * (1.0 + b * b)
    return 1.0


def _cycles_fresnel_node(ior: float, cos_ni: float, front: bool) -> float:
    f = max(ior, 1e-5)
    eta = f if front else 1.0 / f
    return _cycles_fresnel_dielectric_cos(cos_ni, eta)


def _cycles_layer_weight(blend: float, cos_ni: float, front: bool):
    eta = max(1.0 - blend, 1e-5)
    eta = eta if not front else 1.0 / eta
    fresnel = _cycles_fresnel_dielectric_cos(cos_ni, eta)
    facing = abs(cos_ni)
    if blend != 0.5:
        blend = min(max(blend, 0.0), 1.0 - 1e-5)
        blend = 2.0 * blend if blend < 0.5 else 0.5 / (1.0 - blend)
        facing = facing ** blend
    return fresnel, 1.0 - facing


def _env(cos_ni: float, front: bool = True):
    # A unit normal along +Z and a view vector whose z is the cosine.
    s = math.sqrt(max(0.0, 1.0 - cos_ni * cos_ni))
    return {"normal": (0.0, 0.0, 1.0), "view": (s, 0.0, cos_ni), "front": front}


# ---------------------------------------------------------------------------
# Type lists
# ---------------------------------------------------------------------------


def test_reader_types_are_supported_and_the_dead_entries_are_gone():
    for t in ("TEX_COORD", "NEW_GEOMETRY", "FRESNEL", "LAYER_WEIGHT"):
        assert t in validate.SUPPORTED_TYPES
        assert t not in validate.UNSUPPORTED_TYPES
    # Blender 5.2 reports the Geometry node as NEW_GEOMETRY and Camera Data as
    # CAMERA; the old spellings matched nothing.
    assert "GEOMETRY" not in validate.UNSUPPORTED_TYPES
    assert "CAMERA_DATA" not in validate.UNSUPPORTED_TYPES
    assert "CAMERA" in validate.SUPPORTED_TYPES


# ---------------------------------------------------------------------------
# Texture Coordinate and Geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "nodedef", "inputs"),
    [
        ("UV", "ND_texcoord_vector3", {}),
        ("Object", "ND_position_vector3", {"space": "object"}),
        ("Generated", "ND_position_vector3", {"space": "object"}),
    ],
)
def test_texture_coordinate_outputs_author_standard_readers(output, nodedef, inputs):
    node = _reader_node("TEX_COORD", ["Generated", "Normal", "UV", "Object", "Camera", "Window", "Reflection"])
    expr = _resolve(_link_output(node, output))
    assert expr["kind"] == "node" and expr["node_id"] == nodedef
    assert {k: v["value"] for k, v in expr["inputs"].items()} == inputs


@pytest.mark.parametrize(
    ("output", "nodedef", "inputs"),
    [
        ("Position", "ND_position_vector3", {"space": "world"}),
        ("Normal", "ND_normal_vector3", {"space": "world"}),
    ],
)
def test_geometry_outputs_author_world_space_readers(output, nodedef, inputs):
    node = _reader_node("NEW_GEOMETRY", ["Position", "Normal", "Tangent", "True Normal", "Incoming", "Parametric", "Backfacing"])
    expr = _resolve(_link_output(node, output))
    # Turned back into Blender's axes: (x, -z, y) of the reader.
    assert expr["node_id"] == "ND_combine3_vector3"
    reader = expr["inputs"]["in1"]["inputs"]["in1"]
    assert reader["node_id"] == nodedef
    assert {k: v["value"] for k, v in reader["inputs"].items()} == inputs


def _cycles_geometry_tangent(generated, normal):
    """Cycles' primitive_tangent with a Generated attribute (object transform identity)."""
    data = (-(generated[1] - 0.5), generated[0] - 0.5, 0.0)

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])

    c = cross(data, normal)
    length = math.sqrt(sum(x * x for x in c))
    return cross(normal, tuple(x / length for x in c))


def test_geometry_tangent_is_refused_because_it_is_not_the_uv_tangent():
    """Blender's Geometry Tangent circles the object's Z axis; it is not the UV
    tangent RealityKit's reader returns. On an upward face at Generated
    (0.9, 0.5, 0) Cycles gives +Y, where a UV-aligned tangent is +X."""
    assert _cycles_geometry_tangent((0.9, 0.5, 0.0), (0.0, 0.0, 1.0)) == pytest.approx((0.0, 1.0, 0.0))
    node = _reader_node("NEW_GEOMETRY", ["Position", "Normal", "Tangent"])
    target = _link_output(node, "Tangent")
    resolved = _resolve(target)
    assert resolved["kind"] == "unresolved" and "Geometry 'Tangent'" in resolved["reason"]
    result = validate.validate_material(_material([node], {"Base Color": target}), strict=True)
    assert result["ok"] is False
    assert any("Geometry 'Tangent'" in e["message"] for e in result["errors"])


def test_texture_coordinate_object_with_a_reference_object_is_refused():
    """Cycles' NODE_TEXCO_OBJECT_WITH_TRANSFORM reads the position in the
    reference object's space, not the shaded mesh's own."""
    node = _reader_node("TEX_COORD", ["UV", "Object"])
    node.object = SimpleNamespace(name="Empty")
    target = _link_output(node, "Object")
    resolved = _resolve(target)
    assert resolved["kind"] == "unresolved" and "'Empty'" in resolved["reason"]
    result = validate.validate_material(_material([node], {"Base Color": target}), strict=True)
    assert any("Texture Coordinate 'Object'" in e["message"] for e in result["errors"])
    node.object = None
    assert _resolve(_link_output(node, "Object"))["kind"] == "node"


def test_world_readers_return_blenders_axes():
    """The export's root turns Blender's Z-up world into RealityKit's Y-up one,
    so a RealityKit world normal (x, y, z) is Blender's (x, -z, y). Measured by
    importing t26 and t36: unconverted, an upward normal read green."""
    node = _reader_node("NEW_GEOMETRY", ["Normal"])
    expr = _resolve(_link_output(node, "Normal"))
    realitykit_up = (0.0, 1.0, 0.0)
    assert _evaluate(expr, {"normal": realitykit_up}) == pytest.approx((0.0, 0.0, 1.0))
    realitykit_toward_viewer = (0.0, 0.0, 1.0)
    assert _evaluate(expr, {"normal": realitykit_toward_viewer}) == pytest.approx((0.0, -1.0, 0.0))


def test_incoming_uses_apples_view_direction_normalised():
    """Measured by import: the reader is not unit length, Blender's Incoming is."""
    node = _reader_node("NEW_GEOMETRY", ["Incoming"])
    expr = _resolve(_link_output(node, "Incoming"))
    assert expr["node_id"] == "ND_combine3_vector3"
    normalised = expr["inputs"]["in1"]["inputs"]["in1"]
    assert normalised["node_id"] == "ND_normalize_vector3"
    reader = normalised["inputs"]["in"]
    assert reader["node_id"] == "ND_realitykit_surface_view_direction"
    assert reader["output"] == "viewDirection"


@pytest.mark.parametrize("front", [True, False])
def test_backfacing_is_one_minus_the_front_facing_flag(front):
    node = _reader_node("NEW_GEOMETRY", ["Backfacing"])
    expr = _resolve(_link_output(node, "Backfacing"))
    assert _evaluate(expr, {"front": front}) == (0.0 if front else 1.0)


@pytest.mark.parametrize(
    ("node_type", "outputs", "output"),
    [
        ("TEX_COORD", ["Window", "Camera", "Reflection", "Normal"], "Window"),
        ("NEW_GEOMETRY", ["Parametric", "True Normal", "Pointiness"], "Parametric"),
    ],
)
def test_outputs_without_a_reader_resolve_unresolved_and_validate_as_errors(node_type, outputs, output):
    node = _reader_node(node_type, outputs)
    target = _link_output(node, output)
    assert _resolve(target)["kind"] == "unresolved"
    result = validate.validate_material(_material([node], {"Roughness": target}), strict=True)
    assert result["ok"] is False
    assert any(f"output '{output}' has no RealityKit reader" in e["message"] for e in result["errors"])


def test_generated_warns_about_the_missing_bounding_box_normalization():
    node = _reader_node("TEX_COORD", ["Generated"])
    target = _link_output(node, "Generated")
    result = validate.validate_material(_material([node], {"Roughness": target}), strict=True)
    assert result["ok"] is True
    assert any("bounding-box normalization" in w["message"] for w in result["warnings"])


# ---------------------------------------------------------------------------
# Fresnel and Layer Weight: values, not node names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ior", [1.0, 1.33, 1.45, 2.4])
@pytest.mark.parametrize("cos_ni", [1.0, 0.8, 0.5, 0.2, 0.05, -0.5])
@pytest.mark.parametrize("front", [True, False])
def test_fresnel_node_matches_cycles(ior, cos_ni, front):
    node = _reader_node("FRESNEL", ["Factor"], {"IOR": _Socket(ior), "Normal": _Socket()})
    expr = _resolve(_link_output(node, "Factor"))
    got = _evaluate(expr, _env(cos_ni, front))
    assert got == pytest.approx(_cycles_fresnel_node(ior, cos_ni, front), abs=1e-9)


def test_fresnel_ior_may_be_linked():
    ior_node = _reader_node("VALUE", ["Value"])
    ior_node.outputs = {"Value": SimpleNamespace(default_value=1.6, name="Value")}
    ior_socket = _Socket(linked=True, link=_Link(ior_node, ior_node.outputs["Value"]))
    node = _reader_node("FRESNEL", ["Factor"], {"IOR": ior_socket, "Normal": _Socket()})
    expr = _resolve(_link_output(node, "Factor"))
    got = _evaluate(expr, _env(0.6))
    assert got == pytest.approx(_cycles_fresnel_node(1.6, 0.6, True), abs=1e-9)


@pytest.mark.parametrize("blend", [0.0, 0.2, 0.5, 0.7, 1.0])
@pytest.mark.parametrize("cos_ni", [1.0, 0.7, 0.3, 0.0])
@pytest.mark.parametrize("front", [True, False])
def test_layer_weight_matches_cycles(blend, cos_ni, front):
    node = _reader_node("LAYER_WEIGHT", ["Fresnel", "Facing"], {"Blend": _Socket(blend), "Normal": _Socket()})
    expected_fresnel, expected_facing = _cycles_layer_weight(blend, cos_ni, front)
    fresnel = _evaluate(_resolve(_link_output(node, "Fresnel")), _env(cos_ni, front))
    facing = _evaluate(_resolve(_link_output(node, "Facing")), _env(cos_ni, front))
    assert fresnel == pytest.approx(expected_fresnel, abs=1e-9)
    assert facing == pytest.approx(expected_facing, abs=1e-9)


def test_total_internal_reflection_branch_never_roots_a_negative():
    """cos near 0 with eta < 1 makes g negative: Cycles returns 1.0 and the
    authored tree must too, through max(g, 0) under the sqrt."""
    node = _reader_node("FRESNEL", ["Factor"], {"IOR": _Socket(1.45), "Normal": _Socket()})
    expr = _resolve(_link_output(node, "Factor"))
    # back-facing flips eta to 1/1.45 < 1; a grazing cosine drives g below 0
    assert _evaluate(expr, _env(0.05, front=False)) == pytest.approx(1.0)


@pytest.mark.parametrize("node_type", ["FRESNEL", "LAYER_WEIGHT"])
def test_linked_normal_is_refused_with_bake_advice(node_type):
    normal_source = _reader_node("NORMAL_MAP", ["Normal"])
    normal_socket = _Socket(linked=True, link=_Link(normal_source, normal_source.outputs[0]))
    inputs = {"Normal": normal_socket, "IOR": _Socket(1.45)} if node_type == "FRESNEL" else {"Normal": normal_socket, "Blend": _Socket(0.5)}
    node = _reader_node(node_type, ["Factor", "Fresnel", "Facing"], inputs)
    target = _link_output(node, "Factor" if node_type == "FRESNEL" else "Fresnel")
    assert _resolve(target)["kind"] == "unresolved"
    result = validate.validate_material(_material([node, normal_source], {"Roughness": target}), strict=True)
    assert any("linked Normal requires baking" in e["message"] for e in result["errors"])


def test_linked_layer_weight_blend_is_refused():
    blend_source = _reader_node("VALUE", ["Value"])
    blend_socket = _Socket(linked=True, link=_Link(blend_source, blend_source.outputs[0]))
    node = _reader_node("LAYER_WEIGHT", ["Fresnel", "Facing"], {"Blend": blend_socket, "Normal": _Socket()})
    target = _link_output(node, "Facing")
    assert _resolve(target)["kind"] == "unresolved"
    result = validate.validate_material(_material([node, blend_source], {"Roughness": target}), strict=True)
    assert any("linked Blend requires baking" in e["message"] for e in result["errors"])


def test_every_authored_nodedef_is_in_the_manifest():
    """Walk every reader expression once and check each nodedef it names."""
    seen = set()

    def walk(expr):
        if not isinstance(expr, dict) or expr.get("kind") != "node":
            return
        seen.add(expr["node_id"])
        for value in expr["inputs"].values():
            walk(value)

    tex = _reader_node("TEX_COORD", ["UV", "Object", "Generated"])
    geo = _reader_node("NEW_GEOMETRY", ["Position", "Normal", "Tangent", "Incoming", "Backfacing"])
    for node in (tex, geo):
        for socket in list(node.outputs):
            walk(_resolve(_link_output(node, socket.name)))
    fresnel = _reader_node("FRESNEL", ["Factor"], {"IOR": _Socket(1.45), "Normal": _Socket()})
    walk(_resolve(_link_output(fresnel, "Factor")))
    layer = _reader_node("LAYER_WEIGHT", ["Fresnel", "Facing"], {"Blend": _Socket(0.3), "Normal": _Socket()})
    for name in ("Fresnel", "Facing"):
        walk(_resolve(_link_output(layer, name)))
    missing = sorted(n for n in seen if n not in _MANIFEST["nodes"])
    assert missing == []
    assert len(seen) >= 15


# ---------------------------------------------------------------------------
# Vector Math: the dot product the view-direction probe is built from
# ---------------------------------------------------------------------------


def test_vector_math_dot_product_of_two_readers():
    geo = _reader_node("NEW_GEOMETRY", ["Normal", "Incoming"])
    vmath = _Node()
    vmath.type = "VECT_MATH"
    vmath.name = "Vector Math"
    vmath.operation = "DOT_PRODUCT"
    vmath.inputs = [_link_output(geo, "Incoming"), _link_output(geo, "Normal"), _Socket((0, 0, 0)), _Socket(1.0)]
    vmath.outputs = [_Socket(name="Vector"), _Socket(name="Value")]
    expr = _resolve(_link_output(vmath, "Value"))
    assert expr["node_id"] == "ND_dotproduct_vector3"
    # Both readers are turned into Blender's axes; the dot product is unchanged.
    assert _evaluate(expr, _env(0.25)) == pytest.approx(0.25)


def test_vector_math_other_operations_still_refuse():
    vmath = _Node()
    vmath.type = "VECT_MATH"
    vmath.name = "Vector Math"
    vmath.operation = "MODULO"
    vmath.inputs = [_Socket((1, 0, 0)), _Socket((0, 1, 0)), _Socket((0, 0, 0)), _Socket(1.0)]
    vmath.outputs = [_Socket(name="Vector"), _Socket(name="Value")]
    target = _link_output(vmath, "Vector")
    assert _resolve(target)["kind"] == "unresolved"
    assert "VECT_MATH" in validate.BAKE_TYPES and "VECTOR_MATH" not in validate.BAKE_TYPES
    result = validate.validate_material(_material([vmath], {"Roughness": target}), strict=True)
    assert any("Vector Math 'MODULO' requires baking; Blender's truncated modulo" in e["message"] for e in result["errors"])


# ---------------------------------------------------------------------------
# The Fresnel rim pattern: two constant colours mixed by a wired factor
# ---------------------------------------------------------------------------


def test_plain_mix_of_constants_with_a_fresnel_factor_is_authored():
    fresnel = _reader_node("FRESNEL", ["Factor"], {"IOR": _Socket(1.45), "Normal": _Socket()})
    mix = _Node()
    mix.type = "MIX"
    mix.name = "Mix"
    mix.data_type = "RGBA"
    mix.blend_type = "MIX"
    mix.clamp_factor = False
    mix.inputs = _Inputs()
    for key, socket in {
        "Factor": _link_output(fresnel, "Factor"),
        "A": _Socket((0.03, 0.03, 0.03, 1.0)),
        "B": _Socket((1.0, 1.0, 1.0, 1.0)),
    }.items():
        socket.name = key
        mix.inputs[key] = socket
    mix.outputs = [_Socket(name="Result")]
    assert core._is_supported_mix(mix)
    expr = _resolve(_link_output(mix, "Result"), expected_type="color3")
    assert expr["node_id"] == "ND_mix_color3"
    assert expr["inputs"]["bg"]["kind"] == "constant" and expr["inputs"]["fg"]["kind"] == "constant"
    assert expr["inputs"]["mix"]["node_id"] == "ND_ifgreater_float"


# ---------------------------------------------------------------------------
# Separate XYZ over a reader authors a real component read
# ---------------------------------------------------------------------------


def test_separate_xyz_of_a_texcoord_reader_extracts_the_component():
    """The UV cube in t25 rendered black: the channel hint on a reader
    expression folded to the socket constant. A component read is explicit."""
    coords = _reader_node("TEX_COORD", ["UV"])
    separate = _Node()
    separate.type = "SEPXYZ"
    separate.name = "Separate XYZ"
    separate.inputs = _Inputs(Vector=_link_output(coords, "UV"))
    separate.inputs["Vector"].name = "Vector"
    separate.outputs = [_Socket(name="X"), _Socket(name="Y"), _Socket(name="Z")]
    expr = _resolve(_link_output(separate, "Y"), expected_type="float")
    assert expr["kind"] == "node" and expr["node_id"] == "ND_dotproduct_vector3"
    assert expr["inputs"]["in1"]["node_id"] == "ND_texcoord_vector3"
    assert tuple(expr["inputs"]["in2"]["value"]) == (0.0, 1.0, 0.0)


def test_combine_color_reads_blender_52_socket_names():
    """Combine Color's sockets are Red/Green/Blue; reading R/G/B folded every
    Combine Color to black (measured on the t25 UV cube)."""
    coords = _reader_node("TEX_COORD", ["UV"])
    separate = _Node()
    separate.type = "SEPXYZ"
    separate.name = "Separate XYZ"
    separate.inputs = _Inputs(Vector=_link_output(coords, "UV"))
    separate.inputs["Vector"].name = "Vector"
    separate.outputs = [_Socket(name="X"), _Socket(name="Y"), _Socket(name="Z")]
    combine = _Node()
    combine.type = "COMBINE_COLOR"
    combine.name = "Combine Color"
    combine.mode = "RGB"
    combine.inputs = _Inputs(Red=_link_output(separate, "X"), Green=_link_output(separate, "Y"), Blue=_Socket(0.0))
    for k, sock in combine.inputs.items(): sock.name = k
    combine.outputs = [_Socket(name="Color")]
    expr = _resolve(_link_output(combine, "Color"), expected_type="color3")
    assert expr["node_id"] == "ND_combine3_color3"
    assert expr["inputs"]["in1"]["node_id"] == "ND_dotproduct_vector3"
    assert expr["inputs"]["in2"]["node_id"] == "ND_dotproduct_vector3"
    assert expr["inputs"]["in3"] == {"kind": "constant", "value": 0.0}


@pytest.mark.parametrize("interpolation, ok", [("LINEAR", True), ("STEPPED", False), ("SMOOTHSTEP", False), ("SMOOTHERSTEP", False)])
def test_map_range_exports_only_linear_interpolation(interpolation, ok):
    """A Stepped or Smooth Map Range used to export as linear with no warning."""
    node = _Node()
    node.type = "MAP_RANGE"
    node.name = "Map Range"
    node.interpolation_type = interpolation
    node.data_type = "FLOAT"
    node.inputs = _Inputs(**{k: _Socket(v, name=k) for k, v in {"Value": 0.5, "From Min": 0.0, "From Max": 1.0, "To Min": 0.0, "To Max": 2.0}.items()})
    node.outputs = [_Socket(name="Result")]
    target = _link_output(node, "Result")
    result = validate.validate_material(_material([node], {"Roughness": target}), strict=True)
    assert result["ok"] is ok
    if not ok:
        assert any(f"Map Range interpolation '{interpolation}'" in e["message"] for e in result["errors"])
