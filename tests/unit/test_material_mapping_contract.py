"""RealityKit one-texture-transform material contract regressions."""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdShade  # noqa: E402

from Plugin.export.materials.author import create_materialx_material  # noqa: E402
from Plugin.export.materials.graph import MaterialXGraphBuilder  # noqa: E402
from Plugin.export.materials.mapping import (  # noqa: E402
    effective_texture_mapping_contract,
    require_realitykit_mapping_contract,
)
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402


def _mapping(*, offset=(0.125, 0.25), scale=(0.5, 0.75)):
    return {
        "offset": offset,
        "scale": scale,
        "rotate": 0.2,
        "pivot": (0.0, 0.0),
        "operationorder": 0,
    }


def _pbr_graph(base_mapping, roughness_mapping):
    return MaterialXGraphBuilder(load_manifest()).build_pbr_material(
        {
            "base_color_texture": "textures/base.png",
            "base_color_texture_colorspace": "srgb",
            "base_color_texture_mapping": base_mapping,
            "roughness_texture": "textures/roughness.png",
            "roughness_texture_colorspace": "raw",
            "roughness_texture_channel": "r",
            "roughness_texture_mapping": roughness_mapping,
        }
    )


def _shader_ids(stage):
    return {
        str(prim.GetPath()): str(UsdShade.Shader(prim).GetIdAttr().Get() or "")
        for prim in stage.Traverse()
        if prim.IsA(UsdShade.Shader)
    }


def test_identity_mapping_contract_is_default_even_with_inert_pivot_and_order():
    assert effective_texture_mapping_contract(
        {
            "offset": (-0.0, 1.0e-10),
            "scale": (1.0, 1.0),
            "rotate": 0.0,
            "pivot": (0.75, 0.25),
            "operationorder": 1,
        },
        "UV1",
    ) is None


def test_identical_base_color_and_roughness_mappings_share_one_place2d():
    graph = _pbr_graph(_mapping(), dict(_mapping()))
    stage = Usd.Stage.CreateInMemory()

    create_materialx_material(
        stage,
        "/Material",
        "SharedMapping",
        graph,
        load_manifest(),
    )

    shader_ids = _shader_ids(stage)
    place_paths = [
        path for path, shader_id in shader_ids.items() if "place2d" in shader_id
    ]
    assert len(place_paths) == 1
    place_path = place_paths[0]
    image_shaders = [
        UsdShade.Shader(stage.GetPrimAtPath(path))
        for path, shader_id in shader_ids.items()
        if shader_id.startswith("ND_image_")
    ]
    assert len(image_shaders) == 2
    sources = []
    for image in image_shaders:
        result = image.GetInput("texcoord").GetConnectedSources()
        infos = result[0] if isinstance(result, tuple) else result
        assert len(infos) == 1
        sources.append(str(infos[0].source.GetPrim().GetPath()))
    assert sources == [place_path, place_path]


def test_distinct_base_color_and_roughness_mappings_fail_before_usd_mutation():
    graph = _pbr_graph(
        _mapping(offset=(0.125, 0.25)),
        _mapping(offset=(0.5, 0.25)),
    )
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    before = stage.GetRootLayer().ExportToString()

    with pytest.raises(ValueError, match="distinct non-default texture mappings"):
        create_materialx_material(
            stage,
            "/Root/Material",
            "DistinctMappings",
            graph,
            load_manifest(),
        )

    assert stage.GetRootLayer().ExportToString() == before
    assert not stage.GetPrimAtPath("/Root/Material")


def test_two_explicit_place2d_nodes_fail_closed_before_authoring():
    mapping_inputs = {
        "offset": (0.1, 0.2),
        "scale": (0.5, 0.5),
        "rotate": 10.0,
        "pivot": (0.0, 0.0),
        "operationorder": 0,
    }
    graph = {
        "nodes": [
            {
                "name": "TransformA",
                "node_id": "ND_place2d_vector2",
                "inputs": dict(mapping_inputs),
            },
            {
                "name": "TransformB",
                "node_id": "ND_place2d_vector2",
                "inputs": dict(mapping_inputs),
            },
        ],
        "connections": [],
        "output": "TransformA",
    }

    with pytest.raises(ValueError, match="2 explicit MaterialX place2d nodes"):
        require_realitykit_mapping_contract(graph, "ExplicitTransforms")


def test_explicit_place2d_and_generated_mapping_cannot_bypass_gate():
    graph = _pbr_graph(_mapping(), None)
    graph["nodes"].append(
        {
            "name": "ExplicitTransform",
            "node_id": "ND_place2d_vector2",
            "inputs": {
                # An identity transform still consumes RealityKit's first and
                # only transform slot, so it cannot hide ahead of this graph's
                # generated non-default Mapping.
                "offset": (0.0, 0.0),
                "scale": (1.0, 1.0),
                "rotate": 0.0,
            },
        }
    )

    with pytest.raises(ValueError, match="combines an explicit MaterialX place2d"):
        require_realitykit_mapping_contract(graph, "MixedTransforms")
