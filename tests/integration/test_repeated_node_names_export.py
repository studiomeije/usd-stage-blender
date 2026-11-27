"""Integration test - repeated same-type nodes export under their own names.

A scan whose roughness is the green channel of its ORM map, plus a tile
detail whose roughness is the green channel of a second map: two Separate
Color nodes, each into a Math node. Both channel reads used to be authored at
one prim path, and the second rewired the first, so the scan's roughness
silently read the tile map. Each read must reach its own file.
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
import bpy, os, sys
out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)

def image(name, value):
    made = bpy.data.images.new(name, 4, 4, is_data=True)
    made.pixels = [0.2, value, 0.4, 1.0] * 16
    made.filepath_raw = os.path.join(os.path.dirname(out), name + ".png")
    made.file_format = "PNG"
    made.save()
    loaded = bpy.data.images.load(made.filepath_raw, check_existing=False)
    loaded.colorspace_settings.name = "Non-Color"
    return loaded

bpy.ops.mesh.primitive_cube_add()
material = bpy.data.materials.new("Scan")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
add = tree.nodes.new("ShaderNodeMath")
add.operation = "ADD"
for index, (name, value, weight) in enumerate((("scan_orm", 0.6, 0.7), ("tile_detail", 0.3, 0.3))):
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image(name, value)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    scale = tree.nodes.new("ShaderNodeMath")
    scale.operation = "MULTIPLY"
    scale.inputs[1].default_value = weight
    tree.links.new(texture.outputs["Color"], separate.inputs[0])
    tree.links.new(separate.outputs["Green"], scale.inputs[0])
    tree.links.new(scale.outputs[0], add.inputs[index])
tree.links.new(add.outputs[0], bsdf.inputs["Roughness"])
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _files_reaching(shader: UsdShade.Shader, seen=None) -> set:
    seen = set() if seen is None else seen
    files = set()
    if shader.GetPrim().GetPath() in seen:
        return files
    seen.add(shader.GetPrim().GetPath())
    for shader_input in shader.GetInputs():
        value = shader_input.Get()
        if hasattr(value, "path") and value.path:
            files.add(Path(value.path).name)
        for source in shader_input.GetConnectedSources()[0]:
            files |= _files_reaching(UsdShade.Shader(source.source.GetPrim()), seen)
    return files


def test_two_separate_color_reads_each_reach_their_own_map(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    output = tmp_path / "scene.usda"
    exported = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend),
         "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr

    stage = Usd.Stage.Open(str(output))
    adds = [
        UsdShade.Shader(prim) for prim in stage.Traverse()
        if UsdShade.Shader(prim) and UsdShade.Shader(prim).GetIdAttr().Get() == "ND_add_float"
    ]
    assert len(adds) == 1
    branches = []
    for name in ("in1", "in2"):
        (source,) = adds[0].GetInput(name).GetConnectedSources()[0]
        branches.append(_files_reaching(UsdShade.Shader(source.source.GetPrim())))
    assert all(len(files) == 1 for files in branches), branches
    (scan,), (tile,) = branches
    assert "scan_orm" in scan and "tile_detail" in tile, branches
