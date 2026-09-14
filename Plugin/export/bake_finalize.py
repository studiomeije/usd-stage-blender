"""Shared post-bake material-mode policy.

Orientation is handled exclusively by Blender's native USD conversion and the
non-configurable Apple spatial contract. Baking never mutates source geometry.
"""

from __future__ import annotations


def resolve_force_unlit(settings) -> bool:
    """Return whether baked materials should be authored as RealityKit Unlit.

    ``LIT_ALBEDO`` authors Lit PBR so RealityKit lights the baked color. The
    other modes stay Unlit: ``UNLIT_ALBEDO`` by design and ``LIT_IBL`` because
    it already includes Blender lighting and shadows in the baked texture.
    """
    return str(getattr(settings, "bake_mode", "LIT_IBL")) != "LIT_ALBEDO"


def apply_force_unlit(settings) -> None:
    """Apply the export-local material mode without touching source materials."""
    settings.force_unlit_materials = resolve_force_unlit(settings)
