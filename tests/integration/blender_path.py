"""The Blender executable the integration tests drive."""

from __future__ import annotations

import os
import shutil

import pytest


def blender_executable() -> str:
    """Resolve ``$USDSTAGE_BLENDER`` (or ``blender``), skipping when absent."""
    resolved = shutil.which(os.environ.get("USDSTAGE_BLENDER", "blender"))
    if resolved is None:  # pragma: no cover - guarded by the integration marker
        pytest.skip("Blender not available")
    return resolved
