"""Integration test — shapes and finishes as two independent variant sets.

The combination this feature exists for: a Shape set activating one top or the
other, and a Finish set rebinding whichever is active. They vary different USD
properties, so both act on the same objects without either shadowing the other.

It also covers the awkward case a finish set is for: offering a material that
no object in the Blender scene currently wears, which Blender's exporter does
not write a material prim for at all.
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
Plugin.register()

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def material(name, colour):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = colour
    return mat

oak = material("OakMat", (0.52, 0.31, 0.13, 1.0))
# Never assigned to anything: Blender exports no prim for it, which is exactly
# the case a finish variant has to cope with.
walnut = material("WalnutMat", (0.13, 0.07, 0.04, 1.0))

def top(name, collection, primitive):
    primitive()
    obj = bpy.context.object
    obj.name = name
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    collection.objects.link(obj)
    obj.data.materials.append(oak)
    return obj

shape = bpy.data.collections.new("Shape")
scene.collection.children.link(shape)
round_c = bpy.data.collections.new("Round")
square_c = bpy.data.collections.new("Square")
shape.children.link(round_c)
shape.children.link(square_c)

round_top = top("TopRound", round_c, lambda: bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=0.04))
square_top = top("TopSquare", square_c, lambda: bpy.ops.mesh.primitive_cube_add(size=0.9))

# The finish set rebinds whichever top is active, so it targets both.
tops = bpy.data.collections.new("Tops")
scene.collection.children.link(tops)
tops.objects.link(round_top)
tops.objects.link(square_top)

sets = scene.usd_stage_variant_sets

shape_set = sets.add()
shape_set.name = "Shape"
shape_set.kind = 'GEOMETRY'
for variant_name, collection in (("Round", round_c), ("Square", square_c)):
    variant = shape_set.variants.add()
    variant.name = variant_name
    variant.collection = collection
shape_set.default_variant = "Round"

finish_set = sets.add()
finish_set.name = "Finish"
finish_set.kind = 'MATERIAL'
finish_set.target_collection = tops
for variant_name, mat in (("Oak", oak), ("Walnut", walnut)):
    variant = finish_set.variants.add()
    variant.name = variant_name
    variant.material = mat
finish_set.default_variant = "Oak"

bpy.context.view_layer.layer_collection.children["Shape"].children["Square"].exclude = True
bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
print("SAVED")
'''


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> dict:
    workdir = tmp_path_factory.mktemp("material_variants")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "table.blend"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python", str(script), "--", str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    output = workdir / "table.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "export", str(blend),
         "diagnostics_enabled=true", "-o", str(output), "--format", "USDA"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {"stage": output, "diagnostics": output.with_suffix(".diagnostics.json")}


def _state(stage_path: Path, shape: str, finish: str):
    from pxr import Usd, UsdGeom, UsdShade

    stage = Usd.Stage.Open(str(stage_path))
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection(shape)
    root.GetVariantSet("Finish").SetVariantSelection(finish)
    tops = [c for c in root.GetChildren() if c.GetName().startswith("Top")]
    assert len(tops) == 1, [t.GetName() for t in tops]
    # The binding lives on the mesh beneath the object's Xform, which is where
    # Blender puts it and therefore where the variants rebind it.
    meshes = [p for p in Usd.PrimRange(tops[0]) if p.IsA(UsdGeom.Mesh)]
    assert meshes, f"{tops[0].GetName()} has no mesh"
    bound = UsdShade.MaterialBindingAPI(meshes[0]).ComputeBoundMaterial()[0]
    return tops[0].GetName(), (bound.GetPrim().GetName() if bound else None)


def test_both_sets_reach_the_stage(exported):
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(exported["stage"]))
    root = stage.GetDefaultPrim()

    assert sorted(root.GetVariantSets().GetNames()) == ["Finish", "Shape"]
    assert root.GetVariantSet("Shape").GetVariantSelection() == "Round"
    assert root.GetVariantSet("Finish").GetVariantSelection() == "Oak"


def test_every_shape_and_finish_combination_composes(exported):
    pytest.importorskip("pxr")

    seen = {
        (shape, finish): _state(exported["stage"], shape, finish)
        for shape in ("Round", "Square")
        for finish in ("Oak", "Walnut")
    }

    assert seen == {
        ("Round", "Oak"): ("TopRound", "OakMat"),
        ("Round", "Walnut"): ("TopRound", "WalnutMat"),
        ("Square", "Oak"): ("TopSquare", "OakMat"),
        ("Square", "Walnut"): ("TopSquare", "WalnutMat"),
    }


def test_a_finish_no_object_wears_still_exports_as_a_real_material(exported):
    """Blender writes no prim for WalnutMat; the finish set has to supply one."""
    pytest.importorskip("pxr")
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(exported["stage"]))
    materials = {
        p.GetName(): p for p in stage.TraverseAll() if p.IsA(UsdShade.Material)
    }
    assert "WalnutMat" in materials, "the unused finish never reached the stage"

    # OakMat went through the ordinary path, so whatever shape the rewrite
    # gives a material, WalnutMat must have it too.
    def shader_kinds(prim):
        # The suffix is a per-material counter, so compare the kind of shader
        # the rewrite built, not the exact prim name.
        return sorted(
            child.GetName().rsplit("_", 1)[0] for child in prim.GetChildren()
        )

    assert shader_kinds(materials["WalnutMat"]) == shader_kinds(materials["OakMat"]), (
        "WalnutMat did not get the same shader the ordinary path gives OakMat"
    )
    assert shader_kinds(materials["WalnutMat"]) == ["pbr_surfaceshader"]


def test_no_binding_survives_outside_the_variants(exported):
    """A local opinion would beat the variants and freeze the finish."""
    text = exported["stage"].read_text()
    block = text.split('variantSet "Finish"')[0]

    assert "material:binding" not in block, (
        "a binding outside the Finish variants would shadow them"
    )


def test_the_export_reports_both_sets(exported):
    payload = json.loads(exported["diagnostics"].read_text())
    kinds = {entry["name"]: entry["kind"] for entry in payload["variant_sets"]}

    assert kinds == {"Shape": "GEOMETRY", "Finish": "MATERIAL"}
    assert payload["errors"] == []
