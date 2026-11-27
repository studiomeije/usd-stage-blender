"""The AO source is declared by property or label, never inferred from names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from Plugin.export.materials.extract.occlusion import (
    AmbientOcclusionError,
    ambient_occlusion_refusal,
    ambient_occlusion_source,
)


class _Material(dict):
    def __init__(self, nodes, **props):
        super().__init__(props)
        self.node_tree = SimpleNamespace(nodes=nodes)


def _node(name, label="", outputs=("Color", "Alpha")):
    node = SimpleNamespace(name=name, label=label, inputs=[])
    node.outputs = [SimpleNamespace(name=o, enabled=True, node=node) for o in outputs]
    return node


def test_a_node_name_containing_ao_is_not_a_source():
    assert ambient_occlusion_source(_Material([_node("Chaos AO"), _node("Baked_AO")])) is None


def test_the_property_names_a_node_and_output():
    split = _node("ORM Split", outputs=("Red", "Green", "Blue"))
    node, socket = ambient_occlusion_source(
        _Material([split], usd_stage_ambient_occlusion="ORM Split:Red")
    )
    assert (node, socket.name) == (split, "Red")


def test_the_property_wins_over_a_label():
    labelled = _node("Image", label="Ambient Occlusion")
    named = _node("Occlusion Map")
    node, socket = ambient_occlusion_source(
        _Material([labelled, named], usd_stage_ambient_occlusion="Occlusion Map")
    )
    assert node is named and socket.name == "Color"


def test_a_label_is_a_source():
    labelled = _node("Image.001", label="Ambient Occlusion")
    assert ambient_occlusion_source(_Material([labelled]))[0] is labelled


@pytest.mark.parametrize(
    "material,message",
    [
        (_Material([_node("A", "Ambient Occlusion"), _node("B", "Ambient Occlusion")]), "all labelled"),
        (_Material([_node("A")], usd_stage_ambient_occlusion="B"), "no node with that name"),
        (_Material([_node("A")], usd_stage_ambient_occlusion="A:Red"), "does not have"),
        (_Material([_node("A")], usd_stage_ambient_occlusion=3), "must be a node name"),
    ],
)
def test_a_broken_declaration_is_refused(material, message):
    with pytest.raises(AmbientOcclusionError, match=message):
        ambient_occlusion_source(material)
    assert message in ambient_occlusion_refusal(material)
