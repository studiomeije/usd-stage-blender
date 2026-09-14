"""Tests for CLI exit code mapping — no Blender required."""

from __future__ import annotations

import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

_WINDOWS_NO_PY_BLENDER = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake Blender is a .py script, which Windows cannot run as the Blender executable",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.cli.__main__ import main  # noqa: E402
from Plugin.cli.bridge import BridgeError  # noqa: E402


COMMAND_INVOCATIONS = [
    pytest.param(("version",), id="version"),
    pytest.param(("info", "scene.blend"), id="info"),
    pytest.param(("list-objects", "scene.blend"), id="list-objects"),
    pytest.param(("list-materials", "scene.blend"), id="list-materials"),
    pytest.param(("validate", "scene.blend"), id="validate"),
    pytest.param(("settings", "get", "scene.blend"), id="settings-get"),
    pytest.param(
        ("settings", "set", "scene.blend", "export_format=USDZ"),
        id="settings-set",
    ),
    pytest.param(("settings", "list"), id="settings-list"),
    pytest.param(("export", "scene.blend", "-o", "out.usdz"), id="export"),
    pytest.param(
        ("bake-export", "scene.blend", "-o", "out.usdz"),
        id="bake-export",
    ),
    pytest.param(("support-bundle", "scene.blend"), id="support-bundle"),
    pytest.param(("preferences", "get"), id="preferences-get"),
    pytest.param(
        ("preferences", "set", "default_export_format=USDZ"),
        id="preferences-set",
    ),
]


def _run_main(*args: str) -> tuple[int, str, str]:
    """Run main() with given args, capturing stdout/stderr and return (exit_code, stdout, stderr)."""
    captured_out = StringIO()
    captured_err = StringIO()
    with patch("sys.argv", ["usdstage"] + list(args)), \
         patch("sys.stdout", captured_out), \
         patch("sys.stderr", captured_err):
        exit_code = main()
    return exit_code, captured_out.getvalue(), captured_err.getvalue()


# ---------------------------------------------------------------------------
# Exit code mapping
# ---------------------------------------------------------------------------


class TestExitCodeMapping:
    def test_blender_not_found_returns_2(self):
        error = BridgeError(
            "Unable to locate the configured executable.",
            code="BLENDER_NOT_FOUND",
        )
        with patch("Plugin.cli.__main__.bridge.run", side_effect=error):
            code, _, stderr = _run_main("version")
            assert code == 2
            assert "Unable to locate" in stderr

    def test_plugin_not_loaded_returns_3(self):
        error = BridgeError(
            "USD Stage for Blender add-on could not be loaded. Attempted: usd_stage.",
            response={
                "ok": False,
                "error": {
                    "code": "ADDON_LOAD_FAILED",
                    "message": "USD Stage for Blender add-on could not be loaded.",
                },
            },
        )
        with patch("Plugin.cli.__main__.bridge.run", side_effect=error):
            code, _, stderr = _run_main("version")
            assert code == 3
            assert "add-on could not be loaded" in stderr

    def test_failed_to_import_returns_3(self):
        error = BridgeError(
            "Registry initialization failed.",
            code="ADDON_LOAD_FAILED",
        )
        with patch("Plugin.cli.__main__.bridge.run", side_effect=error):
            code, _, stderr = _run_main("version")
            assert code == 3
            assert "Registry initialization failed" in stderr

    def test_message_text_does_not_control_exit_classification(self):
        with patch(
            "Plugin.cli.__main__.bridge.run",
            side_effect=RuntimeError("Blender not found and plugin not loaded"),
        ):
            code, _, _ = _run_main("version")
            assert code == 1

    def test_generic_error_returns_1(self):
        with patch("Plugin.cli.__main__.bridge.run", side_effect=RuntimeError("Something unexpected")):
            code, _, stderr = _run_main("version")
            assert code == 1
            assert "Something unexpected" in stderr

    def test_keyboard_interrupt_returns_130(self):
        with patch("Plugin.cli.__main__.bridge.run", side_effect=KeyboardInterrupt):
            code, _, stderr = _run_main("version")
            assert code == 130
            assert "Aborted" in stderr

    def test_happy_path_returns_0(self):
        with patch("Plugin.cli.__main__.bridge.run", return_value={"version": "1.0"}):
            code, stdout, stderr = _run_main("version")
            assert code == 0
            parsed = json.loads(stdout)
            assert parsed["version"] == "1.0"

    @pytest.mark.parametrize("invocation", COMMAND_INVOCATIONS)
    def test_all_commands_return_1_for_structured_failure(self, invocation):
        failure = {
            "ok": False,
            "error": {
                "code": "EXPECTED_REJECTION",
                "message": "The command was rejected.",
            },
        }
        with patch("Plugin.cli.__main__.bridge.run", return_value=failure):
            code, stdout, stderr = _run_main("--json", *invocation)

        assert code == 1
        assert json.loads(stdout) == failure
        assert stderr == ""

    @pytest.mark.parametrize("invocation", COMMAND_INVOCATIONS)
    def test_all_commands_return_0_for_structured_success(self, invocation):
        success = {"ok": True, "value": "completed"}
        with patch("Plugin.cli.__main__.bridge.run", return_value=success):
            code, stdout, stderr = _run_main("--json", *invocation)

        assert code == 0
        assert json.loads(stdout) == success
        assert stderr == ""


class TestJsonErrorOutput:
    def test_json_mode_wraps_error(self):
        """With --json flag, errors should be output as JSON on stdout."""
        with patch("Plugin.cli.__main__.bridge.run", side_effect=RuntimeError("Test error")):
            code, stdout, stderr = _run_main("--json", "version")
            assert code == 1
            parsed = json.loads(stdout)
            assert parsed["ok"] is False
            assert parsed["error"]["code"] == "CLI_RUNTIME_ERROR"
            assert "Test error" in parsed["error"]["message"]
            # stderr should be suppressed in json mode
            assert stderr == ""

    def test_json_mode_blender_not_found(self):
        error = BridgeError(
            "Blender not found at '/x'.",
            code="BLENDER_NOT_FOUND",
        )
        with patch("Plugin.cli.__main__.bridge.run", side_effect=error):
            code, stdout, stderr = _run_main("--json", "version")
            assert code == 2
            parsed = json.loads(stdout)
            assert parsed["ok"] is False
            assert parsed["error"]["code"] == "BLENDER_NOT_FOUND"
            assert "Blender not found" in parsed["error"]["message"]
            assert stderr == ""

    def test_json_mode_happy_path(self):
        with patch("Plugin.cli.__main__.bridge.run", return_value={"key": "val"}):
            code, stdout, stderr = _run_main("--json", "version")
            assert code == 0
            parsed = json.loads(stdout)
            assert parsed["key"] == "val"

    def test_json_mode_preserves_bridge_error_envelope(self):
        error = BridgeError(
            "Postprocess failed",
            response={
                "ok": False,
                "schema_version": "1.0",
                "command": "export",
                "error": {"code": "POSTPROCESS_FAILED", "message": "Postprocess failed"},
                "artifacts": {"diagnostics_path": "/tmp/out.diagnostics.json"},
            },
            returncode=1,
            command="export",
        )
        with patch("Plugin.cli.__main__.bridge.run", side_effect=error):
            code, stdout, stderr = _run_main("--json", "export", "scene.blend", "-o", "out.usdz")
            assert code == 1
            parsed = json.loads(stdout)
            assert parsed["error"]["code"] == "POSTPROCESS_FAILED"
            assert parsed["artifacts"]["diagnostics_path"] == "/tmp/out.diagnostics.json"
            assert stderr == ""


@_WINDOWS_NO_PY_BLENDER
@pytest.mark.parametrize(
    ("command_result", "expected_exit_code"),
    [
        pytest.param(
            {
                "ok": False,
                "error": {
                    "code": "EXPECTED_REJECTION",
                    "message": "The export was rejected.",
                },
            },
            1,
            id="structured-failure",
        ),
        pytest.param(
            {
                "ok": True,
                "export_path": "/tmp/out.usdz",
                "duration_seconds": 0.1,
            },
            0,
            id="structured-success",
        ),
    ],
)
def test_plugin_entrypoint_propagates_structured_result_exit_code(
    tmp_path: Path,
    command_result: dict,
    expected_exit_code: int,
):
    """Exercise ``python Plugin`` through a fake Blender process boundary."""
    marker = "---USDSTAGE_JSON---"
    runner_response = json.dumps({"ok": True, "result": command_result})
    fake_blender = tmp_path / "fake_blender.py"
    fake_blender.write_text(
        "#!/usr/bin/env python3\n"
        f"print({marker!r} + {runner_response!r} + {marker!r})\n",
        encoding="utf-8",
    )
    fake_blender.chmod(0o755)

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "Plugin"),
            "--json",
            "--blender",
            str(fake_blender),
            "export",
            "scene.blend",
            "-o",
            str(tmp_path / "out.usdz"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == expected_exit_code, proc.stderr
    assert json.loads(proc.stdout) == command_result
    assert proc.stderr == ""


@_WINDOWS_NO_PY_BLENDER
def test_plugin_entrypoint_maps_stable_addon_load_code_to_exit_3(tmp_path: Path):
    marker = "---USDSTAGE_JSON---"
    runner_response = json.dumps(
        {
            "ok": False,
            "schema_version": "1.0",
            "command": "version",
            "error": {
                "code": "ADDON_LOAD_FAILED",
                "message": "Extension initialization was rejected.",
            },
            "context": {},
            "artifacts": {},
        }
    )
    fake_blender = tmp_path / "fake_blender.py"
    fake_blender.write_text(
        "#!/usr/bin/env python3\n"
        f"print({marker!r} + {runner_response!r} + {marker!r})\n",
        encoding="utf-8",
    )
    fake_blender.chmod(0o755)

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "Plugin"),
            "--json",
            "--blender",
            str(fake_blender),
            "version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 3
    assert proc.stderr == ""
    payload = json.loads(proc.stdout)
    assert payload["error"]["code"] == "ADDON_LOAD_FAILED"
    assert payload["error"]["message"] == "Extension initialization was rejected."


@pytest.mark.parametrize(
    ("args", "expected_exit_code", "expected_error_code"),
    [
        pytest.param(
            ["export", "scene.blend"],
            1,
            "INVALID_ARGUMENTS",
            id="argparse-missing-required-output",
        ),
        pytest.param(
            ["export", "scene.blend", "not-an-override", "-o", "out.usdc"],
            1,
            "INVALID_OVERRIDE",
            id="malformed-export-override",
        ),
        pytest.param(
            ["bake-export", "scene.blend", "not-an-override", "-o", "out.usdc"],
            1,
            "INVALID_OVERRIDE",
            id="malformed-bake-override",
        ),
        pytest.param(
            ["settings", "set", "scene.blend", "not-a-setting"],
            1,
            "INVALID_SETTING_FORMAT",
            id="malformed-setting",
        ),
        pytest.param(
            ["preferences", "set", "not-a-preference"],
            1,
            "INVALID_PREFERENCE_FORMAT",
            id="malformed-preference",
        ),
    ],
)
def test_json_local_failures_are_stdout_only_structured_envelopes(
    args: list[str],
    expected_exit_code: int,
    expected_error_code: str,
):
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, str(repo_root / "Plugin"), "--json", *args],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == expected_exit_code
    assert proc.stderr == ""
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["schema_version"] == "1.0"
    assert payload["error"]["code"] == expected_error_code
    assert payload["error"]["message"]


def test_non_executable_blender_path_is_json_only_startup_failure():
    """A valid path that cannot be executed must not escape as a traceback."""
    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "Plugin"),
            "--json",
            "--blender",
            str(repo_root),
            "version",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert proc.stderr == ""
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BLENDER_START_FAILED"
    assert payload["context"]["blender_path"] == str(repo_root)
