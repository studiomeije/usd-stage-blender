"""Blender's Bump node, against Cycles.

Cycles' ``svm_node_set_bump`` reduces to ``normalize(N - Distance grad_s h)``
blended toward N by Strength, with Invert negating Distance. The expected
normals below were baked by Cycles on Blender 5.2: an Emission bake of the
Bump's Normal output, offset by 10 into a float image, on a plane scaled to
2 m by 1 m whose height is ``a u + b v`` of its UVs. The same plane in the
evaluator has dP/du = (2, 0, 0) and dP/dv = (0, 1, 0).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval as mx  # noqa: E402
from mx_eval import Env, Node, evaluate, resolve  # noqa: E402

core = mx.core

CYCLES_BUMP = [
    # a, b, distance, strength, invert -> baked normal
    ((0.3, 0.2, 0.5, 1.0, False), (-0.07442, -0.09923, 0.99228)),
    ((0.3, 0.2, 0.5, 0.4, False), (-0.02982, -0.03976, 0.99876)),
    ((-0.8, 0.5, 0.25, 1.0, True), (-0.09874, 0.12343, 0.98743)),
]


def _plane_env(**extra):
    # World-space readers see RealityKit's Y-up axes; the object sits under the
    # export root's turn, so its model-to-world matrix is that turn.
    primvars = {"blenderDPdu": (2.0, 0.0, 0.0), "blenderDPdv": (0.0, 1.0, 0.0)}
    extra.setdefault("normal_world", mx.to_realitykit((0.0, 0.0, 1.0)))
    return Env(texcoord=(0.37, 0.61), primvars=primvars, model_to_world=mx.ROOT_ROTATION, **extra)


def _uv_height(a, b):
    coords = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV", "Object"))
    return Node("VECT_MATH", "Dot", {"A": coords.out("UV"), "B": (a, b, 0.0), "C": (0.0, 0.0, 0.0), "Scale": 1.0},
                outputs=("Vector", "Value"), operation="DOT_PRODUCT").out("Value")


def _bump(height, distance, strength, invert, normal=None):
    inputs = {"Strength": strength, "Distance": distance, "Filter Width": 0.1, "Height": height}
    inputs["Normal"] = normal if normal is not None else mx.Socket("Normal", (0.0, 0.0, 0.0))
    return Node("BUMP", "Bump", inputs, outputs=("Normal",), invert=invert)


@pytest.mark.parametrize("case, baked", CYCLES_BUMP)
def test_bump_matches_cycles_bake(case, baked):
    a, b, distance, strength, invert = case
    node = _bump(_uv_height(a, b), distance, strength, invert)
    assert evaluate(resolve(node, "Normal", "vector3"), _plane_env()) == pytest.approx(baked, abs=2e-5)


def test_a_position_height_bumps_along_its_gradient_in_the_tangent_plane():
    # h = k . P on a sheared patch: grad_s h is k projected onto the surface.
    pu, pv = (0.8, 0.3, 0.1), (-0.2, 0.9, 0.4)
    n = _normalize(_cross(pu, pv))
    k = (0.4, -0.7, 0.25)
    coords = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV", "Object"))
    height = Node("VECT_MATH", "Dot", {"A": coords.out("Object"), "B": k, "C": (0.0, 0.0, 0.0), "Scale": 1.0},
                  outputs=("Vector", "Value"), operation="DOT_PRODUCT").out("Value")
    node = _bump(height, 0.6, 1.0, False)
    env = Env(normal_world=mx.to_realitykit(n), position_object=(0.2, 0.1, 0.3), model_to_world=mx.ROOT_ROTATION,
              primvars={"blenderDPdu": pu, "blenderDPdv": pv})
    projected = tuple(ki - n_i * sum(x * y for x, y in zip(k, n)) for ki, n_i in zip(k, n))
    expected = _normalize(tuple(ni - 0.6 * gi for ni, gi in zip(n, projected)))
    assert evaluate(resolve(node, "Normal", "vector3"), env) == pytest.approx(expected, abs=1e-4)


def test_an_unlinked_height_leaves_the_normal():
    node = _bump(mx.Socket("Height", 1.0), 0.5, 1.0, False)
    env = Env(normal_world=mx.to_realitykit((0.0, 0.6, 0.8)))
    assert evaluate(resolve(node, "Normal", "vector3"), env) == pytest.approx((0.0, 0.6, 0.8))


def test_a_height_from_a_mesh_attribute_is_refused(monkeypatch):
    monkeypatch.setattr(core, "_attribute_description", lambda node, name: ("generic", "FLOAT", "POINT"))
    attribute = Node("ATTRIBUTE", "Attribute", {}, outputs=("Color", "Vector", "Fac", "Alpha"),
                     attribute_name="wear", attribute_type="GEOMETRY")
    node = _bump(attribute.out("Fac"), 0.5, 1.0, False)
    resolved = resolve(node, "Normal", "vector3")
    assert resolved["kind"] == "unresolved" and "attribute" in resolved["reason"]


def test_a_bump_normal_reaches_pbr_surface_2_in_tangent_space():
    node = _bump(_uv_height(0.3, 0.2), 0.5, 1.0, False)
    tangent, bitangent = (0.6, 0.8, 0.0), (-0.8, 0.6, 0.0)
    env = _plane_env(tangent_world=mx.to_realitykit(tangent), bitangent_world=mx.to_realitykit(bitangent))
    world = evaluate(resolve(node, "Normal", "vector3"), env)
    tangent_space = evaluate(core.world_normal_to_tangent_space(resolve(node, "Normal", "vector3")), env)
    assert tangent_space == pytest.approx((_dot(world, tangent), _dot(world, bitangent), world[2]))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normalize(v):
    length = math.sqrt(_dot(v, v))
    return tuple(x / length for x in v)


def test_the_export_writes_uv_derivatives_per_corner():
    pytest.importorskip("pxr")
    from Plugin.export import postprocess_usd

    points = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (2.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    dpdu, dpdv = postprocess_usd._uv_derivatives(points, [4], [0, 1, 2, 3], uvs)
    assert all(tuple(v) == pytest.approx((2.0, 0.0, 0.0)) for v in dpdu)
    assert all(tuple(v) == pytest.approx((0.0, 1.0, 0.0)) for v in dpdv)
