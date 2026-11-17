"""Integration - what a finish reads reaches its mesh, and only exported finishes ship.

A finish is bound inside the variant that offers it, after the passes that
write what a mesh's materials read. Those passes must count every material a
finish can bind, or a finish with a Bump or an Object Info Random that the
worn material lacks renders flat or zero once it is chosen - with nothing in
the export saying so. (A Color Attribute read in a finish nothing wears is
refused earlier, by the material rewrite; the unit tests cover the
displayColor pass on its own.)

The other half is scope: a selected-only export that leaves a finish set out
must not convert, validate or ship that set's materials and textures.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD_READS = r'''
import bpy, sys

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin
Plugin.register()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

plain = bpy.data.materials.new("Fabric_Plain")
plain.use_nodes = True

# Only this finish reads anything from the mesh: a Bump over a noise height,
# and Object Info Random.
rich = bpy.data.materials.new("Fabric_Rich")
rich.use_nodes = True
tree = rich.node_tree
bsdf = tree.nodes["Principled BSDF"]
noise = tree.nodes.new("ShaderNodeTexNoise")
bump = tree.nodes.new("ShaderNodeBump")
tree.links.new(noise.outputs["Fac"], bump.inputs["Height"])
tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
info = tree.nodes.new("ShaderNodeObjectInfo")
tree.links.new(info.outputs["Random"], bsdf.inputs["Roughness"])

def cube(name, location):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.data.materials.append(plain)
    return obj

seat = cube("Seat", (0, 0, 0))
# Wears the same plain material but no finish targets it: nothing to write.
bystander = cube("Bystander", (3, 0, 0))

seats = bpy.data.collections.new("Seats")
scene.collection.children.link(seats)
seats.objects.link(seat)

finish = scene.usd_stage_variant_sets.add()
finish.name = "Finish"
finish.kind = 'MATERIAL'
finish.target_collection = seats
for variant_name, mat in (("Plain", plain), ("Rich", rich)):
    variant = finish.variants.add()
    variant.name = variant_name
    variant.material = mat
finish.default_variant = "Plain"

bpy.ops.wm.save_as_mainfile(filepath=out)
print("SAVED")
'''

_BUILD_SCOPE = r'''
import bpy, os, sys

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin
Plugin.register()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
folder = os.path.dirname(out)

def textured(name, image_name, colour):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    image = bpy.data.images.new(image_name, 8, 8)
    image.pixels = list(colour) * 64
    image.filepath_raw = os.path.join(folder, image_name + ".png")
    image.file_format = 'PNG'
    image.save()
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    tree.links.new(node.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])
    return mat

plain = textured("Fabric_Plain", "plain_tex", (0.2, 0.6, 0.9, 1.0))
leather = textured("Fabric_Leather", "leather_tex", (0.9, 0.1, 0.1, 1.0))
# The export cannot translate a Wireframe node; a finish that is not exported
# must not be asked to.
wire = leather.node_tree.nodes.new("ShaderNodeWireframe")
leather.node_tree.links.new(
    wire.outputs["Fac"], leather.node_tree.nodes["Principled BSDF"].inputs["Roughness"])

seats = bpy.data.collections.new("Seats")
scene.collection.children.link(seats)
bpy.ops.mesh.primitive_cube_add(size=1.0)
seat = bpy.context.object
seat.name = "Seat"
for existing in seat.users_collection:
    existing.objects.unlink(seat)
seats.objects.link(seat)
seat.data.materials.append(plain)

finish = scene.usd_stage_variant_sets.add()
finish.name = "Finish"
finish.kind = 'MATERIAL'
finish.target_collection = seats
for variant_name, mat in (("Plain", plain), ("Leather", leather)):
    variant = finish.variants.add()
    variant.name = variant_name
    variant.material = mat
finish.default_variant = "Plain"

other_mat = bpy.data.materials.new("OtherMat")
other_mat.use_nodes = True
bpy.ops.mesh.primitive_cube_add(size=0.5, location=(3, 0, 0))
other = bpy.context.object
other.name = "Other"
other.data.materials.append(other_mat)

for obj in bpy.context.view_layer.objects:
    obj.select_set(obj == other)
bpy.context.view_layer.objects.active = other

bpy.ops.wm.save_as_mainfile(filepath=out)
print("SAVED")
'''


def _build(workdir: Path, script_body: str, name: str) -> Path:
    script = workdir / f"build_{name}.py"
    script.write_text(script_body)
    blend = workdir / f"{name}.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python", str(script), "--", str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _export(blend: Path, output: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "export", str(blend),
         "-o", str(output), "--format", "USDA", *extra],
        capture_output=True, text=True, timeout=900,
    )


@pytest.fixture(scope="module")
def finish_reads(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("finish_reads")
    blend = _build(workdir, _BUILD_READS, "reads")
    output = workdir / "out" / "seat.usda"
    output.parent.mkdir()
    result = _export(blend, output)
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def _mesh_under(stage, object_name: str):
    from pxr import UsdGeom

    root = stage.GetDefaultPrim()
    holder = next(c for c in root.GetChildren() if c.GetName() == object_name)
    meshes = [p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)
              and p.GetPath().HasPrefix(holder.GetPath())]
    assert len(meshes) == 1, [str(m.GetPath()) for m in meshes]
    return meshes[0]


def _geomprops_read(material_prim) -> set[str]:
    from pxr import Usd, UsdShade

    names = set()
    for prim in Usd.PrimRange(material_prim):
        shader = UsdShade.Shader(prim)
        if not shader:
            continue
        geomprop = shader.GetInput("geomprop")
        if geomprop and geomprop.Get():
            names.add(str(geomprop.Get()))
    return names


def test_the_finish_material_reads_what_the_worn_one_does_not(finish_reads):
    """The premise: Rich, and only Rich, reads the Bump's derivatives and Random."""
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(finish_reads))
    root = stage.GetDefaultPrim()
    seat = _mesh_under(stage, "Seat")
    reads = {}
    for selection in ("Plain", "Rich"):
        root.GetVariantSet("Finish").SetVariantSelection(selection)
        material = UsdShade.MaterialBindingAPI(seat).ComputeBoundMaterial()[0]
        reads[selection] = _geomprops_read(material.GetPrim())
    assert {"blenderDPdu", "blenderObjectRandom"} <= reads["Rich"], reads
    assert not reads["Plain"] & {"blenderDPdu", "blenderObjectRandom"}, reads


def test_a_finish_with_a_bump_gets_its_uv_derivatives(finish_reads):
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(finish_reads))
    seat = UsdGeom.PrimvarsAPI(_mesh_under(stage, "Seat"))
    for name in ("blenderDPdu", "blenderDPdv"):
        primvar = seat.GetPrimvar(name)
        assert primvar and primvar.HasAuthoredValue(), name
        assert any(any(component for component in value) for value in primvar.Get()), name


def test_a_finish_reading_object_random_gets_it(finish_reads):
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(finish_reads))
    primvar = UsdGeom.PrimvarsAPI(_mesh_under(stage, "Seat")).GetPrimvar("blenderObjectRandom")
    assert primvar and primvar.HasAuthoredValue()
    assert any(primvar.Get())


def test_a_mesh_no_finish_targets_gets_none_of_it(finish_reads):
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(str(finish_reads))
    bystander = UsdGeom.PrimvarsAPI(_mesh_under(stage, "Bystander"))
    for name in ("blenderDPdu", "blenderDPdv", "blenderObjectRandom"):
        assert not bystander.GetPrimvar(name), name


def test_a_selected_only_export_ships_none_of_an_unexported_finish(tmp_path):
    from pxr import Usd, UsdShade

    blend = _build(tmp_path, _BUILD_SCOPE, "scope")
    output = tmp_path / "out" / "other.usda"
    output.parent.mkdir()
    result = _export(blend, output, "--selected-only")
    # Leather carries a node the export cannot translate; were the set still
    # reserved, the material rewrite would refuse the export here.
    assert result.returncode == 0, result.stdout + result.stderr

    stage = Usd.Stage.Open(str(output))
    materials = {
        prim.GetName() for prim in stage.TraverseAll() if prim.IsA(UsdShade.Material)
    }
    assert "OtherMat" in materials, materials
    assert not {name for name in materials if name.startswith("Fabric")}, materials
    assert not stage.GetDefaultPrim().GetVariantSets().GetNames()

    shipped = [path.name for path in output.parent.rglob("*") if path.is_file()]
    assert not [name for name in shipped if "plain_tex" in name or "leather_tex" in name], shipped
