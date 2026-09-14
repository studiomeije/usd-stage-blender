"""A Principled Hair BSDF surface, exported as RealityKit's hair surface.

The mapped input names are checked against the shipped nodedef rather than a
copy of our own table, and the authored terminal is read back from USD.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

import mx_eval  # noqa: E402
from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.export.materials.graph import RCP3_HAIR_NODEDEF, MaterialXGraphBuilder  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

_MANIFEST = load_manifest()
_HAIR_DEF = _MANIFEST["nodes"][RCP3_HAIR_NODEDEF]
_DECLARED = {entry["name"]: entry["type"] for entry in _HAIR_DEF["inputs"]}


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


_HAIR_DEFAULTS = (
    ("Color", (0.0175, 0.0058, 0.0021, 1.0), "RGBA"),
    ("Melanin", 0.8, "VALUE"), ("Melanin Redness", 1.0, "VALUE"),
    ("Tint", (1.0, 1.0, 1.0, 1.0), "RGBA"),
    ("Absorption Coefficient", (0.2455, 0.52, 1.365), "VECTOR"),
    ("Aspect Ratio", 0.85, "VALUE"), ("Roughness", 0.3, "VALUE"),
    ("Radial Roughness", 0.3, "VALUE"), ("Coat", 0.0, "VALUE"), ("IOR", 1.55, "VALUE"),
    ("Offset", 0.0349, "VALUE"), ("Random Color", 0.0, "VALUE"),
    ("Random Roughness", 0.0, "VALUE"), ("Random", 0.0, "VALUE"), ("Weight", 0.0, "VALUE"),
    ("Reflection", 1.0, "VALUE"), ("Transmission", 1.0, "VALUE"),
    ("Secondary Reflection", 1.0, "VALUE"),
)


#: Which sockets Blender leaves enabled, measured on 5.2 across both models
#: and all three colour parametrizations. A disabled socket changes nothing in
#: the render, so the exporter must not read it.
_HAIR_COLOUR_GROUP = {
    "COLOR": {"Color"},
    "MELANIN": {"Melanin", "Melanin Redness", "Tint", "Random Color"},
    "ABSORPTION": {"Absorption Coefficient"},
}
_HAIR_COMMON = {"Roughness", "IOR", "Offset", "Random Roughness", "Random"}
_HAIR_MODEL_GROUP = {
    "CHIANG": {"Radial Roughness", "Coat"},
    "HUANG": {"Aspect Ratio", "Reflection", "Transmission", "Secondary Reflection"},
}


def _enabled_names(model, parametrization):
    return _HAIR_COLOUR_GROUP[parametrization] | _HAIR_COMMON | _HAIR_MODEL_GROUP[model]


def _hair(parametrization="COLOR", model="CHIANG", node_type="BSDF_HAIR_PRINCIPLED", **overrides):
    node = _Node(type=node_type, name="Principled Hair BSDF", parametrization=parametrization, model=model)
    enabled = _enabled_names(model, parametrization) if node_type == "BSDF_HAIR_PRINCIPLED" else None
    sockets = {}
    for name, value, stype in _HAIR_DEFAULTS:
        socket = _Socket(overrides.get(name, value), name=name, socket_type=stype)
        if enabled is not None:
            socket.enabled = name in enabled
        sockets[name] = socket
    node.inputs = _Sockets(**sockets)
    node.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    return node


def _legacy_hair():
    node = _Node(type="BSDF_HAIR", name="Hair BSDF", component="Reflection")
    node.inputs = _Sockets(Color=_Socket((0.8, 0.8, 0.8, 1.0), name="Color", socket_type="RGBA"))
    node.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    return node


def _material(surface, *nodes):
    output = _Node(type="OUTPUT_MATERIAL", name="Material Output", is_active_output=True)
    output.inputs = _Sockets(
        Surface=_Socket(linked=True, link=_Link(surface, next(iter(surface.outputs))), name="Surface", socket_type="SHADER"),
        Volume=_Socket(name="Volume", socket_type="SHADER"),
        Displacement=_Socket((0.0, 0.0, 0.0), name="Displacement", socket_type="VECTOR"),
    )
    all_nodes = [output, surface, *nodes]
    tree = SimpleNamespace(nodes=_Sockets({n.name + str(i): n for i, n in enumerate(all_nodes)}), links=[], animation_data=None)
    return SimpleNamespace(name="Hair", node_tree=tree, displacement_method="BUMP",
                           surface_render_method="DITHERED", blend_method="OPAQUE", diffuse_color=(1, 1, 1, 1))


def test_every_mapped_target_is_declared_by_the_shipped_hair_nodedef():
    """The mapping is checked against the platform's own declaration, so a
    renamed or dropped input fails here rather than at import."""
    for _socket, targets, expected_type in core._HAIR_MAPPED_SOCKETS:
        for target in targets:
            assert target in _DECLARED, target
            assert _DECLARED[target] == expected_type, (target, _DECLARED[target])


def test_the_mapped_controls_reach_the_hair_surface():
    material = _material(_hair(**{
        "Color": (0.35, 0.12, 0.03, 1.0), "Roughness": 0.25,
        "Reflection": 0.8, "Secondary Reflection": 0.4,
    }))
    data = core.extract_blender_material_data(material)
    assert data["type"] == "hair"
    # Chiang disables the two lobe weights, so only colour and roughness are
    # authored; the resting weights would otherwise be exported as real values.
    assert data["hair_inputs"] == {
        "baseColor": pytest.approx([0.35, 0.12, 0.03]),
        "primaryRoughness": pytest.approx(0.25),
        "secondaryRoughness": pytest.approx(0.25),
        "tangent": [1.0, 0.0, 0.0],
    }
    # The bake lanes rebuild this as unlit, which needs a base colour.
    assert data["base_color"] == pytest.approx([0.35, 0.12, 0.03])


def test_a_linked_roughness_feeds_both_lobes():
    value = _Node(type="VALUE", name="Rough")
    value.outputs = _Sockets(Value=_Socket(0.42, name="Value"))
    value.inputs = _Sockets()
    node = _hair()
    node.inputs["Roughness"].is_linked = True
    node.inputs["Roughness"].links = [_Link(value, value.outputs["Value"])]
    data = core.extract_blender_material_data(_material(node, value))
    assert data["hair_inputs"]["primaryRoughness"] == pytest.approx(0.42)
    assert data["hair_inputs"]["secondaryRoughness"] == pytest.approx(0.42)


def test_a_graph_colour_is_wired_into_base_color():
    """A colour the resolver cannot fold - here a Fresnel reader - has to
    arrive as a connection on the hair surface's baseColor."""
    fresnel = _Node(type="FRESNEL", name="Fresnel")
    fresnel.inputs = _Sockets(IOR=_Socket(1.45, name="IOR"),
                              Normal=_Socket((0.0, 0.0, 0.0), name="Normal", socket_type="VECTOR"))
    fresnel.outputs = _Sockets(Fac=_Socket(name="Fac"))
    node = _hair()
    node.inputs["Color"].is_linked = True
    node.inputs["Color"].links = [_Link(fresnel, fresnel.outputs["Fac"])]
    data = core.extract_blender_material_data(_material(node, fresnel))
    assert data["input_graphs"]["baseColor"]["kind"] == "node"
    graph = MaterialXGraphBuilder(_MANIFEST).build_hair_material(data)
    hair = next(n for n in graph["nodes"] if n["node_id"] == RCP3_HAIR_NODEDEF)
    assert graph["output"] == hair["name"]
    assert graph["surface_profile"] == "realitykit_hair"
    assert any(c["to_node"] == hair["name"] and c["to_input"] == "baseColor" for c in graph["connections"])


def test_the_authored_material_terminates_in_the_hair_surface():
    pytest.importorskip("pxr")
    from pxr import UsdShade, Usd
    from Plugin.export.materials.author import create_materialx_material

    material = _material(_hair(**{"Color": (0.35, 0.12, 0.03, 1.0), "Roughness": 0.2}))
    data = core.extract_blender_material_data(material)
    graph = MaterialXGraphBuilder(_MANIFEST).build_hair_material(data)
    stage = Usd.Stage.CreateInMemory()
    create_materialx_material(stage, "/Material", "Hair", graph, _MANIFEST)
    usd_material = UsdShade.Material(stage.GetPrimAtPath("/Material"))
    source = usd_material.GetSurfaceOutput("mtlx").GetConnectedSource()[0].GetPrim()
    assert UsdShade.Shader(source).GetIdAttr().Get() == RCP3_HAIR_NODEDEF
    assert UsdShade.Shader(source).GetInput("primaryRoughness").Get() == pytest.approx(0.2)
    # normal stays alone so the platform uses the shading normal; tangent is
    # authored, because the surface never substitutes a strand direction.
    normal = UsdShade.Shader(source).GetInput("normal")
    assert not normal or not normal.HasConnectedSource()
    tangent = UsdShade.Shader(source).GetInput("tangent")
    assert not tangent.HasConnectedSource()
    assert tuple(tangent.Get()) == pytest.approx((1.0, 0.0, 0.0))
    assert stage.GetPrimAtPath("/Material").GetCustomDataByKey("USDStage:surfaceProfile") == "realitykit_hair"


@pytest.mark.parametrize("surface, fragment", [
    (_hair(parametrization="MELANIN"), "Melanin concentration coloring"),
    (_hair(parametrization="ABSORPTION"), "Absorption coefficient coloring"),
    (_legacy_hair(), "single-lobe Cycles shader"),
])
def test_unsupported_hair_surfaces_are_refused_by_name(surface, fragment):
    material = _material(surface)
    reason = core.hair_refusal(material)
    assert reason is not None and fragment in reason
    assert core.extract_blender_material_data(material)["type"] != "hair"
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any(fragment in e["message"] for e in result["errors"])


def test_an_exported_hair_surface_warns_that_it_approximates():
    material = _material(_hair())
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True, result["errors"]
    assert any("two-lobe approximation" in w["message"] for w in result["warnings"])


def test_the_huang_model_and_dropped_links_are_named():
    value = _Node(type="VALUE", name="Tinted")
    value.outputs = _Sockets(Value=_Socket(0.5, name="Value"))
    value.inputs = _Sockets()
    node = _hair(model="HUANG")
    for socket_name in ("Offset", "Transmission"):
        node.inputs[socket_name].is_linked = True
        node.inputs[socket_name].links = [_Link(value, value.outputs["Value"])]
    notices = core.hair_notices(_material(node, value))
    assert any("Huang" in n and "Aspect Ratio" in n for n in notices)
    dropped = next(n for n in notices if "linked Principled Hair inputs" in n)
    assert "Offset" in dropped and "Transmission" in dropped


def test_the_huang_model_carries_its_two_lobe_weights_at_the_surfaces_scale():
    """Measured with RealityRenderer on macOS 27 (a UV sphere under uniform
    light, tangent along U): a lobe's contribution grows linearly with its
    weight up to 1 and not beyond (0.25: +0.0088, 0.5: +0.0180, 1: +0.0363,
    2: +0.0363), and the secondary lobe matches the primary at the same weight
    once its colour is white, drawing nothing while the colour keeps its black
    default. The weight shares PBR Surface 2's set_specular, whose 0.5 is a
    dielectric's 4%, so Blender's unscaled lobe weight 1 is 0.5."""
    material = _material(_hair(model="HUANG", **{"Reflection": 0.8, "Secondary Reflection": 0.4}))
    data = core.extract_blender_material_data(material)
    assert data["hair_inputs"]["primarySpecular"] == pytest.approx(0.4)
    assert data["hair_inputs"]["secondarySpecular"] == pytest.approx(0.2)
    assert data["hair_inputs"]["secondarySpecularColor"] == [1.0, 1.0, 1.0]
    graph = MaterialXGraphBuilder(_MANIFEST).build_hair_material(data)
    hair = next(n for n in graph["nodes"] if n["node_id"] == RCP3_HAIR_NODEDEF)
    assert hair["inputs"]["secondarySpecularColor"] == [1.0, 1.0, 1.0]
    assert _DECLARED["secondarySpecularColor"] == "color3"


def test_a_linked_lobe_weight_is_scaled_in_the_graph():
    dot = mx_eval.Node("VECT_MATH", "Vector Math", {"A": (0.9, 0.0, 0.0), "B": (0.5, 0.0, 0.0), "C": (0.0, 0.0, 0.0),
                                                     "Scale": 1.0}, outputs=("Vector", "Value"), operation="DOT_PRODUCT")
    node = _hair(model="HUANG")
    node.inputs["Secondary Reflection"].is_linked = True
    node.inputs["Secondary Reflection"].links = [dot.out("Value")]
    data = core.extract_blender_material_data(_material(node, dot))
    assert mx_eval.evaluate(data["input_graphs"]["secondarySpecular"]) == pytest.approx(0.45 * 0.5)
    assert data["hair_inputs"]["secondarySpecularColor"] == [1.0, 1.0, 1.0]


def test_the_chiang_model_leaves_the_secondary_lobe_unauthored():
    data = core.extract_blender_material_data(_material(_hair()))
    assert "secondarySpecular" not in data["hair_inputs"]
    assert "secondarySpecularColor" not in data["hair_inputs"]


def test_a_disabled_socket_is_never_read():
    """Blender's own switch, not ours: a socket it disables keeps a resting
    value that changes nothing, so exporting it would invent a control."""
    node = _hair(model="HUANG", **{"Reflection": 0.8})
    for socket in node.inputs:
        if socket.name == "Reflection":
            socket.enabled = False
    data = core.extract_blender_material_data(_material(node))
    assert "primarySpecular" not in data["hair_inputs"]
    assert "secondarySpecular" in data["hair_inputs"]


def test_a_dropped_link_on_a_disabled_socket_is_not_named():
    value = _Node(type="VALUE", name="Unused")
    value.outputs = _Sockets(Value=_Socket(0.5, name="Value"))
    value.inputs = _Sockets()
    node = _hair()  # Chiang: Transmission is disabled
    node.inputs["Transmission"].is_linked = True
    node.inputs["Transmission"].links = [_Link(value, value.outputs["Value"])]
    notices = core.hair_notices(_material(node, value))
    assert not any("linked Principled Hair inputs" in n for n in notices)


def test_the_strand_direction_is_the_constant_u_axis_of_the_tangent_frame():
    """Measured with RealityRenderer on macOS 27 on a UV sphere whose U runs
    around it: tangent (1,0,0) draws the highlight of fibres along U and
    (0,1,0) that of fibres along V, and both turn with the sphere when it is
    rolled 90 degrees, so the input is read in the tangent frame; (0,0,1) and
    the object-space dP/du primvar the export used to write draw no strand
    highlight."""
    data = core.extract_blender_material_data(_material(_hair()))
    assert "tangent" not in (data.get("input_graphs") or {})
    assert data["hair_inputs"]["tangent"] == [1.0, 0.0, 0.0]
    graph = MaterialXGraphBuilder(_MANIFEST).build_hair_material(data)
    hair = next(n for n in graph["nodes"] if n["node_id"] == RCP3_HAIR_NODEDEF)
    assert hair["inputs"]["tangent"] == [1.0, 0.0, 0.0]
    assert not any(c["to_input"] == "tangent" for c in graph["connections"])
    assert not any(n["node_id"].startswith("ND_geompropvalue") for n in graph["nodes"])


def test_the_uv_convention_is_stated_in_a_warning():
    notices = core.hair_notices(_material(_hair()))
    assert any("+U" in n for n in notices)
