"""Regression coverage for extension package identity and standalone loading."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

from Plugin.core.package_bootstrap import load_extension_package


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_source_package_reuses_existing_plugin_module():
    package_name, module = load_extension_package(REPO_ROOT / "Plugin")

    assert package_name == "Plugin"
    assert module is sys.modules["Plugin"]


def test_copied_package_uses_stable_private_name_without_plugin_alias(tmp_path):
    copied_root = tmp_path / "copied-extension"
    copied_root.mkdir()
    (copied_root / "__init__.py").write_text("VALUE = 42\n")
    plugin_modules_before = {
        name: module
        for name, module in sys.modules.items()
        if name == "Plugin" or name.startswith("Plugin.")
    }

    first_name, first_module = load_extension_package(copied_root)
    second_name, second_module = load_extension_package(copied_root)

    assert first_name.startswith("_usdstage_runtime_")
    assert second_name == first_name
    assert second_module is first_module
    assert first_module.VALUE == 42
    assert {
        name: module
        for name, module in sys.modules.items()
        if name == "Plugin" or name.startswith("Plugin.")
    } == plugin_modules_before


def test_production_modules_do_not_import_plugin_absolute_name():
    violations = []
    for path in sorted((REPO_ROOT / "Plugin").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if imported == "Plugin" or imported.startswith("Plugin."):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "Plugin" or alias.name.startswith("Plugin."):
                        violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")

    assert violations == []
