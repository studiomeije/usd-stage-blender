"""Pure, import-safe material compatibility policies.

These helpers operate on copied scalar values only.  They never assign to
Blender nodes or material datablocks, which keeps export repair non-destructive
and leaves the artist's source ``.blend`` unchanged.
"""

from __future__ import annotations

import math
from typing import Any


SPECULAR_TINT_NORMALIZATION_SETTING = "clamp_specular_tint"
SPECULAR_TINT_NORMALIZED_VALUE = (1.0, 1.0, 1.0)


def safe_overbright_achromatic_specular_tint(
    value: Any,
    *,
    linked: bool = False,
    epsilon: float = 1e-6,
) -> dict[str, object] | None:
    """Describe the one Specular Tint value that can be safely normalized.

    A linked value is never rewritten.  A constant is eligible only when its
    RGB channels are finite, achromatic, non-negative, and brighter than the
    supported unit range.  Colored values require artist judgment because a
    clamp would change their hue or saturation.
    """
    if linked:
        return None
    try:
        components = [float(component) for component in list(value)[:3]]
    except (TypeError, ValueError):
        return None
    if len(components) != 3 or not all(math.isfinite(item) for item in components):
        return None
    if min(components) < 0.0:
        return None
    if max(components) - min(components) > epsilon:
        return None
    if max(components) <= 1.0 + epsilon:
        return None
    return {
        "input": tuple(components),
        "output": SPECULAR_TINT_NORMALIZED_VALUE,
        "reason": "constant achromatic Specular Tint exceeds the supported [0, 1] range",
    }


def normalize_extracted_specular_tint(
    material_data: dict[str, Any],
) -> dict[str, object] | None:
    """Clamp an eligible extracted constant and return an audit record."""
    policy = safe_overbright_achromatic_specular_tint(
        material_data.get("specular_tint")
    )
    if policy is None:
        return None
    material_data["specular_tint"] = list(SPECULAR_TINT_NORMALIZED_VALUE)
    return policy


def format_color(value: Any) -> str:
    """Format an RGB value compactly for artist-facing diagnostics."""
    try:
        components = [float(component) for component in list(value)[:3]]
    except (TypeError, ValueError):
        return str(value)
    return "[" + ", ".join(f"{component:g}" for component in components) + "]"


def specular_tint_normalization_message(policy: dict[str, object]) -> str:
    """Return the prominent, non-destructive export warning."""
    return (
        "Export-only clamp applied: Principled 'Specular Tint' "
        f"{format_color(policy['input'])} was clamped to "
        f"{format_color(policy['output'])}. The source Blender material and "
        ".blend file were not changed. Review the result in Reality Composer Pro."
    )
