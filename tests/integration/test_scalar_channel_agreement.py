"""Integration - a scalar input fed by a colour texture reads it as Blender does.

When a roughness, metallic or alpha input is driven by an Image Texture's Color
output, Blender converts the colour to a float at the socket with Cycles'
``linear_rgb_to_gray``: a weighted sum of all three channels. The exporter once
read the red channel (and, for Alpha, the file's alpha channel) instead, to
agree with a UsdPreviewSurface fallback that exports no longer carry. On any
chromatic map that exported a different surface from the one Blender renders.

The Alpha output is different: it is the file's alpha channel, read alone.

The reference is Cycles, measured: rendering RGB to BW of pure red, green and
blue as emission in Blender 5.2's Cycles gives 0.21263909, 0.71516913 and
0.07219274. The test reads the weights back from the arithmetic that runs - the
dot product's second operand - rather than from a channels string.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Cycles' linear_rgb_to_gray weights, measured by rendering (see the docstring).
CYCLES_GRAY = (0.21263909, 0.71516913, 0.07219274)

#: A deliberately chromatic texture: reading one channel and reading the gray
#: conversion give different numbers, which is the whole point of the scene.
_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
texture = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)

image = bpy.data.images.new("chroma", 8, 8, alpha=True)
pixels = []
for i in range(8 * 8):
    pixels += [0.9, 0.4, 0.1, 0.5]   # r != g != b != a, so a channel swap shows
image.pixels = pixels
image.filepath_raw = texture
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)

def plane(name, x, socket, output="Color"):
    bpy.ops.mesh.primitive_plane_add(location=(x, 0, 0))
    obj = bpy.context.active_object
    obj.name = "Obj_" + name
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode='OBJECT')
    material = bpy.data.materials.new(name)
    obj.data.materials.append(material)
    tree = material.node_tree
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(texture)
    node.image.colorspace_settings.name = 'Non-Color'
    tree.links.new(
        node.outputs[output],
        tree.nodes["Principled BSDF"].inputs[socket],
    )

plane("RoughMat", 0.0, "Roughness")
plane("MetalMat", 3.0, "Metallic")
plane("AlphaColorMat", 6.0, "Alpha")
plane("AlphaOutputMat", 9.0, "Alpha", output="Alpha")

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _material_scope(text: str, material_name: str) -> str:
    match = re.search(
        rf'def Material "{material_name}".*?(?=\n        def Material |\Z)',
        text,
        re.S,
    )
    assert match, f"material {material_name} not found"
    return match.group(0)


@pytest.fixture(scope="module")
def channel_export(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("channels")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "channels.blend"
    texture = workdir / "chroma.png"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(texture)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    stage = workdir / "out" / "channels.usda"
    stage.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"),
         "export", str(blend), "-o", str(stage), "--format", "USDA"],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return stage.read_text()


def _scalar_weights(scope: str, input_name: str):
    """The weights the MaterialX network applies to the texture's channels.

    Both a component read and the gray conversion are ``convert`` +
    ``dotproduct``; the dot product's constant operand is the arithmetic that
    actually runs.
    """
    surface = re.search(
        rf'inputs:{input_name}\.connect = </[^>]*/(\w+)\.outputs:out>', scope
    )
    assert surface, f"MaterialX network does not drive {input_name} from a node"
    node = re.search(
        rf'def Shader "{surface.group(1)}"\s*\{{(.*?)\n            \}}', scope, re.S
    )
    assert node, f"node {surface.group(1)} not found"
    assert 'info:id = "ND_dotproduct_vector' in node.group(1), node.group(1)
    weights = re.search(r'inputs:in2 = \(([^)]*)\)', node.group(1))
    assert weights, f"node {surface.group(1)} authors no weights"
    return tuple(float(v) for v in weights.group(1).split(","))


@pytest.mark.parametrize(
    ("material", "input_name"),
    [("RoughMat", "roughness"), ("MetalMat", "metallic"), ("AlphaColorMat", "opacity")],
)
def test_a_scalar_input_fed_a_colour_reads_blenders_gray(channel_export, material, input_name):
    scope = _material_scope(channel_export, material)
    assert "ND_luminance" not in scope, "ND_luminance weighs with ACEScg coefficients"
    assert _scalar_weights(scope, input_name) == pytest.approx(CYCLES_GRAY, abs=1e-6)


def test_the_alpha_output_reads_the_alpha_channel(channel_export):
    scope = _material_scope(channel_export, "AlphaOutputMat")
    assert _scalar_weights(scope, "opacity") == (0.0, 0.0, 0.0, 1.0)


@pytest.mark.parametrize("material", ["RoughMat", "MetalMat"])
def test_no_preview_network_competes_for_the_channel(channel_export, material):
    scope = _material_scope(channel_export, material)
    assert "UsdUVTexture" not in scope and "UsdPreviewSurface" not in scope
