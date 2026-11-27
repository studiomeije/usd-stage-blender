"""Integration test - data maps export through a linear vector3 reader.

Measured on macOS 27.2.1, RealityKit decodes every ``ND_image_color3`` read as
sRGB, untagged or tagged, except ``lin_rec709`` over a three-channel file; a
texel of 128 then reads 0.216 instead of 0.502. An untagged
``ND_image_vector3`` reads 0.502 for RGB and greyscale files alike. So a
Non-Color roughness map, greyscale or RGB, read directly, through Separate
Color, or through a Math node, must reach the surface through an untagged
vector3 reader, and the colour map beside it stays a tagged color3 read.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable
from PIL import Image
from pxr import Usd, UsdShade


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, os, sys
out, channels, wiring = sys.argv[sys.argv.index("--") + 1:][:3]

bpy.ops.wm.read_factory_settings(use_empty=True)
folder = os.path.dirname(out)

def save(name, pixels, grey):
    image = bpy.data.images.new(name, 8, 8, is_data=True)
    image.pixels = pixels
    image.filepath_raw = os.path.join(folder, name + ".png")
    image.file_format = "PNG"
    if grey:
        settings = bpy.context.scene.render.image_settings
        settings.color_mode = "BW"
        image.save_render(image.filepath_raw)
    else:
        image.save()
    loaded = bpy.data.images.load(image.filepath_raw, check_existing=False)
    return loaded

rough = save("rough", [0.502, 0.502, 0.502, 1.0] * 64, channels == "grey")
rough.colorspace_settings.name = "Non-Color"
albedo = save("albedo", [0.8, 0.4, 0.2, 1.0] * 64, False)
albedo.colorspace_settings.name = "sRGB"

bpy.ops.mesh.primitive_cube_add()
material = bpy.data.materials.new("Scan")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
base = tree.nodes.new("ShaderNodeTexImage")
base.image = albedo
tree.links.new(base.outputs["Color"], bsdf.inputs["Base Color"])
texture = tree.nodes.new("ShaderNodeTexImage")
texture.image = rough
if wiring == "direct":
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Roughness"])
elif wiring == "separate":
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(texture.outputs["Color"], separate.inputs[0])
    tree.links.new(separate.outputs["Green"], bsdf.inputs["Roughness"])
elif wiring == "math":
    math = tree.nodes.new("ShaderNodeMath")
    math.operation = "MULTIPLY"
    math.inputs[1].default_value = 0.9
    tree.links.new(texture.outputs["Color"], math.inputs[0])
    tree.links.new(math.outputs[0], bsdf.inputs["Roughness"])
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _export(tmp_path: Path, channels: str, wiring: str) -> Usd.Stage:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), channels, wiring],
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
    return Usd.Stage.Open(str(output))


def _readers(stage: Usd.Stage) -> dict:
    found = {}
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if not shader or not str(shader.GetIdAttr().Get() or "").startswith("ND_image"):
            continue
        file_input = shader.GetInput("file")
        found[Path(file_input.Get().path).stem] = (
            shader.GetIdAttr().Get(), file_input.GetAttr().GetColorSpace()
        )
    return found


@pytest.mark.parametrize("channels", ["grey", "rgb"])
@pytest.mark.parametrize("wiring", ["direct", "separate", "math"])
def test_a_roughness_map_reads_through_an_untagged_vector3_reader(tmp_path, channels, wiring):
    readers = _readers(_export(tmp_path, channels, wiring))
    with Image.open(tmp_path / "rough.png") as source:
        assert source.mode == ("L" if channels == "grey" else "RGB")

    rough = next(value for key, value in readers.items() if "rough" in key)
    albedo = next(value for key, value in readers.items() if "albedo" in key)
    assert rough == ("ND_image_vector3", "")
    assert albedo == ("ND_image_color3", "srgb_texture")
