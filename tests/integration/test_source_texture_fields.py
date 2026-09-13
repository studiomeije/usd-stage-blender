"""Integration test — the direct route's texture fields drive the optimization gate.

The panel draws Maximum Resolution and Image Format for a direct export
without a separate toggle. Both read Keep Original and Original while
``export_texture_settings_enabled`` is off, whatever the bake route has stored.
Choosing anything else turns the gate on without reviving a hidden value, and
choosing Original for both turns it off. The fields are display only: the CLI
and the background bake never see them. A new scene exports USDZ.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

_DRIVER = r'''
import json, sys
sys.path.insert(0, sys.argv[sys.argv.index("--") + 1])
import Plugin
Plugin.register()
import bpy
from Plugin.api.commands._settings_common import INTERNAL_KEYS
from Plugin.ops.bake_export_operator import _SERIALIZED_SETTINGS_SKIP_KEYS

s = bpy.context.scene.usd_stage_export_settings
s.persist_suspended = True
steps = {"format": s.export_format}
s.export_texture_settings_enabled = False
s.bake_resolution = 'ORIGINAL'
s.bake_image_format = 'PNG'
steps["off"] = [s.ui_source_texture_resolution, s.ui_source_texture_format]
s.ui_source_texture_resolution = '2048'
steps["resolution"] = [s.export_texture_settings_enabled, s.bake_resolution, s.bake_image_format]
s.ui_source_texture_format = 'AVIF'
steps["format_choice"] = [s.export_texture_settings_enabled, s.bake_resolution, s.bake_image_format]
s.ui_source_texture_resolution = 'ORIGINAL'
steps["one_original"] = [s.export_texture_settings_enabled, s.ui_source_texture_format]
s.ui_source_texture_format = 'ORIGINAL'
steps["both_original"] = [s.export_texture_settings_enabled, s.ui_source_texture_resolution, s.ui_source_texture_format]
fields = ("ui_source_texture_resolution", "ui_source_texture_format")
steps["hidden"] = all(k in INTERNAL_KEYS and k in _SERIALIZED_SETTINGS_SKIP_KEYS for k in fields)
print("FIELDS_RESULT " + json.dumps(steps))
'''


def test_texture_fields_drive_the_gate_and_stay_out_of_the_cli(tmp_path):
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER)
    proc = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(Path(__file__).resolve().parents[2])],
        capture_output=True, text=True, timeout=300,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("FIELDS_RESULT ")), None)
    assert line, proc.stdout + proc.stderr
    steps = json.loads(line[len("FIELDS_RESULT "):])

    assert steps["format"] == "USDZ"
    assert steps["off"] == ["ORIGINAL", "ORIGINAL"]
    assert steps["resolution"] == [True, "2048", "ORIGINAL"]
    assert steps["format_choice"] == [True, "2048", "AVIF"]
    assert steps["one_original"] == [True, "AVIF"]
    assert steps["both_original"] == [False, "ORIGINAL", "ORIGINAL"]
    assert steps["hidden"] is True
