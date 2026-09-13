"""Unit tests for MaterialX texture authoring."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export.materials.textures import _create_texture_connection
from Plugin.export.usd_utils import PXR_AVAILABLE, Usd
from Plugin.manifest.materialx_nodes import load_manifest


pytestmark = pytest.mark.skipif(
    not PXR_AVAILABLE,
    reason="OpenUSD Python bindings are required for MaterialX USD authoring tests.",
)


def test_normal_texture_uses_realitykit_normal_map_decode():
    stage = Usd.Stage.CreateInMemory()
    manifest = load_manifest()

    output = _create_texture_connection(
        stage,
        "/Material0",
        "normal",
        {
            "path": "textures/normal.avif",
            "output_type": "vector3",
            "type": "normal_texture",
        },
        manifest,
        "Material0",
    )

    assert output is not None
    authored = stage.GetRootLayer().ExportToString()
    assert 'uniform token info:id = "ND_normal_map_decode"' in authored
    assert 'uniform token info:id = "ND_normalmap"' not in authored
    assert "inputs:space" not in authored
    assert "inputs:scale" not in authored


def _normal_connection(stage, manifest, **spec):
    return _create_texture_connection(
        stage,
        "/Material0",
        "normal",
        {
            "path": "textures/normal.avif",
            "output_type": "vector3",
            "type": "normal_texture",
            **spec,
        },
        manifest,
        "Material0",
    )


def test_non_default_strength_stays_on_the_tangent_space_decode():
    """Strength used to force the ND_normalmap fallback, which returns a
    world-space normal into a tangent-space input. It is expressed in tangent
    space after RealityKit's decode; its values are checked against Cycles'
    bake in test_normal_map_exact."""
    stage = Usd.Stage.CreateInMemory()

    assert _normal_connection(stage, load_manifest(), scale=0.5) is not None
    authored = stage.GetRootLayer().ExportToString()
    assert 'uniform token info:id = "ND_normal_map_decode"' in authored
    assert 'uniform token info:id = "ND_normalmap"' not in authored
    assert 'uniform token info:id = "ND_normalize_vector3"' in authored


def test_non_tangent_space_is_refused_rather_than_decoded_in_the_wrong_basis():
    """No node RealityKit resolves can carry an object- or world-space normal
    map into the surface's tangent-space input, so there is nothing to author."""
    for space in ("object", "world"):
        stage = Usd.Stage.CreateInMemory()
        with pytest.raises(ValueError, match="cannot be represented"):
            _normal_connection(stage, load_manifest(), scale=0.5, space=space)


def test_alpha_of_a_file_without_alpha_reads_one_as_blender_does():
    """Cycles fills a three-channel file's alpha with 1. Leaving the input at
    its nodedef default gave a roughness read of that alpha 0.5, not 1."""
    from Plugin.export.materials.author import create_materialx_material
    from Plugin.export.materials.graph import RCP3_PBR2_NODEDEF
    from Plugin.export.usd_utils import UsdShade

    graph = {
        "nodes": [{
            "name": "surface",
            "node_id": RCP3_PBR2_NODEDEF,
            "inputs": {
                "roughness": {
                    "type": "texture", "path": "textures/rgb.png", "output_type": "float",
                    "channel": "a", "colorspace_role": "data", "source_channels": 3,
                    "source_has_alpha": False,
                },
            },
        }],
        "connections": [],
        "output": "surface",
    }
    stage = Usd.Stage.CreateInMemory()
    create_materialx_material(stage, "/Material0", "Material0", graph, load_manifest())
    shader = UsdShade.Shader(stage.GetPrimAtPath("/Material0/surface"))
    roughness = shader.GetInput("roughness")
    assert not roughness.HasConnectedSource()
    assert float(roughness.Get()) == 1.0
    assert "ND_image_" not in stage.GetRootLayer().ExportToString()
