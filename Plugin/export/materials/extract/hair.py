"""The Principled Hair BSDF, exported as RealityKit's hair surface."""

from typing import Any, Dict, List, Optional

from . import core as _core


# ---------------------------------------------------------------------------
# Hair: a Principled Hair BSDF surface, exported as RealityKit's hair surface.
#
# ``ND_realitykit_hair_surfaceshader`` is a two-lobe hair model - a primary and
# a secondary specular with their own colour, roughness and shift, plus a
# backlit term - where Blender's Principled Hair is a full multi-lobe
# scattering model (Chiang or Huang). Four controls map one to one; the rest
# have no input on the platform surface and are named in a warning rather than
# approximated.
#
# ``tangent`` is the strand direction the two lobes shift along, in the
# surface's tangent frame: its setter is ``set_hair_tangent_space_tangent``,
# and the fragment stage turns it into a world direction with the same UV
# tangent frame it uses for a normal map. The nodedef default ``(0, 1, 0)`` is
# +V. Measured with RealityRenderer on macOS 27 on a UV sphere whose U runs
# around it: a constant ``(1, 0, 0)`` gives the highlight of fibres laid along
# U, ``(0, 1, 0)`` that of fibres along V, and both turn with the sphere when
# it is rolled; ``(0, 0, 1)`` and an object-space direction read from a
# primvar give no strand highlight at all. So the export authors the constant
# ``(1, 0, 0)``, which makes the strand direction an artist convention: each
# strand or card has to run along +U of the mesh's UV set. ``normal`` is left
# alone, exactly as PBR Surface 2's is for a material with no normal map.
# ---------------------------------------------------------------------------

#: The strand direction in the surface's tangent frame: +U.
HAIR_STRAND_TANGENT = (1.0, 0.0, 0.0)

#: The hair surface's lobe weight that renders like Blender's unscaled lobe.
#: ``primarySpecular`` and ``secondarySpecular`` share PBR Surface 2's
#: ``set_specular``, whose 0.5 is the 4% reflectance of a dielectric; measured
#: with RealityRenderer, both lobes grow linearly with the weight and saturate
#: at 1, so a Huang weight of 1 maps to 0.5 and a weight of 2 is the brightest
#: lobe the platform draws.
HAIR_LOBE_WEIGHT_SCALE = 0.5

_HAIR_NODE_TYPES = frozenset({'BSDF_HAIR_PRINCIPLED', 'BSDF_HAIR'})

#: Blender socket -> the hair-surface inputs it feeds, and its resolved type.
#: Roughness feeds both lobes: the platform surface has one roughness per lobe
#: and Blender has one longitudinal roughness for all of them.
_HAIR_MAPPED_SOCKETS = (
    ('Color', ('baseColor',), 'color3'),
    ('Roughness', ('primaryRoughness', 'secondaryRoughness'), 'float'),
    # Huang-model only: Blender disables both under Chiang, where the lobes
    # carry no separate weight, so nothing is authored for them there.
    ('Reflection', ('primarySpecular',), 'float'),
    ('Secondary Reflection', ('secondarySpecular',), 'float'),
)

#: Principled Hair inputs the platform surface has no form for. A linked one
#: is dropped, which is the silent case worth naming.
_HAIR_UNMAPPED_SOCKETS = (
    'Melanin', 'Melanin Redness', 'Tint', 'Absorption Coefficient', 'Aspect Ratio',
    'Radial Roughness', 'Coat', 'IOR', 'Offset', 'Random Color', 'Random Roughness',
    'Random', 'Transmission',
)

_HAIR_PARAMETRIZATION_LABELS = {
    'MELANIN': "Melanin concentration",
    'ABSORPTION': "Absorption coefficient",
}


def _enabled_hair_socket(node, name):
    """A hair BSDF input by name, only while Blender has it enabled.

    Two measured facts drive this. ``node.inputs.get(name)`` returns None for
    these sockets even when iterating ``node.inputs`` shows them, so the lookup
    has to walk the collection; and Blender disables the sockets that belong to
    the other model or colour parametrization - the lobe weights are Huang-only
    and Melanin is Melanin-only - which means a disabled socket carries a
    resting value that changes nothing in the render and must not be exported.
    """
    for socket in getattr(node, "inputs", []) or []:
        if getattr(socket, "name", "") != name:
            continue
        return socket if getattr(socket, "enabled", True) else None
    return None


def hair_surface_node(material):
    """The hair BSDF on the active Material Output, or None."""
    surface = _core._get_surface_shader_node(material)
    if surface is not None and getattr(surface, "type", "") in _HAIR_NODE_TYPES:
        return surface
    return None


def hair_refusal(material) -> Optional[str]:
    """Why the material's hair surface cannot be exported, or None."""
    node = hair_surface_node(material)
    if node is None:
        return None
    name = getattr(node, "name", "Hair BSDF")
    if getattr(node, "type", "") == 'BSDF_HAIR':
        return (
            f"Hair BSDF '{name}' is not exported; it is a single-lobe Cycles shader with no "
            "RealityKit form. Use Principled Hair BSDF, or bake the material."
        )
    parametrization = (getattr(node, "parametrization", "COLOR") or "COLOR").upper()
    label = _HAIR_PARAMETRIZATION_LABELS.get(parametrization)
    if label is not None:
        return (
            f"Principled Hair BSDF '{name}' uses {label} coloring, which has no colour to read: "
            "converting it needs the scattering model RealityKit's hair surface does not run. "
            "Set Color parametrization to Direct coloring, or bake the material."
        )
    return None


def hair_notices(material) -> List[str]:
    """Warnings an exportable hair surface always carries."""
    node = hair_surface_node(material)
    if node is None or hair_refusal(material) is not None:
        return []
    notices = [
        "Principled Hair BSDF exports as RealityKit's hair surface, a two-lobe approximation: "
        "Color and Roughness are carried, and the Huang model's two lobe weights with them, "
        "while the transmission lobe, the cuticle Offset and the radial roughness have no "
        "input on it, so the look will not match Cycles.",
        "The hair highlight follows +U of the mesh's UV set: the export gives RealityKit's hair "
        "surface that strand direction in the surface's tangent frame, so each strand or card "
        "must run along +U, and a mesh with no UV set has no strand direction."
    ]
    if (getattr(node, "model", "CHIANG") or "CHIANG").upper() == 'HUANG':
        notices.append(
            "The Huang model's Aspect Ratio is dropped; RealityKit's hair surface has no "
            "elliptical cross-section. Its Reflection and Secondary Reflection weights do map."
        )
    dropped = []
    for socket_name in _HAIR_UNMAPPED_SOCKETS:
        socket = _enabled_hair_socket(node, socket_name)
        if socket is not None and getattr(socket, "is_linked", False):
            dropped.append(socket_name)
    if dropped:
        notices.append(
            "These linked Principled Hair inputs are dropped, since the hair surface has no "
            "input for them: " + ", ".join(dropped) + "."
        )
    return notices


def hair_issue(material, node) -> Optional[str]:
    """Why ``node`` (a hair BSDF) is refused, or None when it is exported."""
    surface = hair_surface_node(material)
    if _core._same_node(surface, node):
        return hair_refusal(material)
    name = getattr(node, "name", getattr(node, "type", ""))
    return (
        f"{_core._shader_label(node)} '{name}' is not the shader on the Material Output; a hair "
        "surface is only exported when it terminates the material. Bake the material instead."
    )


def _extract_hair_material_data(node, data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill ``data`` for a Principled Hair BSDF surface."""
    data['type'] = 'hair'
    hair_inputs: Dict[str, Any] = {}
    input_graphs: Dict[str, Any] = {}
    unresolved: List[str] = []
    cache: Dict[Any, Dict[str, Any]] = {}

    for socket_name, targets, expected_type in _HAIR_MAPPED_SOCKETS:
        socket = _enabled_hair_socket(node, socket_name)
        if socket is None:
            continue
        if getattr(socket, "is_linked", False):
            resolved = _core._resolve_socket_value(socket, cache=cache, expected_type=expected_type)
        else:
            value = _core._socket_default_value(socket)
            if value is None:
                continue
            resolved = _core._constant_expr(
                _core._coerce_constant_value(value, 'color' if expected_type == 'color3' else 'float')
            )
        if resolved is None:
            continue
        if resolved.get("kind") == "unresolved":
            chain = resolved.get("provenance") or []
            unresolved.append(
                f"Material '{data['name']}': Unable to resolve hair '{socket_name}'"
                + (f" through chain: {' -> '.join(chain)}" if chain else "")
            )
            continue
        for target in targets:
            if resolved.get("kind") == "constant":
                hair_inputs[target] = resolved["value"]
            else:
                input_graphs[target] = resolved

    # The Huang lobe weights: Blender's unscaled lobe is the hair surface's
    # weight 0.5.
    for target in ('primarySpecular', 'secondarySpecular'):
        if target in hair_inputs:
            hair_inputs[target] = float(hair_inputs[target]) * HAIR_LOBE_WEIGHT_SCALE
        elif target in input_graphs:
            input_graphs[target] = _core._make_node_expr(
                _core._nodedef_for("multiply", "float"),
                {"in1": input_graphs[target], "in2": _core._constant_expr(HAIR_LOBE_WEIGHT_SCALE)},
            )
    # The secondary lobe renders saturate(secondarySpecularColor) *
    # secondarySpecular and the colour defaults to black, so a mapped
    # Secondary Reflection drew nothing without it.
    if 'secondarySpecular' in hair_inputs or 'secondarySpecular' in input_graphs:
        hair_inputs['secondarySpecularColor'] = [1.0, 1.0, 1.0]
    hair_inputs['tangent'] = list(HAIR_STRAND_TANGENT)

    # The bake lanes rebuild this material as unlit, which reads base_color.
    if 'baseColor' in hair_inputs:
        data['base_color'] = list(hair_inputs['baseColor'])[:3]
    if hair_inputs:
        data['hair_inputs'] = hair_inputs
    if input_graphs:
        data['input_graphs'] = input_graphs
    if unresolved:
        data['unresolved_warnings'] = unresolved
    return data
