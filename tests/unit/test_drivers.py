"""Driver expressions over ``frame`` exported as RealityKit's time reader.

The background process the CLI runs never evaluates drivers, so a driven
socket used to export as zero with no warning. Now a scripted driver over
``frame`` on a Value node output, or on a float input the resolver reads live,
becomes ``time * fps`` arithmetic; every other driver is refused by name.
The expression tests evaluate the authored tree with a fixed time and compare
it with Python evaluating the same expression at ``frame = time * fps``.
"""

from __future__ import annotations

import math
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
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

_MANIFEST = load_manifest()
FPS = 24.0
TIME = 2.75  # seconds


def _evaluate(expr):
    kind = expr.get("kind")
    if kind == "constant":
        return float(expr["value"])
    assert kind == "node", expr
    nid = expr["node_id"]
    assert nid in _MANIFEST["nodes"], nid
    ins = {k: _evaluate(v) for k, v in expr["inputs"].items()}
    op = nid[3:].rsplit("_", 1)[0]
    if op == "time":
        return TIME
    table = {
        "add": lambda: ins["in1"] + ins["in2"], "subtract": lambda: ins["in1"] - ins["in2"],
        "multiply": lambda: ins["in1"] * ins["in2"], "divide": lambda: ins["in1"] / ins["in2"],
        "sin": lambda: math.sin(ins["in"]), "cos": lambda: math.cos(ins["in"]), "tan": lambda: math.tan(ins["in"]),
        "absval": lambda: abs(ins["in"]), "floor": lambda: math.floor(ins["in"]), "ceil": lambda: math.ceil(ins["in"]),
        "sqrt": lambda: math.sqrt(ins["in"]), "min": lambda: min(ins["in1"], ins["in2"]), "max": lambda: max(ins["in1"], ins["in2"]),
        "power": lambda: ins["in1"] ** ins["in2"],
        "clamp": lambda: min(max(ins["in"], ins["low"]), ins["high"]),
    }
    return table[op]()


@pytest.mark.parametrize("expression", [
    "frame", "frame / 24", "frame * 0.05", "sin(frame * 0.1) + 1", "(frame - 10) / 2",
    "-frame", "max(0, frame - 12) / 24", "abs(sin(frame / 7))", "pow(frame / 100, 2)", "floor(frame / 24) * 0.5",
])
def test_driver_expression_matches_python_at_frame_equals_time_times_fps(expression):
    expr = core.driver_expression_expr(expression, FPS)
    want = eval(expression, {"__builtins__": {}}, {"frame": TIME * FPS, "sin": math.sin, "cos": math.cos, "tan": math.tan, "abs": abs, "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt, "min": min, "max": max, "pow": pow})
    assert _evaluate(expr) == pytest.approx(want, abs=1e-9)


def test_constants_fold_and_frame_is_time_times_fps():
    expr = core.driver_expression_expr("frame * (2 + 3)", FPS)
    assert expr["node_id"] == "ND_multiply_float"
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": 5.0}
    frame = expr["inputs"]["in1"]
    assert frame["node_id"] == "ND_multiply_float"
    assert frame["inputs"]["in1"]["node_id"] == "ND_time_float"
    assert frame["inputs"]["in1"]["inputs"]["fps"] == {"kind": "constant", "value": FPS}
    assert frame["inputs"]["in2"] == {"kind": "constant", "value": FPS}


@pytest.mark.parametrize("expression, fragment", [
    ("frame ** 2", "something other than"),
    ("noise(frame)", "something other than"),
    ("x + 1", "uses 'x'"),
    ("frame % 10", "something other than"),
    ("import os", "does not parse"),
])
def test_unsupported_expressions_are_refused_by_name(expression, fragment):
    with pytest.raises(core._DriverUnsupported, match=fragment):
        core.driver_expression_expr(expression, FPS)


# --- sockets with drivers, through the resolver and the validator ------------

class _Socket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value", path=None, tree=None, socket_type="VALUE"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name
        self._path = path
        self.id_data = tree
        self.type = socket_type

    def path_from_id(self, prop):
        return f"{self._path}.{prop}"


class _Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _Node:
    """Hashable stand-in: the resolver keeps visited nodes in a set."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _Inputs(dict):
    """Blender's socket collection: iterates sockets, indexes by name or position."""

    def __iter__(self):
        return iter(self.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Nodes(_Inputs):
    """Blender's node collection: iterates nodes, ``get``/``[]`` by name."""


def _driver(expression, data_path, index=0, variables=(), driver_type="SCRIPTED"):
    return SimpleNamespace(
        data_path=data_path, array_index=index,
        driver=SimpleNamespace(expression=expression, type=driver_type, variables=list(variables)),
    )


def _tree_with(nodes, drivers):
    tree = SimpleNamespace(nodes=_Nodes({n.name: n for n in nodes}), links=[], animation_data=SimpleNamespace(drivers=list(drivers)))
    for n in nodes:
        for s in list(getattr(n, "outputs", []) or []) + list(getattr(n, "inputs", []) or []):
            s.id_data = tree
    return tree


def _value_node(name, value, tree_ref):
    node = _Node(type="VALUE", name=name)
    out = _Socket(value, name="Value", path=f'nodes["{name}"].outputs[0]')
    node.outputs = _Outputs(out)
    node.inputs = _Inputs()
    return node


class _Outputs(list):
    def __init__(self, *sockets):
        super().__init__(sockets)

    def get(self, name):
        return next((s for s in self if s.name == name), None)


def _material(nodes, drivers, roughness_source):
    principled = _Node(type="BSDF_PRINCIPLED", name="Principled BSDF")
    principled.inputs = _Inputs(Roughness=roughness_source)
    roughness_source.name = "Roughness"
    output = _Node(type="OUTPUT_MATERIAL", name="Material Output", is_active_output=True)
    output.inputs = _Inputs(Surface=_Socket(linked=True, link=_Link(principled, SimpleNamespace(name="BSDF")), name="Surface"))
    all_nodes = [output, principled, *nodes]
    tree = _tree_with(all_nodes, drivers)
    return SimpleNamespace(name="Driven", node_tree=tree)


def test_a_driven_value_node_resolves_to_the_time_expression():
    value = _value_node("Speed", 0.0, None)
    material = _material([value], [_driver("frame / 24", 'nodes["Speed"].outputs[0].default_value')],
                         _Socket(linked=True, link=_Link(value, value.outputs[0])))
    expr = core._resolve_socket_value(material.node_tree.nodes["Principled BSDF"].inputs["Roughness"])
    assert expr["node_id"] == "ND_divide_float"
    assert _evaluate(expr) == pytest.approx(TIME)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True
    assert any("exports as RealityKit's time reader" in w["message"] for w in result["warnings"])


def test_a_driver_with_variables_is_refused():
    value = _value_node("Speed", 0.0, None)
    material = _material([value], [_driver("var * 2", 'nodes["Speed"].outputs[0].default_value', variables=[SimpleNamespace(name="var", type="SINGLE_PROP")])],
                         _Socket(linked=True, link=_Link(value, value.outputs[0])))
    expr = core._resolve_socket_value(material.node_tree.nodes["Principled BSDF"].inputs["Roughness"])
    assert expr["kind"] == "unresolved"
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any("reads driver variables (var)" in e["message"] for e in result["errors"])


def test_a_driver_on_a_principled_input_is_refused_by_name():
    roughness = _Socket(0.5, path='nodes["Principled BSDF"].inputs[0]')
    material = _material([], [_driver("frame / 100", 'nodes["Principled BSDF"].inputs[0].default_value')], roughness)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any("Driver 'frame / 100' on Principled BSDF 'Roughness' is not exported" in e["message"] for e in result["errors"])


def test_a_driven_math_input_is_live():
    math_node = _Node(type="MATH", name="Math", operation="MULTIPLY", use_clamp=False)
    a = _Socket(2.0, name="Value", path='nodes["Math"].inputs[0]')
    b = _Socket(0.0, name="Value", path='nodes["Math"].inputs[1]')
    math_node.inputs = [a, b]
    math_node.outputs = _Outputs(_Socket(name="Value"))
    material = _material([math_node], [_driver("frame", 'nodes["Math"].inputs[1].default_value')],
                         _Socket(linked=True, link=_Link(math_node, math_node.outputs[0])))
    expr = core._resolve_socket_value(material.node_tree.nodes["Principled BSDF"].inputs["Roughness"])
    assert _evaluate(expr) == pytest.approx(2.0 * TIME * FPS)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True


def test_a_driven_mix_factor_blends_live():
    """The Mix branch used to read Factor's default directly, so a driven,
    unlinked Factor folded to input A and the driver vanished."""
    mix = _Node(type="MIX", name="Mix", blend_type="MIX", data_type="RGBA", clamp_factor=True, clamp_result=False)
    fac = _Socket(0.0, name="Factor", path='nodes["Mix"].inputs[0]')
    a = _Socket((1.0, 0.0, 0.0, 1.0), name="A", path='nodes["Mix"].inputs[1]', socket_type="RGBA")
    b = _Socket((0.0, 0.0, 1.0, 1.0), name="B", path='nodes["Mix"].inputs[2]', socket_type="RGBA")
    mix.inputs = _Inputs(Factor=fac, A=a, B=b)
    mix.outputs = _Outputs(_Socket(name="Result"))
    material = _material([mix], [_driver("frame / 48 - floor(frame / 48)", 'nodes["Mix"].inputs[0].default_value')],
                         _Socket(linked=True, link=_Link(mix, mix.outputs[0])))
    expr = core._resolve_socket_value(material.node_tree.nodes["Principled BSDF"].inputs["Roughness"], expected_type="color3")
    assert expr["node_id"] == "ND_mix_color3"
    assert _evaluate(expr["inputs"]["mix"]) == pytest.approx((TIME * FPS / 48) % 1.0)
    assert core._is_supported_mix(mix) is True
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is True, result["errors"]


def test_a_driven_vector_math_scale_is_live():
    vmath = _Node(type="VECT_MATH", name="Vector Math", operation="SCALE")
    vector = _Socket((1.0, 2.0, 3.0), name="Vector", path='nodes["Vector Math"].inputs[0]', socket_type="VECTOR")
    scale = _Socket(0.0, name="Scale", path='nodes["Vector Math"].inputs[1]')
    vmath.inputs = _Inputs(Vector=vector, Scale=scale)
    vmath.outputs = _Outputs(_Socket(name="Vector"), _Socket(name="Value"))
    material = _material([vmath], [_driver("frame * 0.5", 'nodes["Vector Math"].inputs[1].default_value')],
                         _Socket(linked=True, link=_Link(vmath, vmath.outputs[0])))
    expr = core._resolve_socket_value(material.node_tree.nodes["Principled BSDF"].inputs["Roughness"], expected_type="vector3")
    assert expr["node_id"] == "ND_multiply_vector3"
    broadcast = expr["inputs"]["in2"]
    assert broadcast["node_id"] == "ND_combine3_vector3"
    assert _evaluate(broadcast["inputs"]["in1"]) == pytest.approx(TIME * FPS * 0.5)
