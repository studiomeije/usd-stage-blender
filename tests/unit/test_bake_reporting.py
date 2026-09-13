"""What the bake lane reports about itself: resolution, format, job status."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export import bake_textures  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402


RUNNER_PATH = Path(__file__).resolve().parents[2] / "Plugin" / "bake_export_runner.py"
_ORIGINAL_FORMAT_WARNING = (
    "Original texture format is only available for existing texture staging; "
    "baked textures are saved as PNG."
)


def _settings(**values):
    defaults = {
        "bake_mode": "LIT_IBL",
        "export_texture_settings_enabled": True,
        "bake_resolution": "ORIGINAL",
        "bake_image_format": "PNG",
        "bake_margin": 8,
    }
    defaults.update(values)
    return types.SimpleNamespace(**defaults)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # Lighting & Shadows bakes a source-keyed 0 at the 2048 default.
        ({"bake_mode": "LIT_IBL"}, 2048),
        ({"bake_mode": "LIT_IBL", "export_texture_settings_enabled": False}, 2048),
        ({"bake_mode": "NOT_A_MODE"}, 2048),
        # The albedo modes keep 0: each material bakes at its source size.
        ({"bake_mode": "UNLIT_ALBEDO"}, 0),
        ({"bake_mode": "LIT_ALBEDO"}, 0),
        # A fixed size is the size, in every mode.
        ({"bake_mode": "LIT_IBL", "bake_resolution": "512"}, 512),
        ({"bake_mode": "LIT_ALBEDO", "bake_resolution": "1024"}, 1024),
        (
            {
                "bake_mode": "LIT_IBL",
                "bake_resolution": "CUSTOM",
                "bake_resolution_custom": 300,
            },
            300,
        ),
    ],
)
def test_effective_resolution_is_the_size_the_bake_uses(values, expected):
    assert bake_textures.resolve_effective_bake_resolution(_settings(**values)) == expected


def test_bake_block_records_lit_ibl_resolution_after_the_default_applies():
    diagnostics = ExportDiagnostics()

    block = bake_textures.record_bake_diagnostics(
        diagnostics,
        _settings(bake_mode="LIT_IBL", bake_resolution="ORIGINAL"),
        object_count=3,
        native_export_object_count=2,
        texture_dir=Path("/staging/textures"),
    )

    assert diagnostics.data["bake"] is block
    assert block["resolution"] == 2048
    assert block["margin"] == 8
    assert (block["object_count"], block["native_export_object_count"]) == (3, 2)
    assert block["texture_dir"] == str(Path("/staging/textures"))


def test_bake_block_resolves_the_format_without_repeating_its_warning(capsys):
    """The bake reports the ORIGINAL -> PNG substitution itself. Recording the
    block, before or after the bake, must not add a second copy."""
    diagnostics = ExportDiagnostics()
    settings = _settings(bake_image_format="ORIGINAL")

    block = bake_textures.record_bake_diagnostics(
        diagnostics,
        settings,
        object_count=1,
        native_export_object_count=1,
        texture_dir="textures",
    )

    assert block["image_format"] == "PNG"
    assert diagnostics.data["warnings"] == []
    assert _ORIGINAL_FORMAT_WARNING not in capsys.readouterr().out

    resolved = bake_textures._resolve_bake_image_format(
        settings, diagnostics, safe_for_blender_save=True
    )

    assert resolved["file_format"] == "PNG"
    assert diagnostics.data["warnings"] == [_ORIGINAL_FORMAT_WARNING]


def test_runner_terminal_status_carries_warning_text(tmp_path, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "bpy", types.SimpleNamespace(data=types.SimpleNamespace(filepath=""))
    )
    spec = importlib.util.spec_from_file_location("_bake_reporting_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    status_path = tmp_path / "status.json"

    runner._update_status(
        status_path,
        "done",
        1.0,
        "Baked export complete - 1 warning(s)",
        warnings=["The scene has no World and no light objects."],
    )

    status = json.loads(status_path.read_text())
    assert status["warnings"] == ["The scene has no World and no light objects."]
