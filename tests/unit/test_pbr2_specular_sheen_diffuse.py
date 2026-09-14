"""Specular IOR Level, IOR, Sheen and Diffuse Roughness on PBR Surface 2.

Two independent references, neither of them the exporter's tables:

- Cycles, ported from ``svm_node_closure_bsdf`` and ``bsdf_util.h``: the
  dielectric lobe's F0 is ``F0_from_ior(ior) * 2 * level`` and its IOR is
  ``ior_from_F0`` of that (the level-0.5 case keeps the IOR); a sheen layer
  exists only above ``CLOSURE_WEIGHT_CUTOFF``; diffuse is Lambert below a
  roughness of 1e-5.
- RealityKit, measured with RealityRenderer on macOS 27 by matching renders
  of black dielectric spheres: while ``baseDiffuseRoughness`` is unauthored
  F0 is 0.08 * saturate(specular) and ``specularIOR`` changes nothing; once
  it is authored F0 is saturate(specular) * F0(specularIOR). Any authored
  ``sheenColor``, black included, replaces the specular lobe (a white metal
  at roughness 0.3: mean 0.378 without, 0 with), and any authored
  ``baseDiffuseRoughness``, 0 included, darkens environment-lit diffuse to
  about a third (a 0.5 grey: 0.357 without, 0.113 with 0).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402
import test_shader_closures as tc  # noqa: E402

core = tc.core
validate = tc.validate

_CUTOFF = 1e-5


# --------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------

def _cycles_f0(ior, level):
    """The dielectric lobe's F0 in Cycles 5.2."""
    def f0_from_ior(x):
        return ((x - 1.0) / (x + 1.0)) ** 2

    def ior_from_f0(f0):
        root = math.sqrt(min(max(f0, 0.0), 0.99))
        return (1.0 + root) / (1.0 - root)

    ior = max(ior, 1e-5)
    level = max(level, 0.0)
    eta = ior
    if level != 0.5:
        eta = ior_from_f0(f0_from_ior(ior) * 2.0 * level)
        if ior < 1.0:
            eta = 1.0 / eta
    return f0_from_ior(eta)


def _realitykit_f0(inputs, evaluate=lambda value: value):
    """PBR Surface 2's dielectric F0 for the authored inputs, as measured."""
    specular = min(max(evaluate(inputs.get("specular", 0.5)), 0.0), 1.0)
    if "baseDiffuseRoughness" in inputs:
        ior = evaluate(inputs.get("specularIOR", 1.5))
        return specular * ((ior - 1.0) / (ior + 1.0)) ** 2
    return 0.08 * specular


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def _principled(**overrides):
    sockets = {}
    for name, value in core._PRINCIPLED_DEFAULTS:
        value = overrides.get(name, value)
        if isinstance(value, mx_eval.Link):
            sockets[name] = mx_eval.Socket(name, link=value)
        else:
            sockets[name] = mx_eval.Socket(name, value)
    return mx_eval.Node("BSDF_PRINCIPLED", "Principled BSDF", sockets, outputs=("BSDF",),
                        distribution="MULTI_GGX", subsurface_method="RANDOM_WALK")


def _export(**overrides):
    node = _principled(**overrides)
    material = tc._material(node)
    data = core.extract_blender_material_data(material)
    graph = tc.MaterialXGraphBuilder(tc._MANIFEST).build_pbr_material(data)
    surface = graph["nodes"][0]
    authored = dict(surface["inputs"])
    for connection in graph["connections"]:
        if connection["to_node"] == surface["name"]:
            authored[connection["to_input"]] = _subgraph(graph, connection["from_node"])
    return material, data, authored


def _subgraph(graph, name):
    """Rebuild the expression tree feeding a surface input from graph nodes."""
    nodes = {node["name"]: node for node in graph["nodes"]}
    node = nodes[name]
    inputs = {key: {"kind": "constant", "value": value} for key, value in node["inputs"].items()}
    for connection in graph["connections"]:
        if connection["to_node"] == name:
            inputs[connection["to_input"]] = _subgraph(graph, connection["from_node"])
    return {"kind": "node", "node_id": node["node_id"], "inputs": inputs}


def _value(authored):
    return mx_eval.evaluate(authored) if isinstance(authored, dict) else authored


# --------------------------------------------------------------------------
# specular
# --------------------------------------------------------------------------

def test_the_default_principled_lands_on_cycles_four_percent():
    _material, _data, inputs = _export()
    assert _cycles_f0(1.5, 0.5) == pytest.approx(0.04)
    assert inputs["specular"] == pytest.approx(0.5)
    assert _realitykit_f0(inputs) == pytest.approx(0.04)
    for silent in ("specularWeight", "specularIOR", "sheenColor", "baseDiffuseRoughness"):
        assert silent not in inputs, silent


@pytest.mark.parametrize("ior, level, diffuse_roughness", [
    (1.45, 0.5, 0.0), (1.5, 0.25, 0.0), (1.33, 1.0, 0.0), (1.5, 0.0, 0.0), (1.6, 0.6, 0.0),
    (1.5, 0.5, 0.4), (3.0, 0.5, 0.4), (1.5, 2.0, 0.4), (0.8, 0.7, 0.4), (2.4, 0.0, 0.4),
])
def test_constant_ior_and_level_reflect_what_cycles_does(ior, level, diffuse_roughness):
    _material, _data, inputs = _export(**{"IOR": ior, "Specular IOR Level": level, "Diffuse Roughness": diffuse_roughness})
    # Diffuse Roughness is never authored, so specular alone carries F0, capped at 0.08.
    expected = min(_cycles_f0(ior, level), 0.08)
    assert "baseDiffuseRoughness" not in inputs
    assert _realitykit_f0(inputs) == pytest.approx(expected, abs=1e-6)
    assert "specularWeight" not in inputs


def test_a_reflectance_above_the_cap_is_capped_and_named():
    material, _data, inputs = _export(**{"IOR": 2.5})
    assert _realitykit_f0(inputs) == pytest.approx(0.08)
    notices = core.principled_notices(core.resolve_surface_closure(material)["principled"])
    assert any(f"{_cycles_f0(2.5, 0.5):.3f}" in n and "caps" in n for n in notices)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True
    assert any("caps" in w["message"] for w in result["warnings"])


def test_a_metal_never_reports_the_dielectric_cap():
    material, _data, _inputs = _export(**{"IOR": 2.5, "Metallic": 1.0})
    assert not any("caps" in n for n in core.principled_notices(core.resolve_surface_closure(material)["principled"]))


@pytest.mark.parametrize("diffuse_roughness", [0.0, 0.4])
@pytest.mark.parametrize("ior, level, link", [
    (1.5, 0.7, "level"), (1.8, 0.5, "ior"), (1.4, 0.3, "both"), (1.5, 1.3, "level"),
])
def test_a_linked_ior_or_level_is_built_to_cycles_arithmetic(ior, level, link, diffuse_roughness):
    overrides = {"Diffuse Roughness": diffuse_roughness,
                 "IOR": mx_eval.float_value(ior) if link in ("ior", "both") else ior,
                 "Specular IOR Level": mx_eval.float_value(level) if link in ("level", "both") else level}
    material, _data, inputs = _export(**overrides)
    expected = min(_cycles_f0(ior, level), 0.08)
    assert _realitykit_f0(inputs, _value) == pytest.approx(expected, abs=1e-6)
    notices = core.principled_notices(core.resolve_surface_closure(material)["principled"])
    assert any("is linked" in n and "caps" in n for n in notices)


# --------------------------------------------------------------------------
# sheen
# --------------------------------------------------------------------------

def test_an_unweighted_sheen_authors_nothing():
    _material, _data, inputs = _export(**{"Sheen Weight": 0.0, "Sheen Tint": (0.2, 0.4, 0.9, 1.0),
                                          "Metallic": 1.0, "Base Color": (1.0, 1.0, 1.0, 1.0)})
    assert "sheenColor" not in inputs


def test_a_linked_tint_under_zero_weight_authors_nothing():
    tint = mx_eval.Node("VECT_MATH", "Vector Math", {"A": (0.3, 0.2, 0.1), "B": (1.0, 1.0, 1.0), "C": (0.0, 0.0, 0.0),
                                                     "Scale": 1.0}, outputs=("Vector", "Value"), operation="MULTIPLY")
    _material, _data, inputs = _export(**{"Sheen Weight": 0.0, "Sheen Tint": tint.out("Vector")})
    assert "sheenColor" not in inputs


def test_a_weighted_sheen_is_weight_times_tint_and_named_as_replacing_the_highlight():
    material, _data, inputs = _export(**{"Sheen Weight": 0.6, "Sheen Tint": (1.0, 0.5, 0.25, 1.0)})
    assert inputs["sheenColor"] == pytest.approx([0.6, 0.3, 0.15])
    notices = core.principled_notices(core.resolve_surface_closure(material)["principled"])
    assert any("Sheen Weight" in n and "replaces the specular lobe" in n for n in notices)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True
    assert any("replaces the specular lobe" in w["message"] for w in result["warnings"])


def test_a_linked_sheen_weight_is_built_as_weight_times_tint():
    _material, _data, inputs = _export(**{"Sheen Weight": mx_eval.float_value(0.35), "Sheen Tint": (0.2, 0.4, 0.8, 1.0)})
    assert _value(inputs["sheenColor"]) == pytest.approx((0.07, 0.14, 0.28))


# --------------------------------------------------------------------------
# diffuse roughness
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value, rough", [(0.0, False), (5e-6, False), (-0.3, False), (0.3, True), (1.6, True)])
def test_diffuse_roughness_is_never_authored_and_named_where_cycles_leaves_lambert(value, rough):
    """Any authored baseDiffuseRoughness switches RealityKit to a rough diffuse
    that lights the environment about a third as brightly (RealityRenderer),
    so Lambert is exported and a rough value is named."""
    material, _data, inputs = _export(**{"Diffuse Roughness": value})
    notices = core.principled_notices(core.resolve_surface_closure(material)["principled"])
    assert "baseDiffuseRoughness" not in inputs
    assert any("Diffuse Roughness" in n and "not exported" in n for n in notices) == rough


def test_a_linked_diffuse_roughness_is_not_authored_but_named():
    material, _data, inputs = _export(**{"Diffuse Roughness": mx_eval.float_value(0.25)})
    assert "baseDiffuseRoughness" not in inputs
    assert any("Diffuse Roughness" in n for n in core.principled_notices(core.resolve_surface_closure(material)["principled"]))


def test_the_diffuse_preset_is_named_under_its_own_socket():
    node = tc._Node(type="BSDF_DIFFUSE", name="Diffuse BSDF")
    node.inputs = tc._Sockets(Color=tc._Socket((0.5, 0.5, 0.5, 1.0), name="Color", socket_type="RGBA"),
                              Roughness=tc._Socket(0.5, name="Roughness"),
                              Normal=tc._Socket((0.0, 0.0, 0.0), name="Normal", socket_type="VECTOR"))
    node.outputs = tc._Sockets(BSDF=tc._Socket(name="BSDF", socket_type="SHADER"))
    result = validate.validate_material(tc._material(node), strict=True)
    assert result["ok"] is True
    assert any(w["message"].startswith("Diffuse BSDF 'Roughness'") for w in result["warnings"])
