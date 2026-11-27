"""Blender's anisotropic specular, carried onto RealityKit's PBR Surface 2.

Both are GGX with alpha = roughness squared, so matching the two alphas
matches the highlight. They split the alpha differently.

Cycles' Principled (``svm_node_closure_bsdf``) with Anisotropic ``A``:
``s = sqrt(1 - 0.9 A)``, ``alpha_t = r^2 / s`` along the tangent and
``alpha_b = r^2 s`` across it. Measured by rendering a metal sphere in Cycles
(Blender 5.3, GGX) and fitting both alphas: A = 0.25, 0.5 and 1 give
alpha_t = 0.103, 0.122, 0.293 and alpha_b = 0.080, 0.067, 0.029 at roughness
0.3, the formula's values within 3%.

RealityKit with ``specularAnisotropyLevel`` ``L``: ``alpha_t = r^2 (1 + L^2)``
and ``alpha_b = r^2 (1 - L^2)``. Measured on macOS 27.2.1 (26B131) by the
same fit on 460-pixel metal spheres: L = 0.4, 0.8, 0.9 and 0.95 at roughness
0.3 give alpha_t = 0.105, 0.148, 0.163, 0.169 and alpha_b = 0.076, 0.032,
0.017, 0.009, within 1%; L = 1 collapses the highlight to a line.

So the export authors ``L^2 = 0.9 A / (2 - 0.9 A)`` and scales roughness by
``sqrt((1/s + s) / 2)``, which reproduces both of Cycles' alphas: rendered
back in RealityKit, A = 0.5 and 1 at roughness 0.3 came out at
alpha = (0.122, 0.067) and (0.291, 0.028). The scaled roughness has to stay at
or below 1, RealityKit's range; above it the export stops.

Direction. Both stretch the highlight along the tangent. RealityKit's tangent
is the render UV map's +u; Cycles takes the Tangent input, so the export
requires it to come from a Tangent node in UV Map mode on the render UV map
(the default Tangent is radial around the object's Z axis, which RealityKit
cannot express). Blender's Anisotropic Rotation turns the tangent
counter-clockwise as the camera sees it, by that fraction of a turn (measured:
0.125 puts the highlight at +45 degrees from +u); RealityKit's
``specularAnisotropyAngle`` turns it clockwise by its fraction of a turn
(measured: 0.125 gives -45 degrees). So the angle is ``-rotation`` modulo 1.
Both were measured on UV layouts whose +v runs counter-clockwise of +u as the
camera sees them; on a mirrored UV island the two may turn opposite ways.
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Tuple

#: Cycles' Principled anisotropy aspect factor.
_CYCLES_ASPECT_FACTOR = 0.9

#: Inputs, per shader, that carry the anisotropy. The Principled names are
#: what the extraction stand-ins for Glossy and Metallic present too.
_LEVEL, _ROTATION, _TANGENT = "Anisotropic", "Anisotropic Rotation", "Tangent"


def realitykit_anisotropy(anisotropic: float, rotation: float) -> Tuple[float, float, float]:
    """(specularAnisotropyLevel, specularAnisotropyAngle, roughness scale)."""
    a = min(max(float(anisotropic), 0.0), 1.0)
    s = math.sqrt(1.0 - _CYCLES_ASPECT_FACTOR * a)
    level = math.sqrt(_CYCLES_ASPECT_FACTOR * a / (2.0 - _CYCLES_ASPECT_FACTOR * a))
    roughness_scale = math.sqrt((1.0 / s + s) / 2.0)
    angle = (-float(rotation)) % 1.0
    return level, angle, roughness_scale


def _socket(node, name):
    inputs = getattr(node, "inputs", None)
    return inputs.get(name) if inputs is not None else None


def _linked(socket) -> bool:
    return bool(socket is not None and getattr(socket, "is_linked", False))


def _constant(socket, default=0.0) -> float:
    try:
        return float(getattr(socket, "default_value", default))
    except (TypeError, ValueError):
        return default


def anisotropy_active(node) -> bool:
    level = _socket(node, _LEVEL)
    return _linked(level) or abs(_constant(level)) > 1e-6


def _tangent_source(socket):
    from .extract.core import _upstream_through_reroutes

    return _upstream_through_reroutes(socket)


def _tangent_refusal(node) -> Optional[str]:
    from .extract.core import uv_set_resolution

    tangent = _socket(node, _TANGENT)
    if not _linked(tangent):
        return (
            "'Tangent' is unlinked, so Cycles stretches the highlight around the "
            "object's Z axis, which RealityKit cannot express; link a Tangent node set "
            "to UV Map on the render UV map, whose +u RealityKit stretches along"
        )
    source, _output = _tangent_source(tangent)
    if getattr(source, "type", "") != "TANGENT" or str(
        getattr(source, "direction_type", "") or ""
    ).upper() != "UV_MAP":
        return (
            "'Tangent' is not a Tangent node in UV Map mode; RealityKit stretches the "
            "highlight along the render UV map's +u only, so link a Tangent node set to "
            "UV Map"
        )
    uv_map = (getattr(source, "uv_map", "") or "").strip()
    if uv_map:
        texcoord, refusal = uv_set_resolution(source, uv_map)
        if refusal is not None:
            return refusal
        if texcoord != "UV0":
            return (
                f"'Tangent' follows the UV map '{uv_map}', but RealityKit builds tangents "
                "from the render UV map only; make that UV map the render one or clear the "
                "Tangent node's UV Map field"
            )
    return None


def anisotropy_issues(node) -> List[str]:
    """Why a shader's anisotropy cannot export faithfully; empty when it can."""
    if not anisotropy_active(node):
        return []
    issues: List[str] = []
    for name in (_LEVEL, _ROTATION):
        if _linked(_socket(node, name)):
            issues.append(
                f"Principled '{name}' is linked; only a constant anisotropy is carried "
                "to RealityKit. Set a constant, or bake the material."
            )
    tangent = _tangent_refusal(node)
    if tangent is not None:
        issues.append(f"Principled anisotropy: {tangent}.")
    roughness = _socket(node, "Roughness")
    if not _linked(_socket(node, _LEVEL)) and not _linked(roughness):
        _level, _angle, scale = realitykit_anisotropy(_constant(_socket(node, _LEVEL)), 0.0)
        needed = _constant(roughness, 0.5) * scale
        if needed > 1.0 + 1e-6:
            issues.append(
                f"Principled anisotropy needs RealityKit roughness {needed:.3f} to match "
                f"Cycles' highlight at Roughness {_constant(roughness, 0.5):.3f}, above "
                "RealityKit's maximum of 1; lower Roughness or Anisotropic, or bake the "
                "material."
            )
    return issues


def roughness_notice(node) -> Optional[str]:
    """The warning for a linked Roughness under anisotropy, or None."""
    if not anisotropy_active(node) or not _linked(_socket(node, "Roughness")):
        return None
    _level, _angle, scale = realitykit_anisotropy(_constant(_socket(node, _LEVEL)), 0.0)
    return (
        f"Principled Roughness is linked under anisotropy; the export multiplies it by "
        f"{scale:.3f} to match Cycles' highlight, and RealityKit clamps the result at 1, "
        f"so texels above {1.0 / scale:.3f} render less rough than in Cycles."
    )


def tangent_node_feeds_only_anisotropy(node) -> bool:
    """A Tangent node whose every link ends in a shader's Tangent input.

    Such a node is not translated itself: it only declares the frame the
    anisotropy uses, which ``anisotropy_issues`` checks.
    """
    links: List[Any] = []
    for output in getattr(node, "outputs", ()) or ():
        links.extend(getattr(output, "links", ()) or ())
    return bool(links) and all(
        getattr(getattr(link, "to_socket", None), "name", "") == _TANGENT for link in links
    )
