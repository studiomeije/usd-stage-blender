"""Finish sets over collection instances.

A collection instance's meshes are not the instancer's own prims: Blender
writes them beneath it, once per instance, and with Instancing on they sit in
a shared prototype that nothing outside can rebind. These stages reproduce
what the exporter writes in both modes, with the prim map it reports.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export.variant_authoring import (  # noqa: E402
    VariantAuthoringError,
    author_variants,
    reserve_variant_materials,
)
from Plugin.export.variants import resolve_variant_plan  # noqa: E402


def _settings():
    return SimpleNamespace(
        export_variants=True,
        export_armatures=True,
        selected_objects_only=False,
        root_prim_name="Root",
        allow_unicode=False,
    )


def _mesh_object(name):
    return SimpleNamespace(name=name, type="MESH", instance_type="NONE",
                           instance_collection=None)


def _instancer(name, *members):
    proto = SimpleNamespace(name="Proto", objects=list(members), children=[],
                            all_objects=list(members))
    return SimpleNamespace(name=name, type="EMPTY", instance_type="COLLECTION",
                           instance_collection=proto)


def _finish_plan(*targets):
    collection = SimpleNamespace(name="Targets", objects=list(targets), children=[])
    finish = SimpleNamespace(
        name="Finish",
        kind="MATERIAL",
        variants=[
            SimpleNamespace(name="Oak", material=SimpleNamespace(name="OakMat")),
            SimpleNamespace(name="Walnut", material=SimpleNamespace(name="WalnutMat")),
        ],
        target_collection=collection,
        default_variant="Oak",
    )
    context = SimpleNamespace(scene=SimpleNamespace(usd_stage_variant_sets=[finish]))
    return resolve_variant_plan(context, _settings())


def _stage():
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    for name in ("OakMat", "WalnutMat"):
        UsdShade.Material.Define(stage, f"/Root/_materials/{name}")
    return stage


def _bound_mesh(stage, path, material="OakMat"):
    mesh = UsdGeom.Mesh.Define(stage, path)
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
    UsdShade.MaterialBindingAPI(mesh.GetPrim()).Bind(
        UsdShade.Material.Get(stage, f"/Root/_materials/{material}")
    )
    return mesh


def _instanced_stage(count):
    """Instancing on: each pawn references one prototype, instanceable."""
    stage = _stage()
    stage.CreateClassPrim("/Root/prototypes")
    UsdGeom.Xform.Define(stage, "/Root/prototypes/PawnBody_0")
    _bound_mesh(stage, "/Root/prototypes/PawnBody_0/Cube")
    object_map = {}
    for index in range(count):
        UsdGeom.Xform.Define(stage, f"/Root/Pawn_{index}")
        body = UsdGeom.Xform.Define(stage, f"/Root/Pawn_{index}/PawnBody_0").GetPrim()
        body.GetReferences().AddInternalReference("/Root/prototypes/PawnBody_0")
        body.SetInstanceable(True)
        object_map[f"/Root/Pawn_{index}"] = f"Pawn_{index}"
        object_map[f"/Root/Pawn_{index}/PawnBody_0"] = "PawnBody"
    return stage, object_map


def _real_stage(count):
    """Instancing off: every pawn gets its own copy of the collection's mesh."""
    stage = _stage()
    object_map = {}
    for index in range(count):
        UsdGeom.Xform.Define(stage, f"/Root/Pawn_{index}")
        UsdGeom.Xform.Define(stage, f"/Root/Pawn_{index}/PawnBody_0")
        _bound_mesh(stage, f"/Root/Pawn_{index}/PawnBody_0/Cube")
        object_map[f"/Root/Pawn_{index}"] = f"Pawn_{index}"
        object_map[f"/Root/Pawn_{index}/PawnBody_0"] = "PawnBody"
    return stage, object_map


def _author(stage, plan, object_map):
    reserve_variant_materials(stage, plan)
    return author_variants(stage, plan, object_map, (), _settings())


def _resolved(stage, finish):
    stage.GetDefaultPrim().GetVariantSet("Finish").SetVariantSelection(finish)
    return {
        str(prim.GetPath()): UsdShade.MaterialBindingAPI(prim)
        .ComputeBoundMaterial()[0].GetPrim().GetName()
        for prim in Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies())
        if prim.IsA(UsdGeom.Mesh)
    }


@pytest.mark.parametrize("count", [1, 3])
def test_a_finish_over_an_instance_is_refused(count):
    stage, object_map = _instanced_stage(count)
    plan = _finish_plan(*(_instancer(f"Pawn_{i}", _mesh_object("PawnBody"))
                          for i in range(count)))

    with pytest.raises(VariantAuthoringError, match=r"cannot rebind 'Pawn_0'.*"
                       r"exported as an instance \(/Root/Pawn_0/PawnBody_0\)"
                       r".*Turn off Instancing"):
        _author(stage, plan, object_map)


def test_an_instanceable_object_prim_is_refused_too():
    stage = _stage()
    stage.CreateClassPrim("/Root/prototypes")
    _bound_mesh(stage, "/Root/prototypes/Body/Cube")
    pawn = UsdGeom.Xform.Define(stage, "/Root/Pawn").GetPrim()
    pawn.GetReferences().AddInternalReference("/Root/prototypes/Body")
    pawn.SetInstanceable(True)

    with pytest.raises(VariantAuthoringError, match="exported as an instance"):
        _author(stage, _finish_plan(_instancer("Pawn")), {"/Root/Pawn": "Pawn"})


@pytest.mark.parametrize("count", [1, 3])
def test_every_copy_of_a_real_instance_follows_the_finish(count):
    """One object name owns every copy; none of them may be skipped."""
    stage, object_map = _real_stage(count)
    plan = _finish_plan(*(_instancer(f"Pawn_{i}", _mesh_object("PawnBody"))
                          for i in range(count)))

    _author(stage, plan, object_map)

    for finish, expected in (("Oak", "OakMat"), ("Walnut", "WalnutMat")):
        resolved = _resolved(stage, finish)
        assert len(resolved) == count
        assert set(resolved.values()) == {expected}, (finish, resolved)
    # Blender's own binding is gone, so no local opinion shadows the variants.
    layer = stage.GetRootLayer()
    for index in range(count):
        spec = layer.GetPrimAtPath(f"/Root/Pawn_{index}/PawnBody_0/Cube")
        assert spec.relationships.get("material:binding") is None


def test_a_parented_object_is_still_left_alone():
    """Only an instancer's collection counts as its own; a child does not."""
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/Root/Table")
    _bound_mesh(stage, "/Root/Table/Top")
    UsdGeom.Xform.Define(stage, "/Root/Table/Vase")
    _bound_mesh(stage, "/Root/Table/Vase/Body")
    object_map = {"/Root/Table": "Table", "/Root/Table/Vase": "Vase"}

    _author(stage, _finish_plan(_mesh_object("Table")), object_map)

    resolved = _resolved(stage, "Walnut")
    assert resolved == {"/Root/Table/Top": "WalnutMat", "/Root/Table/Vase/Body": "OakMat"}


def test_instancing_through_a_nested_instancer_is_followed():
    """A collection that itself instances another is still the instancer's."""
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/Root/Set")
    UsdGeom.Xform.Define(stage, "/Root/Set/Pair_0")
    UsdGeom.Xform.Define(stage, "/Root/Set/Pair_0/PawnBody_0")
    _bound_mesh(stage, "/Root/Set/Pair_0/PawnBody_0/Cube")
    object_map = {
        "/Root/Set": "Set",
        "/Root/Set/Pair_0": "Pair",
        "/Root/Set/Pair_0/PawnBody_0": "PawnBody",
    }
    inner = _instancer("Pair", _mesh_object("PawnBody"))

    _author(stage, _finish_plan(_instancer("Set", inner)), object_map)

    assert _resolved(stage, "Walnut") == {"/Root/Set/Pair_0/PawnBody_0/Cube": "WalnutMat"}


@pytest.mark.parametrize("bound_parts", [(), ("PartA",), ("PartA", "PartB")])
def test_every_mesh_of_a_multi_object_instance_follows_the_finish(bound_parts):
    """An instanced collection of two objects: both take the finish.

    Blender may have bound either, both or neither of them, and an unbound
    mesh must not be skipped just because another one carries a binding.
    """
    stage = _stage()
    object_map = {"/Root/Pawn": "Pawn"}
    UsdGeom.Xform.Define(stage, "/Root/Pawn")
    for part in ("PartA", "PartB"):
        UsdGeom.Xform.Define(stage, f"/Root/Pawn/{part}")
        mesh_path = f"/Root/Pawn/{part}/Cube"
        if part in bound_parts:
            _bound_mesh(stage, mesh_path)
        else:
            UsdGeom.Mesh.Define(stage, mesh_path)
        object_map[f"/Root/Pawn/{part}"] = part
    plan = _finish_plan(_instancer("Pawn", _mesh_object("PartA"), _mesh_object("PartB")))

    _author(stage, plan, object_map)

    for finish, expected in (("Oak", "OakMat"), ("Walnut", "WalnutMat")):
        resolved = _resolved(stage, finish)
        assert sorted(resolved) == ["/Root/Pawn/PartA/Cube", "/Root/Pawn/PartB/Cube"]
        assert set(resolved.values()) == {expected}, (finish, resolved)
