"""Tests for Blender-hosted API runner behavior."""

from __future__ import annotations

import json
import math
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.api import runner  # noqa: E402
from Plugin.api.errors import CommandError, json_safe  # noqa: E402


class _FakeRNA:
    identifier = "ShaderNodeBsdfPrincipled"


class _FakeValidationNode:
    name = "Principled BSDF"
    type = "BSDF_PRINCIPLED"
    bl_idname = "ShaderNodeBsdfPrincipled"
    bl_rna = _FakeRNA()

    def __repr__(self):
        return "<_FakeValidationNode object at 0xDEADBEEF>"


def test_runner_outputs_structured_json_when_addon_load_fails(monkeypatch, capsys):
    request = json.dumps({"command": "export", "args": {"filepath": "/tmp/out.usdz"}})
    monkeypatch.setattr(sys, "argv", ["runner.py", "--", request])

    def fail_load():
        raise RuntimeError("addon load failed")

    monkeypatch.setattr(runner, "_ensure_addon_loaded", fail_load)

    code = runner.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out.split(runner.OUTPUT_MARKER)[1])

    assert code == 1
    assert payload["ok"] is False
    assert payload["command"] == "export"
    assert payload["error"]["code"] == "ADDON_LOAD_FAILED"
    assert payload["error"]["stage"] == "addon_load"
    assert payload["error"]["message"] == "addon load failed"


def test_runner_reports_registry_import_as_stable_addon_load_failure(
    monkeypatch,
    capsys,
):
    request = json.dumps({"command": "version", "args": {}})
    monkeypatch.setattr(sys, "argv", ["runner.py", "--", request])

    original_import_module = runner.importlib.import_module

    def routed_import(name):
        if name.endswith(".api.commands"):
            raise ImportError("registry dependency missing")
        return original_import_module(name)

    monkeypatch.setattr(runner.importlib, "import_module", routed_import)

    code = runner.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out.split(runner.OUTPUT_MARKER)[1])

    assert code == 1
    assert payload["error"]["code"] == "ADDON_LOAD_FAILED"
    assert payload["error"]["stage"] == "command_registry"


def test_json_safe_projects_rna_objects_cycles_paths_sets_and_nonfinite_values(tmp_path):
    cyclic: dict = {}
    cyclic["self"] = cyclic
    payload = {
        "node": _FakeValidationNode(),
        "path": tmp_path / "scene.usdz",
        "tuple": (1, 2),
        "set": {"z", "a"},
        "nan": math.nan,
        "positive_infinity": math.inf,
        "negative_infinity": -math.inf,
        "decimal_nan": Decimal("NaN"),
        "cyclic": cyclic,
    }

    projected = json_safe(payload)
    encoded = json.dumps(projected, allow_nan=False, sort_keys=True)

    assert projected["node"] == {
        "__type__": "_FakeValidationNode",
        "name": "Principled BSDF",
        "type": "BSDF_PRINCIPLED",
        "bl_idname": "ShaderNodeBsdfPrincipled",
        "rna_identifier": "ShaderNodeBsdfPrincipled",
    }
    assert projected["path"] == str(tmp_path / "scene.usdz")
    assert projected["tuple"] == [1, 2]
    assert projected["set"] == ["a", "z"]
    assert projected["nan"] == "NaN"
    assert projected["positive_infinity"] == "Infinity"
    assert projected["negative_infinity"] == "-Infinity"
    assert projected["decimal_nan"] == "NaN"
    assert projected["cyclic"]["self"] == "<cycle>"
    assert "0xDEADBEEF" not in encoded


def test_json_safe_stops_at_the_configured_depth():
    nested = {"one": {"two": {"three": "value"}}}

    assert json_safe(nested, max_depth=2) == {
        "one": {"two": "<max-depth>"},
    }


def test_command_error_metadata_is_json_safe_at_construction(tmp_path):
    cyclic = []
    cyclic.append(cyclic)
    error = CommandError(
        "Unsupported node",
        details=[{"node": _FakeValidationNode(), "cycle": cyclic}],
        context={"value": math.nan},
        artifacts={"output": tmp_path / "scene.usdz"},
    )

    payload = error.to_response_error()

    assert payload["details"][0]["node"]["name"] == "Principled BSDF"
    assert payload["details"][0]["cycle"] == ["<cycle>"]
    assert error.context == {"value": "NaN"}
    assert error.artifacts == {"output": str(tmp_path / "scene.usdz")}
    json.dumps(payload, allow_nan=False)


def test_runner_success_boundary_serializes_live_validation_node(monkeypatch, capsys):
    request = json.dumps({"command": "probe", "args": {}})
    monkeypatch.setattr(sys, "argv", ["runner.py", "--", request])

    cyclic = {}
    cyclic["self"] = cyclic
    registry = SimpleNamespace(
        REGISTRY={
            "probe": lambda _args: {
                "issue": {"node": _FakeValidationNode()},
                "cycle": cyclic,
            }
        }
    )
    original_import_module = runner.importlib.import_module
    monkeypatch.setattr(
        runner.importlib,
        "import_module",
        lambda name: registry if name.endswith(".api.commands") else original_import_module(name),
    )
    monkeypatch.setattr(runner, "_ensure_addon_loaded", lambda: None)

    code = runner.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out.split(runner.OUTPUT_MARKER)[1])

    assert code == 0
    assert payload["ok"] is True
    assert payload["result"]["issue"]["node"]["name"] == "Principled BSDF"
    assert payload["result"]["cycle"]["self"] == "<cycle>"


def test_runner_error_boundary_serializes_command_error_metadata(monkeypatch, capsys):
    request = json.dumps({"command": "probe", "args": {}})
    monkeypatch.setattr(sys, "argv", ["runner.py", "--", request])

    def fail(_args):
        cyclic = []
        cyclic.append(cyclic)
        raise CommandError(
            "Unsupported node",
            code="UNSUPPORTED_MATERIAL_NODES",
            details=[{"node": _FakeValidationNode(), "cycle": cyclic}],
        )

    registry = SimpleNamespace(REGISTRY={"probe": fail})
    original_import_module = runner.importlib.import_module
    monkeypatch.setattr(
        runner.importlib,
        "import_module",
        lambda name: registry if name.endswith(".api.commands") else original_import_module(name),
    )
    monkeypatch.setattr(runner, "_ensure_addon_loaded", lambda: None)

    code = runner.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out.split(runner.OUTPUT_MARKER)[1])

    assert code == 1
    assert payload["error"]["code"] == "UNSUPPORTED_MATERIAL_NODES"
    assert payload["error"]["details"][0]["node"]["name"] == "Principled BSDF"
    assert payload["error"]["details"][0]["cycle"] == ["<cycle>"]


# ---------------------------------------------------------------------------
# Error envelopes must not leak the install layout.
#
# _error_response attached a full Python traceback to every failure, including
# deliberate CommandErrors like a typo'd setting key, and never redacted it.
# The support bundle has redacted $HOME since it was written; this envelope,
# which is what users paste into public issues, did not.
# ---------------------------------------------------------------------------


def test_command_error_carries_no_traceback():
    """A diagnosed user-facing condition is not an internal fault."""
    error = runner._error_response(
        "settings_set",
        CommandError("Invalid setting key.", code="INVALID_SETTING_OVERRIDE"),
        tb="Traceback (most recent call last):\n  File \"/Users/someone/x.py\"\n",
    )
    assert "traceback" not in error["error"]
    assert error["error"]["code"] == "INVALID_SETTING_OVERRIDE"


def test_unexpected_fault_keeps_a_redacted_traceback():
    home = str(Path.home())
    error = runner._error_response(
        "export",
        RuntimeError("boom"),
        tb=f'Traceback:\n  File "{home}/Library/x/plugin.py", line 1\n',
    )
    traceback_text = error["error"]["traceback"]
    assert home not in traceback_text
    assert "$HOME/Library/x/plugin.py" in traceback_text


def test_redaction_is_a_noop_without_a_home_match():
    assert runner._redact_home("/opt/somewhere/else.py") == "/opt/somewhere/else.py"
