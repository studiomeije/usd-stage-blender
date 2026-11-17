"""Last-result feedback must stay complete and attached to the right export."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

# Import a bpy-free helper without executing Plugin.ops package registration.
ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "_export_results_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "Plugin" / "ops")]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.export_results", ROOT / "Plugin" / "ops" / "export_results.py")
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)


def settings(**values):
    return SimpleNamespace(last_export_result="", background_job_dir="", filepath="", last_diagnostics_path="", **values)


def test_latest_result_preserves_all_warnings_without_sidecar():
    state = settings()
    warnings = [f"Warning {i}" for i in range(12)]
    results.record_result(state, state="done", data={"warnings": warnings}, timestamp=10)
    assert results.result_diagnostics(results.get_latest_result(state))["warnings"] == warnings


def test_newer_background_result_wins_and_older_one_does_not():
    state = settings()
    results.record_result(state, state="done", export_path="direct.usdz", timestamp=20)
    assert results.get_latest_result(state, {"state": "error", "time": 10})["export_path"] == "direct.usdz"
    assert results.get_latest_result(state, {"state": "error", "time": 30})["state"] == "error"


def test_success_without_sidecar_never_displays_old_failure(tmp_path):
    old = tmp_path / "scene.diagnostics.json"
    old.write_text('{"errors": ["Old failure"]}')
    state = settings()
    state.filepath = str(tmp_path / "scene.usdz")
    state.last_diagnostics_path = str(old)
    results.record_result(state, state="done", timestamp=20)
    assert results.resolve_diagnostics_path(state) == ""


def test_fallback_report_choice_prefers_current_output(tmp_path):
    current = tmp_path / "current.diagnostics.json"
    previous = tmp_path / "previous.diagnostics.json"
    current.write_text("{}")
    previous.write_text("{}")
    state = settings()
    state.filepath = str(tmp_path / "current.usdz")
    state.last_diagnostics_path = str(previous)
    assert results.resolve_diagnostics_path(state) == str(current)


def test_material_errors_are_readable_when_general_errors_are_empty():
    data = {"errors": [], "validation": {"unsupported_nodes": [
        {"severity": "error", "material": "Glass", "node_name": "Glass BSDF", "message": "Unsupported node"},
        {"severity": "warning", "material": "Paint", "message": "Warning"},
    ]}}
    assert results.diagnostic_errors(data) == ["Glass: Glass BSDF: Unsupported node"]


def test_corrupt_session_result_falls_back_to_background():
    state = settings()
    state.last_export_result = "broken JSON"
    assert results.get_latest_result(state, {"time": 5, "state": "done"})["state"] == "done"


def test_background_failure_without_report_has_readable_error():
    assert results.result_diagnostics({"state": "error", "message": "Worker exited"})["errors"] == ["Worker exited"]


def test_worker_without_current_diagnostics_never_reuses_existing_sidecar(tmp_path):
    old = tmp_path / "old.diagnostics.json"
    old.write_text('{"errors": ["Old failure"]}')
    background = {
        "state": "done", "time": 20,
        "diagnostics_path": str(old), "diagnostics_available": False,
    }
    assert results.resolve_diagnostics_path(settings(), background) == ""
    del background["diagnostics_available"]
    assert results.resolve_diagnostics_path(settings(), background) == str(old)
