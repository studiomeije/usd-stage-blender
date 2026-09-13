"""Diagnostics collector tests."""

from __future__ import annotations

import json
from pathlib import Path

from Plugin.export.diagnostics import ExportDiagnostics


def test_default_schema_contains_support_sections():
    diag = ExportDiagnostics()
    data = diag.to_dict()
    for key in (
        "timestamp",
        "materials",
        "textures",
        "animations",
        "export_context",
        "environment",
        "phases",
        "validation",
        "material_issues",
        "generated_files",
        "exceptions",
        "artifacts",
        "errors",
        "warnings",
    ):
        assert key in data


def test_phase_and_artifact_persistence(tmp_path: Path):
    diag = ExportDiagnostics()
    diag.set_export_context(command="export", resolved_output_path="/tmp/out.usdz")
    diag.set_environment(blender={"version": "5.0.0"})
    diag.set_artifact("diagnostics_path", "/tmp/out.diagnostics.json")
    diag.begin_phase("export")
    diag.end_phase("export", context={"file_size": 12})
    diag.add_generated_file("export", "/tmp/out.usdz")

    path = tmp_path / "nested" / "out.diagnostics.json"
    diag.save(path)

    data = json.loads(path.read_text())
    assert data["export_context"]["command"] == "export"
    assert data["environment"]["blender"]["version"] == "5.0.0"
    assert data["artifacts"]["diagnostics_path"] == "/tmp/out.diagnostics.json"
    assert data["phases"][0]["name"] == "export"
    assert data["generated_files"][0]["role"] == "export"


def test_validation_issue_is_structured():
    diag = ExportDiagnostics()
    diag.add_validation_issue(
        "Mat",
        {"node_name": "Noise", "node_type": "ShaderNodeTexNoise", "message": "Unsupported"},
    )

    issue = diag.to_dict()["validation"]["unsupported_nodes"][0]
    assert issue["material"] == "Mat"
    assert issue["node_name"] == "Noise"
    assert diag.to_dict()["material_issues"]["unsupported_nodes"][0]["message"] == "Unsupported"
