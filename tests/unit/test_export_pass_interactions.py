"""How the late export passes behave together.

Each pass is tested on its own elsewhere. These are the mistakes that only
appear when they run in sequence - the kind that a pass reordering would
reintroduce without any single pass's tests noticing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd, UsdGeom  # noqa: E402

from Plugin.export.asset_metadata import author_asset_metadata  # noqa: E402
from Plugin.export.colliders import author_colliders, resolve_collider_plan  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.payload import split_asset_payload  # noqa: E402
from Plugin.export.variant_authoring import author_variants  # noqa: E402
from Plugin.export.variants import (  # noqa: E402
    require_valid_variant_plan,
    resolve_variant_plan,
)


def _settings(**overrides):
    values = {
        "export_variants": True,
        "export_armatures": True,
        "export_colliders": True,
        "selected_objects_only": False,
        "root_prim_kind": "COMPONENT",
        "payload_geometry": False,
        "filepath": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _physical(name):
    return SimpleNamespace(
        name=name, type="MESH", rigid_body=SimpleNamespace(collision_shape="BOX")
    )


def _collection(name, objects):
    return SimpleNamespace(name=name, objects=list(objects), children=[])


def _two_shape_stage(path=None):
    stage = Usd.Stage.CreateNew(str(path)) if path else Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    for name in ("Round", "Square"):
        UsdGeom.Xform.Define(stage, f"/root/{name}")
        cube = UsdGeom.Cube.Define(stage, f"/root/{name}/Mesh")
        cube.GetSizeAttr().Set(1.0)
        cube.GetExtentAttr().Set([(-0.5,) * 3, (0.5,) * 3])
    return stage


def _shape_context():
    objects = [_physical("Round"), _physical("Square")]
    return SimpleNamespace(
        scene=SimpleNamespace(
            objects=objects,
            usd_stage_variant_sets=[
                SimpleNamespace(
                    name="Shape",
                    kind="GEOMETRY",
                    default_variant="Round",
                    variants=[
                        SimpleNamespace(
                            name=name, collection=_collection(name, [obj])
                        )
                        for name, obj in zip(("Round", "Square"), objects)
                    ],
                )
            ],
        )
    )


_OBJECT_MAP = {"/root/Round": "Round", "/root/Square": "Square"}


def test_every_variant_member_gets_a_collider():
    """Colliders are authored before the variants, so none is skipped.

    Authored afterwards, a member outside the default selection has no
    composed prim and silently goes without one.
    """
    stage = _two_shape_stage()
    context, settings = _shape_context(), _settings()

    author_colliders(
        stage, resolve_collider_plan(context, settings), _OBJECT_MAP
    )
    author_variants(
        stage, resolve_variant_plan(context, settings), _OBJECT_MAP, (), settings
    )

    shape = stage.GetDefaultPrim().GetVariantSet("Shape")
    for selection in ("Round", "Square"):
        shape.SetVariantSelection(selection)
        assert stage.GetPrimAtPath(f"/root/{selection}/Collider"), selection


def test_a_variant_warning_is_reported_once():
    """Two passes resolve the plan; only the first validates it."""
    context = SimpleNamespace(
        scene=SimpleNamespace(
            usd_stage_variant_sets=[
                SimpleNamespace(
                    name="Seat",
                    kind="GEOMETRY",
                    default_variant="Fabric",
                    variants=[
                        SimpleNamespace(
                            name="Fabric",
                            collection=_collection("Fabric", [_physical("SeatFabric")]),
                        )
                    ],
                )
            ]
        )
    )
    diagnostics = ExportDiagnostics()
    plan = resolve_variant_plan(context, _settings())

    require_valid_variant_plan(plan, diagnostics)

    warnings = diagnostics.to_dict()["warnings"]
    assert len(warnings) == 1, warnings


def test_asset_metadata_survives_the_payload_split(tmp_path):
    """All three are read from the interface with the geometry unloaded."""
    path = tmp_path / "table.usda"
    stage = _two_shape_stage(path)
    settings = _settings(payload_geometry=True)

    author_asset_metadata(stage, path, settings)
    split_asset_payload(stage, path, settings)
    stage.GetRootLayer().Save()

    unloaded = Usd.Stage.Open(str(path), Usd.Stage.LoadNone)
    root = unloaded.GetDefaultPrim()

    assert Usd.ModelAPI(root).GetKind() == "component"
    assert sorted(root.GetAssetInfo()) == ["identifier", "name"]
    assert [list(point) for point in UsdGeom.ModelAPI(root).GetExtentsHint()] == [
        [-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]
    ]
    assert [child.GetName() for child in root.GetChildren()] == []


def test_colliders_move_into_the_payload_with_their_objects(tmp_path):
    path = tmp_path / "table.usda"
    stage = _two_shape_stage(path)
    context, settings = _shape_context(), _settings(payload_geometry=True)

    author_colliders(stage, resolve_collider_plan(context, settings), _OBJECT_MAP)
    split_asset_payload(stage, path, settings)
    stage.GetRootLayer().Save()

    loaded = Usd.Stage.Open(str(path))
    assert loaded.GetPrimAtPath("/root/Round/Collider")
