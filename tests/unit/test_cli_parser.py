"""Tests for CLI argument parsing — no Blender required."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure Plugin package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.cli.__main__ import CLIUsageError, build_parser  # noqa: E402


@pytest.fixture
def parser():
    return build_parser()


# ---------------------------------------------------------------------------
# Global flags
# ---------------------------------------------------------------------------

class TestGlobalFlags:
    def test_json_flag(self, parser):
        args = parser.parse_args(["--json", "version"])
        assert args.json_only is True

    def test_verbose_flag(self, parser):
        args = parser.parse_args(["--verbose", "version"])
        assert args.verbose is True

    def test_quiet_flag(self, parser):
        args = parser.parse_args(["--quiet", "version"])
        assert args.quiet is True

    def test_blender_flag(self, parser):
        args = parser.parse_args(["--blender", "/usr/bin/blender", "version"])
        assert args.blender == "/usr/bin/blender"

    def test_defaults(self, parser):
        args = parser.parse_args(["version"])
        assert args.json_only is False
        assert args.verbose is False
        assert args.quiet is False
        assert args.blender is None


# ---------------------------------------------------------------------------
# Subcommand parsing
# ---------------------------------------------------------------------------

class TestVersionCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["version"])
        assert args.command == "version"


class TestInfoCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["info", "scene.blend"])
        assert args.command == "info"
        assert args.blend_file == "scene.blend"

    def test_requires_blend_file(self, parser):
        with pytest.raises(CLIUsageError):
            parser.parse_args(["info"])


class TestListObjectsCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["list-objects", "scene.blend"])
        assert args.command == "list-objects"

    def test_type_filter(self, parser):
        args = parser.parse_args(["list-objects", "scene.blend", "--type", "MESH"])
        assert args.type == ["MESH"]

    def test_multiple_types(self, parser):
        args = parser.parse_args(["list-objects", "scene.blend", "--type", "MESH", "--type", "LIGHT"])
        assert args.type == ["MESH", "LIGHT"]

    def test_selected_flag(self, parser):
        args = parser.parse_args(["list-objects", "scene.blend", "--selected"])
        assert args.selected is True


class TestListMaterialsCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["list-materials", "scene.blend"])
        assert args.command == "list-materials"

    def test_unused_flag(self, parser):
        args = parser.parse_args(["list-materials", "scene.blend", "--unused"])
        assert args.unused is True


class TestValidateCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["validate", "scene.blend"])
        assert args.command == "validate"

    def test_material_filter(self, parser):
        args = parser.parse_args(["validate", "scene.blend", "--material", "Wood"])
        assert args.material == "Wood"

    def test_strict_flag_is_removed_because_validation_always_matches_export(self, parser):
        with pytest.raises(CLIUsageError):
            parser.parse_args(["validate", "scene.blend", "--strict"])

    def test_only_errors(self, parser):
        args = parser.parse_args(["validate", "scene.blend", "--only-errors"])
        assert args.only_errors is True


class TestExportCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["export", "scene.blend", "-o", "out.usdz"])
        assert args.command == "export"
        assert args.output == "out.usdz"
        assert args.blend_file == "scene.blend"

    def test_format(self, parser):
        args = parser.parse_args(["export", "scene.blend", "-o", "out.usdz", "--format", "USDZ"])
        assert args.format == "USDZ"

    def test_selected_only(self, parser):
        args = parser.parse_args(["export", "scene.blend", "-o", "out.usdz", "--selected-only"])
        assert args.selected_only is True

    def test_no_diagnostics(self, parser):
        args = parser.parse_args(["export", "scene.blend", "-o", "out.usdz", "--no-diagnostics"])
        assert args.no_diagnostics is True

    def test_diagnostics(self, parser):
        args = parser.parse_args(["export", "scene.blend", "-o", "out.usdz", "--diagnostics"])
        assert args.diagnostics is True

    def test_missing_output_raises(self, parser):
        with pytest.raises(CLIUsageError):
            parser.parse_args(["export", "scene.blend"])

    def test_overrides_plain(self, parser):
        """Overrides without leading dashes are parsed as positional args."""
        args = parser.parse_args([
            "export", "scene.blend",
            "export-animation=true", "triangulate-meshes=true",
            "-o", "out.usdz",
        ])
        assert "export-animation=true" in args.overrides
        assert "triangulate-meshes=true" in args.overrides


class TestBakeExportCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz"])
        assert args.command == "bake-export"
        assert args.output == "out.usdz"

    def test_bake_mode(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--bake-mode", "LIT_IBL"])
        assert args.bake_mode == "LIT_IBL"

    def test_resolution(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--resolution", "4096"])
        assert args.resolution == "4096"

    def test_image_format(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--image-format", "PNG"])
        assert args.image_format == "PNG"

    def test_original_image_format(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--image-format", "ORIGINAL"])
        assert args.image_format == "ORIGINAL"

    def test_margin_is_int(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--margin", "16"])
        assert args.margin == 16
        assert isinstance(args.margin, int)

    def test_ibl_strength_is_float(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--ibl-strength", "1.5"])
        assert args.ibl_strength == 1.5
        assert isinstance(args.ibl_strength, float)

    def test_ibl_rotation_is_float(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--ibl-rotation", "3.14"])
        assert args.ibl_rotation == pytest.approx(3.14)

    def test_boolean_flags(self, parser):
        args = parser.parse_args([
            "bake-export", "scene.blend", "-o", "out.usdz",
            "--selected-only", "--no-diagnostics", "--isolate-meshes",
            "--no-base-color", "--no-opacity",
        ])
        assert args.selected_only is True
        assert args.no_diagnostics is True
        assert args.diagnostics is False
        assert args.isolate_meshes is True
        assert args.no_base_color is True
        assert args.no_opacity is True

    def test_diagnostics(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--diagnostics"])
        assert args.diagnostics is True

    def test_step_timeout(self, parser):
        args = parser.parse_args(["bake-export", "scene.blend", "-o", "out.usdz", "--step-timeout", "300"])
        assert args.timeout_step == 300

    def test_global_timeout(self, parser):
        args = parser.parse_args(["--timeout", "3600", "bake-export", "scene.blend", "-o", "out.usdz"])
        assert args.timeout == 3600

    def test_global_timeout_default(self, parser):
        from Plugin.cli.__main__ import _timeout_for

        assert parser.parse_args(["version"]).timeout is None
        assert _timeout_for("version", None) == 600
        assert _timeout_for("bake_export", None) is None
        assert _timeout_for("export", None) is None
        assert _timeout_for("bake_export", 3600) == 3600
        assert _timeout_for("list_objects", 0) is None

    def test_new_bake_flags(self, parser):
        args = parser.parse_args([
            "bake-export", "scene.blend", "-o", "out.usdz",
            "--roughness-mode", "AVERAGE",
        ])
        assert args.roughness_mode == "AVERAGE"

    @pytest.mark.parametrize("command", ["export", "bake-export"])
    def test_apply_yup_flag_is_removed(self, parser, command):
        with pytest.raises(CLIUsageError):
            parser.parse_args(
                [command, "scene.blend", "--apply-yup", "-o", "out.usda"]
            )


class TestArgumentPlacement:
    def test_global_options_work_after_the_subcommand(self, parser):
        from Plugin.cli.__main__ import _parse_arguments

        args = _parse_arguments(parser, ["export", "a.blend", "-o", "x.usdz", "--json", "--timeout", "5"])
        assert args.json_only is True
        assert args.timeout == 5
        before = _parse_arguments(parser, ["--json", "export", "a.blend", "-o", "x.usdz"])
        assert before.json_only is True

    def test_overrides_are_collected_anywhere_after_the_blend_file(self, parser):
        from Plugin.cli.__main__ import _parse_arguments

        args = _parse_arguments(parser, ["export", "a.blend", "a=1", "-o", "x.usdz", "b=2"])
        assert args.overrides == ["a=1", "b=2"]

    def test_a_token_without_an_equals_sign_is_still_refused(self, parser):
        from Plugin.cli.__main__ import CLIError, _collect_overrides, _parse_arguments

        args = _parse_arguments(parser, ["export", "a.blend", "-o", "x.usdz", "stray"])
        with pytest.raises(CLIError, match="expected key=value"):
            _collect_overrides(args.overrides)
        with pytest.raises(CLIUsageError, match="unrecognized arguments"):
            _parse_arguments(parser, ["version", "stray"])

    def test_diagnostics_flags_are_exclusive(self, parser):
        with pytest.raises(CLIUsageError):
            parser.parse_args(["export", "a.blend", "-o", "x.usdz", "--diagnostics", "--no-diagnostics"])

    def test_the_output_extension_chooses_the_format_and_must_agree(self):
        from Plugin.cli.__main__ import _resolve_output_format

        assert _resolve_output_format("scene.usda", None) == "USDA"
        assert _resolve_output_format("scene", None) is None
        assert _resolve_output_format("scene.v2", "USDZ") == "USDZ"
        with pytest.raises(CLIUsageError, match="--format is USDZ"):
            _resolve_output_format("scene.usda", "USDZ")


class TestSupportBundleCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["support-bundle", "scene.blend", "--export-path", "out.usdz", "-o", "b.zip"])
        assert args.command == "support-bundle"
        assert args.blend_file == "scene.blend"
        assert args.export_path == "out.usdz"
        assert args.bundle_output == "b.zip"

    def test_the_blend_file_is_optional(self, parser):
        assert parser.parse_args(["support-bundle"]).blend_file is None

    def test_options(self, parser):
        args = parser.parse_args([
            "support-bundle", "scene.blend",
            "--bundle-output", "support.zip",
            "--job-dir", ".usdstage_jobs/job",
            "--diagnostics-path", "out.diagnostics.json",
            "--include-output",
            "--include-blend",
            "--full-log",
            "--no-redact",
        ])
        assert args.bundle_output == "support.zip"
        assert args.job_dir == ".usdstage_jobs/job"
        assert args.diagnostics_path == "out.diagnostics.json"
        assert args.include_output is True
        assert args.include_blend is True
        assert args.full_log is True
        assert args.no_redact is True


class TestSettingsGetCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["settings", "get", "scene.blend"])
        assert args.command == "settings"
        assert args.settings_command == "get"

    def test_keys(self, parser):
        args = parser.parse_args(["settings", "get", "scene.blend", "--keys", "export_format", "bake_resolution"])
        assert args.keys == ["export_format", "bake_resolution"]

    def test_group(self, parser):
        args = parser.parse_args(["settings", "get", "scene.blend", "--group", "texture"])
        assert args.group == "texture"


class TestSettingsSetCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["settings", "set", "scene.blend", "export_format=USDZ"])
        assert args.settings == ["export_format=USDZ"]

    def test_multiple_settings(self, parser):
        args = parser.parse_args(["settings", "set", "scene.blend", "export_format=USDZ", "bake_resolution=4096"])
        assert len(args.settings) == 2

    def test_save_flag(self, parser):
        args = parser.parse_args(["settings", "set", "scene.blend", "export_format=USDZ", "--save"])
        assert args.save is True

    def test_dry_run_flag(self, parser):
        args = parser.parse_args(["settings", "set", "scene.blend", "export_format=USDZ", "--dry-run"])
        assert args.dry_run is True


class TestSettingsListCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["settings", "list"])
        assert args.settings_command == "list"


class TestPreferencesGetCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["preferences", "get"])
        assert args.command == "preferences"
        assert args.prefs_command == "get"


class TestPreferencesSetCommand:
    def test_parses(self, parser):
        args = parser.parse_args(["preferences", "set", "usdzip_path=/opt/usd/bin/usdzip"])
        assert args.settings == ["usdzip_path=/opt/usd/bin/usdzip"]


class TestMissingCommand:
    def test_no_command_raises(self, parser):
        with pytest.raises(CLIUsageError):
            parser.parse_args([])


def _captured_run(monkeypatch):
    """Stub the Blender round trip and record the args each command sends."""
    import Plugin.cli.__main__ as cli

    sent = {}

    def fake_run(command, args, parsed):
        sent["command"], sent["args"] = command, args
        return {"ok": True}

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_print_json", lambda result: None)
    return cli, sent


def test_settings_set_folds_hyphens_in_keys_like_export_overrides(parser, monkeypatch):
    """CLI.md documents key spellings as interchangeable; `export` always
    folded them and `settings set` did not, so `bake-margin=4` was refused
    there as an unknown setting."""
    cli, sent = _captured_run(monkeypatch)
    parsed = parser.parse_args(["settings", "set", "scene.blend", "bake-margin=4", "export_format=USDZ", "--dry-run"])
    assert cli.cmd_settings_set(parsed) == 0
    assert sent["args"]["settings"] == {"bake_margin": "4", "export_format": "USDZ"}


def test_settings_get_keys_accept_either_spelling(parser, monkeypatch):
    cli, sent = _captured_run(monkeypatch)
    parsed = parser.parse_args(["settings", "get", "scene.blend", "--keys", "bake-margin", "export_format"])
    assert cli.cmd_settings_get(parsed) == 0
    assert sent["args"]["keys"] == ["bake_margin", "export_format"]


def test_export_help_offers_only_the_formats_that_exist(parser):
    """The `.import` package lane is gone; its help text must be too."""
    import argparse

    subparsers = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name in ("export", "bake-export"):
        help_text = next(c.help for c in subparsers._choices_actions if c.dest == name)
        assert ".import" not in help_text and "experimental" not in help_text.lower(), help_text
