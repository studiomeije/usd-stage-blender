"""Integration test — a finish set over objects a shape set hides.

The ordinary layout of a configurable seat: a Seat shape set offering Fabric or
Leather, a Colour finish set over every upholstered object, and a frame both
shapes share. Leather is hidden by default, so its mesh does not compose when
the finish is authored - and still has to be recoloured when it is chosen.

It also carries the names artists actually give things: a finish material
nobody wears yet, called ``Bordeaux.001`` the way Blender names a duplicate, and
a collision proxy parented under the frame and called ``Collider``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin

bpy.ops.wm.read_factory_settings(use_empty=True)
Plugin.register()
scene = bpy.context.scene


def material(name, colour):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = colour
    return mat


navy = material("Navy Blue", (0.05, 0.08, 0.25, 1.0))
# Worn by nothing, so Blender writes no prim for it, and named the way
# Blender names a duplicate, which is not a USD identifier.
bordeaux = material("Bordeaux.001", (0.35, 0.03, 0.06, 1.0))


def place(name, collection, primitive, parent=None):
    primitive()
    obj = bpy.context.object
    obj.name = name
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    collection.objects.link(obj)
    obj.data.materials.append(navy)
    if parent is not None:
        obj.parent = parent
    return obj


seat = bpy.data.collections.new("Seat")
scene.collection.children.link(seat)
fabric_c = bpy.data.collections.new("Fabric")
leather_c = bpy.data.collections.new("Leather")
seat.children.link(fabric_c)
seat.children.link(leather_c)
frames = bpy.data.collections.new("Frames")
scene.collection.children.link(frames)

fabric = place("SeatFabric", fabric_c, lambda: bpy.ops.mesh.primitive_cube_add(size=0.9, location=(0, 0, 0.6)))
leather = place("SeatLeather", leather_c, lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 0.6)))
frame = place("Frame", frames, lambda: bpy.ops.mesh.primitive_cube_add(size=1.2, location=(0, 0, 0.1)))
frame.scale = (1.0, 1.0, 0.1)
place("Collider", frames, lambda: bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.1)), parent=frame)

bpy.context.view_layer.objects.active = frame
frame.select_set(True)
bpy.ops.rigidbody.object_add()
frame.rigid_body.type = 'PASSIVE'

upholstery = bpy.data.collections.new("Upholstery")
scene.collection.children.link(upholstery)
for obj in (fabric, leather, frame):
    upholstery.objects.link(obj)

sets = scene.usd_stage_variant_sets
shape = sets.add()
shape.name = "Seat"
for name, collection in (("Fabric", fabric_c), ("Leather", leather_c)):
    variant = shape.variants.add()
    variant.name = name
    variant.collection = collection
shape.default_variant = "Fabric"

colour = sets.add()
colour.name = "Colour"
colour.kind = 'MATERIAL'
colour.target_collection = upholstery
for name, mat in (("Navy", navy), ("Bordeaux", bordeaux)):
    variant = colour.variants.add()
    variant.name = name
    variant.material = mat
colour.default_variant = "Navy"

bpy.context.view_layer.layer_collection.children["Seat"].children["Leather"].exclude = True
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> dict:
    work = tmp_path_factory.mktemp("finish_over_hidden_shapes")
    script = work / "build.py"
    script.write_text(_BUILD)
    blend = work / "seat.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python", str(script), "--", str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    output = work / "seat.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "export", str(blend),
         "diagnostics_enabled=true", "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {"stage": output, "diagnostics": output.with_suffix(".diagnostics.json")}


def _mesh_material(stage, object_name: str):
    """The material bound directly on the object's mesh, by Blender name."""
    from pxr import Usd, UsdGeom, UsdShade

    obj = next(p for p in stage.GetDefaultPrim().GetChildren() if p.GetName() == object_name)
    mesh = next(
        p for p in Usd.PrimRange(obj)
        if p.IsA(UsdGeom.Mesh) and p.GetParent() == obj
    )
    material = UsdShade.MaterialBindingAPI(mesh).GetDirectBinding().GetMaterial()
    assert material, f"{object_name}'s mesh binds nothing directly"
    return material.GetPrim()


def _select(stage, seat: str, colour: str) -> None:
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Seat").SetVariantSelection(seat)
    root.GetVariantSet("Colour").SetVariantSelection(colour)


def test_the_hidden_shape_takes_every_finish(exported):
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(exported["stage"]))
    _select(stage, "Leather", "Navy")
    navy = _mesh_material(stage, "SeatLeather")
    _select(stage, "Leather", "Bordeaux")
    bordeaux = _mesh_material(stage, "SeatLeather")

    assert navy.GetPath() != bordeaux.GetPath()
    assert _mesh_material(stage, "Frame").GetPath() == bordeaux.GetPath()


def test_an_unworn_finish_named_like_a_duplicate_is_converted(exported):
    """``Bordeaux.001`` has no prim of Blender's; it must still be a real material."""
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(exported["stage"]))
    _select(stage, "Fabric", "Bordeaux")
    bordeaux = _mesh_material(stage, "SeatFabric")
    _select(stage, "Fabric", "Navy")
    navy = _mesh_material(stage, "SeatFabric")

    def shader_kinds(prim):
        return sorted(child.GetName().rsplit("_", 1)[0] for child in prim.GetChildren())

    assert shader_kinds(bordeaux) == shader_kinds(navy) == ["pbr_surfaceshader"]


def test_a_child_object_called_collider_keeps_its_geometry(exported):
    pytest.importorskip("pxr")
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(exported["stage"]))
    frame = next(p for p in stage.GetDefaultPrim().GetChildren() if p.GetName() == "Frame")
    proxy = frame.GetChild("Collider")

    assert proxy.GetTypeName() == "Xform"
    assert any(p.IsA(UsdGeom.Mesh) for p in Usd.PrimRange(proxy))
    assert frame.GetChild("Collider_1").GetTypeName() == "RealityKitComponent"


def test_the_export_is_clean(exported):
    payload = json.loads(exported["diagnostics"].read_text())

    assert payload["errors"] == []
    assert [path.split("/")[-2:] for path in payload["colliders"]] == [["Frame", "Collider_1"]]
