"""Integration test — a successful export's warnings actually reach the user.

Measured before this existed: a Lighting & Shadows bake of a scene with no
World and no lights produced black textures, printed "Done in 34.5s", exited 0,
and its (well-written) warning reached no surface at all — not stdout, not
stderr, not the job card. It lived only inside a diagnostics sidecar that the
result payload denied existed, because the cleanup path wrote the file
unconditionally while reporting diagnostics_path: null.

Three contracts pinned here:
1. success payloads carry a ``warnings`` array,
2. human mode prints them to stderr (not gated by --quiet - that flag
   suppresses progress, and "your textures will be black" is not progress),
3. a clean bake writes no phantom sidecar.
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
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1
scene.world = None                       # nothing illuminates the scene

bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("M")
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
    1.0, 1.0, 1.0, 1.0,
)
obj.data.materials.append(material)

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


@pytest.fixture(scope="module")
def dark_bake(tmp_path_factory):
    """Bake the unlit scene once; every test inspects the same run."""
    workdir = tmp_path_factory.mktemp("warnings")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "dark.blend"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    out_dir = workdir / "out"
    out_dir.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "bake-export", str(blend), "-o", str(out_dir / "dark.usda"),
         "--format", "USDA", "--bake-mode", "LIT_IBL", "--resolution", "64"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout), out_dir, blend


def test_success_payload_carries_the_warning(dark_bake):
    payload, _out_dir, _blend = dark_bake

    assert "warnings" in payload, "success payloads must carry a warnings array"
    assert any("no World and no light objects" in w for w in payload["warnings"]), (
        "a bake that can only be black must say so in the result, "
        f"got: {payload['warnings']}"
    )


def test_clean_bake_writes_no_phantom_sidecar(dark_bake):
    """The cleanup path used to write <output>.diagnostics.json on every bake
    while the payload reported diagnostics_path: null."""
    payload, out_dir, _blend = dark_bake

    sidecars = list(out_dir.glob("*.diagnostics.json"))
    assert payload.get("diagnostics_path") is None
    assert sidecars == [], (
        f"phantom sidecar(s) written on a clean bake: {[p.name for p in sidecars]}"
    )


def test_human_mode_prints_the_warning_to_stderr(dark_bake, tmp_path):
    _payload, _out_dir, blend = dark_bake

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"),
         "bake-export", str(blend), "-o", str(tmp_path / "dark.usda"),
         "--format", "USDA", "--bake-mode", "LIT_IBL", "--resolution", "64"],
        capture_output=True, text=True, timeout=900,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no World and no light objects" in result.stderr, (
        "the warning must reach a default surface, not only the JSON payload"
    )
