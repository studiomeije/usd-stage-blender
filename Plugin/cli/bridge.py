"""
Blender bridge — spawns ``blender --background`` and parses the JSON result.

All CLI commands go through this module. It handles:
- Locating the Blender binary
- Constructing the command line
- Extracting the JSON result from Blender's noisy stdout
- Error handling and timeouts
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

OUTPUT_MARKER = "---USDSTAGE_JSON---"


def redact_home(text: str) -> str:
    """Replace the user's home directory with ``$HOME``.

    Deliberately duplicated from ``api.runner._redact_home`` rather than
    imported: this module runs in the host interpreter and keeps no Plugin
    imports, while the runner runs inside Blender. Same placeholder as the
    support bundle so redacted output reads consistently.
    """
    if not text:
        return text
    try:
        home = str(Path.home())
    except Exception:
        return text
    return text.replace(home, "$HOME") if home else text
# runner.py is at Plugin/api/runner.py — one level up from cli/
RUNNER_PATH = str(Path(__file__).resolve().parent.parent / "api" / "runner.py")


def write_timeout_diagnostics(
    diagnostics_path: str | None,
    base_payload: dict,
    timeout_details: dict,
    message: str,
) -> None:
    """Atomically publish a timeout-only diagnostic snapshot.

    The main Blender thread may itself be blocked while writing the ordinary
    diagnostics file. Writing a private snapshot and replacing the destination
    avoids interleaved/truncated JSON and never touches the live diagnostics
    collector from the watchdog thread.
    """
    if not diagnostics_path:
        return
    target = Path(diagnostics_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = copy.deepcopy(base_payload)
    payload["timeout"] = copy.deepcopy(timeout_details)
    payload.setdefault("errors", []).append(message)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.timeout.tmp"
    )
    try:
        temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


class StepTimeoutWatchdog:
    """Worker-owned watchdog for a sequence of blocking export steps.

    Blender's bake and USD operators block the main Python thread, so a normal
    ``try``/``except`` timeout cannot interrupt them.  This watchdog runs on a
    daemon thread and invokes ``on_timeout`` when the *current* step exceeds its
    limit.  It then terminates the worker process so a wedged Blender operator
    cannot carry on mutating the scene after an error was reported.

    ``timeout_seconds <= 0`` disables the watchdog.  ``exit_func`` and the
    callback grace period are injectable solely so the timing and callback
    contract can be unit-tested without terminating pytest.
    """

    EXIT_CODE = 124
    CALLBACK_EXIT_GRACE_SECONDS = 1.0

    def __init__(
        self,
        timeout_seconds: float | int,
        on_timeout: Callable[[str, float, float], None],
        *,
        exit_func: Callable[[int], None] = os._exit,
        poll_interval: float = 0.25,
        callback_exit_grace_seconds: float = CALLBACK_EXIT_GRACE_SECONDS,
    ) -> None:
        self.timeout_seconds = max(0.0, float(timeout_seconds or 0))
        self.on_timeout = on_timeout
        self._exit_func = exit_func
        self._poll_interval = max(0.01, float(poll_interval))
        self._callback_exit_grace_seconds = max(
            0.01,
            float(callback_exit_grace_seconds),
        )
        self._lock = threading.Lock()
        self._exit_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._callback_finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._hard_exit_thread: threading.Thread | None = None
        self._step = "Preparing bake/export"
        self._step_started = time.monotonic()
        self._triggered = False
        self._exit_started = False

    @property
    def enabled(self) -> bool:
        return self.timeout_seconds > 0

    def start(self, step: str = "Preparing bake/export") -> None:
        if not self.enabled or self._thread is not None:
            return
        self.enter_step(step)
        self._thread = threading.Thread(
            target=self._run,
            name="USDStageStepTimeout",
            daemon=True,
        )
        self._thread.start()

    def enter_step(self, step: str) -> None:
        """Start timing ``step``; repeated reports for it do not reset time."""
        label = str(step or "Unknown bake/export step")
        with self._lock:
            if label == self._step and self._thread is not None:
                return
            self._step = label
            self._step_started = time.monotonic()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=max(1.0, self._poll_interval * 2.0))
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval):
            with self._lock:
                step = self._step
                elapsed = max(0.0, time.monotonic() - self._step_started)
                if self._triggered or elapsed < self.timeout_seconds:
                    continue
                self._triggered = True

            # Diagnostics and status files may live on a stalled external or
            # network volume. Arm an independent hard-exit path *before* the
            # callback performs any I/O, so reporting can never defeat the
            # timeout that it is trying to report.
            self._hard_exit_thread = threading.Thread(
                target=self._hard_exit_after_callback_grace,
                name="USDStageHardExit",
                daemon=True,
            )
            try:
                self._hard_exit_thread.start()
            except Exception:
                # Thread creation itself is allowed to fail closed: a timed-out
                # worker must not resume scene or output mutation.
                self._exit_once()

            try:
                self.on_timeout(step, elapsed, self.timeout_seconds)
            finally:
                self._callback_finished.set()
                # A timed-out bpy operation may still own the main thread. A
                # hard worker exit is the only reliable way to prevent it from
                # resuming after the timeout response/status has been flushed.
                self._exit_once()
            return

    def _hard_exit_after_callback_grace(self) -> None:
        if not self._callback_finished.wait(self._callback_exit_grace_seconds):
            self._exit_once()

    def _exit_once(self) -> None:
        """Invoke the hard-exit function once across both watchdog threads."""
        with self._exit_lock:
            if self._exit_started:
                return
            self._exit_started = True
        self._exit_func(self.EXIT_CODE)


class BridgeError(RuntimeError):
    """RuntimeError with structured Blender subprocess context."""

    def __init__(
        self,
        message: str,
        *,
        response: dict | None = None,
        stdout_tail: str = "",
        stderr_tail: str = "",
        returncode: int | None = None,
        command: str | None = None,
        blend_file: str | None = None,
        blender_path: str | None = None,
        code: str = "BLENDER_BRIDGE_FAILED",
    ):
        super().__init__(message)
        self.response = response
        self.stdout_tail = stdout_tail
        self.stderr_tail = stderr_tail
        self.returncode = returncode
        self.command = command
        self.blend_file = blend_file
        self.blender_path = blender_path
        self.code = code

    @property
    def error_code(self) -> str:
        """Return the stable error code used by CLI exit classification."""
        if isinstance(self.response, dict):
            error = self.response.get("error")
            if isinstance(error, dict) and error.get("code"):
                return str(error["code"])
        return self.code

    def to_json(self) -> dict:
        if self.response:
            payload = dict(self.response)
        else:
            payload = {
                "ok": False,
                "schema_version": "1.0",
                "command": self.command,
                "error": {
                    "code": self.error_code,
                    "type": self.__class__.__name__,
                    "message": str(self),
                },
                "context": {},
                "artifacts": {},
            }
        payload.setdefault("context", {})
        payload["context"].update({
            "blend_file": self.blend_file,
            "blender_path": self.blender_path,
            "returncode": self.returncode,
        })
        if self.stdout_tail or self.stderr_tail:
            payload.setdefault("process_output", {})
            # Captured Blender output carries absolute install and repository
            # paths, and this envelope is what users paste into public issues.
            # The support bundle already redacts $HOME before writing anything;
            # do the same here.
            if self.stdout_tail:
                payload["process_output"]["stdout_tail"] = redact_home(self.stdout_tail)
            if self.stderr_tail:
                payload["process_output"]["stderr_tail"] = redact_home(self.stderr_tail)
        return payload


def find_blender() -> str:
    """Resolve the Blender binary path.

    Priority:
    1. ``--blender`` CLI flag (handled by caller, passed as argument)
    2. ``USDSTAGE_BLENDER`` environment variable
    3. ``blender`` on PATH
    """
    env = os.environ.get("USDSTAGE_BLENDER")
    if env:
        return env
    return "blender"


def run(
    command: str,
    args: dict,
    blend_file: str | None = None,
    blender_path: str | None = None,
    timeout: int | None = 600,
    verbose: bool = False,
) -> dict:
    """Execute a command via ``blender --background`` and return the result dict.

    Parameters
    ----------
    command:
        The API command name (e.g. ``"export"``, ``"validate"``).
    args:
        Arguments dict passed to the command handler.
    blend_file:
        Optional ``.blend`` file to open before running.
    blender_path:
        Path to the Blender executable. If ``None`` uses :func:`find_blender`.
    timeout:
        Maximum seconds to wait for Blender to finish.
    verbose:
        If ``True``, Blender's stderr is printed to the terminal.

    Returns
    -------
    dict
        The ``result`` value from the API response on success.

    Raises
    ------
    RuntimeError
        On any failure (Blender not found, command error, timeout, etc.).
    """
    blender = blender_path or find_blender()
    payload = json.dumps({"command": command, "args": args})

    cmd: list[str] = [blender, "--background"]
    if command == "bake_export":
        cmd.append("--factory-startup")
    if blend_file:
        cmd.append(blend_file)
    cmd.extend(["--python", RUNNER_PATH, "--", payload])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
    except FileNotFoundError:
        raise BridgeError(
            f"Blender not found at '{blender}'. "
            "Set USDSTAGE_BLENDER or use --blender <path>.",
            command=command,
            blend_file=blend_file,
            blender_path=blender,
            code="BLENDER_NOT_FOUND",
        ) from None
    except subprocess.TimeoutExpired:
        raise BridgeError(
            f"Blender timed out after {timeout}s.",
            command=command,
            blend_file=blend_file,
            blender_path=blender,
            code="BLENDER_TIMEOUT",
        ) from None
    except OSError as exc:
        # ``subprocess.run`` can fail before Blender starts for reasons other
        # than a missing path: a directory or non-executable file was supplied,
        # execute permission was denied, or the OS rejected the executable
        # format.  Keep those failures inside the structured CLI contract.
        detail = exc.strerror or str(exc)
        raise BridgeError(
            f"Blender failed to start at '{blender}': {detail}.",
            command=command,
            blend_file=blend_file,
            blender_path=blender,
            code="BLENDER_START_FAILED",
        ) from None

    if verbose:
        if proc.stderr:
            # Redacted like the copy carried inside the error envelope.
            # --verbose is exactly what the docs tell users to pass when
            # capturing output for a support issue, so this stream is the one
            # most likely to be pasted somewhere public.
            print(redact_home(proc.stderr), file=sys.stderr)

    try:
        return extract_result(proc.stdout or "", proc.stderr or "", proc.returncode, blender)
    except BridgeError as exc:
        exc.command = command
        exc.blend_file = blend_file
        exc.blender_path = blender
        raise


def extract_result(
    stdout: str,
    stderr: str,
    returncode: int,
    blender: str = "blender",
) -> dict:
    """Extract the JSON result from Blender's stdout.

    Split out from :func:`run` so it can be unit-tested without spawning
    a subprocess.

    Returns
    -------
    dict
        The ``result`` value from the API response on success.

    Raises
    ------
    RuntimeError
        On any failure (missing markers, invalid JSON, command error, etc.).
    """
    pattern = re.escape(OUTPUT_MARKER) + r"(.+?)" + re.escape(OUTPUT_MARKER)
    matches = re.findall(pattern, stdout, re.DOTALL)

    if not matches:
        snippet = (stderr or stdout)[-500:]
        if returncode == 127:
            raise BridgeError(
                f"Blender not found at '{blender}'. "
                "Set USDSTAGE_BLENDER or use --blender <path>.",
                stdout_tail=stdout[-500:],
                stderr_tail=stderr[-500:],
                returncode=returncode,
                blender_path=blender,
                code="BLENDER_NOT_FOUND",
            )
        raise BridgeError(
            f"No output from Blender (exit code {returncode}). "
            f"Last output:\n{snippet}",
            stdout_tail=stdout[-500:],
            stderr_tail=stderr[-500:],
            returncode=returncode,
            blender_path=blender,
            code="BLENDER_PROCESS_FAILED",
        )

    responses = []
    parse_errors = []
    for raw_response in matches:
        try:
            candidate = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            parse_errors.append(exc)
            continue
        if isinstance(candidate, dict):
            responses.append(candidate)

    if not responses:
        detail = parse_errors[-1] if parse_errors else "response was not a JSON object"
        raise BridgeError(
            f"Failed to parse Blender output: {detail}",
            stdout_tail=stdout[-500:],
            stderr_tail=stderr[-500:],
            returncode=returncode,
            blender_path=blender,
        )

    def _error_code(response: dict) -> str | None:
        error = response.get("error")
        if isinstance(error, dict) and error.get("code"):
            return str(error["code"])
        return None

    # A watchdog can fire on the success boundary, leaving both a normal API
    # response and the timeout envelope in stdout. Never report that race as a
    # success: the worker's termination and BAKE_STEP_TIMEOUT are authoritative.
    timeout_response = next(
        (
            response
            for response in reversed(responses)
            if _error_code(response) == "BAKE_STEP_TIMEOUT"
        ),
        None,
    )
    response = timeout_response or responses[-1]

    if returncode != 0 and response.get("ok"):
        error_response = next(
            (candidate for candidate in reversed(responses) if not candidate.get("ok")),
            None,
        )
        if error_response is not None:
            response = error_response
        else:
            message = (
                f"Blender exited with code {returncode} after reporting success; "
                "the result was discarded."
            )
            response = {
                "ok": False,
                "schema_version": "1.0",
                "error": {
                    "code": "BLENDER_PROCESS_FAILED",
                    "type": "BridgeError",
                    "message": message,
                },
                "context": {"returncode": returncode},
                "artifacts": {},
            }

    if not response.get("ok"):
        error = response.get("error", "Unknown error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "Unknown error"
        else:
            message = str(error)
        raise BridgeError(
            message,
            response=response,
            stdout_tail=stdout[-500:],
            stderr_tail=stderr[-500:],
            returncode=returncode,
            blender_path=blender,
        )

    return response["result"]
