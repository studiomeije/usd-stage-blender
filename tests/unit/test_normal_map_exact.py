"""Normal Map and the Principled normal inputs, against Cycles.

The reference normals were baked by Cycles on Blender 5.2: an Emission bake
of the Normal Map's output, offset by 10 into a float image, on a 2 m plane
whose tangent frame is the world axes, for a constant colour at several
Strengths. Every bake of a smooth or flat face, with Base Original or
Displaced, gave the values below; ``svm_node_normal_map``'s scaled form
``(s x, s y, mix(1, z, saturate(s)))`` reproduces them and the
``normalize(mix((0, 0, 1), n, s))`` the export used to author does not. With
the material displacing vertices, smooth faces and Base Original, Cycles
blends toward the undisplaced normal instead (Strength 2 baked
(0.79841, -0.39919, 0.45076)), which the export refuses.

The world-space tests use a Python port of ``svm_node_normal_map`` in a
rotated tangent frame.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval as mx  # noqa: E402
from mx_eval import Env, Node, Socket, evaluate, resolve, vector_value  # noqa: E402

core = mx.core

UNIT_COLOR = (0.65, 0.4, 0.966369)
CYCLES_NORMAL_MAP = [
    # colour, strength -> baked world normal (tangent frame = world axes)
    ((0.7, 0.4, 0.8), 2.0, (0.74279, -0.37138, 0.55708)),
    ((0.7, 0.4, 0.8), 0.35, (0.16017, -0.08006, 0.98384)),
    ((0.7, 0.4, 0.8), 1.0, (0.53453, -0.26725, 0.80178)),
    (UNIT_COLOR, 2.0, (0.50893, -0.33926, 0.79114)),
    (UNIT_COLOR, 0.35, (0.10666, -0.07108, 0.99175)),
    (UNIT_COLOR, 1.0, (0.30001, -0.19999, 0.93274)),
]


def _normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return tuple(x / length for x in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _cycles_normal_map(color, strength, tangent, sign, normal, directx=False):
    c = [2.0 * (x - 0.5) for x in color]
    if directx:
        c[1] = -c[1]
    saturated = min(max(strength, 0.0), 1.0)
    c = (c[0] * strength, c[1] * strength, 1.0 + (c[2] - 1.0) * saturated)
    bitangent = tuple(sign * x for x in _cross(normal, tangent))
    return _normalize(tuple(c[0] * t + c[1] * b + c[2] * n for t, b, n in zip(tangent, bitangent, normal)))


def _frame():
    """An orthonormal tangent frame turned away from the world axes."""
    normal = _normalize((0.3, -0.5, 0.8))
    tangent = _normalize(_cross((0.0, 0.0, 1.0), normal))
    return tangent, 1.0, normal


def _env(tangent, sign, normal, **extra):
    bitangent = tuple(sign * x for x in _cross(normal, tangent))
    return Env(tangent_world=mx.to_realitykit(tangent), bitangent_world=mx.to_realitykit(bitangent),
               normal_world=mx.to_realitykit(normal), **extra)


def _normal_map(color, strength, **attributes):
    attributes.setdefault("space", "TANGENT")
    attributes.setdefault("uv_map", "")
    attributes.setdefault("convention", "OPENGL")
    attributes.setdefault("base", "ORIGINAL")
    return Node("NORMAL_MAP", "Normal Map", {"Strength": strength, "Color": color}, outputs=("Normal",), **attributes)


# --------------------------------------------------------------------------
# Strength, on both paths, against the bake
# --------------------------------------------------------------------------

@pytest.mark.parametrize("color, strength, baked", CYCLES_NORMAL_MAP)
def test_a_computed_colour_normal_map_matches_the_bake_in_tangent_space(color, strength, baked):
    node = _normal_map(vector_value(color), strength)
    tangent = core._normal_map_tangent_expr(node, set(), [], {})
    assert evaluate(tangent) == pytest.approx(baked, abs=2e-4)


def _usd_expr(output):
    """A USD shader network back into an expression mx_eval can evaluate."""
    from pxr import Sdf, UsdShade

    shader = UsdShade.Shader(output.GetPrim())
    inputs = {}
    for shader_input in shader.GetInputs():
        name = shader_input.GetBaseName()
        if shader_input.HasConnectedSource():
            source, source_name, _ = shader_input.GetConnectedSource()
            inputs[name] = _usd_expr(source.GetOutput(source_name))
            continue
        value = shader_input.Get()
        if isinstance(value, Sdf.AssetPath):
            inputs[name] = {"kind": "file_asset", "path": value.path}
        elif isinstance(value, str):
            inputs[name] = {"kind": "constant", "value": value}
        elif hasattr(value, "__len__"):
            inputs[name] = {"kind": "constant", "value": tuple(float(x) for x in value)}
        else:
            inputs[name] = {"kind": "constant", "value": float(value)}
    return {"kind": "node", "node_id": shader.GetIdAttr().Get(), "inputs": inputs, "output": output.GetBaseName()}


@pytest.mark.parametrize("color, strength, baked", CYCLES_NORMAL_MAP)
def test_the_realitykit_decode_path_applies_strength_like_the_bake(color, strength, baked):
    pytest.importorskip("pxr")
    from pxr import Usd

    from Plugin.export.materials.textures import _create_texture_connection
    from Plugin.manifest.materialx_nodes import load_manifest

    stage = Usd.Stage.CreateInMemory()
    output = _create_texture_connection(
        stage, "/Material0", "normal",
        {"path": "normal.png", "output_type": "vector3", "type": "normal_texture", "texcoord": "UV0",
         "scale": strength, "space": "tangent", "colorspace_role": "data"},
        load_manifest(), "Material0",
    )
    env = Env(texcoord=(0.3, 0.6), images={"normal.png": lambda u, v: color})
    # PBR Surface 2 normalises what it is handed; compare directions.
    assert _normalize(evaluate(_usd_expr(output), env)) == pytest.approx(baked, abs=2e-4)


# --------------------------------------------------------------------------
# The Normal Map's world-space output
# --------------------------------------------------------------------------

@pytest.mark.parametrize("color, strength, baked", CYCLES_NORMAL_MAP)
@pytest.mark.parametrize("directx", [False, True])
def test_the_normal_map_output_is_blenders_world_normal(color, strength, baked, directx):
    tangent, sign, normal = _frame()
    node = _normal_map(vector_value(color), strength, convention="DIRECTX" if directx else "OPENGL")
    world = evaluate(resolve(node, "Normal", "vector3"), _env(tangent, sign, normal))
    assert world == pytest.approx(_cycles_normal_map(color, strength, tangent, sign, normal, directx), abs=1e-9)


def test_a_linked_strength_is_authored():
    tangent, sign, normal = _frame()
    for strength in (-0.5, 0.35, 1.7):
        node = _normal_map(vector_value((0.7, 0.4, 0.8)), mx.float_value(strength))
        world = evaluate(resolve(node, "Normal", "vector3"), _env(tangent, sign, normal))
        assert world == pytest.approx(_cycles_normal_map((0.7, 0.4, 0.8), strength, tangent, sign, normal), abs=1e-9)


def test_a_neutral_colour_at_zero_length_falls_back_to_the_shading_normal():
    tangent, sign, normal = _frame()
    node = _normal_map(vector_value((0.5, 0.5, 0.5)), 1.0)
    assert evaluate(resolve(node, "Normal", "vector3"), _env(tangent, sign, normal)) == pytest.approx(normal)


# --------------------------------------------------------------------------
# What reaches PBR Surface 2's tangent-space normal
# --------------------------------------------------------------------------

def _material(**links):
    principled = Node("BSDF_PRINCIPLED", "Principled BSDF", {
        name: Socket(name, link=link) for name, link in links.items()
    }, outputs=("BSDF",), distribution="MULTI_GGX", subsurface_method="RANDOM_WALK")
    output = Node("OUTPUT_MATERIAL", "Material Output", {"Surface": Socket("Surface", link=principled.out("BSDF"))},
                  outputs=(), is_active_output=True, target="ALL")
    for socket in principled.inputs:
        socket.links[0].from_socket.is_linked = True
    tree = SimpleNamespace(nodes=[output, principled], links=[], animation_data=None)
    return SimpleNamespace(name="Normals", node_tree=tree, displacement_method="BUMP",
                           surface_render_method="DITHERED", blend_method="OPAQUE", diffuse_color=(1, 1, 1, 1))


def test_a_geometry_normal_reaches_the_surface_in_tangent_space():
    """Blender's Principled Normal is a world-space normal. Geometry Normal
    wired in is the unperturbed normal, (0, 0, 1) in tangent space; it used to
    reach the tangent-space input as its world components."""
    tangent, sign, normal = _frame()
    geometry = Node("NEW_GEOMETRY", "Geometry", {}, outputs=("Position", "Normal"))
    graphs = core.extract_blender_material_data(_material(**{
        "Normal": geometry.out("Normal"), "Coat Normal": geometry.out("Normal"),
    }))["input_graphs"]
    env = _env(tangent, sign, normal)
    assert evaluate(graphs["normal"], env) == pytest.approx((0.0, 0.0, 1.0))
    assert evaluate(graphs["clearcoatNormal"], env) == pytest.approx((0.0, 0.0, 1.0))


def test_a_normal_map_through_a_node_reaches_the_surface_as_its_tangent_direction():
    """A Normal Map through a Normalize is its world normal, taken back to
    tangent space: the Cycles tangent-space direction."""
    tangent, sign, normal = _frame()
    color, strength = (0.7, 0.4, 0.8), 0.35
    node = _normal_map(vector_value(color), strength)
    normalize = Node("VECT_MATH", "Normalize", {"A": node.out("Normal"), "B": (0.0, 0.0, 0.0), "C": (0.0, 0.0, 0.0),
                                                "Scale": 1.0}, outputs=("Vector", "Value"), operation="NORMALIZE")
    graphs = core.extract_blender_material_data(_material(Normal=normalize.out("Vector")))["input_graphs"]
    world = _cycles_normal_map(color, strength, tangent, sign, normal)
    bitangent = tuple(sign * x for x in _cross(normal, tangent))
    expected = tuple(sum(w * a for w, a in zip(world, axis)) for axis in (tangent, bitangent, normal))
    assert evaluate(graphs["normal"], _env(tangent, sign, normal)) == pytest.approx(expected, abs=1e-9)


def test_a_normal_map_of_a_computed_colour_reaches_the_surface_decoded():
    """The colour used to reach ``normal`` undecoded when it was not a plain image."""
    color, strength, baked = CYCLES_NORMAL_MAP[0]
    node = _normal_map(vector_value(color), strength)
    graphs = core.extract_blender_material_data(_material(Normal=node.out("Normal")))["input_graphs"]
    assert evaluate(graphs["normal"]) == pytest.approx(baked, abs=2e-4)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_a_normal_map_tangent_uv_map_other_than_the_render_map_is_refused(monkeypatch):
    class _Layers(list):
        def get(self, name):
            return next((layer for layer in self if layer.name == name), None)

    mesh = SimpleNamespace(name="Plane", uv_layers=_Layers([
        SimpleNamespace(name="UVMap", active_render=True), SimpleNamespace(name="Detail", active_render=False),
    ]))
    monkeypatch.setattr(core, "_material_meshes", lambda node: [mesh])
    node = _normal_map(vector_value((0.7, 0.4, 0.8)), 1.0, uv_map="Detail")
    resolved = resolve(node, "Normal", "vector3")
    assert resolved["kind"] == "unresolved" and "render UV map" in resolved["reason"]
    assert "render UV map" in core.normal_map_refusal(node)
    node.uv_map = "UVMap"
    assert core.normal_map_refusal(node) is None


def test_base_original_with_displacement_and_strength_is_refused(monkeypatch):
    material = SimpleNamespace(name="Displaced")
    monkeypatch.setattr(core, "_owning_materials", lambda node: [material])
    monkeypatch.setattr(core, "displacement_source", lambda m: (object(), object()))
    node = _normal_map(vector_value((0.7, 0.4, 0.8)), 2.0)
    assert "Base Original" in core.normal_map_refusal(node)
    node.base = "DISPLACED"
    assert core.normal_map_refusal(node) is None
    node.base = "ORIGINAL"
    node.inputs.get("Strength").default_value = 1.0
    assert core.normal_map_refusal(node) is None


def test_the_validator_names_the_normal_map_refusal(monkeypatch):
    from Plugin.nodes import validate

    monkeypatch.setattr(core, "_material_meshes", lambda node: None)
    node = _normal_map(vector_value((0.7, 0.4, 0.8)), 1.0, space="OBJECT")
    assert any("tangent-space" in message for message, _warning in validate._normal_map_issues(node))
    node.space = "TANGENT"
    assert validate._normal_map_issues(node) == []
