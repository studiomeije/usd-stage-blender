"""Unit tests for choosing the UV map a bake writes into (``Plugin/export/bake_uv.py``)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export import bake_uv  # noqa: E402


def _grid(u0, v0, u1, v1, cells):
    """Two triangles per cell over the rectangle (u0, v0)-(u1, v1)."""
    us = np.linspace(u0, u1, cells + 1)
    vs = np.linspace(v0, v1, cells + 1)
    triangles = []
    for i in range(cells):
        for j in range(cells):
            a = (us[i], vs[j])
            b = (us[i + 1], vs[j])
            c = (us[i + 1], vs[j + 1])
            d = (us[i], vs[j + 1])
            triangles += [(a, b, c), (a, c, d)]
    return np.array(triangles)


def test_a_dense_grid_filling_the_square_holds_a_bake():
    assert bake_uv.uv_layout_problem(_grid(0.0, 0.0, 1.0, 1.0, 64)) is None


def test_islands_side_by_side_hold_a_bake_even_when_one_is_mirrored():
    left = _grid(0.0, 0.0, 0.5, 1.0, 8)
    right = _grid(0.5, 0.0, 1.0, 1.0, 8)
    mirrored = right.copy()
    mirrored[..., 0] = 1.5 - mirrored[..., 0]
    assert bake_uv.uv_layout_problem(np.concatenate([left, mirrored])) is None


def test_uvs_that_tile_past_the_square_cannot_hold_a_bake():
    # The diorama floor: UVs from -0.13 to 1.13 wrap onto each other's texels.
    tiled = _grid(-0.1294, -0.1294, 1.1294, 1.1294, 64)
    assert bake_uv.uv_layout_problem(tiled) == bake_uv.LEAVES_UNIT_SQUARE


def test_stacked_islands_cannot_hold_a_bake():
    island = _grid(0.1, 0.1, 0.6, 0.6, 4)
    shifted = island + 0.2
    assert bake_uv.uv_layout_problem(np.concatenate([island, shifted])) == bake_uv.OVERLAPS


def test_a_sliver_of_overlap_below_one_texel_share_is_ignored():
    island = _grid(0.0, 0.0, 0.5, 1.0, 8)
    # Neighbour overlapping the first by a quarter texel of the raster.
    neighbour = _grid(0.5 - 0.25 / 1024, 0.0, 1.0, 1.0, 8)
    assert bake_uv.uv_layout_problem(np.concatenate([island, neighbour])) is None


def test_no_triangles_is_no_problem():
    assert bake_uv.uv_layout_problem(np.zeros((0, 3, 2))) is None


# --- material periodicity -------------------------------------------------


class _Socket:
    def __init__(self, name, *, value=None, identifier=None, type="VALUE"):
        self.name = name
        self.identifier = identifier or name
        self.default_value = value
        self.links = []
        self.type = type

    @property
    def is_linked(self):
        return bool(self.links)


class _Sockets(list):
    def get(self, name, default=None):
        return next((s for s in self if s.name == name or s.identifier == name), default)


class _Node:
    def __init__(self, type, inputs=(), outputs=("Color",), **attrs):
        self.type = type
        self.mute = False
        self.inputs = _Sockets(_Socket(n) if isinstance(n, str) else n for n in inputs)
        self.outputs = _Sockets(_Socket(n) for n in outputs)
        for key, value in attrs.items():
            setattr(self, key, value)


def _link(from_node, output, to_node, input_name):
    link = types.SimpleNamespace(
        from_node=from_node,
        from_socket=from_node.outputs.get(output),
        is_muted=False,
    )
    to_node.inputs.get(input_name).links.append(link)


def _material(*, source):
    """A Principled BSDF whose Base Color comes from ``source`` (node, output)."""
    principled = _Node("BSDF_PRINCIPLED", inputs=("Base Color", "Roughness", "Normal"), outputs=())
    principled.outputs = _Sockets([_Socket("BSDF", type="SHADER")])
    output = _Node("OUTPUT_MATERIAL", inputs=("Surface",), outputs=(), is_active_output=True)
    _link(principled, "BSDF", output, "Surface")
    if source is not None:
        _link(source[0], source[1], principled, "Base Color")
    nodes = [output, principled]
    tree = types.SimpleNamespace(nodes=nodes, get_output_node=lambda target: output)
    return types.SimpleNamespace(node_tree=tree, use_nodes=True), principled


def _image(**attrs):
    attrs.setdefault("extension", "REPEAT")
    attrs.setdefault("projection", "FLAT")
    return _Node("TEX_IMAGE", inputs=("Vector",), outputs=("Color", "Alpha"), **attrs)


def test_a_repeating_image_on_the_render_uv_map_is_periodic():
    material, _ = _material(source=(_image(), "Color"))
    assert bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")


def test_an_image_on_the_render_uv_map_is_not_periodic_when_the_bake_writes_another_map():
    material, _ = _material(source=(_image(), "Color"))
    assert not bake_uv.material_bake_is_uv_periodic(material, bake_uv="Bake", render_uv="UVMap")


@pytest.mark.parametrize("attrs", [{"extension": "EXTEND"}, {"projection": "BOX"}])
def test_an_image_that_does_not_repeat_is_not_periodic(attrs):
    material, _ = _material(source=(_image(**attrs), "Color"))
    assert not bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")


def test_a_procedural_texture_is_not_periodic():
    noise = _Node("TEX_NOISE", inputs=("Vector", "Scale"), outputs=("Fac", "Color"))
    material, _ = _material(source=(noise, "Color"))
    assert not bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")


def test_generated_coordinates_are_not_periodic():
    coords = _Node("TEX_COORD", outputs=("Generated", "UV"), from_instancer=False)
    image = _image()
    _link(coords, "Generated", image, "Vector")
    material, _ = _material(source=(image, "Color"))
    assert not bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")


def _mapping(scale, rotation=(0.0, 0.0, 0.0)):
    return _Node(
        "MAPPING",
        inputs=(
            _Socket("Vector"),
            _Socket("Location", value=(0.3, 0.1, 0.0)),
            _Socket("Rotation", value=rotation),
            _Socket("Scale", value=scale),
        ),
        outputs=("Vector",),
        vector_type="POINT",
    )


@pytest.mark.parametrize(
    "scale, rotation, periodic",
    [
        ((4.0, 4.0, 1.0), (0.0, 0.0, 0.0), True),
        ((2.5, 1.0, 1.0), (0.0, 0.0, 0.0), False),
        ((1.0, 1.0, 1.0), (0.0, 0.0, 0.5), False),
    ],
)
def test_a_mapping_keeps_the_period_only_for_whole_number_tiling(scale, rotation, periodic):
    uv = _Node("UVMAP", outputs=("UV",), uv_map="UVMap", from_instancer=False)
    mapping = _mapping(scale, rotation)
    image = _image()
    _link(uv, "UV", mapping, "Vector")
    _link(mapping, "Vector", image, "Vector")
    material, _ = _material(source=(image, "Color"))
    assert (
        bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")
        is periodic
    )


def test_a_normal_chain_does_not_affect_a_colour_bake():
    material, principled = _material(source=(_image(), "Color"))
    noise = _Node("TEX_NOISE", inputs=("Vector",), outputs=("Fac",))
    bump = _Node("BUMP", inputs=("Height",), outputs=("Normal",))
    _link(noise, "Fac", bump, "Height")
    _link(bump, "Normal", principled, "Normal")
    assert bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")


def test_a_flat_material_is_periodic():
    material, _ = _material(source=None)
    assert bake_uv.material_bake_is_uv_periodic(material, bake_uv="UVMap", render_uv="UVMap")
