"""The recorded Apple conformance results stay in step with the probes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = REPO_ROOT / "scripts" / "apple_conformance.json"


def _probe_names():
    pytest.importorskip("pxr")
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_apple_conformance import PROBES

    return set(PROBES)


def test_every_probe_has_a_recorded_result():
    """Adding a probe without recording it would make the check silently weaker."""
    assert EXPECTATIONS.exists(), "run scripts/check_apple_conformance.py --update"
    recorded = set(json.loads(EXPECTATIONS.read_text()))

    missing = _probe_names() - recorded
    assert not missing, (
        f"these probes have no recorded result: {sorted(missing)}. "
        "Run scripts/check_apple_conformance.py --update."
    )


def test_no_recorded_result_outlives_its_probe():
    recorded = set(json.loads(EXPECTATIONS.read_text()))

    orphaned = recorded - _probe_names()
    assert not orphaned, f"recorded results with no probe: {sorted(orphaned)}"


def test_the_known_variant_finding_is_still_recorded():
    """The result the project most depends on knowing."""
    recorded = json.loads(EXPECTATIONS.read_text())

    for name in ("variant_activation", "variant_binding", "variant_nested"):
        reality = recorded[name]["reality"]
        assert reality["configuration_sets"] == [], (
            f"{name}: realitytool now reports configurations; "
            "re-read APPLE_CONFORMANCE.md before trusting it"
        )
