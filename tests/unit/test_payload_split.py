"""Splitting a finished export into an asset interface and a payload."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.payload import split_asset_payload  # noqa: E402


def _settings(**overrides):
    values = {"payload_geometry": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _stage(path, with_variants=False):
    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    Usd.ModelAPI(root.GetPrim()).SetKind("component")

    for name, size in (("Small", 1.0), ("Large", 2.0)):
        cube = UsdGeom.Cube.Define(stage, f"/root/{name}")
        cube.GetSizeAttr().Set(size)
        half = size / 2.0
        cube.GetExtentAttr().Set([(-half,) * 3, (half,) * 3])

    if with_variants:
        variants = root.GetPrim().GetVariantSets().AddVariantSet("size")
        for name in ("Small", "Large"):
            variants.AddVariant(name)
            variants.SetVariantSelection(name)
            with variants.GetVariantEditContext():
                for other in ("Small", "Large"):
                    stage.GetPrimAtPath(f"/root/{other}").SetActive(other == name)
        variants.SetVariantSelection("Small")
    return stage


def _split(tmp_path, **kwargs):
    path = tmp_path / "table.usda"
    stage = _stage(path, **kwargs)
    written = split_asset_payload(stage, path, _settings())
    stage.GetRootLayer().Save()
    return path, written


def test_the_setting_leaves_the_export_whole_by_default(tmp_path):
    path = tmp_path / "table.usda"
    stage = _stage(path)

    assert split_asset_payload(stage, path, _settings(payload_geometry=False)) == {}
    assert not (tmp_path / "assets").exists()


def test_the_geometry_moves_into_a_payload_layer(tmp_path):
    path, written = _split(tmp_path)

    assert written["payload_path"] == "./assets/table_payload.usda"
    assert (tmp_path / "assets" / "table_payload.usda").exists()


def test_the_interface_keeps_only_the_root_prim(tmp_path):
    path, _ = _split(tmp_path)

    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(str(path))
    root = layer.GetPrimAtPath("/root")
    assert list(root.nameChildren.keys()) == []
    assert root.payloadList.explicitItems


def test_composition_is_unchanged_once_loaded(tmp_path):
    path, _ = _split(tmp_path)

    stage = Usd.Stage.Open(str(path))
    children = sorted(c.GetName() for c in stage.GetDefaultPrim().GetChildren())
    assert children == ["Large", "Small"]


def test_the_asset_can_be_read_without_loading_its_geometry(tmp_path):
    """The whole point: identity and size without the weight."""
    path, _ = _split(tmp_path)

    stage = Usd.Stage.Open(str(path), Usd.Stage.LoadNone)
    root = stage.GetDefaultPrim()

    assert [c.GetName() for c in root.GetChildren()] == []
    assert Usd.ModelAPI(root).GetKind() == "component"


def test_a_variant_can_be_chosen_while_the_payload_is_unloaded(tmp_path):
    """Why the guidelines put variant sets on the root prim."""
    path, _ = _split(tmp_path, with_variants=True)

    stage = Usd.Stage.Open(str(path), Usd.Stage.LoadNone)
    variants = stage.GetDefaultPrim().GetVariantSet("size")

    assert sorted(variants.GetVariantNames()) == ["Large", "Small"]
    variants.SetVariantSelection("Large")
    assert variants.GetVariantSelection() == "Large"


def test_the_payload_does_not_carry_a_second_copy_of_the_variants(tmp_path):
    """Two layers authoring one selection would fight over it."""
    _split(tmp_path, with_variants=True)

    from pxr import Sdf

    payload = Sdf.Layer.FindOrOpen(str(tmp_path / "assets" / "table_payload.usda"))
    root = payload.GetPrimAtPath("/root")
    assert list(root.variantSets.keys()) == []
    assert sorted(root.nameChildren.keys()) == ["Large", "Small"]


def test_the_variants_still_switch_after_the_split(tmp_path):
    path, _ = _split(tmp_path, with_variants=True)

    stage = Usd.Stage.Open(str(path))
    root = stage.GetDefaultPrim()
    variants = root.GetVariantSet("size")

    seen = {}
    for name in ("Small", "Large"):
        variants.SetVariantSelection(name)
        seen[name] = sorted(c.GetName() for c in root.GetChildren())
    assert seen == {"Small": ["Small"], "Large": ["Large"]}


def test_the_payload_stands_alone(tmp_path):
    """Opened directly it still knows its axis, scale and default prim."""
    _split(tmp_path)

    payload = Usd.Stage.Open(str(tmp_path / "assets" / "table_payload.usda"))
    assert UsdGeom.GetStageUpAxis(payload) == UsdGeom.Tokens.y
    assert UsdGeom.GetStageMetersPerUnit(payload) == 1.0
    assert payload.GetDefaultPrim().GetName() == "root"


def test_an_empty_asset_is_left_whole_and_said_so(tmp_path):
    path = tmp_path / "empty.usda"
    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())
    diagnostics = ExportDiagnostics()

    assert split_asset_payload(stage, path, _settings(), diagnostics) == {}
    assert any("payload" in w for w in diagnostics.to_dict()["warnings"])


def test_what_was_written_reaches_the_sidecar(tmp_path):
    path = tmp_path / "table.usda"
    stage = _stage(path)
    diagnostics = ExportDiagnostics()

    split_asset_payload(stage, path, _settings(), diagnostics)

    assert diagnostics.to_dict()["payload_split"]["payload_layer"] == (
        "table_payload.usda"
    )
