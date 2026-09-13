"""Blender Vector Math -> MaterialX vector nodes, checked on values.

Every supported operation is evaluated through the authored expression tree
and compared with a Python port of Cycles' vector_math.h, so operand order,
normalisation of the second vector in reflect/refract, the project guard and
the GLSL faceforward argument order all fail on numbers, not on node names.
"""

from __future__ import annotations

import math
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

_MANIFEST = load_manifest()


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


def _vmath(operation, a, b=(0.0, 0.0, 0.0), c=(0.0, 0.0, 0.0), scale=1.0):
    node = _Node()
    node.type = "VECT_MATH"
    node.name = "Vector Math"
    node.operation = operation
    node.inputs = [_Socket(a, name="Vector"), _Socket(b, name="Vector"), _Socket(c, name="Vector"), _Socket(scale, name="Scale")]
    node.outputs = [_Socket(name="Vector"), _Socket(name="Value")]
    return node


def _resolve(node, output):
    socket = next(s for s in node.outputs if s.name == output)
    socket.is_linked = True
    return core._resolve_socket_value(_Socket(linked=True, link=_Link(node, socket)))


# --- RealityKit's arithmetic -------------------------------------------------
#
# The shared evaluator models RealityKit where it differs from Cycles: bare
# division, sqrt and pow (inf or NaN where Cycles guards to 0), normalize of a
# zero vector is NaN, and round goes half away from zero. A translation that
# forgets a Cycles guard fails on numbers here.

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402


def _evaluate(expr):
    return mx_eval.evaluate(expr)


# --- Cycles, ported (svm_vector_math in intern/cycles/kernel/svm/math_util.h,
# and intern/cycles/util/math_float3.h / math_base.h) --------------------------

def _dot(a, b): return sum(x * y for x, y in zip(a, b))
def _len(a): return math.sqrt(_dot(a, a))
def _cw(f, *args): return tuple(f(*p) for p in zip(*args))


def _safe_normalize(a):
    t = _len(a)
    return tuple(x * (1.0 / t) for x in a) if t != 0.0 else a


def _safe_divide(x, y):
    return x / y if y != 0.0 else 0.0


def _compatible_powf(x, y):
    if y == 0.0:
        return 1.0
    if x == 0.0:
        return 0.0
    if x < 0.0:
        # CPU powf: a negative base with an integer exponent is well defined.
        return (-x) ** y if math.fmod(-y, 2.0) == 0.0 else -((-x) ** y)
    return x ** y


def _safe_powf(a, b):
    if a < 0.0 and b != float(int(b)):
        return 0.0
    return _compatible_powf(a, b)


def _cycles(op, a, b, c, scale):
    if op == "ADD": return _cw(lambda x, y: x + y, a, b)
    if op == "SUBTRACT": return _cw(lambda x, y: x - y, a, b)
    if op == "MULTIPLY": return _cw(lambda x, y: x * y, a, b)
    if op == "DIVIDE": return _cw(_safe_divide, a, b)
    if op == "MULTIPLY_ADD": return _cw(lambda x, y, z: x * y + z, a, b, c)
    if op == "CROSS_PRODUCT": return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
    if op == "PROJECT":
        ls = _dot(b, b); return tuple((_dot(a, b) / ls) * y for y in b) if ls != 0 else (0.0, 0.0, 0.0)
    if op == "REFLECT":
        n = _safe_normalize(b); d = _dot(a, n); return tuple(x - 2 * d * y for x, y in zip(a, n))
    if op == "REFRACT":
        n = _safe_normalize(b); eta = scale; ci = _dot(n, a); k = 1 - eta * eta * (1 - ci * ci)
        return (0.0, 0.0, 0.0) if k < 0 else tuple(eta * x - (eta * ci + math.sqrt(k)) * y for x, y in zip(a, n))
    if op == "FACEFORWARD": return a if _dot(c, b) < 0 else tuple(-x for x in a)
    if op == "DOT_PRODUCT": return _dot(a, b)
    if op == "DISTANCE": return _len(_cw(lambda x, y: x - y, a, b))
    if op == "LENGTH": return _len(a)
    if op == "SCALE": return tuple(x * scale for x in a)
    if op == "NORMALIZE": return _safe_normalize(a)
    if op == "ABSOLUTE": return _cw(abs, a)
    if op == "POWER": return _cw(_safe_powf, a, b)
    if op == "SIGN": return _cw(lambda x: float((x > 0) - (x < 0)), a)
    if op == "MINIMUM": return _cw(min, a, b)
    if op == "MAXIMUM": return _cw(max, a, b)
    if op == "ROUND": return _cw(lambda x: math.floor(x + 0.5), a)
    if op == "FLOOR": return _cw(math.floor, a)
    if op == "CEIL": return _cw(math.ceil, a)
    if op == "FRACTION": return _cw(lambda x: x - math.floor(x), a)
    if op == "SINE": return _cw(math.sin, a)
    if op == "COSINE": return _cw(math.cos, a)
    if op == "TANGENT": return _cw(math.tan, a)
    raise AssertionError(op)


A = (0.3, -1.25, 2.0)
B = (0.5, 0.75, -0.2)
C = (1.0, 0.1, 0.4)


@pytest.mark.parametrize("op", sorted(validate.SUPPORTED_VECTOR_MATH_OPERATIONS))
def test_vector_math_matches_cycles(op):
    # Operands with a negative component, so POWER exercises safe_powf's
    # negative-base cases (0 for -1.25 ** 0.75, and an integer exponent below).
    node = _vmath(op, A, B, C, scale=1.3)
    output = "Value" if op in ("DOT_PRODUCT", "DISTANCE", "LENGTH") else "Vector"
    expr = _resolve(node, output)
    assert expr is not None and expr.get("kind") == "node", (op, expr)
    got = _evaluate(expr)
    want = _cycles(op, A, B, C, 1.3)
    if isinstance(want, tuple):
        assert got == pytest.approx(want, abs=1e-9), op
    else:
        assert got == pytest.approx(want, abs=1e-9), op


@pytest.mark.parametrize(("op", "a", "b", "scale"), [
    # A zero divisor component is 0 for that component.
    ("DIVIDE", (1.0, -2.0, 3.0), (0.0, 4.0, 0.0), 1.0),
    # safe_normalize passes a zero vector through.
    ("NORMALIZE", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0),
    # Reflect and refract about a zero B normalise it safely.
    ("REFLECT", (0.3, -0.4, 0.5), (0.0, 0.0, 0.0), 1.0),
    ("REFRACT", (0.3, -0.4, 0.5), (0.0, 0.0, 0.0), 1.2),
    # A negative base: 0 for a fractional exponent, the signed power for an
    # integer one, 1 for a zero exponent, 0 for a zero base.
    ("POWER", (-2.0, -2.0, -2.0), (0.5, 3.0, 2.0), 1.0),
    ("POWER", (0.0, -3.0, 0.0), (-1.0, 0.0, 0.0), 1.0),
    # Cycles rounds -0.5 up to 0, RealityKit's round would give -1.
    ("ROUND", (-0.5, 0.5, -1.5), (0.0, 0.0, 0.0), 1.0),
])
def test_vector_math_guards_match_cycles(op, a, b, scale):
    expr = _resolve(_vmath(op, a, b, scale=scale), "Vector")
    got = _evaluate(expr)
    want = _cycles(op, a, b, C, scale)
    assert all(math.isfinite(x) for x in got), (op, got)
    assert got == pytest.approx(want, abs=1e-9), op


def test_refract_at_exactly_critical_k_refracts_like_cycles():
    """k == 0: Cycles returns zero only for k < 0, so the grazing ray refracts."""
    # eta = 1 and an incident perpendicular to the normal give k = 1 - 1 * (1 - 0) = 0.
    incident = (1.0, 0.0, 0.0)
    normal = (0.0, 0.0, 1.0)
    want = _cycles("REFRACT", incident, normal, C, 1.0)
    k = 1 - 1.0 * (1 - _dot(normal, incident) ** 2)
    assert k == 0.0 and want != (0.0, 0.0, 0.0)
    expr = _resolve(_vmath("REFRACT", incident, normal, scale=1.0), "Vector")
    assert _evaluate(expr) == pytest.approx(want, abs=1e-9)


def test_project_of_a_zero_vector_is_zero_like_cycles():
    expr = _resolve(_vmath("PROJECT", A, (0.0, 0.0, 0.0)), "Vector")
    assert _evaluate(expr) == (0.0, 0.0, 0.0)


def test_refract_total_internal_reflection_is_zero_like_cycles():
    grazing = (0.99, 0.0, -0.14)
    expr = _resolve(_vmath("REFRACT", grazing, (0.0, 0.0, 1.0), scale=1.8), "Vector")
    assert _evaluate(expr) == (0.0, 0.0, 0.0)
    assert _cycles("REFRACT", grazing, (0.0, 0.0, 1.0), C, 1.8) == (0.0, 0.0, 0.0)


def test_a_float_wired_into_a_vector_socket_broadcasts():
    value = _Node(); value.type = "VALUE"; value.name = "Value"
    value.outputs = {"Value": types.SimpleNamespace(default_value=2.5, name="Value")}
    node = _vmath("ADD", A, B)
    node.inputs[1] = _Socket(linked=True, link=_Link(value, value.outputs["Value"]), name="Vector")
    expr = _resolve(node, "Vector")
    # A Value node folds to a constant, which broadcasts at resolve time; a
    # live float (a reader, a math node) would get an explicit convert.
    assert expr["inputs"]["in2"] == {"kind": "constant", "value": (2.5, 2.5, 2.5)}
    assert _evaluate(expr) == pytest.approx(tuple(x + 2.5 for x in A))

    reader = _Node(); reader.type = "NEW_GEOMETRY"; reader.name = "Geometry"
    reader.outputs = [_Socket(name="Backfacing")]
    live = _vmath("ADD", A, B)
    live.inputs[1] = _Socket(linked=True, link=_Link(reader, reader.outputs[0]), name="Vector")
    reader.outputs[0].is_linked = True
    expr = _resolve(live, "Vector")
    assert expr["inputs"]["in2"]["node_id"] == "ND_convert_float_vector3"


@pytest.mark.parametrize("op", ["MODULO", "WRAP", "SNAP"])
def test_refused_operations_name_their_reason(op):
    node = _vmath(op, A, B, C)
    assert _resolve(node, "Vector")["kind"] == "unresolved"
    assert op not in validate.SUPPORTED_VECTOR_MATH_OPERATIONS
    assert validate.vector_math_refusal_message(op).startswith(f"Vector Math '{op}' requires baking; ")


def test_combine_xyz_builds_a_vector_from_its_sockets():
    node = _Node(); node.type = "COMBXYZ"; node.name = "Combine XYZ"
    class _Inputs(dict):
        def __iter__(self): return iter(self.values())
    node.inputs = _Inputs(X=_Socket(0.25, name="X"), Y=_Socket(-1.0, name="Y"), Z=_Socket(3.0, name="Z"))
    node.outputs = [_Socket(name="Vector")]
    expr = _resolve(node, "Vector")
    assert expr["node_id"] == "ND_combine3_vector3"
    assert _evaluate(expr) == (0.25, -1.0, 3.0)
    assert "COMBXYZ" in validate.SUPPORTED_TYPES and "COMBXYZ" not in validate.BAKE_TYPES


def test_every_authored_nodedef_exists_and_is_scoped():
    seen = set()
    def walk(e):
        if isinstance(e, dict) and e.get("kind") == "node":
            seen.add(e["node_id"]); [walk(v) for v in e["inputs"].values()]
    for op in validate.SUPPORTED_VECTOR_MATH_OPERATIONS:
        walk(_resolve(_vmath(op, A, B, C, scale=1.3), "Value" if op in ("DOT_PRODUCT", "DISTANCE", "LENGTH") else "Vector"))
    assert sorted(n for n in seen if n not in _MANIFEST["nodes"]) == []
    assert len(seen) >= 20


def test_separate_xyz_over_a_vector_math_result_reads_unit_components():
    """t26 rendered flat grey: the Vector Math branch reduced the vector to its
    mean before Separate XYZ could split it, so X, Y and Z all read the mean."""
    node = _vmath("SCALE", A, scale=0.5)
    vector_socket = node.outputs[0]; vector_socket.is_linked = True
    separate = _Node(); separate.type = "SEPXYZ"; separate.name = "Separate XYZ"
    class _Inputs(dict):
        def __iter__(self): return iter(self.values())
    separate.inputs = _Inputs(Vector=_Socket(linked=True, link=_Link(node, vector_socket), name="Vector"))
    separate.outputs = [_Socket(name="X"), _Socket(name="Y"), _Socket(name="Z")]
    masks = {}
    for name in ("X", "Y", "Z"):
        expr = _resolve(separate, name)
        assert expr["node_id"] == "ND_dotproduct_vector3"
        masks[name] = tuple(expr["inputs"]["in2"]["value"])
        assert _evaluate(expr) == pytest.approx(A["XYZ".index(name)] * 0.5)
    assert masks == {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def test_a_vector_math_result_feeding_a_float_socket_takes_the_mean():
    node = _vmath("ADD", A, B)
    vector_socket = node.outputs[0]; vector_socket.is_linked = True
    expr = core._resolve_socket_value(_Socket(linked=True, link=_Link(node, vector_socket)), expected_type="float")
    assert expr["node_id"] == "ND_dotproduct_vector3"
    assert _evaluate(expr) == pytest.approx(sum(x + y for x, y in zip(A, B)) / 3)
