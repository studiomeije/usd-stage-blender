"""Every UI module that can register must actually be registered.

`shader_authoring_panel.py` and `shader_menu.py` shipped for several releases
without being imported anywhere: `Plugin/ui/__init__.py` registered only
`panel` and `shader_panel`, so the "RealityKit Authoring" sidebar and the
`Add > RealityKit Nodes` menu never appeared, while README.md and
docs/ARCHITECTURE.md documented both as features. Nothing failed - the modules
were simply unreachable, and their operators were only findable through F3
search.

A module defining `register()` is a module meant to be registered, so assert
the wiring structurally rather than trusting a reviewer to notice the next one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


UI_DIR = Path(__file__).resolve().parents[2] / "Plugin" / "ui"


def _modules_defining_register() -> set[str]:
    modules = set()
    for source in UI_DIR.glob("*.py"):
        if source.name == "__init__.py":
            continue
        tree = ast.parse(source.read_text())
        if any(
            isinstance(node, ast.FunctionDef) and node.name == "register"
            for node in tree.body
        ):
            modules.add(source.stem)
    return modules


def _init_source() -> str:
    return (UI_DIR / "__init__.py").read_text()


def test_every_registerable_ui_module_is_imported_by_the_package():
    init = _init_source()
    missing = sorted(
        name for name in _modules_defining_register()
        if f"from . import {name} " not in init and f"from . import {name}\n" not in init
    )
    assert not missing, (
        f"Plugin/ui/{{{','.join(missing)}}}.py define register() but are never "
        "imported by Plugin/ui/__init__.py, so their panels and menus never "
        "appear in Blender"
    )


def test_every_registerable_ui_module_is_registered_and_unregistered():
    init = _init_source()
    tree = ast.parse(init)
    bodies = {
        node.name: ast.unparse(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"register", "unregister"}
    }
    assert set(bodies) == {"register", "unregister"}

    for name in sorted(_modules_defining_register()):
        for hook in ("register", "unregister"):
            assert f"_{name}.{hook}()" in bodies[hook], (
                f"Plugin/ui/{name}.py is imported but never {hook}ed"
            )


@pytest.mark.parametrize("module", ["shader_authoring_panel", "shader_menu"])
def test_documented_shader_editor_entry_points_are_wired(module):
    """Both were advertised in README.md while unreachable in Blender."""
    assert module in _modules_defining_register()
    assert f"from . import {module} as _{module}" in _init_source()
