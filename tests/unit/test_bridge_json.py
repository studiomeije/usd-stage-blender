"""Tests for bridge JSON extraction and find_blender — no Blender required."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.cli import bridge  # noqa: E402
from Plugin.cli.bridge import (  # noqa: E402
    BridgeError,
    OUTPUT_MARKER,
    extract_result,
    find_blender,
    run,
)


# ---------------------------------------------------------------------------
# extract_result — tests the REAL function from bridge.py
# ---------------------------------------------------------------------------


class TestExtractResultSuccess:
    def test_clean_output(self):
        payload = {"ok": True, "result": {"version": "1.0"}}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        result = extract_result(stdout, "", 0)
        assert result == {"version": "1.0"}

    def test_noisy_output(self):
        """Blender prints startup messages before/after the JSON markers."""
        payload = {"ok": True, "result": {"count": 5}}
        stdout = (
            "Blender 5.1.0\n"
            "Read prefs: /Users/test/.config/blender\n"
            f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}\n"
            "Blender quit\n"
        )
        result = extract_result(stdout, "", 0)
        assert result == {"count": 5}

    def test_multiline_json(self):
        payload = {"ok": True, "result": {"items": [1, 2, 3]}}
        pretty = json.dumps(payload, indent=2)
        stdout = f"noise\n{OUTPUT_MARKER}{pretty}{OUTPUT_MARKER}\nmore noise"
        result = extract_result(stdout, "", 0)
        assert result["items"] == [1, 2, 3]

class TestExtractResultErrors:
    def test_missing_markers_raises(self):
        with pytest.raises(RuntimeError, match="No output from Blender"):
            extract_result("Blender 5.1.0\nSome error occurred\n", "", 1)

    def test_empty_stdout_raises(self):
        with pytest.raises(RuntimeError, match="No output from Blender"):
            extract_result("", "", 1)

    def test_invalid_json_between_markers_raises(self):
        stdout = f"{OUTPUT_MARKER}not valid json{OUTPUT_MARKER}"
        with pytest.raises(RuntimeError, match="Failed to parse"):
            extract_result(stdout, "", 0)

    def test_error_response_raises(self):
        payload = {"ok": False, "error": "Export failed: missing UV map"}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        with pytest.raises(RuntimeError, match="Export failed: missing UV map"):
            extract_result(stdout, "", 0)

    def test_structured_error_response_is_preserved(self):
        payload = {
            "ok": False,
            "schema_version": "1.0",
            "command": "export",
            "error": {"code": "POSTPROCESS_FAILED", "message": "Postprocess failed"},
            "artifacts": {"diagnostics_path": "/tmp/out.diagnostics.json"},
        }
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        with pytest.raises(BridgeError) as exc_info:
            extract_result(stdout, "stderr tail", 1)
        assert exc_info.value.response["artifacts"]["diagnostics_path"] == "/tmp/out.diagnostics.json"
        assert exc_info.value.to_json()["error"]["code"] == "POSTPROCESS_FAILED"

    def test_error_response_unknown_error(self):
        payload = {"ok": False}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        with pytest.raises(RuntimeError, match="Unknown error"):
            extract_result(stdout, "", 0)

    def test_only_first_marker_raises(self):
        stdout = f"{OUTPUT_MARKER}{{\"ok\": true}}"
        with pytest.raises(RuntimeError, match="No output from Blender"):
            extract_result(stdout, "", 1)

    def test_exit_code_127_raises_blender_not_found(self):
        with pytest.raises(RuntimeError, match="Blender not found"):
            extract_result("", "command not found", 127)

    def test_exit_code_127_includes_blender_path(self):
        with pytest.raises(RuntimeError, match="/custom/blender"):
            extract_result("", "", 127, blender="/custom/blender")

    def test_includes_exit_code_in_message(self):
        with pytest.raises(RuntimeError, match="exit code 42"):
            extract_result("", "", 42)

    def test_stderr_snippet_in_error(self):
        """When no markers found, stderr tail is included in error message."""
        with pytest.raises(RuntimeError, match="some blender error"):
            extract_result("", "some blender error", 1)


# ---------------------------------------------------------------------------
# bridge.run() — mock subprocess to test integration logic
# ---------------------------------------------------------------------------


class TestBridgeRun:
    def test_file_not_found_raises(self):
        """FileNotFoundError from subprocess -> RuntimeError 'Blender not found'."""
        with patch("Plugin.cli.bridge.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(BridgeError, match="Blender not found") as exc_info:
                run("version", {}, blender_path="/nonexistent/blender")
        assert exc_info.value.error_code == "BLENDER_NOT_FOUND"

    @pytest.mark.parametrize(
        "failure",
        [
            PermissionError(13, "Permission denied"),
            IsADirectoryError(21, "Is a directory"),
            OSError(8, "Exec format error"),
        ],
    )
    def test_os_launch_failure_is_structured(self, failure):
        with patch("Plugin.cli.bridge.subprocess.run", side_effect=failure):
            with pytest.raises(BridgeError, match="failed to start") as exc_info:
                run("version", {}, blender_path="/bad/blender")

        assert exc_info.value.error_code == "BLENDER_START_FAILED"
        payload = exc_info.value.to_json()
        assert payload["error"]["code"] == "BLENDER_START_FAILED"
        assert payload["context"]["blender_path"] == "/bad/blender"

    def test_timeout_raises(self):
        """TimeoutExpired -> RuntimeError with timeout message."""
        with patch(
            "Plugin.cli.bridge.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="blender", timeout=10),
        ):
            with pytest.raises(RuntimeError, match="timed out after 10s"):
                run("version", {}, blender_path="blender", timeout=10)

    def test_success_returns_result(self):
        """Successful run returns the result dict."""
        payload = {"ok": True, "result": {"version": "1.0"}}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        mock_proc = SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with patch("Plugin.cli.bridge.subprocess.run", return_value=mock_proc):
            result = run("version", {}, blender_path="blender")
            assert result == {"version": "1.0"}

    def test_command_error_raises(self):
        """When API returns ok=False, run() raises RuntimeError."""
        payload = {"ok": False, "error": "Command failed"}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        mock_proc = SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with patch("Plugin.cli.bridge.subprocess.run", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="Command failed"):
                run("export", {}, blender_path="blender")

    def test_blend_file_in_command(self):
        """blend_file is inserted into the command line."""
        payload = {"ok": True, "result": {}}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        mock_proc = SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with patch("Plugin.cli.bridge.subprocess.run", return_value=mock_proc) as mock_run:
            run("info", {}, blend_file="/path/to/scene.blend", blender_path="blender")
            cmd = mock_run.call_args[0][0]
            assert "/path/to/scene.blend" in cmd

    def test_bake_export_uses_factory_startup(self):
        """bake_export isolates the background Blender session from user add-ons."""
        payload = {"ok": True, "result": {}}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        mock_proc = SimpleNamespace(stdout=stdout, stderr="", returncode=0)
        with patch("Plugin.cli.bridge.subprocess.run", return_value=mock_proc) as mock_run:
            run("bake_export", {}, blend_file="/path/to/scene.blend", blender_path="blender")
            cmd = mock_run.call_args[0][0]
            assert "--factory-startup" in cmd

    def test_verbose_prints_stderr(self, capsys):
        """verbose=True prints Blender's stderr."""
        payload = {"ok": True, "result": {}}
        stdout = f"{OUTPUT_MARKER}{json.dumps(payload)}{OUTPUT_MARKER}"
        mock_proc = SimpleNamespace(
            stdout=stdout, stderr="Blender startup noise\n", returncode=0
        )
        with patch("Plugin.cli.bridge.subprocess.run", return_value=mock_proc):
            run("version", {}, blender_path="blender", verbose=True)
        captured = capsys.readouterr()
        assert "Blender startup noise" in captured.err


# ---------------------------------------------------------------------------
# find_blender tests
# ---------------------------------------------------------------------------


class TestFindBlender:
    def test_returns_env_var(self):
        with patch.dict(os.environ, {"USDSTAGE_BLENDER": "/custom/blender"}):
            assert find_blender() == "/custom/blender"

    def test_falls_back_to_blender(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("USDSTAGE_BLENDER", None)
            assert find_blender() == "blender"

    def test_empty_env_var_falls_back(self):
        with patch.dict(os.environ, {"USDSTAGE_BLENDER": ""}):
            assert find_blender() == "blender"


# ---------------------------------------------------------------------------
# OUTPUT_MARKER sanity
# ---------------------------------------------------------------------------


class TestOutputMarker:
    def test_marker_is_distinctive(self):
        """Marker should not appear in normal Blender output."""
        assert "USDSTAGE" in OUTPUT_MARKER
        assert len(OUTPUT_MARKER) > 10


def test_process_output_tails_are_redacted():
    """Captured Blender output carries absolute install and repo paths."""
    from pathlib import Path as _Path

    home = str(_Path.home())
    error = BridgeError(
        "Blender process failed.",
        code="BLENDER_PROCESS_FAILED",
        stdout_tail=f"reading {home}/scene.blend",
        stderr_tail=f'  File "{home}/Library/plugin/runner.py", line 1',
    )
    payload = error.to_json()

    assert home not in payload["process_output"]["stdout_tail"]
    assert home not in payload["process_output"]["stderr_tail"]
    assert "$HOME/scene.blend" in payload["process_output"]["stdout_tail"]
    assert "$HOME/Library/plugin/runner.py" in payload["process_output"]["stderr_tail"]


def test_verbose_stderr_forward_is_redacted(monkeypatch, capsys):
    """--verbose is what the docs tell users to pass for a support issue."""
    from pathlib import Path as _Path
    from types import SimpleNamespace

    home = str(_Path.home())
    completed = SimpleNamespace(
        stdout=f"{bridge.OUTPUT_MARKER}"
               '{"ok": true, "result": {}}'
               f"{bridge.OUTPUT_MARKER}",
        stderr=f'  File "{home}/Library/plugin/runner.py", line 1',
        returncode=0,
    )
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **k: completed)

    bridge.run("version", {}, blender_path="blender", verbose=True)

    captured = capsys.readouterr()
    assert home not in captured.err
    assert "$HOME/Library/plugin/runner.py" in captured.err
