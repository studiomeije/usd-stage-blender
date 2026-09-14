"""
Blender material extraction for RealityKit export.

Extracts supported parameters and emits warnings for unsupported nodes.
"""

import hashlib
import math
import os
import re
import shutil
import struct
import tempfile
from array import array
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ....manifest.materialx_nodes import select_nodedef_name_for_node
from ..graph import (
    texture_colorspace_role,
)

_MANIFEST_CACHE: Optional[Dict[str, Any]] = None
_STAGED_IMAGE_CACHE: Dict[Any, str] = {}
_STAGED_IMAGE_DIR: Optional[Path] = None
_STAGED_IMAGE_DIR_OWNED = False

# Blender 5.2 no longer has a material render mode that means "alpha clip".
# A RealityKit opacityThreshold is therefore authored only when the scene opts
# into this exporter contract with an explicit, finite threshold value.
_ALPHA_CUTOUT_THRESHOLD_PROPERTY = "usd_stage_alpha_cutout_threshold"

_FORMAT_TO_EXTENSION = {
    "AVIF": ".avif",
    "PNG": ".png",
    "JPEG": ".jpg",
    "JPG": ".jpg",
    "TIFF": ".tif",
    "TIF": ".tif",
    "TARGA": ".tga",
    "TARGA_RAW": ".tga",
    "OPEN_EXR": ".exr",
    "OPEN_EXR_MULTILAYER": ".exr",
    "HDR": ".hdr",
    "BMP": ".bmp",
    "WEBP": ".webp",
}

_EXTENSION_TO_FORMAT = {
    ".avif": "AVIF",
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".tga": "TARGA",
    ".exr": "OPEN_EXR",
    ".hdr": "HDR",
    ".bmp": "BMP",
    ".webp": "WEBP",
}


def material_has_transparency(material) -> bool:
    """True when a material's alpha actually produces transparency.

    Blender 5.2's ``surface_render_method`` chooses how Eevee renders
    transparency; it does not say whether the active surface contains any.
    Transparency must be read from the real Alpha input instead.
    """
    if not material:
        return False
    node_tree = getattr(material, "node_tree", None)
    if not node_tree:
        return False
    # Only the shader connected to the active Material Output contributes to
    # rendering. Disconnected Principled nodes must not make an opaque material
    # transparent or disagree with the bake pipeline.
    surface_node = _get_surface_shader_node(material)
    if not surface_node:
        return False
    if surface_node.type == 'MIX_SHADER':
        # The bake lane supports a Mix Shader over Principled BSDFs; its
        # transparency is whatever either side's Alpha establishes, and a
        # mixed-in Transparent BSDF is transparency by construction.
        found_alpha = False
        for socket in getattr(surface_node, "inputs", []) or []:
            if not getattr(socket, "is_linked", False):
                continue
            links = list(getattr(socket, "links", ()) or ())
            source = getattr(links[0], "from_node", None) if links else None
            if source is None:
                continue
            source_type = getattr(source, "type", "")
            if source_type == 'BSDF_TRANSPARENT':
                return True
            if source_type == 'BSDF_PRINCIPLED':
                if _principled_alpha_is_transparent(source):
                    found_alpha = True
        return found_alpha
    if surface_node.type == 'ADD_SHADER':
        principled = resolve_surface_closure(material).get("principled")
        return principled is not None and _principled_alpha_is_transparent(principled)
    if surface_node.type != 'BSDF_PRINCIPLED':
        return False
    return _principled_alpha_is_transparent(surface_node)


def _principled_alpha_is_transparent(principled) -> bool:
    alpha_socket = principled.inputs.get('Alpha')
    if not alpha_socket:
        return False
    if alpha_socket.is_linked:
        return True
    try:
        return float(alpha_socket.default_value) < 0.999
    except (TypeError, ValueError):
        return False


def opacity_threshold_from_material(
    material,
    is_transparent: bool,
) -> Optional[float]:
    """Return an explicit RealityKit alpha-cutout threshold, if present.

    Blender 5.2 exposes only ``DITHERED`` and ``BLENDED`` surface render
    methods. Neither is a cutout declaration, so render method must never imply
    a hard threshold. Scenes that intentionally want a RealityKit cutout can
    set ``usd_stage_alpha_cutout_threshold`` to a finite value in [0, 1].
    A boolean flag is deliberately insufficient: without a numeric threshold
    there is no complete cutout contract to export.
    """
    if not is_transparent:
        return None
    try:
        value = material.get(_ALPHA_CUTOUT_THRESHOLD_PROPERTY)
    except Exception:
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        threshold = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (0.0 <= threshold <= 1.0):
        return None
    return threshold


def extract_blender_material_data(material) -> Dict[str, Any]:
    """Extract supported material parameters from a Blender material."""
    data = {
        'name': material.name,
        'type': 'unknown',
    }
    data['surface_render_method'] = getattr(
        material,
        "surface_render_method",
        "DITHERED",
    )
    data['is_transparent'] = material_has_transparency(material)

    surface_node = _get_surface_shader_node(material)
    if surface_node and surface_node.type == 'GROUP':
        node_tree = getattr(surface_node, "node_tree", None)
        node_id = node_tree.get("rk_node_id") if node_tree else None
        node_name = (node_tree.name or "").lstrip(".") if node_tree else ""
        if node_id or (node_tree and node_name.startswith("RK_")):
            return _extract_rk_group_material_data(surface_node, data)

    closure = resolve_surface_closure(material)
    principled = closure["principled"] if closure["refusal"] is None else None

    hair = hair_surface_node(material)
    if hair is not None and hair_refusal(material) is None:
        _extract_hair_material_data(hair, data)
        extract_vertex_offset(material, data)
        return data

    if principled:
        data['type'] = 'principled'
        resolve_cache = {}
        unresolved_warnings: List[str] = []
        input_graphs: Dict[str, Any] = {}

        graph_input_map = {
            'Base Color': 'baseColor',
            'Metallic': 'metallic',
            'Roughness': 'roughness',
            'Specular IOR Level': '_specularLevel',
            'Normal': 'normal',
            'Coat Normal': 'clearcoatNormal',
            'Alpha': 'opacity',
            'Emission Color': '_emissionColor',
            'Emission Strength': '_emissionStrength',
            'Subsurface Weight': 'subsurfaceWeight',
            'Subsurface Radius': 'subsurfaceRadiusScale',
            'Subsurface Scale': 'subsurfaceRadius',
            'Subsurface Anisotropy': 'subsurfaceScatterAnisotropy',
            'IOR': 'specularIOR',
            'Specular Tint': 'specularColor',
            'Anisotropic': 'specularAnisotropyLevel',
            'Anisotropic Rotation': 'specularAnisotropyAngle',
            'Coat IOR': 'clearcoatIOR',
            'Sheen Weight': '_sheenWeight',
            'Sheen Roughness': '_sheenRoughness',
            'Sheen Tint': '_sheenTint',
        }
        expected_type_map = {
            'Base Color': 'color3',
            'Metallic': 'float',
            'Roughness': 'float',
            'Specular IOR Level': 'float',
            'Normal': 'vector3',
            'Coat Normal': 'vector3',
            'Alpha': 'float',
            'Emission Color': 'color3',
            'Emission Strength': 'float',
            'Diffuse Roughness': 'float',
            'Subsurface Weight': 'float',
            'Subsurface Radius': 'color3',
            'Subsurface Scale': 'float',
            'Subsurface Anisotropy': 'float',
            'IOR': 'float',
            'Specular Tint': 'color3',
            'Anisotropic': 'float',
            'Anisotropic Rotation': 'float',
            'Coat IOR': 'float',
            'Sheen Weight': 'float',
            'Sheen Roughness': 'float',
            'Sheen Tint': 'color3',
        }

        base_color_socket = principled.inputs.get('Base Color')
        metallic_socket = principled.inputs.get('Metallic')
        roughness_socket = principled.inputs.get('Roughness')
        specular_socket = principled.inputs.get('Specular IOR Level')
        alpha_socket = principled.inputs.get('Alpha')

        if base_color_socket:
            data['base_color'] = list(base_color_socket.default_value)[:3]
        if metallic_socket:
            data['metallic'] = metallic_socket.default_value
        if roughness_socket:
            data['roughness'] = roughness_socket.default_value
        if specular_socket:
            data['specular'] = specular_socket.default_value
        if alpha_socket:
            data['alpha'] = alpha_socket.default_value

        emission_color_socket = principled.inputs.get('Emission Color')
        emission_strength_socket = principled.inputs.get('Emission Strength')
        if emission_color_socket:
            data['emission_color'] = list(emission_color_socket.default_value)[:3]
        if emission_strength_socket and not emission_strength_socket.is_linked:
            data['emission_strength'] = emission_strength_socket.default_value

        clearcoat_socket = principled.inputs.get('Coat Weight')
        clearcoat_roughness_socket = principled.inputs.get('Coat Roughness')
        if clearcoat_socket:
            data['clearcoat'] = clearcoat_socket.default_value
        if clearcoat_roughness_socket:
            data['clearcoat_roughness'] = clearcoat_roughness_socket.default_value

        pbr2_socket_map = {
            'Diffuse Roughness': ('diffuse_roughness', 'float'),
            'Subsurface Weight': ('subsurface_weight', 'float'),
            # Blender's scalar Scale is the physical radius; its RGB Radius is
            # the per-channel multiplier PBR Surface 2 takes.
            'Subsurface Scale': ('subsurface_radius', 'float'),
            'Subsurface Radius': ('subsurface_radius_scale', 'color'),
            'Subsurface Anisotropy': ('subsurface_anisotropy', 'float'),
            'IOR': ('ior', 'float'),
            'Specular Tint': ('specular_tint', 'color'),
            'Anisotropic': ('anisotropic', 'float'),
            'Anisotropic Rotation': ('anisotropic_rotation', 'float'),
            'Coat IOR': ('clearcoat_ior', 'float'),
            'Coat Tint': ('clearcoat_tint', 'color'),
            'Sheen Weight': ('sheen_weight', 'float'),
            'Sheen Roughness': ('sheen_roughness', 'float'),
            'Sheen Tint': ('sheen_tint', 'color'),
        }
        for socket_name, (data_name, value_kind) in pbr2_socket_map.items():
            socket = principled.inputs.get(socket_name)
            if socket is None or socket.is_linked:
                continue
            value = _socket_default_value(socket)
            if value is None:
                continue
            data[data_name] = _coerce_constant_value(value, value_kind)

        alpha_threshold = opacity_threshold_from_material(
            material,
            data['is_transparent'],
        )
        if alpha_threshold is not None:
            data['alpha_threshold'] = alpha_threshold

        texture_map = {
            'Base Color': 'base_color_texture',
            'Metallic': 'metallic_texture',
            'Roughness': 'roughness_texture',
            'Normal': 'normal_texture',
            'Alpha': 'alpha_texture',
            'Coat Normal': 'clearcoat_normal_texture',
            'Emission Color': 'emission_texture',
        }
        constant_map = {
            'Base Color': ('base_color', 'color'),
            'Metallic': ('metallic', 'float'),
            'Roughness': ('roughness', 'float'),
            'Specular IOR Level': ('specular', 'float'),
            'Alpha': ('alpha', 'float'),
            'Emission Color': ('emission_color', 'color'),
            'Emission Strength': ('emission_strength', 'float'),
            'Coat Weight': ('clearcoat', 'float'),
            'Coat Roughness': ('clearcoat_roughness', 'float'),
            'Diffuse Roughness': ('diffuse_roughness', 'float'),
            'Subsurface Weight': ('subsurface_weight', 'float'),
            'Subsurface Radius': ('subsurface_radius_scale', 'color'),
            'Subsurface Scale': ('subsurface_radius', 'float'),
            'Subsurface Anisotropy': ('subsurface_anisotropy', 'float'),
            'IOR': ('ior', 'float'),
            'Specular Tint': ('specular_tint', 'color'),
            'Anisotropic': ('anisotropic', 'float'),
            'Anisotropic Rotation': ('anisotropic_rotation', 'float'),
            'Coat IOR': ('clearcoat_ior', 'float'),
            'Coat Tint': ('clearcoat_tint', 'color'),
            'Sheen Weight': ('sheen_weight', 'float'),
            'Sheen Roughness': ('sheen_roughness', 'float'),
            'Sheen Tint': ('sheen_tint', 'color'),
        }
        for input_name, input_socket in principled.inputs.items():
            if not input_socket.is_linked:
                continue

            expected_type = expected_type_map.get(input_name)
            if input_name in ('Normal', 'Coat Normal'):
                # Already in the tangent space the surface takes: a Normal
                # Map's own decode, or a world-space normal turned into it.
                resolved = surface_normal_expr(input_socket, resolve_cache)
            else:
                resolved = _resolve_socket_value(
                    input_socket,
                    cache=resolve_cache,
                    expected_type=expected_type,
                )
            if resolved and resolved.get("kind") == "texture" and input_name in texture_map:
                texture_key = texture_map[input_name]
                data[texture_key] = resolved["path"]
                channel = resolved.get("channel")
                if channel:
                    data[f"{texture_key}_channel"] = channel
                uv_map = resolved.get("uv_map")
                if uv_map:
                    data[f"{texture_key}_texcoord"] = uv_map
                mapping = resolved.get("mapping")
                if mapping:
                    data[f"{texture_key}_mapping"] = mapping
                colorspace = resolved.get("colorspace")
                if colorspace:
                    data[f"{texture_key}_colorspace"] = colorspace
                alpha_mode = resolved.get("alpha_mode")
                if alpha_mode:
                    data[f"{texture_key}_alpha_mode"] = alpha_mode
                sampling = resolved.get("sampling")
                if sampling:
                    data[f"{texture_key}_sampling"] = sampling
                scale = resolved.get("scale")
                if scale is not None:
                    data[f"{texture_key}_scale"] = scale
                space = resolved.get("space")
                if space:
                    data[f"{texture_key}_space"] = space
                for source_key in ("source_channels", "source_has_alpha"):
                    source_value = resolved.get(source_key)
                    if source_value is not None:
                        data[f"{texture_key}_{source_key}"] = source_value
                if input_name == 'Base Color':
                    data.pop('base_color', None)
                    _record_base_color_texture_semantics(data, resolved)
                continue

            if resolved and resolved.get("kind") == "texture":
                target_input = graph_input_map.get(input_name)
                if target_input:
                    input_graphs[target_input] = resolved
                    continue

            if resolved and resolved.get("kind") == "node":
                target_input = graph_input_map.get(input_name)
                if target_input:
                    input_graphs[target_input] = resolved
                    if input_name == 'Base Color':
                        _record_base_color_texture_semantics(data, resolved)
                    continue

            if resolved and resolved.get("kind") == "constant" and input_name in constant_map:
                key, expected = constant_map[input_name]
                data[key] = _coerce_constant_value(resolved["value"], expected)
                continue

            if resolved and resolved.get("kind") == "unresolved":
                chain = resolved.get("provenance") or []
                if chain:
                    reason = resolved.get("reason")
                    suffix = f" ({reason})" if reason else ""
                    unresolved_warnings.append(
                        f"Material '{material.name}': Unable to resolve '{input_name}' "
                        f"through chain: {' -> '.join(chain)}{suffix}"
                    )

            constant = _extract_constant_from_socket(input_socket)
            if constant is not None and input_name in constant_map:
                key, expected = constant_map[input_name]
                data[key] = _coerce_constant_value(constant, expected)

            from_node = input_socket.links[0].from_node
            if from_node.type == 'TEX_IMAGE' and 'ao' in from_node.name.lower():
                texture_path = _resolve_image_path(from_node.image)
                if texture_path:
                    data['ao_texture'] = texture_path

        _apply_surface_closure(material, closure, principled, data, input_graphs, unresolved_warnings)
        if unresolved_warnings:
            data['unresolved_warnings'] = unresolved_warnings
        # Sheen (Weight x Tint), Specular IOR Level with IOR, and Diffuse
        # Roughness stay as the independent Blender controls; the graph
        # builder derives PBR Surface 2's inputs from them.
        if input_graphs:
            data['input_graphs'] = input_graphs

    else:
        # Only the shader wired to the active Material Output contributes.
        # Never replace an unsupported active shader with an orphan Emission.
        emission_node = (
            surface_node
            if surface_node and surface_node.type == 'EMISSION'
            else None
        )

        if emission_node:
            data['type'] = 'emission'
            color_socket = emission_node.inputs.get('Color')
            strength_socket = emission_node.inputs.get('Strength')
            resolve_cache = {}
            color_expr = None
            strength_expr = _constant_expr(1.0)

            if color_socket:
                if color_socket.is_linked:
                    color_expr = _resolve_socket_value(
                        color_socket,
                        cache=resolve_cache,
                        expected_type='color3',
                    )
                else:
                    color_expr = _constant_expr(
                        _coerce_constant_value(_socket_default_value(color_socket), 'color')
                    )

            if strength_socket:
                if strength_socket.is_linked:
                    strength_expr = _resolve_socket_value(
                        strength_socket,
                        cache=resolve_cache,
                        expected_type='float',
                    )
                else:
                    strength_expr = _constant_expr(
                        _coerce_constant_value(_socket_default_value(strength_socket), 'float')
                    )

            unresolved = []
            if not color_expr or color_expr.get('kind') == 'unresolved':
                unresolved.append(
                    f"Material '{material.name}': standalone Emission Color requires baking; "
                    "the linked graph could not be resolved exactly."
                )
            if not strength_expr or strength_expr.get('kind') == 'unresolved':
                unresolved.append(
                    f"Material '{material.name}': standalone Emission Strength requires baking; "
                    "the linked graph could not be resolved exactly."
                )

            if unresolved:
                data['unresolved_warnings'] = unresolved
            elif color_expr.get('kind') == 'constant' and strength_expr.get('kind') == 'constant':
                color = _coerce_constant_value(color_expr.get('value'), 'color')
                strength = _coerce_constant_value(strength_expr.get('value'), 'float')
                data['base_color'] = [component * strength for component in color]
                data['emission_strength'] = strength
            else:
                if color_expr.get('kind') == 'texture':
                    color_expr = dict(color_expr)
                    color_expr['colorspace_role'] = 'color'
                if strength_expr.get('kind') == 'texture':
                    strength_expr = dict(strength_expr)
                    strength_expr['colorspace_role'] = 'data'

                if strength_expr.get('kind') == 'constant':
                    strength = _coerce_constant_value(strength_expr.get('value'), 'float')
                    if color_expr.get('kind') == 'texture':
                        color_expr = dict(color_expr)
                        color_expr['scale'] = strength
                        final_expr = color_expr
                    elif abs(strength - 1.0) <= 1e-6:
                        final_expr = color_expr
                    else:
                        strength_expr = _constant_expr(strength)
                        strength_color = _make_node_expr(
                            _nodedef_for('combine3', 'color3'),
                            {
                                'in1': strength_expr,
                                'in2': strength_expr,
                                'in3': strength_expr,
                            },
                        )
                        final_expr = _make_node_expr(
                            _nodedef_for('multiply', 'color3'),
                            {'in1': color_expr, 'in2': strength_color},
                        )
                else:
                    strength_color = _make_node_expr(
                        _nodedef_for('combine3', 'color3'),
                        {
                            'in1': strength_expr,
                            'in2': strength_expr,
                            'in3': strength_expr,
                        },
                    )
                    final_expr = _make_node_expr(
                        _nodedef_for('multiply', 'color3'),
                        {'in1': color_expr, 'in2': strength_color},
                    )
                data['input_graphs'] = {'color': final_expr}

    extract_vertex_offset(material, data)
    return data


def collect_material_warnings(material) -> List[str]:
    """Collect warnings for Blender nodes unsupported by RealityKit export."""
    warnings: List[str] = []
    if not material or not material.node_tree:
        return warnings

    used_nodes, volume_linked, displacement_linked = _collect_used_nodes(material)
    if volume_linked:
        warnings.append(
            f"Material '{material.name}': Volume output is not supported in RCP; bake or remove."
        )
    # A linked Displacement is judged by the validator (``displacement_refusal``
    # and ``displacement_notices``), which names the node; repeating it here
    # printed every notice twice.

    # Derived from the validator rather than a second hand-maintained list.
    # The local copy had drifted 14 entries behind - TEX_NOISE, CLAMP,
    # MAP_RANGE, REROUTE and others export correctly but were reported as
    # "unrecognized; export may differ", and INVERT as "requires baking",
    # directly contradicting what `validate` had just told the user about the
    # same material. The validator is the capability authority; this pass only
    # phrases the warnings.
    from ....nodes.validate import BAKE_TYPES as _VALIDATOR_BAKE_TYPES
    from ....nodes.validate import SUPPORTED_TYPES as _VALIDATOR_SUPPORTED_TYPES
    from ....nodes.validate import (
        SUPPORTED_MATH_OPERATIONS as _VALIDATOR_SUPPORTED_MATH_OPS,
    )
    from ....nodes.validate import math_refusal_message as _math_refusal_message
    from ....nodes.validate import (
        SUPPORTED_VECTOR_MATH_OPERATIONS as _VALIDATOR_SUPPORTED_VECTOR_MATH_OPS,
    )
    from ....nodes.validate import vector_math_refusal_message as _vector_math_refusal_message

    from ....nodes.validate import UNSUPPORTED_TYPES as _VALIDATOR_UNSUPPORTED_TYPES

    supported_types = set(_VALIDATOR_SUPPORTED_TYPES)
    bake_types = set(_VALIDATOR_BAKE_TYPES)
    unsupported_types = set(_VALIDATOR_UNSUPPORTED_TYPES)


    for node in used_nodes:
        node_type = getattr(node, "type", "")
        node_name = getattr(node, "name", node_type)

        if node_type == 'GROUP':
            node_tree = getattr(node, "node_tree", None)
            node_id = node_tree.get("rk_node_id") if node_tree else None
            node_name = (node_tree.name or "").lstrip(".") if node_tree else ""
            if node_id or (node_tree and node_name.startswith("RK_")):
                continue
            warnings.append(
                f"Material '{material.name}': Node group '{node_name}' is not RCP-aware; bake or replace."
            )
            continue

        if node_type in supported_types:
            if node_type == 'TEX_IMAGE' and getattr(node, "image", None) is None:
                warnings.append(
                    f"Material '{material.name}': Image Texture node '{node_name}' has no image."
                )
            if node_type in {'TEX_NOISE', 'TEX_VORONOI'}:
                # Always warn: even with wired coordinates the exported
                # pattern is hashed by MaterialX's noise (fractal3d,
                # worleynoise3d), not Blender's, so only the value
                # distribution matches; an unwired Vector also substitutes
                # object-space position for Generated coordinates.
                warnings.append(
                    f"Material '{material.name}': Procedural texture "
                    f"'{node_name}' ({node_type}) exports with MaterialX's "
                    "noise, which has Blender's range and octaves but a "
                    "different hash; the pattern will not match Blender "
                    "pixel-for-pixel. Bake the material for an exact match."
                )
            if node_type == 'TEX_GRADIENT':
                vector = _named_socket(node, 'Vector')
                if vector is None or not getattr(vector, "is_linked", False):
                    # The gradient itself is transcribed exactly; only the
                    # coordinate stand-in differs.
                    warnings.append(
                        f"Material '{material.name}': Gradient Texture '{node_name}' has no "
                        "Vector linked, so it samples object-space position in place of "
                        "Blender's Generated coordinates, which are normalised to the object's "
                        "bounding box. Link a coordinate for an exact match."
                    )
            if node_type == 'TEX_CHECKER':
                vector = _named_socket(node, 'Vector')
                if vector is None or not getattr(vector, "is_linked", False):
                    warnings.append(
                        f"Material '{material.name}': Checker Texture '{node_name}' has no "
                        "Vector linked, so it samples object-space position in place of "
                        "Blender's Generated coordinates, which are normalised to the object's "
                        "bounding box; the checks will be sized differently. Link a coordinate "
                        "for an exact match."
                    )
            if node_type == 'TEX_IMAGE':
                projection = str(
                    getattr(node, "projection", "FLAT") or "FLAT"
                ).upper()
                if projection not in ('FLAT', 'BOX'):
                    warnings.append(
                        f"Material '{material.name}': Image Texture "
                        f"'{node_name}' projection {projection} requires "
                        "baking; only Flat and Box projections are exported."
                    )
            if node_type == 'VALTORGB':
                ramp = getattr(node, "color_ramp", None)
                interpolation = (getattr(ramp, "interpolation", "LINEAR") or "LINEAR").upper()
                color_mode = (getattr(ramp, "color_mode", "RGB") or "RGB").upper()
                if color_mode != "RGB" or interpolation not in {"LINEAR", "CONSTANT", "EASE"}:
                    warnings.append(
                        f"Material '{material.name}': Color Ramp '{node_name}' {color_mode}/"
                        f"{interpolation} requires baking."
                    )
            continue

        if node_type in _HAIR_NODE_TYPES:
            # Judged by the validator (``hair_issue`` and ``hair_notices``),
            # which names the node; repeating it here printed every notice
            # twice on a successful export.
            continue

        if node_type in _CLOSURE_NODE_TYPES or node_type == 'BSDF_TRANSPARENT':
            closure = resolve_surface_closure(material)
            reason = closure_issue(closure, node)
            if reason is not None:
                warnings.append(f"Material '{material.name}': {reason}")
            continue

        if node_type in PRINCIPLED_PRESET_TYPES:
            reason = principled_like_issue(resolve_surface_closure(material), node)
            if reason is not None:
                warnings.append(f"Material '{material.name}': {reason}")
            continue

        if node_type in {'MIX_RGB', 'MIX'}:
            if _is_supported_mix(node):
                continue
            warnings.append(
                f"Material '{material.name}': Node '{node_name}' ({node_type}) requires baking unless "
                "it is a multiply/add/subtract or plain mix of resolvable inputs, or Factor is 0/1 "
                "with a passthrough input."
            )
            continue

        if node_type == 'VECT_MATH':
            operation = (getattr(node, "operation", "") or "").upper()
            if operation in _VALIDATOR_SUPPORTED_VECTOR_MATH_OPS:
                continue
            warnings.append(
                f"Material '{material.name}': Node '{node_name}' (VECT_MATH): "
                + _vector_math_refusal_message(operation)
            )
            continue

        if node_type == 'MATH':
            operation = (getattr(node, "operation", "") or "").upper()
            if operation in _VALIDATOR_SUPPORTED_MATH_OPS:
                continue
            warnings.append(
                f"Material '{material.name}': Node '{node_name}' (MATH): "
                + _math_refusal_message(operation)
            )
            continue

        if node_type in bake_types:
            warnings.append(
                f"Material '{material.name}': Node '{node_name}' ({node_type}) requires baking for RCP."
            )
            continue

        if node_type in unsupported_types:
            warnings.append(
                f"Material '{material.name}': Node '{node_name}' ({node_type}) is not supported by RCP."
            )
            continue

        warnings.append(
            f"Material '{material.name}': Node '{node_name}' ({node_type}) is unrecognized; export may differ."
        )

    return _dedupe_warnings(warnings)


def _collect_used_nodes(material):
    """Collect nodes contributing to the active material output."""
    used_nodes = set()
    volume_linked = False
    displacement_linked = False

    active_output = _active_output_node(material)
    if active_output is None:
        return used_nodes, volume_linked, displacement_linked

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
        if socket_name == "Volume":
            volume_linked = True
        if socket_name == "Displacement":
            displacement_linked = True
        for link in socket.links:
            if link.from_node:
                visit(link.from_node)

    return used_nodes, volume_linked, displacement_linked


def _dedupe_warnings(warnings: List[str]) -> List[str]:
    """Deduplicate warnings while preserving order."""
    seen = set()
    deduped = []
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        deduped.append(warning)
    return deduped


def _extract_rk_group_material_data(group_node, base_data: Dict[str, Any]) -> Dict[str, Any]:
    """Extract inputs from a RealityKit node group."""
    data = dict(base_data)
    graph = _build_rk_node_graph(group_node)
    if graph:
        data['type'] = 'rk_graph'
        data['rk_graph'] = graph
        for key in (
            'base_color_texture_sources',
            'base_color_texture_alpha_modes',
            'base_color_alpha_semantics_error',
            'has_premultiplied_alpha',
        ):
            if key in graph:
                data[key] = graph[key]
        return data

    node_tree = group_node.node_tree
    node_id = node_tree.get("rk_node_id") if node_tree else None

    if not node_id:
        node_id = _infer_rk_node_id(node_tree.name if node_tree else "")

    data['type'] = 'rk_group'
    data['rk_node_id'] = node_id
    data['rk_inputs'] = _extract_group_inputs(group_node, node_id)
    base_color_inputs = [
        value
        for name, value in data['rk_inputs'].items()
        if _is_surface_base_color_input(name)
    ]
    sources = []
    for value in base_color_inputs:
        sources.extend(_texture_specs_from_value(value))
    _apply_base_color_texture_semantics(data, sources)
    if (
        data.get('has_premultiplied_alpha')
        and _input_mtlx_type(node_id, 'hasPremultipliedAlpha')
    ):
        data['rk_inputs']['hasPremultipliedAlpha'] = True
    return data


def _infer_rk_node_id(group_name: str) -> str:
    """Infer a RealityKit node id from a group name."""
    name = (group_name or "").lstrip(".").lower()
    if "pbr surface" in name or name.startswith("rk_pbr"):
        return "realitykit_pbr_surfaceshader"
    if "unlit surface" in name or name.startswith("rk_unlit"):
        return "realitykit_unlit_surfaceshader"
    return group_name


def _get_manifest() -> Dict[str, Any]:
    """Load and cache the MaterialX manifest."""
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        from ....manifest.materialx_nodes import load_manifest

        _MANIFEST_CACHE = load_manifest()
    return _MANIFEST_CACHE


def _input_mtlx_type(node_id: Optional[str], input_name: str) -> Optional[str]:
    """Look up the MaterialX type for a node input."""
    if not node_id:
        return None
    manifest = _get_manifest()
    try:
        from ....manifest.materialx_nodes import select_node_def_for_node
        node_def = select_node_def_for_node(manifest, node_id)
    except Exception:
        node_def = None
    if not node_def and isinstance(node_id, str) and node_id.startswith("ND_"):
        node_def = manifest.get("nodes", {}).get(node_id)
    if not node_def:
        return None
    for input_def in node_def.get("inputs", []):
        if input_def.get("name") == input_name:
            return input_def.get("type")
    return None


def _mtlx_type_to_output_type(type_name: Optional[str]) -> Optional[str]:
    """Map MaterialX types to texture output hints."""
    if not type_name:
        return None
    type_name = type_name.lower()
    if type_name in {"color3", "half3"}:
        return "color3"
    if type_name in {"color4", "half4"}:
        return "color4"
    if type_name in {"vector2", "half2"}:
        return "vector2"
    if type_name in {"vector3"}:
        return "vector3"
    if type_name in {"vector4"}:
        return "vector4"
    if type_name in {"float", "half", "integer", "int"}:
        return "float"
    return None


def _build_rk_node_graph(surface_node) -> Optional[Dict[str, Any]]:
    """Build a MaterialX-style graph from RealityKit group nodes."""
    if not _is_rk_group_node(surface_node):
        return None

    nodes: List[Dict[str, Any]] = []
    connections: List[Dict[str, str]] = []
    node_map: Dict[object, str] = {}
    used_names: Set[str] = set()

    def _unique_name(base: str) -> str:
        base = _sanitize_node_name(base)
        if base not in used_names:
            used_names.add(base)
            return base
        idx = 1
        while f"{base}_{idx}" in used_names:
            idx += 1
        name = f"{base}_{idx}"
        used_names.add(name)
        return name

    def _node_name(node) -> str:
        if node in node_map:
            return node_map[node]
        label = (node.label or node.name or node.node_tree.name or "Node")
        name = _unique_name(label)
        node_map[node] = name
        return name

    def visit(node) -> None:
        if node in node_map:
            return
        if not _is_rk_group_node(node):
            return

        node_id = node.node_tree.get("rk_node_id") if node.node_tree else None
        if not node_id:
            return

        node_name = _node_name(node)
        inputs: Dict[str, Any] = {}

        for socket in node.inputs:
            if not socket:
                continue
            input_name = socket.name

            if socket.is_linked:
                link = socket.links[0]
                from_node = link.from_node
                if _is_rk_group_node(from_node):
                    visit(from_node)
                    connections.append(
                        {
                            "from_node": _node_name(from_node),
                            "from_output": link.from_socket.name,
                            "to_node": node_name,
                            "to_input": input_name,
                        }
                    )
                    continue

                texture_path = _extract_image_path_from_socket(socket)
                if texture_path:
                    mtlx_type = _input_mtlx_type(node_id, input_name)
                    output_type = _mtlx_type_to_output_type(mtlx_type) or _socket_output_type(socket)
                    texture_spec = {
                        'type': (
                            'normal_texture'
                            if _input_expects_decoded_normal(node_id, input_name)
                            else 'texture'
                        ),
                        'path': texture_path,
                        'output_type': output_type,
                        # Without a role this texture skips the data-texture
                        # colour-space branch, so a normal or roughness image
                        # left at Blender's default sRGB would be decoded
                        # without the warning the Principled path gives.
                        'colorspace_role': texture_colorspace_role(input_name),
                    }
                    uv_map = _extract_uv_map_from_socket(socket)
                    if uv_map:
                        texture_spec['texcoord'] = uv_map
                    mapping = _extract_mapping_from_socket(socket)
                    if mapping:
                        texture_spec['mapping'] = mapping
                    colorspace = _extract_colorspace_from_socket(socket)
                    if colorspace:
                        texture_spec['colorspace'] = colorspace
                    alpha_mode = _extract_alpha_mode_from_socket(socket)
                    if alpha_mode:
                        texture_spec['alpha_mode'] = alpha_mode
                    texture_spec.update(
                        _extract_source_alpha_from_socket(socket, texture_path)
                    )
                    inputs[input_name] = texture_spec
                    continue

                constant = _extract_constant_from_socket(socket)
                if constant is not None:
                    inputs[input_name] = constant
                    continue

            default_value = _socket_default_value(socket)
            if default_value is not None:
                inputs[input_name] = default_value

        nodes.append(
            {
                "name": node_name,
                "node_id": node_id,
                "inputs": inputs,
            }
        )

    visit(surface_node)
    if not nodes:
        return None

    result = {
        "nodes": nodes,
        "connections": connections,
        "output": _node_name(surface_node),
    }
    _apply_base_color_texture_semantics(
        result,
        _rk_graph_base_color_texture_sources(result),
    )
    if result.get("has_premultiplied_alpha"):
        surface = next(
            (node for node in nodes if node.get("name") == result["output"]),
            None,
        )
        if surface and _input_mtlx_type(
            surface.get("node_id"),
            "hasPremultipliedAlpha",
        ):
            surface.setdefault("inputs", {})["hasPremultipliedAlpha"] = True
    return result


def _extract_group_inputs(group_node, rk_node_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract group input values and texture references."""
    inputs = {}
    for socket in group_node.inputs:
        input_name = socket.name
        if socket.is_linked:
            texture_path = _extract_image_path_from_socket(socket)
            if texture_path:
                output_type = _socket_output_type(socket)
                tex_type = (
                    'normal_texture'
                    if _input_expects_decoded_normal(rk_node_id, input_name)
                    else 'texture'
                )
                texture_spec = {
                    'type': tex_type,
                    'path': texture_path,
                    'output_type': output_type,
                    # See _build_rk_node_graph: an untagged texture skips
                    # the data-texture colour-space warning.
                    'colorspace_role': texture_colorspace_role(input_name),
                }
                uv_map = _extract_uv_map_from_socket(socket)
                if uv_map:
                    texture_spec['texcoord'] = uv_map
                mapping = _extract_mapping_from_socket(socket)
                if mapping:
                    texture_spec['mapping'] = mapping
                colorspace = _extract_colorspace_from_socket(socket)
                if colorspace:
                    texture_spec['colorspace'] = colorspace
                alpha_mode = _extract_alpha_mode_from_socket(socket)
                if alpha_mode:
                    texture_spec['alpha_mode'] = alpha_mode
                texture_spec.update(
                    _extract_source_alpha_from_socket(socket, texture_path)
                )
                inputs[input_name] = texture_spec
            else:
                value = _socket_default_value(socket)
                if value is not None:
                    inputs[input_name] = value
        else:
            value = _socket_default_value(socket)
            if value is not None:
                inputs[input_name] = value

    return inputs


def _socket_output_type(socket) -> str:
    """Infer MaterialX output type for a Blender socket."""
    socket_type = getattr(socket, "type", "") or ""
    if socket_type in {'VALUE', 'FLOAT', 'INT'}:
        return 'float'
    if socket_type in {'BOOLEAN', 'BOOL'}:
        return 'boolean'
    if socket_type in {'VECTOR'}:
        return 'vector3'
    if socket_type in {'RGBA', 'COLOR'}:
        return 'color4'

    default = _socket_default_value(socket)
    if isinstance(default, (list, tuple)):
        if len(default) == 4:
            return 'color4'
        if len(default) == 3:
            return 'color3'
        if len(default) == 2:
            return 'vector2'
    return 'color3'


def _socket_default_value(socket):
    """Get a socket default value normalized to Python primitives."""
    if not hasattr(socket, "default_value"):
        return None
    value = socket.default_value
    if isinstance(value, str):
        return value
    if isinstance(value, (float, int, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    # mathutils.Euler is a sequence by indexing only, with no __iter__, so
    # test for either protocol; list() handles both.
    if (hasattr(value, "__iter__") or hasattr(value, "__getitem__")) and not isinstance(value, (str, bytes)):
        try:
            return [float(v) for v in list(value)]
        except (TypeError, ValueError):
            return None
    return None


def _is_rk_group_node(node) -> bool:
    """Return True for RealityKit-authored node groups."""
    if not node or getattr(node, "type", None) != 'GROUP':
        return False
    node_tree = getattr(node, "node_tree", None)
    if not node_tree:
        return False
    return bool(node_tree.get("rk_node_id"))


def _sanitize_node_name(value: str) -> str:
    """Sanitize a name for use as a USD prim."""
    if not value:
        return "node"
    sanitized = re.sub(r'[^A-Za-z0-9_]', '_', value)
    if not sanitized[0].isalpha() and sanitized[0] != '_':
        sanitized = f"n_{sanitized}"
    return sanitized


def _node_label(node, socket=None) -> str:
    """Return a readable node label for provenance."""
    if not node:
        return "Unknown"
    name = getattr(node, "name", None) or getattr(node, "label", None) or node.type
    socket_name = getattr(socket, "name", None)
    if socket_name:
        return f"{name} ({node.type}:{socket_name})"
    return f"{name} ({node.type})"
def _is_unit_z_vector(value) -> bool:
    """Whether a nodedef default spells the unit Z vector, in any formatting.

    Judged numerically: the manifest carries both "0, 0, 1" (the surface
    normals) and "0.0, 0.0, 1.0" (transformnormal), and a literal string
    comparison would flip answers under any regeneration that renumbers
    defaults.
    """
    if value is None:
        return False
    parts = str(value).split(",")
    if len(parts) != 3:
        return False
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return False
    return numbers == [0.0, 0.0, 1.0]


def _input_expects_decoded_normal(node_id: Optional[str], input_name: str) -> bool:
    """Whether ``input_name`` on ``node_id`` receives a decoded normal.

    Answered from the nodedef rather than the socket name alone so both RK
    extraction paths reach the same conclusion. _extract_group_inputs decided
    this by name and got a normal_map_decode; _build_rk_node_graph never
    decided it at all and authored a raw colour->vector convert, so the same RK
    PBR Surface group produced different normals depending on which path ran.

    A vector3 input is a decoded-normal socket when its declared default is
    the unit Z vector AND its name says normal. Both conditions are load
    bearing: measured across the shipped manifest, the five real normal
    sockets (normal, clearcoatNormal, bentNormal on the RK surfaces) satisfy
    both, while ``ND_transformnormal_vector3.in`` also defaults to unit Z but
    receives an ordinary direction — the default alone is not a semantic.

    Falls back to the name when the nodedef cannot be resolved - a user node
    group with no manifest entry - which is the only signal available there.
    """
    if not input_name:
        return False
    declared_type = _input_mtlx_type(node_id, input_name)
    if declared_type == "vector3":
        default = _input_mtlx_default(node_id, input_name)
        if default is not None:
            return (
                _is_unit_z_vector(default)
                and "normal" in input_name.lower()
            )
    if declared_type is not None:
        # The nodedef resolved and this is not a decoded-normal input.
        return False
    return "normal" in input_name.lower()


def _input_mtlx_default(node_id: Optional[str], input_name: str):
    """Return the declared default for a nodedef input, or None."""
    if not node_id:
        return None
    manifest = _get_manifest()
    try:
        from ....manifest.materialx_nodes import select_node_def_for_node
        node_def = select_node_def_for_node(manifest, node_id)
    except Exception:
        node_def = None
    if not node_def and isinstance(node_id, str) and node_id.startswith("ND_"):
        node_def = manifest.get("nodes", {}).get(node_id)
    if not node_def:
        return None
    for entry in node_def.get("inputs", []) or []:
        if entry.get("name") == input_name:
            return entry.get("value")
    return None


def _extract_image_path_from_socket(socket) -> Optional[str]:
    """Get image file path from a linked socket."""
    if not socket or not socket.is_linked:
        return None

    from_node = socket.links[0].from_node
    image = _extract_image_from_node(from_node)
    return _resolve_image_path(image)


def _extract_colorspace_from_socket(socket) -> Optional[str]:
    """Get the colorspace name from a linked image texture."""
    image_node = _extract_image_node_from_socket(socket)
    if not image_node or not getattr(image_node, "image", None):
        return None
    try:
        name = image_node.image.colorspace_settings.name
    except Exception:
        return None
    return _normalize_colorspace(name)


def _extract_source_alpha_from_socket(socket, texture_path: Optional[str]) -> Dict[str, Any]:
    """Get the source channel count for a linked image texture."""
    try:
        image_node = _extract_image_node_from_socket(socket)
    except Exception:
        return {}
    image = getattr(image_node, "image", None) if image_node else None
    if not image:
        return {}
    return _image_source_alpha(image, texture_path)


def _extract_alpha_mode_from_socket(socket) -> Optional[str]:
    """Get alpha mode for a linked image texture."""
    image_node = _extract_image_node_from_socket(socket)
    if not image_node or not getattr(image_node, "image", None):
        return None
    mode = getattr(image_node.image, "alpha_mode", None)
    if not mode:
        return None
    mode = str(mode).upper()
    if mode == 'PREMUL':
        return 'premul'
    if mode == 'STRAIGHT':
        return 'straight'
    return mode.lower()


def _extract_image_node_from_socket(socket):
    """Resolve the image node linked into a socket, if any."""
    if not socket or not socket.is_linked:
        return None
    from_node = socket.links[0].from_node
    return _extract_image_node(from_node)


def _extract_mapping_from_socket(socket) -> Optional[Dict[str, Any]]:
    """Get UV mapping transform info from a linked socket."""
    image_node = _extract_image_node_from_socket(socket)
    if not image_node:
        return None
    vector_socket = image_node.inputs.get("Vector") if hasattr(image_node, "inputs") else None
    if not vector_socket or not vector_socket.is_linked:
        return None
    return _extract_mapping_from_node(vector_socket.links[0].from_node)

def _extract_uv_map_from_socket(socket) -> Optional[str]:
    """Get UV map name for a linked texture socket if present."""
    if not socket or not socket.is_linked:
        return None

    from_node = socket.links[0].from_node
    image_node = _extract_image_node(from_node)
    if not image_node:
        return None

    uv_map = getattr(image_node, "uv_map", "") or ""
    vector_socket = image_node.inputs.get("Vector") if hasattr(image_node, "inputs") else None
    if not uv_map and vector_socket and vector_socket.is_linked:
        uv_map = _extract_uv_map_from_node(vector_socket.links[0].from_node)
    if not uv_map:
        return None
    texcoord, refusal = uv_set_resolution(image_node, uv_map)
    if refusal is not None:
        raise ValueError(refusal)
    return texcoord


def _get_surface_shader_node(material):
    """Return the shader node connected to the active Material Output surface."""
    active_output = _active_output_node(material)
    if active_output is None:
        return None

    surface_socket = active_output.inputs.get('Surface')
    if not surface_socket or not surface_socket.is_linked:
        return None
    link = surface_socket.links[0]
    return link.from_node if link else None


def _extract_image_from_node(node):
    """Resolve an image from known Blender node types."""
    image_node = _extract_image_node(node)
    if image_node:
        return image_node.image
    return None


def _extract_image_node(node):
    """Resolve an image node from known Blender node types."""
    if not node:
        return None

    if node.type == 'TEX_IMAGE':
        return node

    if node.type == 'SEPARATE_COLOR':
        input_socket = node.inputs.get('Color') if hasattr(node, "inputs") else None
        if input_socket is None:
            input_socket = node.inputs.get('Image') if hasattr(node, "inputs") else None
        if input_socket and input_socket.is_linked:
            return _extract_image_node(input_socket.links[0].from_node)

    if node.type == 'NORMAL_MAP':
        color_socket = node.inputs.get('Color')
        if color_socket and color_socket.is_linked:
            return _extract_image_node(color_socket.links[0].from_node)

    if node.type == 'BUMP':
        height_socket = node.inputs.get('Height')
        if height_socket and height_socket.is_linked:
            return _extract_image_node(height_socket.links[0].from_node)

    return None


def _resolve_socket_value(
    socket,
    visited=None,
    channel=None,
    provenance=None,
    cache: Optional[Dict[Any, Dict[str, Any]]] = None,
    expected_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a linked socket to a texture or constant spec."""
    if not socket or not socket.is_linked:
        return None

    if visited is None:
        visited = set()
    if provenance is None:
        provenance = []

    cache_key = None
    if cache is not None and hasattr(socket, "as_pointer"):
        try:
            cache_key = (socket.as_pointer(), channel, expected_type)
        except Exception:
            cache_key = None
    if cache is not None and cache_key is not None and cache_key in cache:
        return dict(cache[cache_key])

    link = socket.links[0]
    from_node = getattr(link, "from_node", None)
    from_socket = getattr(link, "from_socket", None)
    if not from_node:
        return None

    if from_node in visited:
        return None
    visited.add(from_node)

    provenance.append(_node_label(from_node, from_socket))
    node_type = getattr(from_node, "type", "")

    if node_type == '_USDSTAGE_DERIVED':
        expr = from_node.build(visited, provenance, cache, expected_type)
        if expr is None:
            return {"kind": "unresolved", "provenance": list(provenance)}
        return expr

    if node_type == 'REROUTE':
        input_socket = from_node.inputs[0] if from_node.inputs else None
        return _resolve_socket_value(
            input_socket,
            visited,
            channel,
            provenance,
            cache,
            expected_type=expected_type,
        )

    if node_type == 'SEPARATE_COLOR':
        # The output socket names the channel: Red, Green, Blue in every mode.
        # A channel a downstream Separate node asked of this float output does
        # not change which component this node reads.
        ch = _channel_from_socket_name(from_socket.name if from_socket else "")
        mode = (getattr(from_node, "mode", "RGB") or "RGB").upper()
        if mode == 'HSL':
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    "Separate Color in HSL mode has no exact MaterialX form (RealityKit "
                    "has no RGB to HSL node); use RGB or HSV mode, or bake the material"
                ),
            }
        if mode == 'HSV':
            color = _named_color_operand(from_node, 'Color', visited, provenance, cache)
            if not isinstance(color, dict) or color.get("kind") == "unresolved":
                return color or {"kind": "unresolved", "provenance": list(provenance)}
            return _component_of(_cycles_rgb_to_hsv(color), ch or "r")
        input_socket = from_node.inputs.get('Color') if hasattr(from_node, "inputs") else None
        if input_socket is None and hasattr(from_node, "inputs") and from_node.inputs:
            input_socket = from_node.inputs[0]
        resolved = _resolve_socket_value(
            input_socket,
            visited,
            None,
            provenance,
            cache,
            expected_type='color3',
        )
        if not ch or not isinstance(resolved, dict):
            return resolved
        return _component_of(resolved, ch)

    if node_type == 'SEPXYZ':
        ch = _channel_from_socket_name(from_socket.name if from_socket else "")
        input_socket = from_node.inputs.get('Vector') if hasattr(from_node, "inputs") else None
        if input_socket is None and hasattr(from_node, "inputs") and from_node.inputs:
            input_socket = from_node.inputs[0]
        resolved = _resolve_socket_value(
            input_socket,
            visited,
            None,
            provenance,
            cache,
            expected_type='vector3',
        )
        if not ch or not isinstance(resolved, dict):
            return resolved
        # A colour, vector or color4 node hands back its whole value; the
        # component read is authored explicitly (a colour reads as a vector
        # component for component, so X is red).
        return _component_of(resolved, ch)

    if node_type == 'NORMAL_MAP':
        # The node's output is a world-space normal. Straight into a
        # Principled Normal it is authored in tangent space instead
        # (surface_normal_expr); anything else gets Blender's world vector.
        tangent = _normal_map_tangent_expr(from_node, visited, provenance[:-1], cache)
        if not tangent or tangent.get("kind") == "unresolved":
            return tangent
        return _coerce_output(tangent_normal_to_world(tangent), expected_type, channel)

    if node_type == 'BUMP':
        expr = _bump_expr(from_node, visited, provenance, cache)
        if expr is None:
            return {"kind": "unresolved", "provenance": list(provenance)}
        return _coerce_output(expr, expected_type, channel)

    if node_type in ('MAPPING', 'GAMMA', 'TEX_CHECKER', 'CAMERA', 'ATTRIBUTE', 'OBJECT_INFO', 'UVMAP'):
        if node_type == 'ATTRIBUTE':
            expr = _attribute_node_expr(from_node, from_socket, provenance)
        elif node_type == 'UVMAP':
            expr = _uv_map_node_expr(from_node, provenance)
        elif node_type == 'OBJECT_INFO':
            expr = _object_info_expr(from_socket, provenance)
        elif node_type == 'MAPPING':
            expr = _mapping_vector_expr(from_node, visited, provenance, cache)
        elif node_type == 'GAMMA':
            expr = _gamma_expr(from_node, visited, provenance, cache)
        elif node_type == 'TEX_CHECKER':
            expr = _checker_expr(from_node, from_socket, visited, provenance, cache)
        else:
            expr = _camera_data_expr(getattr(from_socket, "name", "") or "")
        if expr is not None:
            if expr.get("kind") == "constant" and expected_type in ("color3", "vector3"):
                value = expr.get("value")
                if not isinstance(value, (list, tuple)):
                    expr = _constant_expr((float(value),) * 3)
            expr = _coerce_output(expr, expected_type, channel)
            if cache is not None and cache_key is not None:
                cache[cache_key] = dict(expr)
            return expr
        return {"kind": "unresolved", "provenance": list(provenance)}

    if node_type == 'GROUP':
        group_tree = getattr(from_node, "node_tree", None)
        if not group_tree:
            return {"kind": "unresolved", "provenance": list(provenance)}

        outputs = [n for n in group_tree.nodes if n.type == 'GROUP_OUTPUT']
        if not outputs:
            return {"kind": "unresolved", "provenance": list(provenance)}
        output_node = next((n for n in outputs if getattr(n, "is_active_output", False)), outputs[0])

        input_socket = None
        if from_socket and hasattr(output_node, "inputs"):
            input_socket = output_node.inputs.get(from_socket.name)
        if input_socket is None and from_socket and hasattr(from_node, "outputs"):
            try:
                index = list(from_node.outputs).index(from_socket)
                if hasattr(output_node, "inputs") and index < len(output_node.inputs):
                    input_socket = output_node.inputs[index]
            except Exception:
                input_socket = None

        if input_socket and input_socket.is_linked:
            return _resolve_socket_value(
                input_socket,
                visited,
                channel,
                provenance,
                cache,
                expected_type=expected_type,
            )
        return {"kind": "unresolved", "provenance": list(provenance)}

    if node_type in {'MIX_RGB', 'MIX'}:
        expr = _mix_expr(from_node, visited, channel, provenance, cache, expected_type)
        if expr is not None:
            return expr
        # A shape the capability gate refuses falls through to the unresolved
        # tail so the bake advice fires.


    if node_type == 'MATH':
        operation = (getattr(from_node, "operation", "") or "").upper()
        use_clamp = bool(getattr(from_node, "use_clamp", False))
        # Collapse a pass-through (add 0, subtract 0, multiply 1, divide 1)
        # to its linked input instead of authoring a no-op MaterialX node.
        # A clamped pass-through is NOT a pass-through - the clamp must be
        # authored, so it takes the general path below. The input is still a
        # scalar socket: a colour or vector feeding it converts to a float
        # first, and the float then broadcasts to the consumer.
        if not use_clamp and hasattr(from_node, "inputs") and len(from_node.inputs) >= 2:
            in0 = from_node.inputs[0]
            in1 = from_node.inputs[1]
            for linked, other, linked_index in ((in0, in1, 0), (in1, in0, 1)):
                if not (linked and linked.is_linked) or (other is not None and _socket_is_live(other)):
                    continue
                try:
                    value = float(other.default_value)
                except Exception:
                    value = None
                if _is_identity_math(operation, value, linked_index=linked_index):
                    passed = _float_math_input_expr(_resolve_socket_value(
                        linked,
                        visited,
                        None,
                        provenance,
                        cache,
                        expected_type="float",
                    ))
                    return _float_to_expected(passed, expected_type)
        expr = _math_node_expr(from_node, operation, visited, channel, provenance, cache)
        if expr is not None:
            # Math is always scalar; a colour/vector consumer needs an
            # explicit broadcast, matching Blender's implicit conversion.
            return _float_to_expected(expr, expected_type)
        # Unsupported operation: fall through to the unresolved tail so the
        # bake advice fires - never approximate silently.

    if node_type == 'CLAMP':
        # Float only: a colour or vector Value converts first, and the float
        # result converts for the consumer.
        return _typed_result(_clamp_node_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'MAP_RANGE':
        return _typed_result(_map_range_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'HUE_SAT':
        return _typed_result(_hue_saturation_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'INVERT':
        return _typed_result(_invert_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'BRIGHTCONTRAST':
        return _typed_result(_bright_contrast_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'VALTORGB':
        fac_expr = _expr_from_socket(
            from_node.inputs.get('Fac') if hasattr(from_node, "inputs") else None,
            visited,
            channel,
            provenance,
            cache,
            default=0.0,
        )
        ramp = getattr(from_node, "color_ramp", None)
        elements = sorted(
            list(getattr(ramp, "elements", []) or []),
            key=lambda item: float(getattr(item, "position", 0.0)),
        )
        if not elements:
            return {"kind": "unresolved", "provenance": list(provenance)}

        interpolation = (getattr(ramp, "interpolation", "LINEAR") or "LINEAR").upper()
        color_mode = (getattr(ramp, "color_mode", "RGB") or "RGB").upper()
        if color_mode != "RGB" or interpolation not in {"LINEAR", "CONSTANT", "EASE"}:
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    f"Color Ramp mode {color_mode}/{interpolation} requires baking; "
                    "the OS 27 exporter supports RGB Linear, Constant, and Ease"
                ),
            }

        output_name = (getattr(from_socket, "name", "") or "").lower()
        alpha_output = output_name == "alpha"
        ramp_type = "float" if alpha_output else "color3"

        stops = []
        for element in elements:
            position = float(getattr(element, "position", 0.0))
            color = list(getattr(element, "color", (0.0, 0.0, 0.0, 1.0)))
            while len(color) < 4:
                color.append(1.0)
            value = float(color[3]) if alpha_output else [float(v) for v in color[:3]]
            stops.append((position, value))

        result = _constant_expr(stops[0][1])
        for index in range(1, len(stops)):
            left_position, left_value = stops[index - 1]
            right_position, right_value = stops[index]

            if interpolation == "CONSTANT" or right_position <= left_position:
                result = _make_node_expr(
                    _nodedef_for("ifgreatereq", ramp_type),
                    {
                        "value1": fac_expr,
                        "value2": _constant_expr(right_position),
                        "in1": _constant_expr(right_value),
                        "in2": result,
                    },
                )
                continue

            if interpolation == "EASE":
                mix_factor = _make_node_expr(
                    _nodedef_for("smoothstep", "float"),
                    {
                        "in": fac_expr,
                        "low": _constant_expr(left_position),
                        "high": _constant_expr(right_position),
                    },
                )
            else:
                mix_factor = _make_node_expr(
                    _nodedef_for("range", "float"),
                    {
                        "in": fac_expr,
                        "inlow": _constant_expr(left_position),
                        "inhigh": _constant_expr(right_position),
                        "outlow": _constant_expr(0.0),
                        "outhigh": _constant_expr(1.0),
                        "gamma": _constant_expr(1.0),
                        "doclamp": _constant_expr(True),
                    },
                )
            segment = _make_node_expr(
                _nodedef_for("mix", ramp_type),
                {
                    "bg": _constant_expr(left_value),
                    "fg": _constant_expr(right_value),
                    "mix": mix_factor,
                },
            )
            result = _make_node_expr(
                _nodedef_for("ifgreatereq", ramp_type),
                {
                    "value1": fac_expr,
                    "value2": _constant_expr(left_position),
                    "in1": segment,
                    "in2": result,
                },
            )
        return result

    if node_type == 'CURVE_RGB':
        # The validator requires baking for RGB Curves: ND_curveadjust has no
        # implementation in RealityKit's MaterialX 1.39 library, so an authored
        # curve would cost the material its shader graph.
        return _unresolved(provenance, "RGB Curves requires baking; its curve node has no RealityKit implementation")

    if node_type == 'RGBTOBW':
        return _float_to_expected(_rgb_to_bw_expr(from_node, visited, provenance, cache), expected_type)

    if node_type == 'COMBINE_COLOR':
        # Blender 5.2 names the sockets Red, Green, Blue (and A when present).
        # Reading 'R'/'G'/'B' matched nothing and folded every Combine Color
        # to black; measured on t25_surface_readers' UV cube.
        mode = (getattr(from_node, "mode", "") or "").upper()
        if mode and mode != "RGB":
            return {"kind": "unresolved", "provenance": list(provenance)}
        r_expr = _expr_from_socket(
            from_node.inputs.get('Red') if hasattr(from_node, "inputs") else None,
            visited,
            channel,
            provenance,
            cache,
            default=0.0,
        )
        g_expr = _expr_from_socket(
            from_node.inputs.get('Green') if hasattr(from_node, "inputs") else None,
            visited,
            channel,
            provenance,
            cache,
            default=0.0,
        )
        b_expr = _expr_from_socket(
            from_node.inputs.get('Blue') if hasattr(from_node, "inputs") else None,
            visited,
            channel,
            provenance,
            cache,
            default=0.0,
        )
        a_socket = from_node.inputs.get('A') if hasattr(from_node, "inputs") else None
        if a_socket:
            a_expr = _expr_from_socket(
                a_socket,
                visited,
                channel,
                provenance,
                cache,
                default=1.0,
            )
            return _make_node_expr(
                _nodedef_for("combine4", "color4"),
                {"in1": r_expr, "in2": g_expr, "in3": b_expr, "in4": a_expr},
            )
        return _make_node_expr(
            _nodedef_for("combine3", "color3"),
            {"in1": r_expr, "in2": g_expr, "in3": b_expr},
        )

    if node_type == 'NORMAL':
        if getattr(from_socket, "name", "") == 'Dot':
            # Only the Normal output, the widget's constant direction, is
            # exported; reading it for the Dot output exported a vector.
            return {
                "kind": "unresolved",
                "provenance": list(provenance or ()),
                "reason": "the Normal node's Dot output has no MaterialX export; bake the material",
            }
        output = from_node.outputs.get('Normal') if hasattr(from_node, "outputs") else None
        value = None
        if output:
            try:
                value = list(output.default_value)[:3]
            except Exception:
                value = None
        if value is not None:
            return _typed_result({"kind": "constant", "value": value, "value_type": "vector3"}, expected_type, channel)
        return _make_node_expr(_nodedef_for("normal", "vector3"), {"space": "world"})

    if node_type == 'VERTEX_COLOR':
        # Blender 5.2's Color Attribute node (bl_idname ShaderNodeVertexColor,
        # type VERTEX_COLOR). Its USD exporter writes each mesh color
        # attribute as ``primvars:<attribute name>``, and that primvar is
        # color4f[] for every attribute type and domain (see
        # _COLOR_ATTRIBUTE_PRIMVAR_TYPES for the measured table). The read
        # declares the primvar's own type; a narrower consumer is reached by
        # an explicit extraction node, never by narrowing the read.
        layer_name = (getattr(from_node, "layer_name", "") or "").strip()
        output_name = (getattr(from_socket, "name", "") or "").lower()
        if not layer_name:
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    "Color Attribute names no color attribute; the exported "
                    "primvar cannot be referenced implicitly - pick the "
                    "attribute explicitly on the node"
                ),
            }
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', layer_name):
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    f"Color attribute name '{layer_name}' is not a valid USD "
                    "primvar identifier (letters, digits, underscore; not "
                    "starting with a digit); rename the attribute"
                ),
            }
        primvar_type = _color_attribute_primvar_type(from_node, layer_name)
        if primvar_type is None:
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    f"Color attribute '{layer_name}' was not found on a mesh "
                    "using this material, so the exported USD would carry no "
                    f"primvars:{layer_name} for the geompropvalue read; add "
                    "the attribute or fix the node's name"
                ),
            }
        # MaterialX has a dedicated vertex-colour reader, and it is what
        # RealityKit uses: across 281 shipping RCP-authored materials,
        # ND_geomcolor_* appears while ND_geompropvalue_* appears not once.
        # A geompropvalue read of a colour attribute makes Reality Composer
        # Pro substitute its striped placeholder for the whole material.
        # geomcolor takes an integer set index rather than a primvar name, so
        # only the first colour attribute is addressable; a named attribute
        # beyond the first cannot be expressed and is refused above by the
        # index lookup.
        color_set_index = _color_attribute_set_index(from_node, layer_name)
        if color_set_index is None:
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    f"Color attribute '{layer_name}' is not the mesh's first "
                    "colour attribute. RealityKit addresses vertex colours by "
                    "set index, so only the first is exportable; reorder the "
                    "attributes or bake the material"
                ),
            }
        read = _make_node_expr(
            _nodedef_for("geomcolor", primvar_type),
            {"index": _constant_expr(color_set_index)},
        )

        if output_name == 'alpha':
            if primvar_type != "color4":
                # Only reachable if the measured table ever gains a
                # three-channel case; alpha is not in the data then, so say so
                # rather than reading a channel that does not exist.
                return {
                    "kind": "unresolved",
                    "provenance": list(provenance),
                    "reason": (
                        f"Color attribute '{layer_name}' exports as a "
                        f"{primvar_type} primvar, which carries no alpha "
                        "channel; use the Color output or bake the material"
                    ),
                }
            alpha = _swizzle_expr(read, "color4", "float", "a")
            if expected_type and expected_type not in {"float", "half", "integer"}:
                # A scalar into a colour/vector socket: Blender broadcasts it.
                return _make_node_expr(
                    _nodedef_for("convert", expected_type, input_type="float"),
                    {"in": alpha},
                )
            return alpha

        if channel in {"r", "g", "b"}:
            if primvar_type == "color3":
                return _swizzle_color3_to_float_expr(read, channel)
            return _swizzle_expr(read, primvar_type, "float", channel)

        if expected_type == primvar_type:
            return read

        rgb = (
            read
            if primvar_type == "color3"
            else _swizzle_expr(read, primvar_type, "color3", "rgb")
        )
        if expected_type in {"float", "half", "integer"}:
            # Blender's implicit colour-to-float conversion is linear RGB to
            # gray; a Separate node asking for one channel reads it itself.
            return _coerce_output(rgb, "float", channel)
        return rgb

    if node_type == 'TEX_NOISE':
        return _typed_result(
            _noise_texture_expr(from_node, from_socket, visited, provenance, cache), expected_type, channel
        )

    if node_type == 'TEX_VORONOI':
        return _typed_result(
            _voronoi_texture_expr(from_node, from_socket, visited, provenance, cache), expected_type, channel
        )

    if node_type == 'TEX_GRADIENT':
        # Fac and Color carry the same value; Color repeats it in r, g and b.
        return _typed_result(_gradient_texture_expr(from_node, visited, provenance, cache), expected_type, channel)

    if node_type == 'TEX_ENVIRONMENT':
        expr = _environment_texture_expr(
            from_node, from_socket, visited, provenance, cache, expected_type, channel
        )
        if expr is not None and cache is not None and cache_key is not None:
            cache[cache_key] = dict(expr)
        return expr

    if node_type == 'TEX_IMAGE':
        projection = str(getattr(from_node, "projection", "FLAT") or "FLAT").upper()
        if projection == 'BOX':
            return _box_projection_expr(
                from_node, from_socket, visited, provenance, cache, expected_type, channel
            )
        if projection != 'FLAT':
            # SPHERE and TUBE have no MaterialX projection nodedef in the
            # manifest, and the FLAT path would silently sample by UV instead.
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": (
                    f"Image Texture projection {projection} has no MaterialX "
                    "equivalent (only Flat and Box are exported); bake the "
                    "material"
                ),
            }
        if _computed_image_reads or not image_uses_uv_transform(from_node) or image_premultiplies_color(from_node):
            expr = _computed_image_expr(
                from_node, from_socket, visited, provenance, cache, expected_type, channel
            )
            if expr is not None and cache is not None and cache_key is not None:
                cache[cache_key] = dict(expr)
            return expr
        texture_info = _texture_info_from_image_node(from_node)
        if not texture_info:
            return None
        if texture_info.get("kind") == "unresolved":
            return dict(texture_info, provenance=list(provenance))
        image_channel = _image_channel_from_output_socket(from_socket)
        if image_channel == "a" and texture_info.get("source_has_alpha") is False:
            # Cycles reads 1 from the Alpha of a file with no alpha channel,
            # as the computed read above does.
            return _constant_expr((1.0, 1.0, 1.0) if expected_type in ("color3", "vector3") else 1.0)
        if image_channel:
            texture_info["channel"] = image_channel
            if image_channel == "a":
                texture_info["output_type"] = "float"
        if expected_type:
            texture_info.setdefault("output_type", expected_type)
        if channel:
            texture_info.setdefault("channel", channel)
        if cache is not None and cache_key is not None:
            cache[cache_key] = dict(texture_info)
        return texture_info

    if node_type in {'INPUT_BOOL', 'INPUT_INT', 'INPUT_VECTOR'}:
        value = _input_constant_node_value(from_node)
        if value is None:
            return None
        result = {"kind": "constant", "value": value}
        if isinstance(value, (list, tuple)):
            result["value_type"] = "vector3"
        result = _typed_result(result, expected_type, channel)
        if cache is not None and cache_key is not None:
            cache[cache_key] = dict(result)
        return result

    if node_type == 'RGB':
        output = from_node.outputs.get('Color') if hasattr(from_node, "outputs") else None
        value = None
        if output:
            try:
                value = list(output.default_value)[:3]
            except Exception:
                value = None
        if value is None:
            try:
                value = list(from_node.outputs[0].default_value)[:3]
            except Exception:
                value = None
        if value is None:
            return None
        # A scalar consumer takes Blender's colour-to-float conversion; a
        # Separate node asking for a channel reads the whole colour.
        result = _typed_result({"kind": "constant", "value": value, "value_type": "color3"}, expected_type, channel)
        if cache is not None and cache_key is not None:
            cache[cache_key] = dict(result)
        return result

    if node_type == 'VALUE':
        output = from_node.outputs.get('Value') if hasattr(from_node, "outputs") else None
        driven = _driven_socket_expr(output) if output is not None else None
        if driven is not None:
            if cache is not None and cache_key is not None:
                cache[cache_key] = dict(driven)
            return driven
        value = None
        if output:
            try:
                value = float(output.default_value)
            except Exception:
                value = None
        if value is None and hasattr(from_node, "outputs") and from_node.outputs:
            try:
                value = float(from_node.outputs[0].default_value)
            except Exception:
                value = None
        if value is None:
            return None
        result = {"kind": "constant", "value": value}
        if cache is not None and cache_key is not None:
            cache[cache_key] = dict(result)
        return result

    if node_type in ('VECT_MATH', 'VECTOR_ROTATE'):
        if node_type == 'VECT_MATH':
            expr = _vector_math_expr(from_node, from_socket, visited, channel, provenance, cache)
        else:
            expr = _vector_rotate_expr(from_node, visited, provenance, cache)
        if expr is not None:
            # The mean for a scalar consumer, a convert for a colour one; a
            # Separate node reads its component from the whole vector.
            expr = _typed_result(expr, expected_type, channel)
            if cache is not None and cache_key is not None:
                cache[cache_key] = dict(expr)
            return expr

    if node_type == 'COMBXYZ':
        parts = []
        for name in ('X', 'Y', 'Z'):
            socket = from_node.inputs.get(name) if hasattr(from_node, "inputs") else None
            parts.append(_float_socket_expr(socket, visited, provenance, cache, default=0.0))
        if all(part is not None for part in parts):
            expr = _make_node_expr(
                _nodedef_for("combine3", "vector3"),
                {"in1": parts[0], "in2": parts[1], "in3": parts[2]},
            )
            expr = _typed_result(expr, expected_type, channel)
            if cache is not None and cache_key is not None:
                cache[cache_key] = dict(expr)
            return expr

    if node_type in _READER_NODE_TYPES:
        expr = _reader_node_expr(from_node, from_socket, visited, channel, provenance, cache)
        if expr is not None:
            if cache is not None and cache_key is not None:
                cache[cache_key] = dict(expr)
            return expr
        # An output with no reader falls through to the unresolved tail below,
        # so the refusal names the socket rather than approximating it.

    # Unsupported node type: return unresolved with provenance chain.
    if cache is not None and cache_key is not None:
        cache[cache_key] = {"kind": "unresolved", "provenance": list(provenance)}
    return {"kind": "unresolved", "provenance": list(provenance)}

    return None


#: Blender Image Texture extension -> MaterialX image uaddressmode/vaddressmode.
#: REPEAT is MaterialX's declared default (periodic) and is omitted.
_EXTENSION_TO_ADDRESSMODE = {
    "EXTEND": "clamp",
    "CLIP": "constant",
    "MIRROR": "mirror",
}

#: Blender Image Texture interpolation -> MaterialX image filtertype.
#: Linear is the declared default and is omitted; Blender's Smart (Cubic when
#: magnifying, otherwise Linear) has no MaterialX equivalent — cubic is the
#: closest match for its visible magnified behaviour.
_INTERPOLATION_TO_FILTERTYPE = {
    "CLOSEST": "closest",
    "CUBIC": "cubic",
    "SMART": "cubic",
}


def _image_node_sampling(image_node) -> Optional[Dict[str, str]]:
    """MaterialX sampling inputs for a Blender image node's non-default modes.

    The shipped RCP 3 ``ND_image_*`` nodedefs declare ``uaddressmode``/
    ``vaddressmode`` (constant, clamp, periodic, mirror; default periodic) and
    ``filtertype`` (closest, linear, cubic; default linear), and the runtime
    wires them into its Metal samplers. Not authoring them silently turned
    Blender's Extend/Clip/Mirror and Closest/Cubic settings into
    repeat + linear.
    """
    sampling: Dict[str, str] = {}
    extension = str(getattr(image_node, "extension", "") or "").upper()
    addressmode = _EXTENSION_TO_ADDRESSMODE.get(extension)
    if addressmode:
        sampling["uaddressmode"] = addressmode
        sampling["vaddressmode"] = addressmode
    interpolation = str(getattr(image_node, "interpolation", "") or "").upper()
    filtertype = _INTERPOLATION_TO_FILTERTYPE.get(interpolation)
    if filtertype:
        sampling["filtertype"] = filtertype
    return sampling or None


def _upstream_through_reroutes(socket):
    """The (node, output socket) feeding a linked input, past any reroutes."""
    while socket is not None and getattr(socket, "is_linked", False):
        links = list(getattr(socket, "links", []) or [])
        if not links:
            return None, None
        node = getattr(links[0], "from_node", None)
        if getattr(node, "type", "") != 'REROUTE':
            return node, getattr(links[0], "from_socket", None)
        inputs = getattr(node, "inputs", None)
        socket = inputs[0] if inputs else None
    return None, None


def _texture_info_from_image_node(image_node) -> Optional[Dict[str, Any]]:
    """Build a texture spec from a Blender image node."""
    image = getattr(image_node, "image", None)
    texture_path = _resolve_image_path(image)
    if not texture_path:
        return None

    uv_map = getattr(image_node, "uv_map", "") or ""
    vector_socket = image_node.inputs.get("Vector") if hasattr(image_node, "inputs") else None
    if not uv_map and vector_socket and vector_socket.is_linked:
        uv_map = _extract_uv_map_from_node(vector_socket.links[0].from_node)
    if uv_map:
        # From here on the name is the texture-coordinate set it reads.
        uv_map, refusal = uv_set_resolution(image_node, uv_map)
        if refusal is not None:
            return {"kind": "unresolved", "provenance": [_node_label(image_node)], "reason": refusal}

    mapping = None
    if vector_socket and vector_socket.is_linked:
        mapping = _extract_mapping_from_node(vector_socket.links[0].from_node)

    colorspace = None
    try:
        colorspace = _normalize_colorspace(image.colorspace_settings.name) if image else None
    except Exception:
        colorspace = None

    alpha_mode = None
    try:
        mode = getattr(image, "alpha_mode", None)
        if mode:
            mode = str(mode).upper()
            if mode == 'PREMUL':
                alpha_mode = 'premul'
            elif mode == 'STRAIGHT':
                alpha_mode = 'straight'
            else:
                alpha_mode = mode.lower()
    except Exception:
        alpha_mode = None

    info = {
        "kind": "texture",
        "path": texture_path,
        "uv_map": uv_map or None,
        "mapping": mapping,
        "colorspace": colorspace,
        "alpha_mode": alpha_mode,
        "sampling": _image_node_sampling(image_node),
    }
    info.update(_image_source_alpha(image, texture_path))
    return info


def _expression_texture_sources(expr: Any) -> List[Dict[str, Any]]:
    """Return every texture leaf in a supported MaterialX expression."""
    if not isinstance(expr, dict):
        return []
    sources: List[Dict[str, Any]] = []
    if expr.get("kind") == "texture" and expr.get("path"):
        sources.append(
            {
                "path": str(expr["path"]),
                "alpha_mode": expr.get("alpha_mode"),
            }
        )
    for child in (expr.get("inputs") or {}).values():
        sources.extend(_expression_texture_sources(child))
    return sources


def _texture_specs_from_value(value: Any) -> List[Dict[str, Any]]:
    """Return texture specs from either expression or RK graph payloads."""
    if not isinstance(value, dict):
        return []
    sources: List[Dict[str, Any]] = []
    is_texture = value.get("kind") == "texture" or value.get("type") in {
        "texture",
        "normal_texture",
    }
    if is_texture and value.get("path"):
        sources.append(
            {
                "path": str(value["path"]),
                "alpha_mode": value.get("alpha_mode"),
                "output_type": value.get("output_type"),
            }
        )
    for child in (value.get("inputs") or {}).values():
        sources.extend(_texture_specs_from_value(child))
    return sources


def _is_surface_base_color_input(input_name: str) -> bool:
    normalized = str(input_name or "").replace("_", "").replace(" ", "").lower()
    return normalized in {"basecolor", "color"}


def _rk_graph_base_color_texture_sources(graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect color texture leaves feeding an RK surface's base-color input."""
    nodes_by_name = {
        node.get("name"): node
        for node in graph.get("nodes", [])
        if node.get("name")
    }
    output_name = graph.get("output")
    surface = nodes_by_name.get(output_name)
    if surface is None:
        return []

    sources: List[Dict[str, Any]] = []
    for input_name, value in (surface.get("inputs") or {}).items():
        if _is_surface_base_color_input(input_name):
            sources.extend(_texture_specs_from_value(value))

    incoming = {}
    for connection in graph.get("connections", []):
        incoming.setdefault(connection.get("to_node"), []).append(connection)
    stack = [
        connection.get("from_node")
        for connection in incoming.get(output_name, [])
        if _is_surface_base_color_input(connection.get("to_input"))
    ]
    visited = set()
    while stack:
        node_name = stack.pop()
        if not node_name or node_name in visited:
            continue
        visited.add(node_name)
        node = nodes_by_name.get(node_name)
        if node is None:
            continue
        for value in (node.get("inputs") or {}).values():
            for source in _texture_specs_from_value(value):
                if str(source.get("output_type") or "").lower() in {
                    "color3",
                    "color4",
                }:
                    sources.append(source)
        stack.extend(
            connection.get("from_node")
            for connection in incoming.get(node_name, [])
        )
    return sources


def _apply_base_color_texture_semantics(
    data: Dict[str, Any],
    sources: List[Dict[str, Any]],
) -> None:
    """Attach surface-wide alpha semantics derived only from base color."""
    unique_sources = []
    seen = set()
    for source in sources:
        path = str(source.get("path") or "")
        if not path:
            continue
        alpha_mode = str(source.get("alpha_mode") or "").strip().lower() or None
        key = (path, alpha_mode)
        if key in seen:
            continue
        seen.add(key)
        unique_sources.append({"path": path, "alpha_mode": alpha_mode})
    if not unique_sources:
        return

    data["base_color_texture_sources"] = unique_sources
    modes = sorted(
        {
            source["alpha_mode"]
            for source in unique_sources
            if source.get("alpha_mode")
        }
    )
    if modes:
        data["base_color_texture_alpha_modes"] = modes
    if modes == ["premul"]:
        data["has_premultiplied_alpha"] = True
    else:
        data.pop("has_premultiplied_alpha", None)
    if "premul" in modes and any(mode != "premul" for mode in modes):
        data["base_color_alpha_semantics_error"] = (
            "Base Color combines textures with incompatible alpha conventions: "
            + ", ".join(modes)
        )


def _record_base_color_texture_semantics(data: Dict[str, Any], expr: Any) -> None:
    _apply_base_color_texture_semantics(data, _expression_texture_sources(expr))
def _channel_from_socket_name(name: str) -> Optional[str]:
    """Normalize a socket name to a channel token."""
    name = (name or "").lower()
    if name in {"r", "red"}:
        return "r"
    if name in {"g", "green"}:
        return "g"
    if name in {"b", "blue"}:
        return "b"
    if name in {"a", "alpha"}:
        return "a"
    if name == "x":
        return "x"
    if name == "y":
        return "y"
    if name == "z":
        return "z"
    return None


def _image_channel_from_output_socket(socket) -> Optional[str]:
    """Map an image texture output socket to a logical channel token."""
    if socket is None:
        return None
    name = (getattr(socket, "name", None) or "").lower()
    if name == "alpha":
        return "a"
    return None


def _is_identity_math(operation: str, value: Optional[float], linked_index: int) -> bool:
    """Return True when a math node is effectively a pass-through."""
    if value is None:
        return False
    if operation == "ADD" and value == 0.0:
        return True
    if operation == "SUBTRACT" and linked_index == 0 and value == 0.0:
        return True
    if operation == "MULTIPLY" and value == 1.0:
        return True
    if operation == "DIVIDE" and linked_index == 0 and value == 1.0:
        return True
    return False


#: Blender Math operation -> MaterialX node name, single-input float ops.
#: These are the ops whose Cycles function is the bare GPU function on every
#: input. The ops Cycles guards (safe_sqrtf, safe_asinf, safe_acosf) or
#: defines differently (ROUND is floor(x + 0.5), RealityKit's round goes half
#: away from zero) are composed in ``_math_node_expr`` instead.
_MATH_SINGLE_INPUT_OPS = {
    'ABSOLUTE': 'absval',
    'EXPONENT': 'exp',
    'FLOOR': 'floor',
    'CEIL': 'ceil',
    'SINE': 'sin',
    'COSINE': 'cos',
    'TANGENT': 'tan',
}

#: Blender Math operation -> MaterialX node name, two-input float ops that
#: need no guard. DIVIDE, POWER and FLOORED_MODULO return 0 in Cycles where
#: RealityKit's bare fdiv and pow return inf or NaN, so they are composed.
_MATH_TWO_INPUT_OPS = {
    'ADD': 'add',
    'SUBTRACT': 'subtract',
    'MULTIPLY': 'multiply',
    'MINIMUM': 'min',
    'MAXIMUM': 'max',
}

#: Two-socket ops whose nodedef names its inputs something other than in1/in2.
#: ARCTAN2 is composed (Cycles' compatible_atan2 returns 0 at the origin), so
#: nothing rides this table today; it stays so the capability-parity test
#: keeps seeing every table.
_MATH_NAMED_INPUT_OPS: Dict[str, str] = {}


def _atan2_expr(y_term, x_term):
    """Author ``atan2`` with the input names RealityKit's nodedef declares.

    ``iny``/``inx``, not ``in1``/``in2``. Both MaterialX libraries RealityKit
    loads agree on this; the manifest carried the older spelling, and an input
    the bound nodedef does not declare makes the compiler discard the whole
    material's shader graph and substitute default PBR, reporting nothing.
    Measured with ``realitytool compile``: the wrong spelling is byte-for-byte
    indistinguishable from an invented input name.
    """
    return _make_node_expr(
        _nodedef_for("atan2", "float"), {"iny": y_term, "inx": x_term}
    )

#: Ops authored as exact compositions of implemented nodedefs, each transcribed
#: from Cycles' ``svm_math`` (intern/cycles/kernel/svm/math_util.h) and the
#: guards in intern/cycles/util/math_base.h. RealityKit computes with bare
#: fdiv, sqrt, pow, log, asin and atan2, which return inf or NaN where Cycles
#: returns 0, so every guard is an explicit select (see ``_safe_*_float``):
#:
#: - DIVIDE is safe_divide: 0 where the divisor is 0.
#: - POWER is safe_powf: 1 for a zero exponent, 0 for a zero base, 0 for a
#:   negative base with a non-integer exponent, and the signed power of the
#:   magnitude for a negative base with an integer exponent.
#: - LOGARITHM is safe_logf: 0 unless value and base are both positive, and 0
#:   where ln(base) is 0.
#: - SQRT is safe_sqrtf, sqrt(max(x, 0)); ARCSINE and ARCCOSINE clamp to [-1, 1].
#: - ARCTAN2 is compatible_atan2: 0 when both arguments are 0.
#: - FLOORED_MODULO is safe_floored_modulo, a - floor(a / b) * b, 0 for b = 0.
#: - ROUND is floor(x + 0.5); RealityKit's round goes half away from zero.
#: - FRACT is ``x - floor(x)``: ``ND_fract_float`` resolves but has no
#:   implementation in RealityKit's 1.39 library.
#: - MULTIPLY_ADD is add(multiply(a, b), c); ARCTANGENT is atan2(x, 1).
_MATH_COMPOSED_OPS = frozenset({
    'MULTIPLY_ADD', 'LOGARITHM', 'ARCTANGENT', 'FRACT', 'DIVIDE', 'POWER', 'SQRT',
    'ROUND', 'ARCSINE', 'ARCCOSINE', 'ARCTAN2', 'FLOORED_MODULO',
})


#: Blender's implicit colour-to-float socket conversion, Cycles'
#: ``linear_rgb_to_gray``: the Y row of the scene-linear RGB to XYZ matrix
#: Cycles derives from the OpenColorIO config's ``aces_interchange`` role
#: (ShaderManager::init_xyz_transforms). Measured on Blender 5.2.0 LTS with a
#: Linear Rec.709 working space by rendering RGB to BW of pure red, green and
#: blue as emission in Cycles: 0.21263909, 0.71516913, 0.07219274. These are
#: not the config's rounded luma (0.2126, 0.7152, 0.0722), and they are not
#: RealityKit's ND_luminance default, which is ACEScg's. They hold for the
#: Linear Rec.709 working space only.
_RGB_TO_GRAY = (0.21263909, 0.71516913, 0.07219274)

#: Blender's implicit vector-to-float socket conversion is the mean.
_VECTOR_MEAN = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)


#: Channel order of each MaterialX type a component read can land on.
_CHANNEL_ORDER = {
    "color3": "rgb", "vector3": "xyz",
    "color4": "rgba", "vector4": "xyzw",
}

#: The vector type each colour type converts to, for the dot-product trick.
_VECTOR_EQUIVALENT = {
    "color3": "vector3", "color4": "vector4",
    "vector3": "vector3", "vector4": "vector4",
}


def _component_expr(expr: Dict[str, Any], from_type: str, channel: str) -> Dict[str, Any]:
    """Read one channel of a colour/vector expression as a float.

    The read is a convert to the matching vector type followed by a dot
    product with a unit mask: exact arithmetic from two implemented nodes,
    and the one component-read form every kit scene has been imported with.
    """
    order = _CHANNEL_ORDER.get(from_type)
    vector_type = _VECTOR_EQUIVALENT.get(from_type)
    if not order or not vector_type:
        raise ValueError(
            f"Cannot read a component of {from_type!r}; bake the material."
        )
    letter = (channel or order[0])[:1].lower()
    index = order.find(letter)
    if index < 0:
        # r/g/b/a and x/y/z/w name the same positions.
        index = max("rgba".find(letter), "xyzw".find(letter))
        if index >= len(order):
            index = -1
    if index < 0:
        raise ValueError(
            f"{from_type!r} has no channel {channel!r}; bake the material."
        )

    value = expr
    if from_type != vector_type:
        value = _make_node_expr(
            _nodedef_for("convert", vector_type, input_type=from_type),
            {"in": value},
        )
    mask = tuple(1.0 if i == index else 0.0 for i in range(len(order)))
    return _make_node_expr(
        _nodedef_for("dotproduct", "float", input_type=vector_type),
        {"in1": value, "in2": _constant_expr(mask)},
    )


def _swizzle_color3_to_float_expr(expr: Dict[str, Any], channels: str) -> Dict[str, Any]:
    """Read one channel of a color3 expression as a float."""
    return _component_expr(expr, "color3", channels)


#: MaterialX type of the primvar Blender's USD exporter writes for each
#: Blender mesh color attribute type. Measured on Blender 5.2.0 LTS by
#: exporting one cube per case and reading the primvar back with pxr:
#:
#:   FLOAT_COLOR / CORNER -> color4f[]  interpolation=faceVarying
#:   FLOAT_COLOR / POINT  -> color4f[]  interpolation=vertex
#:   BYTE_COLOR  / CORNER -> color4f[]  interpolation=faceVarying
#:   BYTE_COLOR  / POINT  -> color4f[]  interpolation=vertex
#:
#: The domain only picks the interpolation; the value type is four-channel in
#: every case, for both the float and the byte storage. A geompropvalue read
#: must declare that same type - Reality Composer Pro 3
#: replaces a material whose read declares color3 over a color4f primvar with
#: its striped placeholder rather than erroring visibly.
_COLOR_ATTRIBUTE_PRIMVAR_TYPES = {
    "FLOAT_COLOR": "color4",
    "BYTE_COLOR": "color4",
}

#: What to read a color attribute with when its Blender type cannot be
#: inspected (a node group tree, or a pure-Python unit double outside a live
#: session). Matches what Blender actually writes; narrowing to color3 on a
#: guess is the defect this table exists to prevent.
_COLOR_ATTRIBUTE_DEFAULT_PRIMVAR_TYPE = "color4"


def _swizzle_expr(
    expr: Dict[str, Any], from_type: str, to_type: str, channels: str
) -> Dict[str, Any]:
    """Convert between two MaterialX types with ``convert`` and ``dotproduct``.

    A component read is a dot product with a unit mask, the one form every
    export authors, so the arithmetic can be read back from the mask.
    """
    if from_type == to_type:
        return expr
    if to_type in {"float", "half"}:
        return _component_expr(expr, from_type, channels)
    # Widening or narrowing a whole colour: `convert` is implemented in
    # RealityKit's Metal library, `swizzle` is not. See _component_expr.
    return _make_node_expr(
        _nodedef_for("convert", to_type, input_type=from_type), {"in": expr}
    )


def _color_attribute_primvar_type(node, layer_name: str) -> Optional[str]:
    """MaterialX type of the primvar Blender writes for ``layer_name``.

    Blender 5.2's USD exporter writes each mesh color attribute as
    ``primvars:<attribute name>`` (verified against a real export). A
    geompropvalue read must name an attribute that exists on a mesh using the
    material, or the authored read dangles at runtime - and it must declare
    the type that primvar actually carries, or Reality Composer Pro cannot
    instantiate the material at all.

    Returns ``None`` when no mesh using this node's material carries the
    attribute, which is the fail-closed signal callers refuse on. Fails
    closed when no owning material/mesh can be found (e.g. the node lives in
    a node group tree). Outside a live Blender session (pure-Python unit
    doubles) the graph is trusted and reads with the measured default; the
    real export path always runs inside Blender.
    """
    try:
        import bpy
        meshes = bpy.data.meshes
        materials = bpy.data.materials
    except Exception:
        return _COLOR_ATTRIBUTE_DEFAULT_PRIMVAR_TYPE
    node_tree = getattr(node, "id_data", None)
    owners = [
        material
        for material in materials
        if getattr(material, "node_tree", None) == node_tree
    ]
    if not owners:
        return None
    for mesh in meshes:
        mesh_materials = list(getattr(mesh, "materials", []) or [])
        if not any(
            any(candidate == owner for owner in owners)
            for candidate in mesh_materials
            if candidate is not None
        ):
            continue
        attributes = getattr(mesh, "color_attributes", None)
        attribute = (
            attributes.get(layer_name) if attributes is not None else None
        )
        if attribute is not None:
            data_type = (getattr(attribute, "data_type", "") or "").upper()
            return _COLOR_ATTRIBUTE_PRIMVAR_TYPES.get(
                data_type, _COLOR_ATTRIBUTE_DEFAULT_PRIMVAR_TYPE
            )
    return None


def _color_attribute_set_index(node, layer_name: str) -> Optional[int]:
    """Index of ``layer_name`` among the mesh's colour attributes.

    RealityKit's vertex-colour reader (``ND_geomcolor_*``) selects a colour
    set by integer index, not by name, so only an attribute the mesh lists
    first is addressable. Returns ``None`` when the attribute is not first,
    which callers refuse on rather than silently exporting a different set.

    Outside a live Blender session the graph is trusted and index 0 is
    assumed; the real export path always runs inside Blender.
    """
    try:
        import bpy
        meshes = bpy.data.meshes
        materials = bpy.data.materials
    except Exception:
        return 0
    node_tree = getattr(node, "id_data", None)
    owners = [
        material
        for material in materials
        if getattr(material, "node_tree", None) == node_tree
    ]
    if not owners:
        return None
    for mesh in meshes:
        mesh_materials = list(getattr(mesh, "materials", []) or [])
        if not any(
            any(candidate == owner for owner in owners)
            for candidate in mesh_materials
            if candidate is not None
        ):
            continue
        collection = getattr(mesh, "color_attributes", None)
        if collection is None:
            continue
        try:
            attributes = list(collection)
        except TypeError:
            # Name-keyed doubles (and some bpy collection shims) are not
            # iterable; presence is all they can answer, and a single
            # attribute is necessarily the first one.
            found = collection.get(layer_name)
            if found is not None:
                return 0
            continue
        for index, attribute in enumerate(attributes):
            if getattr(attribute, "name", None) == layer_name:
                return index if index == 0 else None
    return None


def _color_attribute_reaches_export(node, layer_name: str) -> bool:
    """Whether a mesh using this node's material carries ``layer_name``."""
    return _color_attribute_primvar_type(node, layer_name) is not None


def _float_math_input_expr(expr):
    """Coerce a resolved expression to a float, as a Blender scalar socket does.

    Blender converts a colour feeding a scalar socket with linear RGB to gray
    (``_RGB_TO_GRAY``) and a vector with the mean (``_VECTOR_MEAN``). A
    channel-bearing or float texture already reads a scalar; a channelless
    colour texture is converted with the same dot product. Constants fold with
    the weights their ``value_type`` names; an untyped three-component
    constant is a colour, since only colour nodes produce them.
    """
    if not isinstance(expr, dict):
        return None
    kind = expr.get("kind")
    if kind == "constant":
        value = expr.get("value")
        if isinstance(value, (list, tuple)):
            try:
                components = [float(component) for component in value]
            except (TypeError, ValueError):
                return None
            if expr.get("value_type") == "vector3" and components:
                # Cycles' ``average``, over however many components the
                # vector socket carries.
                return _constant_expr(sum(components) / len(components))
            if len(components) < 3:
                return None
            return _constant_expr(sum(c * w for c, w in zip(components, _RGB_TO_GRAY)))
        return expr
    if kind == "texture" and (expr.get("channel") or "rgb") in ("rgb", "rgba"):
        texture = dict(expr)
        texture["output_type"] = "color3"
        texture.pop("channel", None)
        as_vector = _make_node_expr(
            _nodedef_for("convert", "vector3", input_type="color3"), {"in": texture}
        )
        return _vector_dot(as_vector, _constant_expr(_RGB_TO_GRAY))
    if kind == "node":
        return _coerce_output(expr, "float")
    return expr


def _math_socket_expr(node, index, visited, channel, provenance, cache):
    """Resolve a Math node input socket (linked or constant) as a float expr.

    ``channel`` is ignored: a scalar socket reads its source whole, whatever a
    downstream Separate node asked of the Math node's own output.
    """
    inputs = getattr(node, "inputs", None)
    if inputs is None or len(inputs) <= index:
        return None
    socket = inputs[index]
    if socket is None:
        return None
    if getattr(socket, "is_linked", False):
        # Sibling branches each get their own ancestry copy (diamond graphs).
        resolved = _resolve_socket_value(
            socket,
            set(visited or ()),
            None,
            list(provenance or ()),
            cache,
            expected_type="float",
        )
        return _float_math_input_expr(resolved)
    driven = _driven_socket_expr(socket)
    if driven is not None:
        return driven
    value = _socket_default_value(socket)
    if value is None:
        return None
    try:
        return _constant_expr(float(value))
    except (TypeError, ValueError):
        return None


def _constant_scalar(expr) -> Optional[float]:
    """The float an expression folds to, or None when it is not a scalar constant."""
    if not isinstance(expr, dict) or expr.get("kind") != "constant":
        return None
    value = expr.get("value")
    if isinstance(value, (list, tuple, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fnode(name: str, **inputs: Any) -> Dict[str, Any]:
    return _make_node_expr(_nodedef_for(name, "float"), inputs)


def _select(kind: str, value1, value2, in1, in2, value_type: str = "float") -> Dict[str, Any]:
    """``ifgreater`` (strictly greater), ``ifgreatereq`` or ``ifequal``: in1 when
    the test holds, in2 otherwise. RealityKit selects, so a NaN or inf in the
    branch not taken never reaches the result."""
    def as_expr(value):
        return value if isinstance(value, dict) else _constant_expr(value)
    input_type = None if value_type == "float" else value_type
    return _make_node_expr(
        _nodedef_for(kind, value_type, input_type=input_type),
        {"value1": as_expr(value1), "value2": as_expr(value2), "in1": as_expr(in1), "in2": as_expr(in2)},
    )


def _safe_divide_float(a, b) -> Dict[str, Any]:
    """Cycles' ``safe_divide``: ``b != 0 ? a / b : 0``."""
    divisor = _constant_scalar(b)
    if divisor is not None:
        return _constant_expr(0.0) if divisor == 0.0 else _fnode("divide", in1=a, in2=b)
    return _select("ifequal", b, 0.0, 0.0, _fnode("divide", in1=a, in2=b))


def _safe_sqrt_float(a) -> Dict[str, Any]:
    """Cycles' ``safe_sqrtf``: ``sqrt(max(x, 0))``."""
    return _fnode("sqrt", **{"in": _fnode("max", in1=a, in2=_constant_expr(0.0))})


def _clamp_float(value, low: float, high: float) -> Dict[str, Any]:
    return _fnode("clamp", **{"in": value, "low": _constant_expr(low), "high": _constant_expr(high)})


def _safe_power_float(a, b) -> Dict[str, Any]:
    """Cycles' ``safe_powf`` and ``compatible_powf``, exactly.

    ``b == 0`` is 1 (0^0 included); ``a == 0`` is 0 (a negative exponent
    included); a negative base with a non-integer exponent is 0; a negative
    base with an integer exponent is ``pow(-a, b)``, negated for an odd
    exponent; anything else is ``pow(a, b)``. RealityKit's ``pow`` is only
    trusted on a non-negative base, so the negative cases take the power of
    the magnitude and restore the sign.
    """
    exponent = _constant_scalar(b)
    if exponent is not None:
        if exponent == 0.0:
            return _constant_expr(1.0)
        if exponent != math.floor(exponent):
            # A negative base is 0, and so is a zero base; pow(0, b > 0) is 0
            # already, but a negative exponent would divide by zero.
            return _select("ifgreater", a, 0.0, _fnode("power", in1=a, in2=b), 0.0)
        magnitude = _fnode("power", in1=_fnode("absval", **{"in": a}), in2=b)
        if int(exponent) % 2:
            magnitude = _fnode("multiply", in1=magnitude, in2=_fnode("sign", **{"in": a}))
        if exponent > 0.0:
            return magnitude
        return _select("ifequal", a, 0.0, 0.0, magnitude)
    magnitude = _fnode("power", in1=_fnode("absval", **{"in": a}), in2=b)
    floor_b = _fnode("floor", **{"in": b})
    # For an integer b, b - 2 * floor(b / 2) is exactly 0 (even) or 1 (odd).
    odd = _fnode(
        "subtract",
        in1=b,
        in2=_fnode("multiply", in1=_fnode("floor", **{"in": _fnode("multiply", in1=b, in2=_constant_expr(0.5))}), in2=_constant_expr(2.0)),
    )
    sign = _fnode("subtract", in1=_constant_expr(1.0), in2=_fnode("multiply", in1=odd, in2=_constant_expr(2.0)))
    negative_base = _select("ifequal", b, floor_b, _fnode("multiply", in1=magnitude, in2=sign), 0.0)
    result = _select("ifgreater", a, 0.0, magnitude, negative_base)
    result = _select("ifequal", a, 0.0, 0.0, result)
    return _select("ifequal", b, 0.0, 1.0, result)


def _safe_log_float(a, base) -> Dict[str, Any]:
    """Cycles' ``safe_logf``: 0 unless ``a > 0`` and ``base > 0``, then
    ``safe_divide(log(a), log(base))``."""
    base_value = _constant_scalar(base)
    if base_value is not None and (base_value <= 0.0 or base_value == 1.0):
        return _constant_expr(0.0)
    ln_a = _fnode("ln", **{"in": a})
    if base_value is not None:
        quotient = ln_a if abs(base_value - math.e) < 1e-6 else _fnode(
            "divide", in1=ln_a, in2=_fnode("ln", **{"in": base})
        )
    else:
        ln_base = _fnode("ln", **{"in": base})
        quotient = _select("ifequal", ln_base, 0.0, 0.0, _fnode("divide", in1=ln_a, in2=ln_base))
        quotient = _select("ifgreater", base, 0.0, quotient, 0.0)
    return _select("ifgreater", a, 0.0, quotient, 0.0)


def _compatible_atan2_float(y, x) -> Dict[str, Any]:
    """Cycles' ``compatible_atan2``: 0 when both arguments are 0."""
    angle = _atan2_expr(y, x)
    y_value, x_value = _constant_scalar(y), _constant_scalar(x)
    if (x_value is not None and x_value != 0.0) or (y_value is not None and y_value != 0.0):
        return angle
    if x_value == 0.0 and y_value == 0.0:
        return _constant_expr(0.0)
    return _select("ifequal", x, 0.0, _select("ifequal", y, 0.0, 0.0, angle), angle)


def _safe_floored_modulo_float(a, b) -> Dict[str, Any]:
    """Cycles' ``safe_floored_modulo``: ``b != 0 ? a - floor(a / b) * b : 0``."""
    remainder = _fnode(
        "subtract",
        in1=a,
        in2=_fnode("multiply", in1=_fnode("floor", **{"in": _fnode("divide", in1=a, in2=b)}), in2=b),
    )
    divisor = _constant_scalar(b)
    if divisor is not None:
        return _constant_expr(0.0) if divisor == 0.0 else remainder
    return _select("ifequal", b, 0.0, 0.0, remainder)


def _round_half_up_float(a) -> Dict[str, Any]:
    """Cycles' round, ``floor(x + 0.5)``: -0.5 rounds to 0, where RealityKit's
    ``round`` (half away from zero) gives -1."""
    return _fnode("floor", **{"in": _fnode("add", in1=a, in2=_constant_expr(0.5))})


def _math_node_expr(node, operation, visited, channel, provenance, cache):
    """Author a Blender Math node as exact MaterialX float nodes.

    Returns None when the operation has no exact MaterialX mapping - the
    caller falls through to the unresolved tail so the bake advice fires;
    nothing is approximated silently.
    """
    def socket_expr(index):
        return _math_socket_expr(node, index, visited, channel, provenance, cache)

    arity = {'MULTIPLY_ADD': 3}.get(operation)
    if arity is None:
        arity = 1 if operation in _MATH_SINGLE_INPUT_OPS or operation in {
            'SQRT', 'ROUND', 'ARCSINE', 'ARCCOSINE', 'FRACT', 'ARCTANGENT',
        } else 2
    if operation not in _MATH_SINGLE_INPUT_OPS and operation not in _MATH_TWO_INPUT_OPS and operation not in _MATH_COMPOSED_OPS:
        return None
    operands = [socket_expr(index) for index in range(arity)]
    if any(operand is None for operand in operands):
        return None
    for operand in operands:
        if operand.get("kind") == "unresolved":
            return operand

    if operation in _MATH_SINGLE_INPUT_OPS:
        expr = _fnode(_MATH_SINGLE_INPUT_OPS[operation], **{"in": operands[0]})
    elif operation in _MATH_TWO_INPUT_OPS:
        expr = _fnode(_MATH_TWO_INPUT_OPS[operation], in1=operands[0], in2=operands[1])
    elif operation == 'DIVIDE':
        expr = _safe_divide_float(*operands)
    elif operation == 'POWER':
        expr = _safe_power_float(*operands)
    elif operation == 'LOGARITHM':
        expr = _safe_log_float(*operands)
    elif operation == 'SQRT':
        expr = _safe_sqrt_float(operands[0])
    elif operation == 'ROUND':
        expr = _round_half_up_float(operands[0])
    elif operation == 'ARCSINE':
        expr = _fnode("asin", **{"in": _clamp_float(operands[0], -1.0, 1.0)})
    elif operation == 'ARCCOSINE':
        expr = _fnode("acos", **{"in": _clamp_float(operands[0], -1.0, 1.0)})
    elif operation == 'ARCTAN2':
        # Blender's atan2(value0, value1) is (y, x), matching iny/inx.
        expr = _compatible_atan2_float(operands[0], operands[1])
    elif operation == 'FLOORED_MODULO':
        expr = _safe_floored_modulo_float(*operands)
    elif operation == 'MULTIPLY_ADD':
        expr = _fnode("add", in1=_fnode("multiply", in1=operands[0], in2=operands[1]), in2=operands[2])
    elif operation == 'FRACT':
        expr = _fnode("subtract", in1=operands[0], in2=_fnode("floor", **{"in": operands[0]}))
    elif operation == 'ARCTANGENT':
        expr = _atan2_expr(operands[0], _constant_expr(1.0))
    else:
        return None

    if not isinstance(expr, dict) or expr.get("kind") == "unresolved":
        # An unresolved child propagated through _make_node_expr; keep its
        # provenance chain instead of wrapping it further.
        return expr
    if getattr(node, "use_clamp", False):
        # MathNode::expand appends a MINMAX Clamp to [0, 1].
        expr = _clamp_float(expr, 0.0, 1.0)
    return expr


def _nodedef_for(
    node_name: str,
    output_type: Optional[str] = None,
    input_type: Optional[str] = None,
) -> str:
    """Resolve a manifest nodedef name, failing closed on an impossible ask.

    Every consumer treats a non-None result as success, so a missing nodedef
    raises instead of returning a name. The error lands in the per-material
    failure handling in rewrite.py, which names the material and fails the
    export.
    """
    manifest = _get_manifest()
    nodedef = select_nodedef_name_for_node(
        manifest,
        node_name,
        input_type=input_type,
        output_type=output_type,
    )
    if not nodedef:
        wanted = ", ".join(
            part for part in (
                f"input {input_type}" if input_type else "",
                f"output {output_type}" if output_type else "",
            ) if part
        )
        raise ValueError(
            f"No MaterialX nodedef satisfies '{node_name}'"
            + (f" ({wanted})" if wanted else "")
            + ". Bake the material, or simplify the node graph."
        )
    return nodedef


def _make_node_expr(node_id: str, inputs: Dict[str, Any], output: str = "out") -> Dict[str, Any]:
    """Build a node expression, propagating any unresolved child.

    A node whose input could not be resolved cannot itself be authored
    faithfully. Callers check only the *top-level* kind for "unresolved", so
    returning a node expression over a failed child would let the builder drop
    that child and fall back to a nodedef default with no warning.

    Surfacing the child's own unresolved expression keeps its provenance
    chain, so the warning names the node that actually failed rather than the
    one that happened to wrap it.
    """
    for value in (inputs or {}).values():
        if isinstance(value, dict) and value.get("kind") == "unresolved":
            return value
    return {
        "kind": "node",
        "node_id": node_id,
        "inputs": inputs,
        "output": output,
    }


def _constant_expr(value: Any) -> Dict[str, Any]:
    return {"kind": "constant", "value": value}


def _expr_is_constant(expr: Optional[Dict[str, Any]], value: float) -> bool:
    if not isinstance(expr, dict):
        return False
    if expr.get("kind") != "constant":
        return False
    try:
        return abs(float(expr.get("value")) - float(value)) < 1e-6
    except Exception:
        return False


def _expr_from_socket(
    socket,
    visited,
    channel,
    provenance,
    cache,
    default: Optional[Any] = None,
):
    if socket is None:
        if default is None:
            return None
        return _constant_expr(default)
    if socket.is_linked:
        # ``visited`` is the active ancestry for cycle detection, not a global
        # graph-visited set. Each sibling branch needs its own copy so a shared
        # upstream node in a valid diamond graph can be resolved twice.
        branch_visited = set(visited or ())
        branch_provenance = list(provenance or ())
        return _resolve_socket_value(
            socket,
            branch_visited,
            channel,
            branch_provenance,
            cache,
        )
    driven = _driven_socket_expr(socket)
    if driven is not None:
        return driven
    value = _socket_default_value(socket)
    if value is None:
        value = default
    if value is None:
        return None
    return _constant_expr(value)


def _generated_coordinate_expr() -> Dict[str, Any]:
    """Spatially varying stand-in for Blender's Generated coordinates.

    Blender samples a procedural texture's unwired Vector with Generated
    coordinates (object-bounding-box-normalized positions). The manifest's
    closest runtime-resolvable signal is object-space position
    (``ND_position_vector3``): it varies over the surface exactly where
    Generated does, differing only by the bounding-box normalization, which
    Blender applies per object and MaterialX cannot see. The procedural
    warning in ``collect_material_warnings`` names this approximation.
    """
    return _make_node_expr(
        _nodedef_for("position", "vector3"),
        {"space": _constant_expr("object")},
    )


# ---------------------------------------------------------------------------
# Bump: the surface gradient of a height, from Cycles' ``svm_node_set_bump``.
#
# Cycles perturbs the normal to ``fw |det| N - distance sign(det) surfgrad``,
# where ``surfgrad`` is built from the height sampled a filter width along the
# screen derivatives. By the surface-gradient identity that is
# ``fw |det| (N - distance grad_s h)``, so the bumped normal is
# ``normalize(N - distance grad_s h)``, blended toward N by Strength.
#
# RealityKit has no derivatives, so the gradient is taken along the UV
# parametrization instead: the height is evaluated again one small step along
# U and along V, and the export writes each mesh's object-space dP/du and
# dP/dv per face corner (``postprocess_usd._author_uv_derivatives``). With
# ``Pu`` and ``Pv`` in world space, ``grad_s u = (G Pu - F Pv) / (EG - F^2)``
# and ``grad_s v = (E Pv - F Pu) / (EG - F^2)`` over the metric
# ``E = Pu.Pu, F = Pu.Pv, G = Pv.Pv``, and
# ``grad_s h = h_u grad_s u + h_v grad_s v``.
# ---------------------------------------------------------------------------

UV_DERIVATIVE_PRIMVARS = ("blenderDPdu", "blenderDPdv")
#: The UV step the height is differenced over.
_BUMP_UV_STEP = 1.0e-3
#: Depth of height resolution in progress; while above zero every Image
#: Texture reads at a computed coordinate so the UV step can reach it.
_computed_image_reads = 0

#: Readers a height may depend on without being offset: they vary with the
#: surface only through the normal, which the gradient ignores to first order.
_BUMP_STEADY_READERS = (
    "ND_normal_", "ND_tangent_", "ND_bitangent_", "ND_time_", "ND_realitykit_surface_world_to_view",
    "ND_realitykit_surface_model_to_view",
    "ND_realitykit_surface_model_to_world", "ND_realitykit_surface_view_direction", "ND_realitykit_is_front_facing",
)


def _uv_derivative_expr(index: int, space: str) -> Dict[str, Any]:
    local = _make_node_expr(
        _nodedef_for("geompropvalue", "vector3"),
        {"geomprop": _constant_expr(UV_DERIVATIVE_PRIMVARS[index]), "default": _constant_expr((0.0, 0.0, 0.0))},
    )
    if space == "object":
        return local
    to_world = _make_node_expr("ND_realitykit_surface_model_to_world", {}, output="modelToWorld")
    origin = _make_node_expr(_nodedef_for("position", "vector3"), {"space": _constant_expr("object")})
    tip = _vector3_node("add", in1=origin, in2=local)
    return _vector3_node(
        "subtract",
        in1=_make_node_expr("ND_transformmatrix_vector3M4", {"in": tip, "mat": to_world}),
        in2=_make_node_expr("ND_transformmatrix_vector3M4", {"in": origin, "mat": to_world}),
    )


def _offset_height(expr: Any, du: float, dv: float) -> Any:
    """``expr`` evaluated one UV step (du, dv) along the surface, or unresolved."""
    if not isinstance(expr, dict) or expr.get("kind") != "node":
        return expr
    node_id = expr.get("node_id", "")
    if node_id.startswith("ND_texcoord_"):
        width = 2 if node_id.endswith("vector2") else 3
        step = (du, dv) if width == 2 else (du, dv, 0.0)
        return _make_node_expr(
            _nodedef_for("add", "vector2" if width == 2 else "vector3"),
            {"in1": expr, "in2": _constant_expr(step)},
        )
    if node_id == "ND_position_vector3":
        space = _constant_value((expr.get("inputs") or {}).get("space")) or "object"
        direction = _vector3_node(
            "add",
            in1=_vector3_node("multiply", in1=_uv_derivative_expr(0, space), in2=_constant_expr((du, du, du))),
            in2=_vector3_node("multiply", in1=_uv_derivative_expr(1, space), in2=_constant_expr((dv, dv, dv))),
        )
        return _vector3_node("add", in1=expr, in2=direction)
    if node_id.startswith(("ND_geompropvalue_", "ND_geomcolor_", "ND_realitykit_instance")):
        geomprop = _constant_value((expr.get("inputs") or {}).get("geomprop"))
        if geomprop in UV_DERIVATIVE_PRIMVARS:
            return expr
        return {
            "kind": "unresolved",
            "provenance": [node_id],
            "reason": (
                "a Bump height that reads a mesh attribute cannot be offset along the surface; "
                "bake the material"
            ),
        }
    inputs = {}
    for name, value in (expr.get("inputs") or {}).items():
        shifted = _offset_height(value, du, dv)
        if isinstance(shifted, dict) and shifted.get("kind") == "unresolved":
            return shifted
        inputs[name] = shifted
    if not inputs and not node_id.startswith(_BUMP_STEADY_READERS):
        return {
            "kind": "unresolved",
            "provenance": [node_id],
            "reason": f"a Bump height that reads {node_id} cannot be offset along the surface; bake the material",
        }
    return dict(expr, inputs=inputs)


def _bump_expr(node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Bump node as a normal in Blender's world axes; see the section comment.

    Measured by importing ``t36_bump`` into Reality Composer Pro 3:
    the bumped normal shown as colour tilts toward +X on each
    bump's right-facing slope, so the gradient's direction and sign hold on
    the platform. Its shading through PBR Surface 2's tangent-space normal is
    not yet judged; that viewport lit flat.
    """
    global _computed_image_reads
    normal_socket = _named_socket(node, 'Normal')
    if normal_socket is not None and getattr(normal_socket, "is_linked", False):
        upstream, _output = _upstream_through_reroutes(normal_socket)
        if getattr(upstream, "type", "") != 'BUMP':
            return {
                "kind": "unresolved",
                "provenance": list(provenance),
                "reason": "a Bump Normal fed by anything but another Bump is not exported; bake the material",
            }
        normal = _resolve_socket_value(normal_socket, set(visited or ()), None, list(provenance), cache, expected_type="vector3")
        if not normal or normal.get("kind") == "unresolved":
            return normal
    else:
        normal = blender_world_vector(_world_reader_expr("normal"))
    height_socket = _named_socket(node, 'Height')
    if height_socket is None or not getattr(height_socket, "is_linked", False):
        return normal
    strength = _vector_math_scalar_operand(node, 'Strength', visited, None, provenance, cache)
    distance = _vector_math_scalar_operand(node, 'Distance', visited, None, provenance, cache)
    for operand in (strength, distance):
        if not operand:
            return None
        if operand.get("kind") == "unresolved":
            return operand
    _computed_image_reads += 1
    try:
        height = _resolve_socket_value(height_socket, set(visited or ()), None, list(provenance), {}, expected_type="float")
    finally:
        _computed_image_reads -= 1
    height = _float_math_input_expr(height)
    if not height or height.get("kind") == "unresolved":
        return height
    step = _BUMP_UV_STEP
    h_u = _offset_height(height, step, 0.0)
    h_v = _offset_height(height, 0.0, step)
    for sample in (h_u, h_v):
        if isinstance(sample, dict) and sample.get("kind") == "unresolved":
            reason = sample.get("reason")
            return {"kind": "unresolved", "provenance": list(provenance), "reason": reason}
    slope_u = _make_node_expr(_nodedef_for("divide", "float"), {"in1": _fold_float("subtract", h_u, height), "in2": _constant_expr(step)})
    slope_v = _make_node_expr(_nodedef_for("divide", "float"), {"in1": _fold_float("subtract", h_v, height), "in2": _constant_expr(step)})
    pu = blender_world_vector(_uv_derivative_expr(0, "world"))
    pv = blender_world_vector(_uv_derivative_expr(1, "world"))
    e, f, g = _vector_dot(pu, pu), _vector_dot(pu, pv), _vector_dot(pv, pv)
    determinant = _fold_float("subtract", _fold_float("multiply", e, g), _fold_float("multiply", f, f))
    grad_u = _vector_times_float(
        _vector3_node("subtract", in1=_vector_times_float(pu, g), in2=_vector_times_float(pv, f)),
        _make_node_expr(_nodedef_for("divide", "float"), {"in1": _constant_expr(1.0), "in2": determinant}),
    )
    grad_v = _vector_times_float(
        _vector3_node("subtract", in1=_vector_times_float(pv, e), in2=_vector_times_float(pu, f)),
        _make_node_expr(_nodedef_for("divide", "float"), {"in1": _constant_expr(1.0), "in2": determinant}),
    )
    gradient = _vector3_node("add", in1=_vector_times_float(grad_u, slope_u), in2=_vector_times_float(grad_v, slope_v))
    if bool(getattr(node, "invert", False)):
        distance = _negated_float(distance)
    bumped = _safe_normalize(_vector3_node("subtract", in1=normal, in2=_vector_times_float(gradient, distance)))
    strength = _make_node_expr(_nodedef_for("max", "float"), {"in1": strength, "in2": _constant_expr(0.0)}) \
        if _constant_value(strength) is None else _constant_expr(max(float(_constant_value(strength)), 0.0))
    if _constant_value(strength) == 1.0:
        return bumped
    blended = _vector3_node(
        "add",
        in1=_vector_times_float(bumped, strength),
        in2=_vector_times_float(normal, _fold_float("subtract", _constant_expr(1.0), strength)),
    )
    return _vector3_node("normalize", **{"in": blended})


def world_normal_to_tangent_space(normal: Dict[str, Any]) -> Dict[str, Any]:
    """A world-space normal as the tangent-space vector PBR Surface 2's ``normal`` takes."""
    frame = tuple(
        blender_world_vector(reader)
        for reader in (
            _world_reader_expr("tangent", index=0),
            _world_reader_expr("bitangent", index=0),
            _world_reader_expr("normal"),
        )
    )
    parts = [_vector_dot(normal, axis) for axis in frame]
    return _make_node_expr(_nodedef_for("combine3", "vector3"), {"in1": parts[0], "in2": parts[1], "in3": parts[2]})


# ---------------------------------------------------------------------------
# Normal Map, from Cycles' ``svm_node_normal_map`` (tangent space).
#
# The colour decodes to ``c = 2 (color - 0.5)`` (green negated for DirectX).
# Strength scales it as ``(s c.x, s c.y, mix(1, c.z, saturate(s)))``; Cycles
# takes the other branch, a world-space lerp toward the shading normal, only
# for a smooth face whose mesh carries an undisplaced normal, which exists
# when the material displaces vertices and Base is Original. The result is
# ``safe_normalize(to_global(c, T, B, N))``, falling back to the shading
# normal. Measured by an Emission bake on Blender 5.2 (tests/unit/
# test_normal_map_exact.py): at Strength 2 and 0.35 the xy-scale form holds
# for smooth and flat faces and either Base, and the lerp appears only with
# displacement, smooth faces and Base Original.
# ---------------------------------------------------------------------------


def _owning_materials(node):
    """Materials whose node tree holds ``node``; None outside Blender."""
    try:
        import bpy
        materials = list(bpy.data.materials)
    except Exception:
        return None
    node_tree = getattr(node, "id_data", None)
    return [material for material in materials if getattr(material, "node_tree", None) == node_tree]


def normal_map_refusal(node) -> Optional[str]:
    """Why a Normal Map node cannot be exported exactly, or None."""
    label = f"Normal Map '{getattr(node, 'name', 'Normal Map')}'"
    space = str(getattr(node, "space", "TANGENT") or "TANGENT").upper()
    if space != 'TANGENT':
        return (
            f"{label} is in {space.replace('_', ' ').title()} space; RealityKit's normal input "
            "takes tangent-space normals only, so bake a tangent-space normal map"
        )
    uv_map = (getattr(node, "uv_map", "") or "").strip()
    if uv_map:
        texcoord, refusal = uv_set_resolution(node, uv_map)
        if refusal is not None:
            return refusal
        if texcoord != "UV0":
            return (
                f"{label} builds its tangent frame from the UV map '{uv_map}', but RealityKit "
                "builds tangents from the render UV map only; make that UV map the render one "
                "or clear the node's UV Map field"
            )
    base = str(getattr(node, "base", "ORIGINAL") or "ORIGINAL").upper()
    strength = _named_socket(node, "Strength")
    unit_strength = (
        strength is not None
        and not _socket_is_live(strength)
        and abs(float(getattr(strength, "default_value", 1.0)) - 1.0) <= 1e-6
    )
    if base == 'ORIGINAL' and not unit_strength:
        for material in _owning_materials(node) or []:
            if displacement_source(material)[1] is not None:
                return (
                    f"{label} has Base Original and a Strength other than 1 on a material that "
                    "displaces vertices; Cycles then blends toward the undisplaced normal on smooth "
                    "faces only, which the export cannot tell apart. Set Base to Displaced, "
                    "Strength to 1, or bake the material"
                )
    return None


def _normal_map_tangent_expr(node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Normal Map as a unit tangent-space normal, the vector ``normal`` takes."""
    provenance = list(provenance or ()) + [_node_label(node)]
    refusal = normal_map_refusal(node)
    if refusal is not None:
        return {"kind": "unresolved", "provenance": provenance, "reason": refusal}
    global _computed_image_reads
    color_socket = _named_socket(node, "Color")
    if color_socket is not None and getattr(color_socket, "is_linked", False):
        # Every image reads at a computed coordinate so the decode and the
        # strength below can be authored on its value.
        _computed_image_reads += 1
        try:
            color = _resolve_socket_value(color_socket, set(visited or ()), None, list(provenance), {}, expected_type="color3")
        finally:
            _computed_image_reads -= 1
    else:
        value = _socket_default_value(color_socket) if color_socket is not None else (0.5, 0.5, 1.0)
        color = _constant_expr(tuple(float(x) for x in list(value)[:3]))
    color = _vector3_operand_expr(color)
    if not color or color.get("kind") == "unresolved":
        return color
    decoded = _vector3_node(
        "multiply",
        in1=_vector3_node("subtract", in1=color, in2=_constant_expr((0.5, 0.5, 0.5))),
        in2=_constant_expr((2.0, -2.0, 2.0) if str(getattr(node, "convention", "OPENGL")).upper() == 'DIRECTX' else (2.0, 2.0, 2.0)),
    )
    strength = _vector_math_scalar_operand(node, 'Strength', visited, None, provenance, cache)
    if not strength or strength.get("kind") == "unresolved":
        return strength
    s = _constant_value(strength)
    if s is not None:
        s = float(s)
        saturated = min(max(s, 0.0), 1.0)
        if s != 1.0:
            decoded = _vector3_node(
                "add",
                in1=_vector3_node("multiply", in1=decoded, in2=_constant_expr((s, s, saturated))),
                in2=_constant_expr((0.0, 0.0, 1.0 - saturated)),
            )
    else:
        saturated = _make_node_expr(_nodedef_for("clamp", "float"), {
            "in": strength, "low": _constant_expr(0.0), "high": _constant_expr(1.0),
        })
        scale = _make_node_expr(_nodedef_for("combine3", "vector3"), {"in1": strength, "in2": strength, "in3": saturated})
        lift = _make_node_expr(_nodedef_for("combine3", "vector3"), {
            "in1": _constant_expr(0.0), "in2": _constant_expr(0.0),
            "in3": _fold_float("subtract", _constant_expr(1.0), saturated),
        })
        decoded = _vector3_node("add", in1=_vector3_node("multiply", in1=decoded, in2=scale), in2=lift)
    return _make_node_expr(
        _nodedef_for("ifgreater", "vector3", input_type="vector3"),
        {"value1": _vector_dot(decoded, decoded), "value2": _constant_expr(0.0),
         "in1": _vector3_node("normalize", **{"in": decoded}), "in2": _constant_expr((0.0, 0.0, 1.0))},
    )


def tangent_normal_to_world(tangent: Dict[str, Any]) -> Dict[str, Any]:
    """A unit tangent-space normal in Blender's world axes: ``x T + y B + z N``."""
    tangent_axis, bitangent_axis, normal_axis = (
        blender_world_vector(reader)
        for reader in (
            _world_reader_expr("tangent", index=0),
            _world_reader_expr("bitangent", index=0),
            _world_reader_expr("normal"),
        )
    )
    world = _vector3_node(
        "add",
        in1=_vector3_node(
            "add",
            in1=_vector_times_float(tangent_axis, _component_expr(tangent, "vector3", "x")),
            in2=_vector_times_float(bitangent_axis, _component_expr(tangent, "vector3", "y")),
        ),
        in2=_vector_times_float(normal_axis, _component_expr(tangent, "vector3", "z")),
    )
    return _make_node_expr(
        _nodedef_for("ifgreater", "vector3", input_type="vector3"),
        {"value1": _vector_dot(world, world), "value2": _constant_expr(0.0),
         "in1": _vector3_node("normalize", **{"in": world}), "in2": normal_axis},
    )


def _normal_map_texture_spec(node, cache) -> Optional[Dict[str, Any]]:
    """The Normal Map as RealityKit's own tangent decode of a UV image, when it can be.

    That path, verified by the kit's normal-map scene, needs a plain image
    read, a constant Strength and the OpenGL convention; anything else is
    authored by ``_normal_map_tangent_expr``.
    """
    if normal_map_refusal(node) is not None:
        return None
    if str(getattr(node, "convention", "OPENGL") or "OPENGL").upper() != 'OPENGL':
        return None
    strength = _named_socket(node, "Strength")
    if strength is None or _socket_is_live(strength):
        return None
    color_socket = _named_socket(node, "Color")
    if color_socket is None or not getattr(color_socket, "is_linked", False):
        return None
    resolved = _resolve_socket_value(color_socket, cache=cache, expected_type="color3")
    if not resolved or resolved.get("kind") != "texture":
        return None
    resolved = dict(resolved)
    resolved["scale"] = float(strength.default_value)
    resolved["space"] = "tangent"
    return resolved


def surface_normal_expr(socket, cache) -> Optional[Dict[str, Any]]:
    """What a Principled Normal or Coat Normal socket gives PBR Surface 2's tangent-space input.

    A Normal Map wired straight in keeps its tangent-space form; everything
    else - Bump, Geometry Normal, vector math, a Normal Map through another
    node - is a Blender world-space normal and is turned into tangent space.
    """
    upstream, _output = _upstream_through_reroutes(socket)
    if getattr(upstream, "type", "") == 'NORMAL_MAP':
        spec = _normal_map_texture_spec(upstream, cache)
        if spec is not None:
            return spec
        return _normal_map_tangent_expr(upstream, set(), [], cache)
    global _computed_image_reads
    _computed_image_reads += 1
    try:
        resolved = _resolve_socket_value(socket, cache={}, expected_type="vector3")
    finally:
        _computed_image_reads -= 1
    if not resolved or resolved.get("kind") == "unresolved":
        return resolved
    world = _vector3_operand_expr(resolved)
    if not world or world.get("kind") == "unresolved":
        return world
    return world_normal_to_tangent_space(world)


def _reader_node_expr(node, from_socket, visited, channel, provenance, cache) -> Optional[Dict[str, Any]]:
    node_type = getattr(node, "type", "")
    output_name = getattr(from_socket, "name", "") or ""
    refusal = reader_refusal(node, output_name)
    if refusal is not None:
        return {"kind": "unresolved", "provenance": list(provenance), "reason": refusal}
    if node_type == 'TEX_COORD':
        return _texture_coordinate_expr(output_name)
    if node_type == 'NEW_GEOMETRY':
        return _geometry_reader_expr(output_name)
    if node_type == 'FRESNEL':
        return _fresnel_node_expr(node, visited, channel, provenance, cache)
    if node_type == 'LAYER_WEIGHT':
        return _layer_weight_expr(node, output_name, visited, channel, provenance, cache)
    return None


def _unresolved(provenance, reason: str) -> Dict[str, Any]:
    return {"kind": "unresolved", "provenance": list(provenance or ()), "reason": reason}


def _color_socket_expr(socket, visited, provenance, cache, default=(0.0, 0.0, 0.0)) -> Optional[Dict[str, Any]]:
    """A colour socket's value as a color3 expression: its link, else its value."""
    if socket is None:
        return _constant_expr(tuple(default))
    if getattr(socket, "is_linked", False):
        resolved = _resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache, expected_type="color3",
        )
        return _color3_operand_expr(resolved)
    value = _socket_default_value(socket)
    return _color3_operand_expr(_constant_expr(value if value is not None else default))


def _clamp_between(value, low, high, swap: bool) -> Dict[str, Any]:
    """Cycles' ``clamp(value, min, max)``, which is ``min(max(value, min), max)``.

    With ``swap`` (Clamp's Range type, and the clamp Map Range appends) the
    bounds are exchanged when min > max, as ``svm_node_clamp`` does.
    """
    lo, hi = _constant_scalar(low), _constant_scalar(high)
    if lo is not None and hi is not None:
        if swap and lo > hi:
            low, high, lo, hi = high, low, hi, lo
        if lo <= hi:
            return _fnode("clamp", **{"in": value, "low": low, "high": high})
        return _fnode("min", in1=_fnode("max", in1=value, in2=low), in2=high)
    minmax = _fnode("min", in1=_fnode("max", in1=value, in2=low), in2=high)
    if not swap:
        return minmax
    swapped = _fnode("min", in1=_fnode("max", in1=value, in2=high), in2=low)
    return _select("ifgreater", low, high, swapped, minmax)


# --- Mix -------------------------------------------------------------------

#: Blend types whose Cycles function returns input A exactly at factor 0.
#: Dodge and Burn clamp A, Exclusion floors it at 0, and Saturation and Value
#: round-trip it through HSV, so none of them passes A through.
_MIX_BLENDS_A_AT_ZERO = frozenset({
    'MIX', 'ADD', 'MULTIPLY', 'SUBTRACT', 'SCREEN', 'OVERLAY', 'DIVIDE',
    'DIFFERENCE', 'DARKEN', 'LIGHTEN', 'SOFT_LIGHT', 'LINEAR_LIGHT', 'HUE', 'COLOR',
})

#: Mix data types the resolver authors: Blender's Mix node computes Float and
#: Vector as a plain lerp and Color through the blend type.
_MIX_DATA_TYPES = frozenset({'FLOAT', 'VECTOR', 'RGBA'})


def _mix_settings(node) -> Dict[str, Any]:
    """How Cycles evaluates a Mix or legacy MixRGB node.

    From intern/cycles/blender/shader.cpp: MixRGB always saturates its factor
    (``svm_mix_clamped_factor``) and clamps the result when Use Clamp is on.
    The Mix node saturates the factor when Clamp Factor is on, clamps the
    result only for Color and only when Clamp Result is on, applies the blend
    type only to Color, and mixes Vector per component in Non-Uniform mode.
    """
    blend = (getattr(node, "blend_type", "") or "MIX").upper()
    if getattr(node, "type", "") == 'MIX_RGB':
        return {
            "data_type": 'RGBA', "uniform": True, "clamp_factor": True,
            "clamp_result": bool(getattr(node, "use_clamp", False)), "blend": blend,
        }
    data_type = (getattr(node, "data_type", "RGBA") or "RGBA").upper()
    factor_mode = (getattr(node, "factor_mode", "UNIFORM") or "UNIFORM").upper()
    return {
        "data_type": data_type,
        "uniform": not (data_type == 'VECTOR' and factor_mode == 'NON_UNIFORM'),
        "clamp_factor": bool(getattr(node, "clamp_factor", True)),
        "clamp_result": data_type == 'RGBA' and bool(getattr(node, "clamp_result", False)),
        "blend": blend if data_type == 'RGBA' else 'MIX',
    }


def _mix_factor_socket(node):
    inputs = getattr(node, "inputs", None)
    if inputs is None or not hasattr(inputs, "get"):
        return None
    return inputs.get('Fac') or inputs.get('Factor')


def _mix_passthrough(node) -> Optional[str]:
    """'A' or 'B' when Cycles returns that input unchanged (before any result
    clamp), from a constant factor; None otherwise."""
    if not node or getattr(node, "type", "") not in {'MIX', 'MIX_RGB'}:
        return None
    settings = _mix_settings(node)
    if settings["data_type"] not in _MIX_DATA_TYPES:
        return None
    blend, fac, a_socket, b_socket = _mix_node_params(node)
    if fac is None:
        return None
    values = list(fac) if isinstance(fac, (list, tuple)) else [fac]
    if all(value == 0.0 for value in values) and blend in _MIX_BLENDS_A_AT_ZERO:
        return 'A' if a_socket is not None and getattr(a_socket, "is_linked", False) else None
    if all(value == 1.0 for value in values) and blend == 'MIX':
        return 'B' if b_socket is not None and getattr(b_socket, "is_linked", False) else None
    return None


def _mix_expr(node, visited, channel, provenance, cache, expected_type) -> Optional[Dict[str, Any]]:
    """Blender's Mix and MixRGB nodes, from Cycles' ``svm_node_mix``,
    ``svm_node_mix_color``, ``svm_node_mix_float``, ``svm_node_mix_vector``
    and ``svm_node_mix_vector_non_uniform``.

    Each data type is authored in its own MaterialX type and converted for
    the consumer afterwards, as Blender converts the node's output socket.
    Returns None for a shape the capability gate refuses.
    """
    settings = _mix_settings(node)
    data_type = settings["data_type"]
    if data_type not in _MIX_DATA_TYPES:
        return _unresolved(provenance, f"Mix of {data_type.title()} values has no MaterialX export; bake the material")
    if not _is_supported_mix(node):
        return None
    blend, fac, a_socket, b_socket = _mix_node_params(node)
    uniform = settings["uniform"]

    def operand(socket):
        if data_type == 'FLOAT':
            return _float_socket_expr(socket, visited, provenance, cache)
        if data_type == 'VECTOR':
            return _vector_socket_expr(socket, visited, provenance, cache)
        return _color_socket_expr(socket, visited, provenance, cache)

    passthrough = _mix_passthrough(node)
    if passthrough is not None:
        result = operand(a_socket if passthrough == 'A' else b_socket)
    else:
        if fac is not None:
            t = _constant_expr(tuple(fac) if isinstance(fac, (list, tuple)) else float(fac))
        elif uniform:
            t = _float_socket_expr(_mix_factor_socket(node), visited, provenance, cache)
            if settings["clamp_factor"]:
                t = _clamp_float(t, 0.0, 1.0)
        else:
            t = _vector_socket_expr(_mix_factor_socket(node), visited, provenance, cache)
            if settings["clamp_factor"]:
                t = _vector3_node("clamp", **{"in": t, "low": _constant_expr((0.0, 0.0, 0.0)), "high": _constant_expr((1.0, 1.0, 1.0))})
        a = operand(a_socket)
        b = operand(b_socket)
        if a is None or b is None or t is None:
            return None
        native = {'FLOAT': 'float', 'VECTOR': 'vector3', 'RGBA': 'color3'}[data_type]
        if blend == 'MIX':
            target = b
        else:
            op_name = {'MULTIPLY': 'multiply', 'ADD': 'add', 'SUBTRACT': 'subtract'}[blend]
            target = _color3_node(op_name, in1=a, in2=b)
        if uniform:
            if _expr_is_constant(t, 1.0):
                result = target
            else:
                result = _make_node_expr(_nodedef_for("mix", native), {"bg": a, "fg": target, "mix": t})
        else:
            # a * (1 - t) + b * t per component, authored as a + (b - a) * t.
            result = _vector3_node("add", in1=a, in2=_vector3_node("multiply", in1=_vector3_node("subtract", in1=target, in2=a), in2=t))
    if not isinstance(result, dict):
        return None
    if settings["clamp_result"]:
        result = _color3_node("clamp", **{"in": result, "low": _constant_expr((0.0, 0.0, 0.0)), "high": _constant_expr((1.0, 1.0, 1.0))})
    return _typed_result(result, expected_type, channel)


# --- Clamp and Map Range ------------------------------------------------------

def _clamp_node_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Clamp node, ``svm_node_clamp``: Range swaps inverted bounds."""
    value = _named_float_operand(node, 'Value', visited, provenance, cache, default=1.0)
    low = _named_float_operand(node, 'Min', visited, provenance, cache, default=0.0)
    high = _named_float_operand(node, 'Max', visited, provenance, cache, default=1.0)
    clamp_type = (getattr(node, "clamp_type", "MINMAX") or "MINMAX").upper()
    return _clamp_between(value, low, high, swap=clamp_type == 'RANGE')


def map_range_refusal(node) -> Optional[str]:
    """Why a Map Range node cannot be exported, or None."""
    interpolation = (getattr(node, "interpolation_type", "LINEAR") or "LINEAR").upper()
    if interpolation != 'LINEAR':
        return (
            f"Map Range interpolation '{interpolation}' is not exportable; only Linear "
            "is transcribed. Use Linear or bake the material."
        )
    data_type = (getattr(node, "data_type", "FLOAT") or "FLOAT").upper()
    if data_type not in ('FLOAT', 'FLOAT_VECTOR'):
        return f"Map Range data type '{data_type}' is not exportable; bake the material."
    return None


def _map_range_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Map Range node in Linear mode, from ``svm_node_map_range``,
    ``svm_node_vector_map_range`` and ``MapRangeNode::expand``.

    Float: ``from_max != from_min ? to_min + (value - from_min) /
    (from_max - from_min) * (to_max - to_min) : 0``, then with Clamp a Range
    clamp to the To bounds, swapped when inverted. Vector: the division is the
    per-component safe_divide, and the clamp swaps per component.
    """
    refusal = map_range_refusal(node)
    if refusal:
        return _unresolved(provenance, refusal)
    data_type = (getattr(node, "data_type", "FLOAT") or "FLOAT").upper()
    use_clamp = bool(getattr(node, "clamp", False))
    if data_type == 'FLOAT_VECTOR':
        def vec(name, default):
            return _vector_socket_expr(_named_socket(node, name), visited, provenance, cache, default=default)
        value = vec('Vector', (0.0, 0.0, 0.0))
        from_min, from_max = vec('From Min', (0.0, 0.0, 0.0)), vec('From Max', (1.0, 1.0, 1.0))
        to_min, to_max = vec('To Min', (0.0, 0.0, 0.0)), vec('To Max', (1.0, 1.0, 1.0))
        for part in (value, from_min, from_max, to_min, to_max):
            if not isinstance(part, dict):
                return _unresolved(provenance, "a Map Range vector input could not be read")
            if part.get("kind") == "unresolved":
                return part
        factor = _safe_divide_vector(
            _vector3_node("subtract", in1=value, in2=from_min),
            _vector3_node("subtract", in1=from_max, in2=from_min),
        )
        result = _vector3_node(
            "add", in1=to_min,
            in2=_vector3_node("multiply", in1=factor, in2=_vector3_node("subtract", in1=to_max, in2=to_min)),
        )
        if use_clamp:
            result = _per_component_vector3(lambda r, lo, hi: _clamp_between(r, lo, hi, swap=True), result, to_min, to_max)
        return result

    def flt(name, default):
        return _named_float_operand(node, name, visited, provenance, cache, default=default)
    value = flt('Value', 1.0)
    from_min, from_max = flt('From Min', 0.0), flt('From Max', 1.0)
    to_min, to_max = flt('To Min', 0.0), flt('To Max', 1.0)
    for part in (value, from_min, from_max, to_min, to_max):
        if part.get("kind") == "unresolved":
            return part
    low, high = _constant_scalar(from_min), _constant_scalar(from_max)
    if low is not None and high is not None and low == high:
        result = _constant_expr(0.0)
    else:
        factor = _fnode("divide", in1=_fold_float("subtract", value, from_min), in2=_fold_float("subtract", from_max, from_min))
        result = _fold_float("add", to_min, _fold_float("multiply", factor, _fold_float("subtract", to_max, to_min)))
        if low is None or high is None:
            result = _select("ifequal", from_max, from_min, 0.0, result)
    if use_clamp:
        result = _clamp_between(result, to_min, to_max, swap=True)
    return result


# --- Colour nodes ------------------------------------------------------------

def _cycles_rgb_to_hsv(color: Dict[str, Any]) -> Dict[str, Any]:
    """Cycles' ``rgb_to_hsv`` on a color3 expression, as a color3 (h, s, v).

    RealityKit's ND_rgbtohsv_color3 (MaterialX 1.39 metallib) computes s and v
    as Cycles does and the hue with the same branch order, but leaves a
    red-dominant hue in [-1/6, 0) where Cycles adds 1, and computes a hue where
    the maximum is 0 but the channels differ, where Cycles leaves it 0. Both
    are corrected here.
    """
    hsv = _make_node_expr(_nodedef_for("rgbtohsv", "color3"), {"in": color})
    h = _component_expr(hsv, "color3", "r")
    s = _component_expr(hsv, "color3", "g")
    v = _component_expr(hsv, "color3", "b")
    h = _select("ifgreater", 0.0, h, _fnode("add", in1=h, in2=_constant_expr(1.0)), h)
    h = _select("ifequal", s, 0.0, 0.0, h)
    return _make_node_expr(_nodedef_for("combine3", "color3"), {"in1": h, "in2": s, "in3": v})


def _hue_saturation_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Hue/Saturation/Value node, ``svm_node_hsv``.

    ``h = fract(h + hue + 0.5)``, ``s = saturate(s * saturation)``,
    ``v *= value``, back to RGB, mixed with the input by Fac, and floored at 0.
    ND_hsvadjust adds its hue amount without the 0.5 offset, so it is not used.
    The hue wrap RealityKit's rgbtohsv omits is period 1 and vanishes in the
    fract, and hsvtorgb ignores the hue where the saturation is 0.
    """
    color = _named_color_operand(node, 'Color', visited, provenance, cache, default=(0.8, 0.8, 0.8))
    hue = _named_float_operand(node, 'Hue', visited, provenance, cache, default=0.5)
    saturation = _named_float_operand(node, 'Saturation', visited, provenance, cache, default=1.0)
    value = _named_float_operand(node, 'Value', visited, provenance, cache, default=1.0)
    fac = _named_float_operand(node, 'Fac', visited, provenance, cache, default=1.0)
    hsv = _make_node_expr(_nodedef_for("rgbtohsv", "color3"), {"in": color})
    h = _component_expr(hsv, "color3", "r")
    s = _component_expr(hsv, "color3", "g")
    v = _component_expr(hsv, "color3", "b")
    shifted = _fnode("add", in1=_fnode("add", in1=h, in2=hue), in2=_constant_expr(0.5))
    h = _fnode("subtract", in1=shifted, in2=_fnode("floor", **{"in": shifted}))
    s = _clamp_float(_fnode("multiply", in1=s, in2=saturation), 0.0, 1.0)
    v = _fnode("multiply", in1=v, in2=value)
    rgb = _make_node_expr(
        _nodedef_for("hsvtorgb", "color3"),
        {"in": _make_node_expr(_nodedef_for("combine3", "color3"), {"in1": h, "in2": s, "in3": v})},
    )
    if not _expr_is_constant(fac, 1.0):
        rgb = _make_node_expr(_nodedef_for("mix", "color3"), {"bg": color, "fg": rgb, "mix": fac})
    return _color3_node("max", in1=rgb, in2=_constant_expr((0.0, 0.0, 0.0)))


def _invert_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Invert Color node, ``svm_node_invert``: per component
    ``fac * (1 - c) + (1 - fac) * c``, a colour whatever the consumer."""
    color = _named_color_operand(node, 'Color', visited, provenance, cache, default=(0.0, 0.0, 0.0))
    fac = _named_float_operand(node, 'Fac', visited, provenance, cache, default=1.0)
    if _expr_is_constant(fac, 0.0):
        return color
    inverted = _make_node_expr(_nodedef_for("oneminus", "color3"), {"in": color})
    if _expr_is_constant(fac, 1.0):
        return inverted
    return _make_node_expr(_nodedef_for("mix", "color3"), {"bg": color, "fg": inverted, "mix": fac})


def _bright_contrast_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Brightness/Contrast node, ``svm_brightness_contrast``:
    ``max((1 + contrast) * c + (brightness - contrast * 0.5), 0)`` per component."""
    color = _named_color_operand(node, 'Color', visited, provenance, cache, default=(0.0, 0.0, 0.0))
    bright = _named_float_operand(node, 'Bright', visited, provenance, cache, default=0.0)
    contrast = _named_float_operand(node, 'Contrast', visited, provenance, cache, default=0.0)
    a = _fold_float("add", _constant_expr(1.0), contrast)
    b = _fold_float("subtract", bright, _fold_float("multiply", contrast, _constant_expr(0.5)))
    scaled = _color3_node("multiply", in1=color, in2=_broadcast_color3(a))
    shifted = _color3_node("add", in1=scaled, in2=_broadcast_color3(b))
    return _color3_node("max", in1=shifted, in2=_constant_expr((0.0, 0.0, 0.0)))


def _rgb_to_bw_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's RGB to BW node, Cycles' ``linear_rgb_to_gray``: a float."""
    color = _named_color_operand(node, 'Color', visited, provenance, cache, default=(0.5, 0.5, 0.5))
    if not isinstance(color, dict) or color.get("kind") == "unresolved":
        return color or _unresolved(provenance, "RGB to BW has no readable colour")
    value = _constant_value(color)
    if isinstance(value, (list, tuple)):
        return _constant_expr(sum(float(c) * w for c, w in zip(value, _RGB_TO_GRAY)))
    as_vector = _make_node_expr(_nodedef_for("convert", "vector3", input_type="color3"), {"in": color})
    return _vector_dot(as_vector, _constant_expr(_RGB_TO_GRAY))


# --- Procedural textures -------------------------------------------------------

#: 2 * pi as the float32 Cycles divides by (M_2PI_F).
_M_2PI_F = 6.2831855


def _gradient_texture_expr(node, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's Gradient Texture, ``svm_gradient`` then ``saturatef``, exactly.

    An unlinked Vector samples the Generated-coordinate stand-in, which is
    what differs from Blender, and says so in the warning pass.
    """
    p = _vector3_operand_expr(_procedural_vector_expr(node, visited, None, provenance, cache))
    if not isinstance(p, dict):
        return _unresolved(provenance, "the Gradient Texture's Vector could not be read")
    if p.get("kind") == "unresolved":
        return p
    x = _component_of(p, "x")
    y = _component_of(p, "y")
    kind = (getattr(node, "gradient_type", "LINEAR") or "LINEAR").upper()
    if kind == 'LINEAR':
        f = x
    elif kind == 'QUADRATIC':
        r = _fnode("max", in1=x, in2=_constant_expr(0.0))
        f = _fnode("multiply", in1=r, in2=r)
    elif kind == 'EASING':
        r = _clamp_between(x, _constant_expr(0.0), _constant_expr(1.0), swap=False)
        t = _fnode("multiply", in1=r, in2=r)
        f = _fnode(
            "subtract",
            in1=_fnode("multiply", in1=_constant_expr(3.0), in2=t),
            in2=_fnode("multiply", in1=_fnode("multiply", in1=_constant_expr(2.0), in2=t), in2=r),
        )
    elif kind == 'DIAGONAL':
        f = _fnode("multiply", in1=_fnode("add", in1=x, in2=y), in2=_constant_expr(0.5))
    elif kind == 'RADIAL':
        # atan2f(0, 0) is 0 on the CPU Cycles renders with; RealityKit's is NaN.
        angle = _compatible_atan2_float(y, x)
        f = _fnode("add", in1=_fnode("divide", in1=angle, in2=_constant_expr(_M_2PI_F)), in2=_constant_expr(0.5))
    elif kind in ('SPHERICAL', 'QUADRATIC_SPHERE'):
        length = _make_node_expr(_nodedef_for("magnitude", "float", input_type="vector3"), {"in": p})
        r = _fnode("max", in1=_fnode("subtract", in1=_constant_expr(0.999999), in2=length), in2=_constant_expr(0.0))
        f = _fnode("multiply", in1=r, in2=r) if kind == 'QUADRATIC_SPHERE' else r
    else:
        return _unresolved(provenance, f"Gradient Texture type '{kind}' is not exportable; bake the material")
    return _clamp_float(f, 0.0, 1.0)


def _socket_constant_or_none(node, name) -> Tuple[bool, Optional[float]]:
    """(live, value): whether the socket varies, and its value when it does not."""
    socket = _named_socket(node, name)
    if socket is None:
        return False, None
    if _socket_is_live(socket):
        return True, None
    try:
        return False, float(socket.default_value)
    except (TypeError, ValueError):
        return False, None


def noise_texture_refusal(node) -> Optional[str]:
    """Why a Noise Texture cannot be exported, or None.

    The export is ND_fractal3d, MaterialX's fBm of Perlin noise: its hash
    differs from Blender's, so only 3D fBm with a constant Detail and
    Roughness (which fix the octave count and the normalisation) and no
    Distortion has Blender's value distribution.
    """
    dimensions = str(getattr(node, "noise_dimensions", "3D") or "3D").upper()
    if dimensions != '3D':
        return f"Noise Texture in {dimensions} is not exportable; only 3D is. Use 3D or bake the material."
    noise_type = str(getattr(node, "noise_type", "FBM") or "FBM").upper()
    if noise_type != 'FBM':
        return f"Noise Texture type '{noise_type}' is not exportable; only fBM is. Use fBM or bake the material."
    for name in ('Detail', 'Roughness'):
        live, _value = _socket_constant_or_none(node, name)
        if live:
            return (
                f"Noise Texture with a linked or driven {name} is not exportable; the octave "
                "count and normalisation need a constant. Bake the material."
            )
    live, distortion = _socket_constant_or_none(node, 'Distortion')
    if live or (distortion is not None and distortion != 0.0):
        return "Noise Texture Distortion is not exportable; set Distortion to 0 or bake the material."
    return None


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _cycles_hash_uint2(kx: int, ky: int) -> int:
    """Cycles' ``hash_uint2`` (Bob Jenkins' lookup3 ``final``)."""
    mask = 0xFFFFFFFF

    def rot(x, k):
        return ((x << k) | (x >> (32 - k))) & mask

    a = b = c = (0xDEADBEEF + (2 << 2) + 13) & mask
    b = (b + ky) & mask
    a = (a + kx) & mask
    c ^= b
    c = (c - rot(b, 14)) & mask
    a ^= c
    a = (a - rot(c, 11)) & mask
    b ^= a
    b = (b - rot(a, 25)) & mask
    c ^= b
    c = (c - rot(b, 16)) & mask
    a ^= c
    a = (a - rot(c, 4)) & mask
    b ^= a
    b = (b - rot(a, 14)) & mask
    c ^= b
    c = (c - rot(b, 24)) & mask
    return c


def _cycles_random_float3_offset(seed: float) -> Tuple[float, float, float]:
    """Cycles' ``random_float3_offset``, the per-channel offset of Noise Color."""
    def bits(value):
        return struct.unpack("<I", struct.pack("<f", value))[0]

    def component(index):
        hashed = _f32(float(_f32(float(_cycles_hash_uint2(bits(seed), bits(float(index)))))) * _f32(1.0 / 4294967296.0))
        return _f32(100.0 + _f32(hashed * 100.0))

    return (component(0), component(1), component(2))


def _noise_texture_expr(node, from_socket, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's 3D fBm Noise Texture with Blender's octave count and range.

    ``noise_fbm`` in intern/cycles/kernel/svm/fractal_noise.h sums
    ``floor(detail) + 1`` octaves of signed noise with amplitude
    ``roughness^i`` at frequency ``lacunarity^i`` and, with Normalize, maps the
    sum to ``0.5 * sum / maxamp + 0.5``; a fractional Detail blends in one
    more octave. ND_fractal3d sums the same series of MaterialX's Perlin noise,
    which is scaled like Cycles' (0.982) but hashed differently, so the pattern
    differs and its value distribution matches. The Color output is Blender's
    three decorrelated evaluations at ``random_float3_offset(3)`` and ``(4)``.
    """
    refusal = noise_texture_refusal(node)
    if refusal:
        return _unresolved(provenance, refusal)
    p = _vector3_operand_expr(_procedural_vector_expr(node, visited, None, provenance, cache))
    if not isinstance(p, dict):
        return _unresolved(provenance, "the Noise Texture's Vector could not be read")
    if p.get("kind") == "unresolved":
        return p
    scale = _named_float_operand(node, 'Scale', visited, provenance, cache, default=5.0)
    lacunarity = _named_float_operand(node, 'Lacunarity', visited, provenance, cache, default=2.0)
    for part in (scale, lacunarity):
        if part.get("kind") == "unresolved":
            return part
    if not _expr_is_constant(scale, 1.0):
        p = _vector_times_float(p, scale)
    _live, detail = _socket_constant_or_none(node, 'Detail')
    _live, roughness = _socket_constant_or_none(node, 'Roughness')
    detail = min(max(2.0 if detail is None else detail, 0.0), 15.0)
    roughness = max(0.5 if roughness is None else roughness, 0.0)
    normalize = bool(getattr(node, "normalize", True))
    octaves = int(detail) + 1
    maxamp = sum(roughness ** i for i in range(octaves))
    remainder = detail - math.floor(detail)

    def fractal(position, count):
        return _fnode(
            "fractal3d",
            position=position,
            octaves=_constant_expr(count),
            lacunarity=lacunarity,
            diminish=_constant_expr(roughness),
            amplitude=_constant_expr(1.0),
        )

    def normalized(total, amplitude_sum):
        if not normalize:
            return total
        return _fnode(
            "add",
            in1=_fnode("divide", in1=_fnode("multiply", in1=_constant_expr(0.5), in2=total), in2=_constant_expr(amplitude_sum)),
            in2=_constant_expr(0.5),
        )

    def fbm(position):
        value = normalized(fractal(position, octaves), maxamp)
        if remainder != 0.0:
            finer = normalized(fractal(position, octaves + 1), maxamp + roughness ** octaves)
            value = _fnode("mix", bg=value, fg=finer, mix=_constant_expr(remainder))
        return value

    output_name = getattr(from_socket, "name", "") or ""
    if output_name != 'Color':
        return fbm(p)
    channels = [fbm(p)]
    for seed in (3.0, 4.0):
        channels.append(fbm(_vector3_node("add", in1=p, in2=_constant_expr(_cycles_random_float3_offset(seed)))))
    return _make_node_expr(
        _nodedef_for("combine3", "color3"), {"in1": channels[0], "in2": channels[1], "in3": channels[2]}
    )


def voronoi_texture_refusal(node, output_names) -> Optional[str]:
    """Why a Voronoi Texture cannot be exported, or None.

    ND_worleynoise3d is the Euclidean F1 distance of a jittered 3D cell grid;
    only that output and feature, with no fractal octaves, are exported.
    """
    for name in output_names:
        if name not in ('Distance',):
            return (
                f"Voronoi Texture's {name} output is not exportable; only Distance is. "
                "Bake the material."
            )
    dimensions = str(getattr(node, "voronoi_dimensions", "3D") or "3D").upper()
    if dimensions != '3D':
        return f"Voronoi Texture in {dimensions} is not exportable; only 3D is. Use 3D or bake the material."
    feature = str(getattr(node, "feature", "F1") or "F1").upper()
    if feature != 'F1':
        return f"Voronoi Texture feature '{feature}' is not exportable; only F1 is. Use F1 or bake the material."
    metric = str(getattr(node, "distance", "EUCLIDEAN") or "EUCLIDEAN").upper()
    if metric != 'EUCLIDEAN':
        return f"Voronoi Texture distance metric '{metric}' is not exportable; only Euclidean is. Bake the material."
    detail_live, detail = _socket_constant_or_none(node, 'Detail')
    roughness_live, roughness = _socket_constant_or_none(node, 'Roughness')
    single_octave = (not detail_live and (detail is None or detail == 0.0)) or (
        not roughness_live and roughness is not None and roughness <= 0.0
    )
    if not single_octave:
        return "Voronoi Texture Detail above 0 is not exportable; set Detail to 0 or bake the material."
    return None


def _voronoi_texture_expr(node, from_socket, visited, provenance, cache) -> Dict[str, Any]:
    """Blender's 3D Euclidean F1 Voronoi Distance.

    ``svm_node_tex_voronoi`` clamps Randomness to [0, 1], scales the
    coordinate, and with Normalize divides by the largest distance,
    ``length((0.5 + 0.5 * randomness) * (1, 1, 1))``. ND_worleynoise3d jitters
    its feature points with its own hash, so the cells differ and the distance
    distribution matches.
    """
    output_name = getattr(from_socket, "name", "") or "Distance"
    refusal = voronoi_texture_refusal(node, [output_name])
    if refusal:
        return _unresolved(provenance, refusal)
    p = _vector3_operand_expr(_procedural_vector_expr(node, visited, None, provenance, cache))
    if not isinstance(p, dict):
        return _unresolved(provenance, "the Voronoi Texture's Vector could not be read")
    if p.get("kind") == "unresolved":
        return p
    scale = _named_float_operand(node, 'Scale', visited, provenance, cache, default=5.0)
    randomness = _named_float_operand(node, 'Randomness', visited, provenance, cache, default=1.0)
    for part in (scale, randomness):
        if part.get("kind") == "unresolved":
            return part
    if not _expr_is_constant(scale, 1.0):
        p = _vector_times_float(p, scale)
    jitter = _constant_scalar(randomness)
    jitter = _constant_expr(min(max(jitter, 0.0), 1.0)) if jitter is not None else _clamp_float(randomness, 0.0, 1.0)
    distance = _fnode("worleynoise3d", position=p, jitter=jitter)
    if bool(getattr(node, "normalize", False)):
        half = _fold_float("add", _constant_expr(0.5), _fold_float("multiply", _constant_expr(0.5), jitter))
        largest = _fold_float("multiply", half, _constant_expr(math.sqrt(3.0)))
        distance = _fnode("divide", in1=distance, in2=largest)
    return distance


def _procedural_vector_expr(node, visited, channel, provenance, cache):
    """Resolve the coordinate feeding a procedural texture node.

    A wired Vector resolves through the expression tree (an unresolvable
    chain propagates unresolved so the refusal fires). An unwired Vector must
    NOT fold to the socket default: ``_expr_from_socket`` returns the default
    (0, 0, 0) as a constant, which sampled every noise/voronoi/gradient at a
    single point and exported the pattern flat with no warning.
    """
    socket = node.inputs.get('Vector') if hasattr(node, "inputs") else None
    if socket is not None and getattr(socket, "is_linked", False):
        resolved = _expr_from_socket(socket, visited, channel, provenance, cache)
        if resolved is not None:
            return resolved
    return _generated_coordinate_expr()


# Combining blends the resolver can author as a real MaterialX node instead of
# requiring a bake. Plain 'MIX' is handled separately (it is a pure lerp).
_RESOLVABLE_MIX_BLENDS = {'MULTIPLY', 'ADD', 'SUBTRACT'}


def _mix_node_params(node):
    """Return (blend_type, factor, a_socket, b_socket) for a Mix/MixRGB node.

    ``blend_type`` is the blend Cycles applies: the node's own for Color, and
    a plain 'MIX' for the Float and Vector data types, which ignore it.
    ``factor`` is the constant Factor as Cycles uses it - saturated when the
    node clamps its factor, a tuple for a Non-Uniform Vector mix - or None
    when the Factor is linked or driven. Socket lookup handles both the legacy
    MixRGB (Color1/Color2) and the newer Mix (A/B) names; Blender returns the
    socket of the node's current data type for a shared name.
    """
    settings = _mix_settings(node)
    blend = settings["blend"]
    if not hasattr(node, "inputs"):
        return blend, None, None, None
    fac_socket = _mix_factor_socket(node)
    fac = None
    if fac_socket is not None and not _socket_is_live(fac_socket):
        raw = getattr(fac_socket, "default_value", None)
        try:
            if isinstance(raw, (int, float)):
                fac = float(raw)
                if settings["clamp_factor"]:
                    fac = min(max(fac, 0.0), 1.0)
            elif raw is not None and not settings["uniform"]:
                fac = tuple(float(v) for v in list(raw)[:3])
                if settings["clamp_factor"]:
                    fac = tuple(min(max(v, 0.0), 1.0) for v in fac)
        except (TypeError, ValueError):
            fac = None
    a_socket = node.inputs.get('Color1') or node.inputs.get('A')
    b_socket = node.inputs.get('Color2') or node.inputs.get('B')
    return blend, fac, a_socket, b_socket


def _is_supported_mix(node) -> bool:
    """Return True when the resolver can express a Mix/MixRGB node for RCP.

    Either a passthrough, or a combining blend (multiply/add/subtract) /
    general plain mix whose inputs are both linked - those are authored as real
    MaterialX nodes, so they do not require baking. The Factor may be a
    constant or a linked expression: ND_mix_*'s ``mix`` input accepts a wired
    float, and an unresolvable factor chain still fails the export loudly via
    the resolver's unresolved propagation. Rotation mixes are refused.
    """
    if not node or getattr(node, "type", "") not in {'MIX', 'MIX_RGB'}:
        return False
    if _mix_settings(node)["data_type"] not in _MIX_DATA_TYPES:
        return False
    if _mix_passthrough(node) is not None:
        return True
    blend, fac, a_socket, b_socket = _mix_node_params(node)
    if fac is None and not _mix_factor_is_linked(node):
        return False
    both_linked = bool(a_socket and b_socket and a_socket.is_linked and b_socket.is_linked)
    if blend == 'MIX':
        # A plain mix with a wired Factor authors ND_mix_* with the weight
        # connected, and an unlinked A or B is its socket constant, so a
        # Fresnel-driven blend between two colours needs no bake.
        return both_linked or _mix_factor_is_linked(node)
    return blend in _RESOLVABLE_MIX_BLENDS and both_linked


def _mix_factor_is_linked(node) -> bool:
    """Whether a Mix/MixRGB node's Factor socket is driven by a link."""
    fac_socket = _mix_factor_socket(node)
    return bool(fac_socket is not None and _socket_is_live(fac_socket))


def _extract_mapping_from_node(node) -> Optional[Dict[str, Any]]:
    """Extract mapping transform data from a Mapping node chain."""
    if not node:
        return None

    if node.type == 'REROUTE':
        input_socket = node.inputs[0] if getattr(node, "inputs", None) else None
        if input_socket and input_socket.is_linked:
            return _extract_mapping_from_node(input_socket.links[0].from_node)
        return None

    if node.type == 'MAPPING':
        vector_type = (getattr(node, "vector_type", "POINT") or "POINT").upper()
        if vector_type != "POINT":
            # A Principled input reads Texture, Vector and Normal mappings at
            # computed coordinates (image_uses_uv_transform); only a
            # RealityKit node group's texture input still lands here.
            raise ValueError(
                f"Mapping node '{getattr(node, 'name', 'Mapping')}' uses "
                f"vector_type={vector_type}, which RealityKit's texture transform "
                "cannot carry; bake the texture transform"
            )

        def constant_vector(socket_name: str, fallback):
            # Blender 2.81 moved these from node properties to sockets; 5.2 is
            # the only supported host, so the socket is always the source.
            socket = node.inputs.get(socket_name) if hasattr(node, "inputs") else None
            if socket is not None and socket.is_linked:
                raise ValueError(
                    f"Mapping node '{getattr(node, 'name', 'Mapping')}' has linked "
                    f"{socket_name}; bake the texture transform"
                )
            value = getattr(socket, "default_value", fallback) if socket else fallback
            return tuple(float(component) for component in value[:3])

        translation = constant_vector("Location", (0.0, 0.0, 0.0))
        rotation = constant_vector("Rotation", (0.0, 0.0, 0.0))
        scale = constant_vector("Scale", (1.0, 1.0, 1.0))
        if abs(rotation[0]) > 1e-6 or abs(rotation[1]) > 1e-6:
            raise ValueError(
                f"Mapping node '{getattr(node, 'name', 'Mapping')}' has X/Y rotation; "
                "RealityKit place2d can only represent Z rotation"
            )
        if abs(scale[0]) <= 1e-8 or abs(scale[1]) <= 1e-8:
            raise ValueError(
                f"Mapping node '{getattr(node, 'name', 'Mapping')}' has zero X/Y scale; "
                "MaterialX place2d cannot represent it safely"
            )

        # Blender POINT: rotate(uv * scale) + location. MaterialX place2d
        # SRT implements rotate(uv / scale) - offset.
        return {
            'offset': (-float(translation[0]), -float(translation[1])),
            'rotate': float(rotation[2]),
            'scale': (1.0 / float(scale[0]), 1.0 / float(scale[1])),
            'pivot': (0.0, 0.0),
            'operationorder': 0,
        }

    if node.type == 'TEX_COORD':
        return None

    if node.type == 'UVMAP':
        return None

    return None


def _extract_uv_map_from_node(node) -> Optional[str]:
    """Trace UV map name from a Blender vector node chain."""
    if not node:
        return None

    if node.type == 'UVMAP':
        return getattr(node, "uv_map", "") or ""

    if node.type == 'MAPPING':
        vector_socket = node.inputs.get("Vector")
        if vector_socket and vector_socket.is_linked:
            return _extract_uv_map_from_node(vector_socket.links[0].from_node)

    if node.type == 'TEX_COORD':
        # The UV output is the render UV map, which an empty name denotes.
        return ""

    return None


def _normalize_uv_map_name(uv_map: Optional[str]) -> str:
    """Guess a MaterialX geomprop name from a UV map name alone.

    Only for when no mesh can be inspected (outside Blender, or a material
    no mesh uses); ``uv_set_resolution`` decides from the meshes otherwise.
    """
    name = (uv_map or "").strip()
    if not name:
        return "UV0"
    lowered = name.lower()
    if lowered in {"uvmap", "uv0", "uv", "st", "st0"}:
        return "UV0"
    return name


def _uv_set_expr(texcoord: str) -> Dict[str, Any]:
    """A resolved texture-coordinate set as a vector3 with z = 0, as Cycles reads UVs."""
    if texcoord == "UV0":
        return _make_node_expr(_nodedef_for("texcoord", "vector3"), {})
    return _make_node_expr(
        _nodedef_for("convert", "vector3", input_type="vector2"),
        {"in": _make_node_expr(_nodedef_for("geompropvalue", "vector2"), {"geomprop": _constant_expr(texcoord)})},
    )


def _uv_map_node_expr(node, provenance) -> Dict[str, Any]:
    """Blender's UV Map node: the named UV map, or the render UV map when unnamed."""
    texcoord, refusal = uv_map_node_resolution(node)
    if refusal is not None:
        return {"kind": "unresolved", "provenance": list(provenance), "reason": refusal}
    return _uv_set_expr(texcoord)


def _render_uv_layer_name(mesh) -> Optional[str]:
    """The UV map Blender renders with, which its USD exporter writes as ``st``."""
    layers = list(getattr(mesh, "uv_layers", None) or [])
    for layer in layers:
        if getattr(layer, "active_render", False):
            return getattr(layer, "name", None)
    return getattr(layers[0], "name", None) if layers else None


def uv_map_node_resolution(node) -> Tuple[str, Optional[str]]:
    """``uv_set_resolution`` for a UV Map node, which also refuses From Instancer.

    From Instancer reads the UV map of the object instancing this one, which
    the export does not carry onto the instanced mesh.
    """
    if getattr(node, "from_instancer", False):
        return "UV0", (
            "UV Map 'From Instancer' reads the instancing object's UV map, which the export "
            "does not carry; turn it off or bake"
        )
    return uv_set_resolution(node, getattr(node, "uv_map", ""))


def uv_set_resolution(node, uv_map: Optional[str]) -> Tuple[str, Optional[str]]:
    """The texture-coordinate set a UV map name reads, and why it cannot, if so.

    Blender's USD exporter (``rename_uvmaps``) writes each mesh's render UV
    map as ``primvars:st``, which RealityKit's ``texcoord`` reader returns, and
    every other UV map under its own name. So a name is ``UV0`` only where it
    is the render UV map of every mesh using the material, and a
    ``geompropvalue`` of that name where it is none's. An empty name is the
    render UV map, as in Cycles. Measured by export: with ``Detail`` the render
    map, ``primvars:st`` held Detail and ``primvars:UVMap`` the other set.
    """
    name = (uv_map or "").strip()
    if not name:
        return "UV0", None
    meshes = _material_meshes(node)
    if not meshes:
        return _normalize_uv_map_name(name), None
    resolved = set()
    for mesh in meshes:
        layers = getattr(mesh, "uv_layers", None)
        if layers is None or layers.get(name) is None:
            return name, (
                f"UV map '{name}' is not on the mesh '{getattr(mesh, 'name', '?')}', which uses "
                "this material, so the export has no such coordinates to read; add the UV map "
                "or pick one every mesh has"
            )
        resolved.add("UV0" if _render_uv_layer_name(mesh) == name else name)
    if len(resolved) > 1:
        return name, (
            f"UV map '{name}' is the render UV map on some meshes using this material and not "
            "on others, and the export writes the two under different names; make it the "
            "render UV map on all of them or on none"
        )
    texcoord = resolved.pop()
    if texcoord != "UV0" and (texcoord.lower() == "st" or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', texcoord)):
        return name, (
            f"UV map '{name}' is not the render UV map and its name is not a primvar the export "
            "can address by name (letters, digits and underscores, not 'st'); rename it"
        )
    return texcoord, None


def _normalize_colorspace(name: Optional[str]) -> Optional[str]:
    """Normalize only color spaces verified in Blender 5.2 MaterialX output."""
    if not name:
        return None
    lowered = str(name).strip().lower()
    if lowered in {"srgb", "srgb texture", "s-rgb"}:
        return "srgb"
    if "non-color" in lowered or "raw" in lowered:
        return "raw"
    if lowered in {
        "linear rec.709",
        "linear rec709",
        "linear rec. 709",
        "scene_linear",
        "linear",
    }:
        return "lin_rec709"
    # Keep the original name visible so the USD authoring stage can fail
    # closed instead of inventing an unverified MaterialX token.
    return f"unsupported:{str(name).strip()}"


def _extract_constant_from_socket(socket):
    """Extract a constant value from Blender scalar/color/vector nodes."""
    if not socket or not socket.is_linked:
        return None
    from_node = socket.links[0].from_node
    if not from_node:
        return None

    if from_node.type == 'RGB':
        output = from_node.outputs.get('Color') if hasattr(from_node.outputs, 'get') else None
        value = output.default_value if output else from_node.outputs[0].default_value
        return list(value)[:3]

    if from_node.type == 'VALUE':
        output = from_node.outputs.get('Value') if hasattr(from_node.outputs, 'get') else None
        value = output.default_value if output else from_node.outputs[0].default_value
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if from_node.type in {'INPUT_BOOL', 'INPUT_INT', 'INPUT_VECTOR'}:
        return _input_constant_node_value(from_node)

    return None


def _input_constant_node_value(node):
    """Read Blender 5.2 FunctionNode constants from their node properties.

    The display output socket retains its type default; the artist-authored
    value lives on ``boolean``, ``integer``, or ``vector`` instead.
    """
    node_type = getattr(node, "type", "")
    try:
        if node_type == 'INPUT_BOOL':
            return bool(node.boolean)
        if node_type == 'INPUT_INT':
            return int(node.integer)
        if node_type == 'INPUT_VECTOR':
            dimensions = int(getattr(node, "vector_dimensions", len(node.vector)))
            dimensions = max(2, min(4, dimensions))
            return [float(component) for component in list(node.vector)[:dimensions]]
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _coerce_constant_value(value: Any, expected: str):
    """Coerce a constant to the expected type."""
    if expected == 'float':
        if isinstance(value, (list, tuple)):
            # Only colour nodes hand a scalar input a list here (a vector
            # constant is reduced by the resolver, which knows its type):
            # Blender's colour-to-float conversion is linear RGB to gray.
            if len(value) >= 3:
                return float(sum(float(c) * w for c, w in zip(value, _RGB_TO_GRAY)))
            return float(value[0]) if value else 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(value, (list, tuple)):
        if len(value) >= 3:
            return [float(value[0]), float(value[1]), float(value[2])]
        if len(value) == 1:
            return [float(value[0])] * 3
    try:
        return [float(value)] * 3
    except (TypeError, ValueError):
        return [1.0, 1.0, 1.0]


#: Blender reports ``Image.depth`` as total bits per pixel across the file's
#: channels. ``Image.channels`` is the internal pixel-buffer width and is
#: almost always 4, so it cannot answer whether the file carries alpha.
#: Depths that two different layouts share (16 is 16-bit gray or 8-bit
#: gray+alpha; 32 is 8-bit RGBA or one float channel) are left out and
#: resolved from the file header instead.
_IMAGE_DEPTH_CHANNELS = {
    8: 1,
    24: 3,
    48: 3,
    96: 3,
    64: 4,
    128: 4,
}


def _image_source_channels(image) -> Optional[int]:
    """Return the source file's channel count, or None when unknown."""
    if not image:
        return None
    try:
        depth = int(getattr(image, "depth", 0) or 0)
    except (TypeError, ValueError):
        return None
    return _IMAGE_DEPTH_CHANNELS.get(depth)


def _image_source_alpha(image, texture_path: Optional[str]) -> Dict[str, Any]:
    """Describe the source file's channel count and usable alpha.

    Reader selection never depends on this; only a consumer asking for the
    alpha channel does. When the count cannot be established the result is
    empty and the alpha read stays permitted.
    """
    channels = _image_source_channels(image)
    if channels is None and texture_path:
        channels = _file_channel_count(texture_path)
    if channels is None:
        return {}
    alpha_mode = str(getattr(image, "alpha_mode", "") or "").upper()
    return {
        "source_channels": channels,
        # Blender's alpha_mode NONE means "ignore the file's alpha and treat
        # the image as opaque", so it removes the channel from play.
        "source_has_alpha": channels in (2, 4) and alpha_mode != "NONE",
    }


def _file_channel_count(path: str) -> Optional[int]:
    """Read a channel count from an image file header, best effort."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(32)
    except OSError:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 26:
        # IHDR colour type: 0 gray, 2 RGB, 4 gray+alpha, 6 RGBA. Type 3
        # (palette) can carry alpha in a tRNS chunk, so it stays unknown.
        return {0: 1, 2: 3, 4: 2, 6: 4}.get(header[25])
    return None


def _resolve_image_path(image) -> Optional[str]:
    """Resolve a Blender image path to an absolute filesystem path."""
    if not image:
        return None

    filepath = image.filepath or image.filepath_raw or ""
    is_dirty = bool(getattr(image, "is_dirty", False))
    packed = getattr(image, "packed_file", None)
    source = (getattr(image, "source", "") or "").upper()

    if is_dirty and source in {"TILED", "SEQUENCE", "MOVIE"}:
        raise ValueError(
            f"Dirty {source.lower()} image '{getattr(image, 'name', 'Image')}' must be "
            "baked to a single current frame before RealityKit export"
        )

    try:
        import bpy
        if filepath:
            filepath = bpy.path.abspath(filepath)
    except Exception:
        pass

    if filepath:
        try:
            filepath = str(Path(filepath).resolve())
        except Exception:
            filepath = os.path.normpath(filepath)

    # A Blender datablock pointer alone is not an image identity. Blender may
    # reuse it after a reload, and a dirty image can become a clean file-backed
    # image without changing the pointer. Include the resolved source state so
    # only an exact image/file version can reuse a staged snapshot.
    cache_key = _image_cache_key(image, filepath)
    cached_path = _STAGED_IMAGE_CACHE.get(cache_key)
    if (
        cached_path
        and not is_dirty
        and packed is None
        and source != "GENERATED"
        and _is_path_on_disk(cached_path)
    ):
        return cached_path

    # Current Blender pixels and packed bytes are authoritative. Never return
    # an external file first for dirty/packed/generated images.
    if is_dirty or packed is not None or source == "GENERATED":
        return _stage_image_to_temp(
            image,
            filepath,
            force_refresh=is_dirty,
            refresh_packed=packed is not None and not is_dirty,
        )

    if filepath and _is_path_on_disk(filepath) and not _is_temp_path(filepath):
        return filepath

    # Fallback: stage packed or generated images to a temp directory.
    return _stage_image_to_temp(image, filepath)


def _is_path_on_disk(path: str) -> bool:
    """Return True if the path exists on disk."""
    try:
        return Path(path).is_file()
    except Exception:
        return False


def _is_temp_path(path: str) -> bool:
    """Return True if the path points into a temporary directory."""
    lowered = path.replace("\\", "/").lower()
    if "usd_textures_tmp" in lowered:
        return True

    temp_root = Path(tempfile.gettempdir())
    try:
        return Path(path).resolve().is_relative_to(temp_root.resolve())
    except Exception:
        return lowered.startswith(str(temp_root).replace("\\", "/").lower())


def _stage_image_to_temp(
    image,
    filepath: Optional[str],
    force_refresh: bool = False,
    refresh_packed: bool = False,
) -> Optional[str]:
    """Stage image data to a temp file so it can be copied into the export."""
    cache_key = _image_cache_key(image, filepath)
    if not force_refresh and not refresh_packed and cache_key in _STAGED_IMAGE_CACHE:
        cached_path = _STAGED_IMAGE_CACHE[cache_key]
        if _is_path_on_disk(cached_path):
            return cached_path
        _STAGED_IMAGE_CACHE.pop(cache_key, None)

    staging_dir = _get_staging_dir()
    source = (getattr(image, "source", "") or "").upper()
    if force_refresh or source == "GENERATED":
        extension = ".exr" if bool(getattr(image, "is_float", False)) else ".png"
    else:
        extension = _guess_image_extension(image, filepath)
    basename = _sanitize_texture_name(Path(filepath).stem if filepath else image.name)
    destination_key = _image_destination_key(image, filepath)
    digest = hashlib.sha256(repr(destination_key).encode("utf-8")).hexdigest()[:12]
    dest_path = staging_dir / f"{basename}_{digest}{extension}"

    if force_refresh:
        if _save_current_image_snapshot_to_path(image, dest_path):
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        raise ValueError(
            f"Unable to snapshot current pixels for dirty image "
            f"'{getattr(image, 'name', 'Image')}'; refusing stale packed or disk bytes"
        )

    if source == "GENERATED":
        if _save_current_image_snapshot_to_path(image, dest_path):
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        raise ValueError(
            f"Unable to snapshot generated image '{getattr(image, 'name', 'Image')}'; "
            "refusing non-current fallback bytes"
        )

    packed = getattr(image, "packed_file", None)
    if packed:
        packed_data = getattr(packed, "data", None)
        if not packed_data:
            raise ValueError(
                f"Packed image '{getattr(image, 'name', 'Image')}' has no readable bytes; "
                "refusing stale external-file fallback"
            )
        try:
            dest_path.write_bytes(packed_data)
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        except Exception as exc:
            raise ValueError(
                f"Unable to stage authoritative packed bytes for image "
                f"'{getattr(image, 'name', 'Image')}'; refusing stale external-file fallback"
            ) from exc

    if filepath and _is_path_on_disk(filepath):
        try:
            shutil.copy2(filepath, dest_path)
            _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
            return str(dest_path)
        except Exception:
            pass

    if _save_image_to_path(image, dest_path):
        _STAGED_IMAGE_CACHE[cache_key] = str(dest_path)
        return str(dest_path)

    return None


def _save_current_image_snapshot_to_path(image, dest_path: Path) -> bool:
    """Save current in-memory pixels without mutating the source datablock."""
    try:
        import bpy

        width, height = (int(value) for value in image.size[:2])
        if width <= 0 or height <= 0:
            return False
        source_pixels = getattr(image, "pixels", None)
        if source_pixels is None:
            return False

        values = array('f', [0.0]) * len(source_pixels)
        source_pixels.foreach_get(values)
        snapshot = bpy.data.images.new(
            name=f"__USDStage_{getattr(image, 'name', 'Image')}",
            width=width,
            height=height,
            alpha=True,
            float_buffer=bool(getattr(image, "is_float", False)),
        )
        try:
            # Blender 5.2 can clear a newly assigned pixel buffer when its
            # colorspace is set afterward, even when assigning the same name.
            # Configure metadata before copying the authoritative pixels.
            try:
                snapshot.colorspace_settings.name = image.colorspace_settings.name
            except Exception:
                pass
            try:
                snapshot.alpha_mode = image.alpha_mode
            except Exception:
                pass
            snapshot.pixels.foreach_set(values)
            snapshot.update()
            snapshot.filepath_raw = str(dest_path)
            snapshot.file_format = _EXTENSION_TO_FORMAT.get(dest_path.suffix.lower(), "PNG")
            snapshot.save()
            return dest_path.is_file()
        finally:
            bpy.data.images.remove(snapshot)
    except Exception:
        return False


def _image_cache_key(image, resolved_filepath: Optional[str] = None):
    """Return an export-local key for the current image source state.

    ``Image.as_pointer()`` identifies a live datablock, not its pixels. The
    same pointer survives dirty-to-clean transitions and reloads, and may be
    reused after a datablock is removed. File identity and mutable Blender
    source state therefore participate in the key as well.
    """
    pointer = None
    if hasattr(image, "as_pointer"):
        try:
            pointer = int(image.as_pointer())
        except Exception:
            pass
    if pointer is None:
        pointer = id(image)

    filepath = str(
        resolved_filepath
        or getattr(image, "filepath", "")
        or getattr(image, "filepath_raw", "")
        or ""
    )
    file_state = _file_state_fingerprint(filepath)
    size = getattr(image, "size", ()) or ()
    try:
        dimensions = tuple(int(value) for value in size[:2])
    except Exception:
        dimensions = ()
    packed = getattr(image, "packed_file", None)
    library = getattr(image, "library", None)
    library_path = str(getattr(library, "filepath", "") or "")

    return (
        pointer,
        str(getattr(image, "name", "") or ""),
        str(getattr(image, "source", "") or "").upper(),
        bool(getattr(image, "is_dirty", False)),
        bool(packed is not None),
        str(getattr(image, "filepath_raw", "") or ""),
        filepath,
        file_state,
        dimensions,
        bool(getattr(image, "is_float", False)),
        str(getattr(image, "file_format", "") or ""),
        library_path,
    )


def _file_state_fingerprint(filepath: str):
    """Return enough filesystem identity to invalidate a reloaded source."""
    if not filepath:
        return None
    try:
        stat_result = Path(filepath).stat()
    except (OSError, ValueError):
        return None
    return (
        int(getattr(stat_result, "st_dev", 0)),
        int(getattr(stat_result, "st_ino", 0)),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def _image_destination_key(image, resolved_filepath: Optional[str]):
    """Return a stable filename identity separate from cache invalidation.

    Pointer and file-stat state belong in the cache key, but putting them in a
    staged filename would make otherwise identical exports nondeterministic.
    A cache miss always rewrites this file, so a stable image/source identity
    remains safe across reloads and dirty-state transitions.
    """
    size = getattr(image, "size", ()) or ()
    try:
        dimensions = tuple(int(value) for value in size[:2])
    except Exception:
        dimensions = ()
    library = getattr(image, "library", None)
    return (
        str(getattr(image, "name", "") or ""),
        str(getattr(image, "source", "") or "").upper(),
        str(getattr(image, "filepath_raw", "") or resolved_filepath or ""),
        str(getattr(library, "filepath", "") or ""),
        dimensions,
        bool(getattr(image, "is_float", False)),
        str(getattr(image, "file_format", "") or ""),
    )


def begin_image_staging_session(diagnostics=None) -> Path:
    """Start a unique image-staging session for one export.

    Any abandoned previous session is removed first. A unique directory avoids
    reusing files left by an earlier export in the same long-lived Blender
    process, while clearing the cache prevents datablock-pointer reuse.
    """
    global _STAGED_IMAGE_DIR, _STAGED_IMAGE_DIR_OWNED
    cleanup_image_staging_session(diagnostics)
    base_dir = Path(tempfile.gettempdir()) / "usdstage_textures"
    base_dir.mkdir(parents=True, exist_ok=True)
    _STAGED_IMAGE_DIR = Path(
        tempfile.mkdtemp(prefix=f"export_{os.getpid()}_", dir=str(base_dir))
    )
    _STAGED_IMAGE_DIR_OWNED = True
    _STAGED_IMAGE_CACHE.clear()
    return _STAGED_IMAGE_DIR


def cleanup_image_staging_session(diagnostics=None) -> bool:
    """Clear image cache state and remove the current owned temp directory."""
    global _STAGED_IMAGE_DIR, _STAGED_IMAGE_DIR_OWNED
    staging_dir = _STAGED_IMAGE_DIR
    owned = _STAGED_IMAGE_DIR_OWNED
    _STAGED_IMAGE_CACHE.clear()
    _STAGED_IMAGE_DIR = None
    _STAGED_IMAGE_DIR_OWNED = False

    if staging_dir is None or not owned:
        return True
    try:
        shutil.rmtree(staging_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if diagnostics and hasattr(diagnostics, "add_warning"):
            diagnostics.add_warning(
                f"Failed to remove temporary image staging directory "
                f"'{staging_dir}': {exc}"
            )
        return False

    try:
        staging_dir.parent.rmdir()
    except OSError:
        pass
    return not staging_dir.exists()


def _get_staging_dir() -> Path:
    """Return the staging directory for temporary textures."""
    if _STAGED_IMAGE_DIR is None:
        return begin_image_staging_session()
    _STAGED_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    return _STAGED_IMAGE_DIR


def _sanitize_texture_name(name: str) -> str:
    """Sanitize a filename stem for staging."""
    if not name:
        return "image"
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    if sanitized[0].isdigit():
        sanitized = f"img_{sanitized}"
    return sanitized


def _guess_image_extension(image, filepath: Optional[str]) -> str:
    """Return a best-effort extension for the image."""
    if filepath:
        ext = Path(filepath).suffix.lower()
        if ext in _EXTENSION_TO_FORMAT:
            return ext
    fmt = getattr(image, "file_format", None)
    if fmt:
        return _FORMAT_TO_EXTENSION.get(str(fmt).upper(), ".png")
    return ".png"


def _save_image_to_path(image, dest_path: Path) -> bool:
    """Attempt to save an image to disk without mutating the original path."""
    try:
        orig_path = image.filepath_raw
        orig_format = image.file_format
    except Exception:
        orig_path = None
        orig_format = None

    try:
        fmt = _EXTENSION_TO_FORMAT.get(dest_path.suffix.lower(), "PNG")
        image.filepath_raw = str(dest_path)
        if fmt:
            image.file_format = fmt
        image.save()
        return dest_path.exists()
    except Exception:
        try:
            image.save_render(str(dest_path))
            return dest_path.exists()
        except Exception:
            return False
    finally:
        try:
            if orig_path is not None:
                image.filepath_raw = orig_path
            if orig_format is not None:
                image.file_format = orig_format
        except Exception:
            pass


# Sections kept in their own modules, re-exported so callers and tests see one
# namespace. Imported last: those modules read this one's names at call time.
from .drivers import (  # noqa: E402,F401
    DRIVEN_INPUT_NODE_TYPES,
    _DRIVER_BINARY,
    _DRIVER_FUNCTIONS,
    _DriverUnsupported,
    _ast,
    _driven_socket_expr,
    _driver_tree,
    _frame_expr,
    _socket_is_live,
    driver_expression_expr,
    driver_refusal_reason,
    scene_fps,
    socket_driver,
)
from .hair import (  # noqa: E402,F401
    HAIR_LOBE_WEIGHT_SCALE,
    HAIR_STRAND_TANGENT,
    _HAIR_MAPPED_SOCKETS,
    _HAIR_NODE_TYPES,
    _HAIR_PARAMETRIZATION_LABELS,
    _HAIR_UNMAPPED_SOCKETS,
    _enabled_hair_socket,
    _extract_hair_material_data,
    hair_issue,
    hair_notices,
    hair_refusal,
    hair_surface_node,
)
from .closures import (  # noqa: E402,F401
    PRINCIPLED_PRESET_TYPES,
    PrincipledView,
    _CLOSURE_NODE_TYPES,
    _ConstantSocket,
    _DerivedLink,
    _DerivedNode,
    _DerivedSocket,
    _LINEAR_MIX_INPUTS,
    _PRINCIPLED_DEFAULTS,
    _RenamedSocket,
    _SHADER_LABELS,
    _StandInInputs,
    _apply_surface_closure,
    _mix_socket,
    _preset_sqrt_socket,
    _principled_constant,
    _principled_like,
    _same_node,
    _saturate_float,
    _shader_label,
    _shader_source,
    _socket_operand,
    _sockets_agree,
    _value_type,
    _values_differ_loose,
    closure_issue,
    principled_like_issue,
    principled_mix,
    principled_notices,
    principled_preset,
    principled_views,
    resolve_surface_closure,
)
from .displacement import (  # noqa: E402,F401
    _COLOR_SHADER_SOCKETS,
    _DISPLACEMENT_NODE_TYPES,
    _active_output_node,
    _fold_float,
    _vertex_offset_expr,
    displacement_notices,
    displacement_refusal,
    displacement_source,
    extract_vertex_offset,
    srgb_data_image_notices,
    surface_output_refusal,
)
from .readers import (  # noqa: E402,F401
    EXPORTED_WORKING_SPACE_INTEROP_ID,
    OBJECT_RANDOM_PRIMVAR,
    _ATTRIBUTE_COLOR_TYPES,
    _ATTRIBUTE_PRIMVAR_READS,
    _ATTRIBUTE_READABLE_DOMAINS,
    _READER_NODE_TYPES,
    _SINGLE_CHANNELS,
    _VECTOR_MATH_COMPOSED_OPS,
    _VECTOR_MATH_REFUSED_OPS,
    _VECTOR_MATH_SINGLE_INPUT_OPS,
    _VECTOR_MATH_TWO_INPUT_OPS,
    _VECTOR_MATH_VALUE_OUTPUT_OPS,
    _VECTOR_ROTATE_AXES,
    _alpha_output_linked,
    _attribute_description,
    _attribute_node_expr,
    _box_projection_expr,
    _broadcast_color3,
    _camera_data_expr,
    _checker_expr,
    _coerce_output,
    _color3_node,
    _color3_operand_expr,
    _component_of,
    _computed_image_expr,
    _constant_value,
    _cos_view_normal_expr,
    _environment_texture_expr,
    _eta_by_facing_expr,
    _euler_xyz_rotate,
    _float_node,
    _float_socket_expr,
    _float_to_expected,
    _fresnel_dielectric_cos_expr,
    _fresnel_node_expr,
    _front_facing_float_expr,
    _gamma_expr,
    _geometry_reader_expr,
    _image_coordinate_expr,
    _image_file_spec,
    _image_read_refusal,
    _layer_weight_expr,
    _mapping_vector_expr,
    _material_meshes,
    _named_color_operand,
    _named_float_operand,
    _named_socket,
    _named_vector_operand,
    _negated_float,
    _node_expr_output_type,
    _object_info_expr,
    _per_component_vector3,
    _rotate_about_unit_axis,
    _safe_divide_vector,
    _safe_normalize,
    _sample_color,
    _sample_image,
    _srgb_decode_color3,
    _texture_coordinate_expr,
    _texture_is_scalar,
    _typed_result,
    _vector3_node,
    _vector3_operand_expr,
    _vector_dot,
    _vector_math_expr,
    _vector_math_operand,
    _vector_math_scalar_operand,
    _vector_rotate_expr,
    _vector_socket_expr,
    _vector_times_float,
    _view_direction_expr,
    _world_reader_expr,
    blender_world_vector,
    cycles_object_random,
    image_premultiplies_color,
    image_uses_uv_transform,
    reader_refusal,
    working_color_space_refusal,
)
