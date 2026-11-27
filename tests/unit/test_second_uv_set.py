"""The second UV set is st1, where RealityKit's texcoord index 1 reads it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export.realitykit_preflight import validate_stage  # noqa: E402
from Plugin.export.uv_sets import rename_second_uv_sets  # noqa: E402
from tests.unit.test_realitykit_preflight import _codes  # noqa: E402


def _stage(tmp_path, uv_names):
    path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    for name in uv_names:
        primvar = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
            name, Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
        )
        primvar.Set([(0, 0), (1, 0), (0, 1)])
        primvar.SetIndices([0, 1, 2])
    stage.Save()
    return stage, mesh, str(path)


def test_the_non_render_uv_set_is_renamed_st1(tmp_path):
    stage, mesh, path = _stage(tmp_path, ["st", "Lightmap"])

    renamed = rename_second_uv_sets(stage, [path])

    assert renamed == [("/Root/Mesh", "Lightmap")]
    primvars = UsdGeom.PrimvarsAPI(mesh)
    assert primvars.GetPrimvar("st1").GetIndices() == [0, 1, 2]
    assert not primvars.GetPrimvar("Lightmap")


@pytest.mark.parametrize("names", [["st"], ["st", "st1"], ["st", "a", "b"]])
def test_nothing_else_is_renamed(tmp_path, names):
    stage, _mesh, path = _stage(tmp_path, names)
    assert rename_second_uv_sets(stage, [path]) == []


def test_a_layer_the_export_does_not_own_is_left_alone(tmp_path):
    stage, mesh, _path = _stage(tmp_path, ["st", "Lightmap"])
    assert rename_second_uv_sets(stage, []) == []
    assert UsdGeom.PrimvarsAPI(mesh).GetPrimvar("Lightmap")


@pytest.mark.parametrize(
    "names,require,code,severity",
    [
        (["st"], False, "LIGHTMAP_UV_MISSING", "info"),
        (["st"], True, "LIGHTMAP_UV_MISSING", "error"),
        (["st", "st1"], True, "LIGHTMAP_UV_PRESENT", "info"),
        (["st", "Lightmap"], False, "SECOND_UV_SET_NOT_ST1", "warning"),
        (["st", "Lightmap"], True, "SECOND_UV_SET_NOT_ST1", "error"),
    ],
)
def test_require_lightmap_uv_promotes_the_reports(tmp_path, names, require, code, severity):
    stage, _mesh, _path = _stage(tmp_path, names)
    report = validate_stage(stage, settings=SimpleNamespace(require_lightmap_uv=require))
    issue = next(i for i in report.issues if i.code == code)
    assert issue.severity == severity


def test_a_material_reading_texcoord_one_needs_st1(tmp_path):
    stage, mesh, _path = _stage(tmp_path, ["st"])
    material = UsdShade.Material.Define(stage, "/Root/Material")
    reader = UsdShade.Shader.Define(stage, "/Root/Material/UV1")
    reader.CreateIdAttr("ND_texcoord_vector2")
    reader.CreateInput("index", Sdf.ValueTypeNames.Int).Set(1)
    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("ND_realitykit_unlit_surfaceshader")
    surface.CreateInput("color", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        reader.CreateOutput("out", Sdf.ValueTypeNames.Float2)
    )
    material.CreateSurfaceOutput("mtlx").ConnectToSource(surface.ConnectableAPI(), "out")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    assert "SECOND_UV_SET_MISSING" in _codes(validate_stage(stage))


@pytest.mark.parametrize("kind,warned", [("float", True), ("vector3", True), ("vector2", False)])
def test_a_primvar_read_realitykit_does_not_do_warns(tmp_path, kind, warned):
    stage, _mesh, _path = _stage(tmp_path, ["st"])
    UsdShade.Material.Define(stage, "/Root/Material")
    reader = UsdShade.Shader.Define(stage, "/Root/Material/Read")
    reader.CreateIdAttr(f"ND_geompropvalue_{kind}")
    reader.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set("blenderObjectRandom")

    report = validate_stage(stage)
    issues = [i for i in report.issues if i.code == "GEOMPROPVALUE_TYPE_NOT_READ"]
    assert bool(issues) is warned
    assert all(i.severity == "warning" for i in issues)
