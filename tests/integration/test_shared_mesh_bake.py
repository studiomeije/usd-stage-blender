"""Integration test — instances sharing one mesh datablock bake independently.

Material slots are DATA-linked by default, so assigning a baked material writes
it onto the shared mesh datablock. With a linked duplicate (Alt+D — the standard
Blender instancing idiom) the last instance to bake overwrote every earlier
one's slot. In LIT_IBL the reuse cache is deliberately disabled so each instance
captures its own lighting, which made this maximally visible: every instance was
bound to the last instance's bake, so a cube in full sun exported black because
its duplicate sat under an occluder.

LIT_IBL is the default bake mode.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Two cubes sharing one mesh datablock, lit by a sun, with an occluder over the
# second one only. Their bakes must therefore differ.
_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 4

bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 0))
first = bpy.context.active_object
first.name = "CubeA"
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("Shared")
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
    1.0, 1.0, 1.0, 1.0,
)
first.data.materials.append(material)

# Linked duplicate: the same mesh datablock, which is the whole point.
second = first.copy()
second.data = first.data
second.name = "CubeB"
second.location = (6, 0, 0)
scene.collection.objects.link(second)
assert first.data is second.data

bpy.ops.mesh.primitive_plane_add(size=8, location=(6, 0, 3))
bpy.context.active_object.name = "Occluder"

bpy.ops.object.light_add(type='SUN', location=(0, 0, 10))
bpy.context.object.data.energy = 5.0

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


@pytest.fixture(scope="module")
def shared_mesh_export(tmp_path_factory) -> Path:
    """Bake-export the linked-duplicate scene; returns the output directory."""
    workdir = tmp_path_factory.mktemp("shared_mesh")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "shared.blend"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    out_dir = workdir / "out"
    out_dir.mkdir()
    exported = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "bake-export", str(blend),
         "-o", str(out_dir / "s.usda"), "--format", "USDA",
         "--bake-mode", "LIT_IBL", "--resolution", "64"],
        capture_output=True, text=True, timeout=900,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    return out_dir


def _bindings(stage: Path) -> list[str]:
    import re
    return re.findall(r"material:binding = <([^>]+)>", stage.read_text())


def test_each_instance_gets_its_own_baked_material(shared_mesh_export):
    bindings = _bindings(shared_mesh_export / "s.usda")

    assert len(bindings) == 2, f"expected one binding per cube, got {bindings}"
    assert len(set(bindings)) == 2, (
        "both instances bound to the same baked material, so one of them is "
        f"showing the other's lighting: {bindings}"
    )


def test_each_instance_gets_its_own_baked_texture(shared_mesh_export):
    """The occluded cube and the lit cube cannot have identical pixels."""
    textures = [
        path for path in (shared_mesh_export / "textures").rglob("*")
        if path.is_file()
    ]
    assert textures, "no baked textures were published"

    # Assert on distinct *content*, not file count: the same baked image is
    # currently staged under two names because it reaches staging through two
    # asset paths - see the note on _remove_unreferenced_superseded_textures in
    # Plugin/export/usd_textures.py. That duplication is orthogonal to this
    # regression.
    digests = {hashlib.sha256(path.read_bytes()).hexdigest() for path in textures}
    assert len(digests) == 2, (
        "expected one distinct bake per instance - the lit cube and the "
        "occluded cube cannot have identical pixels. Got "
        f"{len(digests)} distinct texture(s) from {len(textures)} file(s); the "
        "per-instance lighting bake collapsed onto the shared mesh datablock"
    )
