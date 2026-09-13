"""Evaluate exported MaterialX expressions on numbers, and fake Blender nodes.

Tests that pin a translation against Blender's arithmetic build a small node
graph out of the fakes below, resolve it through the exporter, and evaluate
the resulting expression here with the geometric readers, images and
matrices injected through ``Env``. The expected values come from Python ports
of Cycles' kernel code written inside each test, never from the exporter.

Every node id the evaluator meets must exist in the shipped manifest, so a
translation cannot pass by authoring a nodedef RealityKit does not have.
"""

from __future__ import annotations

import math
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402

MANIFEST_NODES = frozenset(load_manifest()["nodes"])

IDENTITY4 = ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0))
#: The export root's -90 degree turn about X: Blender's Z-up world into
#: RealityKit's Y-up world. An unparented object's model-to-world matrix.
ROOT_ROTATION = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))


def to_realitykit(vector):
    """A Blender world-space vector as RealityKit's world readers return it."""
    x, y, z = vector
    return (x, z, -y)


@dataclass
class Env:
    """The surface point an expression is evaluated at."""

    position_world: tuple = (0.0, 0.0, 0.0)
    position_object: tuple = (0.0, 0.0, 0.0)
    normal_world: tuple = (0.0, 0.0, 1.0)
    normal_object: tuple = (0.0, 0.0, 1.0)
    tangent_world: tuple = (1.0, 0.0, 0.0)
    bitangent_world: tuple = (0.0, 1.0, 0.0)
    texcoord: tuple = (0.0, 0.0)
    #: Row-major 4x4 matrices, applied to column vectors.
    world_to_view: tuple = IDENTITY4
    model_to_world: tuple = IDENTITY4
    #: The platform's model-to-view matrix, a true frame with no anchor. When
    #: unset it is world_to_view times model_to_world, which holds only for an
    #: identity anchor.
    model_to_view: Optional[tuple] = None
    #: path -> function (u, v) -> (r, g, b, a)
    images: Dict[str, Callable[[float, float], tuple]] = field(default_factory=dict)
    #: primvar name -> value
    primvars: Dict[str, Any] = field(default_factory=dict)
    #: The mesh's first colour attribute, RGBA.
    vertex_color: tuple = (0.0, 0.0, 0.0, 1.0)
    #: Signed gradient noise, position -> float. ND_fractal3d sums octaves of
    #: it; tests inject the same primitive into their Cycles port so the
    #: octave count, amplitudes and normalisation are compared exactly.
    perlin: Callable[[tuple], float] = lambda p: math.sin(1.7 * p[0] + 2.3 * p[1] + 0.9 * p[2])
    #: Cell noise distance, (position, jitter) -> float, for ND_worleynoise3d.
    worley: Callable[[tuple, float], float] = lambda p, jitter: abs(math.sin(p[0] * p[1] + p[2])) * (0.5 + 0.5 * jitter)


# --------------------------------------------------------------------------
# fake Blender nodes
# --------------------------------------------------------------------------

class Socket:
    def __init__(self, name, value=None, *, link=None, enabled=True, socket_type=None, identifier=None):
        self.name = name
        self.identifier = identifier or name
        self.default_value = value
        self.enabled = enabled
        self.is_linked = link is not None
        self.links = [link] if link is not None else []
        if socket_type is not None:
            self.type = socket_type


class Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class Inputs(list):
    def get(self, name):
        # Blender 5.2 answers a shared socket name (Mix's A, Map Range's
        # From Min) with the socket of the node's current data type, and a
        # name or an identifier alike.
        for socket in self:
            if name in (socket.name, socket.identifier) and getattr(socket, "enabled", True):
                return socket
        return None

    def items(self):
        return [(socket.name, socket) for socket in self if getattr(socket, "enabled", True)]


class Node:
    def __init__(self, node_type, name=None, inputs=None, outputs=("Color",), **attributes):
        self.type = node_type
        self.name = name or node_type.title()
        self.inputs = Inputs()
        for socket_name, value in (inputs or {}).items():
            if isinstance(value, Link):
                self.inputs.append(Socket(socket_name, link=value))
            elif isinstance(value, Socket):
                self.inputs.append(value)
            else:
                self.inputs.append(Socket(socket_name, value))
        self.outputs = Inputs(Socket(name) for name in outputs)
        for key, value in attributes.items():
            setattr(self, key, value)

    def out(self, name=None):
        socket = self.outputs.get(name) if name else self.outputs[0]
        return Link(self, socket)


def resolve(node, output=None, expected_type="color3"):
    link = node.out(output)
    link.from_socket.is_linked = True
    return core._resolve_socket_value(Socket("Target", link=link), expected_type=expected_type)


def vector_value(vector):
    """A linked vector that resolves to a node expression, not a constant."""
    return Node("VECT_MATH", "Vector Math", {"A": tuple(vector), "B": (0.0, 0.0, 0.0), "C": (0.0, 0.0, 0.0), "Scale": 1.0},
                outputs=("Vector", "Value"), operation="ADD").out("Vector")


def float_value(value):
    """A linked float that resolves to a node expression, not a constant."""
    return Node("VECT_MATH", "Vector Math", {"A": (value, 0.0, 0.0), "B": (1.0, 0.0, 0.0), "C": (0.0, 0.0, 0.0), "Scale": 1.0},
                outputs=("Vector", "Value"), operation="DOT_PRODUCT").out("Value")


# --------------------------------------------------------------------------
# evaluator
# --------------------------------------------------------------------------

# RealityKit's arithmetic where it differs from Cycles, from the MaterialX
# 1.39 metallib RealityKit ships (ShaderGraph.framework) and GPU measurement:
# division, sqrt, pow, log, asin and atan2 are the bare functions, which give
# inf or NaN where Cycles' safe_* guards return 0, and round is Metal's
# fast_round, half away from zero.

def _realitykit_divide(x, y):
    if y:
        return x / y
    return math.copysign(math.inf, x) if x else math.nan


def _realitykit_pow(x, y):
    # A negative base is NaN for a fractional exponent on the GPU. The
    # exporter never relies on pow with a negative base, so every negative
    # base is NaN here and a translation that does rely on one fails.
    if x < 0:
        return math.nan
    if x == 0 and y < 0:
        return math.inf
    return x ** y


def _realitykit_log(x):
    if x > 0:
        return math.log(x)
    return -math.inf if x == 0 else math.nan


def _finite_or(function):
    """An integer-valued function that passes inf and NaN through, as the GPU does."""
    return lambda x: float(function(x)) if math.isfinite(x) else x


def _realitykit_round(x):
    return math.copysign(math.floor(abs(x) + 0.5), x)


#: 1/360 as the half-precision literal (0xH19B0) RealityKit's rgbtohsv
#: multiplies by. The colour nodes compute in half precision.
_HALF_ONE_THREE_SIXTIETH = 0.002777099609375


def _realitykit_rgbtohsv(rgb):
    """ND_rgbtohsv_color3 as disassembled: no wrap of a negative hue, and a hue
    computed wherever the channels differ."""
    r, g, b = rgb
    high, low = max(r, g, b), min(r, g, b)
    delta = high - low
    if delta == 0:
        h = 0.0
    elif high == r:
        h = (g - b) * 60.0 / delta
    elif high == g:
        h = ((b - r) / delta + 2.0) * 60.0
    else:
        h = ((r - g) / delta + 4.0) * 60.0
    s = 0.0 if high == 0 else delta / high
    return (h * _HALF_ONE_THREE_SIXTIETH, s, high)


def _realitykit_hsvtorgb(hsv):
    """ND_hsvtorgb_color3 as disassembled: v - v*s*max(0, min(k, 4-k, 1)),
    k = fmod(6h + (5, 3, 1), 6)."""
    h, s, v = hsv
    out = []
    for n in (5.0, 3.0, 1.0):
        k = math.fmod(h * 6.0 + n, 6.0)
        out.append(v - v * s * max(0.0, min(k, 4.0 - k, 1.0)))
    return tuple(out)


def _vec(value):
    return tuple(float(x) for x in value)


def _broadcast(a, n):
    return a if isinstance(a, tuple) else (float(a),) * n


def _mat_apply(matrix, point):
    x, y, z = point
    return tuple(row[0] * x + row[1] * y + row[2] * z + row[3] for row in matrix[:3])


def _mat_mul(a, b):
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)) for r in range(4))


def _convert(value, to_type):
    sizes = {"float": 1, "vector2": 2, "vector3": 3, "color3": 3, "vector4": 4, "color4": 4}
    if isinstance(value, bool):
        value = float(value)
    size = sizes[to_type]
    if not isinstance(value, tuple):
        return float(value) if size == 1 else (float(value),) * size
    if size == 1:
        return value[0]
    if len(value) >= size:
        return value[:size]
    return value + (1.0,) * (size - len(value)) if size == 4 and len(value) == 3 else value + (0.0,) * (size - len(value))


def evaluate(expr, env: Optional[Env] = None):
    env = env or Env()
    kind = expr.get("kind")
    if kind == "constant":
        value = expr["value"]
        if isinstance(value, str):
            return value
        return _vec(value) if isinstance(value, (list, tuple)) else float(value)
    if kind == "file_asset":
        return expr["path"]
    assert kind == "node", expr
    node_id = expr["node_id"]
    assert node_id in MANIFEST_NODES, f"{node_id} is not in the shipped manifest"
    inputs = {name: evaluate(value, env) for name, value in expr.get("inputs", {}).items()}
    output = expr.get("output") or "out"
    body = node_id[3:]

    readers = {
        "texcoord_vector2": lambda: _vec(env.texcoord),
        "texcoord_vector3": lambda: _vec(env.texcoord) + (0.0,),
        "realitykit_surface_world_to_view": lambda: env.world_to_view,
        "realitykit_surface_model_to_world": lambda: env.model_to_world,
        "realitykit_surface_model_to_view": lambda: env.model_to_view or _mat_mul(env.world_to_view, env.model_to_world),
    }
    if body in readers:
        return readers[body]()
    if body == "position_vector3":
        return _vec(env.position_world if inputs.get("space") == "world" else env.position_object)
    if body == "tangent_vector3":
        return _vec(env.tangent_world)
    if body == "bitangent_vector3":
        return _vec(env.bitangent_world)
    if body == "normal_vector3":
        return _vec(env.normal_world if inputs.get("space") == "world" else env.normal_object)
    if body.startswith("geomcolor_"):
        rgba = _vec(env.vertex_color)
        return rgba[:3] if body == "geomcolor_color3" else rgba
    if body.startswith("geompropvalue_"):
        value = env.primvars.get(inputs["geomprop"], inputs.get("default"))
        return _vec(value) if isinstance(value, (list, tuple)) else float(value)
    if body.startswith("image_"):
        u, v = inputs["texcoord"]
        rgba = _vec(env.images[inputs["file"]](u, v))
        return rgba[:3] if body == "image_color3" else rgba
    if body == "transformmatrix_vector3M4":
        return _mat_apply(inputs["mat"], inputs["in"])

    name, _, type_part = body.partition("_")
    if name.startswith("convert"):
        return _convert(inputs["in"], type_part.split("_")[-1])
    if name in ("combine2", "combine3", "combine4"):
        return tuple(float(inputs[f"in{i}"]) for i in range(1, int(name[-1]) + 1))
    if name in ("ifgreater", "ifequal", "ifgreatereq"):
        test = {"ifgreater": inputs["value1"] > inputs["value2"],
                "ifequal": inputs["value1"] == inputs["value2"],
                "ifgreatereq": inputs["value1"] >= inputs["value2"]}[name]
        return inputs["in1"] if test else inputs["in2"]
    if name == "mix":
        fg, bg, t = inputs["fg"], inputs["bg"], inputs["mix"]
        if isinstance(fg, tuple) or isinstance(bg, tuple):
            n = len(fg) if isinstance(fg, tuple) else len(bg)
            return tuple(b + (f - b) * w for f, b, w in zip(_broadcast(fg, n), _broadcast(bg, n), _broadcast(t, n)))
        return bg + (fg - bg) * t
    if body == "rgbtohsv_color3":
        return _realitykit_rgbtohsv(inputs["in"])
    if body == "hsvtorgb_color3":
        return _realitykit_hsvtorgb(inputs["in"])
    if body == "fractal3d_float":
        # MaterialX's fBm: octave i samples the noise at position * lacunarity^i
        # with amplitude diminish^i, and the sum is scaled by amplitude.
        total, amplitude, position = 0.0, 1.0, inputs["position"]
        for _ in range(int(inputs["octaves"])):
            total += env.perlin(position) * amplitude
            amplitude *= inputs["diminish"]
            position = tuple(x * inputs["lacunarity"] for x in position)
        return total * inputs["amplitude"]
    if body == "worleynoise3d_float":
        return env.worley(inputs["position"], inputs.get("jitter", 1.0))
    if name == "atan2":
        y, x = inputs["iny"], inputs["inx"]
        return math.nan if x == 0.0 and y == 0.0 else math.atan2(y, x)
    if name == "clamp":
        low, high = inputs.get("low", 0.0), inputs.get("high", 1.0)
        value = inputs["in"]
        if isinstance(value, tuple):
            return tuple(min(max(x, _broadcast(low, len(value))[i]), _broadcast(high, len(value))[i]) for i, x in enumerate(value))
        return min(max(value, low), high)
    if name == "dotproduct":
        return sum(x * y for x, y in zip(inputs["in1"], inputs["in2"]))
    if name == "crossproduct":
        a, b = inputs["in1"], inputs["in2"]
        return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    if name == "magnitude":
        return math.sqrt(sum(x * x for x in inputs["in"]))
    if name == "normalize":
        length = math.sqrt(sum(x * x for x in inputs["in"]))
        return tuple(x / length for x in inputs["in"]) if length else (math.nan,) * len(inputs["in"])
    if name == "oneminus" or body.startswith("realitykit_oneminus"):
        value = inputs["in"]
        return tuple(1.0 - x for x in value) if isinstance(value, tuple) else 1.0 - value

    def _modulo(x, y):
        return x - y * math.floor(x / y) if y else math.nan

    binary = {
        "add": lambda x, y: x + y, "subtract": lambda x, y: x - y, "multiply": lambda x, y: x * y,
        "divide": _realitykit_divide, "power": _realitykit_pow,
        "min": min, "max": max, "modulo": _modulo,
    }
    unary = {
        "absval": abs, "floor": _finite_or(math.floor), "ceil": _finite_or(math.ceil), "sin": math.sin, "cos": math.cos,
        "tan": math.tan, "exp": math.exp,
        "sqrt": lambda x: math.sqrt(x) if x >= 0 else math.nan, "sign": lambda x: float((x > 0) - (x < 0)),
        "round": _realitykit_round, "ln": _realitykit_log,
        "asin": lambda x: math.asin(x) if -1.0 <= x <= 1.0 else math.nan,
        "acos": lambda x: math.acos(x) if -1.0 <= x <= 1.0 else math.nan,
    }
    if name in binary:
        a, b = inputs["in1"], inputs["in2"]
        if isinstance(a, tuple) or isinstance(b, tuple):
            n = len(a) if isinstance(a, tuple) else len(b)
            return tuple(binary[name](x, y) for x, y in zip(_broadcast(a, n), _broadcast(b, n)))
        return binary[name](a, b)
    if name in unary:
        value = inputs["in"]
        return tuple(unary[name](x) for x in value) if isinstance(value, tuple) else unary[name](value)
    if body == "atan2_float":
        return math.atan2(inputs["iny"], inputs["inx"])
    if body == "acos_float":
        return math.acos(inputs["in"]) if -1.0 <= inputs["in"] <= 1.0 else math.nan
    if body == "normal_map_decode":
        # 2 * in - 1, read from RealityKit's shipped implementation.
        return tuple(2.0 * x - 1.0 for x in inputs["in"])
    raise AssertionError(f"evaluator lacks {node_id} ({output})")
