"""Blender's Math node guards, checked on values against Cycles.

Cycles' ``svm_math`` (intern/cycles/kernel/svm/math_util.h) calls the safe_*
functions of intern/cycles/util/math_base.h, which return 0 where RealityKit's
bare fdiv, sqrt, pow, log, asin and atan2 return inf or NaN, and its round is
``floor(x + 0.5)`` where RealityKit's goes half away from zero. The shared
evaluator models RealityKit's arithmetic, so a missing guard fails here on
the number, not on a node name. The expected values come from the port
below, never from the exporter.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402
from mx_eval import Node, core, evaluate, float_value, resolve  # noqa: E402


# --- Cycles, ported ------------------------------------------------------------

def _safe_divide(a, b):
    return a / b if b != 0.0 else 0.0


def _compatible_powf(x, y):
    if y == 0.0:
        return 1.0
    if x == 0.0:
        return 0.0
    if x < 0.0:
        return (-x) ** y if math.fmod(-y, 2.0) == 0.0 else -((-x) ** y)
    return x ** y


def _safe_powf(a, b):
    if a < 0.0 and b != float(int(b)):
        return 0.0
    return _compatible_powf(a, b)


def _safe_logf(a, b):
    if a <= 0.0 or b <= 0.0:
        return 0.0
    return _safe_divide(math.log(a), math.log(b))


def _compatible_atan2(y, x):
    return 0.0 if x == 0.0 and y == 0.0 else math.atan2(y, x)


def _safe_floored_modulo(a, b):
    return a - math.floor(a / b) * b if b != 0.0 else 0.0


_CYCLES = {
    "DIVIDE": _safe_divide,
    "POWER": _safe_powf,
    "LOGARITHM": _safe_logf,
    "SQRT": lambda a, b: math.sqrt(max(a, 0.0)),
    "ARCSINE": lambda a, b: math.asin(min(max(a, -1.0), 1.0)),
    "ARCCOSINE": lambda a, b: math.acos(min(max(a, -1.0), 1.0)),
    "ARCTAN2": _compatible_atan2,
    "FLOORED_MODULO": _safe_floored_modulo,
    "ROUND": lambda a, b: math.floor(a + 0.5),
}


def _math(operation, a, b, *, link=(True, True)):
    """A Math node whose operands are live (linked) or constants."""
    sockets = {}
    for name, value, live in (("Value", a, link[0]), ("Value_001", b, link[1])):
        sockets[name] = float_value(value) if live else value
    sockets["Value_002"] = 0.0
    return Node("MATH", "Math", sockets, outputs=("Value",), operation=operation, use_clamp=False)


_CASES = [
    ("DIVIDE", 1.0, 0.0),
    ("DIVIDE", 0.0, 0.0),
    ("DIVIDE", -3.0, 4.0),
    ("SQRT", -1.0, 0.0),
    ("SQRT", 2.25, 0.0),
    ("POWER", -2.0, 0.5),
    ("POWER", -2.0, 3.0),
    ("POWER", -2.0, 2.0),
    ("POWER", 0.0, -1.0),
    ("POWER", 0.0, 0.0),
    ("POWER", -3.0, 0.0),
    ("POWER", 1.7, 2.2),
    ("LOGARITHM", -1.0, 10.0),
    ("LOGARITHM", 2.0, 1.0),
    ("LOGARITHM", 2.0, -3.0),
    ("LOGARITHM", 0.0, 10.0),
    ("LOGARITHM", 5.0, 10.0),
    ("ARCSINE", 2.0, 0.0),
    ("ARCSINE", -0.3, 0.0),
    ("ARCCOSINE", 2.0, 0.0),
    ("ARCCOSINE", -1.5, 0.0),
    ("ARCTAN2", 0.0, 0.0),
    ("ARCTAN2", 0.5, -0.25),
    ("FLOORED_MODULO", 1.0, 0.0),
    ("FLOORED_MODULO", -1.25, 0.5),
    ("ROUND", -0.5, 0.0),
    ("ROUND", 0.5, 0.0),
    ("ROUND", -1.5, 0.0),
    ("ROUND", 2.49, 0.0),
]


@pytest.mark.parametrize(("operation", "a", "b"), _CASES)
@pytest.mark.parametrize("link", [(True, True), (True, False), (False, True)], ids=["linked", "b-constant", "a-constant"])
def test_guarded_math_matches_cycles(operation, a, b, link):
    got = evaluate(resolve(_math(operation, a, b, link=link), expected_type="float"))
    want = _CYCLES[operation](a, b)
    assert math.isfinite(got), (operation, a, b, got)
    assert got == pytest.approx(want, abs=1e-9), (operation, a, b)


def test_a_colour_red_plus_zero_into_base_color_is_grey_like_blender():
    """The identity collapse used to hand Base Color the red colour itself.
    Blender converts the colour to a float at the Math input (linear RGB to
    gray), adds 0, and repeats the float in r, g and b."""
    red = Node("RGB", "RGB", {}, outputs=("Color",))
    red.outputs[0].default_value = (1.0, 0.0, 0.0, 1.0)
    live = Node(
        "MIX", "Mix", {"Factor": float_value(0.0), "A": red.out(), "B": (0.0, 0.0, 0.0, 1.0)},
        outputs=("Result",), data_type="RGBA", blend_type="MIX", clamp_factor=True,
        clamp_result=False, factor_mode="UNIFORM",
    )
    add = Node("MATH", "Math", {"Value": live.out(), "Value_001": 0.0, "Value_002": 0.0},
               outputs=("Value",), operation="ADD", use_clamp=False)
    # Measured in Cycles: RGB to BW of pure red renders 0.21263909.
    assert evaluate(resolve(add, expected_type="color3")) == pytest.approx((0.21263909,) * 3, abs=1e-7)


def test_a_vector_into_a_math_input_takes_the_mean():
    vector = mx_eval.vector_value((0.3, 0.6, 1.2))
    node = Node("MATH", "Math", {"Value": vector, "Value_001": 2.0, "Value_002": 0.0},
                outputs=("Value",), operation="MULTIPLY", use_clamp=False)
    assert evaluate(resolve(node, expected_type="float")) == pytest.approx(0.7 * 2.0, abs=1e-9)


def test_math_tables_no_longer_map_a_guarded_op_to_a_bare_nodedef():
    bare = set(core._MATH_SINGLE_INPUT_OPS) | set(core._MATH_TWO_INPUT_OPS) | set(core._MATH_NAMED_INPUT_OPS)
    assert bare.isdisjoint(_CYCLES)
