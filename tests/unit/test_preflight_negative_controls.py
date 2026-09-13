"""The preflight is the only thing enforcing the Apple spatial contract.

Measured with the real Apple toolchain (Apple USD Tools 0.25.11, Xcode 27.0):
neither `usdchecker --arkit --strict` nor `realitytool compile` rejects a stage
that violates the contract this exporter exists to guarantee.

    stage             usdchecker --arkit --strict   realitytool   RealityKit load
    correct (Y-up)    PASS                          exit 0        loads
    upAxis = "Z"      PASS                          exit 0        loads
    Camera prim added PASS                          exit 0        loads

The compiled `.reality` from the Z-up stage was even byte-size identical to the
correct one. So Y-up, metersPerUnit == 1, doubleSided == false and the
unsupported-schema policy are guaranteed by `realitykit_preflight.py` alone,
with nothing external corroborating it.

These are negative controls: each asserts the preflight *rejects* a specific
violation. Without them, a regression that weakened or disabled the gate would
leave every downstream Apple check green — 63/63 compiles and 28/28 RealityKit
loads passed in the measurement above while the asset was wrongly oriented.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: E402

from Plugin.export.realitykit_preflight import validate_stage  # noqa: E402


def _contract_compliant_stage():
    """A minimal stage that the preflight accepts, to isolate each violation."""
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    mesh.CreatePointsAttr([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDoubleSidedAttr(False)
    uv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
    )
    uv.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)])
    return stage, mesh


def _error_codes(report):
    return {issue.code for issue in report.issues if issue.severity == "error"}


def test_the_baseline_stage_is_accepted():
    """Guards the controls below: a failure here means the fixture drifted."""
    stage, _mesh = _contract_compliant_stage()

    assert _error_codes(validate_stage(stage)) == set()


def test_z_up_stage_is_rejected():
    """usdchecker --arkit --strict passes this. Nothing else catches it."""
    stage, _mesh = _contract_compliant_stage()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)

    assert "UP_AXIS_NOT_Y" in _error_codes(validate_stage(stage))


@pytest.mark.parametrize("meters_per_unit", [0.01, 0.001, 100.0])
def test_non_metre_scene_scale_is_rejected(meters_per_unit):
    stage, _mesh = _contract_compliant_stage()
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)

    assert "METERS_PER_UNIT_NOT_ONE" in _error_codes(validate_stage(stage))


def test_double_sided_geometry_is_rejected():
    stage, mesh = _contract_compliant_stage()
    mesh.CreateDoubleSidedAttr(True)

    assert "DOUBLE_SIDED_GEOMETRY" in _error_codes(validate_stage(stage))


@pytest.mark.parametrize("prim_type", ["Camera", "SphereLight", "DistantLight"])
def test_unsupported_prim_types_are_rejected(prim_type):
    """A Camera passes usdchecker --arkit --strict and compiles cleanly."""
    stage, _mesh = _contract_compliant_stage()
    stage.DefinePrim("/Root/Unsupported", prim_type)

    assert "UNSUPPORTED_REALITYKIT_PRIM_TYPE" in _error_codes(validate_stage(stage))


def test_each_violation_is_reported_independently():
    """Several violations at once must not mask one another."""
    stage, mesh = _contract_compliant_stage()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    mesh.CreateDoubleSidedAttr(True)
    stage.DefinePrim("/Root/Cam", "Camera")

    codes = _error_codes(validate_stage(stage))

    assert {
        "UP_AXIS_NOT_Y",
        "METERS_PER_UNIT_NOT_ONE",
        "DOUBLE_SIDED_GEOMETRY",
        "UNSUPPORTED_REALITYKIT_PRIM_TYPE",
    } <= codes
