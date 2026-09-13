"""Integration test — Blender image sampling modes reach the MaterialX network.

The shipped RCP 3 (80.0.1.500.1) ``ND_image_*`` nodedefs declare
``uaddressmode``/``vaddressmode`` (constant, clamp, periodic, mirror; default
periodic) and ``filtertype`` (closest, linear, cubic; default linear), and the
ShaderGraph runtime wires them into its Metal samplers. Before this existed
the exporter never authored them, so a texture set to Clip/Extend/Mirror or
Closest in Blender silently rendered as repeat + linear in RealityKit.

Contract: non-default modes are authored on the ND_image shader; default
modes (Repeat + Linear) author nothing and inherit the nodedef defaults.
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

bpy.ops.wm.read_factory_settings(use_empty=True)

image = bpy.data.images.new("grid", 16, 16)
image.generated_type = 'COLOR_GRID'
image.filepath_raw = texture
image.file_format = 'PNG'
image.save()
bpy.data.images.remove(image)

def plane_with_texture(name, x, extension, interpolation):
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
    node.extension = extension
    node.interpolation = interpolation
    tree.links.new(
        node.outputs["Color"],
        tree.nodes["Principled BSDF"].inputs["Base Color"],
    )

plane_with_texture("ClipClosest", 0.0, 'CLIP', 'Closest')
plane_with_texture("ExtendCubic", 3.0, 'EXTEND', 'Cubic')
plane_with_texture("MirrorLinear", 6.0, 'MIRROR', 'Linear')
plane_with_texture("Defaults", 9.0, 'REPEAT', 'Linear')

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _materialx_scope(text: str, material_name: str) -> str:
    """The material's MaterialX shader blocks (excludes the preview scope)."""
    match = re.search(
        rf'def Material "{material_name}".*?(?=\n        def Material |\Z)',
        text,
        re.S,
    )
    assert match, f"material {material_name} not found"
    return match.group(0)


@pytest.fixture(scope="module")
def sampling_export(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("sampling")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "sampling.blend"
    texture = workdir / "grid.png"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(texture)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    stage = workdir / "out" / "sampling.usda"
    stage.parent.mkdir()
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"),
         "export", str(blend), "-o", str(stage), "--format", "USDA"],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return stage.read_text()


@pytest.mark.parametrize(
    ("material", "addressmode", "filtertype"),
    [
        ("ClipClosest", "constant", "closest"),
        ("ExtendCubic", "clamp", "cubic"),
        ("MirrorLinear", "mirror", None),
    ],
)
def test_non_default_modes_are_authored(
    sampling_export, material, addressmode, filtertype
):
    scope = _materialx_scope(sampling_export, material)

    assert f'string inputs:uaddressmode = "{addressmode}"' in scope
    assert f'string inputs:vaddressmode = "{addressmode}"' in scope
    if filtertype is None:
        assert "inputs:filtertype" not in scope
    else:
        assert f'string inputs:filtertype = "{filtertype}"' in scope


def test_default_modes_author_nothing(sampling_export):
    """Repeat + Linear are the nodedef defaults; authoring them would be
    noise and would defeat texture-node sharing across materials."""
    scope = _materialx_scope(sampling_export, "Defaults")

    assert "inputs:uaddressmode" not in scope
    assert "inputs:vaddressmode" not in scope
    assert "inputs:filtertype" not in scope
