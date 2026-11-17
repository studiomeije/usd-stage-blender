"""Exercise payload packaging and baking with hidden shape alternatives."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
sys.path.insert(0, sys.argv[-2])
import Plugin
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.cycles.samples = 1
world = bpy.data.worlds.new('World')
world.node_tree.nodes['Background'].inputs['Color'].default_value = (1, 1, 1, 1)
scene.world = world
material = bpy.data.materials.new('Pattern')
tree = material.node_tree
noise = tree.nodes.new('ShaderNodeTexNoise')
tree.links.new(noise.outputs['Color'], tree.nodes['Principled BSDF'].inputs['Base Color'])
shape = scene.usd_stage_variant_sets.add()
shape.name = 'Shape'
for name in ('Visible', 'Hidden'):
    collection = bpy.data.collections.new(name)
    scene.collection.children.link(collection)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = name
    for owner in list(obj.users_collection):
        owner.objects.unlink(obj)
    collection.objects.link(obj)
    obj.data.materials.append(material)
    variant = shape.variants.add()
    variant.name = name
    variant.collection = collection
shape.default_variant = 'Visible'
bpy.context.view_layer.layer_collection.children['Hidden'].exclude = True
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[-1])
'''


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    work = tmp_path_factory.mktemp("variant_delivery")
    script = work / "build.py"
    script.write_text(_BUILD)
    blend = work / "scene.blend"
    result = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python-exit-code", "1", "--python", str(script), "--",
         str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return blend


def _cli(*args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), *map(str, args), "--json"],
        capture_output=True, text=True, timeout=120,
    )


@pytest.mark.parametrize(("mode", "format"), [
    ("LIT_ALBEDO", "USDA"), ("LIT_IBL", "USDA"), ("LIT_ALBEDO", "USDZ"),
])
def test_hidden_shape_is_baked_and_its_texture_survives_payload_move(scene, tmp_path, mode, format):
    from pxr import Usd, UsdGeom, UsdShade

    output = tmp_path / "delivery" / ("asset.usdz" if format == "USDZ" else "asset.usda")
    result = _cli(
        "bake-export", scene, "-o", output, "--resolution", "32",
        f"bake_mode={mode}", "bake_isolate_meshes_lit=true", "payload_geometry=true",
        "--format", format,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["bake_stats"]["objects_baked"] == 2
    # Test portability independently of the staging directory and source .blend.
    relocated = tmp_path / "relocated"
    shutil.copytree(output.parent, relocated)
    shutil.rmtree(output.parent)
    stage = Usd.Stage.Open(str(relocated / output.name))
    root = stage.GetDefaultPrim()
    for selection in ("Visible", "Hidden"):
        root.GetVariantSet("Shape").SetVariantSelection(selection)
        mesh = next(p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh))
        material = UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial()[0]
        assert "Baked" in material.GetPrim().GetName()
        textures = [
            attr.Get() for prim in Usd.PrimRange(material.GetPrim())
            for attr in prim.GetAttributes() if str(attr.GetTypeName()) == "asset"
        ]
        assert textures and all(asset.resolvedPath for asset in textures)
        assert all(asset.resolvedPath.startswith(str(relocated)) for asset in textures)


def test_lighting_bake_refuses_mutually_occluding_alternatives(scene, tmp_path):
    output = tmp_path / "asset.usda"
    result = _cli("bake-export", scene, "-o", output, "--resolution", "32")
    assert result.returncode != 0
    assert "BAKE_VARIANTS_REQUIRE_ISOLATION" in result.stdout + result.stderr
    assert not output.exists()


def test_bake_restores_the_original_hidden_collection(scene, tmp_path):
    script = tmp_path / "restore.py"
    script.write_text(r'''
import bpy, sys
sys.path.insert(0, sys.argv[-2])
import Plugin
Plugin.register()
from Plugin.api.commands.bake_export import run_bake_export
from Plugin.export.diagnostics import ExportDiagnostics
layer = bpy.context.view_layer.layer_collection.children['Hidden']
assert layer.exclude
settings = bpy.context.scene.usd_stage_export_settings
settings.bake_mode = 'LIT_ALBEDO'
settings.bake_resolution = 'CUSTOM'
settings.bake_resolution_custom = 32
settings.export_format = 'USDA'
settings.filepath = sys.argv[-1]
run_bake_export(bpy.context, settings, settings.filepath, 'USDA',
                ExportDiagnostics(), diagnostics_path=None)
assert layer.exclude, 'Hidden collection was left visible after baking'
''')
    result = subprocess.run(
        [blender_executable(), "--background", str(scene), "--python-exit-code", "1",
         "--python", str(script), "--", str(REPO_ROOT), str(tmp_path / "asset.usda")],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_finish_identity_without_custom_metadata(tmp_path):
    from pxr import Usd, UsdGeom, UsdShade

    script = tmp_path / "finishes.py"
    output = tmp_path / "finishes.usda"
    script.write_text(r'''
import bpy, sys
sys.path.insert(0, sys.argv[-2])
import Plugin
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
target = bpy.data.collections.new('Target')
bpy.context.scene.collection.children.link(target)
target.objects.link(obj)
finish = bpy.context.scene.usd_stage_variant_sets.add()
finish.name, finish.kind, finish.target_collection = 'Finish', 'MATERIAL', target
for name, variant_name, color in [('Oak Wood', 'Red', (1,0,0,1)),
                                  ('Oak_Wood', 'Blue', (0,0,1,1))]:
    material = bpy.data.materials.new(name)
    material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = color
    if variant_name == 'Red': obj.data.materials.append(material)
    variant = finish.variants.add()
    variant.name, variant.material = variant_name, material
finish.default_variant = 'Red'
settings = bpy.context.scene.usd_stage_export_settings
settings.export_format = 'USDA'
settings.export_custom_properties = False
settings.author_blender_name = False
assert bpy.ops.usdstage.export(filepath=sys.argv[-1]) == {'FINISHED'}
''')
    result = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python-exit-code", "1", "--python", str(script), "--",
         str(REPO_ROOT), str(output)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stage = Usd.Stage.Open(str(output))
    paths = []
    for selection in ("Red", "Blue"):
        stage.GetDefaultPrim().GetVariantSet("Finish").SetVariantSelection(selection)
        mesh = next(p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh))
        paths.append(UsdShade.MaterialBindingAPI(mesh).ComputeBoundMaterial()[0].GetPath())
    assert paths[0] != paths[1]
