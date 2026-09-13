"""RealityKit OS 27 export-profile contract tests."""

from __future__ import annotations

from types import SimpleNamespace

from Plugin.api.commands._settings_common import (
    REALITYKIT_OS27_DEFAULTS,
    REALITYKIT_OS27_PROFILE_NAME,
    realitykit_os27_profile_deviations,
)
from Plugin.apple_contract import (
    REALITYKIT_FORWARD_AXIS,
    REALITYKIT_METERS_PER_UNIT,
    REALITYKIT_SCENE_UNITS,
    REALITYKIT_UP_AXIS,
)
from Plugin.export.support_bundle import _collect_export_profile


def _settings(**overrides):
    values = dict(REALITYKIT_OS27_DEFAULTS)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_realitykit_os27_defaults_are_y_up_meters_and_fail_closed():
    assert REALITYKIT_OS27_PROFILE_NAME == "REALITYKIT_OS27"
    assert REALITYKIT_FORWARD_AXIS == "-Z"
    assert REALITYKIT_UP_AXIS == "Y"
    assert REALITYKIT_SCENE_UNITS == "METERS"
    assert REALITYKIT_METERS_PER_UNIT == 1.0
    assert REALITYKIT_OS27_DEFAULTS["clamp_specular_tint"] is False


def test_spatial_contract_is_not_a_profile_deviation():
    settings = _settings(up_axis="Z", convert_orientation=False)

    assert realitykit_os27_profile_deviations(settings) == {}
    assert _collect_export_profile(settings)["strict_defaults_active"] is True


def test_profile_reports_strict_defaults_active():
    profile = _collect_export_profile(_settings())

    assert profile["name"] == "REALITYKIT_OS27"
    assert profile["spatial_contract"] == {
        "convert_orientation": True,
        "forward_axis": "-Z",
        "up_axis": "Y",
        "scene_units": "METERS",
        "meters_per_unit": 1.0,
        "relative_paths": True,
        "export_meshes": True,
        "export_uvmaps": True,
        "rename_uvmaps": True,
        "export_normals": True,
    }
    assert profile["strict_defaults_active"] is True
    assert profile["deviations"] == {}


def test_export_normalization_opt_in_is_reported_as_a_deviation():
    profile = _collect_export_profile(
        _settings(clamp_specular_tint=True)
    )

    assert profile["strict_defaults_active"] is False
    assert profile["deviations"]["clamp_specular_tint"] == {
        "expected": False,
        "actual": True,
    }
