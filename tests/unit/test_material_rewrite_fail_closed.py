"""Fatal and transactional material rewrite boundary regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export import postprocess_usd  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.materials import rewrite as material_rewrite  # noqa: E402


def _flat_material(name: str, color=(0.2, 0.3, 0.4)):
    return SimpleNamespace(
        name=name,
        node_tree=None,
        surface_render_method="DITHERED",
        color=list(color),
    )


@pytest.fixture(autouse=True)
def _flat_emission_extraction(monkeypatch):
    """Stand-in materials extract as flat Emission colours, without a node tree."""
    def extract(material):
        return {
            "name": material.name,
            "type": "emission",
            "base_color": material.color,
            "alpha": 1.0,
        }

    monkeypatch.setattr(material_rewrite, "extract_blender_material_data", extract)


def _settings():
    return SimpleNamespace(
        force_unlit_materials=False,
    )


def _context(*materials):
    return SimpleNamespace(blend_data=SimpleNamespace(materials=list(materials)))


def _stage_with_bound_materials(*names):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    for name in names:
        mesh = UsdGeom.Mesh.Define(stage, f"/Root/{name}Mesh")
        material = UsdShade.Material.Define(stage, f"/Root/Looks/{name}")
        material.GetPrim().CreateAttribute(
            "userProperties:blender:data_name",
            Sdf.ValueTypeNames.String,
        ).Set(name)
        preview = UsdShade.Shader.Define(
            stage,
            f"/Root/Looks/{name}/PreviewSurface",
        )
        preview.CreateIdAttr("UsdPreviewSurface")
        material.CreateSurfaceOutput().ConnectToSource(
            preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        )
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return stage


def test_graph_unresolved_and_missing_mapping_failures_aggregate_before_authoring(
    monkeypatch,
):
    stage = _stage_with_bound_materials("GraphBad", "Unresolved", "Missing")
    before = stage.GetRootLayer().ExportToString()
    extracted = []

    def extract(material):
        extracted.append(material.name)
        if material.name == "Unresolved":
            return {
                "name": material.name,
                "type": "emission",
                "unresolved_warnings": ["linked input requires baking"],
            }
        if material.name == "UnusedBroken":
            raise AssertionError("unused material must not be extracted")
        return {
            "name": material.name,
            "type": "emission",
            "base_color": [0.2, 0.3, 0.4],
            "alpha": 1.0,
        }

    real_build = material_rewrite.MaterialXGraphBuilder.build_unlit_material

    def build(self, material_data):
        if material_data["name"] == "GraphBad":
            raise ValueError("injected graph failure")
        return real_build(self, material_data)

    monkeypatch.setattr(material_rewrite, "extract_blender_material_data", extract)
    monkeypatch.setattr(
        material_rewrite.MaterialXGraphBuilder,
        "build_unlit_material",
        build,
    )
    authored = []
    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        lambda *args, **kwargs: authored.append((args, kwargs)),
    )
    diagnostics = ExportDiagnostics()

    with pytest.raises(RuntimeError) as caught:
        material_rewrite.rewrite_materials(
            stage,
            _settings(),
            _context(
                _flat_material("GraphBad"),
                _flat_material("Unresolved"),
                _flat_material("UnusedBroken"),
            ),
            diagnostics,
        )

    message = str(caught.value)
    assert "3 used material(s)" in message
    assert "GraphBad" in message and "injected graph failure" in message
    assert "Unresolved" in message and "linked input requires baking" in message
    assert "Missing" in message and "No Blender material mapping" in message
    assert set(extracted) == {"GraphBad", "Unresolved"}
    assert authored == []
    assert stage.GetRootLayer().ExportToString() == before
    assert diagnostics.data["materials"]["failed"] == 3
    assert diagnostics.data["materials"]["converted"] == 0


def test_author_failure_without_diagnostics_is_fatal_and_rolled_back(monkeypatch):
    stage = _stage_with_bound_materials("Only")
    before = stage.GetRootLayer().ExportToString()
    real_create = material_rewrite.create_materialx_material

    def fail_after_authoring(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("injected author failure")

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        fail_after_authoring,
    )

    with pytest.raises(RuntimeError, match="injected author failure"):
        material_rewrite.rewrite_materials(
            stage,
            _settings(),
            _context(_flat_material("Only")),
            None,
        )

    assert stage.GetRootLayer().ExportToString() == before
    material = UsdShade.Material(stage.GetPrimAtPath("/Root/Looks/Only"))
    assert not material.GetSurfaceOutput("mtlx")


def test_late_author_failure_rolls_back_earlier_material_atomically(monkeypatch):
    stage = _stage_with_bound_materials("First", "Second")
    before = stage.GetRootLayer().ExportToString()
    real_create = material_rewrite.create_materialx_material
    calls = []

    def fail_second_after_authoring(stage, material_path, *args, **kwargs):
        calls.append(material_path)
        material = real_create(stage, material_path, *args, **kwargs)
        if material_path.endswith("/Second"):
            raise RuntimeError("late author failure")
        return material

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        fail_second_after_authoring,
    )
    diagnostics = ExportDiagnostics()

    with pytest.raises(RuntimeError, match="late author failure"):
        material_rewrite.rewrite_materials(
            stage,
            _settings(),
            _context(
                _flat_material("First"),
                _flat_material("Second"),
            ),
            diagnostics,
        )

    assert calls == ["/Root/Looks/First", "/Root/Looks/Second"]
    assert stage.GetRootLayer().ExportToString() == before
    for name in ("First", "Second"):
        material = UsdShade.Material(stage.GetPrimAtPath(f"/Root/Looks/{name}"))
        assert not material.GetSurfaceOutput("mtlx")
    assert diagnostics.data["materials"]["converted"] == 0
    assert diagnostics.data["materials"]["failed"] == 1


def test_postprocess_stops_before_finalize_and_save_on_author_failure(
    tmp_path,
    monkeypatch,
):
    asset_path = tmp_path / "scene.usda"
    source_stage = Usd.Stage.CreateNew(str(asset_path))
    root = source_stage.DefinePrim("/Root", "Xform")
    source_stage.SetDefaultPrim(root)
    mesh = UsdGeom.Mesh.Define(source_stage, "/Root/Mesh")
    material = UsdShade.Material.Define(source_stage, "/Root/Looks/Only")
    material.GetPrim().CreateAttribute(
        "userProperties:blender:data_name",
        Sdf.ValueTypeNames.String,
    ).Set("Only")
    preview = UsdShade.Shader.Define(
        source_stage,
        "/Root/Looks/Only/PreviewSurface",
    )
    preview.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    source_stage.GetRootLayer().Save()
    before = asset_path.read_text()

    phases = []
    monkeypatch.setattr(
        postprocess_usd,
        "begin_image_staging_session",
        lambda *_args: phases.append("begin"),
    )
    monkeypatch.setattr(
        postprocess_usd,
        "cleanup_image_staging_session",
        lambda *_args: phases.append("cleanup"),
    )

    def prepare(*_args, **_kwargs):
        phases.append("prepare")
        return frozenset()

    monkeypatch.setattr(postprocess_usd, "_prepare_assets", prepare)
    monkeypatch.setattr(
        postprocess_usd,
        "_normalize_localized_scene",
        lambda *_args: phases.append("normalize"),
    )
    monkeypatch.setattr(
        postprocess_usd,
        "author_animation_library",
        lambda *_args: phases.append("animation"),
    )
    monkeypatch.setattr(
        postprocess_usd,
        "_require_realitykit_preflight",
        lambda *_args: phases.append("preflight"),
    )
    real_create = material_rewrite.create_materialx_material

    def fail_after_authoring(*args, **kwargs):
        real_create(*args, **kwargs)
        raise RuntimeError("publication boundary failure")

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        fail_after_authoring,
    )

    with pytest.raises(RuntimeError, match="publication boundary failure"):
        postprocess_usd.process_usd_stage(
            str(asset_path),
            _settings(),
            _context(_flat_material("Only")),
            ExportDiagnostics(),
        )

    assert phases == ["begin", "prepare", "normalize", "cleanup"]
    assert asset_path.read_text() == before
    reopened = Usd.Stage.Open(str(asset_path))
    rewritten = UsdShade.Material(reopened.GetPrimAtPath("/Root/Looks/Only"))
    assert not rewritten.GetSurfaceOutput("mtlx")
