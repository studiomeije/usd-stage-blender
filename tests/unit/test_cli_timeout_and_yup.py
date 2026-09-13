"""Regression tests for the Blender 5.2 CLI release-blocker fixes."""

from __future__ import annotations

import json
import threading
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.cli.__main__ import CLIUsageError, build_parser
from Plugin.cli.bridge import (
    BridgeError,
    OUTPUT_MARKER,
    StepTimeoutWatchdog,
    extract_result,
)


def _run_parsed(argv: list[str]):
    parsed = build_parser().parse_args(argv)
    with patch(
        "Plugin.cli.__main__.bridge.run",
        return_value={"duration_seconds": 0.0},
    ) as mocked_run:
        assert parsed.func(parsed) == 0
    return mocked_run.call_args.kwargs


@pytest.mark.parametrize("command", ["export", "bake-export"])
def test_apply_yup_is_not_a_public_cli_option(command):
    with pytest.raises(CLIUsageError):
        build_parser().parse_args(
            [command, "scene.blend", "-o", "out.usdc", "--apply-yup"]
        )


def test_step_timeout_is_sent_to_the_bake_worker(capsys):
    call = _run_parsed(
        ["bake-export", "scene.blend", "-o", "out.usdc", "--step-timeout", "45"]
    )

    assert call["args"]["timeout"] == 45
    # The per-step timeout does not set the independent overall timeout,
    # which bake-export leaves unlimited by default.
    assert call["timeout"] is None


def test_global_timeout_zero_remains_unlimited(capsys):
    call = _run_parsed(["--timeout", "0", "version"])

    assert call["timeout"] is None


@pytest.mark.parametrize(
    "argv",
    [
        ["--timeout", "-1", "version"],
        [
            "bake-export",
            "scene.blend",
            "-o",
            "out.usdc",
            "--step-timeout",
            "-1",
        ],
    ],
)
def test_negative_timeouts_are_rejected(argv):
    with pytest.raises(CLIUsageError):
        build_parser().parse_args(argv)


def test_settings_get_help_lists_materials_group(capsys):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["settings", "get", "--help"])

    assert exc_info.value.code == 0
    assert "materials" in capsys.readouterr().out


def test_validate_forwards_one_shot_safe_normalization_policy(capsys):
    call = _run_parsed(
        [
            "validate",
            "scene.blend",
            "--clamp-specular-tint",
        ]
    )

    assert call["args"]["clamp_specular_tint"] is True


def test_step_timeout_watchdog_reports_the_current_step_and_exit_code():
    timed_out = threading.Event()
    recorded: dict[str, object] = {}

    def on_timeout(step: str, elapsed: float, limit: float) -> None:
        recorded.update(step=step, elapsed=elapsed, limit=limit)

    def fake_exit(code: int) -> None:
        recorded["exit_code"] = code
        timed_out.set()

    watchdog = StepTimeoutWatchdog(
        0.04,
        on_timeout,
        exit_func=fake_exit,
        poll_interval=0.01,
    )
    watchdog.start("Baking Base Color")

    assert timed_out.wait(1.0)
    watchdog.stop()
    assert recorded["step"] == "Baking Base Color"
    assert recorded["elapsed"] >= 0.04
    assert recorded["limit"] == 0.04
    assert recorded["exit_code"] == StepTimeoutWatchdog.EXIT_CODE


def test_step_timeout_hard_exits_when_reporting_callback_blocks():
    callback_started = threading.Event()
    release_callback = threading.Event()
    hard_exit = threading.Event()
    exit_codes: list[int] = []

    def blocking_callback(_step: str, _elapsed: float, _limit: float) -> None:
        callback_started.set()
        release_callback.wait(5.0)

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        hard_exit.set()

    watchdog = StepTimeoutWatchdog(
        0.02,
        blocking_callback,
        exit_func=fake_exit,
        poll_interval=0.01,
        callback_exit_grace_seconds=0.03,
    )
    watchdog.start("Writing timeout diagnostics")

    assert callback_started.wait(1.0)
    assert hard_exit.wait(1.0)
    assert exit_codes == [StepTimeoutWatchdog.EXIT_CODE]

    # Let the callback's thread unwind and prove its normal finally path cannot
    # emit a second hard exit after the guard already won the race.
    release_callback.set()
    watchdog.stop()
    assert exit_codes == [StepTimeoutWatchdog.EXIT_CODE]


def test_disabled_step_timeout_never_starts_a_worker_thread():
    watchdog = StepTimeoutWatchdog(
        0,
        lambda *_args: (_ for _ in ()).throw(AssertionError("must stay disabled")),
        exit_func=lambda _code: None,
    )

    watchdog.start("Baking")
    assert watchdog.enabled is False
    assert watchdog._thread is None
    watchdog.stop()


def _marked(response: dict) -> str:
    return f"{OUTPUT_MARKER}{json.dumps(response)}{OUTPUT_MARKER}"


def test_timeout_envelope_wins_over_racing_success_response():
    success = {"ok": True, "result": {"export_path": "out.usdc"}}
    timeout = {
        "ok": False,
        "error": {
            "code": "BAKE_STEP_TIMEOUT",
            "message": "Exporting USD timed out",
        },
    }

    with pytest.raises(BridgeError) as exc_info:
        extract_result(_marked(success) + _marked(timeout), "", 124)

    assert exc_info.value.response["error"]["code"] == "BAKE_STEP_TIMEOUT"


def test_timeout_envelope_is_authoritative_regardless_of_marker_order():
    timeout = {
        "ok": False,
        "error": {
            "code": "BAKE_STEP_TIMEOUT",
            "message": "Packaging USDZ timed out",
        },
    }
    success = {"ok": True, "result": {"export_path": "out.usdz"}}

    with pytest.raises(BridgeError) as exc_info:
        extract_result(_marked(timeout) + _marked(success), "", 124)

    assert exc_info.value.response["error"]["code"] == "BAKE_STEP_TIMEOUT"


def test_nonzero_process_exit_discards_a_success_envelope():
    success = {"ok": True, "result": {"export_path": "out.usdc"}}

    with pytest.raises(BridgeError) as exc_info:
        extract_result(_marked(success), "", 124)

    assert exc_info.value.response["error"]["code"] == "BLENDER_PROCESS_FAILED"
