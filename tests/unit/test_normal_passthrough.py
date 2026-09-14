"""Unit tests for normal-map passthrough capture in the bake path.

The bake never renders a normal pass, so a source normal map must be carried
through onto the baked material rather than dropped (which left baked surfaces
flat and over-glossy). These tests pin the capture logic in
``_source_normal_passthrough``; the actual node wiring is verified end-to-end.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# bake_textures does ``import bpy`` at module scope; stub it for plain pytest.
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.bake_textures import (  # noqa: E402
    _source_metallic_passthrough,
    _source_normal_passthrough,
    _validate_bake_material_contract,
)


class FakeImage:
    def __init__(self, name):
        self.name = name


class FakeSocket:
    def __init__(
        self,
        *,
        is_linked=False,
        default_value=0.0,
        from_node=None,
        from_socket_name="Color",
    ):
        self.is_linked = is_linked
        self.default_value = default_value
        self.links = (
            [
                types.SimpleNamespace(
                    from_node=from_node,
                    from_socket=types.SimpleNamespace(name=from_socket_name),
                )
            ]
            if from_node
            else []
        )


class FakeInputs(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeNode:
    def __init__(
        self,
        node_type,
        *,
        image=None,
        uv_map="",
        inputs=None,
        convention="OPENGL",
        space="TANGENT",
        projection="FLAT",
        extension="REPEAT",
        interpolation="LINEAR",
    ):
        self.type = node_type
        self.image = image
        self.uv_map = uv_map
        self.inputs = FakeInputs(inputs or {})
        self.convention = convention
        self.space = space
        self.projection = projection
        self.extension = extension
        self.interpolation = interpolation


class FakeMaterial:

    def __init__(self, nodes, principled):
        self.node_tree = types.SimpleNamespace(nodes=nodes)
        self._principled = principled


def _material(principled_inputs, extra_nodes):
    principled = FakeNode('BSDF_PRINCIPLED', inputs=principled_inputs)
    output = FakeNode(
        'OUTPUT_MATERIAL',
        inputs={
            'Surface': FakeSocket(is_linked=True, from_node=principled),
        },
    )
    output.is_active_output = True
    return FakeMaterial([principled, *extra_nodes, output], principled)


def test_captures_image_via_normal_map_node():
    img = FakeImage("wood_normal.png")
    tex = FakeNode(
        'TEX_IMAGE',
        image=img,
        uv_map="UVMap",
        inputs={'Vector': FakeSocket(is_linked=False)},
    )
    nmap = FakeNode(
        'NORMAL_MAP',
        uv_map="UVMap",
        inputs={'Strength': FakeSocket(default_value=0.8), 'Color': FakeSocket(is_linked=True, from_node=tex)},
    )
    mat = _material({'Normal': FakeSocket(is_linked=True, from_node=nmap)}, [nmap, tex])

    result = _source_normal_passthrough(mat)
    assert result is not None
    assert result["image"] is img
    assert result["strength"] == 0.8
    assert result["uv_layer"] == "UVMap"


def test_rejects_image_wired_directly_to_normal_input():
    img = FakeImage("n.png")
    tex = FakeNode('TEX_IMAGE', image=img, uv_map="UV2", inputs={'Vector': FakeSocket()})
    mat = _material({'Normal': FakeSocket(is_linked=True, from_node=tex)}, [tex])

    with pytest.raises(RuntimeError, match="Normal Map"):
        _source_normal_passthrough(mat)


def test_none_when_normal_unlinked():
    mat = _material({'Normal': FakeSocket(is_linked=False)}, [])
    assert _source_normal_passthrough(mat) is None


def test_none_when_no_image_behind_normal_map():
    nmap = FakeNode('NORMAL_MAP', inputs={'Strength': FakeSocket(default_value=1.0), 'Color': FakeSocket(is_linked=False)})
    mat = _material({'Normal': FakeSocket(is_linked=True, from_node=nmap)}, [nmap])
    with pytest.raises(RuntimeError, match="direct Image Texture"):
        _source_normal_passthrough(mat)


@pytest.mark.parametrize(
    ("node_kwargs", "message"),
    [
        ({"convention": "DIRECTX"}, "DirectX"),
        ({"space": "OBJECT"}, "tangent space"),
    ],
)
def test_rejects_normal_map_semantics_that_rebuild_would_lose(node_kwargs, message):
    img = FakeImage("normal.png")
    tex = FakeNode('TEX_IMAGE', image=img, inputs={'Vector': FakeSocket()})
    nmap = FakeNode(
        'NORMAL_MAP',
        inputs={
            'Strength': FakeSocket(default_value=1.0),
            'Color': FakeSocket(is_linked=True, from_node=tex),
        },
        **node_kwargs,
    )
    mat = _material({'Normal': FakeSocket(is_linked=True, from_node=nmap)}, [nmap, tex])

    with pytest.raises(RuntimeError, match=message):
        _source_normal_passthrough(mat)


def test_rejects_linked_normal_strength_and_vector_mapping():
    mapping = FakeNode('MAPPING')
    img = FakeImage("normal.png")
    tex = FakeNode(
        'TEX_IMAGE',
        image=img,
        inputs={'Vector': FakeSocket(is_linked=True, from_node=mapping)},
    )
    nmap = FakeNode(
        'NORMAL_MAP',
        inputs={
            'Strength': FakeSocket(is_linked=True, from_node=FakeNode('VALUE')),
            'Color': FakeSocket(is_linked=True, from_node=tex),
        },
    )
    mat = _material({'Normal': FakeSocket(is_linked=True, from_node=nmap)}, [nmap, tex])

    with pytest.raises(RuntimeError, match="linked Normal Map Strength"):
        _source_normal_passthrough(mat)


def test_metallic_texture_is_captured():
    img = FakeImage("metal.png")
    tex = FakeNode(
        'TEX_IMAGE',
        image=img,
        uv_map="UVMap",
        inputs={'Vector': FakeSocket(is_linked=False)},
    )
    mat = _material({'Metallic': FakeSocket(is_linked=True, from_node=tex)}, [tex])
    result = _source_metallic_passthrough(mat)
    assert result is not None and result["image"] is img and result["uv_layer"] == "UVMap"


def test_metallic_nonzero_constant_is_captured():
    mat = _material({'Metallic': FakeSocket(is_linked=False, default_value=1.0)}, [])
    assert _source_metallic_passthrough(mat) == {"value": 1.0}


def test_metallic_zero_constant_is_skipped():
    # 0 equals the Principled default, so there is nothing to carry.
    mat = _material({'Metallic': FakeSocket(is_linked=False, default_value=0.0)}, [])
    assert _source_metallic_passthrough(mat) is None


def test_rejects_packed_or_procedural_metallic_chain_instead_of_defaulting_to_zero():
    separate = FakeNode('SEPARATE_COLOR')
    mat = _material({'Metallic': FakeSocket(is_linked=True, from_node=separate)}, [separate])

    with pytest.raises(RuntimeError, match="would become zero"):
        _source_metallic_passthrough(mat)


def test_rejects_alpha_channel_metallic_passthrough():
    img = FakeImage("orm.png")
    tex = FakeNode('TEX_IMAGE', image=img, inputs={'Vector': FakeSocket()})
    mat = _material(
        {
            'Metallic': FakeSocket(
                is_linked=True,
                from_node=tex,
                from_socket_name="Alpha",
            )
        },
        [tex],
    )

    with pytest.raises(RuntimeError, match="output 'Alpha'"):
        _source_metallic_passthrough(mat)


@pytest.mark.parametrize("bake_mode", ["LIT_ALBEDO", "LIT_IBL"])
def test_bake_contract_rejects_mix_shader_with_transparent_fallback(bake_mode):
    principled = FakeNode('BSDF_PRINCIPLED', inputs=FakeInputs())
    transparent = FakeNode('BSDF_TRANSPARENT')
    mix = FakeNode(
        'MIX_SHADER',
        inputs=FakeInputs(
            {
                'Shader': FakeSocket(is_linked=True, from_node=principled),
                'Shader_001': FakeSocket(is_linked=True, from_node=transparent),
            }
        ),
    )
    output = FakeNode(
        'OUTPUT_MATERIAL',
        inputs=FakeInputs({'Surface': FakeSocket(is_linked=True, from_node=mix)}),
    )
    output.is_active_output = True
    mat = FakeMaterial([output, mix, principled, transparent], principled)
    mat.name = "MixedTransparency"

    with pytest.raises(RuntimeError, match="Transparent BSDF"):
        _validate_bake_material_contract(mat, bake_mode=bake_mode)


def _direct_principled_material(name, principled_inputs):
    principled = FakeNode('BSDF_PRINCIPLED', inputs=FakeInputs(principled_inputs))
    output = FakeNode(
        'OUTPUT_MATERIAL',
        inputs=FakeInputs({'Surface': FakeSocket(is_linked=True, from_node=principled)}),
    )
    output.is_active_output = True
    material = FakeMaterial([output, principled], principled)
    material.name = name
    return material


@pytest.mark.parametrize(
    ("socket_name", "value"),
    [
        ("Specular Tint", (2.0, 2.0, 2.0, 1.0)),
        ("Specular IOR Level", 0.75),
        ("Coat Weight", 0.5),
        ("Emission Strength", 1.0),
    ],
)
def test_material_color_only_bake_rejects_active_unpreserved_controls(
    socket_name,
    value,
):
    inputs = {socket_name: FakeSocket(default_value=value)}
    if socket_name == "Emission Strength":
        inputs["Emission Color"] = FakeSocket(default_value=(1.0, 1.0, 1.0, 1.0))
    material = _direct_principled_material("UnpreservedControl", inputs)

    with pytest.raises(RuntimeError, match=socket_name):
        _validate_bake_material_contract(material, bake_mode="LIT_ALBEDO")


def test_material_color_only_bake_accepts_emission_with_a_black_colour():
    """Strength 1 with a black colour emits nothing, as older files keep it."""
    material = _direct_principled_material(
        "BlackEmission",
        {
            "Emission Strength": FakeSocket(default_value=1.0),
            "Emission Color": FakeSocket(default_value=(0.0, 0.0, 0.0, 1.0)),
        },
    )

    assert _validate_bake_material_contract(material, bake_mode="LIT_ALBEDO")["normal"] is None


def _group_surface_material(name, principled_inputs):
    """A material whose surface is a node group ending in one Principled BSDF."""
    principled = FakeNode('BSDF_PRINCIPLED', inputs=FakeInputs(principled_inputs))
    shader_link = types.SimpleNamespace(
        from_node=principled,
        from_socket=types.SimpleNamespace(name="BSDF", identifier="BSDF"),
    )
    group_output = types.SimpleNamespace(
        type="GROUP_OUTPUT",
        is_active_output=True,
        inputs=[types.SimpleNamespace(type="SHADER", identifier="Socket_0", links=[shader_link])],
    )
    group = FakeNode('GROUP')
    group.node_tree = types.SimpleNamespace(name="SH-Wood", nodes=[group_output, principled])
    output = FakeNode(
        'OUTPUT_MATERIAL',
        inputs=FakeInputs({'Surface': FakeSocket(is_linked=True, from_node=group)}),
    )
    output.is_active_output = True
    material = FakeMaterial([output, group], principled)
    material.name = name
    return material


@pytest.mark.parametrize("bake_mode", ["LIT_IBL", "UNLIT_ALBEDO", "LIT_ALBEDO"])
def test_bake_accepts_a_node_group_ending_in_an_opaque_principled(bake_mode):
    material = _group_surface_material("GroupWood", {"Alpha": FakeSocket(default_value=1.0)})

    assert _validate_bake_material_contract(material, bake_mode=bake_mode)["normal"] is None


def test_bake_refuses_a_node_group_whose_principled_is_transparent():
    material = _group_surface_material("GroupGlass", {"Alpha": FakeSocket(default_value=0.4)})

    with pytest.raises(RuntimeError, match="inside node group 'SH-Wood'"):
        _validate_bake_material_contract(material, bake_mode="LIT_IBL")


def test_lighting_and_shadows_bake_allows_lit_controls_to_be_flattened():
    material = _direct_principled_material(
        "LitFlattened",
        {"Specular Tint": FakeSocket(default_value=(2.0, 2.0, 2.0, 1.0))},
    )

    assert _validate_bake_material_contract(material, bake_mode="LIT_IBL") == {
        "normal": None,
        "metallic": None,
    }
