"""The diagnostics report is strict JSON whatever was recorded into it."""

from __future__ import annotations

import json
import math
from pathlib import Path

from Plugin.export.diagnostics import ExportDiagnostics, json_value


def test_non_finite_numbers_are_spelled_out():
    assert json_value({"a": math.nan, "b": [math.inf, -math.inf], "c": 1.5}) == {
        "a": "NaN", "b": ["Infinity", "-Infinity"], "c": 1.5,
    }


def test_a_saved_report_parses_strictly_even_with_raw_values(tmp_path):
    diag = ExportDiagnostics()
    # Stored raw, the way a setter that does not sanitize would store it.
    diag.set_asset_metadata({"extents_hint": [[math.nan, 0.0, 0.0], [1.0, 1.0, 1.0]],
                             "where": Path("/tmp/out.usdz")})
    path = tmp_path / "report.json"
    diag.save(path)

    def refuse_constant(name):
        raise ValueError(f"non-standard JSON constant {name}")

    payload = json.loads(path.read_text(), parse_constant=refuse_constant)
    assert payload["asset_metadata"]["extents_hint"][0][0] == "NaN"
    assert payload["asset_metadata"]["where"] == "/tmp/out.usdz"
    assert json.loads(diag.to_json(), parse_constant=refuse_constant)
