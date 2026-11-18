"""Integration test — staging a variant export changes nothing it should not.

An artist excludes a whole group of collections and re-includes the one they
are working on: ``Variants`` is excluded, its ``Fabric`` child included again,
and its ``Leather`` and ``Props`` children left excluded. Fabric and Leather
are the ``Seat`` variants; Props is not a variant at all.

In Blender 5.3 setting ``exclude`` on a layer collection sets it on the whole
subtree, and an included child of an excluded parent still contributes its
objects. Staging that re-included ``Variants`` to reach the members would pull
``Props`` and the group's own loose object into the export, and putting the
parent back afterwards would cascade over Fabric and leave the user's view
layer changed. Fakes cannot model that cascade faithfully against the real
exporter, so this runs the shipped export operator in Blender.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r'''
import bpy, json, sys
from pathlib import Path

repo, work, mode = sys.argv[-3], Path(sys.argv[-2]), sys.argv[-1]
sys.path.insert(0, repo)
import Plugin

bpy.ops.wm.read_factory_settings(use_empty=True)
Plugin.register()
scene = bpy.context.scene


def collection(name, parent):
    created = bpy.data.collections.new(name)
    parent.children.link(created)
    return created


def cube(name, owner, x):
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(x, 0.0, 0.25))
    obj = bpy.context.object
    obj.name = name
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    owner.objects.link(obj)
    return obj


if mode != "only_members":
    cube("Plinth", scene.collection, 0.0)
group = collection("Variants", scene.collection)
fabric = collection("Fabric", group)
leather = collection("Leather", group)
props = collection("Props", group)
box = collection("Box", props)
cube("LooseLamp", group, 1.0)
cube("FabricSeat", fabric, 2.0)
cube("LeatherSeat", leather, 3.0)
cube("PropCrate", props, 4.0)
cube("BoxCrate", box, 5.0)

if mode == "hidden_proto":
    # Leather shows a collection that lives beside it, not inside it.
    protos = collection("Protos", group)
    cube("PartA", protos, 6.0)
    instancer = bpy.data.objects.new("LeatherInst", None)
    instancer.instance_type = "COLLECTION"
    instancer.instance_collection = protos
    leather.objects.link(instancer)
elif mode == "hidden_rig":
    # The rig deforming a member sits loose in the group, not in a variant.
    rig = bpy.data.objects.new("Rig", bpy.data.armatures.new("RigData"))
    group.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bone = rig.data.edit_bones.new("Bone")
    bone.head, bone.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    seat_mesh = bpy.data.objects["LeatherSeat"]
    seat_mesh.vertex_groups.new(name="Bone").add(
        list(range(len(seat_mesh.data.vertices))), 1.0, "REPLACE")
    seat_mesh.modifiers.new("Armature", "ARMATURE").object = rig

seat = scene.usd_stage_variant_sets.add()
seat.name = "Seat"
for name, owner in (("Fabric", fabric), ("Leather", leather)):
    variant = seat.variants.add()
    variant.name = name
    variant.collection = owner
seat.default_variant = "Fabric"

group_layer = bpy.context.view_layer.layer_collection.children["Variants"]
if mode.startswith("hidden"):
    group.hide_render = True                # hides the whole subtree
else:
    group_layer.exclude = True              # cascades to every child
    group_layer.children["Fabric"].exclude = False


def layer_state():
    found = {}
    stack = [(bpy.context.view_layer.layer_collection, "")]
    while stack:
        layer, path = stack.pop()
        for child in layer.children:
            child_path = f"{path}/{child.name}"
            found[child_path] = {
                "exclude": child.exclude,
                "hide_viewport": child.hide_viewport,
                "collection_hide_render": child.collection.hide_render,
                "collection_hide_viewport": child.collection.hide_viewport,
            }
            stack.append((child, child_path))
    return found


def object_state():
    return {
        obj.name: [obj.hide_render, obj.hide_viewport, obj.hide_get()]
        for obj in bpy.data.objects
    }


before = {"layers": layer_state(), "objects": object_state()}

settings = scene.usd_stage_export_settings
settings.export_format = "USDA"
output = work / "staged.usda"
assert bpy.ops.usdstage.export(filepath=str(output)) == {"FINISHED"}

after = {"layers": layer_state(), "objects": object_state()}

from pxr import Usd, UsdGeom, UsdSkel

stage = Usd.Stage.Open(str(output))
prims = sorted(
    prim.GetName() for prim in Usd.PrimRange.AllPrims(stage.GetPseudoRoot())
)
# Fabric is the default, so Leather's members only compose once selected.
stage.GetDefaultPrim().GetVariantSet("Seat").SetVariantSelection("Leather")
composed = list(Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies()))
instance_meshes = sum(
    1 for prim in composed
    if prim.IsA(UsdGeom.Mesh) and "LeatherInst" in str(prim.GetPath())
)
skeletons = sum(1 for prim in composed if prim.IsA(UsdSkel.Skeleton))
(work / "report.json").write_text(json.dumps({
    "before": before, "after": after, "prims": prims,
    "instance_meshes": instance_meshes, "skeletons": skeletons,
}))
'''


def _run(tmp_path_factory, mode: str) -> dict:
    work = tmp_path_factory.mktemp(f"variant_staging_{mode}")
    script = work / "probe.py"
    script.write_text(_PROBE)
    result = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python-exit-code", "1", "--python", str(script), "--",
            str(REPO_ROOT), str(work), mode,
        ],
        capture_output=True, text=True, timeout=600,
    )
    report_path = work / "report.json"
    assert result.returncode == 0 and report_path.exists(), (
        result.stdout + result.stderr
    )
    return json.loads(report_path.read_text())


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    return _run(tmp_path_factory, "excluded")


@pytest.fixture(scope="module")
def members_only_report(tmp_path_factory) -> dict:
    """The same scene with nothing outside the variant group."""
    return _run(tmp_path_factory, "only_members")


@pytest.fixture(scope="module")
def hidden_proto_report(tmp_path_factory) -> dict:
    """A hidden group whose Leather variant instances a collection beside it."""
    return _run(tmp_path_factory, "hidden_proto")


@pytest.fixture(scope="module")
def hidden_rig_report(tmp_path_factory) -> dict:
    """A hidden group whose rig, loose in the group, deforms a member."""
    return _run(tmp_path_factory, "hidden_rig")


@pytest.fixture(scope="module")
def hidden_parent_report(tmp_path_factory) -> dict:
    """``Variants`` disabled in renders instead of excluded."""
    return _run(tmp_path_factory, "hidden_parent")


def test_the_scene_starts_the_way_the_artist_left_it(report):
    """The control: the cascade really left this mixed state to stage from."""
    excludes = {path: state["exclude"] for path, state in report["before"]["layers"].items()}

    assert excludes == {
        "/Variants": True,
        "/Variants/Fabric": False,
        "/Variants/Leather": True,
        "/Variants/Props": True,
        "/Variants/Props/Box": True,
    }


def test_both_variants_reach_the_export(report):
    assert "FabricSeat" in report["prims"]
    assert "LeatherSeat" in report["prims"]
    assert "Plinth" in report["prims"]


def test_excluded_non_members_stay_out_of_the_export(report):
    """Neither a non-member sibling, its child, nor the group's own object."""
    leaked = {"PropCrate", "BoxCrate", "LooseLamp"} & set(report["prims"])

    assert not leaked, f"staging exported excluded non-members: {sorted(leaked)}"


def test_every_layer_collection_exclude_is_restored(report):
    before = {path: state["exclude"] for path, state in report["before"]["layers"].items()}
    after = {path: state["exclude"] for path, state in report["after"]["layers"].items()}

    assert after == before


def test_every_hide_flag_is_restored(report):
    assert report["after"]["layers"] == report["before"]["layers"]
    assert report["after"]["objects"] == report["before"]["objects"]


def test_members_under_an_excluded_parent_are_in_the_export_scope(members_only_report):
    """The scope walk must look inside an excluded parent, as Blender does.

    With nothing else in the scene, a scope that skipped the excluded
    ``Variants`` subtree would find no object at all and refuse the export.
    """
    assert {"FabricSeat", "LeatherSeat"} <= set(members_only_report["prims"])
    assert members_only_report["after"] == members_only_report["before"]


def test_a_hidden_parent_brings_in_only_the_members(hidden_parent_report):
    """Clearing a hide flag shows a whole subtree; non-members stay hidden."""
    prims = set(hidden_parent_report["prims"])

    assert {"FabricSeat", "LeatherSeat", "Plinth"} <= prims
    leaked = {"PropCrate", "BoxCrate", "LooseLamp"} & prims
    assert not leaked, f"staging exported hidden non-members: {sorted(leaked)}"
    assert hidden_parent_report["after"] == hidden_parent_report["before"]


def test_a_members_instance_prototype_is_not_hidden(hidden_proto_report):
    """Hiding the prototype would empty every instance of it."""
    assert hidden_proto_report["instance_meshes"] >= 1
    assert hidden_proto_report["after"] == hidden_proto_report["before"]


def test_a_members_deforming_rig_is_not_hidden(hidden_rig_report):
    """A skinned member needs its skeleton in the export."""
    assert "LeatherSeat" in hidden_rig_report["prims"]
    assert hidden_rig_report["skeletons"] >= 1
    assert hidden_rig_report["after"] == hidden_rig_report["before"]
