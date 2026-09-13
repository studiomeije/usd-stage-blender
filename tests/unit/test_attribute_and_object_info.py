"""The Attribute node and Object Info > Random.

Attribute outputs follow Cycles' ``svm_node_attr_surface_eval``: a float
repeats into Color and Vector, Fac is the float or the mean of three
components (the first for a UV map), and Alpha is 1 except for a colour's
fourth channel. Object Info > Random is Cycles' per-object hash of the
object's name; the expected values below were baked from Cycles on Blender
5.2 (an Emission bake of the Random output into a float image), including a
non-ASCII name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval as mx  # noqa: E402
from mx_eval import Env, Node, evaluate, resolve  # noqa: E402

core = mx.core

CYCLES_RANDOM = {
    "Cube": 0.31314194202423096,
    "Attr Cube.001": 0.05335604399442673,
    "Ünïcode-name": 0.13577021658420563,
    "Plane.042": 0.6699268817901611,
    "a": 0.5055515170097351,
}


@pytest.mark.parametrize("name", sorted(CYCLES_RANDOM))
def test_object_random_matches_cycles_bake(name):
    assert core.cycles_object_random(name) == CYCLES_RANDOM[name]


def _attribute(name, description, monkeypatch, attribute_type="GEOMETRY", color_index=0):
    monkeypatch.setattr(core, "_attribute_description", lambda node, n: description)
    monkeypatch.setattr(core, "_color_attribute_set_index", lambda node, n: color_index)
    return Node("ATTRIBUTE", "Attribute", {}, outputs=("Color", "Vector", "Fac", "Alpha"),
                attribute_name=name, attribute_type=attribute_type)


def _outputs(node, env):
    return {
        "Color": evaluate(resolve(node, "Color", "color3"), env),
        "Vector": evaluate(resolve(node, "Vector", "vector3"), env),
        "Fac": evaluate(resolve(node, "Fac", "float"), env),
        "Alpha": evaluate(resolve(node, "Alpha", "float"), env),
    }


def test_a_float_attribute_repeats_and_has_alpha_one(monkeypatch):
    node = _attribute("wear", ("generic", "FLOAT", "POINT"), monkeypatch)
    got = _outputs(node, Env(primvars={"wear": 0.37}))
    assert got["Color"] == pytest.approx((0.37,) * 3)
    assert got["Vector"] == pytest.approx((0.37,) * 3)
    assert got["Fac"] == pytest.approx(0.37)
    assert got["Alpha"] == 1.0


def test_a_vector_attribute_fac_is_the_mean(monkeypatch):
    node = _attribute("flow", ("generic", "FLOAT_VECTOR", "CORNER"), monkeypatch)
    got = _outputs(node, Env(primvars={"flow": (0.2, -0.5, 0.9)}))
    assert got["Vector"] == pytest.approx((0.2, -0.5, 0.9))
    assert got["Fac"] == pytest.approx((0.2 - 0.5 + 0.9) / 3)
    assert got["Alpha"] == 1.0


def test_a_color_attribute_reads_the_vertex_colour_with_its_alpha(monkeypatch):
    node = _attribute("Col", ("color", "BYTE_COLOR", "CORNER"), monkeypatch)
    got = _outputs(node, Env(vertex_color=(0.9, 0.3, 0.0, 0.25)))
    assert got["Color"] == pytest.approx((0.9, 0.3, 0.0))
    assert got["Fac"] == pytest.approx(0.4)  # Cycles' average, not luminance
    assert got["Alpha"] == pytest.approx(0.25)


def test_a_uv_map_attribute_fac_is_u(monkeypatch):
    node = _attribute("UVMap", ("uv", None, "CORNER"), monkeypatch)
    got = _outputs(node, Env(texcoord=(0.7, 0.2)))
    assert got["Vector"] == pytest.approx((0.7, 0.2, 0.0))
    assert got["Fac"] == pytest.approx(0.7)
    assert got["Alpha"] == 1.0


@pytest.mark.parametrize("description, attribute_type, name, words", [
    (("generic", "FLOAT", "POINT"), "OBJECT", "wear", "Object"),
    (("generic", "FLOAT", "FACE"), "GEOMETRY", "wear", "Face domain"),
    (("generic", "INT", "POINT"), "GEOMETRY", "count", "Int attribute"),
    (("generic", "FLOAT", "POINT"), "GEOMETRY", "2bad", "not a valid USD primvar"),
    (None, "GEOMETRY", "missing", "was not found"),
    (("color", "FLOAT_COLOR", "POINT"), "GEOMETRY", "Second", "first colour attribute"),
])
def test_unexportable_attributes_are_refused_naming_why(description, attribute_type, name, words, monkeypatch):
    node = _attribute(name, description, monkeypatch, attribute_type=attribute_type,
                      color_index=None if name == "Second" else 0)
    resolved = resolve(node, "Fac", "float")
    assert resolved["kind"] == "unresolved"
    assert words in resolved["reason"]


def test_object_info_random_reads_the_primvar_and_other_outputs_refuse():
    node = Node("OBJECT_INFO", "Object Info", {}, outputs=("Location", "Random"))
    assert evaluate(resolve(node, "Random", "float"), Env(primvars={core.OBJECT_RANDOM_PRIMVAR: 0.61})) == pytest.approx(0.61)
    refused = resolve(node, "Location", "vector3")
    assert refused["kind"] == "unresolved" and "Location" in refused["reason"]


def test_the_export_writes_each_objects_random_on_its_meshes():
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdGeom, UsdShade

    from Plugin.export import postprocess_usd

    stage = Usd.Stage.CreateInMemory()
    material = UsdShade.Material.Define(stage, "/root/_materials/M")
    reader = UsdShade.Shader.Define(stage, "/root/_materials/M/random")
    reader.CreateIdAttr("ND_geompropvalue_float")
    reader.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set(core.OBJECT_RANDOM_PRIMVAR)
    other = UsdShade.Material.Define(stage, "/root/_materials/Other")
    for object_name, prim_name, bound in (("Attr Cube.001", "Attr_Cube_001", material), ("Cube", "Cube", other)):
        xform = UsdGeom.Xform.Define(stage, f"/root/{prim_name}")
        xform.GetPrim().CreateAttribute("userProperties:blender:object_name", Sdf.ValueTypeNames.String).Set(object_name)
        mesh = UsdGeom.Mesh.Define(stage, f"/root/{prim_name}/Mesh")
        mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(bound)
    postprocess_usd._author_object_random(stage)
    written = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Attr_Cube_001/Mesh")).GetPrimvar(core.OBJECT_RANDOM_PRIMVAR)
    assert list(written.Get()) == [CYCLES_RANDOM["Attr Cube.001"]] * 3
    assert written.GetInterpolation() == UsdGeom.Tokens.vertex
    untouched = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Cube/Mesh")).GetPrimvar(core.OBJECT_RANDOM_PRIMVAR)
    assert not untouched
