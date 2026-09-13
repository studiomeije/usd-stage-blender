"""Integration test — a blend file in another working colour space is refused.

The export tags every material colour ``lin_rec709_scene`` and converts
nothing, so a Blender 5.2 file whose working space is ACEScg or Linear
Rec.2020 would export its scene-linear values under the wrong primaries.
Both ``validate`` and ``export`` refuse it by name; the same file in Linear
Rec.709 exports.
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
working_space = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)
if working_space != "Linear Rec.709":
    bpy.ops.wm.set_working_color_space(working_space=working_space)
assert bpy.data.colorspace.working_space == working_space

bpy.ops.mesh.primitive_cube_add()
material = bpy.data.materials.new("Paint")
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1.0)
bpy.context.active_object.data.materials.append(material)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _build(tmp_path: Path, working_space: str) -> Path:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), working_space],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", *args],
        capture_output=True, text=True, timeout=900,
    )


def test_an_acescg_file_is_refused_by_validate_and_export(tmp_path):
    blend = _build(tmp_path, "ACEScg")

    validated = _cli("validate", str(blend))
    assert "working color space is 'ACEScg'" in validated.stdout + validated.stderr

    exported = _cli("export", str(blend), "-o", str(tmp_path / "scene.usda"), "--format", "USDA")
    assert exported.returncode != 0
    assert "working color space is 'ACEScg'" in exported.stdout + exported.stderr


def test_a_linear_rec709_file_exports(tmp_path):
    blend = _build(tmp_path, "Linear Rec.709")
    exported = _cli("export", str(blend), "-o", str(tmp_path / "scene.usda"), "--format", "USDA")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert json.loads(exported.stdout)
