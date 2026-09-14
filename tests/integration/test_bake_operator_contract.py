"""Integration test — bpy.ops.object.bake argument contract, real pixels.

Blender fills any bake property left unset from ``scene.render.bake``. A .blend
saved with a different bake target or with COMBINED lighting passes disabled
therefore silently redirects or empties the bake, while the operator still
reports {'FINISHED'}. The result is an all-black texture that is saved and
packaged with no error anywhere in the pipeline.

These tests poison the scene exactly that way and assert on the pixels the real
operator produces, so they fail if the pinned arguments in ``_bake_object_pass``
are ever dropped again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "BAKE_CONTRACT_RESULT:"

# Runs inside Blender. Builds a plane that also carries a color attribute (so a
# VERTEX_COLORS bake succeeds instead of erroring), poisons scene.render.bake,
# then bakes through the real code path and reports the target image's pixels.
DRIVER = '''
import json, sys
sys.path.insert(0, {repo_root!r})

import bpy
from Plugin.export import bake_textures

MARKER = {marker!r}


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.render.engine = 'CYCLES'
    bpy.context.scene.cycles.samples = 4
    bpy.ops.mesh.primitive_plane_add()
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode='OBJECT')
    # Without a color attribute a VERTEX_COLORS bake hard-errors; with one it
    # succeeds silently, which is the corrupting case worth guarding.
    obj.data.color_attributes.new(name="Col", type='FLOAT_COLOR', domain='POINT')

    mat = bpy.data.materials.new("M")
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
        1.0, 1.0, 1.0, 1.0,
    )
    obj.data.materials.append(mat)

    image = bpy.data.images.new("Target", 16, 16, alpha=True)
    node = mat.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    mat.node_tree.nodes.active = node

    bpy.ops.object.light_add(type='SUN', location=(0, 0, 3))
    bpy.context.object.data.energy = 5.0

    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj, image


def mean_red(image):
    pixels = list(image.pixels)
    return sum(pixels[0::4]) / (len(pixels) // 4)


results = {{}}

# 1. Scene saved with a vertex-color bake target.
obj, image = build()
bpy.context.scene.render.bake.target = 'VERTEX_COLORS'
bake_textures._bake_object_pass(
    bpy.context, obj, bake_type='DIFFUSE', pass_filter={{'COLOR'}}, margin=4,
)
results["poisoned_target"] = mean_red(image)

# 2. Scene saved with COMBINED lighting passes disabled (the LIT_IBL path,
#    which reaches _bake_object_pass with pass_filter=None).
obj, image = build()
bpy.context.scene.render.bake.use_pass_direct = False
bpy.context.scene.render.bake.use_pass_indirect = False
bake_textures._bake_object_pass(
    bpy.context, obj, bake_type='COMBINED', pass_filter=None, margin=4,
)
results["poisoned_passes"] = mean_red(image)

print(MARKER + json.dumps(results))
'''


def _run_driver(tmp_path: Path) -> dict:
    blender = blender_executable()

    script = tmp_path / "driver.py"
    script.write_text(DRIVER.format(repo_root=str(REPO_ROOT), marker=MARKER))

    proc = subprocess.run(
        [blender, "--background", "--factory-startup", "--python", str(script)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    for line in proc.stdout.splitlines():
        if line.startswith(MARKER):
            return json.loads(line[len(MARKER):])
    raise AssertionError(
        "Driver did not report a result.\n"
        f"exit={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


@pytest.fixture(scope="module")
def bake_results(tmp_path_factory) -> dict:
    return _run_driver(tmp_path_factory.mktemp("bake_contract"))


def test_bake_ignores_scene_vertex_color_target(bake_results):
    """A .blend saved with target='VERTEX_COLORS' must not empty the texture."""
    mean = bake_results["poisoned_target"]
    assert mean > 0.5, (
        "Bake wrote nothing to the target image (mean red "
        f"{mean:.4f}); the operator inherited scene.render.bake.target"
    )


def test_combined_bake_ignores_disabled_scene_passes(bake_results):
    """A .blend with use_pass_direct/indirect off must not bake black."""
    mean = bake_results["poisoned_passes"]
    assert mean > 0.5, (
        "COMBINED bake produced a black texture (mean red "
        f"{mean:.4f}); the operator inherited the scene's use_pass_* toggles"
    )
