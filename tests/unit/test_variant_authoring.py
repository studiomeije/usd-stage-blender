"""Authoring declared variant sets onto an exported stage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.realitykit_preflight import (  # noqa: E402
    _check_variant_activation_is_unopposed,
    RealityKitPreflightReport,
)
from Plugin.export.usd_scene import normalize_scene  # noqa: E402
from Plugin.export.variant_authoring import (  # noqa: E402
    VariantAuthoringError,
    author_variant_sets,
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


# --------------------------------------------------------------------------
# Authoring
# --------------------------------------------------------------------------

def test_each_selection_activates_only_its_own_members():
    stage, plan, object_map = _seat_fixture()

    assert author_variant_sets(stage, plan, object_map, (), _settings()) == ("Seat",)

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

    author_variant_sets(
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

    author_variant_sets(stage, plan, object_map, (), _settings())

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
    author_variant_sets(stage, plan, object_map, (), _settings())

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

    authored = author_variant_sets(stage, plan, object_map, (), _settings())

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

    author_variant_sets(stage, plan, object_map, path_edits, _settings())

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
        author_variant_sets(stage, plan, {}, (), _settings())


def test_an_export_without_a_prim_map_is_refused_rather_than_guessed():
    stage, plan, _object_map = _seat_fixture()

    with pytest.raises(VariantAuthoringError, match="no prim map"):
        author_variant_sets(stage, plan, None, (), _settings())


def test_one_empty_variant_beside_a_populated_one_is_refused():
    stage, plan, object_map = _seat_fixture()
    object_map.pop("/Root/SeatLeather")

    with pytest.raises(VariantAuthoringError, match="activates nothing"):
        author_variant_sets(stage, plan, object_map, (), _settings())


def test_a_wholly_missing_set_is_refused_in_a_full_export():
    stage, plan, _object_map = _seat_fixture()

    with pytest.raises(VariantAuthoringError, match="no members"):
        author_variant_sets(stage, plan, {}, (), _settings())


def test_a_wholly_missing_set_is_skipped_in_a_selected_only_export():
    """The selection simply did not include that set; that is not an error."""
    stage, plan, _object_map = _seat_fixture()
    diagnostics = ExportDiagnostics()

    authored = author_variant_sets(
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
        author_variant_sets(stage, plan, object_map, (), _settings())


def test_a_member_that_produced_no_prim_is_warned_about():
    fabric = _collection(
        "Fabric", objects=[_object("SeatFabric"), _object("SeatTrim")]
    )
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan_for(_variant_set("Seat", [("Fabric", fabric), ("Leather", leather)]))
    stage = _stage_with("SeatFabric", "SeatLeather")
    diagnostics = ExportDiagnostics()

    author_variant_sets(
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
    author_variant_sets(stage, plan, object_map, (), _settings())
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

    author_variant_sets(
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

    author_variant_sets(
        stage,
        plan,
        object_map,
        (),
        _settings(export_animation=True, author_animation_library=False),
        diagnostics,
    )

    assert diagnostics.to_dict()["warnings"] == []
