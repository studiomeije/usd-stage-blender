"""Blender Vector Rotate -> MaterialX, checked on values against Blender.

The expected results were measured from Blender 5.2's own Vector Rotate node:
a one-vertex mesh whose Geometry Nodes modifier sets Position to Vector Rotate
of Position, read back from the evaluated mesh. The exported expression is
evaluated with the arithmetic evaluator from ``test_vector_math``, so the
angle unit, the rotation direction, Center, Invert and the Euler order all
fail on numbers. MaterialX ``rotate3d`` would fail twice here: its amount is
in degrees, and its reference implementation rotates the other way.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vector_math as vm  # noqa: E402

core = vm.core

VECTOR = (0.3, -1.25, 2.0)
CENTER = (0.4, 0.1, -0.7)
AXIS = (0.2, -0.6, 1.5)
ANGLE = 0.7
EULER = (0.5, -0.9, 1.3)

#: (rotation_type, invert) -> Blender's result for VECTOR about CENTER.
BLENDER = {
    ("AXIS_ANGLE", False): (0.56969, -1.463295, 1.878724),
    ("AXIS_ANGLE", True): (0.24914, -0.917174, 2.139912),
    ("X_AXIS", False): (0.3, -2.671925, 0.49538),
    ("X_AXIS", True): (0.3, 0.806851, 2.234768),
    ("Y_AXIS", False): (2.062904, -1.25, 1.429496),
    ("Y_AXIS", True): (-1.415872, -1.25, 1.300652),
    ("Z_AXIS", False): (1.19321, -0.996959, 2.0),
    ("Z_AXIS", True): (-0.546178, -0.868115, 2.0),
    ("EULER_XYZ", False): (2.411333, -1.922995, 0.292234),
    ("EULER_XYZ", True): (1.689762, 1.170846, 1.81243),
}


class _Socket:
    def __init__(self, name, value=None, *, enabled=True, link=None):
        self.name = name
        self.default_value = value
        self.enabled = enabled
        self.is_linked = link is not None
        self.links = [link] if link is not None else []


class _Euler:
    """Like mathutils.Euler: a sequence by indexing, with no ``__iter__``."""

    def __init__(self, values):
        self._values = tuple(values)

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        return self._values[index]


class _Inputs(list):
    def get(self, name):
        # Blender returns None for a socket its node has disabled.
        for socket in self:
            if socket.name == name:
                return socket if socket.enabled else None
        return None


def _linked_vector(value):
    """A vector that resolves to a node expression rather than a constant."""
    node = vm._vmath("ADD", value, (0.0, 0.0, 0.0))
    return vm._Link(node, node.outputs[0])


def _linked_float(value):
    node = vm._vmath("DOT_PRODUCT", (value, 0.0, 0.0), (1.0, 0.0, 0.0))
    return vm._Link(node, node.outputs[1])


def _rotate(rotation_type, invert, *, axis=AXIS, linked=False):
    def vector(name, value, enabled=True):
        if linked:
            return _Socket(name, enabled=enabled, link=_linked_vector(value))
        return _Socket(name, value, enabled=enabled)

    uses_angle = rotation_type != "EULER_XYZ"
    node = vm._Node()
    node.type = "VECTOR_ROTATE"
    node.name = "Vector Rotate"
    node.rotation_type = rotation_type
    node.invert = invert
    angle = (
        _Socket("Angle", enabled=uses_angle, link=_linked_float(ANGLE))
        if linked else _Socket("Angle", ANGLE, enabled=uses_angle)
    )
    node.inputs = _Inputs([
        vector("Vector", VECTOR),
        vector("Center", CENTER),
        vector("Axis", axis, enabled=rotation_type == "AXIS_ANGLE"),
        angle,
        _Socket("Rotation", enabled=rotation_type == "EULER_XYZ", link=_linked_vector(EULER))
        if linked else _Socket("Rotation", _Euler(EULER), enabled=rotation_type == "EULER_XYZ"),
    ])
    node.outputs = [_Socket("Vector")]
    return vm._resolve(node, "Vector")


@pytest.mark.parametrize("linked", [False, True], ids=["constant", "linked"])
@pytest.mark.parametrize("case", sorted(BLENDER), ids=lambda c: f"{c[0]}-{'invert' if c[1] else 'plain'}")
def test_vector_rotate_matches_blender(case, linked):
    expr = _rotate(*case, linked=linked)
    # A linked angle cannot fold its cosine; a constant one must.
    assert ("ND_cos_float" in _node_ids(expr)) == linked
    assert vm._evaluate(expr) == pytest.approx(BLENDER[case], abs=1e-5)


def _node_ids(expr):
    if expr.get("kind") != "node":
        return set()
    ids = {expr["node_id"]}
    for value in expr.get("inputs", {}).values():
        ids |= _node_ids(value)
    return ids


@pytest.mark.parametrize("linked", [False, True], ids=["constant", "linked"])
def test_a_zero_axis_passes_the_vector_through_like_blender(linked):
    expr = _rotate("AXIS_ANGLE", False, axis=(0.0, 0.0, 0.0), linked=linked)
    assert vm._evaluate(expr) == pytest.approx(VECTOR, abs=1e-6)


def test_the_angle_is_in_radians():
    # A quarter turn about Z maps +X to +Y about the origin.
    node = vm._Node()
    node.type = "VECTOR_ROTATE"
    node.name = "Vector Rotate"
    node.rotation_type = "Z_AXIS"
    node.invert = False
    node.inputs = _Inputs([
        _Socket("Vector", (1.0, 0.0, 0.0)),
        _Socket("Center", (0.0, 0.0, 0.0)),
        _Socket("Axis", (0.0, 0.0, 1.0), enabled=False),
        _Socket("Angle", math.pi / 2),
        _Socket("Rotation", (0.0, 0.0, 0.0), enabled=False),
    ])
    node.outputs = [_Socket("Vector")]
    assert vm._evaluate(vm._resolve(node, "Vector")) == pytest.approx((0.0, 1.0, 0.0), abs=1e-9)


def test_vector_transform_is_bake_only():
    validate = vm.validate
    assert "VECT_TRANSFORM" in validate.BAKE_TYPES
    assert "VECT_TRANSFORM" not in validate.SUPPORTED_TYPES
