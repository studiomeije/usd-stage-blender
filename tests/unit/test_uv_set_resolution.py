"""Which texture-coordinate set a UV map name reads.

Blender's USD exporter writes each mesh's render UV map as ``primvars:st``,
which RealityKit's ``texcoord`` reader returns, and every other UV map under
its own name (measured on Blender 5.2: with ``Detail`` the render UV map,
``primvars:st`` held Detail and ``primvars:UVMap`` the other set; the
integration test ``test_uv_set_export`` repeats that export). A name
therefore reads ``texcoord`` only where it is the render UV map, whatever it
is called, and its own primvar otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval as mx  # noqa: E402
from mx_eval import Env, Node, evaluate, resolve  # noqa: E402

core = mx.core

RENDER_UV = (0.9, 0.1)
OTHER_UV = (0.2, 0.7)


class _Layers(list):
    def get(self, name):
        return next((layer for layer in self if layer.name == name), None)


def _mesh(name, layers, render):
    return SimpleNamespace(name=name, uv_layers=_Layers(
        SimpleNamespace(name=layer, active_render=layer == render) for layer in layers
    ))


@pytest.fixture
def meshes(monkeypatch):
    found = [_mesh("Plane", ["UVMap", "Detail"], render="Detail")]
    monkeypatch.setattr(core, "_material_meshes", lambda node: found)
    return found


def _env():
    # texcoord is the render UV map (Detail); primvars:UVMap the other set.
    return Env(texcoord=RENDER_UV, primvars={"UVMap": OTHER_UV, "Detail": (-1.0, -1.0)})


def test_a_uv_map_node_naming_a_non_render_map_reads_its_primvar(meshes):
    node = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="UVMap", from_instancer=False)
    assert evaluate(resolve(node, "UV", "vector3"), _env()) == pytest.approx(OTHER_UV + (0.0,))


def test_a_uv_map_node_naming_the_render_map_reads_texcoord(meshes):
    node = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="Detail", from_instancer=False)
    assert evaluate(resolve(node, "UV", "vector3"), _env()) == pytest.approx(RENDER_UV + (0.0,))
    unnamed = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="", from_instancer=False)
    assert evaluate(resolve(unnamed, "UV", "vector3"), _env()) == pytest.approx(RENDER_UV + (0.0,))


def test_an_attribute_named_uvmap_reads_that_set_not_the_render_map(meshes, monkeypatch):
    monkeypatch.setattr(core, "_attribute_description", lambda node, name: ("uv", None, "CORNER"))
    node = Node("ATTRIBUTE", "Attribute", {}, outputs=("Color", "Vector", "Fac", "Alpha"),
                attribute_name="UVMap", attribute_type="GEOMETRY")
    assert evaluate(resolve(node, "Vector", "vector3"), _env()) == pytest.approx(OTHER_UV + (0.0,))


def test_an_image_through_a_uv_map_node_carries_the_resolved_set(meshes, monkeypatch):
    monkeypatch.setattr(core, "_resolve_image_path", lambda image: "/assets/grid.png")
    monkeypatch.setattr(core, "_image_source_alpha", lambda image, path: {"source_channels": 3, "source_has_alpha": False})
    image = SimpleNamespace(name="grid", alpha_mode="STRAIGHT", colorspace_settings=SimpleNamespace(name="sRGB"))
    for uv_map, texcoord in (("UVMap", "UVMap"), ("Detail", "UV0")):
        uv = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map=uv_map, from_instancer=False)
        node = Node("TEX_IMAGE", "Grid", {"Vector": uv.out("UV")}, outputs=("Color", "Alpha"), image=image,
                    projection="FLAT", interpolation="Linear", extension="REPEAT")
        assert core.image_uses_uv_transform(node)
        assert resolve(node, "Color")["uv_map"] == texcoord


def test_a_uv_map_missing_from_a_mesh_or_render_on_only_some_is_refused(meshes):
    node = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="Paint", from_instancer=False)
    resolved = resolve(node, "UV", "vector3")
    assert resolved["kind"] == "unresolved" and "'Paint' is not on the mesh 'Plane'" in resolved["reason"]

    meshes.append(_mesh("Other", ["UVMap", "Detail"], render="UVMap"))
    node = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="Detail", from_instancer=False)
    resolved = resolve(node, "UV", "vector3")
    assert resolved["kind"] == "unresolved" and "some meshes" in resolved["reason"]


def test_a_uv_map_node_reading_from_the_instancer_is_refused(meshes, monkeypatch):
    node = Node("UVMAP", "UV Map", {}, outputs=("UV",), uv_map="UVMap", from_instancer=True)
    resolved = resolve(node, "UV", "vector3")
    assert resolved["kind"] == "unresolved" and "From Instancer" in resolved["reason"]

    monkeypatch.setattr(core, "_resolve_image_path", lambda image: "/assets/grid.png")
    monkeypatch.setattr(core, "_image_source_alpha", lambda image, path: {"source_channels": 3, "source_has_alpha": False})
    image = SimpleNamespace(name="grid", alpha_mode="STRAIGHT", colorspace_settings=SimpleNamespace(name="sRGB"))
    texture = Node("TEX_IMAGE", "Grid", {"Vector": node.out("UV")}, outputs=("Color", "Alpha"), image=image,
                   projection="FLAT", interpolation="Linear", extension="REPEAT")
    assert not core.image_uses_uv_transform(texture)
    resolved = resolve(texture, "Color")
    assert resolved["kind"] == "unresolved" and "From Instancer" in resolved["reason"]
