"""The USD Stage logo ships with the add-on and heads the panels.

The icon is a real PNG under ``Plugin/assets/icons`` (not a Git LFS pointer),
the release archive requires it, and the export and Shader Editor panels draw
it through ``brand.draw_header_icon``, which falls back to a built-in icon when
previews are unavailable, as in background mode.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ICON = ROOT / "Plugin" / "assets" / "icons" / "usd_stage.png"


def test_the_icon_is_a_png_the_release_archive_requires():
    assert ICON.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    source = (ROOT / "scripts" / "release_archive.py").read_text()
    assert '"assets/icons/usd_stage.png"' in source


def test_the_export_and_shader_editor_panels_draw_the_logo():
    for name in ("panel.py", "shader_panel.py", "shader_authoring_panel.py"):
        tree = ast.parse((ROOT / "Plugin" / "ui" / name).read_text())
        headers = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "draw_header"
            and "brand.draw_header_icon" in ast.unparse(node)
        ]
        assert headers, f"{name} has no panel header drawing the USD Stage logo"
