"""Colour, Mix, Clamp and Map Range nodes, checked on values against Cycles.

Each test builds a small node graph out of mx_eval's fakes, resolves it
through the exporter and evaluates the expression with RealityKit's
arithmetic. The expected values come from Python ports of Cycles' kernel
functions written here (intern/cycles/kernel/svm/hsv.h, mix.h, color_util.h,
map_range.h, clamp.h, invert.h, sepcomb_color.h and intern/cycles/util/color.h),
or from Cycles measured: the implicit colour-to-float conversion is
``linear_rgb_to_gray``, and rendering RGB to BW of pure red, green and blue as
emission in Blender 5.2's Cycles gives 0.21263909, 0.71516913 and 0.07219274.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402
from mx_eval import MANIFEST_NODES, Node, Socket, core, evaluate, float_value, resolve, vector_value  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

#: Cycles' linear_rgb_to_gray weights, measured (see the module docstring).
CYCLES_GRAY = (0.21263909, 0.71516913, 0.07219274)

#: RealityKit's colour nodes compute in half precision, and its rgbtohsv
#: multiplies by 1/360 as a half literal; a round trip through HSV is exact to
#: about this much.
HALF = 2e-3


def gray(color):
    return sum(c * w for c, w in zip(color, CYCLES_GRAY))


# --- Cycles, ported ----------------------------------------------------------

def rgb_to_hsv(rgb):
    """``rgb_to_hsv`` (intern/cycles/util/color.h)."""
    cmax = max(rgb)
    cmin = min(rgb)
    cdelta = cmax - cmin
    v = cmax
    if cmax != 0.0:
        s = cdelta / cmax
    else:
        s = 0.0
        h = 0.0
    if s != 0.0:
        c = tuple((cmax - x) / cdelta for x in rgb)
        if rgb[0] == cmax:
            h = c[2] - c[1]
        elif rgb[1] == cmax:
            h = 2.0 + c[0] - c[2]
        else:
            h = 4.0 + c[1] - c[0]
        h /= 6.0
        if h < 0.0:
            h += 1.0
    else:
        h = 0.0
    return (h, s, v)


def hsv_to_rgb(hsv):
    """``hsv_to_rgb`` (intern/cycles/util/color.h)."""
    h, s, v = hsv
    if s == 0.0:
        return (v, v, v)
    if h == 1.0:
        h = 0.0
    h *= 6.0
    i = math.floor(h)
    f = h - i
    p = v * (1.0 - s)
    q = v * (1.0 - (s * f))
    t = v * (1.0 - (s * (1.0 - f)))
    return {0: (v, t, p), 1: (q, v, p), 2: (p, v, t), 3: (p, q, v), 4: (t, p, v)}.get(i, (v, p, q))


def svm_node_hsv(color, hue, sat, val, fac):
    """``svm_node_hsv`` (intern/cycles/kernel/svm/hsv.h)."""
    h, s, v = rgb_to_hsv(color)
    x = h + hue + 0.5
    h = x - math.floor(x)
    s = min(max(s * sat, 0.0), 1.0)
    v *= val
    rgb = hsv_to_rgb((h, s, v))
    mixed = tuple(fac * c + (1.0 - fac) * i for c, i in zip(rgb, color))
    return tuple(max(c, 0.0) for c in mixed)


def saturate(x):
    return min(max(x, 0.0), 1.0)


def svm_mix(blend, t, c1, c2):
    """The blends the exporter authors, from ``svm_mix`` (color_util.h)."""
    def interp(a, b):
        return tuple(x + t * (y - x) for x, y in zip(a, b))
    if blend == "MIX":
        return interp(c1, c2)
    if blend == "ADD":
        return interp(c1, tuple(x + y for x, y in zip(c1, c2)))
    if blend == "MULTIPLY":
        return interp(c1, tuple(x * y for x, y in zip(c1, c2)))
    if blend == "SUBTRACT":
        return interp(c1, tuple(x - y for x, y in zip(c1, c2)))
    raise AssertionError(blend)


def svm_node_map_range_linear(value, from_min, from_max, to_min, to_max, clamp):
    """``svm_node_map_range`` Linear, then ``MapRangeNode::expand``'s Range clamp."""
    if from_max != from_min:
        factor = (value - from_min) / (from_max - from_min)
        result = to_min + factor * (to_max - to_min)
    else:
        result = 0.0
    if clamp:
        result = svm_node_clamp(result, to_min, to_max, "RANGE")
    return result


def svm_node_clamp(value, low, high, clamp_type):
    """``svm_node_clamp``: ``clamp(a, mn, mx)`` is ``min(max(a, mn), mx)``."""
    if clamp_type == "RANGE" and low > high:
        return min(max(value, high), low)
    return min(max(value, low), high)


# --- helpers -----------------------------------------------------------------

def rgb(color):
    node = Node("RGB", "RGB", {}, outputs=("Color",))
    node.outputs[0].default_value = tuple(color) + (1.0,)
    return node.out()


def live_color(color):
    """A colour that resolves to a node expression, not a constant."""
    node = Node(
        "MIX", "Mix", {"Factor": float_value(0.0), "A": rgb(color), "B": (0.0, 0.0, 0.0, 1.0)},
        outputs=("Result",), data_type="RGBA", blend_type="MIX", clamp_factor=True,
        clamp_result=False, factor_mode="UNIFORM",
    )
    return node.out()


def assert_constants_match_nodedefs(expr):
    """Every constant input has the shape its nodedef declares.

    A float constant on a color3 input is what failed the whole export with
    'expected GfVec3f, got double'.
    """
    nodes = load_manifest()["nodes"]
    sizes = {"float": 1, "integer": 1, "boolean": 1, "vector2": 2, "vector3": 3, "color3": 3, "vector4": 4, "color4": 4}
    if not isinstance(expr, dict) or expr.get("kind") != "node":
        return
    assert expr["node_id"] in MANIFEST_NODES
    declared = {entry["name"]: entry["type"] for entry in nodes[expr["node_id"]].get("inputs", [])}
    for name, child in expr["inputs"].items():
        if isinstance(child, dict) and child.get("kind") == "constant" and declared.get(name) in sizes:
            value = child["value"]
            size = len(value) if isinstance(value, (list, tuple)) else 1
            assert size == sizes[declared[name]], (expr["node_id"], name, value)
        assert_constants_match_nodedefs(child)


# --- 1. Hue/Saturation/Value ------------------------------------------------------

@pytest.mark.parametrize(("color", "hue", "sat", "val", "fac"), [
    ((0.8, 0.2, 0.1), 0.5, 1.0, 1.0, 1.0),     # the defaults leave the colour alone
    ((0.8, 0.2, 0.1), 0.25, 1.0, 1.0, 1.0),    # a quarter turn back
    ((0.8, 0.2, 0.4), 0.9, 1.0, 1.0, 1.0),     # red-dominant: Cycles wraps its hue
    ((0.2, 0.7, 0.3), 0.5, 1.8, 0.6, 1.0),     # saturation clamps at 1
    ((0.1, 0.3, 0.9), 0.1, 0.5, 1.2, 0.35),    # Fac blends with the input
    ((0.6, 0.6, 0.6), 0.3, 2.0, 1.0, 1.0),     # grey has no hue to turn
    ((0.8, 0.2, 0.1), 0.5, 1.0, -1.0, 1.0),    # a negative value floors at 0
])
def test_hue_saturation_matches_cycles(color, hue, sat, val, fac):
    node = Node("HUE_SAT", "Hue/Saturation/Value", {
        "Hue": hue, "Saturation": sat, "Value": val,
        "Fac": Socket("Factor", fac, identifier="Fac"), "Color": live_color(color),
    })
    expr = resolve(node)
    assert "ND_hsvadjust_color3" not in str(expr)
    assert_constants_match_nodedefs(expr)
    assert evaluate(expr) == pytest.approx(svm_node_hsv(color, hue, sat, val, fac), abs=HALF)


# --- 2. Separate Color in HSV and HSL -------------------------------------------

@pytest.mark.parametrize("color", [
    (0.8, 0.2, 0.4), (0.2, 0.7, 0.3), (0.1, 0.3, 0.9), (0.5, 0.5, 0.5), (0.0, -1.0, -0.5), (1.5, 0.25, 0.0),
])
def test_separate_color_hsv_matches_cycles(color):
    node = Node("SEPARATE_COLOR", "Separate Color", {"Color": live_color(color)},
                outputs=("Red", "Green", "Blue"), mode="HSV")
    want = rgb_to_hsv(color)
    for index, output in enumerate(("Red", "Green", "Blue")):
        got = evaluate(resolve(node, output, expected_type="float"))
        assert got == pytest.approx(want[index], abs=HALF), (color, output)


def test_separate_color_hsl_is_refused_in_the_resolver_and_the_validator():
    """HSL used to read the RGB channels, since the sockets keep their names."""
    node = Node("SEPARATE_COLOR", "Separate Color", {"Color": live_color((0.8, 0.2, 0.4))},
                outputs=("Red", "Green", "Blue"), mode="HSL")
    expr = resolve(node, "Red", expected_type="float")
    assert expr["kind"] == "unresolved" and "HSL" in expr["reason"]
    result = validate.validate_material(_material_with(node), only_connected=False)
    assert any("HSL" in issue["message"] for issue in result["warnings"])


def _material_with(node):
    from types import SimpleNamespace

    node.mute = False
    return SimpleNamespace(name="M", node_tree=SimpleNamespace(nodes=[node], links=()))


# --- 3 and 4. Colour into a scalar socket is linear RGB to gray -----------------

def test_an_rgb_constant_into_a_float_socket_is_gray():
    """(1, 0, 0) into Roughness used to author 1.0, its first component."""
    node = rgb((1.0, 0.0, 0.0)).from_node
    got = core._resolve_socket_value(Socket("Roughness", link=node.out()), expected_type="float")
    assert got["value"] == pytest.approx(CYCLES_GRAY[0], abs=1e-7)
    assert core._coerce_constant_value([0.2, 0.5, 0.9], "float") == pytest.approx(gray((0.2, 0.5, 0.9)), abs=1e-7)


def test_a_separate_color_channel_of_an_rgb_constant_reads_that_channel():
    node = Node("SEPARATE_COLOR", "Separate Color", {"Color": rgb((0.1, 0.6, 0.3))},
                outputs=("Red", "Green", "Blue"), mode="RGB")
    assert resolve(node, "Green", expected_type="float") == {"kind": "constant", "value": 0.6}


@pytest.mark.parametrize("expected_type", ["float", "vector3", "color3"])
@pytest.mark.parametrize("fac", [1.0, 0.3])
def test_invert_color_matches_cycles_for_every_consumer(expected_type, fac):
    """A float consumer used to read the red channel and ignore Fac, and a
    vector consumer got the colour uninverted."""
    color = (0.2, 0.5, 0.9)
    node = Node("INVERT", "Invert Color", {"Fac": Socket("Factor", fac, identifier="Fac"), "Color": live_color(color)})
    inverted = tuple(fac * (1.0 - c) + (1.0 - fac) * c for c in color)
    got = evaluate(resolve(node, expected_type=expected_type))
    want = gray(inverted) if expected_type == "float" else inverted
    assert got == pytest.approx(want, abs=1e-6)


def test_rgb_to_bw_is_cycles_gray_not_realitykit_luminance():
    """ND_luminance's default coefficients are ACEScg's: red read 0.2722."""
    color = (1.0, 0.0, 0.0)
    node = Node("RGBTOBW", "RGB to BW", {"Color": live_color(color)}, outputs=("Val",))
    expr = resolve(node, expected_type="float")
    assert "ND_luminance" not in str(expr)
    assert evaluate(expr) == pytest.approx(CYCLES_GRAY[0], abs=1e-7)
    assert evaluate(resolve(node, expected_type="color3")) == pytest.approx((CYCLES_GRAY[0],) * 3, abs=1e-7)


def test_a_channelless_texture_into_a_float_is_gray():
    texture = {"kind": "texture", "path": "chroma.png", "output_type": "float"}
    expr = core._float_math_input_expr(texture)
    weights = expr["inputs"]["in2"]["value"]
    assert weights == pytest.approx(CYCLES_GRAY, abs=1e-7)


# --- 5. Mix ------------------------------------------------------------------------

def _mix(data_type, factor, a, b, **settings):
    inputs = [
        Socket("Factor", factor if not isinstance(factor, mx_eval.Link) else None,
               link=factor if isinstance(factor, mx_eval.Link) else None,
               identifier="Factor_Float", enabled=settings.get("factor_mode", "UNIFORM") == "UNIFORM" or data_type != "VECTOR"),
        Socket("Factor", None, identifier="Factor_Vector",
               enabled=data_type == "VECTOR" and settings.get("factor_mode") == "NON_UNIFORM"),
    ]
    if data_type == "VECTOR" and settings.get("factor_mode") == "NON_UNIFORM":
        inputs[1] = Socket("Factor", factor if not isinstance(factor, mx_eval.Link) else None,
                           link=factor if isinstance(factor, mx_eval.Link) else None, identifier="Factor_Vector")
        inputs[0].enabled = False
    for kind in ("FLOAT", "VECTOR", "RGBA"):
        for name, value in (("A", a), ("B", b)):
            enabled = kind == data_type
            live = isinstance(value, mx_eval.Link)
            inputs.append(Socket(name, None if live or not enabled else value, link=value if live and enabled else None,
                                 identifier=f"{name}_{kind}", enabled=enabled))
    node = Node("MIX", "Mix", {}, outputs=("Result",), data_type=data_type,
                blend_type=settings.get("blend_type", "MIX"), clamp_factor=settings.get("clamp_factor", True),
                clamp_result=settings.get("clamp_result", False), factor_mode=settings.get("factor_mode", "UNIFORM"))
    node.inputs = mx_eval.Inputs(inputs)
    return node


@pytest.mark.parametrize(("blend", "factor", "clamp_factor", "clamp_result"), [
    ("MIX", 1.5, True, False),        # Clamp Factor saturates the weight
    ("MIX", 1.5, False, False),       # without it the mix extrapolates
    ("MULTIPLY", -0.4, True, False),
    ("ADD", 0.6, True, True),         # Clamp Result saturates the colour
    ("SUBTRACT", 0.8, False, True),
])
def test_mix_color_matches_svm_node_mix_color(blend, factor, clamp_factor, clamp_result):
    a, b = (0.9, 0.4, 0.2), (0.7, 1.3, -0.3)
    node = _mix("RGBA", float_value(factor), live_color(a), live_color(b),
                blend_type=blend, clamp_factor=clamp_factor, clamp_result=clamp_result)
    t = saturate(factor) if clamp_factor else factor
    want = svm_mix(blend, t, a, b)
    if clamp_result:
        want = tuple(saturate(c) for c in want)
    assert evaluate(resolve(node)) == pytest.approx(want, abs=1e-6)


def test_legacy_mixrgb_always_clamps_its_factor_and_honours_use_clamp():
    a, b = (0.9, 0.4, 0.2), (0.7, 1.3, -0.3)
    node = Node("MIX_RGB", "Mix", {"Fac": Socket("Factor", link=float_value(1.7), identifier="Fac"),
                                   "Color1": live_color(a), "Color2": live_color(b)},
                blend_type="ADD", use_clamp=True)
    want = tuple(saturate(c) for c in svm_mix("ADD", 1.0, a, b))
    assert evaluate(resolve(node)) == pytest.approx(want, abs=1e-6)


def test_a_float_mix_ignores_blend_type_converts_each_input_and_broadcasts():
    """blend_type applies to Color only; a colour into a Float mix converts at
    the input socket, a constant colour included, and the float result feeds a
    colour socket as a grey."""
    a_color = (0.2, 0.6, 0.9)
    node = _mix("FLOAT", float_value(0.25), rgb(a_color), live_color((0.1, 0.9, 0.4)),
                blend_type="MULTIPLY")
    t = 0.25
    a, b = gray(a_color), gray((0.1, 0.9, 0.4))
    want = a * (1 - t) + b * t
    expr = resolve(node, expected_type="color3")
    assert_constants_match_nodedefs(expr)
    assert evaluate(expr) == pytest.approx((want,) * 3, abs=1e-6)


def test_a_vector_mix_ignores_blend_type():
    node = _mix("VECTOR", float_value(0.25), vector_value((1.0, 2.0, 3.0)), vector_value((3.0, 2.0, 1.0)),
                blend_type="ADD")
    want = tuple(x * 0.75 + y * 0.25 for x, y in zip((1.0, 2.0, 3.0), (3.0, 2.0, 1.0)))
    assert evaluate(resolve(node, expected_type="vector3")) == pytest.approx(want, abs=1e-9)


@pytest.mark.parametrize("clamp_factor", [True, False])
def test_a_non_uniform_vector_mix_mixes_each_component(clamp_factor):
    """svm_node_mix_vector_non_uniform: this used to fail the export."""
    factor = (0.25, 1.5, -0.5)
    a, b = (1.0, 2.0, 3.0), (3.0, 2.5, 1.0)
    node = _mix("VECTOR", vector_value(factor), vector_value(a), vector_value(b),
                factor_mode="NON_UNIFORM", clamp_factor=clamp_factor)
    t = tuple(saturate(f) if clamp_factor else f for f in factor)
    want = tuple(x * (1 - w) + y * w for x, y, w in zip(a, b, t))
    assert evaluate(resolve(node, expected_type="vector3")) == pytest.approx(want, abs=1e-9)


def test_a_rotation_mix_is_refused():
    node = _mix("RGBA", float_value(0.5), live_color((0.1, 0.2, 0.3)), live_color((0.3, 0.2, 0.1)))
    node.data_type = "ROTATION"
    assert not core._is_supported_mix(node)
    assert not validate._is_supported_mix(node)


# --- 6. Map Range --------------------------------------------------------------------

@pytest.mark.parametrize(("value", "from_min", "from_max", "to_min", "to_max", "clamp"), [
    (0.25, 0.0, 1.0, 1.0, 0.0, True),    # inverted To range: Blender 0.75, export was 0
    (1.7, 0.0, 1.0, 1.0, 0.0, True),
    (0.4, 0.5, 0.5, 0.2, 0.9, False),    # From Min == From Max is 0, not a division by zero
    (0.4, 0.5, 0.5, 0.2, 0.9, True),     # ...then clamped into the To range
    (-2.0, -1.0, 3.0, 10.0, 20.0, False),
])
@pytest.mark.parametrize("live_bounds", [False, True])
def test_map_range_matches_cycles(value, from_min, from_max, to_min, to_max, clamp, live_bounds):
    def bound(x):
        return float_value(x) if live_bounds else x
    node = Node("MAP_RANGE", "Map Range", {
        "Value": float_value(value), "From Min": bound(from_min), "From Max": bound(from_max),
        "To Min": bound(to_min), "To Max": bound(to_max),
    }, outputs=("Result", "Vector"), data_type="FLOAT", interpolation_type="LINEAR", clamp=clamp)
    got = evaluate(resolve(node, "Result", expected_type="float"))
    assert math.isfinite(got)
    assert got == pytest.approx(svm_node_map_range_linear(value, from_min, from_max, to_min, to_max, clamp), abs=1e-9)


def test_vector_map_range_matches_svm_node_vector_map_range():
    value, from_min, from_max = (0.25, 2.0, -1.0), (0.0, 1.0, -1.0), (1.0, 3.0, -1.0)
    to_min, to_max = (1.0, 0.0, 5.0), (0.0, 1.0, 6.0)
    names = ("Vector", "From Min", "From Max", "To Min", "To Max")
    node = Node("MAP_RANGE", "Map Range", {}, outputs=("Result", "Vector"),
                data_type="FLOAT_VECTOR", interpolation_type="LINEAR", clamp=True)
    node.inputs = mx_eval.Inputs(
        [Socket("Value", 1.0, enabled=False)]
        + [Socket(name, link=vector_value(v)) for name, v in zip(names, (value, from_min, from_max, to_min, to_max))]
    )
    want = []
    for v, fmin, fmax, tmin, tmax in zip(value, from_min, from_max, to_min, to_max):
        factor = (v - fmin) / (fmax - fmin) if fmax - fmin != 0.0 else 0.0
        result = tmin + factor * (tmax - tmin)
        want.append(min(max(result, tmax), tmin) if tmin > tmax else min(max(result, tmin), tmax))
    assert evaluate(resolve(node, "Vector", expected_type="vector3")) == pytest.approx(tuple(want), abs=1e-9)


def test_stepped_map_range_stays_refused_in_both_places():
    node = Node("MAP_RANGE", "Map Range", {"Value": float_value(0.5)}, outputs=("Result",),
                data_type="FLOAT", interpolation_type="STEPPED", clamp=True)
    assert resolve(node, "Result", expected_type="float")["kind"] == "unresolved"
    assert core.map_range_refusal(node)


# --- 7. Clamp ---------------------------------------------------------------------------

@pytest.mark.parametrize("clamp_type", ["MINMAX", "RANGE"])
@pytest.mark.parametrize(("value", "low", "high"), [(0.5, 1.0, 0.0), (1.5, 0.8, 0.2), (-0.2, 0.0, 1.0)])
@pytest.mark.parametrize("live_bounds", [False, True])
def test_clamp_matches_svm_node_clamp(clamp_type, value, low, high, live_bounds):
    def bound(x):
        return float_value(x) if live_bounds else x
    node = Node("CLAMP", "Clamp", {"Value": float_value(value), "Min": bound(low), "Max": bound(high)},
                outputs=("Result",), clamp_type=clamp_type)
    got = evaluate(resolve(node, expected_type="float"))
    assert got == pytest.approx(svm_node_clamp(value, low, high, clamp_type), abs=1e-9)


# --- 10. Separate XYZ on a colour -------------------------------------------------------

def test_separate_xyz_reads_one_component_of_a_colour_node():
    color = (0.1, 0.6, 0.3)
    node = Node("SEPXYZ", "Separate XYZ", {"Vector": live_color(color)}, outputs=("X", "Y", "Z"))
    for index, output in enumerate(("X", "Y", "Z")):
        assert evaluate(resolve(node, output, expected_type="float")) == pytest.approx(color[index], abs=1e-9)


# --- 13. Float-only nodes into colour sockets, vectors into float inputs ---------------

@pytest.mark.parametrize("builder", ["clamp", "map_range", "float_mix", "math"])
def test_float_only_nodes_feed_a_colour_socket_as_grey(builder):
    if builder == "clamp":
        node = Node("CLAMP", "Clamp", {"Value": float_value(1.4), "Min": 0.0, "Max": 1.0}, outputs=("Result",), clamp_type="MINMAX")
        want = 1.0
    elif builder == "map_range":
        node = Node("MAP_RANGE", "Map Range", {"Value": float_value(0.25), "From Min": 0.0, "From Max": 1.0,
                    "To Min": 2.0, "To Max": 4.0}, outputs=("Result",), data_type="FLOAT", interpolation_type="LINEAR", clamp=True)
        want = 2.5
    elif builder == "float_mix":
        node = _mix("FLOAT", 0.5, float_value(0.2), float_value(0.6))
        want = 0.4
    else:
        node = Node("MATH", "Math", {"Value": float_value(0.2), "Value_001": 3.0, "Value_002": 0.0},
                    outputs=("Value",), operation="MULTIPLY", use_clamp=False)
        want = 0.6
    expr = resolve(node, expected_type="color3")
    assert_constants_match_nodedefs(expr)
    assert evaluate(expr) == pytest.approx((want,) * 3, abs=1e-9)


@pytest.mark.parametrize("builder", ["clamp", "map_range", "combine_xyz"])
def test_a_vector_into_a_float_only_input_is_the_mean(builder):
    vector = vector_value((0.3, 0.6, 1.2))
    mean = 0.7
    if builder == "clamp":
        node = Node("CLAMP", "Clamp", {"Value": vector, "Min": 0.0, "Max": 5.0}, outputs=("Result",), clamp_type="MINMAX")
        want = mean
    elif builder == "map_range":
        node = Node("MAP_RANGE", "Map Range", {"Value": vector, "From Min": 0.0, "From Max": 1.0,
                    "To Min": 0.0, "To Max": 2.0}, outputs=("Result",), data_type="FLOAT", interpolation_type="LINEAR", clamp=False)
        want = 2 * mean
    else:
        node = Node("COMBXYZ", "Combine XYZ", {"X": vector, "Y": 0.5, "Z": 0.0}, outputs=("Vector",))
        want = (mean + 0.5 + 0.0) / 3
    assert evaluate(resolve(node, expected_type="float")) == pytest.approx(want, abs=1e-9)


# --- 14. Brightness/Contrast ------------------------------------------------------------

@pytest.mark.parametrize(("bright", "contrast"), [(0.1, 0.5), (-0.4, 0.2), (0.0, -0.5), (0.3, 0.0)])
def test_brightness_contrast_matches_svm_brightness_contrast(bright, contrast):
    """The colour export used to fail ('inputs:pivot: expected GfVec3f, got
    double'), and Cycles floors the result at 0."""
    color = (0.8, 0.2, 0.05)
    node = Node("BRIGHTCONTRAST", "Brightness/Contrast", {
        "Color": live_color(color), "Bright": Socket("Brightness", bright, identifier="Bright"), "Contrast": contrast,
    })
    a = 1.0 + contrast
    b = bright - contrast * 0.5
    want = tuple(max(a * c + b, 0.0) for c in color)
    expr = resolve(node)
    assert_constants_match_nodedefs(expr)
    assert evaluate(expr) == pytest.approx(want, abs=1e-6)
