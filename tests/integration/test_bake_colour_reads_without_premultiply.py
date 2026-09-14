"""Integration test - a baked colour texture feeds the surface as stored.

A baked base-colour image is RGBA, and an opaque bake never reads its alpha.
Left straight-alpha, Cycles would hand its Color multiplied by alpha, and the
export would rebuild that as a multiply, an sRGB decode and a combine in front
of every baked texture. The bake reads such an image channel-packed instead,
so the image node connects to the surface directly.
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
cube.name = "Noisy"
mat = bpy.data.materials.new("Noisy")
nt = mat.node_tree
principled = nt.nodes["Principled BSDF"]
noise = nt.nodes.new("ShaderNodeTexNoise")
nt.links.new(noise.outputs["Color"], principled.inputs["Base Color"])
cube.data.materials.append(mat)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''

_PREMULTIPLY_NODES = ("ND_multiply_color4", "ND_ifgreater_float", "ND_power_float", "ND_combine3_color3")


def _shader_ids(path: Path) -> set[str]:
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(path))
    return {
        str(UsdShade.Shader(prim).GetIdAttr().Get())
        for prim in stage.Traverse()
        if UsdShade.Shader(prim)
    }


@pytest.mark.parametrize("mode", ["UNLIT_ALBEDO", "LIT_IBL"])
def test_baked_colour_texture_connects_without_a_premultiply_chain(tmp_path, mode):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "noisy.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script), "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    baked = tmp_path / "baked" / "noisy.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "bake-export", str(blend), "-o", str(baked),
         "--format", "USDA", "--bake-mode", mode, "--resolution", "32", "--image-format", "PNG"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)

    ids = _shader_ids(Path(payload["export_path"]))
    assert any(shader_id.startswith("ND_image_") for shader_id in ids), ids
    assert not ids.intersection(_PREMULTIPLY_NODES), ids
