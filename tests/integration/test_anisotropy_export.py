"""Integration test - Principled and Glossy anisotropy export to PBR Surface 2.

With a Tangent node in UV Map mode on the render UV map, Anisotropic and
Anisotropic Rotation export as specularAnisotropyLevel and
specularAnisotropyAngle, and Roughness is scaled so both GGX alphas match
Cycles' (measured on macOS 27.2.1; see Plugin/export/materials/anisotropy.py).
Without such a tangent, or where the scaled roughness would exceed 1, the
export stops.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable
from pxr import Usd, UsdShade


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out, case = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_uv_sphere_add()
material = bpy.data.materials.new("Brushed")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
if case == "glossy":
    tree.nodes.remove(bsdf)
    bsdf = tree.nodes.new("ShaderNodeBsdfAnisotropic")
    tree.links.new(bsdf.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
    names = ("Anisotropy", "Rotation")
else:
    bsdf.inputs["Metallic"].default_value = 1.0
    names = ("Anisotropic", "Anisotropic Rotation")
bsdf.inputs["Roughness"].default_value = 0.8 if case == "too_rough" else 0.3
bsdf.inputs[names[0]].default_value = 1.0 if case == "too_rough" else 0.5
bsdf.inputs[names[1]].default_value = 0.125
if case != "unlinked":
    tangent = tree.nodes.new("ShaderNodeTangent")
    tangent.direction_type = "RADIAL" if case == "radial" else "UV_MAP"
    tree.links.new(tangent.outputs[0], bsdf.inputs["Tangent"])
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _export(tmp_path, case):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), case],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    output = tmp_path / "scene.usda"
    exported = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend),
         "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    return exported, output


@pytest.mark.parametrize("case", ["principled", "glossy"])
def test_anisotropy_exports_the_measured_mapping(tmp_path, case):
    exported, output = _export(tmp_path, case)
    assert exported.returncode == 0, exported.stdout + exported.stderr

    stage = Usd.Stage.Open(str(output))
    surface = next(
        UsdShade.Shader(p) for p in stage.Traverse()
        if UsdShade.Shader(p).GetIdAttr().Get() == "ND_realitykit_pbr_surfaceshader_2_0"
    )
    s = math.sqrt(1 - 0.9 * 0.5)
    assert surface.GetInput("specularAnisotropyLevel").Get() == pytest.approx(
        math.sqrt(0.45 / 1.55), abs=1e-5
    )
    assert surface.GetInput("specularAnisotropyAngle").Get() == pytest.approx(0.875, abs=1e-6)
    assert surface.GetInput("roughness").Get() == pytest.approx(
        0.3 * math.sqrt((1 / s + s) / 2), abs=1e-5
    )


@pytest.mark.parametrize(
    "case,message",
    [
        ("unlinked", "'Tangent' is unlinked"),
        ("radial", "not a Tangent node in UV Map mode"),
        ("too_rough", "above RealityKit's maximum of 1"),
    ],
)
def test_unfaithful_anisotropy_stops_the_export(tmp_path, case, message):
    exported, _output = _export(tmp_path, case)
    assert exported.returncode != 0
    assert message in exported.stdout + exported.stderr
