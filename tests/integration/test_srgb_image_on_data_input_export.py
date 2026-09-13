"""Integration test — an sRGB image feeding data inputs exports decoded, as Cycles reads it.

Cycles decodes an sRGB image before any node reads it, so Roughness fed
directly, through a Math node, through Separate Color, or by the image that
also drives Base Color reads decoded values. Each exports with the image
reader authored ``srgb_texture``, and ``validate`` and ``export`` both warn,
naming the Blender image. An sRGB image reaching only colour inputs, or read
through its Alpha output, draws no warning.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, os, sys
out = sys.argv[sys.argv.index("--") + 1]
wiring = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)
image_path = os.path.join(os.path.dirname(out), "gradient.png")
image = bpy.data.images.new("gradient", 8, 8)
image.pixels = [c for y in range(8) for x in range(8) for c in (x / 7, y / 7, 0.5, 1.0)]
image.filepath_raw = image_path
image.file_format = "PNG"
image.save()
image = bpy.data.images.load(image_path, check_existing=False)
image.name = "Rough Map"
image.colorspace_settings.name = "sRGB"

bpy.ops.mesh.primitive_cube_add()
material = bpy.data.materials.new("Tagged")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
texture = tree.nodes.new("ShaderNodeTexImage")
texture.image = image
if wiring == "direct":
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Roughness"])
elif wiring == "math":
    math = tree.nodes.new("ShaderNodeMath")
    math.operation = "MULTIPLY"
    math.inputs[1].default_value = 0.8
    tree.links.new(texture.outputs["Color"], math.inputs[0])
    tree.links.new(math.outputs[0], bsdf.inputs["Roughness"])
elif wiring == "separate":
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(texture.outputs["Color"], separate.inputs[0])
    tree.links.new(separate.outputs["Red"], bsdf.inputs["Roughness"])
elif wiring == "colour_and_data":
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Roughness"])
elif wiring == "colour_only":
    hue = tree.nodes.new("ShaderNodeHueSaturation")
    tree.links.new(texture.outputs["Color"], hue.inputs["Color"])
    tree.links.new(hue.outputs["Color"], bsdf.inputs["Base Color"])
elif wiring == "alpha":
    tree.links.new(texture.outputs["Alpha"], bsdf.inputs["Roughness"])
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


_NOTICE = "Image 'Rough Map' is tagged sRGB and feeds a data input"


def _build(tmp_path: Path, wiring: str) -> Path:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), wiring],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", *args],
        capture_output=True, text=True, timeout=900,
    )


@pytest.mark.parametrize("wiring", ["direct", "math", "separate", "colour_and_data"])
def test_an_srgb_image_on_a_data_input_exports_decoded_with_a_warning(tmp_path, wiring):
    blend = _build(tmp_path, wiring)

    validated = _cli("validate", str(blend))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert _NOTICE in validated.stdout

    output = tmp_path / "scene.usda"
    exported = _cli("export", str(blend), "-o", str(output), "--format", "USDA")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    log = exported.stdout + exported.stderr
    assert _NOTICE in log
    assert "Image file '" not in log

    authored = output.read_text()
    assert 'colorSpace = "srgb_texture"' in authored
    assert 'colorSpace = "raw"' not in authored


@pytest.mark.parametrize("wiring", ["colour_only", "alpha"])
def test_an_srgb_image_on_colour_inputs_or_through_alpha_draws_no_warning(tmp_path, wiring):
    blend = _build(tmp_path, wiring)

    validated = _cli("validate", str(blend))
    assert validated.returncode == 0, validated.stdout + validated.stderr
    assert "tagged sRGB" not in validated.stdout

    exported = _cli("export", str(blend), "-o", str(tmp_path / "scene.usda"), "--format", "USDA")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert "tagged sRGB" not in exported.stdout + exported.stderr
