"""Integration test - ambient occlusion comes from the declared node only.

PBR Surface 2's ``ambientOcclusion`` darkens image-based diffuse light
(measured on macOS 27.2.1). Blender's Principled has no such input, so the
material declares the source: the material property
``usd_stage_ambient_occlusion`` ("Node" or "Node:Output"), or a node labelled
``Ambient Occlusion``; the property wins. A node name is never a declaration:
an image called "Chaos AO" on Base Color is just a base colour.
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
out, case = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)

def image(name, rgb):
    made = bpy.data.images.new(name, 4, 4, is_data=True)
    made.pixels = [*rgb, 1.0] * 16
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

albedo = tree.nodes.new("ShaderNodeTexImage")
albedo.name = "Chaos AO"
albedo.image = image("albedo", (0.5, 0.4, 0.3))
albedo.image.colorspace_settings.name = "sRGB"
tree.links.new(albedo.outputs["Color"], bsdf.inputs["Base Color"])

orm = tree.nodes.new("ShaderNodeTexImage")
orm.name = "ORM"
orm.image = image("orm", (0.25, 0.5, 0.75))
split = tree.nodes.new("ShaderNodeSeparateColor")
split.name = "ORM Split"
tree.links.new(orm.outputs["Color"], split.inputs[0])
tree.links.new(split.outputs["Green"], bsdf.inputs["Roughness"])

if case == "property":
    material["usd_stage_ambient_occlusion"] = "ORM Split:Red"
elif case == "label":
    occlusion = tree.nodes.new("ShaderNodeTexImage")
    occlusion.label = "Ambient Occlusion"
    occlusion.image = image("occlusion", (0.6, 0.6, 0.6))
elif case == "property_over_label":
    occlusion = tree.nodes.new("ShaderNodeTexImage")
    occlusion.label = "Ambient Occlusion"
    occlusion.image = image("occlusion", (0.6, 0.6, 0.6))
    material["usd_stage_ambient_occlusion"] = "ORM Split:Red"
elif case == "two_labels":
    split.label = "Ambient Occlusion"
    orm.label = "Ambient Occlusion"
elif case == "missing":
    material["usd_stage_ambient_occlusion"] = "No Such Node"
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _export(tmp_path, case):
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), case],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    output = tmp_path / "scene.usda"
    exported = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend),
         "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    return exported, output


_STAGES = []


def _occlusion_source(output):
    stage = Usd.Stage.Open(str(output))
    _STAGES.append(stage)  # the returned shader is valid only while its stage lives
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if shader and shader.GetIdAttr().Get() == "ND_realitykit_pbr_surfaceshader_2_0":
            occlusion = shader.GetInput("ambientOcclusion")
            if not occlusion or not occlusion.HasConnectedSource():
                return None
            (source,) = occlusion.GetConnectedSources()[0]
            return UsdShade.Shader(source.source.GetPrim())
    raise AssertionError("no PBR surface")


def _reader_file(shader):
    for shader_input in shader.GetInputs():
        value = shader_input.Get()
        if hasattr(value, "path") and value.path:
            return Path(value.path).name
        for source in shader_input.GetConnectedSources()[0]:
            found = _reader_file(UsdShade.Shader(source.source.GetPrim()))
            if found:
                return found
    return None


def test_a_node_name_is_not_an_occlusion_declaration(tmp_path):
    exported, output = _export(tmp_path, "none")
    assert exported.returncode == 0, exported.stdout + exported.stderr
    assert _occlusion_source(output) is None


@pytest.mark.parametrize("case", ["property", "property_over_label"])
def test_the_property_names_a_node_output(tmp_path, case):
    exported, output = _export(tmp_path, case)
    assert exported.returncode == 0, exported.stdout + exported.stderr

    dot = _occlusion_source(output)
    assert dot.GetIdAttr().Get() == "ND_dotproduct_vector3"
    assert tuple(dot.GetInput("in2").Get()) == (1.0, 0.0, 0.0)
    assert "orm" in _reader_file(dot)


def test_a_labelled_node_is_the_source(tmp_path):
    exported, output = _export(tmp_path, "label")
    assert exported.returncode == 0, exported.stdout + exported.stderr

    gray = _occlusion_source(output)
    assert gray.GetIdAttr().Get() == "ND_dotproduct_vector3"
    assert "occlusion" in _reader_file(gray)


@pytest.mark.parametrize(
    "case,message",
    [("two_labels", "are all labelled 'Ambient Occlusion'"), ("missing", "names 'No Such Node'")],
)
def test_an_ambiguous_or_broken_declaration_stops_the_export(tmp_path, case, message):
    exported, _output = _export(tmp_path, case)
    assert exported.returncode != 0
    assert message in exported.stdout + exported.stderr
