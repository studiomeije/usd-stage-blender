"""Tests for settings common utilities — no Blender required."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.api.commands._settings_common import (  # noqa: E402
    BOOLEAN_FALSE_TOKENS,
    BOOLEAN_TRUE_TOKENS,
    INTERNAL_KEYS,
    SETTING_GROUPS,
    coerce_value,
    suspend_setting_persistence,
)
from Plugin.api.commands import settings_set  # noqa: E402
from Plugin.api.errors import CommandError  # noqa: E402


# ---------------------------------------------------------------------------
# INTERNAL_KEYS
# ---------------------------------------------------------------------------


class TestInternalKeys:
    def test_is_frozenset(self):
        assert isinstance(INTERNAL_KEYS, frozenset)

    def test_contains_expected_keys(self):
        """Verify all expected internal keys are present."""
        expected = {
            "rna_type",
            "name",
            "history_applied",
            "ui_material_type",
            "ui_pbr_processing",
            "ui_unlit_appearance",
        }
        assert expected.issubset(INTERNAL_KEYS)

def test_transient_command_settings_suspend_and_restore_persistence():
    settings = SimpleNamespace(persist_suspended=False)

    with suspend_setting_persistence(settings):
        assert settings.persist_suspended is True

    assert settings.persist_suspended is False


def test_transient_command_settings_restore_persistence_after_failure():
    settings = SimpleNamespace(persist_suspended=False)

    with pytest.raises(RuntimeError, match="command failed"):
        with suspend_setting_persistence(settings):
            assert settings.persist_suspended is True
            raise RuntimeError("command failed")

    assert settings.persist_suspended is False


# ---------------------------------------------------------------------------
# SETTING_GROUPS
# ---------------------------------------------------------------------------


class TestSettingGroups:
    def test_has_groups(self):
        assert set(SETTING_GROUPS.keys()) == {
            "general",
            "geometry",
            "rigging",
            "materials",
            "texture",
            "bake",
            "diagnostics",
        }

    def test_no_overlap_between_groups(self):
        """No setting key should appear in multiple groups."""
        all_keys: list[str] = []
        for keys in SETTING_GROUPS.values():
            all_keys.extend(keys)
        assert len(all_keys) == len(set(all_keys)), "Duplicate keys found across groups"

    def test_no_internal_keys_in_groups(self):
        for group_name, keys in SETTING_GROUPS.items():
            overlap = keys & INTERNAL_KEYS
            assert not overlap, f"Group '{group_name}' contains internal keys: {overlap}"

    def test_all_groups_non_empty(self):
        for group_name, keys in SETTING_GROUPS.items():
            assert len(keys) > 0, f"Group '{group_name}' is empty"

    def test_general_contains_export_format(self):
        assert "export_format" in SETTING_GROUPS["general"]

    def test_bake_contains_bake_mode(self):
        assert "bake_mode" in SETTING_GROUPS["bake"]

    def test_texture_contains_override_toggle(self):
        assert "export_texture_settings_enabled" in SETTING_GROUPS["texture"]

    def test_materials_contains_only_the_normalization_policy(self):
        """The surface-profile enum is gone; one surface needs no selector."""
        assert SETTING_GROUPS["materials"] == {"clamp_specular_tint"}

    def test_geometry_contains_triangulate(self):
        assert "triangulate_meshes" in SETTING_GROUPS["geometry"]

    def test_rigging_contains_armatures(self):
        assert "export_armatures" in SETTING_GROUPS["rigging"]

    def test_no_object_type_group_remains(self):
        assert "objects" not in SETTING_GROUPS

    def test_no_group_exposes_spatial_contract_settings(self):
        assert not {
            "convert_orientation",
            "forward_axis",
            "up_axis",
            "convert_scene_units",
            "meters_per_unit",
            "relative_paths",
            "export_meshes",
            "export_uvmaps",
            "rename_uvmaps",
            "export_normals",
            "apply_yup_geometry",
            "export_curves",
            "export_points",
            "export_hair",
            "export_volumes",
            "export_lights",
            "convert_world_material",
            "export_cameras",
        }.intersection(set().union(*SETTING_GROUPS.values()))

    def test_diagnostics_contains_toggle(self):
        assert "diagnostics_enabled" in SETTING_GROUPS["diagnostics"]

    def test_all_values_are_sets(self):
        for group_name, keys in SETTING_GROUPS.items():
            assert isinstance(keys, (set, frozenset)), f"Group '{group_name}' value is not a set"

    def test_all_keys_are_strings(self):
        for group_name, keys in SETTING_GROUPS.items():
            for key in keys:
                assert isinstance(key, str), f"Non-string key {key!r} in group '{group_name}'"


def test_export_panel_does_not_expose_unsupported_raw_geometry_switches():
    import ast

    panel_path = Path(__file__).resolve().parents[2] / "Plugin" / "ui" / "panel.py"
    tree = ast.parse(panel_path.read_text(), filename=str(panel_path))
    settings_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "USDStageExportSettings"
    )
    declared = {
        node.target.id
        for node in settings_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }
    drawn = {
        call.args[1].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "prop"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    }
    unsupported = {
        "export_curves",
        "export_points",
        "export_hair",
        "export_volumes",
        "export_lights",
        "convert_world_material",
        "export_cameras",
    }

    assert not unsupported.intersection(declared)
    assert not unsupported.intersection(drawn)


def test_export_panel_draws_step_timeout_and_hides_contract_details():
    import ast

    panel_path = Path(__file__).resolve().parents[2] / "Plugin" / "ui" / "panel.py"
    source = panel_path.read_text()
    tree = ast.parse(source, filename=str(panel_path))
    settings_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "USDStageExportSettings"
    )
    declared = {
        node.target.id
        for node in settings_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
    }
    drawn = {
        call.args[1].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "prop"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    }

    assert "bake_step_timeout_seconds" in declared
    assert "bake_step_timeout_seconds" in drawn
    assert "RealityKit OS 27 profile" not in source
    assert "Apple spatial contract" not in source
    assert "USDKit" not in source
    assert "Quick Look" not in source


def test_export_panel_has_one_artist_facing_export_action_and_contextual_options():
    panel_path = Path(__file__).resolve().parents[2] / "Plugin" / "ui" / "panel.py"
    source = panel_path.read_text()

    assert 'bl_label = "Material Settings"' in source
    assert 'bl_label = "Textures"' in source
    assert 'bl_label = "Advanced USD"' in source
    assert 'profile_row.prop(settings, "ui_material_type", expand=True)' in source
    assert 'text="Export"' in source
    assert "actions_box" not in source
    assert 'text="Bake Textures & Export"' not in source
    assert "_draw_usd_material_section" not in source
    assert "_draw_usd_bake_section" not in source
    assert 'text="Optimize Source Textures"' not in source
    assert '"ui_source_texture_resolution", text="Maximum Resolution"' in source
    assert '"ui_source_texture_format", text="Image Format"' in source
    assert 'text="Bake Resolution"' in source
    assert 'layout.prop(settings, "bake_margin")' in source
    assert "_draw_usd_texture_section" not in source
    assert '"usdstage_usd_texture"' not in source
    assert source.count("expand=True") >= 3
    assert 'label(text="Profile")' not in source
    assert 'label(text="Processing")' not in source
    assert 'label(text="Appearance")' not in source
    assert "column(heading=\"Include\"" in source
    assert "export_row.scale_y = 1.4" in source
    assert "_export_route_summary" not in source
    assert "icon='STATUS_WARNING'" in source
    assert "icon='STATUS_ERROR'" in source
    assert "layout.link(" in source


@pytest.mark.parametrize(
    "key",
    [
        "export_curves",
        "export_points",
        "export_hair",
        "export_volumes",
        "export_lights",
        "convert_world_material",
        "export_cameras",
    ],
)
def test_removed_raw_geometry_setting_has_stable_unknown_key_error(monkeypatch, key):
    settings = SimpleNamespace(
        bl_rna=SimpleNamespace(
            properties=[_mock_prop("ENUM", ["USDA", "USDC", "USDZ"], "export_format")]
        )
    )
    monkeypatch.setattr(settings_set, "get_settings", lambda: settings)

    with pytest.raises(CommandError) as caught:
        settings_set.handle({"settings": {key: True}, "dry_run": True})

    assert caught.value.code == "INVALID_SETTING_OVERRIDE"
    assert caught.value.details == [
        {"key": key, "value": True, "reason": "unknown setting"}
    ]


# ---------------------------------------------------------------------------
# coerce_value — mock Blender property objects
# ---------------------------------------------------------------------------


def _mock_prop(prop_type: str, enum_items=None, identifier="test_prop"):
    """Create a mock Blender property-like object."""
    prop = SimpleNamespace(type=prop_type, identifier=identifier)
    if enum_items:
        prop.enum_items = [SimpleNamespace(identifier=v) for v in enum_items]
    return prop


class TestCoerceValueBoolean:
    def test_true_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "true") is True

    def test_false_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "false") is False

    def test_one_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "1") is True

    def test_yes_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "yes") is True

    def test_zero_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "0") is False

    def test_no_string(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "no") is False

    def test_off_string(self):
        """Undocumented false-like strings fail instead of disabling options."""
        prop = _mock_prop("BOOLEAN")
        with pytest.raises(ValueError, match="Invalid boolean value"):
            coerce_value(prop, "off")

    def test_on_string_is_rejected(self):
        """Undocumented true-like strings fail instead of becoming false."""
        prop = _mock_prop("BOOLEAN")
        with pytest.raises(ValueError, match="Invalid boolean value"):
            coerce_value(prop, "on")

    def test_documented_boolean_token_sets_are_closed(self):
        assert BOOLEAN_TRUE_TOKENS == ("true", "1", "yes")
        assert BOOLEAN_FALSE_TOKENS == ("false", "0", "no")

    @pytest.mark.parametrize("value", ["falsee", "truthy", "", "2", None, 2])
    def test_garbage_boolean_values_are_rejected(self, value):
        prop = _mock_prop("BOOLEAN")
        with pytest.raises(ValueError, match="Invalid boolean value"):
            coerce_value(prop, value)

    def test_bool_passthrough(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, True) is True
        assert coerce_value(prop, False) is False

    def test_case_insensitive(self):
        prop = _mock_prop("BOOLEAN")
        assert coerce_value(prop, "TRUE") is True
        assert coerce_value(prop, "False") is False
        assert coerce_value(prop, "YES") is True


class TestCoerceValueInt:
    def test_string_to_int(self):
        prop = _mock_prop("INT")
        assert coerce_value(prop, "42") == 42

    def test_int_passthrough(self):
        prop = _mock_prop("INT")
        assert coerce_value(prop, 42) == 42

    def test_negative_int(self):
        prop = _mock_prop("INT")
        assert coerce_value(prop, "-5") == -5

    def test_zero(self):
        prop = _mock_prop("INT")
        assert coerce_value(prop, "0") == 0

    def test_invalid_raises(self):
        prop = _mock_prop("INT")
        with pytest.raises((ValueError, TypeError)):
            coerce_value(prop, "not_a_number")

    def test_float_string_to_int(self):
        """Float strings should either truncate or raise — verify behavior."""
        prop = _mock_prop("INT")
        # int("1.5") raises ValueError; this is expected behavior
        with pytest.raises((ValueError, TypeError)):
            coerce_value(prop, "1.5")


class TestCoerceValueFloat:
    def test_string_to_float(self):
        prop = _mock_prop("FLOAT")
        assert coerce_value(prop, "1.5") == pytest.approx(1.5)

    def test_int_to_float(self):
        prop = _mock_prop("FLOAT")
        assert coerce_value(prop, 3) == 3.0

    def test_negative_float(self):
        prop = _mock_prop("FLOAT")
        assert coerce_value(prop, "-0.5") == pytest.approx(-0.5)

    def test_zero(self):
        prop = _mock_prop("FLOAT")
        assert coerce_value(prop, "0") == 0.0

    def test_int_string_to_float(self):
        prop = _mock_prop("FLOAT")
        assert coerce_value(prop, "42") == 42.0

    def test_invalid_raises(self):
        prop = _mock_prop("FLOAT")
        with pytest.raises((ValueError, TypeError)):
            coerce_value(prop, "not_a_float")


class TestCoerceValueEnum:
    def test_valid_value(self):
        prop = _mock_prop("ENUM", enum_items=["USDA", "USDC", "USDZ"])
        assert coerce_value(prop, "USDZ") == "USDZ"

    def test_lowercase_auto_uppercased(self):
        prop = _mock_prop("ENUM", enum_items=["USDA", "USDC", "USDZ"])
        assert coerce_value(prop, "usdz") == "USDZ"

    def test_invalid_raises_with_allowed(self):
        prop = _mock_prop("ENUM", enum_items=["USDA", "USDC", "USDZ"], identifier="export_format")
        with pytest.raises(ValueError, match="Invalid value.*Allowed"):
            coerce_value(prop, "FOO")

    def test_mixed_case(self):
        prop = _mock_prop("ENUM", enum_items=["USDA", "USDC", "USDZ"])
        assert coerce_value(prop, "Usdz") == "USDZ"

    def test_empty_string_raises(self):
        prop = _mock_prop("ENUM", enum_items=["USDA", "USDC", "USDZ"], identifier="export_format")
        with pytest.raises(ValueError):
            coerce_value(prop, "")

    def test_mixed_case_enum_identifiers_preserve_canonical_value(self):
        """Canonical spelling is the declared identifier, whatever the caller typed."""
        prop = _mock_prop(
            "ENUM",
            enum_items=("UNLIT_ALBEDO", "LIT_IBL", "LIT_ALBEDO"),
            identifier="bake_mode",
        )
        assert coerce_value(prop, "lit_ibl") == "LIT_IBL"
        assert coerce_value(prop, "Lit_Albedo") == "LIT_ALBEDO"
        assert coerce_value(prop, "UNLIT_ALBEDO") == "UNLIT_ALBEDO"


class TestCoerceValueString:
    def test_string_passthrough(self):
        prop = _mock_prop("STRING")
        assert coerce_value(prop, "hello") == "hello"

    def test_int_to_string(self):
        prop = _mock_prop("STRING")
        assert coerce_value(prop, 42) == "42"

    def test_empty_string(self):
        prop = _mock_prop("STRING")
        assert coerce_value(prop, "") == ""

    def test_path_string(self):
        prop = _mock_prop("STRING")
        assert coerce_value(prop, "/some/path/file.blend") == "/some/path/file.blend"


class TestCoerceValueUnknown:
    def test_unknown_type_passthrough(self):
        prop = _mock_prop("POINTER")
        assert coerce_value(prop, "anything") == "anything"
