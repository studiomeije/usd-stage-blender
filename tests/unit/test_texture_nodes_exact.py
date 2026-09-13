"""Gamma, Checker, Camera Data, Mapping, computed image reads and Box projection.

Each translation is evaluated on numbers through ``mx_eval`` and compared
with an independent reference:

- Mapping against values baked by Cycles itself: an Emission bake of the
  Mapping output, offset by 10 so negative components survive, into a float
  image, on Blender 5.2.
- Gamma, Checker, Camera Data and Box projection against Python ports of
  Cycles' kernel code (``svm_math_gamma_color``, ``svm_checker``,
  ``svm_node_camera``, ``svm_node_tex_image_box``) written in this file.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval as mx  # noqa: E402
from mx_eval import Env, Node, evaluate, float_value, resolve, vector_value  # noqa: E402

core = mx.core


# --------------------------------------------------------------------------
# Gamma
# --------------------------------------------------------------------------

def _cycles_gamma(color, gamma):
    if gamma == 0.0:
        return (1.0, 1.0, 1.0)
    return tuple(c ** gamma if c > 0.0 else c for c in color)


@pytest.mark.parametrize("color, gamma", [
    ((0.2, 0.5, 0.9), 2.2), ((0.2, -0.5, 0.0), 0.45), ((0.3, 0.6, 0.1), 0.0),
])
@pytest.mark.parametrize("linked", [False, True], ids=["constant", "linked"])
def test_gamma_matches_cycles(color, gamma, linked):
    node = Node("GAMMA", "Gamma", {
        "Color": vector_value(color) if linked else color,
        "Gamma": float_value(gamma) if linked else gamma,
    })
    assert evaluate(resolve(node)) == pytest.approx(_cycles_gamma(color, gamma), abs=1e-9)


# --------------------------------------------------------------------------
# Checker
# --------------------------------------------------------------------------

def _cycles_checker(p):
    p = [(x + 0.000001) * 0.999999 for x in p]
    xi, yi, zi = (abs(int(math.floor(x))) for x in p)
    return 1.0 if ((xi % 2 == yi % 2) == (zi % 2)) else 0.0


def test_checker_fac_and_color_match_cycles():
    rng = random.Random(7)
    color1, color2, scale = (0.9, 0.3, 0.1), (0.05, 0.2, 0.8), 3.5
    for _ in range(200):
        co = tuple(rng.uniform(-2.0, 2.0) for _ in range(3))
        node = Node("TEX_CHECKER", "Checker", {
            "Vector": vector_value(co), "Color1": color1, "Color2": color2, "Scale": scale,
        }, outputs=("Color", "Fac"))
        fac = _cycles_checker(tuple(x * scale for x in co))
        assert evaluate(resolve(node, "Fac", "float")) == fac
        assert evaluate(resolve(node, "Color")) == pytest.approx(color1 if fac == 1.0 else color2)


def test_checker_unit_coordinates_land_like_cycles():
    # The nudge exists for integer coordinates; they must pick Cycles' side.
    for co in [(1.0, 0.0, 0.0), (0.0, 1.0, 1.0), (2.0, -1.0, 0.0), (-1.0, -1.0, -1.0)]:
        node = Node("TEX_CHECKER", "Checker", {
            "Vector": vector_value(co), "Color1": (1.0, 1.0, 1.0), "Color2": (0.0, 0.0, 0.0), "Scale": 1.0,
        }, outputs=("Color", "Fac"))
        assert evaluate(resolve(node, "Fac", "float")) == _cycles_checker(co), co


# --------------------------------------------------------------------------
# Camera Data
# --------------------------------------------------------------------------

def test_camera_data_matches_cycles_camera_space():
    # A RealityKit view matrix: a camera at (1, 2, 3) turned 30 degrees about Y,
    # looking down its -Z. Cycles' camera space looks down +Z.
    angle = math.radians(30.0)
    c, s = math.cos(angle), math.sin(angle)
    eye = (1.0, 2.0, 3.0)
    rotation = ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))  # world -> view rotation
    translation = tuple(-sum(rotation[r][k] * eye[k] for k in range(3)) for r in range(3))
    world_to_view = tuple(rotation[r] + (translation[r],) for r in range(3)) + ((0.0, 0.0, 0.0, 1.0),)
    # The shaded object sits under the export root's turn, moved to (0.3, 0.2, -1).
    model_to_world = tuple(row[:3] + (offset,) for row, offset in zip(mx.ROOT_ROTATION[:3], (0.3, 0.2, -1.0))) + (mx.IDENTITY4[3],)
    local = (0.1, 1.5, 0.7)
    point = mx._mat_apply(model_to_world, local)
    view = mx._mat_apply(world_to_view, point)
    cycles = (view[0], view[1], -view[2])
    # The world-space position reader returns anchor space; here the entity's
    # anchor is not the identity, so it disagrees with the true world point
    # the view matrix expects. Camera Data must not depend on it.
    anchored = tuple(x + d for x, d in zip(point, (4.0, -2.0, 1.0)))
    env = Env(position_world=anchored, position_object=local, model_to_world=model_to_world,
              world_to_view=world_to_view)
    node = Node("CAMERA", "Camera Data", {}, outputs=("View Vector", "View Z Depth", "View Distance"))
    length = math.sqrt(sum(x * x for x in cycles))
    assert evaluate(resolve(node, "View Distance", "float"), env) == pytest.approx(length)
    assert evaluate(resolve(node, "View Z Depth", "float"), env) == pytest.approx(cycles[2])
    assert cycles[2] > 0.0  # the point is in front of the camera
    assert evaluate(resolve(node, "View Vector", "vector3"), env) == pytest.approx(tuple(x / length for x in cycles))


# --------------------------------------------------------------------------
# Mapping, against Cycles' own bake
# --------------------------------------------------------------------------

_MAPPING_VECTOR = (0.3, -1.25, 2.0)
_MAPPING_LOCATION = (0.4, 0.1, -0.7)
_MAPPING_ROTATION = (0.5, -0.9, 1.3)
_CYCLES_MAPPING = {
    ("POINT", (1.5, 0.5, -2.0)): (-0.04615, 3.61149, -2.71582),
    ("POINT", (1.5, 0.0, -2.0)): (-0.63744, 3.53204, -2.52956),
    ("TEXTURE", (1.5, 0.5, -2.0)): (0.85984, 2.14169, -1.25622),
    ("TEXTURE", (1.5, 0.0, -2.0)): (0.85984, 0.0, -1.25622),
    ("VECTOR", (1.5, 0.5, -2.0)): (-0.44615, 3.51149, -2.01582),
    ("VECTOR", (1.5, 0.0, -2.0)): (-1.03744, 3.43204, -1.82956),
    ("NORMAL", (1.5, 0.5, -2.0)): (0.78531, 0.45489, -0.41996),
    ("NORMAL", (1.5, 0.0, -2.0)): (-0.24006, 0.89274, -0.3813),
}


def _mapping(mapping_type, scale, *, linked):
    wrap = vector_value if linked else tuple
    return Node("MAPPING", "Mapping", {
        "Vector": wrap(_MAPPING_VECTOR),
        "Location": mx.Socket("Location", _MAPPING_LOCATION, enabled=mapping_type in ("POINT", "TEXTURE"))
        if not linked else mx.Socket("Location", link=vector_value(_MAPPING_LOCATION), enabled=mapping_type in ("POINT", "TEXTURE")),
        "Rotation": wrap(_MAPPING_ROTATION),
        "Scale": wrap(scale),
    }, outputs=("Vector",), vector_type=mapping_type)


@pytest.mark.parametrize("linked", [False, True], ids=["constant", "linked"])
@pytest.mark.parametrize("case", sorted(_CYCLES_MAPPING), ids=lambda c: f"{c[0]}-{'zero' if 0.0 in c[1] else 'full'}")
def test_mapping_matches_cycles_bake(case, linked):
    node = _mapping(*case, linked=linked)
    assert evaluate(resolve(node, "Vector", "vector3")) == pytest.approx(_CYCLES_MAPPING[case], abs=2e-4)


# --------------------------------------------------------------------------
# Computed image coordinates
# --------------------------------------------------------------------------

class _Image:
    # Channel Packed: Cycles never premultiplies these, so the coordinate tests
    # read the plain colour. Premultiplication has its own tests below.
    def __init__(self, path="/assets/grid.png", alpha_mode="CHANNEL_PACKED"):
        self.filepath = self.filepath_raw = path
        self.name = "grid"
        self.is_dirty = False
        self.source = "FILE"
        self.packed_file = None
        self.alpha_mode = alpha_mode
        self.colorspace_settings = type("CS", (), {"name": "sRGB"})()


@pytest.fixture(autouse=True)
def _fake_images(monkeypatch):
    monkeypatch.setattr(core, "_resolve_image_path", lambda image: getattr(image, "filepath", None))
    monkeypatch.setattr(core, "_image_source_alpha", lambda image, path: {"source_channels": 4, "source_has_alpha": True})


def _pattern(u, v):
    return (math.fmod(u * 3.0, 1.0), v * 0.5 + 0.25, 0.5 + 0.25 * math.sin(u + v), 0.2 + 0.6 * v)


def _image_node(vector=None, projection="FLAT", blend=0.0, **kw):
    inputs = {"Vector": vector} if vector is not None else {"Vector": mx.Socket("Vector", (0.0, 0.0, 0.0))}
    return Node("TEX_IMAGE", "Grid", inputs, outputs=("Color", "Alpha"), image=_Image(**kw), projection=projection,
                projection_blend=blend, interpolation="Linear", extension="REPEAT", uv_map="")


def test_an_image_samples_a_computed_coordinate():
    coordinate = (0.27, 0.61, 5.0)
    node = _image_node(vector_value(coordinate))
    env = Env(images={"/assets/grid.png": _pattern})
    expected = _pattern(0.27, 0.61)
    assert evaluate(resolve(node, "Color"), env) == pytest.approx(expected[:3])
    assert evaluate(resolve(node, "Alpha", "float"), env) == pytest.approx(expected[3])


def test_an_image_through_a_non_uv_mapping_samples_the_mapped_coordinate():
    mapping = _mapping("POINT", (1.5, 0.5, -2.0), linked=False)
    node = _image_node(mapping.out("Vector"))
    env = Env(images={"/assets/grid.png": _pattern})
    x, y, _ = _CYCLES_MAPPING[("POINT", (1.5, 0.5, -2.0))]
    assert evaluate(resolve(node, "Color"), env) == pytest.approx(_pattern(x, y)[:3], abs=1e-3)


def test_uv_and_constant_mapping_keep_the_uv_transform_path():
    uv = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV", "Generated"))
    assert core.image_uses_uv_transform(_image_node(uv.out("UV")))
    mapping = Node("MAPPING", "Mapping", {"Vector": uv.out("UV"), "Location": (0.0, 0.0, 0.0),
                                          "Rotation": (0.0, 0.0, 0.3), "Scale": (2.0, 2.0, 1.0)},
                   outputs=("Vector",), vector_type="POINT")
    assert core.image_uses_uv_transform(_image_node(mapping.out("Vector")))
    assert not core.image_uses_uv_transform(_image_node(uv.out("Generated")))
    assert not core.image_uses_uv_transform(_image_node(vector_value((0.1, 0.2, 0.0))))


#: A Texture-type Mapping of the UVs (Location (0.2, 0.3, 0), Rotation
#: (0, 0, 0.5), Scale (2, 4, 1)) baked by Cycles on Blender 5.2: an Emission
#: bake of the Mapping output, offset by 10, on a unit plane. uv -> mapped.
_CYCLES_TEXTURE_MAPPING_OF_UV = [
    ((0.5625, 0.5625), (0.2221, 0.01418)),
    ((0.1875, 0.8125), (0.11748, 0.11398)),
    ((0.9375, 0.3125), (0.32672, -0.08561)),
]


def test_an_image_through_a_texture_mapping_of_uvs_samples_blenders_texels():
    """place2d cannot carry Texture mapping's translate-then-rotate order in
    RealityKit, whose nodedef has no ``operationorder``; the default SRT order
    ran and sampled elsewhere. The image now reads at the computed coordinate."""
    uv = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV", "Generated"))
    mapping = Node("MAPPING", "Mapping", {"Vector": uv.out("UV"), "Location": (0.2, 0.3, 0.0),
                                          "Rotation": (0.0, 0.0, 0.5), "Scale": (2.0, 4.0, 1.0)},
                   outputs=("Vector",), vector_type="TEXTURE")
    node = _image_node(mapping.out("Vector"))
    assert not core.image_uses_uv_transform(node)
    color = resolve(node, "Color")
    for texcoord, (x, y) in _CYCLES_TEXTURE_MAPPING_OF_UV:
        env = Env(texcoord=texcoord, images={"/assets/grid.png": _pattern})
        assert evaluate(color, env) == pytest.approx(_pattern(x, y)[:3], abs=1e-3), texcoord


def test_a_point_mapping_place2d_cannot_carry_reads_at_computed_coordinates():
    uv = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV",))
    for rotation, scale in (((0.4, 0.0, 0.3), (2.0, 2.0, 1.0)), ((0.0, 0.0, 0.3), (0.0, 2.0, 1.0))):
        mapping = Node("MAPPING", "Mapping", {"Vector": uv.out("UV"), "Location": (0.0, 0.0, 0.0),
                                              "Rotation": rotation, "Scale": scale},
                       outputs=("Vector",), vector_type="POINT")
        assert not core.image_uses_uv_transform(_image_node(mapping.out("Vector")))


# --------------------------------------------------------------------------
# Environment Texture, against a port of svm_node_tex_environment
# --------------------------------------------------------------------------

def _cycles_environment_uv(co, projection):
    length = math.sqrt(sum(x * x for x in co))
    d = tuple(x / length for x in co) if length > 0.0 else tuple(co)  # safe_normalize
    if projection == "EQUIRECTANGULAR":
        if not any(d):
            return (0.0, 0.0)
        u = (math.atan2(d[1], d[0]) - math.pi) / (-2.0 * math.pi)
        v = (math.acos(d[2] / math.sqrt(sum(x * x for x in d))) - math.pi) / (-math.pi)
        return (u, v)
    x, y, z = d[0], d[1] - 1.0, d[2]
    div = 2.0 * math.sqrt(max(-0.5 * y, 0.0))
    if div > 0.0:
        x, y, z = x / div, y / div, z / div
    return (0.5 * (x + 1.0), 0.5 * (z + 1.0))


def _environment_node(vector, projection):
    inputs = {"Vector": vector} if vector is not None else {"Vector": mx.Socket("Vector", (0.0, 0.0, 0.0))}
    return Node("TEX_ENVIRONMENT", "Environment", inputs, outputs=("Color", "Alpha"), image=_Image(),
                projection=projection, interpolation="Linear")


@pytest.mark.parametrize("projection", ["EQUIRECTANGULAR", "MIRROR_BALL"])
def test_environment_texture_projects_the_vector_like_cycles(projection):
    rng = random.Random(5)
    directions = [(1.0, 0.0, 0.0), (0.0, -2.0, 0.5), (-0.3, 0.4, -0.8), (0.0, 0.0, 3.0)]
    directions += [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(30)]
    env = Env(images={"/assets/grid.png": _pattern})
    for co in directions:
        color = evaluate(resolve(_environment_node(vector_value(co), projection), "Color"), env)
        assert color == pytest.approx(_pattern(*_cycles_environment_uv(co, projection))[:3], abs=1e-9), co


def test_an_unwired_environment_texture_reads_the_world_position():
    """The Vector socket links Geometry Position (LINK_POSITION), not the UVs."""
    point = (0.6, -1.4, 0.9)
    env = Env(position_world=mx.to_realitykit(point), texcoord=(0.1, 0.1), images={"/assets/grid.png": _pattern})
    for projection in ("EQUIRECTANGULAR", "MIRROR_BALL"):
        node = _environment_node(None, projection)
        expected = _pattern(*_cycles_environment_uv(point, projection))
        assert evaluate(resolve(node, "Color"), env) == pytest.approx(expected[:3], abs=1e-9)
        assert evaluate(resolve(node, "Alpha", "float"), env) == pytest.approx(expected[3], abs=1e-9)


def test_a_premultiplied_environment_image_is_refused_by_resolver_and_validator():
    from Plugin.nodes import validate

    node = _environment_node(None, "EQUIRECTANGULAR")
    node.image = _Image(alpha_mode="PREMUL")
    node.outputs.get("Alpha").is_linked = True
    resolved = resolve(node, "Color")
    assert resolved["kind"] == "unresolved" and "premultiplied" in resolved["reason"]
    assert any("premultiplied" in issue for issue in validate._environment_texture_issues(node))


def test_a_zero_environment_vector_reads_the_image_origin():
    env = Env(images={"/assets/grid.png": _pattern})
    node = _environment_node(vector_value((0.0, 0.0, 0.0)), "EQUIRECTANGULAR")
    assert evaluate(resolve(node, "Color"), env) == pytest.approx(_pattern(0.0, 0.0)[:3])


@pytest.mark.parametrize("vector", [None, "computed"])
def test_the_alpha_of_a_file_without_alpha_is_one(monkeypatch, vector):
    """Cycles fills a three-channel file's alpha with 1, on the UV path too."""
    monkeypatch.setattr(core, "_image_source_alpha", lambda image, path: {"source_channels": 3, "source_has_alpha": False})
    node = _image_node(vector_value((0.1, 0.2, 0.0)) if vector else None)
    assert core.image_uses_uv_transform(node) is (vector is None)
    assert evaluate(resolve(node, "Alpha", "float")) == 1.0
    assert evaluate(resolve(node, "Alpha", "color3")) == pytest.approx((1.0, 1.0, 1.0))


def test_a_premultiplied_image_at_computed_coordinates_is_refused_when_its_alpha_is_used():
    node = _image_node(vector_value((0.1, 0.2, 0.0)), alpha_mode="PREMUL")
    node.outputs.get("Alpha").is_linked = True
    resolved = resolve(node)
    assert resolved["kind"] == "unresolved" and "premultiplied" in resolved["reason"]


def test_a_premultiplied_image_with_its_alpha_unused_reads_the_stored_colour():
    # Cycles stores images associated; with Alpha unused it hands them over as is.
    node = _image_node(vector_value((0.27, 0.61, 0.0)), alpha_mode="PREMUL")
    env = Env(images={"/assets/grid.png": _pattern})
    assert evaluate(resolve(node, "Color"), env) == pytest.approx(_pattern(0.27, 0.61)[:3])


# Cycles bake of a straight PNG texel (230, 51, 26, 128), Blender 5.2: an
# Emission bake of the Color output with Alpha unused, and of Color x Alpha with
# Alpha linked, into a float image. The 16-bit file is exact; the 8-bit file
# quantizes the premultiplied byte, so it lands within one code value.
_TEXEL = (230, 51, 26, 128)
_CYCLES_COLOR_ALPHA_UNUSED = {16: (0.172872, 0.010083, 0.004043), 8: (0.171441, 0.009721, 0.004025)}
_CYCLES_COLOR_ALPHA_LINKED = {16: (0.791298, 0.033105, 0.01033)}


def _encoded_texel(u, v):
    return tuple(c / 255.0 for c in _TEXEL)


def _decode(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


@pytest.mark.parametrize("depth", [16, 8])
def test_a_straight_image_with_its_alpha_unused_reads_premultiplied_like_cycles(depth, monkeypatch):
    node = _image_node(vector_value((0.5, 0.5, 0.0)), alpha_mode="STRAIGHT")
    assert core.image_premultiplies_color(node) == "encoded"
    resolved = resolve(node, "Color")
    # The file is read raw (the evaluator hands back the encoded texel), so the
    # graph's own decode after the multiply is what is being checked.
    color = evaluate(resolved, Env(images={"/assets/grid.png": _encoded_texel}))
    # 16-bit: Cycles computes in float32. 8-bit: Cycles rounds the premultiplied
    # byte, one code value is at most 0.004 in linear at this texel.
    tolerance = 2e-5 if depth == 16 else 0.004
    assert color == pytest.approx(_CYCLES_COLOR_ALPHA_UNUSED[depth], abs=tolerance)


def test_a_straight_image_with_its_alpha_used_reads_the_plain_colour():
    node = _image_node(vector_value((0.5, 0.5, 0.0)), alpha_mode="STRAIGHT")
    node.outputs.get("Alpha").is_linked = True
    assert core.image_premultiplies_color(node) is None
    decoded = tuple(_decode(c) for c in _encoded_texel(0, 0)[:3]) + (128 / 255.0,)
    color = evaluate(resolve(node, "Color"), Env(images={"/assets/grid.png": lambda u, v: decoded}))
    assert color == pytest.approx(_CYCLES_COLOR_ALPHA_LINKED[16], abs=1e-5)


def test_images_cycles_never_premultiplies():
    for mode in ("CHANNEL_PACKED", "NONE", "PREMUL"):
        assert core.image_premultiplies_color(_image_node(alpha_mode=mode)) is None, mode
    data = _image_node(alpha_mode="STRAIGHT")
    data.image.colorspace_settings = type("CS", (), {"name": "Non-Color"})()
    assert core.image_premultiplies_color(data) is None


def test_a_straight_uv_image_with_alpha_leaves_the_uv_path():
    uv = Node("TEX_COORD", "Texture Coordinate", {}, outputs=("UV",))
    node = _image_node(uv.out("UV"), alpha_mode="STRAIGHT")
    resolved = resolve(node, "Color")
    assert resolved["kind"] == "node"  # a computed read, not a texture spec


# --------------------------------------------------------------------------
# Box projection, against a port of svm_node_tex_image_box
# --------------------------------------------------------------------------

def _saturate(x):
    return min(max(x, 0.0), 1.0)


def _cycles_box(normal, co, blend, sample):
    signed = normal
    n = [abs(x) for x in normal]
    total = sum(n)
    n = [x / total for x in n]
    w = [0.0, 0.0, 0.0]
    limit = 0.5 * (1.0 + blend)
    nx, ny, nz = n
    if nx > limit * (nx + ny) and nx > limit * (nx + nz):
        w[0] = 1.0
    elif ny > limit * (nx + ny) and ny > limit * (ny + nz):
        w[1] = 1.0
    elif nz > limit * (nx + nz) and nz > limit * (ny + nz):
        w[2] = 1.0
    elif blend > 0.0:
        if nz < (1.0 - limit) * (ny + nx):
            w[0] = _saturate((nx / (nx + ny) - 0.5 * (1.0 - blend)) / blend)
            w[1] = 1.0 - w[0]
        elif nx < (1.0 - limit) * (ny + nz):
            w[1] = _saturate((ny / (ny + nz) - 0.5 * (1.0 - blend)) / blend)
            w[2] = 1.0 - w[1]
        elif ny < (1.0 - limit) * (nx + nz):
            w[0] = _saturate((nx / (nx + nz) - 0.5 * (1.0 - blend)) / blend)
            w[2] = 1.0 - w[0]
        else:
            w = [((2.0 - limit) * x + (limit - 1.0)) / (2.0 * limit - 1.0) for x in n]
    else:
        w[0] = 1.0
    f = [0.0, 0.0, 0.0, 0.0]
    if w[0] > 0.0:
        s = sample((1.0 - co[1]) if signed[0] < 0.0 else co[1], co[2])
        f = [a + w[0] * b for a, b in zip(f, s)]
    if w[1] > 0.0:
        s = sample((1.0 - co[0]) if signed[1] > 0.0 else co[0], co[2])
        f = [a + w[1] * b for a, b in zip(f, s)]
    if w[2] > 0.0:
        s = sample((1.0 - co[1]) if signed[2] > 0.0 else co[1], co[0])
        f = [a + w[2] * b for a, b in zip(f, s)]
    return tuple(f)


@pytest.mark.parametrize("blend", [0.0, 0.3, 0.8])
def test_box_projection_matches_cycles(blend):
    rng = random.Random(11)
    node = _image_node(Node("TEX_COORD", "Texture Coordinate", {}, outputs=("Object",)).out("Object"),
                       projection="BOX", blend=blend)
    color = resolve(node, "Color")
    alpha = resolve(node, "Alpha", "float")
    normals = [(1.0, 0.1, 0.05), (-0.2, 0.9, 0.1), (0.3, -0.3, -0.9), (0.6, 0.55, 0.1),
               (0.5, 0.45, 0.5), (-0.58, 0.57, -0.58), (0.1, 0.6, 0.65), (0.7, 0.05, -0.68)]
    normals += [tuple(rng.uniform(-1, 1) for _ in range(3)) for _ in range(40)]
    for normal in normals:
        co = tuple(rng.uniform(-1, 1) for _ in range(3))
        env = Env(normal_object=normal, position_object=co, images={"/assets/grid.png": _pattern})
        expected = _cycles_box(normal, co, blend, _pattern)
        assert evaluate(color, env) == pytest.approx(expected[:3], abs=1e-9), normal
        assert evaluate(alpha, env) == pytest.approx(expected[3], abs=1e-9), normal


def test_box_projection_samples_uvs_when_the_vector_is_unwired():
    node = _image_node(projection="BOX", blend=0.0)
    node.inputs = mx.Inputs()
    color = resolve(node, "Color")
    env = Env(normal_object=(0.0, 0.0, 1.0), texcoord=(0.3, 0.7), images={"/assets/grid.png": _pattern})
    # +Z side: uv = (1 - co.y, co.x) with co = (u, v, 0).
    assert evaluate(color, env) == pytest.approx(_pattern(1.0 - 0.7, 0.3)[:3])
