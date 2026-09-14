"""Integration test — a UV Map node reads the UV map it names.

Blender's USD exporter writes the render UV map as ``primvars:st`` and keeps
every other UV map under its own name. A UV Map node naming ``UVMap`` on a
mesh whose render UV map is ``Detail`` used to be read through ``texcoord``,
which is Detail. The export must read ``primvars:UVMap`` instead, and the
render UV map through ``texcoord``.
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

bpy.ops.mesh.primitive_plane_add()
mesh = bpy.context.active_object.data
detail = mesh.uv_layers.new(name="Detail")
for loop in detail.data:
    loop.uv = (loop.uv[0] * 0.5 + 0.25, loop.uv[1] * 0.25)
detail.active_render = True

material = bpy.data.materials.new("TwoSets")
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
for uv_name, target in (("UVMap", "Base Color"), ("Detail", "Roughness")):
    uv = tree.nodes.new("ShaderNodeUVMap")
    uv.uv_map = uv_name
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = bpy.data.images.load(texture, check_existing=False)
    if target == "Roughness":
        node.image.colorspace_settings.name = 'Non-Color'
    tree.links.new(uv.outputs["UV"], node.inputs["Vector"])
    tree.links.new(node.outputs["Color"], bsdf.inputs[target])
mesh.materials.append(material)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def test_uv_map_nodes_read_the_sets_they_name(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "uv.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), str(tmp_path / "grid.png")],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    stage = tmp_path / "uv.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend),
         "-o", str(stage), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = stage.read_text()

    # What the exporter measures against: st holds the render map (Detail,
    # whose u stays within [0.25, 0.75]); UVMap keeps the default unwrap.
    st = re.search(r"texCoord2f\[\] primvars:st = \[([^\]]*)\]", text)
    uvmap = re.search(r"texCoord2f\[\] primvars:UVMap = \[([^\]]*)\]", text)
    assert st and uvmap, text[:2000]
    st_u = [float(u) for u in re.findall(r"\(([-\d.e]+),", st.group(1))]
    uvmap_u = [float(u) for u in re.findall(r"\(([-\d.e]+),", uvmap.group(1))]
    assert min(st_u) >= 0.25 - 1e-6 and max(st_u) <= 0.75 + 1e-6
    assert min(uvmap_u) == pytest.approx(0.0) and max(uvmap_u) == pytest.approx(1.0)

    # Base Color names UVMap: a geompropvalue of that primvar. Roughness names
    # the render map: the texcoord reader, never a primvars:Detail read.
    assert re.search(r'string inputs:geomprop = "UVMap"', text)
    assert 'inputs:geomprop = "Detail"' not in text
    assert 'info:id = "ND_texcoord_vector2"' in text
