"""Mix Shader with a Transparent BSDF becomes opacity; Add Shader with an
Emission shader adds emission. Blender's mix is out = (1 - fac) * A + fac * B,
so the surface's opacity is the factor's complement when the Transparent BSDF
is B and the factor itself when it is A; the tests evaluate the authored
opacity against that arithmetic. Every other closure is refused by name."""

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


def _principled(alpha=1.0, alpha_link=None, emission=(0.0, 0.0, 0.0), strength=1.0):
    node = _Node(type="BSDF_PRINCIPLED", name="Principled BSDF")
    node.inputs = _Sockets(**{
        "Base Color": _Socket((0.8, 0.2, 0.1, 1.0), name="Base Color", socket_type="RGBA"),
        "Metallic": _Socket(0.0, name="Metallic"), "Roughness": _Socket(0.5, name="Roughness"),
        "IOR": _Socket(1.45, name="IOR"),
        "Alpha": _Socket(alpha, name="Alpha", linked=alpha_link is not None, link=alpha_link),
        "Emission Color": _Socket(tuple(emission) + (1.0,), name="Emission Color", socket_type="RGBA"),
        "Emission Strength": _Socket(strength, name="Emission Strength"),
    })
    node.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    return node


def _transparent(color=(1.0, 1.0, 1.0, 1.0)):
    node = _Node(type="BSDF_TRANSPARENT", name="Transparent BSDF")
    node.inputs = _Sockets(Color=_Socket(color, name="Color", socket_type="RGBA"), Weight=_Socket(0.0, name="Weight"))
    node.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    return node


def _emission(color=(0.1, 1.0, 0.35, 1.0), strength=2.0):
    node = _Node(type="EMISSION", name="Emission")
    node.inputs = _Sockets(Color=_Socket(color, name="Color", socket_type="RGBA"), Strength=_Socket(strength, name="Strength"), Weight=_Socket(0.0, name="Weight"))
    node.outputs = _Sockets(Emission=_Socket(name="Emission", socket_type="SHADER"))
    return node


def _shader_out(node):
    return next(iter(node.outputs))


def _mix(a, b, fac=0.5, fac_link=None):
    node = _Node(type="MIX_SHADER", name="Mix Shader")
    node.inputs = _Sockets(
        Fac=_Socket(fac, name="Fac", linked=fac_link is not None, link=fac_link),
        Shader=_Socket(name="Shader", socket_type="SHADER", linked=True, link=_Link(a, _shader_out(a))),
        Shader_001=_Socket(name="Shader", socket_type="SHADER", linked=True, link=_Link(b, _shader_out(b))),
    )
    node.outputs = _Sockets(Shader=_Socket(name="Shader", socket_type="SHADER"))
    return node


def _add(a, b):
    node = _Node(type="ADD_SHADER", name="Add Shader")
    node.inputs = _Sockets(
        Shader=_Socket(name="Shader", socket_type="SHADER", linked=True, link=_Link(a, _shader_out(a))),
        Shader_001=_Socket(name="Shader", socket_type="SHADER", linked=True, link=_Link(b, _shader_out(b))),
    )
    node.outputs = _Sockets(Shader=_Socket(name="Shader", socket_type="SHADER"))
    return node


def _material(surface, *nodes):
    output = _Node(type="OUTPUT_MATERIAL", name="Material Output", is_active_output=True)
    output.inputs = _Sockets(
        Surface=_Socket(linked=True, link=_Link(surface, _shader_out(surface)), name="Surface", socket_type="SHADER"),
        Volume=_Socket(name="Volume", socket_type="SHADER"),
        Displacement=_Socket((0.0, 0.0, 0.0), name="Displacement", socket_type="VECTOR"),
    )
    all_nodes = [output, surface, *nodes]
    tree = SimpleNamespace(nodes=_Sockets({n.name + str(i): n for i, n in enumerate(all_nodes)}), links=[], animation_data=None)
    return SimpleNamespace(name="Closure", node_tree=tree, displacement_method="BUMP",
                           surface_render_method="BLENDED", blend_method="BLEND", diffuse_color=(1, 1, 1, 1))


def _ev(expr):
    kind = expr.get("kind")
    if kind == "constant":
        v = expr["value"]
        return tuple(float(c) for c in v) if isinstance(v, (list, tuple)) else float(v)
    assert kind == "node", expr
    nid = expr["node_id"]
    assert nid in _MANIFEST["nodes"], nid
    ins = {k: _ev(v) for k, v in expr["inputs"].items()}
    op = nid[3:].rsplit("_", 1)[0]
    if op == "combine3":
        return (ins["in1"], ins["in2"], ins["in3"])
    a, b = ins["in1"], ins["in2"]
    f = {"multiply": lambda x, y: x * y, "subtract": lambda x, y: x - y, "add": lambda x, y: x + y}[op]
    if isinstance(a, tuple):
        return tuple(f(x, y) for x, y in zip(a, b))
    return f(a, b)


@pytest.mark.parametrize("fac, alpha", [(0.6, 1.0), (0.25, 0.8), (0.0, 0.5)])
def test_principled_mixed_with_transparent_on_b_is_alpha_times_one_minus_fac(fac, alpha):
    p, t = _principled(alpha), _transparent()
    material = _material(_mix(p, t, fac), p, t)
    assert core.material_has_transparency(material) is True
    data = core.extract_blender_material_data(material)
    assert data["type"] == "principled"
    assert data["is_transparent"] is True
    assert data["alpha"] == pytest.approx(alpha * (1.0 - fac))


def test_transparent_on_a_uses_the_factor_itself():
    p, t = _principled(0.9), _transparent()
    material = _material(_mix(t, p, 0.3), p, t)
    data = core.extract_blender_material_data(material)
    assert data["alpha"] == pytest.approx(0.9 * 0.3)


def test_a_linked_factor_becomes_an_opacity_graph():
    """A factor read from geometry cannot fold, so opacity is authored as
    alpha * (1 - factor) with the reader connected."""
    geometry = _Node(type="NEW_GEOMETRY", name="Geometry")
    geometry.inputs = _Sockets()
    geometry.outputs = _Sockets(Backfacing=_Socket(0.0, name="Backfacing"))
    p, t = _principled(1.0), _transparent()
    mix = _mix(p, t, fac_link=_Link(geometry, geometry.outputs["Backfacing"]))
    material = _material(mix, p, t, geometry)
    data = core.extract_blender_material_data(material)
    assert "alpha" not in data
    opacity = data["input_graphs"]["opacity"]
    assert opacity["node_id"] == "ND_multiply_float"
    assert opacity["inputs"]["in1"] == {"kind": "constant", "value": 1.0}
    complement = opacity["inputs"]["in2"]
    assert complement["node_id"] == "ND_subtract_float"
    assert complement["inputs"]["in1"] == {"kind": "constant", "value": 1.0}
    assert complement["inputs"]["in2"]["kind"] == "node"
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True, result["errors"]
    graph = MaterialXGraphBuilder(_MANIFEST).build_pbr_material(data)
    surface = next(n for n in graph["nodes"] if n["node_id"].endswith("pbr_surfaceshader_2_0"))
    assert any(c["to_node"] == surface["name"] and c["to_input"] == "opacity" for c in graph["connections"])


def test_a_constant_value_node_on_the_factor_folds():
    value = _Node(type="VALUE", name="Cut")
    value.outputs = _Sockets(Value=_Socket(0.75, name="Value"))
    value.inputs = _Sockets()
    p, t = _principled(1.0), _transparent()
    material = _material(_mix(p, t, fac_link=_Link(value, value.outputs["Value"])), p, t, value)
    assert core.extract_blender_material_data(material)["alpha"] == pytest.approx(0.25)


def test_principled_plus_emission_adds_to_the_surface_emission():
    p, e = _principled(emission=(0.2, 0.0, 0.0), strength=1.0), _emission((0.1, 1.0, 0.35, 1.0), 2.0)
    material = _material(_add(p, e), p, e)
    data = core.extract_blender_material_data(material)
    assert data["type"] == "principled"
    assert data["emission_strength"] == 1.0
    assert data["emission_color"] == pytest.approx([0.2 + 0.2, 2.0, 0.7])
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_a_value_node_on_emission_strength_folds_into_the_colour():
    value = _Node(type="VALUE", name="Glow")
    value.outputs = _Sockets(Value=_Socket(3.0, name="Value"))
    value.inputs = _Sockets()
    e = _emission((0.0, 0.5, 1.0, 1.0), 1.0)
    e.inputs["Strength"].is_linked = True
    e.inputs["Strength"].links = [_Link(value, value.outputs["Value"])]
    p = _principled()
    material = _material(_add(e, p), p, e, value)
    data = core.extract_blender_material_data(material)
    # A Value node folds; the product is constant emission at strength 1.
    assert data["emission_color"] == pytest.approx([0.0, 1.5, 3.0])
    assert data["emission_strength"] == 1.0


@pytest.mark.parametrize("build, fragment", [
    (lambda: (lambda p, e: _material(_mix(p, e, 0.5), p, e))(_principled(), _emission()), "fades a Principled BSDF against an Emission"),
    (lambda: (lambda p, t: _material(_mix(p, t, 0.5), p, t))(_principled(), _transparent((0.2, 0.2, 1.0, 1.0))), "tinted or linked Color"),
    (lambda: (lambda p, t: _material(_add(p, t), p, t))(_principled(), _transparent()), "adds Principled BSDF and Transparent BSDF"),
])
def test_other_closures_are_refused_by_name(build, fragment):
    material = build()
    reason = core.resolve_surface_closure(material)["refusal"]
    assert reason is not None and fragment in reason
    assert core.extract_blender_material_data(material)["type"] == "unknown"
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any(fragment in e["message"] for e in result["errors"])


def test_a_transparent_bsdf_anywhere_else_is_refused():
    p, t = _principled(), _transparent()
    material = _material(_add(p, t), p, t)
    result = validate.validate_material(material, strict=True)
    messages = [e["message"] for e in result["errors"]]
    assert any("Add Shader" in m for m in messages)


# --------------------------------------------------------------------------
# Cycles saturates the Mix Shader factor (svm_node_mix_closure) and the
# Principled Alpha (principled_bsdf_emission) before either weighs a closure.
# --------------------------------------------------------------------------

def _saturate(x):
    return min(max(x, 0.0), 1.0)


def _cycles_opacity(fac, alpha, transparent_first=False):
    """The surface weight Cycles leaves beside the Transparent BSDF."""
    fac = _saturate(fac)
    weight = fac if transparent_first else 1.0 - fac
    return weight * _saturate(alpha)


@pytest.mark.parametrize("fac, alpha", [(1.5, 1.0), (-0.4, 0.8), (0.3, 1.7), (0.3, -0.2)])
def test_an_out_of_range_factor_or_alpha_saturates_like_cycles(fac, alpha):
    p, t = _principled(alpha), _transparent()
    material = _material(_mix(p, t, fac), p, t)
    data = core.extract_blender_material_data(material)
    assert data["alpha"] == pytest.approx(_cycles_opacity(fac, alpha))


def test_a_linked_factor_above_one_saturates_in_the_graph():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mx_eval

    driver = mx_eval.Node("VECT_MATH", "Vector Math", {"A": (1.4, 0.0, 0.0), "B": (1.0, 0.0, 0.0), "C": (0.0, 0.0, 0.0),
                                                        "Scale": 1.0}, outputs=("Vector", "Value"), operation="DOT_PRODUCT")
    p, t = _principled(1.0), _transparent()
    material = _material(_mix(t, p, fac_link=driver.out("Value")), p, t, driver)
    data = core.extract_blender_material_data(material)
    assert mx_eval.evaluate(data["input_graphs"]["opacity"]) == pytest.approx(_cycles_opacity(1.4, 1.0, True))


@pytest.mark.parametrize("alpha, alpha_linked", [(0.5, False), (1.0, True)])
def test_an_emission_added_over_a_transparent_principled_is_refused(alpha, alpha_linked):
    """Measured with RealityRenderer on macOS 27: emissiveColor 0.5 renders
    0.372 at opacity 1 and 0.184 at opacity 0.5, while Cycles' Add Shader adds
    the Emission closure outside the Principled BSDF's Alpha."""
    link, extra = None, ()
    if alpha_linked:
        geometry = _Node(type="NEW_GEOMETRY", name="Geometry")
        geometry.inputs = _Sockets()
        geometry.outputs = _Sockets(Backfacing=_Socket(0.0, name="Backfacing"))
        link, extra = _Link(geometry, geometry.outputs["Backfacing"]), (geometry,)
    p, e = _principled(alpha, alpha_link=link), _emission()
    material = _material(_add(p, e), p, e, *extra)
    reason = core.resolve_surface_closure(material)["refusal"]
    assert reason is not None and "Alpha" in reason and "dimmed" in reason
    assert core.extract_blender_material_data(material)["type"] == "unknown"
    result = validate.validate_material(material, strict=True)
    assert any("dimmed" in err["message"] for err in result["errors"])
