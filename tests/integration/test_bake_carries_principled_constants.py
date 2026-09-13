"""Integration test - Material Color Only keeps the Principled constants it rebuilds over.

The bake replaces each material with a fresh Principled BSDF. Measured before
this test existed: a material with IOR 2 and Diffuse Roughness 0.5 exported
directly with its own specular reflectance, and its Material Color Only bake
exported the fresh node's IOR 1.5. Both constants change nothing the colour bake reads, so the bake must
author exactly what the direct export authors for them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2)
cube = bpy.context.active_object
cube.name = "Carried"
mat = bpy.data.materials.new("Carried")
nt = mat.node_tree
principled = nt.nodes["Principled BSDF"]
# A linked Base Color keeps the slot out of the flat-material shortcut.
color = nt.nodes.new("ShaderNodeRGB")
color.outputs["Color"].default_value = (0.2, 0.5, 0.3, 1.0)
nt.links.new(color.outputs["Color"], principled.inputs["Base Color"])
principled.inputs["IOR"].default_value = 2.0
principled.inputs["Diffuse Roughness"].default_value = 0.5
cube.data.materials.append(mat)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''

_CARRIED_INPUTS = ("specular", "specularIOR", "baseDiffuseRoughness")


def _surface_inputs(path: Path) -> dict:
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(path))
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if shader and shader.GetIdAttr().Get() == "ND_realitykit_pbr_surfaceshader_2_0":
            return {
                name: float(shader.GetInput(name).Get())
                for name in _CARRIED_INPUTS
                if shader.GetInput(name) and shader.GetInput(name).Get() is not None
            }
    raise AssertionError(f"no PBR Surface 2 shader in {path}")


def test_the_bake_authors_the_direct_exports_ior_and_diffuse_roughness(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "carried.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script), "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    direct = tmp_path / "direct" / "carried.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend), "-o", str(direct),
         "--format", "USDA"],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    baked = tmp_path / "baked" / "carried.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "bake-export", str(blend), "-o", str(baked),
         "--format", "USDA", "--bake-mode", "LIT_ALBEDO", "--resolution", "32", "--image-format", "PNG"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)

    expected = _surface_inputs(direct)
    # IOR 2 reaches the surface as specular, capped at RealityKit's F0 of 0.08;
    # the fresh node's IOR 1.5 would author 0.5. Diffuse Roughness is never
    # authored, so both exports must leave baseDiffuseRoughness out.
    assert expected["specular"] == pytest.approx(1.0)
    assert "baseDiffuseRoughness" not in expected
    actual = _surface_inputs(Path(payload["export_path"]))
    assert actual.keys() == expected.keys()
    for name, value in expected.items():
        assert actual[name] == pytest.approx(value, abs=1e-5), (name, actual, expected)
