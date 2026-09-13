"""Blender 5.2 opacity and explicit alpha-cutout contract regressions."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.graph import MaterialXGraphBuilder
from Plugin.export import bake_textures
from Plugin.export.materials.extract.core import (
    extract_blender_material_data,
    material_has_transparency,
    opacity_threshold_from_material,
)
from Plugin.manifest.materialx_nodes import load_manifest


class _Socket:
    def __init__(self, default_value=None):
        self.default_value = default_value
        self.links = []
        self.is_linked = False


class _Node:
    def __init__(self, node_type, *, inputs=None, active=False):
        self.type = node_type
        self.inputs = inputs or {}
        self.is_active_output = active


class _Blender52Material(dict):
    def __init__(self, *, surface_render_method="DITHERED"):
        super().__init__()
        self.name = "AlphaMaterial"
        self.surface_render_method = surface_render_method
        self.node_tree = None

    @property
    def blend_method(self):  # pragma: no cover - any access is the failure
        raise AssertionError("Blender 5.2 export must not read blend_method")

    def __bool__(self):
        return True


def _link(from_node, to_socket):
    link = types.SimpleNamespace(from_node=from_node)
    to_socket.links.append(link)
    to_socket.is_linked = True


def _material_with_active_alpha(
    active_alpha,
    *,
    disconnected_alpha=None,
    surface_render_method="DITHERED",
):
    active = _Node('BSDF_PRINCIPLED', inputs={'Alpha': _Socket(active_alpha)})
    active_surface = _Socket()
    _link(active, active_surface)
    active_output = _Node(
        'OUTPUT_MATERIAL',
        inputs={'Surface': active_surface},
        active=True,
    )

    nodes = []
    if disconnected_alpha is not None:
        disconnected = _Node(
            'BSDF_PRINCIPLED',
            inputs={'Alpha': _Socket(disconnected_alpha)},
        )
        inactive_surface = _Socket()
        _link(disconnected, inactive_surface)
        nodes.extend(
            [
                disconnected,
                _Node(
                    'OUTPUT_MATERIAL',
                    inputs={'Surface': inactive_surface},
                    active=False,
                ),
            ]
        )
    nodes.extend([active, active_output])

    material = _Blender52Material(
        surface_render_method=surface_render_method,
    )
    material.node_tree = types.SimpleNamespace(nodes=nodes)
    return material


def _builder() -> MaterialXGraphBuilder:
    return MaterialXGraphBuilder(load_manifest())


def test_opaque_material_authors_no_opacity():
    builder = _builder()
    material_data = {
        "name": "Opaque",
        "type": "principled",
        "base_color": [0.349, 0.220, 0.125],
        "alpha": 1.0,
        "is_transparent": False,
    }

    pbr = builder._map_realitykit_pbr2_inputs(material_data)
    unlit = builder._map_unlit_inputs(material_data)

    assert "opacity" not in pbr
    assert "opacity" not in unlit


def test_transparent_material_authors_opacity():
    builder = _builder()
    material_data = {
        "name": "Glass",
        "type": "principled",
        "base_color": [0.8, 0.8, 0.8],
        "alpha": 0.4,
        "is_transparent": True,
    }

    pbr = builder._map_realitykit_pbr2_inputs(material_data)
    unlit = builder._map_unlit_inputs(material_data)

    assert pbr["opacity"] == 0.4
    assert unlit["opacity"] == 0.4


def test_force_unlit_never_scales_base_color_by_emission_strength():
    """Cycles' principled_bsdf_emission multiplies Emission Color by Emission
    Strength and nothing else, so the unlit colour a Principled BSDF's Base
    Color gives is the same whether it is a texture or a constant."""
    builder = _builder()
    textured = builder._map_unlit_inputs(
        {"name": "Glow", "type": "principled", "base_color_texture": "textures/base.png", "emission_strength": 4.0}
    )
    constant = builder._map_unlit_inputs(
        {"name": "Glow", "type": "principled", "base_color": [0.5, 0.25, 0.125], "emission_strength": 4.0}
    )
    assert "scale" not in textured["color"]
    assert constant["color"] == [0.5, 0.25, 0.125]


def test_missing_flag_defaults_to_opaque():
    """Material data without ``is_transparent`` must not author opacity."""
    builder = _builder()
    material_data = {
        "name": "NoFlag",
        "type": "principled",
        "base_color": [0.5, 0.5, 0.5],
        "alpha": 1.0,
    }

    assert "opacity" not in builder._map_realitykit_pbr2_inputs(material_data)


@pytest.mark.parametrize("surface_render_method", ["DITHERED", "BLENDED"])
def test_blender52_render_method_never_implies_opacity_threshold(
    surface_render_method,
):
    material = _material_with_active_alpha(
        0.4,
        surface_render_method=surface_render_method,
    )

    data = extract_blender_material_data(material)

    assert data["surface_render_method"] == surface_render_method
    assert data["is_transparent"] is True
    assert data["alpha"] == pytest.approx(0.4)
    assert "alpha_threshold" not in data
    assert opacity_threshold_from_material(material, True) is None


def test_explicit_numeric_cutout_contract_authors_threshold():
    material = _material_with_active_alpha(0.4)
    material["usd_stage_alpha_cutout_threshold"] = 0.375

    data = extract_blender_material_data(material)

    assert opacity_threshold_from_material(material, True) == pytest.approx(0.375)
    assert data["alpha_threshold"] == pytest.approx(0.375)


@pytest.mark.parametrize("value", [True, False, -0.1, 1.1, float("nan"), "cutout"])
def test_incomplete_or_invalid_cutout_contract_is_ignored(value):
    material = _material_with_active_alpha(0.4)
    material["usd_stage_alpha_cutout_threshold"] = value

    assert opacity_threshold_from_material(material, True) is None


def test_inactive_principled_alpha_does_not_trigger_opacity_bake():
    material = _material_with_active_alpha(1.0, disconnected_alpha=0.2)

    assert material_has_transparency(material) is False
    assert bake_textures._material_needs_opacity(material) is False
    data = extract_blender_material_data(material)
    assert data["alpha"] == pytest.approx(1.0)
    assert data["is_transparent"] is False


def test_active_second_principled_drives_transparency_and_extraction():
    material = _material_with_active_alpha(0.3, disconnected_alpha=1.0)

    assert material_has_transparency(material) is True
    assert bake_textures._material_needs_opacity(material) is True
    data = extract_blender_material_data(material)
    assert data["alpha"] == pytest.approx(0.3)
    assert data["is_transparent"] is True
