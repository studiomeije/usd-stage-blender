"""Integration test — one texture in the scene, one texture payload in the USDZ.

A packed image makes ``_build_export_kwargs`` choose ``export_textures_mode=
'NEW'``, so Blender's native exporter writes its own copy for the preview
network while the MaterialX pass stages a packed-image snapshot under a
different filename. Both carry identical bytes; before content-digest reuse in
``_finalize_content_addressed_texture`` the package shipped that payload twice
under two content-addressed names.
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
texture = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)

image = bpy.data.images.new("grid", 16, 16)
image.generated_type = 'COLOR_GRID'
image.filepath_raw = texture
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)

bpy.ops.mesh.primitive_plane_add(location=(0, 0, 0))
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')
material = bpy.data.materials.new("PackedTex")
obj.data.materials.append(material)
tree = material.node_tree
node = tree.nodes.new("ShaderNodeTexImage")
node.image = bpy.data.images.load(texture)
node.image.pack()
tree.links.new(
    node.outputs["Color"],
    tree.nodes["Principled BSDF"].inputs["Base Color"],
)

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def test_single_texture_scene_packages_exactly_one_texture_payload(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "packed.blend"
    texture = tmp_path / "grid.png"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(texture)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    package = tmp_path / "out" / "packed.usdz"
    package.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"),
         "export", str(blend), "-o", str(package), "--format", "USDZ"],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    with zipfile.ZipFile(package) as archive:
        members = archive.namelist()
        texture_members = [name for name in members if name.endswith(".png")]
        payloads = {archive.read(name) for name in texture_members}

    assert len(texture_members) == 1, (
        f"expected exactly one packaged texture, got {texture_members}"
    )
    assert len(payloads) == 1
