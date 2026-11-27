"""Every authored texture node gets its own name.

Two Separate Color nodes on two images, each feeding the same input name of
its own graph node (a scan's roughness and a tile-detail roughness, each into
a Math node's first input), used to author both channel reads at one prim
path, ``channel_in1_g``. USD's ``DefinePrim`` returns the existing prim, so
the second read rewired the first: both Math nodes then read the tile map.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdShade  # noqa: E402

from Plugin.export.materials.author import create_materialx_material  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402


def _texture(path, channel, **extra):
    spec = {
        "type": "texture",
        "path": path,
        "output_type": "float",
        "channel": channel,
        "colorspace": "raw",
        "colorspace_role": "data",
    }
    spec.update(extra)
    return spec


def _graph(first, second):
    return {
        "nodes": [
            {"name": "surface", "node_id": "ND_realitykit_pbr_surfaceshader_2_0", "inputs": {}},
            {"name": "scan", "node_id": "ND_multiply_float", "inputs": {"in1": first, "in2": 1.0}},
            {"name": "tile", "node_id": "ND_multiply_float", "inputs": {"in1": second, "in2": 1.0}},
            {"name": "sum", "node_id": "ND_add_float", "inputs": {}},
        ],
        "connections": [
            {"from_node": "scan", "to_node": "sum", "to_input": "in1"},
            {"from_node": "tile", "to_node": "sum", "to_input": "in2"},
            {"from_node": "sum", "to_node": "surface", "to_input": "roughness"},
        ],
        "output": "surface",
        "materialx_version": "1.39",
    }


def _read_file(stage, output_attr):
    """The texture file a float input's chain finally reads."""
    seen = output_attr
    while True:
        prim = seen.GetPrim()
        shader = UsdShade.Shader(prim)
        file_input = shader.GetInput("file")
        if file_input:
            return file_input.Get().path
        sources = []
        for shader_input in shader.GetInputs():
            sources += [s.source for s in shader_input.GetConnectedSources()[0]]
        assert len(sources) == 1, prim.GetPath()
        seen = sources[0]


def _input_file(stage, node, name):
    shader = UsdShade.Shader(stage.GetPrimAtPath(f"/Root/M/{node}"))
    (source,) = shader.GetInput(name).GetConnectedSources()[0]
    return _read_file(stage, source.source)


@pytest.mark.parametrize(
    "first,second",
    [
        (_texture("textures/scan.png", "g"), _texture("textures/tile.png", "g")),
        (_texture("textures/scan.png", "g", scale=0.5), _texture("textures/tile.png", "g", scale=0.8)),
        (_texture("textures/scan.png", ""), _texture("textures/tile.png", "")),
    ],
    ids=["channel", "scaled-channel", "gray"],
)
def test_two_reads_with_one_input_name_keep_their_own_files(first, second):
    stage = Usd.Stage.CreateInMemory()
    stage.SetDefaultPrim(stage.DefinePrim("/Root", "Xform"))

    create_materialx_material(stage, "/Root/M", "M", _graph(first, second), load_manifest())

    assert _input_file(stage, "scan", "in1") == "textures/scan.png"
    assert _input_file(stage, "tile", "in1") == "textures/tile.png"


def test_two_channels_of_one_file_still_share_its_reader():
    stage = Usd.Stage.CreateInMemory()
    stage.SetDefaultPrim(stage.DefinePrim("/Root", "Xform"))
    graph = _graph(_texture("textures/orm.png", "g"), _texture("textures/orm.png", "b"))

    create_materialx_material(stage, "/Root/M", "M", graph, load_manifest())

    readers = [p for p in stage.Traverse() if str(UsdShade.Shader(p).GetIdAttr().Get()).startswith("ND_image")]
    assert len(readers) == 1
