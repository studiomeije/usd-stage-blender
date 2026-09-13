"""Integration test — usdstage bake-export."""

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

BLEND_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "References" / "Blender"


class TestBakeExport:
    def test_bake_export_default(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "baked.usdz"
        result = run_cli(
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--resolution", "512",  # Low res for speed
            timeout=300,
        )
        assert result.ok, f"Bake-export failed: {result.stderr}"
        # The command may adjust the extension to match format
        actual_path = Path(result.json["export_path"])
        assert actual_path.exists(), f"Output file not found at {actual_path}"

    def test_output_has_bake_stats(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "baked.usdz"
        result = run_cli(
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--resolution", "512",
            timeout=300,
        )
        assert result.ok, f"Bake-export failed: {result.stderr}"
        assert "bake_stats" in result.json, "Missing bake_stats in output"
        stats = result.json["bake_stats"]
        assert "objects_baked" in stats
        assert isinstance(stats["objects_baked"], int)
        assert stats["objects_baked"] > 0
        assert "resolution" in stats
        assert "image_format" in stats

    def test_resolution_512(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "baked.usdz"
        result = run_cli(
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--resolution", "512",
            timeout=300,
        )
        assert result.ok, f"Bake-export failed: {result.stderr}"
        assert "bake_stats" in result.json, "Missing bake_stats — cannot verify resolution"
        assert result.json["bake_stats"]["resolution"] == 512

    def test_image_format_png(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "baked_png.usdz"
        result = run_cli(
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--resolution", "512",
            "--image-format", "PNG",
            timeout=300,
        )
        assert result.ok, f"Bake-export with PNG failed: {result.stderr}"
        assert result.json["bake_stats"]["image_format"] == "PNG"

    def test_output_has_duration(self, run_cli, blend_file, tmp_output):
        out = tmp_output / "baked.usdz"
        result = run_cli(
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--resolution", "512",
            timeout=300,
        )
        assert result.ok, f"Bake-export failed: {result.stderr}"
        assert "duration_seconds" in result.json
        assert isinstance(result.json["duration_seconds"], (int, float))
        assert result.json["duration_seconds"] > 0

    def test_each_warning_is_reported_once(self, run_cli, blend_file, tmp_output):
        """ORIGINAL bakes PNG and warns. One run must say so once, in the
        payload and in the sidecar."""
        out = tmp_output / "original_format.usdz"
        result = run_cli(
            "--json",
            "bake-export", str(blend_file), "-o", str(out),
            "--format", "USDZ",
            "--bake-mode", "LIT_ALBEDO",
            "--resolution", "512",
            "--image-format", "ORIGINAL",
            "--diagnostics",
            timeout=300,
        )
        assert result.ok, f"Bake-export failed: {result.stdout}\n{result.stderr}"
        warnings = result.json["warnings"]
        original = [text for text in warnings if text.startswith("Original texture format")]
        assert len(original) == 1, warnings
        assert len(warnings) == len(set(warnings)), warnings
        sidecar = json.loads(Path(result.json["diagnostics_path"]).read_text())
        assert sidecar["warnings"] == warnings

    def test_invalid_blend_file(self, run_cli, tmp_output):
        out = tmp_output / "baked.usdz"
        result = run_cli(
            "bake-export", "/nonexistent/file.blend", "-o", str(out),
            "--resolution", "512",
            timeout=60,
        )
        assert not result.ok
