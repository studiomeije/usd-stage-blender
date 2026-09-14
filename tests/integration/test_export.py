"""Integration test — usdstage export."""

import json
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.integration


class TestExport:
    def test_blender_52_factory_default_cube_is_portable(self, run_cli, tmp_output):
        """Blender's native doubleSided=true default must not block export."""
        blender = os.environ.get("USDSTAGE_BLENDER", "blender")
        blend_path = tmp_output / "factory-default-cube.blend"
        create = subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--python-expr",
                (
                    "import bpy; "
                    f"bpy.ops.wm.save_as_mainfile(filepath={str(blend_path)!r})"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert create.returncode == 0, create.stderr or create.stdout

        out = tmp_output / "factory-default-cube.usda"
        result = run_cli(
            "--json",
            "export",
            str(blend_path),
            "-o",
            str(out),
            "--format",
            "USDA",
            "--diagnostics",
        )

        assert result.ok, f"Export failed: {result.stderr}"
        assert "uniform bool doubleSided = 0" in out.read_text()
        diagnostics_path = Path(result.json["diagnostics_path"])
        diagnostics = json.loads(diagnostics_path.read_text())
        assert diagnostics["realitykit_preflight"]["ok"] is True
        # Reclassified from warning to info: it fires for every mesh on
        # essentially every export (Blender authors doubleSided=true by
        # default), so as a warning it drowned the actionable ones. The
        # normalization itself is unchanged and still recorded.
        matching = [
            note
            for note in diagnostics.get("info", [])
            if "doubleSided=false" in note
        ]
        assert len(matching) == 1
        assert "closed or thick geometry is required" in matching[0]
        assert not any(
            "doubleSided=false" in warning for warning in diagnostics["warnings"]
        )

    def test_export_usdz(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDZ")
        assert result.ok, f"Export failed: {result.stderr}"
        actual_path = Path(result.json["export_path"])
        assert actual_path.exists(), f"Output USDZ file was not created at {actual_path}"
        assert actual_path.stat().st_size > 0, "Output file is empty"

    def test_export_usda(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usda"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDA")
        assert result.ok, f"Export failed: {result.stderr}"
        actual_path = Path(result.json["export_path"])
        assert actual_path.exists(), f"Output USDA file was not created at {actual_path}"
        assert actual_path.stat().st_size > 0, "Output file is empty"

    def test_output_has_duration(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDZ")
        assert result.ok
        assert "duration_seconds" in result.json
        assert isinstance(result.json["duration_seconds"], (int, float))
        assert result.json["duration_seconds"] > 0

    def test_invalid_blend_file(self, run_cli, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", "/nonexistent.blend", "-o", str(out))
        assert not result.ok

    def test_failed_export_always_writes_diagnostics(
        self,
        run_cli,
        tmp_output,
    ):
        blender = os.environ.get("USDSTAGE_BLENDER", "blender")
        blend_path = tmp_output / "nothing-selected.blend"
        create = subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--python-expr",
                (
                    "import bpy; "
                    "bpy.ops.object.select_all(action='DESELECT'); "
                    f"bpy.ops.wm.save_as_mainfile(filepath={str(blend_path)!r})"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert create.returncode == 0, create.stderr or create.stdout

        out = tmp_output / "nothing-selected.usdc"
        result = run_cli(
            "--json",
            "export",
            str(blend_path),
            "-o",
            str(out),
            "--format",
            "USDC",
            "--selected-only",
        )

        assert not result.ok
        diagnostics_path = out.with_suffix(".diagnostics.json")
        assert diagnostics_path.exists()
        diagnostics = json.loads(diagnostics_path.read_text())
        assert diagnostics["export_context"]["selected_only"] is True
        assert result.json["artifacts"]["diagnostics_path"] == str(diagnostics_path)

    def test_no_diagnostics(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDZ", "--no-diagnostics")
        assert result.ok
        actual_path = Path(result.json["export_path"])
        assert actual_path.exists()
        # diagnostics_path should be None when --no-diagnostics is used
        if "diagnostics_path" in result.json:
            assert result.json["diagnostics_path"] is None
        assert not actual_path.with_suffix(".diagnostics.json").exists()

    def test_diagnostics_disabled_by_default(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDZ")
        assert result.ok
        actual_path = Path(result.json["export_path"])
        assert actual_path.exists()
        assert result.json.get("diagnostics_path") is None
        assert not actual_path.with_suffix(".diagnostics.json").exists()

    def test_diagnostics_flag_writes_sidecar(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "scene.usdz"
        result = run_cli("export", str(blend_file), "-o", str(out), "--format", "USDZ", "--diagnostics")
        assert result.ok
        diag_path = Path(result.json["diagnostics_path"])
        assert diag_path.exists()
        assert diag_path.name == "scene.diagnostics.json"
