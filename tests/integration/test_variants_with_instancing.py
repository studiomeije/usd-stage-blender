"""Integration test — variants over instanced objects, and through a bake.

Instancing is on by default, and it is the combination most likely to go
wrong without anyone noticing: with it on, Blender writes prototypes and
instance proxies, and USD does not allow authoring through a proxy. Variant
activation, collider components and finish bindings all author on object
prims, so each had to be checked rather than assumed.

The bake route is here for the same reason: it reaches the same post-process
by a different path, and no variant test had ever taken it.
"""

from __future__ import annotations

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

oak = material("OakMat", (0.5, 0.3, 0.1, 1))
walnut = material("WalnutMat", (0.12, 0.07, 0.04, 1))

# A prototype collection, instanced rather than copied.
proto = bpy.data.collections.new("PawnProto")
bpy.ops.mesh.primitive_cube_add(size=0.4)
body = bpy.context.object
body.name = "PawnBody"
for existing in body.users_collection:
    existing.objects.unlink(body)
proto.objects.link(body)
body.data.materials.append(oak)

shape = bpy.data.collections.new("Shape")
scene.collection.children.link(shape)
pawns = bpy.data.collections.new("Pawns")
scene.collection.children.link(pawns)

for variant_name, count in (("Few", 1), ("Many", 3)):
    group = bpy.data.collections.new(variant_name)
    shape.children.link(group)
    for index in range(count):
        empty = bpy.data.objects.new(f"{variant_name}_{index}", None)
        empty.instance_type = 'COLLECTION'
        empty.instance_collection = proto
        empty.location = (index * 0.6, 0, 0)
        group.objects.link(empty)
        pawns.objects.link(empty)          # the finish set rebinds these

    # A solid mesh too, so an instanced member and an ordinary one are both
    # covered, and give it a rigid body so it asks for a collider.
    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0, 1, 0))
    solid = bpy.context.object
    solid.name = f"{variant_name}_Solid"
    for existing in solid.users_collection:
        existing.objects.unlink(solid)
    group.objects.link(solid)
    solid.data.materials.append(oak)
    bpy.ops.rigidbody.object_add()

sets = scene.usd_stage_variant_sets
shape_set = sets.add()
shape_set.name = "Shape"
shape_set.kind = 'GEOMETRY'
for name in ("Few", "Many"):
    variant = shape_set.variants.add()
    variant.name = name
    variant.collection = bpy.data.collections[name]
shape_set.default_variant = "Few"

finish_set = sets.add()
finish_set.name = "Finish"
finish_set.kind = 'MATERIAL'
finish_set.target_collection = pawns
for name, mat in (("Oak", oak), ("Walnut", walnut)):
    variant = finish_set.variants.add()
    variant.name = name
    variant.material = mat
finish_set.default_variant = "Oak"

bpy.context.view_layer.layer_collection.children["Shape"].children["Many"].exclude = True
bpy.ops.wm.save_as_mainfile(filepath=out, compress=True)
print("SAVED")
'''


def _usdstage(*args, timeout=900):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), *args],
        capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def scene_file(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("instanced_variants")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "pawns.blend"

    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python", str(script), "--", str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


@pytest.fixture(scope="module")
def exported(scene_file, tmp_path_factory) -> Path:
    output = tmp_path_factory.mktemp("instanced_out") / "pawns.usda"
    result = _usdstage(
        "export", str(scene_file), "use_instancing=true",
        "-o", str(output), "--format", "USDA",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


def _with_selection(path: Path, shape: str, finish: str | None = None):
    from pxr import Usd

    stage = Usd.Stage.Open(str(path))
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection(shape)
    if finish and "Finish" in root.GetVariantSets().GetNames():
        root.GetVariantSet("Finish").SetVariantSelection(finish)
    return stage, root


def test_activation_works_on_instance_roots(exported):
    """Deactivating an instance root is allowed; through a proxy it is not."""
    pytest.importorskip("pxr")

    _, few = _with_selection(exported, "Few")
    assert sorted(c.GetName() for c in few.GetChildren() if c.GetName().startswith("Few")) == [
        "Few_0", "Few_Solid"
    ]

    _, many = _with_selection(exported, "Many")
    assert sorted(c.GetName() for c in many.GetChildren() if c.GetName().startswith("Many")) == [
        "Many_0", "Many_1", "Many_2", "Many_Solid"
    ]


def test_the_prototype_is_still_shared(exported):
    """Instancing has to survive, or the variants cost more than they save."""
    pytest.importorskip("pxr")

    stage, _ = _with_selection(exported, "Many")
    instanced = [prim for prim in stage.Traverse() if prim.IsInstanceable()]

    assert len(instanced) == 3, [str(p.GetPath()) for p in instanced]


def test_a_finish_rebinds_instanced_objects(exported):
    """The binding lands on the instance root and composes down to the proxy."""
    pytest.importorskip("pxr")
    from pxr import UsdShade

    for finish, expected in (("Oak", "OakMat"), ("Walnut", "WalnutMat")):
        stage, _ = _with_selection(exported, "Many", finish)
        # The instanced pawns are the finish set's targets; the solid mesh
        # beside them is not, so it must keep its own material.
        bound = {
            prim.GetName(): UsdShade.MaterialBindingAPI(prim)
            .ComputeBoundMaterial()[0].GetPrim().GetName()
            for prim in stage.Traverse()
            if prim.IsInstanceable()
        }
        assert bound, "no instanced pawn was found to check"
        assert set(bound.values()) == {expected}, (finish, bound)


def test_every_variant_member_keeps_its_collider(exported):
    pytest.importorskip("pxr")

    for shape in ("Few", "Many"):
        stage, _ = _with_selection(exported, shape)
        colliders = [
            str(prim.GetPath()) for prim in stage.Traverse()
            if prim.GetName() == "Collider"
        ]
        assert colliders == [f"/root/{shape}_Solid/Collider"], (shape, colliders)


def test_variants_survive_the_bake_route(scene_file, tmp_path_factory):
    """A different path into the same post-process, never taken before."""
    pytest.importorskip("pxr")
    output = tmp_path_factory.mktemp("baked") / "pawns.usda"

    result = _usdstage(
        "bake-export", str(scene_file),
        "-o", str(output), "--format", "USDA", "--resolution", "64",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    stage, root = _with_selection(output, "Many")
    assert "Shape" in root.GetVariantSets().GetNames()
    assert sorted(
        c.GetName() for c in root.GetChildren() if c.GetName().startswith("Many")
    ) == ["Many_0", "Many_1", "Many_2", "Many_Solid"]
    assert [p.GetName() for p in stage.Traverse() if p.GetName() == "Collider"]
