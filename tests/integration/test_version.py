"""Integration test — usdstage version."""

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


class TestVersion:
    def test_has_plugin_version(self, run_cli):
        result = run_cli("version")
        assert result.ok
        assert "plugin" in result.json
        assert isinstance(result.json["plugin"], str)
        assert len(result.json["plugin"]) > 0

    def test_has_blender_version(self, run_cli):
        result = run_cli("version")
        assert result.ok
        assert "blender" in result.json
        assert isinstance(result.json["blender"], str)
        assert len(result.json["blender"]) > 0

    def test_has_python_version(self, run_cli):
        result = run_cli("version")
        assert result.ok
        assert "python" in result.json
        assert isinstance(result.json["python"], str)
        assert len(result.json["python"]) > 0


def test_cli_entrypoint_works_when_extension_folder_is_named_usd_stage(tmp_path: Path):
    plugin = Path(__file__).resolve().parents[2] / "Plugin"
    installed = tmp_path / "usd_stage"
    shutil.copytree(plugin, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    proc = subprocess.run(
        [sys.executable, str(installed), "--json", "version"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    manifest = tomllib.loads((plugin / "blender_manifest.toml").read_text())
    assert json.loads(proc.stdout)["plugin"] == manifest["version"]
