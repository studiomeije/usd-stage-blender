"""Integration test — the shipped CLI exports the variant fixture correctly.

Everything else tests a piece. This drives ``usdstage export`` over the
committed t39 fixture, which is the whole path a user takes: a .blend whose
variant sets were declared in the panel, reopened in another process where the
add-on registers only after the file is loaded.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "References" / "Blender" / "t39_seat_variants.blend"


def _usdstage(*args, timeout=600):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), *args],
        capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> dict:
    assert FIXTURE.exists(), f"fixture not built: {FIXTURE}"
    out_dir = tmp_path_factory.mktemp("t39")
    output = out_dir / "seat.usda"

    result = _usdstage(
        "export", str(FIXTURE), "diagnostics_enabled=true",
        "-o", str(output), "--format", "USDA",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.exists(), result.stdout + result.stderr
    return {
        "stage": output,
        "diagnostics": output.with_suffix(".diagnostics.json"),
    }


def _children(stage_path: Path, selection: str | None) -> list[str]:
    from pxr import Usd

    stage = Usd.Stage.Open(str(stage_path))
    root = stage.GetDefaultPrim()
    if selection is not None:
        root.GetVariantSet("Seat").SetVariantSelection(selection)
    return sorted(child.GetName() for child in root.GetChildren())


def test_the_declared_set_reaches_the_exported_stage(exported):
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(exported["stage"]))
    root = stage.GetDefaultPrim()

    assert root.GetVariantSets().GetNames() == ["Seat"]
    assert root.GetVariantSet("Seat").GetVariantSelection() == "Fabric"


def test_the_hidden_variant_was_staged_into_the_export(exported):
    """Leather is excluded from the fixture's view layer."""
    pytest.importorskip("pxr")

    assert "SeatLeather" in _children(exported["stage"], "Leather")


def test_each_selection_composes_only_its_own_seat(exported):
    pytest.importorskip("pxr")

    fabric = _children(exported["stage"], "Fabric")
    leather = _children(exported["stage"], "Leather")

    assert "SeatFabric" in fabric and "SeatLeather" not in fabric
    assert "SeatLeather" in leather and "SeatFabric" not in leather


def test_the_plinth_belongs_to_no_variant_and_always_ships(exported):
    pytest.importorskip("pxr")

    for selection in ("Fabric", "Leather"):
        assert "Plinth" in _children(exported["stage"], selection)


def test_the_diagnostics_sidecar_records_what_was_authored(exported):
    payload = json.loads(exported["diagnostics"].read_text())

    assert payload["variant_sets"] == [
        {
            "name": "Seat",
            "kind": "GEOMETRY",
            "default_variant": "Fabric",
            "variants": [
                {"name": "Fabric", "collection": "Fabric", "objects": ["SeatFabric"]},
                {
                    "name": "Leather",
                    "collection": "Leather",
                    "objects": ["SeatLeather"],
                },
            ],
        }
    ]


def test_a_clean_variant_export_warns_about_nothing(exported):
    payload = json.loads(exported["diagnostics"].read_text())

    assert payload["errors"] == []
    assert payload["warnings"] == []


def test_info_reports_the_declared_sets_without_exporting(exported):
    result = _usdstage("info", str(FIXTURE), "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["variant_sets"] == [
        {
            "name": "Seat",
            "default_variant": "Fabric",
            "variants": [
                {"name": "Fabric", "collection": "Fabric"},
                {"name": "Leather", "collection": "Leather"},
            ],
        }
    ]


def test_turning_variants_off_exports_what_the_viewport_shows(tmp_path):
    """No variant set, and no staging either: the hidden seat stays out."""
    pytest.importorskip("pxr")
    output = tmp_path / "flat.usda"

    result = _usdstage(
        "export", str(FIXTURE), "export_variants=false",
        "-o", str(output), "--format", "USDA",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    from pxr import Usd

    stage = Usd.Stage.Open(str(output))
    root = stage.GetDefaultPrim()
    assert root.GetVariantSets().GetNames() == []
    children = sorted(child.GetName() for child in root.GetChildren())
    assert "Plinth" in children
    assert "SeatFabric" in children
    assert "SeatLeather" not in children
