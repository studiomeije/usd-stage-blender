"""Diffuse, Glossy, Metallic, Sheen and Subsurface Scattering shaders, and a
Mix Shader of two of them, exported as one Principled BSDF.

The preset mappings come from Cycles' ``svm_node_closure_bsdf``:

- Principled's specular lobe takes ``f0 = F0_from_ior(ior) * 2 * level`` and
  its IOR from that f0, and Cycles allocates the lobe only when that IOR is
  not 1. A level of 0 therefore removes the specular layer, leaving exactly
  the Diffuse BSDF's closure (Lambert, or Oren-Nayar with the same roughness).
- The Principled metallic lobe is the F82 Tint conductor the Metallic BSDF
  builds from Base Color and Edge Tint.
- Principled squares Roughness for the subsurface entry where the Subsurface
  Scattering node uses its Roughness as is.

A Mix Shader in Cycles is a weighted sum of both closures with the factor
clamped to [0, 1]; when Base Color or Metallic is the only input that differs
that equals blending the values, which the tests evaluate, and once two
differ it does not.

The authored specular is judged by the reflectance RealityKit gives it,
measured with RealityRenderer on macOS 27: F0 = 0.08 * saturate(specular)
while baseDiffuseRoughness is unauthored, and saturate(specular) *
F0(specularIOR) once it is.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mx_eval  # noqa: E402
import test_shader_closures as tc  # noqa: E402
from test_shader_closures import _Link, _Node, _Socket, _Sockets, _material, _mix, _principled, _transparent  # noqa: E402

core = tc.core
validate = tc.validate


def _bsdf(node_type, name, sockets, **attrs):
    node = _Node(type=node_type, name=name, **attrs)
    node.inputs = _Sockets(**{key: _Socket(value, name=key, socket_type="RGBA" if isinstance(value, tuple) and len(value) == 4 else "VALUE")
                              for key, value in sockets.items()})
    node.outputs = _Sockets(BSDF=_Socket(name="BSDF", socket_type="SHADER"))
    return node


def diffuse(color=(0.2, 0.6, 0.3, 1.0), roughness=0.4):
    return _bsdf("BSDF_DIFFUSE", "Diffuse BSDF", {"Color": color, "Roughness": roughness, "Normal": (0.0, 0.0, 0.0)})


def glossy(color=(0.9, 0.7, 0.2, 1.0), roughness=0.3, anisotropy=0.0, distribution="MULTI_GGX"):
    return _bsdf("BSDF_GLOSSY", "Glossy BSDF", {"Color": color, "Roughness": roughness, "Anisotropy": anisotropy,
                                                 "Rotation": 0.0, "Normal": (0.0, 0.0, 0.0), "Tangent": (0.0, 0.0, 0.0)},
                 distribution=distribution)


def metallic(edge=(1.0, 1.0, 1.0, 1.0), fresnel="F82", distribution="MULTI_GGX"):
    return _bsdf("BSDF_METALLIC", "Metallic BSDF", {"Base Color": (0.95, 0.64, 0.54, 1.0), "Edge Tint": edge,
                                                     "Roughness": 0.2, "Anisotropy": 0.0, "Rotation": 0.0,
                                                     "Normal": (0.0, 0.0, 0.0), "Tangent": (0.0, 0.0, 0.0),
                                                     "Thin Film Thickness": 0.0, "Thin Film IOR": 1.33},
                 fresnel_type=fresnel, distribution=distribution)


def sheen(color=(0.3, 0.1, 0.9, 1.0), roughness=0.5):
    return _bsdf("BSDF_SHEEN", "Sheen BSDF", {"Color": color, "Roughness": roughness, "Normal": (0.0, 0.0, 0.0)},
                 distribution="MICROFIBER")


def subsurface(ior=1.4, roughness=0.64):
    return _bsdf("SUBSURFACE_SCATTERING", "Subsurface Scattering", {
        "Color": (0.9, 0.5, 0.4, 1.0), "Scale": 0.02, "Radius": (1.0, 0.3, 0.2), "IOR": ior,
        "Roughness": roughness, "Anisotropy": 0.1, "Normal": (0.0, 0.0, 0.0)}, falloff="RANDOM_WALK_SKIN")


def _extract(surface, *nodes):
    material = _material(surface, *nodes)
    return material, core.extract_blender_material_data(material)


# --------------------------------------------------------------------------
# Cycles arithmetic behind the presets
# --------------------------------------------------------------------------

def _f0_from_ior(ior):
    return ((ior - 1.0) / (ior + 1.0)) ** 2


def _ior_from_f0(f0):
    sqrt_f0 = math.sqrt(min(max(f0, 0.0), 0.99))
    return (1.0 + sqrt_f0) / (1.0 - sqrt_f0)


def _realitykit_f0(inputs):
    """PBR Surface 2's dielectric F0 for authored inputs, as measured."""
    specular = min(max(inputs.get("specular", 0.5), 0.0), 1.0)
    if "baseDiffuseRoughness" in inputs:
        return specular * _f0_from_ior(inputs.get("specularIOR", 1.5))
    return 0.08 * specular


def _authored(data):
    graph = tc.MaterialXGraphBuilder(tc._MANIFEST).build_pbr_material(data)
    return graph["nodes"][0]["inputs"]


def test_a_zero_specular_level_removes_principleds_specular_lobe_in_cycles():
    for ior in (1.33, 1.5, 2.4):
        eta = _ior_from_f0(_f0_from_ior(ior) * 2.0 * 0.0)
        assert eta == 1.0  # Cycles allocates the lobe only when eta != 1


# --------------------------------------------------------------------------
# presets
# --------------------------------------------------------------------------

def test_diffuse_is_principled_without_specular():
    node = diffuse()
    material, data = _extract(node)
    assert data["type"] == "principled"
    assert data["base_color"] == pytest.approx([0.2, 0.6, 0.3])
    assert data["diffuse_roughness"] == pytest.approx(0.4)
    assert _realitykit_f0(_authored(data)) == pytest.approx(0.0)
    assert data.get("metallic", 0.0) == 0.0
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_metallic_f82_is_principleds_metallic_lobe():
    material, data = _extract(metallic())
    assert data["metallic"] == 1.0
    assert data["base_color"] == pytest.approx([0.95, 0.64, 0.54])
    assert data["roughness"] == pytest.approx(0.2)
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_a_coloured_edge_tint_is_refused_under_its_own_name():
    material, _data = _extract(metallic(edge=(0.7, 0.72, 0.77, 1.0)))
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any("Metallic BSDF 'Edge Tint'" in e["message"] for e in result["errors"])
    assert not any("Principled" in e["message"] for e in result["errors"])


def test_physical_conductor_is_refused():
    material, data = _extract(metallic(fresnel="PHYSICAL_CONDUCTOR"))
    assert data["type"] == "unknown"
    result = validate.validate_material(material, strict=True)
    assert any("Physical Conductor" in e["message"] for e in result["errors"])


def test_glossy_exports_as_metal_and_says_its_fresnel_differs():
    material, data = _extract(glossy())
    assert data["metallic"] == 1.0
    assert data["base_color"] == pytest.approx([0.9, 0.7, 0.2])
    notices = core.resolve_surface_closure(material)["notices"]
    assert any("every angle" in n for n in notices)


def test_sheen_is_a_black_base_under_full_sheen():
    material, data = _extract(sheen())
    assert data["base_color"] == pytest.approx([0.0, 0.0, 0.0])
    assert data["sheen_weight"] == 1.0
    assert data["sheen_tint"][:3] == pytest.approx([0.3, 0.1, 0.9])
    inputs = _authored(data)
    assert _realitykit_f0(inputs) == pytest.approx(0.0)
    assert inputs["sheenColor"] == pytest.approx([0.3, 0.1, 0.9])
    # Black, non-metallic, no specular: RealityKit's sheen replacing the
    # specular lobe loses nothing, so no approximation is reported.
    assert not any("Sheen" in n for n in core.principled_notices(core.resolve_surface_closure(material)["principled"]))
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_subsurface_scattering_squares_back_its_roughness():
    material, data = _extract(subsurface())
    assert data["subsurface_weight"] == 1.0
    assert data["subsurface_radius"] == pytest.approx(0.02)
    assert data["subsurface_radius_scale"][:3] == pytest.approx([1.0, 0.3, 0.2])
    assert data["roughness"] == pytest.approx(0.8)  # Principled squares it back to 0.64
    assert _realitykit_f0(_authored(data)) == pytest.approx(0.0)


def test_subsurface_scattering_off_skin_says_cycles_scatters_with_its_ior():
    """Principled at Specular IOR Level 0 scatters with eta 1 except under
    Random Walk (Skin), which reads Subsurface IOR; the node always uses its
    own IOR (svm_closure.h)."""
    node = subsurface()
    node.falloff = "RANDOM_WALK"
    material, _data = _extract(node)
    notices = core.resolve_surface_closure(material)["notices"]
    assert any("Random Walk" in n and "IOR" in n for n in notices)
    skin = subsurface()
    material, _data = _extract(skin)
    assert not any("IOR" in n for n in core.resolve_surface_closure(material)["notices"])


def test_a_custom_subsurface_ior_is_refused_under_its_own_name():
    material, _data = _extract(subsurface(ior=1.7))
    result = validate.validate_material(material, strict=True)
    assert any("Subsurface Scattering 'IOR'" in e["message"] for e in result["errors"])


def test_a_preset_off_the_output_is_refused():
    principled = _principled()
    stray = diffuse()
    material = _material(principled, stray)
    result = validate.validate_material(material, only_connected=False, strict=True)
    assert any(e["node_type"] == "BSDF_DIFFUSE" and "Material Output" in e["message"] for e in result["errors"])


def test_a_preset_mixed_with_a_transparent_bsdf_becomes_opacity():
    node, clear = diffuse(), _transparent()
    material, data = _extract(_mix(node, clear, 0.25), node, clear)
    assert data["type"] == "principled"
    assert data["alpha"] == pytest.approx(0.75)


# --------------------------------------------------------------------------
# Mix Shader of two Principled-like shaders
# --------------------------------------------------------------------------

def _two(fac=0.25, fac_link=None, second=None):
    first = _principled()
    first.inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1.0)
    first.inputs["Metallic"].default_value = 0.0
    other = second or _principled()
    if second is None:
        other.inputs["Base Color"].default_value = (0.0, 0.4, 1.0, 1.0)
        other.inputs["Metallic"].default_value = 1.0
    mix = _mix(first, other, fac, fac_link=fac_link)
    return first, other, mix


def test_a_constant_factor_blends_a_lone_base_color_exactly():
    first, other, mix = _two(fac=0.25)
    other.inputs["Metallic"].default_value = 0.0
    material, data = _extract(mix, first, other)
    assert data["type"] == "principled"
    assert data["base_color"] == pytest.approx([0.8 * 0.75, 0.2 * 0.75 + 0.4 * 0.25, 0.1 * 0.75 + 0.25])
    assert core.resolve_surface_closure(material)["notices"] == []
    assert validate.validate_material(material, strict=True)["ok"] is True


def _closure_reflectance(base, metallic, cos_theta):
    """Directional reflectance of the metallic and diffuse lobes Cycles
    allocates for one Principled BSDF: metallic * F82 conductor (Schlick at a
    white Edge Tint) plus (1 - metallic) * Lambert base colour."""
    schlick = (1.0 - cos_theta) ** 5
    return [metallic * (c + (1.0 - c) * schlick) + (1.0 - metallic) * c for c in base]


def test_base_color_and_metallic_differing_together_are_named_as_approximate():
    first, other, mix = _two(fac=0.25)
    material, data = _extract(mix, first, other)
    assert data["base_color"] == pytest.approx([0.8 * 0.75, 0.2 * 0.75 + 0.4 * 0.25, 0.1 * 0.75 + 0.25])
    assert data["metallic"] == pytest.approx(0.25)
    # Cycles' weighted closure sum and the blended surface part at grazing.
    grazing = 0.05
    summed = [0.75 * a + 0.25 * b for a, b in zip(_closure_reflectance((0.8, 0.2, 0.1), 0.0, grazing),
                                                   _closure_reflectance((0.0, 0.4, 1.0), 1.0, grazing))]
    blended = _closure_reflectance(data["base_color"], data["metallic"], grazing)
    assert max(abs(x - y) for x, y in zip(summed, blended)) > 0.05
    notices = core.resolve_surface_closure(material)["notices"]
    assert any("Base Color, Metallic" in n and "approximately" in n for n in notices)
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_a_factor_outside_the_unit_range_clamps_like_cycles():
    first, other, mix = _two(fac=1.7)
    _material_, data = _extract(mix, first, other)
    assert data["base_color"] == pytest.approx([0.0, 0.4, 1.0])


def test_a_linked_factor_blends_in_the_graph():
    driver = mx_eval.Node("VECT_MATH", "Vector Math", {"A": (0.6, 0.0, 0.0), "B": (1.0, 0.0, 0.0), "C": (0.0, 0.0, 0.0), "Scale": 1.0},
                          outputs=("Vector", "Value"), operation="DOT_PRODUCT")
    first, other, mix = _two(fac_link=_Link(driver, driver.outputs.get("Value")))
    _material_, data = _extract(mix, first, other)
    base = mx_eval.evaluate(data["input_graphs"]["baseColor"])
    metal = mx_eval.evaluate(data["input_graphs"]["metallic"])
    assert base == pytest.approx((0.8 * 0.4, 0.2 * 0.4 + 0.4 * 0.6, 0.1 * 0.4 + 0.6))
    assert metal == pytest.approx(0.6)


def test_other_differing_inputs_are_named_in_a_notice():
    first, other, mix = _two()
    other.inputs["Roughness"].default_value = 0.9
    material, data = _extract(mix, first, other)
    notices = core.resolve_surface_closure(material)["notices"]
    assert any("Roughness" in n and "approximately" in n for n in notices)
    assert data["roughness"] == pytest.approx(0.5 * 0.75 + 0.9 * 0.25)


def test_a_diffuse_mixed_with_a_principled_blends_their_specular_levels():
    first = _principled()
    first.inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1.0)
    node = diffuse(color=(0.2, 0.6, 0.3, 1.0), roughness=0.0)
    mix = _mix(first, node, 0.5)
    material, data = _extract(mix, first, node)
    assert data["type"] == "principled"
    assert data["base_color"] == pytest.approx([0.5, 0.4, 0.2])
    assert data["specular"] == pytest.approx(0.25)  # level 0.5 and 0 blended
    # The authored surface reflects what Cycles gives the blended level and IOR.
    ior = data["ior"]
    assert _realitykit_f0(_authored(data)) == pytest.approx(_f0_from_ior(ior) * 2.0 * 0.25)
    assert any("Specular IOR Level" in n for n in core.resolve_surface_closure(material)["notices"])


def test_differing_linked_normals_are_refused():
    first, other, mix = _two()
    bump = _Node(type="NORMAL_MAP", name="Normal Map")
    bump.outputs = _Sockets(Normal=_Socket(name="Normal", socket_type="VECTOR"))
    bump.inputs = _Sockets()
    first.inputs["Normal"] = _Socket((0.0, 0.0, 0.0), name="Normal", socket_type="VECTOR", linked=True,
                                     link=_Link(bump, bump.outputs["Normal"]))
    material = _material(mix, first, other, bump)
    reason = core.resolve_surface_closure(material)["refusal"]
    assert reason and "Normal" in reason
