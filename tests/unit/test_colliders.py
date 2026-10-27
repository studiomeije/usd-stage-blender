"""RealityKit collider components, authored from Blender's rigid bodies."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Usd, UsdGeom  # noqa: E402

from Plugin.export.colliders import (  # noqa: E402
    ColliderMember,
    author_colliders,
    resolve_collider_plan,
)
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402


def _object(name, shape="BOX", physical=True, object_type="MESH"):
    return SimpleNamespace(
        name=name,
        type=object_type,
        rigid_body=(
            SimpleNamespace(collision_shape=shape) if physical else None
        ),
    )


def _context(*objects):
    return SimpleNamespace(scene=SimpleNamespace(objects=list(objects)))


def _settings(**overrides):
    values = {"export_colliders": True, "export_armatures": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _stage(size=2.0, centre=(0.0, 0.0, 0.0)):
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.Xform.Define(stage, "/Root/Table")
    cube = UsdGeom.Cube.Define(stage, "/Root/Table/Mesh")
    cube.GetSizeAttr().Set(size)
    half = size / 2.0
    cube.GetExtentAttr().Set([(-half,) * 3, (half,) * 3])
    if any(centre):
        UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(*centre))
    return stage


def _collider(stage, path="/Root/Table/Collider"):
    return stage.GetPrimAtPath(path)


# --------------------------------------------------------------------------
# Which objects ask for one
# --------------------------------------------------------------------------

def test_only_objects_with_rigid_body_ask_for_a_collider():
    plan = resolve_collider_plan(
        _context(_object("Table"), _object("Lamp", physical=False)),
        _settings(),
    )

    assert [m.object_name for m in plan] == ["Table"]


def test_the_setting_turns_it_off():
    plan = resolve_collider_plan(
        _context(_object("Table")), _settings(export_colliders=False)
    )

    assert plan == ()


def test_an_object_class_the_export_drops_is_not_given_a_collider():
    plan = resolve_collider_plan(
        _context(_object("KeyLight", object_type="LIGHT")), _settings()
    )

    assert plan == ()


# --------------------------------------------------------------------------
# What gets written
# --------------------------------------------------------------------------

def test_the_component_is_spelled_the_way_reality_composer_pro_spells_it():
    stage = _stage()
    plan = resolve_collider_plan(_context(_object("Table")), _settings())

    authored = author_colliders(stage, plan, {"/Root/Table": "Table"})

    assert authored == ("/Root/Table/Collider",)
    collider = _collider(stage)
    assert collider.GetTypeName() == "RealityKitComponent"
    assert collider.GetAttribute("info:id").Get() == "RealityKit.Collider"
    assert collider.GetAttribute("group").Get() == 1
    assert collider.GetAttribute("mask").Get() == 4294967295
    assert collider.GetAttribute("type").Get() == "Default"

    shape = stage.GetPrimAtPath("/Root/Table/Collider/Shape")
    assert shape.GetTypeName() == "RealityKitStruct"
    assert shape.GetAttribute("shapeType").Get() == "Box"


def test_the_box_is_sized_from_the_geometry():
    stage = _stage(size=2.0)
    plan = resolve_collider_plan(_context(_object("Table")), _settings())

    author_colliders(stage, plan, {"/Root/Table": "Table"})

    extent = stage.GetPrimAtPath("/Root/Table/Collider/Shape").GetAttribute(
        "extent"
    ).Get()
    assert [round(value, 5) for value in extent] == [2.0, 2.0, 2.0]


def test_off_centre_geometry_gets_a_pose():
    stage = _stage(size=1.0, centre=(0.0, 0.5, 0.0))
    plan = resolve_collider_plan(_context(_object("Table")), _settings())

    author_colliders(stage, plan, {"/Root/Table": "Table"})

    pose = stage.GetPrimAtPath("/Root/Table/Collider/Shape/pose")
    assert pose.GetTypeName() == "RealityKitStruct"
    assert [round(v, 5) for v in pose.GetAttribute("position").Get()] == [0.0, 0.5, 0.0]


def test_centred_geometry_gets_no_pose():
    """An all-zero pose is noise, and Reality Composer Pro omits it too."""
    stage = _stage()
    plan = resolve_collider_plan(_context(_object("Table")), _settings())

    author_colliders(stage, plan, {"/Root/Table": "Table"})

    assert not stage.GetPrimAtPath("/Root/Table/Collider/Shape/pose")


# --------------------------------------------------------------------------
# Honesty about what it cannot do
# --------------------------------------------------------------------------

def test_a_non_box_shape_is_approximated_and_said_so():
    stage = _stage()
    plan = resolve_collider_plan(
        _context(_object("Table", shape="CONVEX_HULL")), _settings()
    )
    diagnostics = ExportDiagnostics()

    author_colliders(stage, plan, {"/Root/Table": "Table"}, (), diagnostics)

    assert stage.GetPrimAtPath("/Root/Table/Collider/Shape").GetAttribute(
        "shapeType"
    ).Get() == "Box"
    warnings = diagnostics.to_dict()["warnings"]
    assert any("CONVEX_HULL" in warning for warning in warnings)


def test_an_object_that_produced_no_prim_is_reported():
    stage = _stage()
    plan = resolve_collider_plan(_context(_object("Missing")), _settings())
    diagnostics = ExportDiagnostics()

    assert author_colliders(stage, plan, {"/Root/Table": "Table"}, (), diagnostics) == ()
    assert any("Missing" in w for w in diagnostics.to_dict()["warnings"])


def test_without_a_prim_map_nothing_is_guessed():
    stage = _stage()
    plan = resolve_collider_plan(_context(_object("Table")), _settings())
    diagnostics = ExportDiagnostics()

    assert author_colliders(stage, plan, None, (), diagnostics) == ()
    assert any("prim map" in w for w in diagnostics.to_dict()["warnings"])


def test_geometry_with_no_bounds_is_skipped_rather_than_boxed_at_zero():
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.Xform.Define(stage, "/Root/Empty")
    plan = resolve_collider_plan(_context(_object("Empty")), _settings())
    diagnostics = ExportDiagnostics()

    assert author_colliders(stage, plan, {"/Root/Empty": "Empty"}, (), diagnostics) == ()
    assert any("bounds" in w for w in diagnostics.to_dict()["warnings"])


def test_what_was_authored_reaches_the_sidecar():
    stage = _stage()
    plan = resolve_collider_plan(_context(_object("Table")), _settings())
    diagnostics = ExportDiagnostics()

    author_colliders(stage, plan, {"/Root/Table": "Table"}, (), diagnostics)

    assert diagnostics.to_dict()["colliders"] == ["/Root/Table/Collider"]


def test_a_collider_is_sized_before_any_variant_can_hide_its_object():
    """The pipeline authors colliders first, so every variant member gets one.

    Authored after the variants, a member outside the default selection has
    no composed prim, and would silently go without.
    """
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/Root/Spare")
    spare = UsdGeom.Cube.Define(stage, "/Root/Spare/Mesh")
    spare.GetSizeAttr().Set(1.0)
    spare.GetExtentAttr().Set([(-0.5,) * 3, (0.5,) * 3])
    plan = resolve_collider_plan(_context(_object("Table"), _object("Spare")), _settings())

    authored = author_colliders(
        stage, plan, {"/Root/Table": "Table", "/Root/Spare": "Spare"}
    )

    assert sorted(authored) == ["/Root/Spare/Collider", "/Root/Table/Collider"]
    # And a collider rides with its object when a variant deactivates it.
    stage.GetPrimAtPath("/Root/Spare").SetActive(False)
    assert not stage.GetPrimAtPath("/Root/Spare/Collider")


def test_a_child_object_named_collider_is_left_alone():
    """A collision proxy parented under its object is routinely called Collider."""
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/Root/Table/Collider")
    UsdGeom.Cube.Define(stage, "/Root/Table/Collider/Proxy")

    written = author_colliders(stage, (ColliderMember("Table", "BOX"),),
                               {"/Root/Table": "Table"})

    assert written == ("/Root/Table/Collider_1",)
    assert stage.GetPrimAtPath("/Root/Table/Collider").GetTypeName() == "Xform"
    assert stage.GetPrimAtPath("/Root/Table/Collider_1").GetTypeName() == "RealityKitComponent"
