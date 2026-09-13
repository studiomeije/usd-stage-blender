"""The Material Output's Displacement socket as RealityKit's geometry modifier.

The offset is evaluated numerically against Blender's Displacement formula,
``normal * (height - midlevel) * scale`` in object space, and the material is
authored to USD to check the ``realitykit:vertex`` terminal lands beside the
surface. Refusals are checked by message.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.export.materials.graph import MaterialXGraphBuilder  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

_MANIFEST = load_manifest()
NORMAL = (0.0, 0.6, 0.8)  # what the object-space normal reader returns in the evaluator


class _Socket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value", socket_type="VALUE"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name
        self.type = socket_type
        self.id_data = None

    def path_from_id(self, prop):
        return f"socket.{prop}"


class _Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _Node:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _Sockets(dict):
    def __iter__(self):
        return iter(self.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _displacement(height=1.0, midlevel=0.0, scale=0.1, *, space="OBJECT", normal_linked=False, height_link=None):
    node = _Node(type="DISPLACEMENT", name="Displacement", space=space)
    node.inputs = _Sockets(
        Height=_Socket(height, name="Height", linked=height_link is not None, link=height_link),
        Midlevel=_Socket(midlevel, name="Midlevel"),
        Scale=_Socket(scale, name="Scale"),
        Normal=_Socket((0.0, 0.0, 0.0), name="Normal", socket_type="VECTOR",
                       linked=normal_linked, link=_Link(_Node(type="NORMAL_MAP", name="NM"), _Socket(name="Normal")) if normal_linked else None),
    )
    node.outputs = _Sockets(Displacement=_Socket(name="Displacement", socket_type="VECTOR"))
    return node


def _vector_displacement(vector=(0.2, 0.0, 0.5, 1.0), midlevel=0.0, scale=0.5, *, space="OBJECT"):
    node = _Node(type="VECTOR_DISPLACEMENT", name="Vector Displacement", space=space)
    node.inputs = _Sockets(
        Vector=_Socket(vector, name="Vector", socket_type="RGBA"),
        Midlevel=_Socket(midlevel, name="Midlevel"),
        Scale=_Socket(scale, name="Scale"),
    )
    node.outputs = _Sockets(Displacement=_Socket(name="Displacement", socket_type="VECTOR"))
    return node


def _material(displacement_node, method="DISPLACEMENT", extra_nodes=()):
    principled = _Node(type="BSDF_PRINCIPLED", name="Principled BSDF")
    principled.inputs = _Sockets(**{
        name: _Socket(value, name=name, socket_type=stype) for name, value, stype in [
            ("Base Color", (0.8, 0.8, 0.8, 1.0), "RGBA"), ("Metallic", 0.0, "VALUE"), ("Roughness", 0.5, "VALUE"),
            ("IOR", 1.45, "VALUE"), ("Alpha", 1.0, "VALUE"),
        ]
    })
    principled.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    output = _Node(type="OUTPUT_MATERIAL", name="Material Output", is_active_output=True)
    output.inputs = _Sockets(
        Surface=_Socket(linked=True, link=_Link(principled, principled.outputs["BSDF"]), name="Surface", socket_type="SHADER"),
        Volume=_Socket(name="Volume", socket_type="SHADER"),
        Displacement=_Socket(
            (0.0, 0.0, 0.0), name="Displacement", socket_type="VECTOR",
            linked=displacement_node is not None,
            link=_Link(displacement_node, displacement_node.outputs["Displacement"]) if displacement_node else None,
        ),
    )
    nodes = [output, principled] + ([displacement_node] if displacement_node else []) + list(extra_nodes)
    tree = SimpleNamespace(nodes=_Sockets({n.name: n for n in nodes}), links=[], animation_data=None)
    return SimpleNamespace(name="Displaced", node_tree=tree, displacement_method=method,
                           surface_render_method="DITHERED", blend_method="OPAQUE", diffuse_color=(1, 1, 1, 1))


# --- evaluator over the authored tree --------------------------------------

def _ev(expr):
    kind = expr.get("kind")
    if kind == "constant":
        v = expr["value"]
        return tuple(float(c) for c in v) if isinstance(v, (list, tuple)) else float(v)
    assert kind == "node", expr
    nid = expr["node_id"]
    assert nid in _MANIFEST["nodes"], nid
    ins = {k: _ev(v) for k, v in expr["inputs"].items() if k != "space"}
    op = nid[3:].rsplit("_", 1)[0]
    if op == "normal":
        assert expr["inputs"]["space"] == {"kind": "constant", "value": "object"}
        return NORMAL
    if op == "combine3":
        return (ins["in1"], ins["in2"], ins["in3"])
    a, b = ins["in1"], ins["in2"]
    f = {"multiply": lambda x, y: x * y, "subtract": lambda x, y: x - y, "add": lambda x, y: x + y}[op]
    if isinstance(a, tuple):
        return tuple(f(x, y) for x, y in zip(a, b))
    return f(a, b)


@pytest.mark.parametrize("height, midlevel, scale", [(1.0, 0.0, 0.1), (0.25, 0.5, 2.0), (0.0, 0.5, 0.01)])
def test_displacement_is_normal_times_height_minus_midlevel_times_scale(height, midlevel, scale):
    material = _material(_displacement(height, midlevel, scale))
    data = core.extract_blender_material_data(material)
    offset = data["vertex_offset"]
    want = tuple(n * (height - midlevel) * scale for n in NORMAL)
    assert _ev(offset) == pytest.approx(want)


def test_vector_displacement_is_vector_minus_midlevel_times_scale():
    material = _material(_vector_displacement((0.2, -0.4, 0.5, 1.0), 0.1, 0.5))
    data = core.extract_blender_material_data(material)
    assert _ev(data["vertex_offset"]) == pytest.approx(((0.2 - 0.1) * 0.5, (-0.4 - 0.1) * 0.5, (0.5 - 0.1) * 0.5))


def test_a_linked_height_reaches_the_offset():
    value = _Node(type="VALUE", name="Amount")
    value.outputs = _Sockets(Value=_Socket(0.3, name="Value"))
    value.inputs = _Sockets()
    node = _displacement(0.0, 0.0, 0.5, height_link=_Link(value, value.outputs["Value"]))
    material = _material(node, extra_nodes=[value])
    data = core.extract_blender_material_data(material)
    assert _ev(data["vertex_offset"]) == pytest.approx(tuple(n * 0.3 * 0.5 for n in NORMAL))


@pytest.mark.parametrize("material, fragment", [
    (_material(_displacement(), method="BUMP"), "Bump Only"),
    (_material(_displacement(space="WORLD")), "World space"),
    (_material(_displacement(normal_linked=True)), "linked Normal"),
    (_material(_vector_displacement(space="TANGENT")), "Tangent space"),
    (_material(_Node(type="VALUE", name="Raw", inputs=_Sockets(), outputs=_Sockets(Displacement=_Socket(name="Displacement")))), "rather than a Displacement"),
])
def test_refusals_name_the_remedy(material, fragment):
    reason = core.displacement_refusal(material)
    assert reason is not None and fragment in reason
    assert "vertex_offset" not in core.extract_blender_material_data(material)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any(fragment in e["message"] for e in result["errors"])


def test_an_exportable_displacement_validates_with_the_normals_notice():
    material = _material(_displacement(), method="BOTH")
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True, result["errors"]
    messages = [w["message"] for w in result["warnings"]]
    assert any("normals are not recomputed" in m for m in messages)
    assert any("bump half is dropped" in m for m in messages)


def test_the_graph_carries_a_geometry_modifier_fed_by_the_offset():
    material = _material(_displacement(1.0, 0.0, 0.1))
    data = core.extract_blender_material_data(material)
    graph = MaterialXGraphBuilder(_MANIFEST).build_pbr_material(data)
    modifier = next(n for n in graph["nodes"] if n["node_id"] == "realitykit_geometrymodifier_vertexshader")
    assert graph["vertex_output"] == modifier["name"]
    feeds = [c for c in graph["connections"] if c["to_node"] == modifier["name"]]
    assert [c["to_input"] for c in feeds] == ["modelPositionOffset"]


def test_the_material_exposes_a_realitykit_vertex_output():
    pytest.importorskip("pxr")
    from pxr import Usd, UsdShade
    from Plugin.export.materials.author import create_materialx_material

    material = _material(_displacement(1.0, 0.0, 0.1))
    data = core.extract_blender_material_data(material)
    graph = MaterialXGraphBuilder(_MANIFEST).build_pbr_material(data)
    stage = Usd.Stage.CreateInMemory()
    create_materialx_material(stage, "/Material", "Displaced", graph, _MANIFEST)
    usd_material = UsdShade.Material(stage.GetPrimAtPath("/Material"))
    assert usd_material.GetSurfaceOutput("mtlx").GetConnectedSource()
    vertex = usd_material.GetOutput("realitykit:vertex")
    assert vertex and vertex.GetConnectedSource()
    source_prim = vertex.GetConnectedSource()[0].GetPrim()
    assert UsdShade.Shader(source_prim).GetIdAttr().Get() == "ND_realitykit_geometrymodifier_2_0_vertexshader"
    offset_input = UsdShade.Shader(source_prim).GetInput("modelPositionOffset")
    assert offset_input and offset_input.GetConnectedSource()
