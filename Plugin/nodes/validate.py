"""
RealityKit material validation and enforcement helpers.
"""

from __future__ import annotations

import re

from typing import Dict, List, Set

from ..material_policies import (
    SPECULAR_TINT_NORMALIZATION_SETTING,
    format_color,
    safe_overbright_achromatic_specular_tint,
    specular_tint_normalization_message,
)
from . import metadata


ALLOWED_UI_TYPES = {
    'FRAME',
    'REROUTE',
}

SUPPORTED_TYPES = {
    'OUTPUT_MATERIAL',
    'BSDF_PRINCIPLED',
    'EMISSION',
    'TEX_IMAGE',
    'NORMAL_MAP',
    'RGB',
    'VALUE',
    'INPUT_BOOL',
    'INPUT_INT',
    'INPUT_VECTOR',
    'SEPARATE_COLOR',
    'SEPXYZ',
    'TEX_NOISE',
    'TEX_VORONOI',
    'TEX_GRADIENT',
    'TEX_ENVIRONMENT',
    'CLAMP',
    'HUE_SAT',
    'BRIGHTCONTRAST',
    'VALTORGB',
    'RGBTOBW',
    'COMBINE_COLOR',
    'VECTOR_ROTATE',
    'NORMAL',
    'MAP_RANGE',
    'INVERT',
    # Transcribed from Cycles: svm_math_gamma_color, svm_checker,
    # svm_node_camera and svm_mapping.
    'GAMMA',
    'TEX_CHECKER',
    'CAMERA',
    'MAPPING',
    # The surface gradient of the height along the UV parametrization; see
    # _bump_expr.
    'BUMP',
    'UVMAP',
    # Geometry attributes and Object Info > Random; other outputs and
    # attribute kinds are refused by the resolver with the reason named.
    'ATTRIBUTE',
    'OBJECT_INFO',
    # Blender 5.2's Color Attribute node. Exported as a
    # ND_geompropvalue_color4 read of the ``primvars:<attribute name>``
    # primvar Blender's USD exporter writes for mesh color attributes - it is
    # color4f for every attribute type and domain, so the read is
    # four-channel and narrower consumers swizzle off it. Per-node gates
    # below refuse an unnamed attribute and a name that reaches no mesh using
    # the material.
    'VERTEX_COLOR',
    'TEX_COORD',
    'NEW_GEOMETRY',
    'FRESNEL',
    'LAYER_WEIGHT',
    'COMBXYZ',
    # The Material Output's Displacement socket, exported as RealityKit's
    # geometry modifier. Gated per material by ``displacement_refusal``:
    # Object space only, no linked Normal, and a Displacement Method that
    # actually moves vertices.
    'DISPLACEMENT',
    'VECTOR_DISPLACEMENT',
}

#: Blender Math operations the exporter authors as exact MaterialX float
#: nodes (see _MATH_SINGLE_INPUT_OPS / _MATH_TWO_INPUT_OPS /
#: _MATH_COMPOSED_OPS in Plugin/export/materials/extract/core.py; the parity
#: test in tests/unit/test_capability_table_parity.py keeps the tables in
#: sync). Deliberately absent, with the reason:
#: - MODULO: Blender is truncated fmod (result sign follows the dividend);
#:   MaterialX modulo is floored (sign follows in2). They differ for negative
#:   inputs, so MODULO is refused and FLOORED_MODULO is the exact mapping.
#: - INVERSE_SQRT, TRUNC, SNAP, WRAP, PINGPONG, COMPARE, LESS_THAN,
#:   GREATER_THAN, SMOOTH_MIN, SMOOTH_MAX, SIGN, SINH, COSH, TANH, RADIANS,
#:   DEGREES: no verified exact manifest nodedef or composition is authored
#:   for them; they keep the bake path rather than a silent approximation.
SUPPORTED_MATH_OPERATIONS = frozenset({
    'ADD',
    'SUBTRACT',
    'MULTIPLY',
    'DIVIDE',
    'MULTIPLY_ADD',
    'POWER',
    'LOGARITHM',
    'SQRT',
    'EXPONENT',
    'ABSOLUTE',
    'MINIMUM',
    'MAXIMUM',
    'ROUND',
    'FLOOR',
    'CEIL',
    'FRACT',
    'FLOORED_MODULO',
    'SINE',
    'COSINE',
    'TANGENT',
    'ARCSINE',
    'ARCCOSINE',
    'ARCTANGENT',
    'ARCTAN2',
})


#: Vector Math operations authored as exact MaterialX nodes or exact
#: compositions of them (transcribed from Cycles). The three left out have no
#: exact MaterialX form: MODULO truncates where MaterialX floors, and WRAP and
#: SNAP guard a per-component division by zero that MaterialX cannot select on.
SUPPORTED_VECTOR_MATH_OPERATIONS = frozenset({
    'ADD', 'SUBTRACT', 'MULTIPLY', 'DIVIDE', 'MULTIPLY_ADD', 'CROSS_PRODUCT',
    'PROJECT', 'REFLECT', 'REFRACT', 'FACEFORWARD', 'DOT_PRODUCT', 'DISTANCE',
    'LENGTH', 'SCALE', 'NORMALIZE', 'ABSOLUTE', 'POWER', 'SIGN', 'MINIMUM',
    'MAXIMUM', 'ROUND', 'FLOOR', 'CEIL', 'FRACTION', 'SINE', 'COSINE', 'TANGENT',
})
VECTOR_MATH_REFUSAL_REASONS = {
    'MODULO': "Blender's truncated modulo differs from MaterialX's floored modulo",
    'WRAP': "its per-component zero-range guard has no MaterialX equivalent",
    'SNAP': "its per-component safe divide has no MaterialX equivalent",
}


def vector_math_refusal_message(operation: str) -> str:
    reason = VECTOR_MATH_REFUSAL_REASONS.get(operation)
    if reason:
        return f"Vector Math '{operation}' requires baking; {reason}."
    return f"Vector Math '{operation}' requires baking; it has no exact MaterialX form."


def math_refusal_message(operation: str) -> str:
    """Shared phrasing for refusing a Math node operation.

    Both the validator and the export-time warning pass emit this, so the
    user reads one story about the same node.
    """
    operation = (operation or "").upper() or "UNKNOWN"
    if operation == 'MODULO':
        return (
            "Math operation MODULO requires baking: Blender's truncated fmod "
            "has no exact MaterialX equivalent (MaterialX modulo is floored). "
            "Use Floored Modulo for MaterialX semantics, or bake."
        )
    return (
        f"Math operation {operation} requires baking; it has no exact "
        "MaterialX equivalent."
    )

BAKE_TYPES = {
    'TEX_WAVE',
    'TEX_WHITE_NOISE',
    'TEX_MAGIC',
    'TEX_BRICK',
    'TEX_SKY',
    'TEX_GABOR',
    'TEX_IES',
    'BLACKBODY',
    'LIGHT_FALLOFF',
    'WAVELENGTH',
    'VECT_MATH',
    'CURVE_VEC',
    'ShaderNodeRadialTiling',
    'CURVE_FLOAT',
    # Vector Transform's Object, World and Camera spaces need the render's
    # object and camera matrices, which no exported MaterialX node reads.
    'VECT_TRANSFORM',
    'CURVE_RGB',
}

UNSUPPORTED_TYPES = {
    'OUTPUT_AOV',
    'OUTPUT_WORLD',
    'OUTPUT_LIGHT',
    'BACKGROUND',
    'HOLDOUT',
    'BSDF_GLASS',
    'BSDF_REFRACTION',
    'EEVEE_SPECULAR',
    'BSDF_RAY_PORTAL',
    'BSDF_TRANSLUCENT',
    'BSDF_TOON',
    # EEVEE-only: Cycles, which runs every bake, does not implement it.
    'SHADERTORGB',
    'BSDF_HAIR',
    'VOLUME_ABSORPTION',
    'VOLUME_SCATTER',
    'PRINCIPLED_VOLUME',
    'VOLUME_COEFFICIENTS',
    'AMBIENT_OCCLUSION',
    'HAIR_INFO',
    'PARTICLE_INFO',
    'POINT_INFO',
    'VOLUME_INFO',
    'WIREFRAME',
    'LIGHT_PATH',
    'TANGENT',
    'BEVEL',
}


# These Blender 5.2 controls currently have no faithful graph path in any
# surface.  PBR Surface 2 has no thin-film or subsurface-IOR inputs, and no
# RealityKit surface does; accepting them would only lose the artist's values.
_UNMAPPED_PRINCIPLED_INPUTS = (
    ('Subsurface IOR', 1.4, 'Subsurface Weight'),
    ('Thin Film Thickness', 0.0, None),
    ('Thin Film IOR', 1.33, 'Thin Film Thickness'),
)


def _values_differ(value, neutral, epsilon: float = 1e-6) -> bool:
    """Compare scalar/vector Blender socket values against a known default."""
    if isinstance(neutral, (tuple, list)):
        try:
            values = list(value)
        except (TypeError, ValueError):
            return True
        if len(values) < len(neutral):
            return True
        try:
            return any(
                abs(float(values[index]) - float(component)) > epsilon
                for index, component in enumerate(neutral)
            )
        except (TypeError, ValueError):
            return True
    try:
        return abs(float(value) - float(neutral)) > epsilon
    except (TypeError, ValueError):
        return value != neutral


#: Outputs of the reader nodes that have a RealityKit reader. The map is the
#: extractor's; keeping one copy means the validator and the resolver cannot
#: disagree about which socket is refused.
_READER_OUTPUTS = {
    'TEX_COORD': ('UV', 'Object', 'Generated'),
    'NEW_GEOMETRY': ('Position', 'Normal', 'Incoming', 'Backfacing'),
}
_READER_NODE_LABELS = {
    'TEX_COORD': "Texture Coordinate",
    'NEW_GEOMETRY': "Geometry",
    'FRESNEL': "Fresnel",
    'LAYER_WEIGHT': "Layer Weight",
}


def _linked_output_names(node) -> List[str]:
    names: List[str] = []
    for socket in getattr(node, "outputs", None) or []:
        if getattr(socket, "is_linked", False):
            names.append(getattr(socket, "name", "") or "")
    return names


def _bump_issues(node) -> List[tuple]:
    """A Bump the export refuses, with the resolver's reason."""
    from ..export.materials.extract import core

    try:
        expr = core._bump_expr(node, set(), [], {})
    except ValueError as exc:
        return [(str(exc), False)]
    if isinstance(expr, dict) and expr.get("kind") == "unresolved":
        reason = expr.get("reason") or "its height could not be resolved"
        return [(reason[0].upper() + reason[1:] + ".", False)]
    return []


def _normal_map_issues(node) -> List[tuple]:
    """A Normal Map the export refuses, with the resolver's reason.

    DirectX maps and a linked Strength are authored exactly, so only the
    tangent space, the tangent UV map and the displaced-base case remain.
    """
    from ..export.materials.extract.core import normal_map_refusal

    refusal = normal_map_refusal(node)
    if refusal is None:
        return []
    return [(refusal[0].upper() + refusal[1:] + ".", False)]


def _environment_texture_issues(node) -> List[str]:
    """An Environment Texture the export refuses, as the resolver refuses it.

    It is always read at computed coordinates, so a premultiplied image is
    refused exactly as ``_image_read_refusal`` refuses it.
    """
    image = getattr(node, "image", None)
    if image is None:
        return ["Environment Texture node has no image."]
    from ..export.materials.extract import core

    spec = {"alpha_mode": "premul" if str(getattr(image, "alpha_mode", "") or "").upper() == "PREMUL" else None}
    refusal = core._image_read_refusal(spec, [], node)
    if refusal:
        reason = refusal["reason"]
        return [reason[0].upper() + reason[1:] + "."]
    return []


def _attribute_reader_issues(node) -> List[tuple]:
    """Refusals for Attribute and Object Info, from the resolver's own reasons.

    Each linked output is resolved exactly as the export resolves it, so the
    validator and the export name the same problem.
    """
    from types import SimpleNamespace

    from ..export.materials.extract import core

    issues: List[tuple] = []
    for name in _linked_output_names(node):
        socket = SimpleNamespace(name=name)
        if getattr(node, "type", "") == 'ATTRIBUTE':
            expr = core._attribute_node_expr(node, socket, [])
        else:
            expr = core._object_info_expr(socket, [])
        if isinstance(expr, dict) and expr.get("kind") == "unresolved":
            reason = expr.get("reason") or "it has no RealityKit reader"
            issues.append((reason[0].upper() + reason[1:] + ".", False))
    if getattr(node, "type", "") == 'OBJECT_INFO' and 'Random' in _linked_output_names(node):
        issues.append((
            "Object Info Random is written per mesh from the object's name, as Cycles "
            "computes it; copies of one instanced mesh share a single value.",
            True,
        ))
    return issues


def _reader_node_issues(node) -> List[tuple]:
    """Issues for the surface-reader nodes, as ``(message, is_warning)`` pairs.

    Texture Coordinate and Geometry export the outputs RealityKit has a reader
    for and refuse the rest by name. Fresnel and Layer Weight are transcribed
    from Blender's own shading code over the world normal and the view
    direction, so a linked Normal, which the exporter would only have in
    tangent space, is refused, as is a linked Layer Weight Blend, whose
    exponent is folded at export time.
    """
    node_type = getattr(node, "type", "")
    if node_type in ('ATTRIBUTE', 'OBJECT_INFO'):
        return _attribute_reader_issues(node)
    if node_type == 'BUMP':
        return _bump_issues(node)
    label = _READER_NODE_LABELS.get(node_type)
    if label is None:
        return []
    issues: List[tuple] = []
    allowed = _READER_OUTPUTS.get(node_type)
    if allowed is not None:
        from ..export.materials.extract.core import reader_refusal

        for name in _linked_output_names(node):
            refusal = reader_refusal(node, name)
            if refusal is not None:
                issues.append((refusal[0].upper() + refusal[1:] + ".", False))
            elif name not in allowed:
                issues.append((
                    f"{label} output '{name}' has no RealityKit reader; use "
                    f"{', '.join(allowed)}, or bake the material.",
                    False,
                ))
        if node_type == 'TEX_COORD' and 'Generated' in _linked_output_names(node):
            issues.append((
                "Texture Coordinate 'Generated' exports as object-space position; "
                "Blender's bounding-box normalization is not applied, so the pattern "
                "keeps its shape but not its scale.",
                True,
            ))
        return issues
    inputs = getattr(node, "inputs", None)
    normal = inputs.get('Normal') if inputs is not None and hasattr(inputs, "get") else None
    if normal is not None and getattr(normal, "is_linked", False):
        issues.append((
            f"{label} with a linked Normal requires baking; the exporter carries "
            "normal maps in tangent space and cannot combine them with the view "
            "direction.",
            False,
        ))
    if node_type == 'LAYER_WEIGHT':
        blend = inputs.get('Blend') if inputs is not None and hasattr(inputs, "get") else None
        if blend is not None and getattr(blend, "is_linked", False):
            issues.append((
                "Layer Weight with a linked Blend requires baking; only a constant "
                "Blend is exported.",
                False,
            ))
    return issues


def _driver_issues(material) -> List[tuple]:
    """Issues for driver expressions on material sockets: ``(node, message, is_warning)``.

    A scripted driver over ``frame`` on a Value node output, or on a float
    input of a node the resolver reads live, exports as RealityKit's time
    reader and warns that it runs on the material's own clock. Any other
    driver is refused by name: the background process the CLI runs never
    evaluates drivers, so an ignored one would export as zero.
    """
    from ..export.materials.extract.core import (
        DRIVEN_INPUT_NODE_TYPES,
        driver_refusal_reason,
    )

    tree = getattr(material, "node_tree", None)
    animation = getattr(tree, "animation_data", None)
    drivers = getattr(animation, "drivers", None) or []
    issues: List[tuple] = []
    for fcurve in drivers:
        path = str(getattr(fcurve, "data_path", "") or "")
        node = None
        socket = None
        # nodes["X"].inputs[i].default_value / nodes["X"].outputs[i].default_value
        match = re.match(r'nodes\["(.+?)"\]\.(inputs|outputs)\[(\d+)\]\.default_value$', path)
        if match and tree is not None:
            node = tree.nodes.get(match.group(1)) if hasattr(tree.nodes, "get") else None
            if node is not None:
                collection = getattr(node, match.group(2), None)
                try:
                    socket = collection[int(match.group(3))]
                except Exception:
                    socket = None
        label = getattr(node, "name", path) if node is not None else path
        expression = str(getattr(getattr(fcurve, "driver", None), "expression", "") or "")
        if node is None or socket is None:
            issues.append((node, f"Driver '{expression}' on '{path}' targets no material socket the exporter reads; remove it or bake the material.", False))
            continue
        node_type = getattr(node, "type", "")
        is_value_output = node_type == 'VALUE' and match.group(2) == "outputs"
        is_live_input = (
            match.group(2) == "inputs"
            and node_type in DRIVEN_INPUT_NODE_TYPES
            and (getattr(socket, "type", "VALUE") or "VALUE") == 'VALUE'
        )
        if not (is_value_output or is_live_input):
            issues.append((node, (
                f"Driver '{expression}' on {label} '{getattr(socket, 'name', '?')}' is not exported: "
                "only a Value node's output and float inputs of math, mix, clamp, range, colour and "
                "Fresnel nodes carry a driver. Drive a Value node and wire it in, or bake the material."
            ), False))
            continue
        reason = driver_refusal_reason(fcurve)
        if reason is not None:
            issues.append((node, f"Driver '{expression}' on {label} '{getattr(socket, 'name', '?')}' is not exported; {reason}. Bake the material.", False))
            continue
        issues.append((node, (
            f"Driver '{expression}' on {label} '{getattr(socket, 'name', '?')}' exports as RealityKit's time "
            "reader (frame = time × fps). It runs on the material's own clock from the moment it renders, "
            "not on the timeline, so it will not stay in step with keyframed animation."
        ), True))
    return issues


def _unsupported_principled_inputs(
    node,
    clamp_specular_tint: bool = False,
) -> List[str]:
    """Report Principled BSDF inputs that RealityKit PBR cannot represent.

    Only inputs that are linked or deviate from their neutral default are
    reported, so a stock Principled node stays silent.
    """
    issues: List[str] = []

    def _socket(name: str):
        return node.inputs.get(name)

    def _linked(name: str) -> bool:
        socket = _socket(name)
        return bool(socket is not None and getattr(socket, 'is_linked', False))

    def _active(name: str, neutral=0.0) -> bool:
        socket = node.inputs.get(name)
        if socket is None:
            return False
        if getattr(socket, 'is_linked', False):
            return True
        return _values_differ(getattr(socket, 'default_value', None), neutral)

    subsurface_active = _active('Subsurface Weight')
    coat_active = _active('Coat Weight')
    sheen_active = _active('Sheen Weight')
    transmission_active = _active('Transmission Weight')
    thin_film_active = _active('Thin Film Thickness')
    anisotropy_active = _active('Anisotropic')

    thin_wall = node.inputs.get('Thin Wall')
    if (
        thin_wall is not None
        and (getattr(thin_wall, 'is_linked', False) or bool(thin_wall.default_value))
        and (transmission_active or subsurface_active)
    ):
        issues.append("Principled 'Thin Wall' is enabled; RealityKit has no thin-wall shading.")
    if transmission_active:
        issues.append("Principled 'Transmission Weight' is not exportable; the material will be opaque.")

    specular_tint_socket = _socket('Specular Tint')
    specular_tint_policy = safe_overbright_achromatic_specular_tint(
        getattr(specular_tint_socket, 'default_value', None),
        linked=bool(
            specular_tint_socket is not None
            and getattr(specular_tint_socket, 'is_linked', False)
        ),
    )

    # Coat constants are mapped, but linked Coat Weight/Roughness/Tint values
    # have no graph_input_map entry in extraction.  Reject those links until
    # they can be preserved rather than accepting a default value.
    linked_coat_inputs = {
        name
        for name in ('Coat Weight', 'Coat Roughness', 'Coat Tint')
        if _linked(name) and (name == 'Coat Weight' or coat_active)
    }
    for name in sorted(linked_coat_inputs):
        issues.append(
            f"Principled '{name}' is linked, but linked coat controls are not exportable; "
            "bake the material or use an unlinked constant."
        )

    # Blender's anisotropy level and rotation do not map one-to-one to the
    # PBR Surface 2 inputs: native Blender 5.2 applies a level
    # factor and tangent rotation that this exporter does not reproduce.  A
    # partially mapped result is worse than an actionable failure.
    anisotropy_inputs = (
        ('Anisotropic', 0.0, None),
        ('Anisotropic Rotation', 0.0, 'Anisotropic'),
        ('Tangent', (0.0, 0.0, 0.0), 'Anisotropic'),
    )
    for name, neutral, controller in anisotropy_inputs:
        if (controller is None or anisotropy_active) and _active(name, neutral):
            issues.append(
                f"Principled '{name}' requires a verified Blender 5.2 tangent/anisotropy "
                "mapping; bake the material before export."
            )

    active_controllers = {
        'Subsurface Weight': subsurface_active,
        'Coat Weight': coat_active,
        'Sheen Weight': sheen_active,
        'Thin Film Thickness': thin_film_active,
    }

    for name, neutral, controller in _UNMAPPED_PRINCIPLED_INPUTS:
        if (
            (controller is None or active_controllers.get(controller, False))
            and _active(name, neutral)
        ):
            issues.append(
                f"Principled '{name}' has no RealityKit PBR Surface 2 input; "
                "bake the material before export."
            )

    if (
        coat_active
        and 'Coat Tint' not in linked_coat_inputs
        and _active('Coat Tint', (1.0, 1.0, 1.0, 1.0))
    ):
        issues.append(
            "Principled 'Coat Tint' has no RealityKit PBR Surface 2 input, and no "
            "RealityKit surface carries a coat tint; bake the material."
        )

    if _active('Specular Tint', (1.0, 1.0, 1.0, 1.0)):
        if not (clamp_specular_tint and specular_tint_policy is not None):
            if specular_tint_policy is not None:
                issues.append(
                    "Principled 'Specular Tint' "
                    f"{format_color(specular_tint_policy['input'])} exceeds the "
                    "verified [0, 1] range. Set it to [1, 1, 1] in Blender, enable "
                    "'Clamp Overbright Specular Tint' "
                    f"({SPECULAR_TINT_NORMALIZATION_SETTING}) for an export-only "
                    "clamp, or bake the material."
                )
            else:
                issues.append(
                    "Principled 'Specular Tint' color semantics are not verified "
                    "against RealityKit PBR Surface 2. Linked or colored values "
                    "cannot be normalized safely; set an artist-approved value in "
                    "Blender or bake the material."
                )

    if _active('Sheen Weight'):
        roughness = node.inputs.get('Sheen Roughness')
        roughness_is_custom = False
        if roughness is not None:
            if roughness.is_linked:
                roughness_is_custom = True
            else:
                try:
                    roughness_is_custom = abs(float(roughness.default_value) - 0.5) > 1e-6
                except (TypeError, ValueError):
                    pass
        if roughness_is_custom:
            issues.append(
                "Principled 'Sheen Roughness' has no RealityKit PBR Surface 2 input; "
                "bake the material."
            )
    return issues


def _vertex_color_issues(node) -> List[str]:
    """Mirror the extractor's Color Attribute refusals for `validate`.

    Import shared with extraction so the two gates cannot drift: an export
    that refuses (unnamed attribute, or an attribute no mesh using the
    material carries) must be predicted here.

    The Alpha output is deliberately absent: the primvar Blender writes for a
    mesh color attribute is color4f, so the exporter reads alpha as a plain
    swizzle of the same four-channel read, and predicting a refusal for it
    would disagree with the export.
    """
    import re as _re

    from ..export.materials.extract.core import _color_attribute_reaches_export

    issues: List[str] = []
    layer_name = (getattr(node, "layer_name", "") or "").strip()
    if not layer_name:
        issues.append(
            "Color Attribute names no color attribute; pick the attribute "
            "explicitly on the node so the exported primvar can be referenced."
        )
    elif not _re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', layer_name):
        issues.append(
            f"Color attribute name '{layer_name}' is not a valid USD primvar "
            "identifier (letters, digits, underscore; not starting with a "
            "digit); rename the attribute."
        )
    elif not _color_attribute_reaches_export(node, layer_name):
        issues.append(
            f"Color attribute '{layer_name}' was not found on a mesh using "
            "this material; the exported USD would carry no "
            f"primvars:{layer_name} for the export to read."
        )
    return issues


def _effective_texture_mapping_uses(nodes):
    """Collect non-default mappings that the material extractor will author."""

    # Keep the validator importable without OpenUSD/Blender initialization;
    # these helpers are pure until invoked against a real node graph.
    from ..export.materials.extract.core import (
        _extract_mapping_from_node,
        _extract_uv_map_from_node,
        image_premultiplies_color,
        image_uses_uv_transform,
        uv_set_resolution,
    )
    from ..export.materials.mapping import effective_texture_mapping_contract

    contracts = {}
    extraction_errors = []
    image_nodes = sorted(
        (
            node
            for node in nodes
            # An Environment Texture projects a direction, never a UV transform.
            if getattr(node, "type", "") == "TEX_IMAGE"
        ),
        key=lambda node: (str(getattr(node, "name", "")), id(node)),
    )
    for image_node in image_nodes:
        # Box projection and computed coordinates never author a UV transform.
        if (
            str(getattr(image_node, "projection", "FLAT") or "FLAT").upper() != "FLAT"
            or not image_uses_uv_transform(image_node)
            or image_premultiplies_color(image_node) is not None
        ):
            continue
        inputs = getattr(image_node, "inputs", None)
        vector_socket = inputs.get("Vector") if inputs is not None else None
        if not vector_socket or not getattr(vector_socket, "is_linked", False):
            continue
        links = list(getattr(vector_socket, "links", []) or [])
        if not links or not getattr(links[0], "from_node", None):
            continue
        source_node = links[0].from_node
        try:
            mapping = _extract_mapping_from_node(source_node)
            uv_map = getattr(image_node, "uv_map", "") or ""
            if not uv_map:
                uv_map = _extract_uv_map_from_node(source_node)
            texcoord, refusal = uv_set_resolution(image_node, uv_map)
            if refusal is not None:
                raise ValueError(refusal[0].upper() + refusal[1:] + ".")
            contract = effective_texture_mapping_contract(mapping, texcoord)
        except ValueError as exc:
            extraction_errors.append((image_node, str(exc)))
            continue
        if contract is not None:
            contracts.setdefault(contract, []).append(image_node)
    return contracts, extraction_errors


_PRINCIPLED_PRESET_TYPES = frozenset({
    'BSDF_DIFFUSE', 'BSDF_GLOSSY', 'BSDF_METALLIC', 'BSDF_SHEEN', 'SUBSURFACE_SCATTERING',
})


def _same_node_loose(a, b) -> bool:
    if a is None or b is None:
        return False
    return a is b or a == b


def _views_for(closure, node):
    """The stand-in Principled BSDFs exported for a preset shader node."""
    from ..export.materials.extract.core import principled_views

    return [view for view in principled_views(closure) if _same_node_loose(getattr(view, "source", None), node)]


def _renamed_issue(issue: str, view) -> str:
    """Reword a Principled input issue with the names the artist sees on ``view``."""
    label = getattr(view, "label", "Principled")
    for principled_name, own_name in sorted(getattr(view, "rename", {}).items(), key=lambda kv: -len(kv[0])):
        issue = issue.replace(f"Principled '{principled_name}'", f"{label} '{own_name}'")
    return issue.replace("Principled '", f"{label} '")


def validate_material(
    material,
    only_connected: bool = True,
    strict: bool = False,
    clamp_specular_tint: bool = False,
) -> Dict[str, object]:
    """Validate a Blender material against RealityKit compatibility rules."""
    result = {
        "material": getattr(material, "name", "Unknown"),
        "ok": True,
        "errors": [],
        "warnings": [],
        "offending_nodes": [],
        "warning_nodes": [],
    }

    if material:
        from ..export.materials.extract.core import working_color_space_refusal

        color_space_refusal = working_color_space_refusal()
        if color_space_refusal is not None:
            _add_issue(result, "errors", None, color_space_refusal + ".", removable=False)
            result["ok"] = False

    if not material or not material.node_tree:
        return result

    from ..export.materials.extract.core import surface_output_refusal

    surface_refusal = surface_output_refusal(material)
    if surface_refusal is not None:
        _add_issue(result, "errors", None, surface_refusal + ".", removable=False)
        result["ok"] = False

    from ..export.materials.extract.core import srgb_data_image_notices

    for image_node, notice in srgb_data_image_notices(material):
        _add_issue(result, "warnings", image_node, notice, removable=False)

    authored_nodes = _collect_used_nodes(material)
    if only_connected:
        used_nodes = authored_nodes
    else:
        used_nodes = set(material.node_tree.nodes)

    def add_issue(
        kind: str,
        node,
        message: str,
        force_error: bool = False,
        removable: bool = True,
    ) -> None:
        target = "errors" if force_error else kind
        _add_issue(result, target, node, message, removable=removable)

    # Blender evaluates a muted node as a pass-through and a muted link as
    # absent. Nothing in the material pipeline reads either flag, so the
    # extractor walks straight through them as if they were live and the export
    # can silently disagree with the viewport - a muted Mix or Mapping is
    # exported as though the artist had never disabled it.
    #
    # Reject rather than emulate: Blender's pass-through semantics vary by node
    # type and socket layout, so reconstructing them is exactly the kind of
    # guess this exporter refuses to make elsewhere. Unmuting or deleting the
    # node is unambiguous and takes a moment.
    for node in sorted(
        (n for n in used_nodes if getattr(n, "mute", False)),
        key=lambda n: (str(getattr(n, "name", "")), id(n)),
    ):
        add_issue(
            "warnings",
            node,
            (
                "Muted node: Blender bypasses it, but the exporter evaluates it "
                "as if enabled, so the exported material would not match the "
                "viewport. Unmute or delete the node."
            ),
            force_error=strict,
            removable=True,
        )

    muted_links = [
        link
        for link in getattr(material.node_tree, "links", ())
        if getattr(link, "is_muted", False)
        and getattr(link, "to_node", None) in used_nodes
    ]
    if muted_links:
        add_issue(
            "warnings",
            muted_links[0].to_node,
            (
                f"Material has {len(muted_links)} muted link(s). Blender treats "
                "them as disconnected, but the exporter follows them as if live. "
                "Delete the muted links or unmute them."
            ),
            force_error=strict,
            removable=False,
        )

    mapping_contracts, mapping_extraction_errors = (
        _effective_texture_mapping_uses(authored_nodes)
    )
    for image_node, message in mapping_extraction_errors:
        add_issue(
            "warnings",
            image_node,
            message,
            force_error=strict,
            removable=False,
        )
    if len(mapping_contracts) > 1:
        mapped_nodes = sorted(
            (node for nodes in mapping_contracts.values() for node in nodes),
            key=lambda node: (str(getattr(node, "name", "")), id(node)),
        )
        add_issue(
            "warnings",
            mapped_nodes[-1],
            (
                f"Material uses {len(mapping_contracts)} distinct non-default texture "
                "mappings, but RealityKit honors only the first 2D texture transform "
                "per material. Use identical Mapping values and one UV set for every "
                "transformed texture, or bake the transforms into the images."
            ),
            force_error=strict,
            removable=False,
        )

    from ..export.materials.extract.core import (
        displacement_notices,
        displacement_refusal,
        displacement_source,
    )

    _output_node, displacement_node = displacement_source(material)
    if displacement_node is not None:
        refusal = displacement_refusal(material)
        if refusal is not None:
            add_issue("warnings", displacement_node, refusal, force_error=strict, removable=False)
        else:
            for notice in displacement_notices(material):
                add_issue("warnings", displacement_node, notice, removable=False)

    # Approximations PBR Surface 2 makes of the Principled BSDF the surface
    # exports as, reported on the shader wired to the Material Output. The
    # extractor authors the same surface; these are warnings, not refusals.
    from ..export.materials.extract.core import (
        PrincipledView,
        principled_notices,
        resolve_surface_closure,
    )

    surface_closure = resolve_surface_closure(material)
    exported_principled = surface_closure.get("principled")
    if surface_closure.get("refusal") is None and exported_principled is not None:
        for notice in principled_notices(exported_principled):
            if isinstance(exported_principled, PrincipledView):
                notice = _renamed_issue(notice, exported_principled)
            add_issue("warnings", surface_closure.get("surface"), notice, removable=False)

    for driven_node, message, is_warning in _driver_issues(material):
        add_issue(
            "warnings",
            driven_node,
            message,
            force_error=strict and not is_warning,
            removable=False,
        )

    for node in used_nodes:
        node_type = getattr(node, "type", "")

        if node_type in ALLOWED_UI_TYPES:
            continue

        if node_type == 'GROUP':
            if _is_rk_group(node):
                continue
            add_issue("errors", node, "Non-RealityKit node group used.")
            continue

        if node_type in SUPPORTED_TYPES:
            for issue, is_warning in _reader_node_issues(node):
                add_issue(
                    "warnings",
                    node,
                    issue,
                    force_error=strict and not is_warning,
                    removable=False,
                )
            if node_type == 'TEX_IMAGE' and getattr(node, "image", None) is None:
                add_issue(
                    "warnings",
                    node,
                    "Image Texture node has no image.",
                    force_error=strict,
                )
            if node_type == 'UVMAP':
                from ..export.materials.extract.core import uv_map_node_resolution

                _texcoord, refusal = uv_map_node_resolution(node)
                if refusal is not None:
                    add_issue(
                        "warnings",
                        node,
                        refusal[0].upper() + refusal[1:] + ".",
                        force_error=strict,
                        removable=False,
                    )
            if node_type == 'TEX_ENVIRONMENT':
                for issue in _environment_texture_issues(node):
                    add_issue("warnings", node, issue, force_error=strict, removable=False)
            if node_type == 'TEX_IMAGE':
                projection = str(
                    getattr(node, "projection", "FLAT") or "FLAT"
                ).upper()
                if projection not in ('FLAT', 'BOX'):
                    add_issue(
                        "warnings",
                        node,
                        f"Image Texture projection {projection} requires baking; "
                        "only Flat and Box projections are exported.",
                        force_error=strict,
                        removable=False,
                    )
            if node_type == 'VERTEX_COLOR':
                for issue in _vertex_color_issues(node):
                    add_issue(
                        "warnings",
                        node,
                        issue,
                        force_error=strict,
                        removable=False,
                    )
            if node_type == 'BSDF_PRINCIPLED':
                specular_tint_socket = node.inputs.get('Specular Tint')
                normalization = safe_overbright_achromatic_specular_tint(
                    getattr(specular_tint_socket, 'default_value', None),
                    linked=bool(
                        specular_tint_socket is not None
                        and getattr(specular_tint_socket, 'is_linked', False)
                    ),
                )
                if clamp_specular_tint and normalization is not None:
                    add_issue(
                        "warnings",
                        node,
                        specular_tint_normalization_message(normalization),
                        force_error=False,
                        removable=False,
                    )
                for issue in _unsupported_principled_inputs(
                    node,
                    clamp_specular_tint=clamp_specular_tint,
                ):
                    add_issue("warnings", node, issue, force_error=strict, removable=False)
            if node_type == 'NORMAL_MAP':
                for issue, is_warning in _normal_map_issues(node):
                    add_issue(
                        "warnings",
                        node,
                        issue,
                        force_error=strict and not is_warning,
                        removable=False,
                    )
            if node_type == 'MAP_RANGE':
                from ..export.materials.extract.core import map_range_refusal

                refusal = map_range_refusal(node)
                if refusal:
                    add_issue("warnings", node, refusal, force_error=strict, removable=False)
            if node_type == 'TEX_NOISE':
                from ..export.materials.extract.core import noise_texture_refusal

                refusal = noise_texture_refusal(node)
                if refusal:
                    add_issue("warnings", node, refusal, force_error=strict, removable=False)
            if node_type == 'TEX_VORONOI':
                from ..export.materials.extract.core import voronoi_texture_refusal

                refusal = voronoi_texture_refusal(node, _linked_output_names(node))
                if refusal:
                    add_issue("warnings", node, refusal, force_error=strict, removable=False)
            if node_type in ('SEPARATE_COLOR', 'COMBINE_COLOR'):
                mode = str(getattr(node, "mode", "RGB") or "RGB").upper()
                exportable = ('RGB', 'HSV') if node_type == 'SEPARATE_COLOR' else ('RGB',)
                if mode not in exportable:
                    label = "Separate Color" if node_type == 'SEPARATE_COLOR' else "Combine Color"
                    add_issue(
                        "warnings",
                        node,
                        f"{label} in {mode} mode is not exportable; use "
                        f"{' or '.join(exportable)} mode, or bake the material.",
                        force_error=strict,
                        removable=False,
                    )
            if node_type == 'VALTORGB':
                ramp = getattr(node, "color_ramp", None)
                interpolation = (getattr(ramp, "interpolation", "LINEAR") or "LINEAR").upper()
                color_mode = (getattr(ramp, "color_mode", "RGB") or "RGB").upper()
                if color_mode != "RGB" or interpolation not in {"LINEAR", "CONSTANT", "EASE"}:
                    add_issue(
                        "warnings",
                        node,
                        f"Color Ramp {color_mode}/{interpolation} requires baking; only RGB Linear, "
                        "Constant, and Ease are mapped exactly.",
                        force_error=strict,
                        removable=False,
                    )
            continue

        if node_type in {'BSDF_HAIR_PRINCIPLED', 'BSDF_HAIR'}:
            from ..export.materials.extract.core import hair_issue, hair_notices

            reason = hair_issue(material, node)
            if reason is not None:
                add_issue("errors", node, reason)
                continue
            for notice in hair_notices(material):
                add_issue("warnings", node, notice, removable=False)
            continue

        if node_type in _PRINCIPLED_PRESET_TYPES:
            from ..export.materials.extract.core import principled_like_issue, resolve_surface_closure

            closure = resolve_surface_closure(material)
            reason = principled_like_issue(closure, node)
            if reason is not None:
                add_issue("errors", node, reason)
                continue
            for view in _views_for(closure, node):
                for issue in _unsupported_principled_inputs(
                    view, clamp_specular_tint=clamp_specular_tint
                ):
                    add_issue("warnings", node, _renamed_issue(issue, view), force_error=strict, removable=False)
            if _same_node_loose(closure.get("surface"), node):
                # A preset on the output owns the closure's notices; a mixed
                # preset's notices are reported on its Mix Shader.
                for notice in closure.get("notices") or []:
                    add_issue("warnings", node, notice, removable=False)
            continue

        if node_type in {'MIX_SHADER', 'ADD_SHADER', 'BSDF_TRANSPARENT'}:
            # A Mix Shader of a Principled BSDF with a white Transparent BSDF
            # is opacity and an Add Shader of a Principled BSDF with an
            # Emission shader adds emission; the shared closure resolver says
            # which, and names the operands of anything else. The bake lane
            # still takes a mix of two Principled BSDFs - the advice is the
            # point.
            from ..export.materials.extract.core import closure_issue, resolve_surface_closure

            closure = resolve_surface_closure(material)
            reason = closure_issue(closure, node)
            if reason is None:
                if _same_node_loose(closure.get("closure"), node):
                    mixed = closure.get("principled")
                    blended = getattr(mixed, "blended", ())
                    if blended:
                        for issue in _unsupported_principled_inputs(
                            mixed, clamp_specular_tint=clamp_specular_tint
                        ):
                            if any(f"'{name}'" in issue for name in blended):
                                add_issue("warnings", node, _renamed_issue(issue, mixed), force_error=strict, removable=False)
                    for notice in closure.get("notices") or []:
                        add_issue("warnings", node, notice, removable=False)
                continue
            add_issue("errors", node, reason)
            continue

        if node_type in {'MIX_RGB', 'MIX'}:
            if _is_supported_mix(node):
                continue
            add_issue(
                "warnings",
                node,
                "Mix node requires baking unless it is a plain mix or multiply/add/subtract "
                "of resolvable linked inputs, or Factor is 0/1 with a passthrough input.",
                force_error=strict,
            )
            continue

        if node_type == 'MATH':
            operation = (getattr(node, "operation", "") or "").upper()
            if operation in SUPPORTED_MATH_OPERATIONS:
                continue
            add_issue(
                "warnings",
                node,
                math_refusal_message(operation),
                force_error=strict,
            )
            continue

        if node_type == 'VECT_MATH':
            operation = (getattr(node, "operation", "") or "").upper()
            if operation in SUPPORTED_VECTOR_MATH_OPERATIONS:
                continue
            add_issue(
                "warnings",
                node,
                vector_math_refusal_message(operation),
                force_error=strict,
            )
            continue

        if node_type in BAKE_TYPES:
            add_issue(
                "warnings",
                node,
                "Node requires baking for RealityKit.",
                force_error=strict,
            )
            continue

        if node_type in UNSUPPORTED_TYPES:
            add_issue("errors", node, "Node is not supported by RealityKit export.")
            continue

        add_issue("errors", node, "Node type is unrecognized by the exporter.")

    result["ok"] = not result["errors"]
    return result
def select_offending_nodes(material, issues: Dict[str, object]) -> int:
    """Select offending nodes in a material's node tree; return the count."""
    if not material or not material.node_tree:
        return 0
    offending = issues.get("offending_nodes", []) + issues.get("warning_nodes", [])
    if not offending:
        return 0
    node_tree = material.node_tree
    # bpy_prop_collection.__contains__ only accepts string keys, so node
    # membership must be checked against the nodes themselves.
    tree_nodes = list(node_tree.nodes)
    for node in tree_nodes:
        node.select = False
    active = None
    selected = 0
    for entry in offending:
        node = entry.get("node")
        if node is not None and node in tree_nodes:
            if not node.select:
                selected += 1
            node.select = True
            active = node
    if active:
        node_tree.nodes.active = active
    return selected


def collect_scene_materials(context) -> List[object]:
    """Collect the materials an export of the scene processes.

    That is every object Blender's USD exporter writes (see
    ``animation_export.view_layer_objects``) and the objects of the collections
    they instance. Hidden objects are not exported, so their materials are not
    validated.
    """
    from ..export.animation_export import collect_processing_objects, view_layer_objects

    materials = []
    seen = set()
    for obj in collect_processing_objects(context, view_layer_objects(context)):
        for slot in getattr(obj, "material_slots", []):
            mat = slot.material
            if mat and mat not in seen:
                seen.add(mat)
                materials.append(mat)
    return materials


def _add_issue(
    result: Dict[str, object],
    kind: str,
    node,
    message: str,
    removable: bool = True,
) -> None:
    entry = {
        "node_name": getattr(node, "name", ""),
        "node_type": getattr(node, "type", ""),
        "message": message,
        "node": node,
    }
    result[kind].append(entry)
    # Socket-level issues on otherwise-supported nodes (e.g. a Principled with
    # Sheen enabled) must not land in offending_nodes: Remove Offenders would
    # delete the whole node over one bad input.
    if kind == "errors" and removable:
        result["offending_nodes"].append(entry)
    elif kind != "errors":
        result["warning_nodes"].append(entry)


def _is_rk_group(node) -> bool:
    node_tree = getattr(node, "node_tree", None)
    if not node_tree:
        return False
    node_id = node_tree.get("rk_node_id")
    if node_id:
        return True
    name = (node_tree.name or "").lstrip(".")
    return metadata.is_catalog_group_name(name)


def _collect_used_nodes(material) -> Set[object]:
    from ..export.materials.extract.core import _active_output_node

    used_nodes: Set[object] = set()

    active_output = _active_output_node(material)
    if active_output is None:
        return used_nodes

    def visit(node):
        if node in used_nodes:
            return
        used_nodes.add(node)
        for input_socket in getattr(node, "inputs", []):
            if not input_socket.is_linked:
                continue
            for link in input_socket.links:
                from_node = link.from_node
                if from_node:
                    visit(from_node)

    for socket_name in ("Surface", "Volume", "Displacement"):
        socket = active_output.inputs.get(socket_name)
        if not socket or not socket.is_linked:
            continue
        for link in socket.links:
            if link.from_node:
                visit(link.from_node)

    used_nodes.add(active_output)
    return used_nodes


def _is_supported_mix(node) -> bool:
    """Match the exact Mix subset implemented by material extraction.

    The resolver's own gate is the one answer: it knows how Cycles treats the
    data type, factor clamping and blend of each Mix and MixRGB node, and a
    second copy here had already drifted from it once.
    """
    from ..export.materials.extract.core import _is_supported_mix as resolver_gate

    return resolver_gate(node)
