"""Integration test — the surface comes from the output Cycles renders, and a material without one is refused up front.

Cycles renders the active Material Output whose target is All or Cycles. A
material with no such output, or with nothing linked to its Surface, renders no
surface: ``validate`` refuses it by name instead of reporting it clean and
letting the export fail later. An active EEVEE-only output beside a Cycles one
exports the Cycles one. A Color Ramp with a single stop is that stop's colour
everywhere, as Blender evaluates it, and exports.
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
case = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
material = bpy.data.materials.new("Case")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
output = tree.nodes["Material Output"]
bsdf = tree.nodes["Principled BSDF"]
if case == "no_output":
    tree.nodes.remove(output)
elif case == "unlinked_surface":
    tree.links.remove(output.inputs["Surface"].links[0])
elif case == "eevee_only":
    output.target = "EEVEE"
elif case == "eevee_beside_cycles":
    output.target = "EEVEE"
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.0, 0.9, 0.2, 1.0)
    cycles = tree.nodes.new("ShaderNodeOutputMaterial")
    cycles.target = "CYCLES"
    tree.links.new(emission.outputs["Emission"], cycles.inputs["Surface"])
    output.is_active_output = True
    assert tree.get_output_node("CYCLES") == cycles
elif case == "one_stop_ramp":
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements.remove(ramp.color_ramp.elements[1])
    ramp.color_ramp.elements[0].color = (0.8, 0.2, 0.1, 1.0)
    tree.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _build(tmp_path: Path, case: str) -> Path:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), case],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", *args],
        capture_output=True, text=True, timeout=900,
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no_output", "no Material Output that Cycles renders"),
        ("eevee_only", "no Material Output that Cycles renders"),
        ("unlinked_surface", "Nothing is linked to Surface"),
    ],
)
def test_a_material_without_a_cycles_surface_is_refused_by_validate(tmp_path, case, reason):
    blend = _build(tmp_path, case)

    validated = _cli("validate", str(blend))
    assert validated.returncode != 0
    assert json.loads(validated.stdout)["ok"] is False
    assert reason in validated.stdout

    exported = _cli("export", str(blend), "-o", str(tmp_path / "scene.usda"), "--format", "USDA")
    assert exported.returncode != 0
    assert reason in exported.stdout + exported.stderr
    assert "could not be mapped" not in exported.stdout + exported.stderr


def test_an_active_eevee_output_beside_a_cycles_one_exports_the_cycles_surface(tmp_path):
    blend = _build(tmp_path, "eevee_beside_cycles")
    output = tmp_path / "scene.usda"

    exported = _cli("export", str(blend), "-o", str(output), "--format", "USDA")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    authored = output.read_text()
    assert "inputs:color = (0, 0.9, 0.2)" in authored
    assert "baseColor" not in authored


def test_a_one_stop_color_ramp_exports_as_its_stop(tmp_path):
    blend = _build(tmp_path, "one_stop_ramp")
    output = tmp_path / "scene.usda"

    assert json.loads(_cli("validate", str(blend)).stdout)["ok"] is True
    exported = _cli("export", str(blend), "-o", str(output), "--format", "USDA")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert "baseColor = (0.8, 0.2, 0.1)" in output.read_text()
