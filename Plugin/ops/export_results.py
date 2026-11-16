"""Last-export feedback shared by operators and the sidebar, without bpy."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .background_jobs import read_job_status


def record_result(settings, *, state, export_path="", diagnostics_path="",
                  data=None, message="", timestamp=None):
    result = {
        "state": state,
        "time": time.time() if timestamp is None else timestamp,
        "export_path": str(export_path or ""),
        "diagnostics_path": str(diagnostics_path or ""),
        "message": message,
        "data": data or {},
    }
    result["warnings"] = list(result["data"].get("warnings") or [])
    result["errors"] = list(result["data"].get("errors") or [])
    settings.last_export_result = json.dumps(result, default=str)
    return result


def get_latest_result(settings, background_status=None):
    try:
        direct = json.loads(getattr(settings, "last_export_result", "") or "{}")
    except (TypeError, ValueError):
        direct = {}
    if not isinstance(direct, dict):
        direct = {}
    background = background_status
    if background is None:
        background = read_job_status(getattr(settings, "background_job_dir", ""))
    if not isinstance(background, dict):
        background = {}
    def timestamp(result):
        try:
            return float(result.get("time") or 0)
        except (TypeError, ValueError):
            return 0
    return dict(background if timestamp(background) >= timestamp(direct) and background else direct)


def retain_background_result(settings, status, *, stale_message="Background job is no longer running."):
    """Keep terminal feedback before its job-directory attachment is removed.

    Copy the status rather than rebuilding it so warnings, log paths and report
    availability survive dismissal. A stale active status is a new failure,
    while an older terminal job must not replace a newer foreground result.
    """
    result = dict(status) if isinstance(status, dict) else {}
    if result.get("state") not in {"done", "error", "canceled"}:
        result.update(state="error", time=time.time(), message=stale_message,
                      diagnostics_available=False)
        result.setdefault("export_path", str(getattr(settings, "filepath", "") or ""))
    latest = get_latest_result(settings, background_status=result)
    settings.last_export_result = json.dumps(latest, default=str)
    return latest


def resolve_diagnostics_path(settings, background_status=None):
    result = get_latest_result(settings, background_status)
    if result:
        if result.get("diagnostics_available") is False:
            return ""
        path = result.get("diagnostics_path")
        # A result without a sidecar still owns the current feedback. Never
        # silently substitute an older failure report for a newer success.
        return str(path) if path and Path(str(path)).is_file() else ""
    candidates = []
    output = str(getattr(settings, "filepath", "") or "").strip()
    if output:
        candidates.append(str(Path(output).with_suffix(".diagnostics.json")))
    candidates.append(getattr(settings, "last_diagnostics_path", ""))
    return next((str(path) for path in candidates if path and Path(str(path)).is_file()), "")


def result_diagnostics(result):
    data = result.get("data")
    if isinstance(data, dict) and data:
        return data
    return {
        "errors": list(result.get("errors") or []) or (
            [result.get("message")] if result.get("state") == "error" and result.get("message") else []
        ),
        "warnings": list(result.get("warnings") or []),
    }


def diagnostic_errors(data):
    """Readable errors, including structured material refusals."""
    errors = [str(item) for item in data.get("errors", []) or []]
    issues = (data.get("validation") or {}).get("unsupported_nodes") or []
    for issue in issues:
        if issue.get("severity") == "warning":
            continue
        message = ": ".join(str(issue.get(key)) for key in ("material", "node_name", "message") if issue.get(key))
        if message and message not in errors:
            errors.append(message)
    return errors
