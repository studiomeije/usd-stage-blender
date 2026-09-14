"""Procedural textures must sample spatially varying coordinates.

Measured defect: a Noise/Voronoi/Gradient texture with its Vector unwired
exported successfully with no warning, but the authored position/texcoord was
the socket default constant (0, 0, 0) - the pattern was sampled at a single
point and rendered flat. ``_expr_from_socket`` folds an unlinked socket to its
default value, so the intended ``_default_texcoord_expr`` fallback was dead
code.

Blender samples an unwired Vector with Generated coordinates
(object-bounding-box normalized). The manifest's runtime-resolvable stand-in
is object-space position (ND_position_vector3); the deliberate approximation
is named by an always-on warning in ``collect_material_warnings``.

Do not add a check here that every authored node_id is a manifest key. One
lived here and could not fail: ``_nodedef_for`` only ever returns names that
already survived a manifest lookup, so the assertion compared the manifest
with itself and would have stayed green on a manifest that was wrong about
what RealityKit accepts. The real check is
``tests/integration/test_authored_nodedefs_exist.py``, which reads the ids
back out of an exported stage - including the ones textures.py builds from
f-strings and never routes through the manifest at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import math
import struct

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402


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


def _resolve_from(node, output_name="Fac", expected_type="float"):
    output = _Socket(name=output_name)
    target = _Socket(linked=True, link=_Link(node, output))
    return core._resolve_socket_value(target, expected_type=expected_type)


def _noise_node(vector_socket=None):
    node = _Node()
    node.type = "TEX_NOISE"
    node.name = "Noise Texture"
    node.inputs = {
        "Vector": vector_socket or _Socket([0.0, 0.0, 0.0], name="Vector"),
        "Scale": _Socket(7.0),
        "Detail": _Socket(2.0),
        "Roughness": _Socket(0.5),
        "Distortion": _Socket(0.0),
    }
    return node


def _voronoi_node(scale=4.0):
    node = _Node()
    node.type = "TEX_VORONOI"
    node.name = "Voronoi Texture"
    node.inputs = {
        "Vector": _Socket([0.0, 0.0, 0.0], name="Vector"),
        "Scale": _Socket(scale),
        "Randomness": _Socket(1.0),
    }
    return node


def _gradient_node():
    node = _Node()
    node.type = "TEX_GRADIENT"
    node.name = "Gradient Texture"
    node.inputs = {"Vector": _Socket([0.0, 0.0, 0.0], name="Vector")}
    return node


def _assert_is_object_position(expr):
    assert isinstance(expr, dict) and expr.get("kind") == "node", expr
    assert expr["node_id"] == "ND_position_vector3"
    assert expr["inputs"]["space"] == {"kind": "constant", "value": "object"}


def _find(expr, node_id):
    """Every node expression with ``node_id`` under ``expr``."""
    found = []
    if isinstance(expr, dict):
        if expr.get("node_id") == node_id:
            found.append(expr)
        for child in (expr.get("inputs") or {}).values():
            found.extend(_find(child, node_id))
    return found


def test_noise_unwired_vector_authors_position_not_a_constant():
    expr = _resolve_from(_noise_node())
    assert expr["kind"] == "node"
    # fractal3d, not unifiednoise3d: the latter exists only in RealityKit's
    # MaterialX 1.39 store while this profile declares 1.38, where a missing
    # nodedef silently costs the material its whole shader graph. Measured with
    # realitytool compile: the old shape produced 0 shadergraphs and 1 PBR
    # fallback; this one produces 1 and 0.
    fractals = _find(expr, "ND_fractal3d_float")
    assert fractals
    # fractal3d has no frequency input, so Scale folds into the sample position
    # exactly as it does for Voronoi below.
    position = fractals[0]["inputs"]["position"]
    assert position["node_id"] == "ND_multiply_vector3"
    _assert_is_object_position(position["inputs"]["in1"])
    assert position["inputs"]["in2"] == {"kind": "constant", "value": (7.0, 7.0, 7.0)}


def test_voronoi_unwired_vector_authors_scaled_position():
    expr = _resolve_from(_voronoi_node(scale=4.0), output_name="Distance")
    assert expr["node_id"] == "ND_worleynoise3d_float"
    position = expr["inputs"]["position"]
    # Blender applies Scale to the sample position; worleynoise3d has no
    # frequency input, so it must be an explicit multiply.
    assert position["node_id"] == "ND_multiply_vector3"
    _assert_is_object_position(position["inputs"]["in1"])
    assert position["inputs"]["in2"] == {"kind": "constant", "value": (4.0, 4.0, 4.0)}


def test_voronoi_unit_scale_skips_the_multiply():
    expr = _resolve_from(_voronoi_node(scale=1.0), output_name="Distance")
    _assert_is_object_position(expr["inputs"]["position"])


def test_gradient_unwired_vector_authors_position():
    expr = _resolve_from(_gradient_node(), output_name="Factor")
    readers = _find(expr, "ND_position_vector3")
    assert readers
    for reader in readers:
        _assert_is_object_position(reader)


def test_unresolvable_wired_vector_still_refuses():
    bad = _Node()
    bad.type = "LIGHT_PATH"
    bad.name = "Light Path"
    bad.inputs = {}
    bad_output = _Socket(name="Is Camera Ray")
    vector_socket = _Socket(linked=True, link=_Link(bad, bad_output), name="Vector")
    expr = _resolve_from(_noise_node(vector_socket=vector_socket))
    assert expr["kind"] == "unresolved"


# --- Gradient Texture, exact ---------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402


def _cycles_gradient(p, kind):
    """``svm_gradient`` then ``saturatef`` (intern/cycles/kernel/svm/gradient.h)."""
    x, y, z = p
    if kind == "LINEAR":
        f = x
    elif kind == "QUADRATIC":
        r = max(x, 0.0)
        f = r * r
    elif kind == "EASING":
        r = min(max(x, 0.0), 1.0)
        t = r * r
        f = 3.0 * t - 2.0 * t * r
    elif kind == "DIAGONAL":
        f = (x + y) * 0.5
    elif kind == "RADIAL":
        f = math.atan2(y, x) / (2.0 * math.pi) + 0.5
    else:
        r = max(0.999999 - math.sqrt(x * x + y * y + z * z), 0.0)
        f = r * r if kind == "QUADRATIC_SPHERE" else r
    return min(max(f, 0.0), 1.0)


_GRADIENT_POINTS = [
    (0.3, 0.2, -0.1), (-0.4, 0.7, 0.2), (1.4, -0.2, 0.5), (0.1, 0.1, 0.1),
    (0.0, 0.0, 0.0), (0.6, 0.0, 0.0), (-0.3, -0.9, 0.4),
]


@pytest.mark.parametrize("kind", ["LINEAR", "QUADRATIC", "EASING", "DIAGONAL", "SPHERICAL", "QUADRATIC_SPHERE", "RADIAL"])
@pytest.mark.parametrize("output", ["Factor", "Color"])
def test_gradient_texture_matches_cycles_for_every_type(kind, output):
    """Every type used to export as the Linear ramp."""
    for point in _GRADIENT_POINTS:
        node = mx_eval.Node(
            "TEX_GRADIENT", "Gradient Texture",
            {"Vector": mx_eval.vector_value(point)},
            outputs=("Factor", "Color"), gradient_type=kind,
        )
        expected_type = "float" if output == "Factor" else "color3"
        got = mx_eval.evaluate(mx_eval.resolve(node, output, expected_type=expected_type))
        want = _cycles_gradient(point, kind)
        if output == "Color":
            assert got == pytest.approx((want, want, want), abs=1e-6), (kind, point)
        else:
            assert got == pytest.approx(want, abs=1e-6), (kind, point)


def test_a_linked_gradient_does_not_warn_and_an_unlinked_one_names_the_coordinate():
    warnings = _collect_warnings_for_node_types(["TEX_GRADIENT"])
    assert len(warnings) == 1 and "Generated coordinates" in warnings[0], warnings
    assert "pixel-for-pixel" not in warnings[0]


# --- Noise Texture: Blender's octaves and range ------------------------------

def _cycles_noise_fbm(p, detail, roughness, lacunarity, normalize, snoise):
    """``noise_fbm`` for float3 (intern/cycles/kernel/svm/fractal_noise.h), with
    ``svm_node_tex_noise``'s clamps, over an injected signed noise."""
    detail = min(max(detail, 0.0), 15.0)
    roughness = max(roughness, 0.0)
    fscale, amp, maxamp, total = 1.0, 1.0, 0.0, 0.0
    for _ in range(int(detail) + 1):
        t = snoise(tuple(fscale * c for c in p))
        total += t * amp
        maxamp += amp
        amp *= roughness
        fscale *= lacunarity
    rmd = detail - math.floor(detail)
    if rmd != 0.0:
        t = snoise(tuple(fscale * c for c in p))
        sum2 = total + t * amp
        if normalize:
            a, b = 0.5 * total / maxamp + 0.5, 0.5 * sum2 / (maxamp + amp) + 0.5
        else:
            a, b = total, sum2
        return a + rmd * (b - a)
    return 0.5 * total / maxamp + 0.5 if normalize else total


def _noise(detail, roughness, lacunarity=2.0, scale=1.5, normalize=True, **settings):
    return mx_eval.Node(
        "TEX_NOISE", "Noise Texture",
        {
            "Vector": mx_eval.vector_value((0.4, -0.3, 1.1)),
            "Scale": scale, "Detail": detail, "Roughness": roughness,
            "Lacunarity": lacunarity, "Distortion": settings.pop("distortion", 0.0),
        },
        outputs=("Factor", "Color"),
        noise_dimensions=settings.pop("dimensions", "3D"),
        noise_type=settings.pop("noise_type", "FBM"),
        normalize=normalize,
    )


@pytest.mark.parametrize(("detail", "roughness", "lacunarity", "normalize"), [
    (2.0, 0.5, 2.0, True),     # Blender's defaults: 3 octaves, not 2
    (0.0, 0.5, 2.0, True),     # one octave
    (3.6, 0.7, 1.8, True),     # a fractional detail blends a fifth octave in
    (2.0, 0.5, 2.0, False),    # unnormalised: the raw sum
    (20.0, -1.0, 2.0, True),   # detail clamps to 15, roughness to 0
])
def test_noise_texture_has_blenders_octaves_and_normalisation(detail, roughness, lacunarity, normalize):
    """ND_fractal3d used to get octaves = Detail and a zero-centred sum, so the
    exported value sat in [-1, 1] around 0 where Blender's sits around 0.5."""
    env = mx_eval.Env()
    node = _noise(detail, roughness, lacunarity, normalize=normalize)
    got = mx_eval.evaluate(mx_eval.resolve(node, "Factor", expected_type="float"), env)
    point = tuple(1.5 * c for c in (0.4, -0.3, 1.1))
    want = _cycles_noise_fbm(point, detail, roughness, lacunarity, normalize, env.perlin)
    assert got == pytest.approx(want, abs=1e-9)


def _cycles_hash_uint2(kx, ky):
    """Cycles' ``hash_uint2`` (intern/cycles/util/hash.h), transcribed separately."""
    def rot(x, k):
        return ((x << k) | (x >> (32 - k))) & 0xFFFFFFFF
    a = b = c = (0xDEADBEEF + (2 << 2) + 13) & 0xFFFFFFFF
    b = (b + ky) & 0xFFFFFFFF
    a = (a + kx) & 0xFFFFFFFF
    for x, y, k, target in (("b", "c", 14, "c"), ("c", "a", 11, "a"), ("a", "b", 25, "b"),
                            ("b", "c", 16, "c"), ("c", "a", 4, "a"), ("a", "b", 14, "b"),
                            ("b", "c", 24, "c")):
        values = {"a": a, "b": b, "c": c}
        values[target] ^= values[x]
        values[target] = (values[target] - rot(values[x], k)) & 0xFFFFFFFF
        a, b, c = values["a"], values["b"], values["c"]
    return c


def _float_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _random_float3_offset(seed):
    """``random_float3_offset`` (intern/cycles/kernel/svm/noisetex.h)."""
    return tuple(
        100.0 + (_cycles_hash_uint2(_float_bits(seed), _float_bits(float(i))) / 4294967296.0) * 100.0
        for i in range(3)
    )


def test_noise_color_is_three_decorrelated_evaluations_at_blenders_offsets():
    env = mx_eval.Env()
    node = _noise(2.0, 0.5)
    got = mx_eval.evaluate(mx_eval.resolve(node, "Color", expected_type="color3"), env)
    point = tuple(1.5 * c for c in (0.4, -0.3, 1.1))
    want = [_cycles_noise_fbm(point, 2.0, 0.5, 2.0, True, env.perlin)]
    for seed in (3.0, 4.0):
        offset = _random_float3_offset(seed)
        want.append(_cycles_noise_fbm(tuple(p + o for p, o in zip(point, offset)), 2.0, 0.5, 2.0, True, env.perlin))
    assert got == pytest.approx(tuple(want), abs=1e-4)
    assert len({round(v, 6) for v in want}) == 3


@pytest.mark.parametrize(("settings", "fragment"), [
    ({"dimensions": "4D"}, "4D"),
    ({"noise_type": "RIDGED_MULTIFRACTAL"}, "RIDGED_MULTIFRACTAL"),
    ({"distortion": 0.5}, "Distortion"),
])
def test_noise_settings_without_blenders_distribution_are_refused(settings, fragment):
    node = _noise(2.0, 0.5, **settings)
    expr = mx_eval.resolve(node, "Factor", expected_type="float")
    assert expr["kind"] == "unresolved" and fragment in expr["reason"]
    assert fragment in core.noise_texture_refusal(node)


def test_a_linked_noise_detail_is_refused():
    node = _noise(mx_eval.float_value(2.0), 0.5)
    expr = mx_eval.resolve(node, "Factor", expected_type="float")
    assert expr["kind"] == "unresolved" and "Detail" in expr["reason"]


# --- Voronoi Texture: only what worleynoise3d computes -----------------------

def _voronoi(**settings):
    outputs = ("Distance", "Color", "Position")
    return mx_eval.Node(
        "TEX_VORONOI", "Voronoi Texture",
        {
            "Vector": mx_eval.vector_value((0.4, -0.3, 1.1)),
            "Scale": 2.0, "Detail": settings.pop("detail", 0.0), "Roughness": 0.5,
            "Randomness": settings.pop("randomness", 1.0),
        },
        outputs=outputs,
        voronoi_dimensions=settings.pop("dimensions", "3D"),
        feature=settings.pop("feature", "F1"),
        distance=settings.pop("distance", "EUCLIDEAN"),
        normalize=settings.pop("normalize", False),
    )


@pytest.mark.parametrize(("output", "settings", "fragment"), [
    ("Color", {}, "Color output"),
    ("Position", {}, "Position output"),
    ("Distance", {"feature": "F2"}, "F2"),
    ("Distance", {"distance": "MANHATTAN"}, "MANHATTAN"),
    ("Distance", {"dimensions": "2D"}, "2D"),
    ("Distance", {"detail": 2.0}, "Detail"),
])
def test_voronoi_settings_worleynoise_cannot_compute_are_refused(output, settings, fragment):
    """Color and Position used to export the distance, and feature, metric and
    dimensions were ignored."""
    node = _voronoi(**settings)
    expr = mx_eval.resolve(node, output, expected_type="float")
    assert expr["kind"] == "unresolved" and fragment in expr["reason"], expr


def test_voronoi_normalize_divides_by_cycles_largest_distance():
    env = mx_eval.Env()
    node = _voronoi(normalize=True, randomness=1.7)
    got = mx_eval.evaluate(mx_eval.resolve(node, "Distance", expected_type="float"), env)
    randomness = 1.0  # svm_node_tex_voronoi clamps Randomness to [0, 1]
    position = tuple(2.0 * c for c in (0.4, -0.3, 1.1))
    m = 0.5 + 0.5 * randomness
    assert got == pytest.approx(env.worley(position, randomness) / math.sqrt(3 * m * m), abs=1e-9)
def test_noise_and_voronoi_always_warn():
    warnings = _collect_warnings_for_node_types(
        ["TEX_NOISE", "TEX_VORONOI"]
    )
    procedural = [w for w in warnings if "pixel-for-pixel" in w]
    assert len(procedural) == 2, warnings
    # The sweep's capability-noise contract must not be violated by the
    # intentional-approximation warning.
    for warning in procedural:
        lowered = warning.lower()
        for term in ("unrecognized", "requires baking", "limited support"):
            assert term not in lowered, warning


class _TreeNode:
    def __init__(self, node_type, name, inputs=()):
        self.type = node_type
        self.name = name
        self.inputs = list(inputs)


class _OutputInputs:
    def __init__(self, surface_socket):
        self._surface_socket = surface_socket

    def get(self, name):
        return self._surface_socket if name == "Surface" else None


def _collect_warnings_for_node_types(node_types):
    surface = _TreeNode("BSDF_PRINCIPLED", "Principled BSDF")
    upstream = [
        _TreeNode(node_type, f"{node_type.title()} {index}")
        for index, node_type in enumerate(node_types)
    ]
    surface.inputs = [
        SimpleNamespace(is_linked=True, links=[SimpleNamespace(from_node=node)])
        for node in upstream
    ]

    output = _TreeNode("OUTPUT_MATERIAL", "Material Output")
    output.is_active_output = True
    output.inputs = _OutputInputs(
        SimpleNamespace(is_linked=True, links=[SimpleNamespace(from_node=surface)])
    )

    material = SimpleNamespace(
        name="ProceduralWarnings",
        node_tree=SimpleNamespace(
            nodes=[surface, *upstream, output],
            links=[],
        ),
    )
    return core.collect_material_warnings(material)


def test_the_validator_refuses_what_the_resolver_refuses():
    from Plugin.nodes import validate

    def warnings_for(node):
        node.mute = False
        material = SimpleNamespace(name="M", node_tree=SimpleNamespace(nodes=[node], links=()))
        result = validate.validate_material(material, only_connected=False)
        return [issue["message"] for issue in result["warnings"] + result["errors"]]

    assert any("Distortion" in m for m in warnings_for(_noise(2.0, 0.5, distortion=0.3)))
    assert not any("Noise Texture" in m for m in warnings_for(_noise(2.0, 0.5)))
    colour_voronoi = _voronoi()
    colour_voronoi.outputs.get("Color").is_linked = True
    assert any("Color output" in m for m in warnings_for(colour_voronoi))
    distance_voronoi = _voronoi()
    distance_voronoi.outputs.get("Distance").is_linked = True
    assert not any("Voronoi Texture" in m for m in warnings_for(distance_voronoi))
