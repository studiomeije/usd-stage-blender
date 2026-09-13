"""Profile propagation and early export-validation regressions."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from Plugin.api.commands import export as export_command
from Plugin.api.commands import validate as validate_command
from Plugin.api.errors import CommandError
from Plugin.export import support_bundle


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _bpy_module(*, context, filepath: str = "") -> ModuleType:
    module = ModuleType("bpy")
    module.context = context
    module.data = SimpleNamespace(filepath=filepath, materials={})
    return module


def _nodes_package(validate_module) -> ModuleType:
    package = ModuleType("Plugin.nodes")
    package.validate = validate_module
    return package


def _settings(**overrides):
    values = {
        "bl_rna": SimpleNamespace(properties=[]),
        "diagnostics_enabled": False,
        "export_format": "USDZ",
        "filepath": "",
        "clamp_specular_tint": False,
        "selected_objects_only": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validate_command_normalization_policy_is_fail_closed():
    settings = _settings(clamp_specular_tint=False)

    assert validate_command._resolve_normalization_policy({}, settings) is False
    assert validate_command._resolve_normalization_policy(
        {"clamp_specular_tint": "false"},
        settings,
    ) is False
    assert validate_command._resolve_normalization_policy(
        {"clamp_specular_tint": True},
        settings,
    ) is True
    with pytest.raises(CommandError, match="Invalid clamp_specular_tint"):
        validate_command._resolve_normalization_policy(
            {"clamp_specular_tint": "maybe"},
            settings,
        )


class _FakeDiagnostics:
    def __init__(self):
        self.data = {}
        self.export_context = {}

    def set_export_context(self, **values):
        self.export_context.update(values)

    def set_environment(self, **_values):
        return None

    def add_validation_issue(self, *_args, **_kwargs):
        return None

    def begin_phase(self, *_args, **_kwargs):
        return None

    def end_phase(self, *_args, **_kwargs):
        return None

    def add_generated_file(self, *_args, **_kwargs):
        return None

    def set_artifact(self, *_args, **_kwargs):
        return None

    def save(self, path, *_args, **_kwargs):
        Path(path).write_text("{}")

    def record_phase_error(self, *_args, **_kwargs):
        return None

    def add_exception(self, *_args, **_kwargs):
        return None


def _install_export_dependencies(
    monkeypatch,
    *,
    settings,
    export_objects,
    processing_objects=None,
    validate_module,
):
    import Plugin.export as export_package

    context = SimpleNamespace(
        scene=SimpleNamespace(usd_stage_export_settings=settings),
        selected_objects=[],
    )
    bpy = _bpy_module(context=context, filepath="/tmp/Scene.blend")
    animation_export = SimpleNamespace(
        collect_export_objects=lambda _context, _settings: export_objects,
        collect_processing_objects=lambda _context, objects: (
            list(objects) if processing_objects is None else processing_objects
        ),
    )

    def fake_export(_context, _settings, filepath, _diagnostics, **_kwargs):
        Path(filepath).write_text("#usda 1.0\n")
        return filepath

    monkeypatch.setattr(export_command, "get_settings", lambda: settings)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "Plugin.nodes", _nodes_package(validate_module))
    monkeypatch.setattr(export_package, "animation_export", animation_export, raising=False)
    monkeypatch.setattr(
        export_package,
        "blender_usd_export",
        SimpleNamespace(
            export_blender_scene=fake_export,
            publish_unpacked_export=lambda *_args, **_kwargs: None,
            remove_export_staging_dir=lambda *_args, **_kwargs: None,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "diagnostics",
        SimpleNamespace(ExportDiagnostics=_FakeDiagnostics),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "pack_usdz",
        SimpleNamespace(create_usdz=lambda *_args, **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "postprocess_usd",
        SimpleNamespace(process_usd_stage=lambda *_args, **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_environment",
        lambda _context: {},
    )
    monkeypatch.setattr(
        support_bundle,
        "collect_scene_snapshot",
        lambda _context: {},
    )
    return context


def test_api_export_fails_selected_only_empty_before_material_validation(
    monkeypatch,
    tmp_path,
):
    settings = _settings(selected_objects_only=True)

    def unexpected_validation(*_args, **_kwargs):
        raise AssertionError("material validation must not run for an empty selection")

    validate_module = SimpleNamespace(
        collect_scene_materials=unexpected_validation,
        validate_material=unexpected_validation,
    )
    _install_export_dependencies(
        monkeypatch,
        settings=settings,
        export_objects=[],
        validate_module=validate_module,
    )

    with pytest.raises(CommandError) as caught:
        export_command.handle({"filepath": str(tmp_path / "scene.usdz")})

    assert caught.value.code == "NO_EXPORTABLE_OBJECTS"
    assert caught.value.stage == "validation"
    diagnostics_path = tmp_path / "scene.diagnostics.json"
    assert diagnostics_path.exists()
    assert caught.value.artifacts["diagnostics_path"] == str(diagnostics_path)


def test_api_premultiplied_avif_postprocess_failure_never_publishes_final(
    monkeypatch,
    tmp_path,
):
    settings = _settings(
        export_format="USDA",
    )
    validate_module = SimpleNamespace(
        collect_scene_materials=lambda _context: [],
        validate_material=lambda *_args, **_kwargs: {
            "ok": True,
            "errors": [],
            "warnings": [],
        },
    )
    _install_export_dependencies(
        monkeypatch,
        settings=settings,
        export_objects=[SimpleNamespace(name="Mesh")],
        validate_module=validate_module,
    )

    import Plugin.export as export_package

    final = tmp_path / "scene.usda"
    final.write_bytes(b"previous-published-export")
    staging_dir = tmp_path / ".usdstage_temp" / "scene.usda.attempt"
    staging_dir.mkdir(parents=True)
    staged = staging_dir / "scene.usda"
    staged.write_bytes(b"private-staged-export")
    exported = []
    processed = []
    published = []
    cleaned = []

    def stage_export(*_args, **_kwargs):
        exported.append(str(staged))
        return str(staged)

    export_package.blender_usd_export.export_blender_scene = stage_export
    export_package.blender_usd_export.publish_unpacked_export = (
        lambda *args, **kwargs: published.append((args, kwargs))
    )
    export_package.blender_usd_export.remove_export_staging_dir = (
        lambda *args, **kwargs: cleaned.append((args, kwargs))
    )

    def reject_premultiplied_avif(*_args, **_kwargs):
        processed.append(str(staged))
        raise RuntimeError(
            "Premultiplied base-color texture cannot be encoded as AVIF; Select PNG"
        )

    export_package.postprocess_usd.process_usd_stage = reject_premultiplied_avif

    with pytest.raises(
        CommandError,
        match="Premultiplied base-color texture",
    ) as caught:
        export_command.handle({"filepath": str(final)})

    assert exported == [str(staged)]
    assert processed == [str(staged)]
    assert final.read_bytes() == b"previous-published-export"
    assert published == []
    assert len(cleaned) == 1, (caught.value, caught.value.__cause__)
    assert cleaned[0][1]["staging_dir"] == staging_dir


def test_api_selected_export_ignores_unselected_unsupported_material(
    monkeypatch,
    tmp_path,
):
    selected_material = SimpleNamespace(name="SelectedPrincipled")
    unselected_glass = SimpleNamespace(name="UnselectedGlass")
    selected_cube = SimpleNamespace(
        name="SelectedCube",
        material_slots=[SimpleNamespace(material=selected_material)],
    )
    validated = []

    def collect_scene_materials(_context):
        raise AssertionError("selected export must not collect every scene material")

    def validate_material(
        material, *, strict, clamp_specular_tint
    ):
        validated.append(
            (material.name, strict, clamp_specular_tint)
        )
        return {
            "material": material.name,
            "ok": material is not unselected_glass,
            "errors": (
                [{"node_name": "Glass BSDF", "node_type": "BSDF_GLASS", "message": "unsupported"}]
                if material is unselected_glass
                else []
            ),
            "warnings": [],
        }

    settings = _settings(
        export_format="USDA",
        selected_objects_only=True,
    )
    _install_export_dependencies(
        monkeypatch,
        settings=settings,
        export_objects=[selected_cube],
        validate_module=SimpleNamespace(
            collect_scene_materials=collect_scene_materials,
            validate_material=validate_material,
        ),
    )

    result = export_command.handle({"filepath": str(tmp_path / "selected.usda")})

    assert result["ok"] is True
    assert validated == [("SelectedPrincipled", True, False)]


def test_api_full_scene_export_still_rejects_unselected_unsupported_material(
    monkeypatch,
    tmp_path,
):
    selected_material = SimpleNamespace(name="SelectedPrincipled")
    unselected_glass = SimpleNamespace(name="UnselectedGlass")
    validated = []

    def validate_material(
        material, *, strict, clamp_specular_tint
    ):
        validated.append(material.name)
        return {
            "material": material.name,
            "ok": material is not unselected_glass,
            "errors": (
                [{"node_name": "Glass BSDF", "node_type": "BSDF_GLASS", "message": "unsupported"}]
                if material is unselected_glass
                else []
            ),
            "warnings": [],
        }

    settings = _settings(export_format="USDA", selected_objects_only=False)
    _install_export_dependencies(
        monkeypatch,
        settings=settings,
        export_objects=[],
        validate_module=SimpleNamespace(
            collect_scene_materials=lambda _context: [
                selected_material,
                unselected_glass,
            ],
            validate_material=validate_material,
        ),
    )

    with pytest.raises(CommandError) as caught:
        export_command.handle({"filepath": str(tmp_path / "full.usda")})

    assert caught.value.code == "UNSUPPORTED_MATERIAL_NODES"
    assert caught.value.context["material"] == "UnselectedGlass"
    assert validated == ["SelectedPrincipled", "UnselectedGlass"]


def test_api_selected_export_validates_collection_prototype_material(
    monkeypatch,
    tmp_path,
):
    prototype_glass = SimpleNamespace(name="PrototypeGlass")
    instance = SimpleNamespace(name="SelectedInstance", material_slots=[])
    prototype = SimpleNamespace(
        name="PrototypeMesh",
        material_slots=[SimpleNamespace(material=prototype_glass)],
    )

    validate_module = SimpleNamespace(
        collect_scene_materials=lambda _context: (_ for _ in ()).throw(
            AssertionError("selected export must use its dependency closure")
        ),
        validate_material=lambda material, **_kwargs: {
            "material": material.name,
            "ok": False,
            "errors": [
                {
                    "node_name": "Glass BSDF",
                    "node_type": "BSDF_GLASS",
                    "message": "unsupported",
                }
            ],
            "warnings": [],
        },
    )
    _install_export_dependencies(
        monkeypatch,
        settings=_settings(export_format="USDA", selected_objects_only=True),
        export_objects=[instance],
        processing_objects=[instance, prototype],
        validate_module=validate_module,
    )

    with pytest.raises(CommandError) as caught:
        export_command.handle({"filepath": str(tmp_path / "instance.usda")})

    assert caught.value.code == "UNSUPPORTED_MATERIAL_NODES"
    assert caught.value.context["material"] == "PrototypeGlass"


def _load_export_operator_with_blender_stubs(monkeypatch):
    """Load only export_operator.py without importing the whole Blender add-on."""
    bpy = ModuleType("bpy")
    bpy.__path__ = []
    bpy_props = ModuleType("bpy.props")
    bpy_types = ModuleType("bpy.types")

    class Operator:
        def report(self, levels, message):
            self.reports = getattr(self, "reports", [])
            self.reports.append((levels, message))

    class ExportHelper:
        pass

    bpy_props.StringProperty = lambda **_kwargs: None
    bpy_types.Operator = Operator
    bpy.types = bpy_types

    bpy_extras = ModuleType("bpy_extras")
    bpy_extras.__path__ = []
    bpy_extras_io = ModuleType("bpy_extras.io_utils")
    bpy_extras_io.ExportHelper = ExportHelper

    prefs = ModuleType("Plugin.prefs")
    prefs.get_preferences = lambda _context: None
    prefs.set_last_export_path = lambda *_args, **_kwargs: None
    prefs.apply_last_export_path = lambda *_args, **_kwargs: None

    ops_package = ModuleType("Plugin.ops")
    ops_package.__path__ = [str(_REPO_ROOT / "Plugin" / "ops")]

    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", bpy_props)
    monkeypatch.setitem(sys.modules, "bpy.types", bpy_types)
    monkeypatch.setitem(sys.modules, "bpy_extras", bpy_extras)
    monkeypatch.setitem(sys.modules, "bpy_extras.io_utils", bpy_extras_io)
    monkeypatch.setitem(sys.modules, "Plugin.prefs", prefs)
    monkeypatch.setitem(sys.modules, "Plugin.ops", ops_package)
    monkeypatch.delitem(sys.modules, "Plugin.ops.export_operator", raising=False)

    return importlib.import_module("Plugin.ops.export_operator")


def _install_ui_export_dependencies(
    monkeypatch,
    tmp_path,
    *,
    settings,
    export_objects,
    processing_objects=None,
    validate_module,
):
    module = _load_export_operator_with_blender_stubs(monkeypatch)
    context = SimpleNamespace(
        scene=SimpleNamespace(usd_stage_export_settings=settings),
        blend_data=SimpleNamespace(filepath="/tmp/Scene.blend"),
    )
    import Plugin.export as export_package

    output = tmp_path / "scene.usda"

    def fake_export(_context, _settings, filepath, _diagnostics, **_kwargs):
        Path(filepath).write_text("#usda 1.0\n")
        return filepath

    monkeypatch.setattr(
        module,
        "_resolve_output_path_from_settings",
        lambda *_args, **_kwargs: str(output),
    )
    monkeypatch.setattr(module, "_apply_persisted_settings", lambda *_args: None)
    monkeypatch.setattr(module, "_store_last_export_settings", lambda *_args: None)
    monkeypatch.setitem(sys.modules, "Plugin.nodes", _nodes_package(validate_module))
    monkeypatch.setattr(
        export_package,
        "animation_export",
        SimpleNamespace(
            collect_export_objects=lambda *_args: export_objects,
            collect_processing_objects=lambda _context, objects: (
                list(objects) if processing_objects is None else processing_objects
            ),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "diagnostics",
        SimpleNamespace(ExportDiagnostics=_FakeDiagnostics),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "blender_usd_export",
        SimpleNamespace(
            export_blender_scene=fake_export,
            publish_unpacked_export=lambda *_args, **_kwargs: None,
            remove_export_staging_dir=lambda *_args, **_kwargs: None,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "postprocess_usd",
        SimpleNamespace(process_usd_stage=lambda *_args, **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "pack_usdz",
        SimpleNamespace(create_usdz=lambda *_args, **_kwargs: None),
        raising=False,
    )
    monkeypatch.setattr(support_bundle, "collect_environment", lambda _context: {})
    monkeypatch.setattr(support_bundle, "collect_scene_snapshot", lambda _context: {})

    operator = module.USDSTAGE_OT_export()
    operator.filepath = str(output)
    return module, context, operator


def test_ui_export_fails_selected_only_empty_before_export(monkeypatch, tmp_path):
    module = _load_export_operator_with_blender_stubs(monkeypatch)
    settings = _settings(
        selected_objects_only=True,
        export_format="USDA",
        history_applied=True,
        last_diagnostics_path="",
    )
    context = SimpleNamespace(
        scene=SimpleNamespace(usd_stage_export_settings=settings),
        blend_data=SimpleNamespace(filepath="/tmp/Scene.blend"),
    )
    import Plugin.export as export_package

    monkeypatch.setattr(
        module,
        "_resolve_output_path_from_settings",
        lambda *_args, **_kwargs: str(tmp_path / "scene.usda"),
    )
    monkeypatch.setattr(module, "_apply_persisted_settings", lambda *_args: None)
    monkeypatch.setattr(
        export_package,
        "animation_export",
        SimpleNamespace(collect_export_objects=lambda *_args: []),
        raising=False,
    )
    monkeypatch.setattr(
        export_package,
        "diagnostics",
        SimpleNamespace(ExportDiagnostics=_FakeDiagnostics),
        raising=False,
    )
    monkeypatch.setattr(support_bundle, "collect_environment", lambda _context: {})
    monkeypatch.setattr(support_bundle, "collect_scene_snapshot", lambda _context: {})

    operator = module.USDSTAGE_OT_export()
    operator.filepath = str(tmp_path / "scene.usda")

    assert operator.execute(context) == {'CANCELLED'}
    assert any(
        "no objects are selected" in message
        for _levels, message in operator.reports
    )


def test_ui_selected_export_ignores_unselected_unsupported_material(
    monkeypatch,
    tmp_path,
):
    selected_material = SimpleNamespace(name="SelectedPrincipled")
    unselected_glass = SimpleNamespace(name="UnselectedGlass")
    selected_cube = SimpleNamespace(
        name="SelectedCube",
        material_slots=[SimpleNamespace(material=selected_material)],
    )
    validated = []

    def collect_scene_materials(_context):
        raise AssertionError("selected export must not collect every scene material")

    def validate_material(
        material, *, strict, clamp_specular_tint
    ):
        validated.append(
            (material.name, strict, clamp_specular_tint)
        )
        return {
            "material": material.name,
            "ok": material is not unselected_glass,
            "errors": (
                [{"node_name": "Glass BSDF", "node_type": "BSDF_GLASS", "message": "unsupported"}]
                if material is unselected_glass
                else []
            ),
            "warnings": [],
        }

    settings = _settings(
        export_format="USDA",
        selected_objects_only=True,
        history_applied=True,
        last_diagnostics_path="",
    )
    _module, context, operator = _install_ui_export_dependencies(
        monkeypatch,
        tmp_path,
        settings=settings,
        export_objects=[selected_cube],
        validate_module=SimpleNamespace(
            collect_scene_materials=collect_scene_materials,
            validate_material=validate_material,
        ),
    )

    assert operator.execute(context) == {'FINISHED'}
    assert validated == [("SelectedPrincipled", True, False)]


def test_ui_full_scene_export_still_rejects_unselected_unsupported_material(
    monkeypatch,
    tmp_path,
):
    selected_material = SimpleNamespace(name="SelectedPrincipled")
    unselected_glass = SimpleNamespace(name="UnselectedGlass")
    validated = []

    def validate_material(
        material, *, strict, clamp_specular_tint
    ):
        validated.append(material.name)
        return {
            "material": material.name,
            "ok": material is not unselected_glass,
            "errors": (
                [{"node_name": "Glass BSDF", "node_type": "BSDF_GLASS", "message": "unsupported"}]
                if material is unselected_glass
                else []
            ),
            "warnings": [],
        }

    settings = _settings(
        export_format="USDA",
        selected_objects_only=False,
        history_applied=True,
        last_diagnostics_path="",
    )
    _module, context, operator = _install_ui_export_dependencies(
        monkeypatch,
        tmp_path,
        settings=settings,
        export_objects=[],
        validate_module=SimpleNamespace(
            collect_scene_materials=lambda _context: [
                selected_material,
                unselected_glass,
            ],
            validate_material=validate_material,
        ),
    )

    assert operator.execute(context) == {'CANCELLED'}
    assert validated == ["SelectedPrincipled", "UnselectedGlass"]
    assert any(
        "UnselectedGlass" in message
        for _levels, message in operator.reports
    )


def test_ui_selected_export_validates_collection_prototype_material(
    monkeypatch,
    tmp_path,
):
    prototype_glass = SimpleNamespace(name="PrototypeGlass")
    instance = SimpleNamespace(name="SelectedInstance", material_slots=[])
    prototype = SimpleNamespace(
        name="PrototypeMesh",
        material_slots=[SimpleNamespace(material=prototype_glass)],
    )
    settings = _settings(
        export_format="USDA",
        selected_objects_only=True,
        history_applied=True,
        last_diagnostics_path="",
    )
    _module, context, operator = _install_ui_export_dependencies(
        monkeypatch,
        tmp_path,
        settings=settings,
        export_objects=[instance],
        processing_objects=[instance, prototype],
        validate_module=SimpleNamespace(
            collect_scene_materials=lambda _context: (_ for _ in ()).throw(
                AssertionError("selected export must use its dependency closure")
            ),
            validate_material=lambda material, **_kwargs: {
                "material": material.name,
                "ok": False,
                "errors": [
                    {
                        "node_name": "Glass BSDF",
                        "node_type": "BSDF_GLASS",
                        "message": "unsupported",
                    }
                ],
                "warnings": [],
            },
        ),
    )

    assert operator.execute(context) == {'CANCELLED'}
    assert any(
        "PrototypeGlass" in message
        for _levels, message in operator.reports
    )
