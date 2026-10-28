"""Documentation that lists code keeps listing what the code has."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("pxr")

from Plugin.export import postprocess_usd  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_the_architecture_page_lists_every_post_processing_pass_in_order():
    text = (DOCS / "ARCHITECTURE.md").read_text()
    section = text.split("### Post-processing passes", 1)[1].split("\n## ", 1)[0]
    documented = re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE)

    assert documented == [name for name, _run, _keep in postprocess_usd.PASSES]
