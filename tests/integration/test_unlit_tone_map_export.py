"""Integration test - an unlit material can opt out of RealityKit's tone map.

RealityKit's unlit surface tone-maps its colour unless
``applyPostProcessToneMap`` is false (measured on macOS 27.2.1: with the
camera's tone mapping on, a 0.18 unlit grey reads 0.129 by default and 0.180
with it false). The material property ``usd_stage_unlit_tonemap = False``
authors it; without the property nothing changes.
"""

from __future__ import annotations

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
bpy.ops.mesh.primitive_plane_add()
material = bpy.data.materials.new("Screen")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
tree.nodes.remove(tree.nodes["Principled BSDF"])
emission = tree.nodes.new("ShaderNodeEmission")
emission.inputs["Color"].default_value = (0.18, 0.18, 0.18, 1.0)
tree.links.new(emission.outputs[0], tree.nodes["Material Output"].inputs["Surface"])
if case == "off":
    material["usd_stage_unlit_tonemap"] = False
elif case == "on":
    material["usd_stage_unlit_tonemap"] = True
elif case == "invalid":
    material["usd_stage_unlit_tonemap"] = "sometimes"
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


@pytest.mark.parametrize("case,authored", [("off", False), ("on", None), ("unset", None)])
def test_the_property_switches_the_unlit_tone_map(tmp_path, case, authored):
    exported, output = _export(tmp_path, case)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    stage = Usd.Stage.Open(str(output))
    unlit = next(
        UsdShade.Shader(p) for p in stage.Traverse()
        if UsdShade.Shader(p).GetIdAttr().Get() == "ND_realitykit_unlit_surfaceshader"
    )
    tone_map = unlit.GetInput("applyPostProcessToneMap")
    assert (tone_map.Get() if tone_map else None) is authored


def test_a_value_that_is_not_a_boolean_stops_the_export(tmp_path):
    exported, _output = _export(tmp_path, "invalid")
    assert exported.returncode != 0
    assert "usd_stage_unlit_tonemap" in exported.stdout + exported.stderr
