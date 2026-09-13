"""Strict and transactional CLI setting validation regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from Plugin.api.commands import bake_export
from Plugin.api.commands import export as export_command
from Plugin.api.commands import settings_set
from Plugin.api.errors import CommandError


def _prop(
    identifier: str,
    prop_type: str,
    *,
    enum_items: tuple[str, ...] = (),
    hard_min=None,
    hard_max=None,
):
    prop = SimpleNamespace(type=prop_type, identifier=identifier)
    if prop_type == "ENUM":
        prop.enum_items = [SimpleNamespace(identifier=item) for item in enum_items]
    if hard_min is not None:
        prop.hard_min = hard_min
    if hard_max is not None:
        prop.hard_max = hard_max
    return prop


class _FakeSettings:
    def __init__(self, properties, values, *, fail_on=None):
        object.__setattr__(
            self,
            "bl_rna",
            SimpleNamespace(properties=list(properties)),
        )
        object.__setattr__(self, "_values", dict(values))
        object.__setattr__(self, "_fail_on", dict(fail_on or {}))
        object.__setattr__(self, "assignments", [])

    def __getattr__(self, key):
        try:
            return self._values[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        if key.startswith("_") or key in {"bl_rna", "assignments"}:
            object.__setattr__(self, key, value)
            return
        self.assignments.append((key, value))
        if self._fail_on.get(key) == value:
            raise RuntimeError(f"RNA rejected {key}")
        self._values[key] = value


def _boolean_settings():
    return _FakeSettings(
        [
            _prop("export_animation", "BOOLEAN"),
            _prop("triangulate_meshes", "BOOLEAN"),
        ],
        {
            "export_animation": False,
            "triangulate_meshes": False,
        },
    )


@pytest.mark.parametrize("dry_run", [False, True])
def test_settings_set_rejects_boolean_typo_without_partial_mutation(
    monkeypatch,
    dry_run,
):
    settings = _boolean_settings()
    monkeypatch.setattr(settings_set, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        settings_set.handle({
            "settings": {
                "export_animation": "yes",
                "triangulate_meshes": "flase",
            },
            "dry_run": dry_run,
        })

    assert caught.value.code == "INVALID_SETTING_VALUE"
    assert caught.value.details[0]["key"] == "triangulate_meshes"
    assert caught.value.details[0]["value"] == "flase"
    assert "Invalid boolean value" in caught.value.details[0]["reason"]
    assert settings.export_animation is False
    assert settings.triangulate_meshes is False
    assert settings.assignments == []


def test_export_rejects_boolean_typo_with_stable_code(monkeypatch):
    settings = _boolean_settings()
    monkeypatch.setattr(export_command, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        export_command.handle({
            "filepath": "ignored.usdz",
            "overrides": {"export_animation": "on"},
        })

    assert caught.value.code == "INVALID_SETTING_VALUE"
    assert caught.value.details[0]["key"] == "export_animation"
    assert caught.value.details[0]["value"] == "on"
    assert settings.assignments == []


def test_bake_export_rejects_boolean_typo_with_stable_code(monkeypatch):
    settings = _boolean_settings()
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "overrides": {"export_animation": "off"},
        })

    assert caught.value.code == "INVALID_SETTING_VALUE"
    assert caught.value.details[0]["key"] == "export_animation"
    assert caught.value.details[0]["value"] == "off"
    assert settings.assignments == []


def _direct_bake_settings(*, fail_on=None):
    properties = [
        _prop("export_format", "ENUM", enum_items=("USDA", "USDC", "USDZ")),
        _prop("bake_margin", "INT", hard_min=0, hard_max=32767),
        _prop("bake_ibl_strength", "FLOAT", hard_min=0.0, hard_max=1000.0),
        _prop("export_texture_settings_enabled", "BOOLEAN"),
        _prop(
            "bake_resolution",
            "ENUM",
            enum_items=("ORIGINAL", "512", "1024", "2048", "4096", "CUSTOM"),
        ),
        _prop("bake_resolution_custom", "INT", hard_min=32, hard_max=32767),
    ]
    return _FakeSettings(
        properties,
        {
            "export_format": "USDA",
            "bake_margin": 8,
            "bake_ibl_strength": 1.0,
            "export_texture_settings_enabled": False,
            "bake_resolution": "2048",
            "bake_resolution_custom": 2048,
        },
        fail_on=fail_on,
    )


def test_bake_direct_args_aggregate_coercion_failures_before_mutation(monkeypatch):
    settings = _direct_bake_settings()
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "format": "NOT_USD",
            "margin": "wide",
            "ibl_strength": "strong",
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert [detail["key"] for detail in caught.value.details] == [
        "format",
        "margin",
        "ibl_strength",
    ]
    assert all("value" in detail and "reason" in detail for detail in caught.value.details)
    assert settings.assignments == []
    assert settings._values == {
        "export_format": "USDA",
        "bake_margin": 8,
        "bake_ibl_strength": 1.0,
        "export_texture_settings_enabled": False,
        "bake_resolution": "2048",
        "bake_resolution_custom": 2048,
    }


def test_bake_direct_rna_failures_are_aggregated_and_rolled_back(monkeypatch):
    settings = _direct_bake_settings(
        fail_on={
            "export_format": "USDZ",
            "bake_margin": 16,
        }
    )
    original = dict(settings._values)
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "format": "USDZ",
            "margin": 16,
            "ibl_strength": 2.0,
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert [detail["key"] for detail in caught.value.details] == [
        "format",
        "margin",
    ]
    assert all(detail["source"] == "direct" for detail in caught.value.details)
    assert settings._values == original
    assert ("bake_ibl_strength", 2.0) in settings.assignments
    assert ("bake_ibl_strength", 1.0) in settings.assignments


def test_bake_direct_range_failure_does_not_apply_valid_positional_override(monkeypatch):
    settings = _direct_bake_settings()
    settings.bl_rna.properties.append(_prop("export_animation", "BOOLEAN"))
    settings._values["export_animation"] = False
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "overrides": {"export_animation": "true"},
            "margin": -1,
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert caught.value.details[0]["key"] == "margin"
    assert "minimum is 0" in caught.value.details[0]["reason"]
    assert settings.export_animation is False
    assert settings.assignments == []


def test_bake_direct_error_takes_precedence_over_positional_value_error(monkeypatch):
    settings = _direct_bake_settings()
    settings.bl_rna.properties.append(_prop("export_animation", "BOOLEAN"))
    settings._values["export_animation"] = False
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "overrides": {"export_animation": "flase"},
            "margin": "wide",
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert [detail["key"] for detail in caught.value.details] == [
        "export_animation",
        "margin",
    ]
    assert settings.assignments == []


def test_bake_invalid_custom_resolution_does_not_switch_resolution_mode(monkeypatch):
    settings = _direct_bake_settings()
    original = dict(settings._values)
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "format": "USDZ",
            "resolution": "not-a-resolution",
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert caught.value.details[0]["key"] == "resolution"
    assert caught.value.details[0]["value"] == "not-a-resolution"
    assert settings._values == original
    assert settings.assignments == []


def test_bake_too_small_custom_resolution_is_rejected_before_mutation(monkeypatch):
    settings = _direct_bake_settings()
    original = dict(settings._values)
    monkeypatch.setattr(bake_export, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        bake_export.handle({
            "filepath": "ignored.usdz",
            "resolution": "16",
        })

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert caught.value.details[0]["key"] == "resolution"
    assert "minimum is 32" in caught.value.details[0]["reason"]
    assert settings._values == original
    assert settings.assignments == []
