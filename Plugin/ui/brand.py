"""The USD Stage logo, loaded once for panel headers."""

from pathlib import Path

import bpy

_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "icons" / "usd_stage.png"
_previews = None


def icon_id() -> int:
    """The logo's icon id, or 0 when it is not loaded (background mode, missing file)."""
    if _previews is None or "usd_stage" not in _previews:
        return 0
    return _previews["usd_stage"].icon_id


def draw_header_icon(layout, fallback: str) -> None:
    """Draw the logo as a panel header icon, or ``fallback`` when it is unavailable."""
    logo = icon_id()
    if logo:
        layout.label(text="", icon_value=logo)
    else:
        layout.label(text="", icon=fallback)


def register():
    global _previews
    if bpy.app.background or _previews is not None or not _ICON_PATH.is_file():
        return
    from bpy.utils import previews

    _previews = previews.new()
    _previews.load("usd_stage", str(_ICON_PATH), 'IMAGE')


def unregister():
    global _previews
    if _previews is None:
        return
    from bpy.utils import previews

    previews.remove(_previews)
    _previews = None
