"""Integration test — usdstage settings get/set/list."""

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


class TestSettingsGet:
    def test_returns_dict(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file))
        assert result.ok
        assert isinstance(result.json, dict)
        assert len(result.json) > 0, "Settings dict should not be empty"

    def test_has_export_format(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file))
        assert "export_format" in result.json
        assert isinstance(result.json["export_format"], str)
        assert result.json["export_format"] in ("USDA", "USDC", "USDZ")

    def test_group_filter_bake(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--group", "bake")
        assert result.ok
        assert "bake_mode" in result.json
        assert isinstance(result.json["bake_mode"], str)
        # Should NOT contain keys from other groups
        assert "export_format" not in result.json
        assert "bake_resolution" not in result.json

    def test_group_filter_texture(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--group", "texture")
        assert result.ok
        assert "export_texture_settings_enabled" in result.json
        assert "bake_resolution" in result.json
        assert "bake_mode" not in result.json

    def test_group_filter_materials(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--group", "materials")
        assert result.ok
        assert result.json == {"clamp_specular_tint": False}

    def test_group_filter_general(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--group", "general")
        assert result.ok
        assert "export_format" in result.json
        assert "bake_mode" not in result.json

    def test_group_filter_diagnostics(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--group", "diagnostics")
        assert result.ok
        assert result.json == {"diagnostics_enabled": False}

    def test_keys_filter(self, run_cli, blend_file):
        result = run_cli("settings", "get", str(blend_file), "--keys", "export_format")
        assert result.ok
        assert "export_format" in result.json
        # Should only return requested keys
        assert len(result.json) == 1


class TestSettingsSet:
    def test_dry_run(self, run_cli, blend_file):
        result = run_cli("settings", "set", str(blend_file), "export_format=USDZ", "--dry-run")
        assert result.ok
        assert result.json is not None

    def test_invalid_format_rejected(self, run_cli, blend_file):
        """Malformed key=value pairs should fail."""
        result = run_cli("settings", "set", str(blend_file), "no_equals_sign")
        assert not result.ok

    @pytest.mark.parametrize(
        "key",
        [
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
        ],
    )
    def test_removed_raw_geometry_setting_is_rejected(self, run_cli, blend_file, key):
        result = run_cli(
            "settings",
            "set",
            str(blend_file),
            f"{key}=true",
            "--dry-run",
        )

        assert not result.ok


class TestSettingsList:
    def test_returns_list(self, run_cli):
        result = run_cli("settings", "list")
        assert result.ok
        assert isinstance(result.json, list)
        assert len(result.json) > 0, "Settings list should not be empty"

    def test_entries_have_key(self, run_cli):
        result = run_cli("settings", "list")
        assert len(result.json) > 0
        for entry in result.json:
            assert "key" in entry
            assert isinstance(entry["key"], str)

    def test_entries_have_type(self, run_cli):
        result = run_cli("settings", "list")
        assert len(result.json) > 0
        for entry in result.json:
            assert "type" in entry
            assert isinstance(entry["type"], str)

    def test_contains_export_format(self, run_cli):
        result = run_cli("settings", "list")
        keys = [entry["key"] for entry in result.json]
        assert "export_format" in keys
        assert "diagnostics_enabled" in keys

    def test_realitykit_os27_artist_settings_exclude_spatial_contract(self, run_cli):
        result = run_cli("settings", "list")
        assert result.ok
        defaults = {entry["key"]: entry.get("default") for entry in result.json}

        assert defaults["clamp_specular_tint"] is False
        for key in (
            # One surface, no selector: the profile setting is gone too.
            "materialx_surface_profile",
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
        ):
            assert key not in defaults


class TestSettingsSetPersistence:
    """settings set must actually reach the .blend.

    Regression: settings_set was the only settings-writing command that did not
    bring the scene settings profile to the current schema first. The RNA update
    callback runs the profile migrator, which treats saved keys with no current
    schema stamp as legacy state and resets them. On a pristine .blend the first
    assignment is what creates that saved key, so the migrator wiped it while
    the command still reported {"updated": [...], "saved": true} and exit 0.
    """

    @staticmethod
    def _pristine_blend(tmp_path) -> Path:
        """A .blend saved from factory settings — no settings schema stamp."""
        blender = os.environ.get("USDSTAGE_BLENDER", "blender")
        target = tmp_path / "pristine.blend"
        proc = subprocess.run(
            [
                blender, "--background", "--factory-startup", "--python-expr",
                f"import bpy; bpy.ops.wm.save_as_mainfile(filepath={str(target)!r})",
            ],
            capture_output=True, text=True, timeout=120,
        )
        assert target.exists(), proc.stdout + proc.stderr
        return target

    def test_save_persists_every_key_including_the_first(self, run_cli, tmp_path):
        blend = self._pristine_blend(tmp_path)

        result = run_cli(
            "--json", "settings", "set", str(blend),
            "export_format=USDZ", "triangulate_meshes=true", "bake_margin=32",
            "--save",
        )
        assert result.ok, result.stderr
        assert result.json["saved"] is True

        read_back = run_cli(
            "--json", "settings", "get", str(blend),
            "--keys", "export_format", "triangulate_meshes", "bake_margin",
        )
        assert read_back.ok, read_back.stderr
        # export_format is the first key applied and the one the migrator ate.
        assert read_back.json["export_format"] == "USDZ", (
            "First applied key did not survive; the settings profile migrator "
            f"reset it. Got {read_back.json}"
        )
        assert read_back.json["triangulate_meshes"] is True
        assert read_back.json["bake_margin"] == 32

    def test_save_persists_a_single_key(self, run_cli, tmp_path):
        """With one key there is no later write to mask the reset."""
        blend = self._pristine_blend(tmp_path)

        result = run_cli(
            "--json", "settings", "set", str(blend), "export_format=USDZ", "--save"
        )
        assert result.ok, result.stderr

        read_back = run_cli(
            "--json", "settings", "get", str(blend), "--keys", "export_format"
        )
        assert read_back.ok, read_back.stderr
        assert read_back.json["export_format"] == "USDZ"

    def test_without_save_warns_that_nothing_was_written(self, run_cli, tmp_path):
        """The no-save form is a no-op; it must not read as a successful write."""
        blend = self._pristine_blend(tmp_path)

        result = run_cli("--json", "settings", "set", str(blend), "export_format=USDA")
        assert result.ok, result.stderr
        assert result.json["saved"] is False
        assert result.json.get("warnings"), (
            "settings set without --save discards the change when the worker "
            "exits; the result must say so"
        )

        read_back = run_cli(
            "--json", "settings", "get", str(blend), "--keys", "export_format"
        )
        assert read_back.json["export_format"] == "USDZ", (
            "value unexpectedly persisted without --save"
        )
