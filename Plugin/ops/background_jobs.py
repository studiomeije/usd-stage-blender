"""Status and process checks shared by the background bake job and the panel.

This module does not import bpy, so the export operator and the sidebar can
use it without loading the background operator.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

ACTIVE_JOB_STATES = frozenset({"queued", "running"})

#: Popen handles for background jobs launched by this Blender session, keyed by
#: pid. A dead direct child stays a zombie until it is reaped, and
#: ``os.kill(pid, 0)`` still succeeds on a zombie. ``Popen.poll()`` reports the
#: real state and reaps the child. Only this session's own children can be
#: polled; a pid from a previous session falls back to the signal probe.
_BACKGROUND_PROCESSES: dict = {}


def remember_background_process(proc) -> None:
    """Track a launched background job so its exit can be detected."""
    _BACKGROUND_PROCESSES[int(proc.pid)] = proc
    # Drop handles for children that have already exited, so a long-lived
    # Blender session does not accumulate them.
    for pid in [p for p, handle in _BACKGROUND_PROCESSES.items() if handle.poll() is not None]:
        if pid != int(proc.pid):
            _BACKGROUND_PROCESSES.pop(pid, None)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False

    proc = _BACKGROUND_PROCESSES.get(int(pid))
    if proc is not None:
        try:
            if proc.poll() is None:
                return True
            _BACKGROUND_PROCESSES.pop(int(pid), None)
            return False
        except Exception:
            pass

    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    except Exception:
        return False


def read_job_status(job_dir: str):
    """The job's status.json, or None when it is missing or unreadable."""
    if not job_dir:
        return None
    status_path = Path(job_dir) / "status.json"
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def safe_int(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def status_pid(status) -> int | None:
    if not isinstance(status, dict) or status.get("pid") is None:
        return None
    return safe_int(status.get("pid"))
