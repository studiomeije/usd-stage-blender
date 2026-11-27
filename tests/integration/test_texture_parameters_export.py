"""Integration test - texture file inputs exposed as material parameters.

RealityKit's ``ShaderGraphMaterial.setParameter`` rebinds inputs of the
material's interface. With the material custom property
``usd_stage_texture_parameters = True`` each texture file input becomes a
named material input the reader connects to. Without it the export is exactly
what it was, and with it the export differs only by those inputs and
connections.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable
from pxr import Sdf, Usd, UsdShade


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, os, sys
out, case = sys.argv[sys.argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)

def image(name, rgb, colorspace="Non-Color"):
    made = bpy.data.images.new(name, 4, 4)
    made.pixels = [*rgb, 1.0] * 16
    made.filepath_raw = os.path.join(os.path.dirname(out), name + ".png")
    made.file_format = "PNG"
    made.save()
    loaded = bpy.data.images.load(made.filepath_raw, check_existing=False)
    loaded.colorspace_settings.name = colorspace
    return loaded

bpy.ops.mesh.primitive_uv_sphere_add()
material = bpy.data.materials.new("Scan")
bpy.context.active_object.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
albedo = tree.nodes.new("ShaderNodeTexImage")
albedo.image = image("albedo", (0.6, 0.4, 0.2), "sRGB")
tree.links.new(albedo.outputs["Color"], bsdf.inputs["Base Color"])
orm = tree.nodes.new("ShaderNodeTexImage")
orm.image = image("orm", (1.0, 0.5, 0.25))
split = tree.nodes.new("ShaderNodeSeparateColor")
tree.links.new(orm.outputs["Color"], split.inputs[0])
tree.links.new(split.outputs["Green"], bsdf.inputs["Roughness"])
tree.links.new(split.outputs["Blue"], bsdf.inputs["Metallic"])
normal = tree.nodes.new("ShaderNodeTexImage")
normal.image = image("normal", (0.5, 0.5, 1.0))
normal_map = tree.nodes.new("ShaderNodeNormalMap")
tree.links.new(normal.outputs["Color"], normal_map.inputs["Color"])
tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
if case == "on":
    material["usd_stage_texture_parameters"] = True
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _export(tmp_path, case):
    folder = tmp_path / case
    folder.mkdir()
    script = folder / "build.py"
    script.write_text(_BUILD)
    blend = folder / "scene.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), case],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    output = folder / "scene.usda"
    exported = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "export", str(blend),
         "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    return output


def _without_parameters(path: Path) -> str:
    """The material network with the interface inputs and connections removed."""
    stage = Usd.Stage.Open(str(path))
    material = next(UsdShade.Material(p) for p in stage.Traverse() if UsdShade.Material(p))
    prim = material.GetPrim()
    # Blender exports the opt-in custom property itself as a user property.
    prim.RemoveProperty("userProperties:usd_stage_texture_parameters")
    for parameter in material.GetInputs():
        if parameter.GetTypeName() == Sdf.ValueTypeNames.Asset:
            prim.RemoveProperty(parameter.GetAttr().GetName())
    for shader_prim in Usd.PrimRange(prim):
        shader = UsdShade.Shader(shader_prim)
        for shader_input in shader.GetInputs() if shader else ():
            if shader_input.GetTypeName() == Sdf.ValueTypeNames.Asset:
                shader_input.ClearSources()
    flat = Sdf.Layer.CreateAnonymous()
    Sdf.CopySpec(stage.GetRootLayer(), prim.GetPath(), flat, Sdf.Path("/Material"))
    text = flat.ExportToString()
    # The staged texture names carry the export's own file name and a digest.
    return re.sub(r"@[^@]*textures/[^@]*@", "@texture@", text)


def test_textures_become_named_material_parameters(tmp_path):
    output = _export(tmp_path, "on")
    stage = Usd.Stage.Open(str(output))
    material = next(UsdShade.Material(p) for p in stage.Traverse() if UsdShade.Material(p))

    parameters = {i.GetBaseName(): i for i in material.GetInputs()}
    assert set(parameters) == {"baseColorTexture", "metallicRoughnessTexture", "normalTexture"}
    assert parameters["baseColorTexture"].GetAttr().GetColorSpace() == "srgb_texture"
    for name, parameter in parameters.items():
        path = parameter.Get().path
        assert path.startswith("./") or not Path(path).is_absolute(), (name, path)
        readers = [
            UsdShade.Shader(p) for p in Usd.PrimRange(material.GetPrim())
            if UsdShade.Shader(p) and UsdShade.Shader(p).GetInput("file")
            and UsdShade.Shader(p).GetInput("file").HasConnectedSource()
            and UsdShade.Shader(p).GetInput("file").GetConnectedSources()[0][0].sourceName == name
        ]
        assert len(readers) == 1, name
        assert readers[0].GetInput("file").Get().path == path


def test_off_by_default_and_otherwise_identical(tmp_path):
    off = _export(tmp_path, "off")
    on = _export(tmp_path, "on")
    stage = Usd.Stage.Open(str(off))
    material = next(UsdShade.Material(p) for p in stage.Traverse() if UsdShade.Material(p))
    assert not [i for i in material.GetInputs() if i.GetTypeName() == Sdf.ValueTypeNames.Asset]

    assert _without_parameters(on) == _without_parameters(off)
