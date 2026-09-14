"""Shared fixtures for USD Stage for Blender CLI tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Resolved paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "Plugin"

BLEND_FIXTURES: dict[str, Path] = {
    "RedCube": REPO_ROOT / "References" / "Blender" / "t22_red_cube.blend",
    "CubeWith4Animations": REPO_ROOT / "References" / "Blender" / "t23_cube_with_4_animations.blend",
    "SkinnedLimb": REPO_ROOT / "References" / "Blender" / "t12_skinned_limb.blend",
    "SpecularTint": REPO_ROOT / "References" / "Blender" / "t21_specular_tint_refusal.blend",
}


# ---------------------------------------------------------------------------
# Blender availability
# ---------------------------------------------------------------------------

def _blender_available() -> bool:
    """Check if Blender is reachable."""
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    return shutil.which(blender) is not None


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when Blender is not available."""
    if _blender_available():
        return
    skip_marker = pytest.mark.skip(reason="Blender not available (set USDSTAGE_BLENDER)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def blend_file() -> Path:
    """Default .blend fixture — RedCube."""
    path = BLEND_FIXTURES["RedCube"]
    assert path.exists(), f"Fixture not found: {path}"
    return path


@pytest.fixture
def tmp_output(tmp_path) -> Path:
    """Temporary directory for export outputs."""
    return tmp_path


@dataclass
class CLIResult:
    """Result from a CLI invocation."""
    exit_code: int
    stdout: str
    stderr: str
    json: dict | list | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@pytest.fixture
def run_cli():
    """Fixture that returns a function to invoke the CLI via subprocess.

    Usage::

        result = run_cli("info", "scene.blend")
        result = run_cli("export", "scene.blend", "-o", "/tmp/out.usdz")
        result = run_cli("--json", "version")
    """
    def _run(*args: str, timeout: int = 120) -> CLIResult:
        cmd = [sys.executable, str(PLUGIN_DIR)] + list(args)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Try to parse stdout as JSON
        parsed = None
        stdout = proc.stdout.strip()
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return CLIResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            json=parsed,
        )
    return _run
