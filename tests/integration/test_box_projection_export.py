"""Integration - Image Texture Box projection exports; Sphere and Tube refuse.

Every non-Flat projection used to fall through the Flat path and export a plain
UV-sampled image. Box projection is now Cycles' own weighting of three image
reads, each on the same staged file; Sphere and Tube refuse with bake advice.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys, os
out = sys.argv[sys.argv.index("--") + 1]
texdir = sys.argv[sys.argv.index("--") + 2]
projection = sys.argv[sys.argv.index("--") + 3]

bpy.ops.wm.read_factory_settings(use_empty=True)

path = os.path.join(texdir, "boxtex.png")
image = bpy.data.images.new("boxtex", 8, 8)
image.generated_color = (0.8, 0.4, 0.2, 1.0)
image.filepath_raw = path
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)
loaded = bpy.data.images.load(path)

bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("BoxProjected")
obj.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
tex = tree.nodes.new("ShaderNodeTexImage")
tex.image = loaded
tex.projection = projection
if projection == 'BOX':
    tex.projection_blend = 0.3
tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _build_blend(tmp_path: Path, projection: str) -> Path:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / f"box_{projection.lower()}.blend"
    texdir = tmp_path / "textures"
    texdir.mkdir(exist_ok=True)
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(texdir), projection],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _export(blend: Path, out_dir: Path):
    out_dir.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "export", str(blend), "-o", str(out_dir / "box.usda"),
         "--format", "USDA", "--diagnostics"],
        capture_output=True, text=True, timeout=900,
    )


@pytest.fixture(scope="module")
def box_export(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("box_projection")
    blend = _build_blend(workdir, "BOX")
    result = _export(blend, workdir / "out")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload, workdir / "out" / "box.usda"


def test_box_projection_samples_the_staged_file_per_side(box_export):
    """Box projection is three image reads weighted as Cycles weights them
    (tests/unit/test_texture_nodes_exact.py pins the arithmetic); every read
    points at the same staged file."""
    _payload, stage = box_export
    text = stage.read_text()
    assert "ND_triplanarprojection" not in text
    files = re.findall(r'asset inputs:file = @([^@]+)@', text)
    assert len(files) >= 3, files
    assert len(set(files)) == 1, files
    staged = files[0]
    assert staged.startswith("textures/"), staged
    assert (stage.parent / staged).is_file(), f"staged texture missing: {staged}"


def test_box_projection_file_carries_a_color_space(box_export):
    _payload, stage = box_export
    text = stage.read_text()
    file_input = re.search(
        r'asset inputs:file = @[^@]+@ \(\s*colorSpace = "([^"]+)"', text
    )
    assert file_input, "the image file has no colorSpace token"
    assert file_input.group(1) == "srgb_texture"


def test_box_projection_no_longer_warns_about_approximation(box_export):
    payload, _stage = box_export
    warnings = payload.get("warnings") or []
    assert not [w for w in warnings if "Box projection" in w], warnings


def test_sphere_projection_refuses_with_bake_advice(tmp_path):
    blend = _build_blend(tmp_path, "SPHERE")
    result = _export(blend, tmp_path / "out")
    assert result.returncode != 0, (
        "SPHERE projection must refuse, not export a silently flat sample\n"
        + result.stdout
    )
    combined = result.stdout + result.stderr
    assert "SPHERE" in combined
    assert "requires baking" in combined
