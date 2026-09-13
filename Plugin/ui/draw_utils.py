"""Shared UI drawing helpers for USD Stage for Blender panels and dialogs."""

from __future__ import annotations


def draw_issue_list(layout, items, *, title, icon, alert=False, max_items=8, format_item=str):
    """Draw a titled, truncated list of error/warning items.

    Single source for the issue-list card used by the Shader Editor
    validation panel and the diagnostics dialog, so truncation and styling
    can't drift between them. ``alert=True`` renders the block in Blender's
    error styling.
    """
    if not items:
        return
    box = layout.box()
    header = box.row()
    header.alert = alert
    header.label(text=title, icon=icon)
    column = box.column(align=True)
    column.alert = alert
    for item in items[:max_items]:
        column.label(text=format_item(item))
    if len(items) > max_items:
        box.label(text=f"... {len(items) - max_items} more")
