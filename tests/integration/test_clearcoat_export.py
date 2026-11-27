"""Integration test - textured clearcoat, coat normal and coat IOR.

Coat Weight and Coat Roughness export constant or textured, through the linear
data reader, to clearcoat and clearcoatRoughness; Coat Normal to
clearcoatNormal. Cycles' coat follows the base Normal when Coat Normal is
unlinked and RealityKit's does not, so the base normal is authored on the coat
too. RealityKit ignores clearcoatIOR (its coat reflects like IOR 1.5 whatever
the value, measured on macOS 27.2.1), so it is never authored and a Coat IOR
other than 1.5 stops the export.
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

bpy.ops.mesh.primitive_uv_sphere_add()
material = bpy.data.materials.new("Lacquer")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
coat = tree.nodes.new("ShaderNodeTexImage")
coat.image = image("coat", (0.8, 0.2, 0.0))
split = tree.nodes.new("ShaderNodeSeparateColor")
tree.links.new(coat.outputs["Color"], split.inputs[0])
tree.links.new(split.outputs["Red"], bsdf.inputs["Coat Weight"])
tree.links.new(split.outputs["Green"], bsdf.inputs["Coat Roughness"])
normal_image = tree.nodes.new("ShaderNodeTexImage")
normal_image.image = image("normal", (0.5, 0.6, 1.0))
normal_map = tree.nodes.new("ShaderNodeNormalMap")
tree.links.new(normal_image.outputs["Color"], normal_map.inputs["Color"])
target = "Coat Normal" if case == "coat_normal" else "Normal"
tree.links.new(normal_map.outputs["Normal"], bsdf.inputs[target])
if case == "coat_ior":
    bsdf.inputs["Coat IOR"].default_value = 1.7
bpy.ops.wm.save_as_mainfile(filepath=out)
'''

_STAGES = []


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


def _surface(output):
    stage = Usd.Stage.Open(str(output))
    _STAGES.append(stage)
    return next(
        UsdShade.Shader(p) for p in stage.Traverse()
        if UsdShade.Shader(p).GetIdAttr().Get() == "ND_realitykit_pbr_surfaceshader_2_0"
    )


def _source(shader, name):
    (source,) = shader.GetInput(name).GetConnectedSources()[0]
    return UsdShade.Shader(source.source.GetPrim())


def _reader(shader):
    if str(shader.GetIdAttr().Get()).startswith("ND_image"):
        return shader
    for shader_input in shader.GetInputs():
        for source in shader_input.GetConnectedSources()[0]:
            found = _reader(UsdShade.Shader(source.source.GetPrim()))
            if found:
                return found
    return None


@pytest.mark.parametrize("case", ["base_normal", "coat_normal"])
def test_textured_coat_exports_through_linear_reads(tmp_path, case):
    exported, output = _export(tmp_path, case)
    assert exported.returncode == 0, exported.stdout + exported.stderr
    surface = _surface(output)

    for name, mask in (("clearcoat", (1.0, 0.0, 0.0)), ("clearcoatRoughness", (0.0, 1.0, 0.0))):
        dot = _source(surface, name)
        assert dot.GetIdAttr().Get() == "ND_dotproduct_vector3", name
        assert tuple(dot.GetInput("in2").Get()) == mask
        reader = _reader(dot)
        assert reader.GetIdAttr().Get() == "ND_image_vector3"
        assert reader.GetInput("file").GetAttr().GetColorSpace() == ""
    assert surface.GetInput("clearcoatNormal").HasConnectedSource()
    assert "normal" in _reader(_source(surface, "clearcoatNormal")).GetInput("file").Get().path
    if case == "base_normal":
        assert _reader(_source(surface, "normal")).GetPath() == _reader(
            _source(surface, "clearcoatNormal")
        ).GetPath()
    else:
        assert not surface.GetInput("normal")
    assert not surface.GetInput("clearcoatIOR")


def test_a_coat_ior_realitykit_ignores_stops_the_export(tmp_path):
    exported, _output = _export(tmp_path, "coat_ior")
    assert exported.returncode != 0
    assert "set Coat IOR to 1.5" in exported.stdout + exported.stderr
