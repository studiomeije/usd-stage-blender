"""Integration test — an authored collider survives realitytool.

The unit tests check what gets written. This checks that Apple's compiler
turns it into something, which is the only claim that matters: a mesh with
UsdPhysics applied compiles to a .reality with no collision asset at all and
no complaint, so "it compiled" proves nothing on its own. See
docs/APPLE_CONFORMANCE.md.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
REALITYTOOL = Path(
    "/Applications/Xcode.app/Contents/Developer/usr/bin/realitytool"
)

pytest.importorskip("pxr")


def _authored_stage(path: Path):
    from pxr import Usd, UsdGeom

    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from Plugin.export.colliders import author_colliders, resolve_collider_plan

    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, "/Asset")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/Asset/Table")
    cube = UsdGeom.Cube.Define(stage, "/Asset/Table/Mesh")
    cube.GetSizeAttr().Set(1.0)
    cube.GetExtentAttr().Set([(-0.5,) * 3, (0.5,) * 3])

    context = SimpleNamespace(
        scene=SimpleNamespace(
            objects=[
                SimpleNamespace(
                    name="Table",
                    type="MESH",
                    rigid_body=SimpleNamespace(collision_shape="BOX"),
                )
            ]
        )
    )
    settings = SimpleNamespace(export_colliders=True, export_armatures=True)
    authored = author_colliders(
        stage, resolve_collider_plan(context, settings), {"/Asset/Table": "Table"}
    )
    stage.GetRootLayer().Save()
    return authored


@pytest.fixture(scope="module")
def compiled(tmp_path_factory) -> dict:
    if not REALITYTOOL.exists():
        pytest.skip("realitytool not installed")

    workdir = tmp_path_factory.mktemp("collider")
    source = workdir / "table.usda"
    authored = _authored_stage(source)

    bundle = workdir / "table.rkassets"
    bundle.mkdir()
    (bundle / source.name).write_bytes(source.read_bytes())
    output = workdir / "table.reality"

    result = subprocess.run(
        [
            str(REALITYTOOL), "compile",
            "--platform", "xros", "--deployment-target", "27.0",
            "-o", str(output), str(bundle),
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert output.exists(), result.stdout + result.stderr

    with zipfile.ZipFile(output) as archive:
        asset_map = json.loads(archive.read("assetMap.json"))
    return {"authored": authored, "asset_map": asset_map, "source": source}


def test_the_collider_was_authored(compiled):
    assert compiled["authored"] == ("/Asset/Table/Collider",)


def test_it_compiles_into_a_collision_shape(compiled):
    """UsdPhysics compiles into nothing; this is why we author components."""
    types = {asset.get("typeName") for asset in compiled["asset_map"]["assets"]}

    assert "CollisionShape" in types, (
        f"realitytool produced no collision asset, only {sorted(types)}"
    )


def test_the_written_component_matches_reality_composer_pros_spelling(compiled):
    text = compiled["source"].read_text()

    assert 'def RealityKitComponent "Collider"' in text
    assert 'uniform token info:id = "RealityKit.Collider"' in text
    assert 'def RealityKitStruct "Shape"' in text
    assert 'token shapeType = "Box"' in text
    assert "float3 extent = (1, 1, 1)" in text
