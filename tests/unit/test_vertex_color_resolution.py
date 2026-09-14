"""Color Attribute (VERTEX_COLOR) -> ND_geomcolor_color4 translation.

Blender 5.2's Color Attribute node (bl_idname ShaderNodeVertexColor, node
type VERTEX_COLOR) reads a mesh color attribute; Blender's USD exporter
writes that attribute as ``primvars:<attribute name>``. Measured on Blender
5.2.0 LTS by exporting one cube per case and reading the primvar back with
pxr:

===================  ====================  =================
Blender attribute    USD primvar type      interpolation
===================  ====================  =================
FLOAT_COLOR/CORNER   ``color4f[]``         ``faceVarying``
FLOAT_COLOR/POINT    ``color4f[]``         ``vertex``
BYTE_COLOR/CORNER    ``color4f[]``         ``faceVarying``
BYTE_COLOR/POINT     ``color4f[]``         ``vertex``
===================  ====================  =================

The domain only picks the interpolation; the value type is four-channel in
every case. The exact translation is therefore a *color4* geomcolor read
of the same name, adapted to whatever the consumer needs with a
convert/dotproduct chain. A color3 read of a color4f primvar is a type
mismatch, and Reality Composer Pro 3.0 replaces the whole material with its
striped placeholder rather than erroring visibly.

Refused rather than approximated: an unnamed attribute, a name that is not a
valid USD identifier, and a name no mesh using the material carries (the read
would dangle).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export import realitykit_preflight as preflight  # noqa: E402
from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402


_MANIFEST = load_manifest()
_MANIFEST_NODES = frozenset(_MANIFEST["nodes"].keys())


def _authored_node_ids(expr):
    """Every nodedef id an expression tree authors."""
    if not isinstance(expr, dict) or expr.get("kind") != "node":
        return []
    ids = [expr["node_id"]]
    for child in (expr.get("inputs") or {}).values():
        ids.extend(_authored_node_ids(child))
    return ids


def _geomcolor_read(expr):
    """The geompropvalue node at the root of an expression tree."""
    for node_id in _authored_node_ids(expr):
        assert any(
            token in node_id
            for token in ("geomcolor", "convert", "dotproduct", "luminance")
        ), node_id
    node = expr
    while isinstance(node, dict) and node.get("kind") == "node":
        if node["node_id"].startswith("ND_geomcolor"):
            return node
        inputs = node.get("inputs") or {}
        node = inputs.get("in") or inputs.get("in1")
    raise AssertionError(f"no geomcolor read in {expr}")


def _assert_manifest_backed(expr):
    ids = _authored_node_ids(expr)
    assert ids, "expression authors no nodes"
    for node_id in ids:
        node_def = _MANIFEST["nodes"].get(node_id)
        assert node_def is not None, f"{node_id} is not in the manifest"
        assert not (node_def.get("policy") or {}).get("editor_unresolvable"), (
            f"{node_id} is flagged editor_unresolvable"
        )


class _Socket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name


class _Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _Node:
    pass


def _vertex_color_node(layer_name="MyColors"):
    node = _Node()
    node.type = "VERTEX_COLOR"
    node.name = "Color Attribute"
    node.layer_name = layer_name
    node.inputs = {}
    return node


def _resolve(node, output_name="Color", expected_type="color3", channel=None):
    output = _Socket(name=output_name)
    target = _Socket(linked=True, link=_Link(node, output))
    return core._resolve_socket_value(
        target, channel=channel, expected_type=expected_type
    )


class _FakeColorAttribute:
    """The subset of ``bpy.types.Attribute`` the type probe reads."""

    def __init__(self, name, data_type, domain):
        self.name = name
        self.data_type = data_type
        self.domain = domain


class _FakeColorAttributes:
    def __init__(self, attributes):
        self._attributes = {attr.name: attr for attr in attributes}

    def get(self, name, default=None):
        return self._attributes.get(name, default)


class _FakeMesh:
    def __init__(self, materials, attributes):
        self.materials = materials
        self.color_attributes = _FakeColorAttributes(attributes)


class _FakeMaterial:
    def __init__(self, node_tree):
        self.node_tree = node_tree


def _blender_scene(monkeypatch, node, *, attributes):
    """Give the node an owning material and mesh inside a fake bpy session.

    ``_color_attribute_primvar_type`` walks bpy.data to find a mesh using the
    node's material and reads the named attribute's ``data_type``; outside a
    live Blender session it cannot, so the four attribute-type/domain
    combinations can only be exercised through a double of that walk.
    """
    tree = object()
    node.id_data = tree
    material = _FakeMaterial(tree)
    mesh = _FakeMesh([material], attributes)
    monkeypatch.setattr(
        _bpy_stub,
        "data",
        types.SimpleNamespace(meshes=[mesh], materials=[material]),
        raising=False,
    )
    return node


def test_float_color_corner_reads_the_type_blender_writes(monkeypatch):
    """The reproduce-first case: FLOAT_COLOR/CORNER is a color4f primvar.

    Measured export of a cube carrying a FLOAT_COLOR/CORNER attribute:
    ``primvars:Paint  color4f[]  interpolation=faceVarying  count=24``. A
    ND_geomcolor_color3 read of that primvar is the type mismatch RCP 3.0
    replaces with its striped placeholder.
    """
    node = _blender_scene(
        monkeypatch,
        _vertex_color_node("Paint"),
        attributes=[_FakeColorAttribute("Paint", "FLOAT_COLOR", "CORNER")],
    )
    assert core._color_attribute_primvar_type(node, "Paint") == "color4"

    expr = _resolve(node)
    read = _geomcolor_read(expr)
    assert read["node_id"] == "ND_geomcolor_color4"
    assert read["inputs"]["index"] == {"kind": "constant", "value": 0}
    _assert_manifest_backed(expr)


@pytest.mark.parametrize(
    "data_type,domain",
    [
        ("FLOAT_COLOR", "CORNER"),
        ("FLOAT_COLOR", "POINT"),
        ("BYTE_COLOR", "CORNER"),
        ("BYTE_COLOR", "POINT"),
    ],
)
def test_every_attribute_type_and_domain_reads_color4(
    monkeypatch, data_type, domain
):
    """All four combinations export as ``color4f[]``; only interpolation moves.

    Measured on Blender 5.2.0 LTS: CORNER gives faceVarying, POINT gives
    vertex, and every case is four-channel. Nothing may select a color3 read.
    """
    node = _blender_scene(
        monkeypatch,
        _vertex_color_node("MyColors"),
        attributes=[_FakeColorAttribute("MyColors", data_type, domain)],
    )
    assert core._color_attribute_primvar_type(node, "MyColors") == "color4"
    assert _geomcolor_read(_resolve(node))["node_id"] == (
        "ND_geomcolor_color4"
    )


def test_unknown_attribute_type_still_reads_color4(monkeypatch):
    """An attribute type we have not measured reads with what Blender writes.

    Guessing color3 is the failure this fix exists to remove, so an
    unrecognised data type keeps the four-channel read rather than narrowing.
    """
    node = _blender_scene(
        monkeypatch,
        _vertex_color_node("MyColors"),
        attributes=[_FakeColorAttribute("MyColors", "SOME_FUTURE_TYPE", "CORNER")],
    )
    assert core._color_attribute_primvar_type(node, "MyColors") == "color4"


def test_color_attribute_authors_geomcolor_read():
    expr = _resolve(_vertex_color_node("MyColors"))
    read = _geomcolor_read(expr)
    assert read["kind"] == "node"
    assert read["node_id"] == "ND_geomcolor_color4"
    assert read["node_id"] in _MANIFEST_NODES
    assert read["inputs"]["index"] == {"kind": "constant", "value": 0}


def test_color3_consumer_extracts_rgb_from_the_color4_read():
    """A color3 base colour adapts with an implemented convert node.

    The read matches the primvar; the consumer's narrower type is reached by
    an explicit extraction node, never by declaring the read narrower than
    the data.
    """
    expr = _resolve(_vertex_color_node("MyColors"), expected_type="color3")
    assert expr["node_id"] == "ND_convert_color4_color3"
    assert expr["inputs"]["in"]["node_id"] == "ND_geomcolor_color4"
    _assert_manifest_backed(expr)

    signature = _MANIFEST["nodes"]["ND_convert_color4_color3"]["signature"]
    assert signature == "in[in:color4]|out[out:color3]"


def test_float_consumer_gets_blender_luminance_conversion():
    expr = _resolve(_vertex_color_node("MyColors"), expected_type="float")
    assert expr["node_id"] == "ND_dotproduct_vector3"
    # Cycles' linear_rgb_to_gray weights, measured by a Cycles render; not
    # ND_luminance, whose default coefficients are ACEScg's.
    assert expr["inputs"]["in2"]["value"] == pytest.approx((0.21263909, 0.71516913, 0.07219274), abs=1e-7)
    assert expr["inputs"]["in1"]["node_id"] == "ND_convert_color3_vector3"
    rgb = expr["inputs"]["in1"]["inputs"]["in"]
    assert rgb["node_id"] == "ND_convert_color4_color3"
    assert rgb["inputs"]["in"]["node_id"] == "ND_geomcolor_color4"
    _assert_manifest_backed(expr)


def test_channel_consumer_gets_a_component_read():
    expr = _resolve(
        _vertex_color_node("MyColors"), expected_type="float", channel="g"
    )
    # A component read is a dot product with a unit mask - `swizzle` resolves
    # in RealityKit but has no Metal implementation, so it produces a material
    # with no compiled shader.
    assert expr["node_id"] == "ND_dotproduct_vector4"
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": (0.0, 1.0, 0.0, 0.0)}
    convert = expr["inputs"]["in1"]
    assert convert["node_id"] == "ND_convert_color4_vector4"
    assert convert["inputs"]["in"]["node_id"] == "ND_geomcolor_color4"
    _assert_manifest_backed(expr)


def test_authored_ids_are_never_the_four_channel_nodes_rcp_rejects():
    """The color4 read must not drag in a vector4 node RCP cannot build."""
    for kwargs in (
        {"expected_type": "color3"},
        {"expected_type": "float"},
        {"expected_type": "float", "channel": "b"},
        {"output_name": "Alpha", "expected_type": "float"},
    ):
        expr = _resolve(_vertex_color_node("MyColors"), **kwargs)
        authored = set(_authored_node_ids(expr))
        assert not (authored & preflight.RCP_UNSUPPORTED_NODEDEFS), authored


def test_unnamed_attribute_is_refused():
    expr = _resolve(_vertex_color_node(""))
    assert expr["kind"] == "unresolved"
    assert "names no color attribute" in expr["reason"]


def test_invalid_primvar_identifier_is_refused():
    expr = _resolve(_vertex_color_node("My Colors"))
    assert expr["kind"] == "unresolved"
    assert "not a valid USD primvar identifier" in expr["reason"]


def test_alpha_output_reads_the_fourth_channel():
    """The Alpha output is exact now that the read is four-channel.

    It was refused while the read was ND_geomcolor_color3, which has no
    fourth channel to name. The primvar Blender writes is color4f, so alpha
    is a plain component read of the same read - nothing is approximated.
    """
    expr = _resolve(
        _vertex_color_node("MyColors"), output_name="Alpha", expected_type="float"
    )
    assert expr["node_id"] == "ND_dotproduct_vector4"
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": (0.0, 0.0, 0.0, 1.0)}
    convert = expr["inputs"]["in1"]
    assert convert["node_id"] == "ND_convert_color4_vector4"
    assert convert["inputs"]["in"]["node_id"] == "ND_geomcolor_color4"
    _assert_manifest_backed(expr)


def test_alpha_output_into_a_colour_input_broadcasts():
    """Blender broadcasts a scalar into a colour socket; so must the export."""
    expr = _resolve(
        _vertex_color_node("MyColors"), output_name="Alpha", expected_type="color3"
    )
    assert expr["node_id"] == "ND_convert_float_color3"
    assert expr["inputs"]["in"]["node_id"] == "ND_dotproduct_vector4"
    _assert_manifest_backed(expr)


def test_alpha_output_still_refuses_the_same_bad_names():
    """Naming gates apply to the Alpha output exactly as to the Color one."""
    expr = _resolve(_vertex_color_node(""), output_name="Alpha")
    assert expr["kind"] == "unresolved"
    assert "names no color attribute" in expr["reason"]

    expr = _resolve(_vertex_color_node("My Colors"), output_name="Alpha")
    assert expr["kind"] == "unresolved"
    assert "not a valid USD primvar identifier" in expr["reason"]


def test_attribute_missing_from_meshes_is_refused(monkeypatch):
    monkeypatch.setattr(
        core, "_color_attribute_primvar_type", lambda node, name: None
    )
    expr = _resolve(_vertex_color_node("MyColors"))
    assert expr["kind"] == "unresolved"
    assert "primvars:MyColors" in expr["reason"]


def test_attribute_missing_from_meshes_refuses_the_alpha_output(monkeypatch):
    monkeypatch.setattr(
        core, "_color_attribute_primvar_type", lambda node, name: None
    )
    expr = _resolve(_vertex_color_node("MyColors"), output_name="Alpha")
    assert expr["kind"] == "unresolved"
    assert "primvars:MyColors" in expr["reason"]


def test_a_mesh_without_the_attribute_does_not_satisfy_the_guard(monkeypatch):
    """The fail-closed guard keeps working through the type probe."""
    node = _blender_scene(
        monkeypatch,
        _vertex_color_node("MyColors"),
        attributes=[_FakeColorAttribute("OtherColors", "BYTE_COLOR", "CORNER")],
    )
    assert core._color_attribute_primvar_type(node, "MyColors") is None
    assert core._color_attribute_reaches_export(node, "MyColors") is False
    expr = _resolve(node)
    assert expr["kind"] == "unresolved"
    assert "primvars:MyColors" in expr["reason"]


def test_validator_moved_vertex_color_to_supported():
    assert "VERTEX_COLOR" in validate.SUPPORTED_TYPES
    assert "VERTEX_COLOR" not in validate.UNSUPPORTED_TYPES


def test_validator_flags_the_same_refusals(monkeypatch):
    node = _vertex_color_node("")
    node.outputs = {}
    issues = validate._vertex_color_issues(node)
    assert any("names no color attribute" in issue for issue in issues)

    node = _vertex_color_node("My Colors")
    node.outputs = {}
    issues = validate._vertex_color_issues(node)
    assert any("not a valid USD primvar identifier" in issue for issue in issues)

    monkeypatch.setattr(
        core, "_color_attribute_reaches_export", lambda node, name: False
    )
    node = _vertex_color_node("MyColors")
    node.outputs = {}
    issues = validate._vertex_color_issues(node)
    assert any("primvars:MyColors" in issue for issue in issues)


def test_validator_no_longer_flags_a_linked_alpha_output(monkeypatch):
    """The validator must not predict a refusal the exporter no longer makes."""
    node = _blender_scene(
        monkeypatch,
        _vertex_color_node("MyColors"),
        attributes=[_FakeColorAttribute("MyColors", "FLOAT_COLOR", "CORNER")],
    )
    node.outputs = {"Alpha": _Socket(linked=True, name="Alpha")}
    assert validate._vertex_color_issues(node) == []
