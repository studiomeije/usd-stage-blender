"""Changing the Blender scene for an export step, and putting it back.

A bake switches the render engine to Cycles, needs Object Mode, and changes
the selection; the user's scene must come back exactly as it was even when
the bake fails. Each helper here does one such change or restores one, and
never raises: a restore that fails must not hide the error that made it run.
"""

from __future__ import annotations

import bpy


def ensure_object_mode(context) -> None:
    """Leave Edit, Sculpt or any other mode for Object Mode."""
    active = context.view_layer.objects.active
    if active and active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def set_render_engine(scene, engine: str) -> None:
    """Switch the render engine, ignoring an engine this Blender lacks."""
    try:
        scene.render.engine = engine
    except Exception:
        pass


def restore_selection(context, selection, active) -> None:
    """Select exactly ``selection`` and make ``active`` the active object."""
    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        pass
    for obj in selection:
        try:
            obj.select_set(True)
        except Exception:
            pass
    if active:
        try:
            context.view_layer.objects.active = active
        except Exception:
            pass


def restore_mode(context, active, mode: str) -> None:
    """Make ``active`` active again and return it to ``mode``."""
    if not active:
        return
    try:
        context.view_layer.objects.active = active
    except Exception:
        return
    try:
        if mode and active.mode != mode:
            bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass
