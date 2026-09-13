"""Integration test — a tiled texture (Mapping node) is exportable.

Exports used to carry two networks per material: the MaterialX ShaderGraph
RealityKit consumes, and Blender's UsdPreviewSurface network. One Mapping node
appeared in both - as a MaterialX place2d (texcoord UV0, reciprocal SRT scale)
and as a UsdTransform2d (texcoord st, direct scale). The preflight counted the
two networks together, so ANY non-identity Mapping produced
distinct_transform_count == 2 and the export failed:

    validate  -> ok: True, errors: 0
    export    -> EXPORT_FAILED  MATERIAL_TEXTURE_TRANSFORM_CONFLICT

while the message told the artist to "use one identical transform ... or bake"
— a conflict the exporter created between its own two networks. Tiling a
texture is the second material graph most artists build. The preview network is
now removed from every export, and this still guards the place2d arithmetic.
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

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
texture = sys.argv[sys.argv.index("--") + 2]
two_transforms = sys.argv[sys.argv.index("--") + 3] == "1"

bpy.ops.wm.read_factory_settings(use_empty=True)

image = bpy.data.images.new("grid", 16, 16)
image.generated_type = 'COLOR_GRID'
image.filepath_raw = texture
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)

bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("Tiled")
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
coords = tree.nodes.new("ShaderNodeTexCoord")

targets = [(3.0, "Base Color")]
if two_transforms:
    targets.append((7.0, "Roughness"))

for scale, target in targets:
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, scale)
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(texture)
    if target == "Roughness":
        node.image.colorspace_settings.name = 'Non-Color'
    tree.links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    tree.links.new(node.outputs["Color"], bsdf.inputs[target])

obj.data.materials.append(material)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _build_and_export(tmp_path: Path, *, two_transforms: bool):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "tiled.blend"
    texture = tmp_path / "grid.png"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(texture), "1" if two_transforms else "0"],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    stage = tmp_path / "out" / "tiled.usda"
    stage.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"),
         "export", str(blend), "-o", str(stage), "--format", "USDA"],
        capture_output=True, text=True, timeout=600,
    )
    return result, stage


def test_single_mapping_transform_exports(tmp_path):
    result, stage = _build_and_export(tmp_path, two_transforms=False)

    assert result.returncode == 0, (
        "a single Mapping node is plain texture tiling and must export\n"
        + result.stdout + result.stderr
    )
    text = stage.read_text()
    # Scope to the place2d shader block, which carries the reciprocal scale.
    place2d = re.search(
        r'def Shader "place2d[^"]*"\s*\{(.*?)\n            \}', text, re.S
    )
    assert place2d, "no place2d shader authored"
    match = re.search(
        r'float2 inputs:scale = \(([\d.]+), ([\d.]+)\)', place2d.group(1)
    )
    assert match, "no scale on the place2d shader"
    # Blender Scale 3 under the place2d SRT convention is the reciprocal.
    assert abs(float(match.group(1)) - 1.0 / 3.0) < 1e-4, match.group(0)


def test_no_preview_network_ships(tmp_path):
    """Blender's UsdPreviewSurface network is removed from every export.

    RealityKit never reads it: a kit copy without it imported silently into
    Reality Composer Pro, passed usdchecker --arkit --strict, and rendered
    unchanged in Quick Look. The texture it shared must still be referenced by
    the MaterialX reader, or removing the network would have dropped it.
    """
    result, stage = _build_and_export(tmp_path, two_transforms=False)

    assert result.returncode == 0, result.stdout + result.stderr
    text = stage.read_text()
    for schema in ("UsdPreviewSurface", "UsdUVTexture", "UsdTransform2d", "UsdPrimvarReader"):
        assert schema not in text, schema
    for terminal in ("token outputs:surface", "token outputs:displacement"):
        assert terminal not in text, terminal
    assert 'info:id = "ND_image_color3"' in text
    assert re.search(r"asset inputs:file = @[^@]+\.png@", text)


def test_two_genuinely_distinct_transforms_are_still_refused(tmp_path):
    result, _stage = _build_and_export(tmp_path, two_transforms=True)

    assert result.returncode != 0, (
        "two different Mapping transforms in one material exceed RealityKit's "
        "one-transform limit and must be refused"
    )
