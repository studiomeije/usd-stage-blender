"""Authoring declared variant sets onto an exported stage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.realitykit_preflight import (  # noqa: E402
    _check_variant_activation_is_unopposed,
    RealityKitPreflightReport,
)
from Plugin.export.usd_scene import normalize_scene  # noqa: E402
from Plugin.export.variant_authoring import (  # noqa: E402
    VariantAuthoringError,
    author_variants,
    reserve_variant_materials,
)
from Plugin.export.variants import resolve_variant_plan  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _object(name: str):
    return SimpleNamespace(name=name, type="MESH")


def _collection(name: str, objects=()):
    return SimpleNamespace(name=name, objects=list(objects), children=[])


def _settings(**overrides):
    values = {
        "export_variants": True,
        "export_armatures": True,
        "selected_objects_only": False,
        "root_prim_name": "Root",
        "allow_unicode": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _plan_for(*variant_sets, settings=None):
    context = SimpleNamespace(
        scene=SimpleNamespace(usd_stage_variant_sets=list(variant_sets))
    )
    return resolve_variant_plan(context, settings or _settings())


def _variant_set(name, variants, default_variant=None):
    return SimpleNamespace(
        name=name,
        variants=[SimpleNamespace(name=n, collection=c) for n, c in variants],
        default_variant=default_variant or variants[0][0],
    )


def _stage_with(*object_names, root="Root"):
    stage = Usd.Stage.CreateInMemory()
    root_prim = UsdGeom.Xform.Define(stage, f"/{root}")
    stage.SetDefaultPrim(root_prim.GetPrim())
    for name in object_names:
        UsdGeom.Xform.Define(stage, f"/{root}/{name}")
    return stage


def _seat_fixture():
    """A Seat set over two one-object collections, and its prim map."""
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan_for(
        _variant_set("Seat", [("Fabric", fabric), ("Leather", leather)])
    )
    stage = _stage_with("SeatFabric", "SeatLeather")
    object_map = {
        "/Root/SeatFabric": "SeatFabric",
        "/Root/SeatLeather": "SeatLeather",
    }
    return stage, plan, object_map


def _active_children(stage):
    return sorted(child.GetName() for child in stage.GetDefaultPrim().GetChildren())



def _reserve_and_author(stage, plan, object_map, path_edits=(), settings=None,
                        diagnostics=None):
    """What the pipeline does: reserve the finishes, then author everything."""
    reserve_variant_materials(stage, plan)
    return author_variants(
        stage, plan, object_map, path_edits, settings or _settings(), diagnostics
    )


# --------------------------------------------------------------------------
# Authoring
# --------------------------------------------------------------------------

def test_each_selection_activates_only_its_own_members():
    stage, plan, object_map = _seat_fixture()

    assert author_variants(stage, plan, object_map, (), _settings()) == ("Seat",)

    variant_set = stage.GetDefaultPrim().GetVariantSet("Seat")
    assert sorted(variant_set.GetVariantNames()) == ["Fabric", "Leather"]

    variant_set.SetVariantSelection("Fabric")
    assert _active_children(stage) == ["SeatFabric"]
    variant_set.SetVariantSelection("Leather")
    assert _active_children(stage) == ["SeatLeather"]


def test_the_declared_default_is_the_shipped_selection():
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan_for(
        _variant_set(
            "Seat", [("Fabric", fabric), ("Leather", leather)], default_variant="Leather"
        )
    )
    stage = _stage_with("SeatFabric", "SeatLeather")

    author_variants(
        stage,
        plan,
        {"/Root/SeatFabric": "SeatFabric", "/Root/SeatLeather": "SeatLeather"},
        (),
        _settings(),
    )

    assert stage.GetDefaultPrim().GetVariantSet("Seat").GetVariantSelection() == "Leather"
    assert _active_children(stage) == ["SeatLeather"]


def test_active_is_authored_only_inside_the_variants():
    """The whole design rests on nothing stronger opposing the variants."""
    stage, plan, object_map = _seat_fixture()

    author_variants(stage, plan, object_map, (), _settings())

    layer = stage.GetRootLayer()
    for name in ("SeatFabric", "SeatLeather"):
        spec = layer.GetPrimAtPath(f"/Root/{name}")
        assert spec is not None
        assert not spec.HasInfo("active"), (
            f"{name} has a base-layer active opinion, which would beat the "
            "variant opinions"
        )


def test_the_authored_stage_passes_the_shadowing_check():
    """A false positive here would refuse every variant export we write."""
    stage, plan, object_map = _seat_fixture()
    author_variants(stage, plan, object_map, (), _settings())

    report = RealityKitPreflightReport()
    _check_variant_activation_is_unopposed(stage, report)

    assert [issue.code for issue in report.issues] == []


def test_two_sets_are_authored_on_the_same_root_prim():
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    oak = _collection("Oak", objects=[_object("LegOak")])
    steel = _collection("Steel", objects=[_object("LegSteel")])
    plan = _plan_for(
        _variant_set("Seat", [("Fabric", fabric), ("Leather", leather)]),
        _variant_set("Legs", [("Oak", oak), ("Steel", steel)]),
    )
    stage = _stage_with("SeatFabric", "SeatLeather", "LegOak", "LegSteel")
    object_map = {f"/Root/{name}": name for name in
                  ("SeatFabric", "SeatLeather", "LegOak", "LegSteel")}

    authored = author_variants(stage, plan, object_map, (), _settings())

    assert sorted(authored) == ["Legs", "Seat"]
    root = stage.GetDefaultPrim()
    assert sorted(root.GetVariantSets().GetNames()) == ["Legs", "Seat"]
    root.GetVariantSet("Seat").SetVariantSelection("Leather")
    root.GetVariantSet("Legs").SetVariantSelection("Steel")
    assert _active_children(stage) == ["LegSteel", "SeatLeather"]


def test_a_member_renamed_by_normalization_still_resolves():
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.Xform.Define(stage, "/Root/SeatFabric")
    UsdGeom.Xform.Define(stage, "/Root/Chaise_Café")
    # The map was captured before this rename, as it is during a real export.
    object_map = {
        "/Root/SeatFabric": "SeatFabric",
        "/Root/Chaise_Café": "Chaise_Café",
    }
    path_edits = normalize_scene(stage, _settings())
    assert path_edits, "fixture must actually rename something"

    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("Chaise_Café")])
    plan = _plan_for(_variant_set("Seat", [("Fabric", fabric), ("Leather", leather)]))

    author_variants(stage, plan, object_map, path_edits, _settings())

    variant_set = stage.GetDefaultPrim().GetVariantSet("Seat")
    variant_set.SetVariantSelection("Leather")
    assert _active_children(stage) == ["Chaise_Caf_"]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_a_stage_without_a_default_prim_is_refused():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Root/SeatFabric")
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan_for(_variant_set("Seat", [("Fabric", fabric), ("Leather", leather)]))

    with pytest.raises(VariantAuthoringError, match="no default prim"):
        author_variants(stage, plan, {}, (), _settings())


def test_an_export_without_a_prim_map_is_refused_rather_than_guessed():
    stage, plan, _object_map = _seat_fixture()

    with pytest.raises(VariantAuthoringError, match="no prim map"):
        author_variants(stage, plan, None, (), _settings())


def test_one_empty_variant_beside_a_populated_one_is_refused():
    stage, plan, object_map = _seat_fixture()
    object_map.pop("/Root/SeatLeather")

    with pytest.raises(VariantAuthoringError, match="activates nothing"):
        author_variants(stage, plan, object_map, (), _settings())


def test_a_wholly_missing_set_is_refused_in_a_full_export():
    stage, plan, _object_map = _seat_fixture()

    with pytest.raises(VariantAuthoringError, match="no members"):
        author_variants(stage, plan, {}, (), _settings())


def test_a_wholly_missing_set_is_skipped_in_a_selected_only_export():
    """The selection simply did not include that set; that is not an error."""
    stage, plan, _object_map = _seat_fixture()
    diagnostics = ExportDiagnostics()

    authored = author_variants(
        stage, plan, {}, (), _settings(selected_objects_only=True), diagnostics
    )

    assert authored == ()
    assert not stage.GetDefaultPrim().GetVariantSets().GetNames()
    assert len(diagnostics.to_dict()["warnings"]) == 1


def test_a_member_parented_under_another_variants_member_is_refused():
    """Deactivating the parent would take the child with it."""
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.Xform.Define(stage, "/Root/Seat")
    UsdGeom.Xform.Define(stage, "/Root/Seat/Cushion")

    outer = _collection("Fabric", objects=[_object("Seat")])
    inner = _collection("Leather", objects=[_object("Cushion")])
    plan = _plan_for(_variant_set("Seat", [("Fabric", outer), ("Leather", inner)]))
    object_map = {"/Root/Seat": "Seat", "/Root/Seat/Cushion": "Cushion"}

    with pytest.raises(VariantAuthoringError, match="is a parent of"):
        author_variants(stage, plan, object_map, (), _settings())


def test_a_member_that_produced_no_prim_is_warned_about():
    fabric = _collection(
        "Fabric", objects=[_object("SeatFabric"), _object("SeatTrim")]
    )
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan_for(_variant_set("Seat", [("Fabric", fabric), ("Leather", leather)]))
    stage = _stage_with("SeatFabric", "SeatLeather")
    diagnostics = ExportDiagnostics()

    author_variants(
        stage,
        plan,
        {"/Root/SeatFabric": "SeatFabric", "/Root/SeatLeather": "SeatLeather"},
        (),
        _settings(),
        diagnostics,
    )

    warnings = diagnostics.to_dict()["warnings"]
    assert any("SeatTrim" in warning for warning in warnings)


# --------------------------------------------------------------------------
# The shadowing preflight check
# --------------------------------------------------------------------------

def test_an_active_opinion_outside_the_variants_is_refused():
    stage, plan, object_map = _seat_fixture()
    author_variants(stage, plan, object_map, (), _settings())
    # Exactly the trap: a local-layer opinion on the field the variants vary.
    stage.GetPrimAtPath("/Root/SeatLeather").SetActive(True)

    report = RealityKitPreflightReport()
    _check_variant_activation_is_unopposed(stage, report)

    assert [issue.code for issue in report.issues] == ["VARIANT_OPINION_SHADOWED"]
    assert report.issues[0].prim_path == "/Root/SeatLeather"


def test_a_stage_with_no_variants_reports_nothing():
    stage = _stage_with("SeatFabric")
    stage.GetPrimAtPath("/Root/SeatFabric").SetActive(False)

    report = RealityKitPreflightReport()
    _check_variant_activation_is_unopposed(stage, report)

    assert report.issues == []


# --------------------------------------------------------------------------
# The animation-library interaction
# --------------------------------------------------------------------------

def test_an_animation_inside_a_deactivated_member_is_warned_about():
    stage, plan, object_map = _seat_fixture()
    stage.DefinePrim("/Root/SeatLeather/Anim", "SkelAnimation")
    diagnostics = ExportDiagnostics()

    author_variants(
        stage,
        plan,
        object_map,
        (),
        _settings(export_animation=True, author_animation_library=True),
        diagnostics,
    )

    warnings = diagnostics.to_dict()["warnings"]
    assert any("SeatLeather" in warning and "animation" in warning.lower()
               for warning in warnings)


def test_no_animation_warning_without_the_clip_library():
    stage, plan, object_map = _seat_fixture()
    stage.DefinePrim("/Root/SeatLeather/Anim", "SkelAnimation")
    diagnostics = ExportDiagnostics()

    author_variants(
        stage,
        plan,
        object_map,
        (),
        _settings(export_animation=True, author_animation_library=False),
        diagnostics,
    )

    assert diagnostics.to_dict()["warnings"] == []


# --------------------------------------------------------------------------
# Material variant sets
# --------------------------------------------------------------------------



def _material_variant_set(name, variants, target, default_variant=None):
    return SimpleNamespace(
        name=name,
        kind="MATERIAL",
        variants=[
            SimpleNamespace(name=n, material=(None if m is None else SimpleNamespace(name=m)))
            for n, m in variants
        ],
        target_collection=target,
        default_variant=default_variant or variants[0][0],
    )


def _bound_stage(*material_names, bind="OakMat"):
    """A mesh bound to a material, the way Blender's exporter leaves it."""
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/Root")
    stage.SetDefaultPrim(root.GetPrim())
    top = UsdGeom.Mesh.Define(stage, "/Root/TableTop")
    for name in material_names:
        UsdShade.Material.Define(stage, f"/Root/_materials/{name}")
    if bind:
        UsdShade.MaterialBindingAPI.Apply(top.GetPrim())
        UsdShade.MaterialBindingAPI(top.GetPrim()).Bind(
            UsdShade.Material.Get(stage, f"/Root/_materials/{bind}")
        )
    return stage


def _finish_plan(target_objects=("TableTop",), materials=(("Oak", "OakMat"), ("Walnut", "WalnutMat"))):
    target = _collection("Tops", objects=[_object(n) for n in target_objects])
    return _plan_for(_material_variant_set("Finish", list(materials), target))


def _binding(stage, path="/Root/TableTop"):
    material = UsdShade.MaterialBindingAPI(
        stage.GetPrimAtPath(path)
    ).GetDirectBinding().GetMaterial()
    return material.GetPrim().GetName() if material else None


def test_each_finish_binds_its_own_material():
    stage = _bound_stage("OakMat", "WalnutMat")
    plan = _finish_plan()

    assert _reserve_and_author(stage, plan, {"/Root/TableTop": "TableTop"}, (), _settings()
    ) == ("Finish",)

    variant_set = stage.GetDefaultPrim().GetVariantSet("Finish")
    assert sorted(variant_set.GetVariantNames()) == ["Oak", "Walnut"]
    variant_set.SetVariantSelection("Oak")
    assert _binding(stage) == "OakMat"
    variant_set.SetVariantSelection("Walnut")
    assert _binding(stage) == "WalnutMat"


def test_the_base_binding_is_removed_so_the_variants_are_not_shadowed():
    """Blender writes a binding on every mesh, and it beats every variant."""
    stage = _bound_stage("OakMat", "WalnutMat")
    assert stage.GetRootLayer().GetPrimAtPath(
        "/Root/TableTop"
    ).relationships.get("material:binding") is not None

    _reserve_and_author(stage, _finish_plan(), {"/Root/TableTop": "TableTop"}, (), _settings()
    )

    assert stage.GetRootLayer().GetPrimAtPath(
        "/Root/TableTop"
    ).relationships.get("material:binding") is None


def test_an_authored_material_set_passes_the_shadowing_check():
    stage = _bound_stage("OakMat", "WalnutMat")
    _reserve_and_author(stage, _finish_plan(), {"/Root/TableTop": "TableTop"}, (), _settings()
    )

    report = RealityKitPreflightReport()
    _check_variant_activation_is_unopposed(stage, report)

    assert [issue.code for issue in report.issues] == []


def test_a_binding_left_outside_the_variants_is_refused():
    stage = _bound_stage("OakMat", "WalnutMat")
    _reserve_and_author(stage, _finish_plan(), {"/Root/TableTop": "TableTop"}, (), _settings()
    )
    # Put the shadowing opinion back, as an earlier pass might.
    UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/Root/TableTop")).Bind(
        UsdShade.Material.Get(stage, "/Root/_materials/OakMat")
    )

    report = RealityKitPreflightReport()
    _check_variant_activation_is_unopposed(stage, report)

    assert [issue.code for issue in report.issues] == ["VARIANT_OPINION_SHADOWED"]
    assert report.issues[0].details["varied_property"] == "material:binding"


def test_a_finish_no_object_wears_still_gets_a_material_prim():
    """Blender exports no prim for an unused material; this is that case."""
    stage = _bound_stage("OakMat")           # WalnutMat never exported
    _reserve_and_author(stage, _finish_plan(), {"/Root/TableTop": "TableTop"}, (), _settings()
    )

    assert stage.GetPrimAtPath("/Root/_materials/WalnutMat")
    variant_set = stage.GetDefaultPrim().GetVariantSet("Finish")
    variant_set.SetVariantSelection("Walnut")
    assert _binding(stage) == "WalnutMat"


def test_a_shape_set_and_a_finish_set_compose_together():
    """The combination the kinds exist for: four looks from two sets."""
    stage = _bound_stage("OakMat", "WalnutMat")
    UsdGeom.Mesh.Define(stage, "/Root/TableTopSquare")

    round_c = _collection("Round", objects=[_object("TableTop")])
    square_c = _collection("Square", objects=[_object("TableTopSquare")])
    tops = _collection("Tops", objects=[_object("TableTop"), _object("TableTopSquare")])
    plan = _plan_for(
        _variant_set("Shape", [("Round", round_c), ("Square", square_c)]),
        _material_variant_set("Finish", [("Oak", "OakMat"), ("Walnut", "WalnutMat")], tops),
    )
    object_map = {
        "/Root/TableTop": "TableTop",
        "/Root/TableTopSquare": "TableTopSquare",
    }

    _reserve_and_author(stage, plan, object_map, (), _settings())
    author_variants(stage, plan, object_map, (), _settings())

    root = stage.GetDefaultPrim()
    shape, finish = root.GetVariantSet("Shape"), root.GetVariantSet("Finish")
    seen = {}
    for shape_name in ("Round", "Square"):
        for finish_name in ("Oak", "Walnut"):
            shape.SetVariantSelection(shape_name)
            finish.SetVariantSelection(finish_name)
            active = [c.GetName() for c in root.GetChildren()
                      if c.GetName().startswith("TableTop")]
            assert len(active) == 1, active
            seen[(shape_name, finish_name)] = (active[0], _binding(stage, f"/Root/{active[0]}"))

    assert seen == {
        ("Round", "Oak"): ("TableTop", "OakMat"),
        ("Round", "Walnut"): ("TableTop", "WalnutMat"),
        ("Square", "Oak"): ("TableTopSquare", "OakMat"),
        ("Square", "Walnut"): ("TableTopSquare", "WalnutMat"),
    }


def test_material_variants_without_a_prim_map_are_refused():
    stage = _bound_stage("OakMat", "WalnutMat")
    with pytest.raises(VariantAuthoringError, match="no prim map"):
        _reserve_and_author(stage, _finish_plan(), None, (), _settings())


def test_a_finish_set_whose_targets_are_absent_is_skipped_in_a_selected_only_export():
    stage = _bound_stage("OakMat", "WalnutMat")
    diagnostics = ExportDiagnostics()

    authored = _reserve_and_author(stage, _finish_plan(), {}, (), _settings(selected_objects_only=True), diagnostics
    )

    assert authored == ()
    assert len(diagnostics.to_dict()["warnings"]) == 1


# --------------------------------------------------------------------------
# Nested finish sets — a finish offered on some shapes and not others
# --------------------------------------------------------------------------

def _nested_fixture():
    """Round offers Oak only; Square offers Oak and Walnut."""
    stage = _bound_stage("OakMat", "WalnutMat")
    UsdGeom.Mesh.Define(stage, "/Root/TableTopSquare")
    UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/Root/TableTopSquare"))
    UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/Root/TableTopSquare")).Bind(
        UsdShade.Material.Get(stage, "/Root/_materials/OakMat")
    )

    round_c = _collection("Round", objects=[_object("TableTop")])
    square_c = _collection("Square", objects=[_object("TableTopSquare")])
    tops = _collection("Tops", objects=[_object("TableTop"), _object("TableTopSquare")])

    finish = _material_variant_set(
        "Finish", [("Oak", "OakMat"), ("Walnut", "WalnutMat")], tops
    )
    finish.parent_set = "Shape"
    finish.variants[1].applies_to = "Square"   # walnut only on the square top

    plan = _plan_for(
        _variant_set("Shape", [("Round", round_c), ("Square", square_c)]),
        finish,
    )
    object_map = {
        "/Root/TableTop": "TableTop",
        "/Root/TableTopSquare": "TableTopSquare",
    }
    return stage, plan, object_map


def _finish_names(stage, shape_name):
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection(shape_name)
    return sorted(root.GetVariantSet("Finish").GetVariantNames())


def test_each_shape_offers_only_its_own_finishes():
    stage, plan, object_map = _nested_fixture()

    _reserve_and_author(stage, plan, object_map)

    assert _finish_names(stage, "Round") == ["Oak"]
    assert _finish_names(stage, "Square") == ["Oak", "Walnut"]


def test_a_nested_finish_still_rebinds_the_shape_that_is_present():
    stage, plan, object_map = _nested_fixture()

    _reserve_and_author(stage, plan, object_map)

    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection("Square")
    root.GetVariantSet("Finish").SetVariantSelection("Walnut")
    assert _binding(stage, "/Root/TableTopSquare") == "WalnutMat"

    root.GetVariantSet("Finish").SetVariantSelection("Oak")
    assert _binding(stage, "/Root/TableTopSquare") == "OakMat"


def test_a_shape_that_offers_one_finish_ships_with_it():
    stage, plan, object_map = _nested_fixture()

    _reserve_and_author(stage, plan, object_map)

    root = stage.GetDefaultPrim()
    root.GetVariantSet("Shape").SetVariantSelection("Round")
    assert root.GetVariantSet("Finish").GetVariantSelection() == "Oak"
    assert _binding(stage, "/Root/TableTop") == "OakMat"


def test_a_nested_set_leaves_no_binding_outside_its_variants():
    stage, plan, object_map = _nested_fixture()

    _reserve_and_author(stage, plan, object_map)

    for path in ("/Root/TableTop", "/Root/TableTopSquare"):
        spec = stage.GetRootLayer().GetPrimAtPath(path)
        assert spec.relationships.get("material:binding") is None, path


def test_a_nested_set_is_declared_inside_the_shape_variants():
    """What makes it nested is where it is declared, not what composes.

    A nested Finish still shows up on the composed root prim - scoped by the
    shape selected - but the root prim *spec* declares only Shape, and each
    Shape variant declares its own Finish.
    """
    stage, plan, object_map = _nested_fixture()

    _reserve_and_author(stage, plan, object_map)

    root_spec = stage.GetRootLayer().GetPrimAtPath("/Root")
    assert list(root_spec.variantSets.keys()) == ["Shape"]

    shape = root_spec.variantSets["Shape"]
    for name in ("Round", "Square"):
        nested = shape.variants[name].primSpec.variantSets
        assert list(nested.keys()) == ["Finish"], name
