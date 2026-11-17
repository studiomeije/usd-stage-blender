"""Integration - what a finish reads reaches its mesh, and only exported finishes ship.

A finish is bound inside the variant that offers it, after the passes that
write what a mesh's materials read. Those passes must count every material a
finish can bind, or a finish with a Bump or an Object Info Random that the
worn material lacks renders flat or zero once it is chosen - with nothing in
the export saying so. The material rewrite has the same blind spot: a UV map
or colour attribute a finish nothing wears reads must be looked up on the
meshes the finish targets, not guessed or refused.

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


_BUILD_MESH_DATA = r"""
import bpy, os, sys

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin
Plugin.register()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
folder = os.path.dirname(out)

plain = bpy.data.materials.new("Fabric_Plain")
plain.use_nodes = True

# Nothing wears this finish. It reads the mesh's only UV map, renamed so the
# name alone says nothing about it being the one Blender writes as st, and a
# colour attribute.
painted = bpy.data.materials.new("Fabric_Painted")
painted.use_nodes = True
tree = painted.node_tree
bsdf = tree.nodes["Principled BSDF"]
image = bpy.data.images.new("paint_tex", 8, 8)
image.pixels = [0.8, 0.4, 0.1, 1.0] * 64
image.filepath_raw = os.path.join(folder, "paint_tex.png")
image.file_format = "PNG"
image.save()
texture = tree.nodes.new("ShaderNodeTexImage")
texture.image = image
uv = tree.nodes.new("ShaderNodeUVMap")
uv.uv_map = "Detail"
tree.links.new(uv.outputs["UV"], texture.inputs["Vector"])
tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
paint = tree.nodes.new("ShaderNodeVertexColor")
paint.layer_name = "Paint"
tree.links.new(paint.outputs["Color"], bsdf.inputs["Emission Color"])

bpy.ops.mesh.primitive_cube_add(size=1.0)
seat = bpy.context.object
seat.name = "Seat"
seat.data.uv_layers[0].name = "Detail"
seat.data.color_attributes.new("Paint", "FLOAT_COLOR", "POINT")
seat.data.materials.append(plain)
seats = bpy.data.collections.new("Seats")
scene.collection.children.link(seats)
for existing in seat.users_collection:
    existing.objects.unlink(seat)
seats.objects.link(seat)

finish = scene.usd_stage_variant_sets.add()
finish.name = "Finish"
finish.kind = "MATERIAL"
finish.target_collection = seats
for variant_name, mat in (("Plain", plain), ("Painted", painted)):
    variant = finish.variants.add()
    variant.name = variant_name
    variant.material = mat
finish.default_variant = "Plain"

bpy.ops.wm.save_as_mainfile(filepath=out)
print("SAVED")
"""

#: The same scene without the colour attribute, so the UV lookup is tested on
#: its own rather than behind the attribute refusal.
_BUILD_UV_ONLY = _BUILD_MESH_DATA.replace(
    """paint = tree.nodes.new("ShaderNodeVertexColor")
paint.layer_name = "Paint"
tree.links.new(paint.outputs["Color"], bsdf.inputs["Emission Color"])
""", "").replace(
    """seat.data.color_attributes.new("Paint", "FLOAT_COLOR", "POINT")
""", "")

#: A finish nested in a shape set, with Walnut offered on Square only. Walnut
#: reads a UV map only the square top has, so it must not be checked against
#: the round top it is never bound on.
_BUILD_NESTED = r"""
import bpy, os, sys
repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
folder = os.path.dirname(out)

oak = bpy.data.materials.new("OakMat"); oak.use_nodes = True
walnut = bpy.data.materials.new("WalnutMat"); walnut.use_nodes = True
tree = walnut.node_tree
image = bpy.data.images.new("walnut_tex", 8, 8)
image.pixels = [0.8, 0.4, 0.1, 1.0] * 64
image.filepath_raw = os.path.join(folder, "walnut_tex.png")
image.file_format = "PNG"; image.save()
tex = tree.nodes.new("ShaderNodeTexImage"); tex.image = image
uv = tree.nodes.new("ShaderNodeUVMap"); uv.uv_map = "Detail"
tree.links.new(uv.outputs["UV"], tex.inputs["Vector"])
tree.links.new(tex.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])

def top(name, collection, primitive, mat):
    primitive()
    obj = bpy.context.object
    obj.name = name
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    collection.objects.link(obj)
    obj.data.materials.append(mat)
    return obj

shape = bpy.data.collections.new("Shape"); scene.collection.children.link(shape)
round_c = bpy.data.collections.new("Round"); square_c = bpy.data.collections.new("Square")
shape.children.link(round_c); shape.children.link(square_c)
round_top = top("TopRound", round_c, lambda: bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=0.04), oak)
square_top = top("TopSquare", square_c, lambda: bpy.ops.mesh.primitive_cube_add(size=0.9), walnut)
# Only the square top carries the second UV map Walnut reads.
square_top.data.uv_layers.new(name="Detail")
assert [l.name for l in square_top.data.uv_layers] == ["UVMap", "Detail"], [l.name for l in square_top.data.uv_layers]
assert square_top.data.uv_layers["UVMap"].active_render

tops = bpy.data.collections.new("Tops"); scene.collection.children.link(tops)
tops.objects.link(round_top); tops.objects.link(square_top)
sets = scene.usd_stage_variant_sets
shape_set = sets.add(); shape_set.name = "Shape"; shape_set.kind = 'GEOMETRY'
for n, c in (("Round", round_c), ("Square", square_c)):
    v = shape_set.variants.add(); v.name = n; v.collection = c
shape_set.default_variant = "Square"
finish_set = sets.add(); finish_set.name = "Finish"; finish_set.kind = 'MATERIAL'
finish_set.target_collection = tops
for n, m in (("Oak", oak), ("Walnut", walnut)):
    v = finish_set.variants.add(); v.name = n; v.material = m
finish_set.default_variant = "Oak"
finish_set.parent_set = "Shape"
finish_set.variants[1].applies_to = "Square"
bpy.ops.wm.save_as_mainfile(filepath=out)
print("SAVED")
"""

#: A selected-only export of ``Other``. The finish set it does not touch
#: offers DetailMat to ``Seat``, which lacks the ``Detail`` UV map; that set
#: is out of the export and must not refuse it.
_BUILD_OUT_OF_SCOPE = r"""
import bpy, os, sys
repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
folder = os.path.dirname(out)

plain = bpy.data.materials.new("Fabric_Plain"); plain.use_nodes = True
detail = bpy.data.materials.new("DetailMat"); detail.use_nodes = True
tree = detail.node_tree
image = bpy.data.images.new("detail_tex", 8, 8)
image.pixels = [0.8, 0.4, 0.1, 1.0] * 64
image.filepath_raw = os.path.join(folder, "detail_tex.png")
image.file_format = "PNG"; image.save()
tex = tree.nodes.new("ShaderNodeTexImage"); tex.image = image
uv = tree.nodes.new("ShaderNodeUVMap"); uv.uv_map = "Detail"
tree.links.new(uv.outputs["UV"], tex.inputs["Vector"])
tree.links.new(tex.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])

seats = bpy.data.collections.new("Seats"); scene.collection.children.link(seats)
bpy.ops.mesh.primitive_cube_add(size=1.0)
seat = bpy.context.object; seat.name = "Seat"
for existing in seat.users_collection:
    existing.objects.unlink(seat)
seats.objects.link(seat)
seat.data.materials.append(plain)

finish = scene.usd_stage_variant_sets.add(); finish.name = "Finish"; finish.kind = 'MATERIAL'
finish.target_collection = seats
for n, m in (("Plain", plain), ("Detail", detail)):
    v = finish.variants.add(); v.name = n; v.material = m
finish.default_variant = "Plain"

bpy.ops.mesh.primitive_cube_add(size=0.5, location=(3, 0, 0))
other = bpy.context.object; other.name = "Other"
other.data.uv_layers.new(name="Detail")
other.data.materials.append(detail)
for obj in bpy.context.view_layer.objects:
    obj.select_set(obj == other)
bpy.context.view_layer.objects.active = other
bpy.ops.wm.save_as_mainfile(filepath=out)
print("SAVED")
"""

#: Two targets, and only ``Seat`` carries the colour attribute the finish
#: reads: ``Back`` would read nothing, so the export must refuse.
_BUILD_PAINT_ON_ONE_TARGET = _BUILD_MESH_DATA.replace(
    """finish = scene.usd_stage_variant_sets.add()""",
    """bpy.ops.mesh.primitive_cube_add(size=0.5, location=(0, 1, 0))
back = bpy.context.object
back.name = "Back"
back.data.uv_layers[0].name = "Detail"
back.data.materials.append(plain)
for existing in back.users_collection:
    existing.objects.unlink(back)
seats.objects.link(back)

finish = scene.usd_stage_variant_sets.add()""")

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



@pytest.fixture(scope="module")
def unworn_finish(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("unworn_finish")
    blend = _build(workdir, _BUILD_MESH_DATA, "mesh_data")
    output = workdir / "out" / "seat.usda"
    output.parent.mkdir()
    return output, _export(blend, output)


@pytest.fixture(scope="module")
def unworn_uv_finish(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("unworn_uv_finish")
    blend = _build(workdir, _BUILD_UV_ONLY, "uv_only")
    output = workdir / "out" / "seat.usda"
    output.parent.mkdir()
    return output, _export(blend, output)


def test_an_unworn_finish_reading_a_colour_attribute_exports(unworn_finish):
    """The attribute is on the mesh the finish targets, so nothing is refused.

    The finish reads it through RealityKit's colour-set reader, and the mesh
    gets the displayColor that reader returns.
    """
    from pxr import Usd, UsdGeom, UsdShade

    output, result = unworn_finish
    assert result.returncode == 0, result.stdout + result.stderr
    stage = Usd.Stage.Open(str(output))
    seat = _mesh_under(stage, "Seat")
    stage.GetDefaultPrim().GetVariantSet("Finish").SetVariantSelection("Painted")
    material = UsdShade.MaterialBindingAPI(seat).ComputeBoundMaterial()[0]
    shader_ids = {
        str(UsdShade.Shader(prim).GetIdAttr().Get() or "")
        for prim in Usd.PrimRange(material.GetPrim()) if UsdShade.Shader(prim)
    }

    assert any(node.startswith("ND_geomcolor_") for node in shader_ids), shader_ids
    display = UsdGeom.PrimvarsAPI(seat).GetPrimvar("displayColor")
    assert display and display.HasAuthoredValue()


def test_an_unworn_finish_resolves_the_render_uv_map(unworn_uv_finish):
    """``Detail`` is the render UV map, which Blender writes as ``st``.

    Guessed from the name alone it would be read as ``primvars:Detail``, which
    the export never writes, and the texture would sample nothing.
    """
    from pxr import Usd, UsdGeom, UsdShade

    output, result = unworn_uv_finish
    assert result.returncode == 0, result.stdout + result.stderr
    stage = Usd.Stage.Open(str(output))
    seat = _mesh_under(stage, "Seat")
    assert not UsdGeom.PrimvarsAPI(seat).GetPrimvar("Detail")
    stage.GetDefaultPrim().GetVariantSet("Finish").SetVariantSelection("Painted")
    material = UsdShade.MaterialBindingAPI(seat).ComputeBoundMaterial()[0]

    assert material.GetPrim().GetName().endswith("Painted")
    assert "Detail" not in _geomprops_read(material.GetPrim())



def test_a_nested_finish_is_checked_only_where_it_is_offered(tmp_path):
    """Walnut, offered on Square, reads the square top's own ``Detail`` map."""
    from pxr import Usd, UsdShade

    blend = _build(tmp_path, _BUILD_NESTED, "nested")
    output = tmp_path / "out" / "table.usda"
    output.parent.mkdir()
    result = _export(blend, output)
    assert result.returncode == 0, result.stdout + result.stderr

    stage = Usd.Stage.Open(str(output))
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection("Square")
    root.GetVariantSet("Finish").SetVariantSelection("Walnut")
    top = _mesh_under(stage, "TopSquare")
    material = UsdShade.MaterialBindingAPI(top).ComputeBoundMaterial()[0]
    assert material.GetPrim().GetName() == "WalnutMat"
    assert "Detail" in _geomprops_read(material.GetPrim())


def test_a_finish_set_outside_a_selected_only_export_refuses_nothing(tmp_path):
    blend = _build(tmp_path, _BUILD_OUT_OF_SCOPE, "out_of_scope")
    output = tmp_path / "out" / "other.usda"
    output.parent.mkdir()

    result = _export(blend, output, "--selected-only")

    assert result.returncode == 0, result.stdout + result.stderr



def test_a_finish_target_without_the_colour_attribute_is_refused(tmp_path):
    """Any one target lacking it would render the finish without its colours."""
    blend = _build(tmp_path, _BUILD_PAINT_ON_ONE_TARGET, "paint_on_one")
    output = tmp_path / "out" / "seat.usda"
    output.parent.mkdir()

    result = _export(blend, output)

    assert result.returncode != 0
    output_text = result.stdout + result.stderr
    # The refusal names the object the user can find, not its mesh data.
    assert "Color attribute 'Paint' is not on object 'Back'" in output_text, output_text
