"""Unit tests for the Blender MATH node -> MaterialX float translation.

The Math node used to pass only as an identity (add 0, subtract 0,
multiply 1, divide 1); everything else was refused with bake advice. These
tests pin the real mapping: every supported operation authors the exact
manifest-backed nodedef, composed operations (MULTIPLY_ADD, LOGARITHM,
ARCTANGENT) stay exact compositions, use_clamp wraps the result in a clamp,
and operations with no exact MaterialX equivalent still refuse loudly -
including MODULO, whose truncated-fmod semantics differ from MaterialX's
floored modulo for negative inputs.
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


def _value_node(value: float):
    node = _Node()
    node.type = "VALUE"
    node.name = "Value"
    node.outputs = {"Value": SimpleNamespace(default_value=value, name="Value")}
    return node


def _linked_value_socket(value: float) -> _Socket:
    node = _value_node(value)
    return _Socket(linked=True, link=_Link(node, node.outputs["Value"]))


def _math_node(operation, inputs, *, use_clamp=False, name="Math"):
    node = _Node()
    node.type = "MATH"
    node.name = name
    node.operation = operation
    node.use_clamp = use_clamp
    node.inputs = list(inputs)
    return node


def _resolve(math_node, expected_type="float"):
    output = _Socket(name="Value")
    target = _Socket(linked=True, link=_Link(math_node, output))
    return core._resolve_socket_value(target, expected_type=expected_type)


_MANIFEST_NODES = frozenset(load_manifest()["nodes"].keys())


def _walk_expr(expr):
    if not isinstance(expr, dict):
        return
    yield expr
    for child in (expr.get("inputs") or {}).values():
        yield from _walk_expr(child)


# --- op -> nodedef mapping -------------------------------------------------

_TWO_INPUT_EXPECTED = {
    "ADD": "ND_add_float",
    "SUBTRACT": "ND_subtract_float",
    "MULTIPLY": "ND_multiply_float",
    "MINIMUM": "ND_min_float",
    "MAXIMUM": "ND_max_float",
    # DIVIDE, POWER, FLOORED_MODULO and ARCTAN2 are not here: Cycles guards
    # them where RealityKit's bare functions return inf or NaN, so they are
    # compositions, checked on values in test_math_safe_guards.py.
}

_SINGLE_INPUT_EXPECTED = {
    "ABSOLUTE": "ND_absval_float",
    "EXPONENT": "ND_exp_float",
    "FLOOR": "ND_floor_float",
    "CEIL": "ND_ceil_float",
    "SINE": "ND_sin_float",
    "COSINE": "ND_cos_float",
    "TANGENT": "ND_tan_float",
    # SQRT, ARCSINE, ARCCOSINE and ROUND are compositions too.
}


@pytest.mark.parametrize(("operation", "nodedef"), sorted(_TWO_INPUT_EXPECTED.items()))
def test_two_input_operation_authors_manifest_nodedef(operation, nodedef):
    # 0.8 on the second socket is not an identity for any of these ops.
    node = _math_node(operation, [_linked_value_socket(0.35), _Socket(0.8)])
    expr = _resolve(node)
    assert expr["kind"] == "node"
    assert expr["node_id"] == nodedef
    assert nodedef in _MANIFEST_NODES
    assert set(expr["inputs"]) == {"in1", "in2"}
    assert expr["inputs"]["in1"] == {"kind": "constant", "value": 0.35}
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": 0.8}


@pytest.mark.parametrize(("operation", "nodedef"), sorted(_SINGLE_INPUT_EXPECTED.items()))
def test_single_input_operation_authors_manifest_nodedef(operation, nodedef):
    node = _math_node(operation, [_linked_value_socket(0.35), _Socket(0.5)])
    expr = _resolve(node)
    assert expr["kind"] == "node"
    assert expr["node_id"] == nodedef
    assert nodedef in _MANIFEST_NODES
    assert set(expr["inputs"]) == {"in"}
    assert expr["inputs"]["in"] == {"kind": "constant", "value": 0.35}


def test_roughness_times_constant_is_a_real_multiply():
    # The motivating case: roughness x 0.8 must export, not demand a bake.
    node = _math_node("MULTIPLY", [_linked_value_socket(0.5), _Socket(0.8)])
    expr = _resolve(node)
    assert expr["node_id"] == "ND_multiply_float"


def test_atan2_input_names_match_the_nodedef_realitykit_binds():
    """atan2 takes iny/inx, not in1/in2, in both MaterialX versions RealityKit
    ships. Getting this wrong is not cosmetic: measured with `realitytool
    compile`, an input the bound nodedef does not declare makes the compiler
    discard the material's entire shader graph and substitute default PBR,
    silently. `in1`/`in2` produced a byte-identical result to invented names.

    This is deliberately asserted against literals rather than against the
    manifest. The manifest is generated from MaterialX definition files and
    once carried the wrong spelling, so a test that reads it back can only ever
    confirm that we agree with ourselves.
    """
    node = _math_node("ARCTAN2", [_linked_value_socket(0.35), _Socket(0.25)])
    expr = _resolve(node)
    assert expr["node_id"] == "ND_atan2_float"

    # Blender's first socket is the y term, second the x term.
    assert expr["inputs"] == {
        "iny": {"kind": "constant", "value": 0.35},
        "inx": {"kind": "constant", "value": 0.25},
    }

    # The manifest must have been corrected to match, or authoring hard-errors.
    declared = {
        entry["name"]
        for entry in load_manifest()["nodes"]["ND_atan2_float"].get("inputs", [])
    }
    assert declared == {"iny", "inx"}


def test_color_constant_input_folds_to_blender_luminance():
    """Blender implicitly converts a colour feeding a scalar socket with
    linear RGB to gray; the fold must use the same Rec.709 coefficients."""
    rgb = _Node()
    rgb.type = "RGB"
    rgb.name = "RGB"
    rgb.outputs = {
        "Color": SimpleNamespace(default_value=(1.0, 0.0, 0.0, 1.0), name="Color")
    }
    color_socket = _Socket(linked=True, link=_Link(rgb, rgb.outputs["Color"]))
    node = _math_node("MULTIPLY", [color_socket, _Socket(0.8)])
    expr = _resolve(node)
    assert expr["node_id"] == "ND_multiply_float"
    assert expr["inputs"]["in1"]["kind"] == "constant"
    # Cycles' linear_rgb_to_gray of pure red, measured by rendering RGB to BW
    # in Blender 5.2's Cycles with the Linear Rec.709 working space.
    assert expr["inputs"]["in1"]["value"] == pytest.approx(0.21263909, abs=1e-7)


def test_channelless_texture_input_gets_exact_luminance_chain():
    """A colour texture feeding a float Math input must author Blender's
    implicit conversion explicitly: a dot product with Cycles' gray weights.
    ND_luminance is not used: its default lumacoeffs are ACEScg's, so red
    reads 0.2722 where Blender reads 0.2126."""
    texture = {"kind": "texture", "path": "/tmp/t.png", "output_type": "float"}
    expr = core._float_math_input_expr(texture)
    assert expr["node_id"] == "ND_dotproduct_vector3"
    assert expr["inputs"]["in2"]["value"] == pytest.approx((0.21263909, 0.71516913, 0.07219274), abs=1e-7)
    convert = expr["inputs"]["in1"]
    assert convert["node_id"] == "ND_convert_color3_vector3"
    assert convert["inputs"]["in"]["output_type"] == "color3"


def test_channel_bearing_texture_input_passes_through():
    texture = {"kind": "texture", "path": "/tmp/t.png", "channel": "g"}
    assert core._float_math_input_expr(texture) is texture


# --- composed operations ---------------------------------------------------

def test_multiply_add_composes_multiply_then_add():
    node = _math_node(
        "MULTIPLY_ADD",
        [_linked_value_socket(0.5), _Socket(2.0), _Socket(0.25)],
    )
    expr = _resolve(node)
    assert expr["node_id"] == "ND_add_float"
    product = expr["inputs"]["in1"]
    assert product["node_id"] == "ND_multiply_float"
    assert product["inputs"]["in1"] == {"kind": "constant", "value": 0.5}
    assert product["inputs"]["in2"] == {"kind": "constant", "value": 2.0}
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": 0.25}


def test_arctangent_composes_atan2_with_unit_x():
    node = _math_node("ARCTANGENT", [_linked_value_socket(0.35), _Socket(0.5)])
    expr = _resolve(node)
    assert expr["node_id"] == "ND_atan2_float"
    assert expr["inputs"]["iny"] == {"kind": "constant", "value": 0.35}
    assert expr["inputs"]["inx"] == {"kind": "constant", "value": 1.0}


def test_logarithm_with_base_e_is_a_single_guarded_ln():
    import math

    node = _math_node("LOGARITHM", [_linked_value_socket(0.35), _Socket(math.e)])
    expr = _resolve(node)
    # safe_logf: 0 unless the value is positive.
    assert expr["node_id"] == "ND_ifgreater_float"
    assert expr["inputs"]["in1"]["node_id"] == "ND_ln_float"
    assert expr["inputs"]["in1"]["inputs"]["in"] == {"kind": "constant", "value": 0.35}


def test_logarithm_with_other_base_divides_two_lns():
    node = _math_node("LOGARITHM", [_linked_value_socket(0.35), _Socket(10.0)])
    expr = _resolve(node)["inputs"]["in1"]
    assert expr["node_id"] == "ND_divide_float"
    assert expr["inputs"]["in1"]["node_id"] == "ND_ln_float"
    assert expr["inputs"]["in2"]["node_id"] == "ND_ln_float"
    assert expr["inputs"]["in2"]["inputs"]["in"] == {"kind": "constant", "value": 10.0}


# --- use_clamp -------------------------------------------------------------

def test_use_clamp_wraps_the_result_in_a_clamp():
    node = _math_node(
        "MULTIPLY", [_linked_value_socket(0.5), _Socket(0.8)], use_clamp=True
    )
    expr = _resolve(node)
    assert expr["node_id"] == "ND_clamp_float"
    assert expr["inputs"]["low"] == {"kind": "constant", "value": 0.0}
    assert expr["inputs"]["high"] == {"kind": "constant", "value": 1.0}
    assert expr["inputs"]["in"]["node_id"] == "ND_multiply_float"


def test_identity_passthrough_still_collapses_without_a_node():
    node = _math_node("MULTIPLY", [_linked_value_socket(0.5), _Socket(1.0)])
    expr = _resolve(node)
    assert expr == {"kind": "constant", "value": 0.5}


def test_clamped_identity_is_not_collapsed():
    """The old identity shortcut dropped use_clamp silently; a clamped
    multiply-by-1 must still author the clamp."""
    node = _math_node(
        "MULTIPLY", [_linked_value_socket(2.5), _Socket(1.0)], use_clamp=True
    )
    expr = _resolve(node)
    assert expr["node_id"] == "ND_clamp_float"


# --- refusals --------------------------------------------------------------

@pytest.mark.parametrize("operation", ["SMOOTH_MIN", "PINGPONG", "WRAP", "MODULO", "COMPARE"])
def test_unsupported_operation_resolves_unresolved(operation):
    node = _math_node(operation, [_linked_value_socket(0.5), _Socket(0.8)])
    expr = _resolve(node)
    assert expr["kind"] == "unresolved"


def test_modulo_is_refused_for_truncated_vs_floored_semantics():
    """Blender's MODULO is truncated fmod (sign follows the dividend);
    MaterialX modulo is floored (sign follows in2). They disagree for
    negative inputs, so MODULO must refuse and name the exact alternative."""
    assert "MODULO" not in validate.SUPPORTED_MATH_OPERATIONS
    assert "FLOORED_MODULO" in validate.SUPPORTED_MATH_OPERATIONS
    message = validate.math_refusal_message("MODULO")
    assert "requires baking" in message
    assert "Floored Modulo" in message


def test_refusal_message_names_the_operation():
    message = validate.math_refusal_message("SMOOTH_MIN")
    assert "SMOOTH_MIN" in message
    assert "requires baking" in message


# --- validator agreement ---------------------------------------------------

def _material_with(node):
    return SimpleNamespace(
        name="MathMaterial",
        node_tree=SimpleNamespace(nodes=[node], links=()),
    )


def _math_warnings(result):
    return [
        issue["message"]
        for issue in result["warnings"] + result["errors"]
        if "Math operation" in issue["message"]
    ]


def test_validator_accepts_a_supported_math_operation():
    node = _math_node("MULTIPLY", [_linked_value_socket(0.5), _Socket(0.8)])
    node.mute = False
    result = validate.validate_material(_material_with(node), only_connected=False)
    assert _math_warnings(result) == []


def test_validator_refuses_an_unsupported_math_operation():
    node = _math_node("SMOOTH_MIN", [_linked_value_socket(0.5), _Socket(0.8)])
    node.mute = False
    result = validate.validate_material(_material_with(node), only_connected=False)
    messages = _math_warnings(result)
    assert messages, "SMOOTH_MIN must be refused"
    assert any("SMOOTH_MIN" in message for message in messages)


def test_every_mapped_operation_resolves_to_a_manifest_nodedef():
    """No fabricated ids: every table entry must select a real nodedef."""
    for node_name in sorted(
        set(core._MATH_SINGLE_INPUT_OPS.values())
        | set(core._MATH_TWO_INPUT_OPS.values())
        | {"add", "multiply", "divide", "ln", "atan2", "clamp"}
    ):
        nodedef = core._nodedef_for(node_name, "float")
        assert nodedef in _MANIFEST_NODES, nodedef


def test_fract_is_value_minus_floor_because_fract_float_has_no_implementation():
    """``ND_fract_float`` resolves in RealityKit's 1.39 store but has no Metal
    symbol and no nodegraph expansion (scripts/check_shader_implementations.py
    reports it), so Fract is authored as ``x - floor(x)``, Blender's own
    definition."""
    node = _math_node("FRACT", [_linked_value_socket(-1.25), _Socket(0.5)])
    expr = _resolve(node)
    assert expr["node_id"] == "ND_subtract_float"
    assert expr["inputs"]["in2"]["node_id"] == "ND_floor_float"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mx_eval

    assert mx_eval.evaluate(expr) == -1.25 - math.floor(-1.25)
