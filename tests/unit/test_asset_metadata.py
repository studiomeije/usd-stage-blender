"""The root-prim metadata that makes an export an asset rather than a layer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom  # noqa: E402

from Plugin.export.asset_metadata import author_asset_metadata  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402


def _settings(**overrides):
    values = {"root_prim_kind": "COMPONENT"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _stage(with_geometry=True, root="Root"):
    stage = Usd.Stage.CreateInMemory()
    root_prim = UsdGeom.Xform.Define(stage, f"/{root}")
    stage.SetDefaultPrim(root_prim.GetPrim())
    if with_geometry:
        cube = UsdGeom.Cube.Define(stage, f"/{root}/Cube")
        cube.GetSizeAttr().Set(2.0)
        cube.GetExtentAttr().Set([(-1, -1, -1), (1, 1, 1)])
    return stage


def test_the_root_prim_becomes_a_component_model():
    stage = _stage()

    authored = author_asset_metadata(stage, "/out/side_table.usdz", _settings())

    root = stage.GetDefaultPrim()
    assert authored["kind"] == "component"
    assert Usd.ModelAPI(root).GetKind() == "component"
    assert root.IsModel()


@pytest.mark.parametrize(
    "kind,token",
    [("GROUP", "group"), ("ASSEMBLY", "assembly")],
)
def test_the_other_kinds_are_authored_as_asked(kind, token):
    stage = _stage()

    author_asset_metadata(stage, "/out/scene.usdc", _settings(root_prim_kind=kind))

    assert Usd.ModelAPI(stage.GetDefaultPrim()).GetKind() == token


def test_asset_info_names_the_file_it_ships_as():
    stage = _stage()

    author_asset_metadata(stage, "/somewhere/else/side_table.usdz", _settings())

    info = stage.GetDefaultPrim().GetAssetInfo()
    assert info["name"] == "side_table"
    assert str(info["identifier"].path) == "side_table.usdz"


def test_the_bounds_hint_matches_the_geometry():
    stage = _stage()

    authored = author_asset_metadata(stage, "/out/side_table.usdz", _settings())

    assert authored["extents_hint"] == [[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]]
    hint = UsdGeom.ModelAPI(stage.GetDefaultPrim()).GetExtentsHint()
    assert [list(point) for point in hint] == [[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]]


def test_the_hint_describes_the_selection_the_asset_ships_with():
    """A variant asset has one size per selection; the hint is the shipped one."""
    stage = _stage(with_geometry=False)
    root = stage.GetDefaultPrim()
    for name, size in (("Small", 1.0), ("Large", 4.0)):
        cube = UsdGeom.Cube.Define(stage, f"/Root/{name}")
        cube.GetSizeAttr().Set(size)
        half = size / 2.0
        cube.GetExtentAttr().Set([(-half,) * 3, (half,) * 3])
    variants = root.GetVariantSets().AddVariantSet("size")
    for name in ("Small", "Large"):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            for other in ("Small", "Large"):
                stage.GetPrimAtPath(f"/Root/{other}").SetActive(other == name)
    variants.SetVariantSelection("Small")

    authored = author_asset_metadata(stage, "/out/thing.usdz", _settings())

    assert authored["extents_hint"] == [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]]


def test_none_leaves_the_stage_untouched():
    """For a pipeline that assigns model hierarchy itself."""
    stage = _stage()

    assert author_asset_metadata(stage, "/out/x.usdz", _settings(root_prim_kind="NONE")) == {}

    root = stage.GetDefaultPrim()
    assert not Usd.ModelAPI(root).GetKind()
    assert not root.GetAssetInfo()


def test_an_empty_asset_warns_instead_of_claiming_bounds():
    stage = _stage(with_geometry=False)
    diagnostics = ExportDiagnostics()

    authored = author_asset_metadata(stage, "/out/x.usdz", _settings(), diagnostics)

    assert "extents_hint" not in authored
    assert authored["kind"] == "component"
    assert any("extentsHint" in w for w in diagnostics.to_dict()["warnings"])


def test_what_was_authored_reaches_the_sidecar():
    stage = _stage()
    diagnostics = ExportDiagnostics()

    author_asset_metadata(stage, "/out/side_table.usdz", _settings(), diagnostics)

    recorded = diagnostics.to_dict()["asset_metadata"]
    assert recorded["kind"] == "component"
    assert recorded["asset_name"] == "side_table"


def test_a_stage_without_a_default_prim_is_left_alone():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/Root")

    assert author_asset_metadata(stage, "/out/x.usdz", _settings()) == {}


def test_the_identifier_names_the_package_not_the_layer_inside_it():
    """A USDZ export is referenced as the .usdz, not as its inner .usdc."""
    stage = _stage()

    author_asset_metadata(
        stage,
        "/staging/attempt-1/side_table.usdc",
        _settings(filepath="/Users/me/Deliver/side_table.usdz"),
    )

    info = stage.GetDefaultPrim().GetAssetInfo()
    assert str(info["identifier"].path) == "side_table.usdz"
    assert info["name"] == "side_table"


def test_without_a_requested_path_the_staged_name_is_used():
    stage = _stage()

    author_asset_metadata(stage, "/staging/side_table.usdc", _settings())

    assert str(
        stage.GetDefaultPrim().GetAssetInfo()["identifier"].path
    ) == "side_table.usdc"
