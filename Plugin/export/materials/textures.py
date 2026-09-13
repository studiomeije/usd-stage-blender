"""
Texture authoring helpers for MaterialX USD graphs.
"""

import math
from typing import Any, Dict, Optional, Tuple

from ..usd_utils import Gf, Sdf, UsdShade
from ...manifest.materialx_nodes import (
    select_nodedef_name_for_node,
)
from .conversions import _map_mtlx_type_to_sdf, _create_convert_output
from .helpers import _image_shader_name, _sanitize_name
from .mapping import effective_texture_mapping_contract


def _texture_cache_key(texture_spec: Dict[str, Any]) -> Tuple[Any, ...]:
    """Build a stable cache key for shared texture nodes."""
    mapping = texture_spec.get("mapping") or {}
    offset = tuple(mapping.get("offset") or (0.0, 0.0))
    scale = tuple(mapping.get("scale") or (1.0, 1.0))
    rotate = float(mapping.get("rotate") or 0.0)
    pivot = tuple(mapping.get("pivot") or (0.0, 0.0))
    operationorder = mapping.get("operationorder")

    return (
        texture_spec.get("path"),
        texture_spec.get("texcoord"),
        offset,
        scale,
        rotate,
        pivot,
        operationorder,
        texture_spec.get("colorspace"),
        texture_spec.get("colorspace_role"),
        texture_spec.get("alpha_mode"),
        texture_spec.get("type"),
        # Neither the requested output type nor the requested channel belongs
        # here. One file read for two channels is one reader with two
        # component reads hanging off it, which is what every working RealityKit
        # package authors; keying on them produced one duplicate reader prim
        # per consumed channel.
        texture_spec.get("image_type_override"),
        bool(texture_spec.get("force_separate4")),
        tuple(sorted((texture_spec.get("sampling") or {}).items())),
    )


def texture_source_lacks_alpha(texture_spec: Dict[str, Any]) -> bool:
    """Return True only when the source file is known to carry no alpha.

    Reader selection follows what the consumer needs. The one exception is a
    consumer asking for the alpha channel: a four-channel read of a
    three-channel file is what produced the reported placeholder material, so
    an alpha request over a file measured as having no alpha is refused. An
    unknown channel count stays permissive.
    """
    has_alpha = texture_spec.get("source_has_alpha")
    if has_alpha is not None:
        return not has_alpha
    channels = texture_spec.get("source_channels")
    if isinstance(channels, int) and channels > 0:
        return channels < 4
    return False


def _coerce_texture_spec_for_input(
    texture_spec: Dict[str, Any],
    input_def: Optional[Dict[str, Any]],
    diagnostics=None,
) -> Dict[str, Any]:
    """Coerce texture output hints to match the expected input type."""
    if not input_def:
        return texture_spec
    type_name = (input_def.get('type') or '').lower()
    if not type_name:
        return texture_spec
    output_type = (texture_spec.get('output_type') or '').lower()
    if not output_type:
        return texture_spec

    coerced = dict(texture_spec)
    if type_name in ('color3', 'vector3', 'half3') and output_type in ('color4', 'vector4'):
        coerced['output_type'] = 'color3' if type_name in ('color3', 'half3') else 'vector3'
    elif type_name in ('color4', 'half4') and output_type == 'color3':
        coerced['output_type'] = 'color4'
    elif type_name in ('vector2', 'half2') and output_type in ('vector4',):
        coerced['output_type'] = 'vector2'
    elif type_name in ('float', 'integer', 'half') and output_type in ('color3', 'color4', 'vector3', 'vector4'):
        # A channelless read keeps no channel: the colour-to-float conversion
        # (Blender's linear RGB to gray) is authored when the output resolves.
        coerced['output_type'] = 'float'

    if diagnostics and coerced != texture_spec:
        diagnostics.add_warning(
            f"Coerced texture output '{output_type}' to '{coerced.get('output_type')}' "
            f"for input type '{type_name}'."
        )
    return coerced


def _create_texture_connection(
    stage,
    nodegraph_path: str,
    input_name: str,
    texture_spec: Dict[str, Any],
    manifest: Dict[str, Any],
    material_name: str,
    texture_cache: Optional[Dict[Any, Dict[str, Any]]] = None,
    diagnostics=None,
    mapping_cache: Optional[Dict[Any, Any]] = None,
):
    """Create texture nodes and return the output to connect."""
    texture_path = texture_spec.get('path')
    if not texture_path:
        return None

    output_type = texture_spec.get('output_type', 'color3')
    desired_type = (output_type or 'color3').lower()
    channel = (texture_spec.get('channel') or '').lower()
    texture_kind = texture_spec.get('type') or 'texture'

    if channel == 'a' and texture_source_lacks_alpha(texture_spec):
        # A four-channel read of a three-channel file. Leave the input at its
        # authored default (opacity defaults to fully opaque) instead of
        # sampling a channel the file does not carry.
        if diagnostics:
            diagnostics.add_warning(
                f"Texture '{texture_path}' has no alpha channel; input "
                f"'{input_name}' keeps its default instead of reading alpha."
            )
        return None

    node_base = _image_shader_name(stage, nodegraph_path, input_name)

    cache_key = _texture_cache_key(texture_spec)
    if texture_cache is not None:
        cached = texture_cache.get(cache_key)
        if cached:
            texture_output = cached.get("output")
            current_type = cached.get("type") or desired_type
            if texture_output:
                return _postprocess_texture_output(
                    manifest,
                    stage,
                    nodegraph_path,
                    input_name,
                    texture_output,
                    current_type,
                    desired_type,
                    channel,
                    texture_kind,
                    texture_spec,
                    diagnostics,
                )

    image_type = _image_output_hint(
        desired_type,
        channel,
        texture_kind,
        texture_spec.get("colorspace_role"),
    )
    override_type = texture_spec.get("image_type_override")
    if override_type:
        image_type = override_type
    image_type = _IMAGE_OUTPUT_SUBSTITUTIONS.get(image_type, image_type)
    nodedef_name, output_sdf_type = _image_nodedef_for_output(manifest, image_type)
    current_type = image_type

    texture_prim = stage.DefinePrim(f"{nodegraph_path}/{node_base}", "Shader")
    texture_shader = UsdShade.Shader(texture_prim)
    texture_shader.CreateIdAttr(nodedef_name)

    file_input = texture_shader.CreateInput("file", Sdf.ValueTypeNames.Asset)
    file_input.Set(texture_path)
    file_colorspace = _materialx_file_colorspace(texture_spec, input_name, diagnostics)
    if file_colorspace:
        file_input.GetAttr().SetColorSpace(file_colorspace)

    if texture_kind == 'normal_texture' and current_type == 'vector3':
        # ND_image_vector3 declares a (0, 0, 0) default. If the texture ever
        # fails to resolve, the decoder turns that into (-1, -1, -1) and any
        # downstream normalize produces garbage. Author a flat tangent normal
        # instead, which is what Apple's own normal readers do.
        texture_shader.CreateInput(
            "default", Sdf.ValueTypeNames.Float3
        ).Set((0.5, 0.5, 1.0))

    # Non-default sampler modes from Blender's Image Texture node. The shipped
    # RCP 3 ND_image_* nodedefs declare these uniform string inputs
    # (uaddressmode/vaddressmode: constant, clamp, periodic, mirror;
    # filtertype: closest, linear, cubic) and wire them into Metal samplers;
    # leaving them unauthored means periodic + linear regardless of what the
    # artist chose in Blender.
    for sampler_input, sampler_value in sorted(
        (texture_spec.get("sampling") or {}).items()
    ):
        texture_shader.CreateInput(
            sampler_input, Sdf.ValueTypeNames.String
        ).Set(str(sampler_value))

    texcoord_name = texture_spec.get('texcoord')
    raw_mapping = texture_spec.get('mapping')
    mapping_contract = effective_texture_mapping_contract(
        raw_mapping,
        texcoord_name,
    )
    texcoord_output = None
    place2d_output = (
        mapping_cache.get(mapping_contract)
        if mapping_cache is not None and mapping_contract is not None
        else None
    )
    if place2d_output is None:
        if texcoord_name:
            texcoord_output = _create_geomprop_texcoord(
                manifest,
                stage,
                nodegraph_path,
                input_name,
                texcoord_name,
                diagnostics,
            )
        elif raw_mapping:
            # An explicit identity Mapping node still denotes Blender's default
            # UV set, but needs no RealityKit-limited transform node.
            texcoord_output = _create_geomprop_texcoord(
                manifest,
                stage,
                nodegraph_path,
                input_name,
                "UV0",
                diagnostics,
            )

    if mapping_contract is not None:
        if place2d_output is None:
            place2d_output = _create_place2d_node(
                manifest,
                stage,
                nodegraph_path,
                input_name,
                raw_mapping,
                texcoord_output,
                diagnostics,
            )
            if mapping_cache is not None and place2d_output:
                mapping_cache[mapping_contract] = place2d_output
        if place2d_output:
            texcoord_input = texture_shader.CreateInput("texcoord", Sdf.ValueTypeNames.Float2)
            texcoord_input.ConnectToSource(place2d_output)
    elif texcoord_output:
        texcoord_input = texture_shader.CreateInput("texcoord", Sdf.ValueTypeNames.Float2)
        texcoord_input.ConnectToSource(texcoord_output)

    texture_output = texture_shader.CreateOutput("out", output_sdf_type)

    if texture_cache is not None:
        texture_cache[cache_key] = {"output": texture_output, "type": current_type}

    return _postprocess_texture_output(
        manifest,
        stage,
        nodegraph_path,
        input_name,
        texture_output,
        current_type,
        desired_type,
        channel,
        texture_kind,
        texture_spec,
        diagnostics,
    )


def _postprocess_texture_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    texture_output,
    current_type: str,
    desired_type: str,
    channel: str,
    texture_kind: str,
    texture_spec: Dict[str, Any],
    diagnostics=None,
):
    """Apply per-use decode/swizzle/scale operations to a cached raw reader."""
    if texture_kind == 'normal_texture':
        # RealityKit's dedicated tangent normal decoder is part of the RCP
        # contract. The cache stores only the raw image output, so every use
        # still passes through this decoder.
        if current_type != 'vector3':
            texture_output = _create_convert_output(
                manifest,
                stage,
                nodegraph_path,
                input_name,
                texture_output,
                current_type,
                'vector3',
                diagnostics,
            )
            current_type = 'vector3'

        strength_value = _normal_map_strength_value(texture_spec.get("scale"))
        space = _normal_map_space(texture_spec)
        can_use_realitykit_decode = _can_use_realitykit_normal_map_decode(strength_value, space)

        # ND_normalmap is not a fallback: it does not resolve at MaterialX
        # 1.39, which every export declares, so RealityKit would lose the
        # whole material. Anything the decoder cannot carry fails here, before
        # a prim exists, with the bake remedy.
        if not can_use_realitykit_decode:
            raise ValueError(
                f"Normal Map space {space!r} cannot be represented: RealityKit's "
                "normal decoder takes tangent-space maps only, and no node at "
                "MaterialX 1.39 carries another space. Bake a tangent-space "
                "normal map instead."
            )
        normalmap_nodedef = select_nodedef_name_for_node(
            manifest,
            "normal_map_decode",
            output_type="vector3",
        ) or "ND_normal_map_decode"

        normalmap_name = _sanitize_name(f"NormalMap_{input_name}")
        normalmap_path = f"{nodegraph_path}/{normalmap_name}"
        existing = stage.GetPrimAtPath(normalmap_path)
        if existing and existing.IsValid():
            normalmap_shader = UsdShade.Shader(existing)
        else:
            normalmap_prim = stage.DefinePrim(normalmap_path, "Shader")
            normalmap_shader = UsdShade.Shader(normalmap_prim)
            normalmap_shader.CreateIdAttr(normalmap_nodedef)

        in_input = normalmap_shader.GetInput("in") or normalmap_shader.CreateInput(
            "in", Sdf.ValueTypeNames.Float3
        )
        in_input.ConnectToSource(texture_output)

        decoded = normalmap_shader.GetOutput("out") or normalmap_shader.CreateOutput(
            "out", Sdf.ValueTypeNames.Float3
        )
        return _apply_tangent_space_normal_strength(
            stage, normalmap_path, decoded, strength_value
        )

    texture_output, current_type = _resolve_texture_output(
        manifest,
        stage,
        nodegraph_path,
        input_name,
        texture_output,
        current_type,
        desired_type,
        channel,
        texture_kind,
        bool(texture_spec.get("force_separate4")),
        diagnostics,
    )

    scale = texture_spec.get('scale')
    if scale is not None and abs(float(scale) - 1.0) <= 1e-6:
        scale = None  # identity: authoring a multiply by 1 is pure noise
    if scale is not None and texture_output:
        scaled_output = _create_scale_output(
            manifest,
            stage,
            nodegraph_path,
            input_name,
            texture_output,
            desired_type,
            scale,
        )
        if scaled_output:
            texture_output = scaled_output
    return texture_output


def _materialx_file_colorspace(
    texture_spec: Dict[str, Any],
    input_name: str,
    diagnostics=None,
) -> str:
    """Return a verified MaterialX file color-space token or fail closed."""
    role = (texture_spec.get("colorspace_role") or "").strip().lower()
    source = (texture_spec.get("colorspace") or "").strip().lower()
    aliases = {
        "srgb": "srgb_texture",
        "srgb_texture": "srgb_texture",
        "s-rgb": "srgb_texture",
        "srgb texture": "srgb_texture",
        "raw": "raw",
        "non-color": "raw",
        "non color": "raw",
        "data": "raw",
        "linear rec.709": "lin_rec709",
        "linear rec709": "lin_rec709",
        "lin_rec709": "lin_rec709",
        "scene_linear": "lin_rec709",
    }

    if source.startswith("unsupported:"):
        raise ValueError(
            f"Unsupported Blender image color space '{source.split(':', 1)[1]}' for '{input_name}'"
        )
    normalized = aliases.get(source)
    if source and normalized is None:
        raise ValueError(f"Unsupported Blender image color space '{source}' for '{input_name}'")

    if role == "data":
        channel = (texture_spec.get("channel") or "").strip().lower()
        # Cycles decodes an sRGB image before any node reads it, whatever the
        # input, so a data input fed by one reads decoded values. The reader
        # decodes the same way; validation warns, naming the image
        # (``srgb_data_image_notices``). Alpha is never decoded, and Non-Color
        # and Linear Rec.709 (the working space) need no transform.
        if normalized == "srgb_texture" and channel != "a":
            return "srgb_texture"
        # Author no color space at all. An absent MaterialX color space is the
        # no-transform contract, which is what "raw" meant; the lowercase
        # "raw" token appears in no shipping RealityKit package, and RCP 3.0
        # replaces a material whose reader carries it with the striped
        # placeholder.
        return ""
    if role == "color":
        if normalized == "raw":
            # Blender applies no transfer function to a Non-Color image, so a
            # perceptual color input reads its texels as scene-linear values.
            # MaterialX has no "raw" contract for color, and RealityKit rejects
            # the token outright, so name the pass-through Blender actually
            # performs. Telling the user to retag the image sRGB instead would
            # introduce a decode Blender never applied and shift the render.
            if diagnostics:
                diagnostics.add_warning(
                    f"Non-Color image on perceptual color input '{input_name}' "
                    "exported as lin_rec709 (already-linear scene color)."
                )
            return "lin_rec709"
        return normalized or "srgb_texture"
    return normalized or ""


def _create_geomprop_texcoord(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    texcoord_name: str,
    diagnostics=None,
):
    """Create a texcoord or geompropvalue node for texture coordinates."""
    texcoord_name = (texcoord_name or "").strip() or "UV0"
    if texcoord_name.upper() == "UV0":
        texcoord_nodedef = select_nodedef_name_for_node(
            manifest,
            "texcoord",
            output_type="vector2",
        )
        if not texcoord_nodedef:
            if diagnostics:
                diagnostics.add_warning("No texcoord nodedef found for default UVs.")
                diagnostics.add_error("No texcoord nodedef found for default UVs.")
            return None
        texcoord_node_name = _sanitize_name("TextureCoordinates")
        texcoord_path = f"{nodegraph_path}/{texcoord_node_name}"
        existing = stage.GetPrimAtPath(texcoord_path)
        if existing and existing.IsValid():
            existing_shader = UsdShade.Shader(existing)
            output = existing_shader.GetOutput("out")
            return output or existing_shader.CreateOutput("out", Sdf.ValueTypeNames.Float2)
        texcoord_prim = stage.DefinePrim(texcoord_path, "Shader")
        texcoord_shader = UsdShade.Shader(texcoord_prim)
        texcoord_shader.CreateIdAttr(texcoord_nodedef)
        return texcoord_shader.CreateOutput("out", Sdf.ValueTypeNames.Float2)

    texcoord_nodedef = select_nodedef_name_for_node(
        manifest,
        "geompropvalue",
        output_type="vector2",
    )
    if not texcoord_nodedef:
        if diagnostics:
            diagnostics.add_warning(
                f"No geompropvalue nodedef found for texcoord '{texcoord_name}'"
            )
            diagnostics.add_error(
                f"No geompropvalue nodedef found for texcoord '{texcoord_name}'"
            )
        return None

    texcoord_node_name = _sanitize_name(f"texcoord_{texcoord_name}")
    texcoord_path = f"{nodegraph_path}/{texcoord_node_name}"
    existing = stage.GetPrimAtPath(texcoord_path)
    if existing and existing.IsValid():
        existing_shader = UsdShade.Shader(existing)
        output = existing_shader.GetOutput("out")
        return output or existing_shader.CreateOutput("out", Sdf.ValueTypeNames.Float2)
    texcoord_prim = stage.DefinePrim(texcoord_path, "Shader")
    texcoord_shader = UsdShade.Shader(texcoord_prim)
    texcoord_shader.CreateIdAttr(texcoord_nodedef)

    geomprop_input = texcoord_shader.CreateInput("geomprop", Sdf.ValueTypeNames.String)
    geomprop_input.Set(texcoord_name)
    return texcoord_shader.CreateOutput("out", Sdf.ValueTypeNames.Float2)


def _create_place2d_node(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    mapping: Dict[str, Any],
    texcoord_output,
    diagnostics=None,
):
    """Create a place2d node to apply mapping transforms."""
    nodedef_name = select_nodedef_name_for_node(
        manifest,
        "place2d",
        output_type="vector2",
    )
    if not nodedef_name:
        if diagnostics:
            diagnostics.add_warning("No place2d nodedef found for UV mapping transforms.")
            diagnostics.add_error("No place2d nodedef found for UV mapping transforms.")
        return None

    node_name = _sanitize_name(f"place2d_{input_name}")
    place_prim = stage.DefinePrim(f"{nodegraph_path}/{node_name}", "Shader")
    place_shader = UsdShade.Shader(place_prim)
    place_shader.CreateIdAttr(nodedef_name)

    if texcoord_output:
        texcoord_input = place_shader.CreateInput("texcoord", Sdf.ValueTypeNames.Float2)
        texcoord_input.ConnectToSource(texcoord_output)

    offset = mapping.get('offset') or (0.0, 0.0)
    scale = mapping.get('scale') or (1.0, 1.0)
    pivot = mapping.get('pivot') or (0.0, 0.0)
    rotate = mapping.get('rotate') or 0.0
    rotate_degrees = math.degrees(rotate)

    place_shader.CreateInput("offset", Sdf.ValueTypeNames.Float2).Set(offset)
    place_shader.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(scale)
    place_shader.CreateInput("pivot", Sdf.ValueTypeNames.Float2).Set(pivot)
    place_shader.CreateInput("rotate", Sdf.ValueTypeNames.Float).Set(rotate_degrees)
    # `operationorder` is left at its default, which reproduces Blender's
    # fixed-order Mapping node (the t10_texture_transform import shows it).
    # RealityKit's 1.38 nodedef store has no such input, and its compiler drops
    # a material's entire shader graph on an undeclared input; see
    # `_shipped_nodedef_inputs` in realitykit_preflight for the general gate.

    return place_shader.CreateOutput("out", Sdf.ValueTypeNames.Float2)


def _create_scale_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    source_output,
    output_type: str,
    scale: float,
):
    """Multiply a texture output by a scalar scale factor."""
    nodedef_name = select_nodedef_name_for_node(
        manifest,
        "multiply",
        output_type=output_type,
    )
    if not nodedef_name:
        return None

    if output_type == 'color4':
        value = (scale, scale, scale, scale)
    elif output_type == 'color3':
        value = (scale, scale, scale)
    elif output_type == 'vector2':
        value = (scale, scale)
    elif output_type == 'vector3':
        value = (scale, scale, scale)
    elif output_type == 'vector4':
        value = (scale, scale, scale, scale)
    else:
        value = float(scale)

    # The scale is a literal, so it goes straight onto the multiply's second
    # input; an ND_constant_* node would add a prim and nothing else.

    mult_name = _sanitize_name(f"scale_mult_{input_name}")
    mult_prim = stage.DefinePrim(f"{nodegraph_path}/{mult_name}", "Shader")
    mult_shader = UsdShade.Shader(mult_prim)
    mult_shader.CreateIdAttr(nodedef_name)

    mult_in1 = mult_shader.CreateInput("in1", _map_mtlx_type_to_sdf(output_type))
    mult_in1.ConnectToSource(source_output)
    mult_shader.CreateInput("in2", _map_mtlx_type_to_sdf(output_type)).Set(value)
    return mult_shader.CreateOutput("out", _map_mtlx_type_to_sdf(output_type))


#: ``ND_image_vector4`` is in the manifest but Reality Composer Pro 3.0
#: does not instantiate it: the material is replaced by the
#: striped placeholder. A four-channel read is authored as ``ND_image_color4``,
#: which is what every working RealityKit package uses.
_IMAGE_OUTPUT_SUBSTITUTIONS = {'vector4': 'color4'}


def _image_nodedef_for_output(manifest: Dict[str, Any], output_type: str) -> Tuple[str, Any]:
    """Pick MaterialX image nodedef and output type from requested output."""
    output_type = (output_type or '').lower()
    output_type = _IMAGE_OUTPUT_SUBSTITUTIONS.get(output_type, output_type)
    color4_type = getattr(Sdf.ValueTypeNames, "Color4f", Sdf.ValueTypeNames.Float4)
    mapping = {
        'float': ("ND_image_float", Sdf.ValueTypeNames.Float),
        'color3': ("ND_image_color3", Sdf.ValueTypeNames.Color3f),
        'color4': ("ND_image_color4", color4_type),
        'vector2': ("ND_image_vector2", Sdf.ValueTypeNames.Float2),
        'vector3': ("ND_image_vector3", Sdf.ValueTypeNames.Float3),
    }
    nodedef_name = select_nodedef_name_for_node(
        manifest,
        "image",
        output_type=output_type,
    )
    if nodedef_name:
        # If found via manifest, infer output type from the nodedef.
        node_def = manifest.get("nodes", {}).get(nodedef_name)
        if node_def and node_def.get("outputs"):
            out_type = (node_def["outputs"][0].get("type") or "").lower()
            return nodedef_name, _map_mtlx_type_to_sdf(out_type) or mapping.get(output_type, (None, None))[1]
        return nodedef_name, mapping.get(output_type, ("ND_image_color3", Sdf.ValueTypeNames.Color3f))[1]
    return mapping.get(output_type, ("ND_image_color3", Sdf.ValueTypeNames.Color3f))


def _image_output_hint(
    output_type: str,
    channel: str,
    texture_kind: Optional[str],
    colorspace_role: Optional[str] = None,
) -> str:
    """Choose a safe image output type for the requested connection.

    The reader follows what the consumer needs, never the file's channel
    count. Reality Composer Pro 3.0 cannot instantiate ``ND_image_vector4``,
    so a four-channel read is authored only for a genuine alpha consumer
    (``ND_image_color4``), and packed scalars are pulled out of a
    three-channel reader with a component read.
    """
    output_type = (output_type or '').lower()
    channel = (channel or '').lower()
    # Normal maps must be treated as raw vectors (avoid color-space assumptions).
    if texture_kind == 'normal_texture':
        return 'vector3'
    is_data = (colorspace_role or '').strip().lower() == 'data'
    if output_type in ('color4', 'vector4') or (output_type == 'float' and channel == 'a'):
        return 'color4'
    if is_data:
        if output_type == 'vector2':
            return 'vector2'
        if output_type in ('vector3',):
            return 'vector3'
        # A packed scalar channel is read through the same three-channel
        # reader every working RealityKit package uses; the per-use component
        # read selects r, g, or b. The reader applies no transfer function
        # because no color space is authored on a data file.
        return 'color3'
    if output_type in ('vector3', 'vector2'):
        return 'color3'
    if output_type == 'float' and channel:
        return 'color3'
    if output_type == 'float':
        return 'color3'
    return 'color3'


def _normal_map_strength_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _normal_map_space(texture_spec: Dict[str, Any]) -> str:
    return (texture_spec.get("space") or "").strip().lower()


def _can_use_realitykit_normal_map_decode(strength_value: Optional[float], space: str) -> bool:
    """Whether the plain decode node can carry this Normal Map node.

    Only the *space* decides. ``ND_normalmap`` is no alternative: it returns a
    world/anchor-space normal on both of its branches, while the surface's
    ``normal`` input is tangent-space, and it does not resolve at MaterialX
    1.39. Strength is expressed in tangent space instead - see
    ``_apply_tangent_space_normal_strength`` - and a non-tangent map fails the
    export with bake advice.
    """
    del strength_value
    return space in {"", "tangent"}


def _apply_tangent_space_normal_strength(
    stage, normalmap_path: str, decoded_output, strength_value: Optional[float]
):
    """Express Blender's Normal Map Strength in tangent space.

    Cycles' ``svm_node_normal_map`` scales the decoded colour as
    ``(s x, s y, mix(1, z, saturate(s)))`` before it leaves tangent space.
    Its other form, a world-space lerp toward the shading normal, runs only
    on smooth faces of a mesh with an undisplaced normal, which exists only
    when the material displaces vertices with Base Original; that case is
    refused (``normal_map_refusal``). Measured by Emission bakes on Blender
    5.2 at Strength 2 and 0.35 on smooth and flat faces with either Base:
    every one matches the scaled form, and ``normalize(mix((0, 0, 1), n, s))``
    does not.
    """
    if strength_value is None or abs(strength_value - 1.0) <= 1e-6:
        return decoded_output

    # The Principled path hands a linked Strength to the graph resolver, so
    # this is a compile-time constant.
    strength = float(strength_value)
    saturated = min(max(strength, 0.0), 1.0)

    scale_shader = UsdShade.Shader(stage.DefinePrim(f"{normalmap_path}_strength", "Shader"))
    scale_shader.CreateIdAttr("ND_multiply_vector3")
    scale_shader.CreateInput("in1", Sdf.ValueTypeNames.Float3).ConnectToSource(decoded_output)
    scale_shader.CreateInput("in2", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(strength, strength, saturated))
    scaled = scale_shader.CreateOutput("out", Sdf.ValueTypeNames.Float3)

    lift_shader = UsdShade.Shader(stage.DefinePrim(f"{normalmap_path}_strength_z", "Shader"))
    lift_shader.CreateIdAttr("ND_add_vector3")
    lift_shader.CreateInput("in1", Sdf.ValueTypeNames.Float3).ConnectToSource(scaled)
    lift_shader.CreateInput("in2", Sdf.ValueTypeNames.Float3).Set(Gf.Vec3f(0.0, 0.0, 1.0 - saturated))
    mixed = lift_shader.CreateOutput("out", Sdf.ValueTypeNames.Float3)

    normalize_shader = UsdShade.Shader(
        stage.DefinePrim(f"{normalmap_path}_normalize", "Shader")
    )
    normalize_shader.CreateIdAttr("ND_normalize_vector3")
    normalize_shader.CreateInput("in", Sdf.ValueTypeNames.Float3).ConnectToSource(mixed)
    return normalize_shader.GetOutput("out") or normalize_shader.CreateOutput(
        "out", Sdf.ValueTypeNames.Float3
    )


def _resolve_texture_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    texture_output,
    current_type: str,
    desired_type: str,
    channel: str,
    texture_kind: str,
    force_separate4: bool,
    diagnostics=None,
):
    """Resolve texture output conversions including RGBA separation."""
    if not texture_output:
        return None, current_type

    current_type = (current_type or '').lower()
    desired_type = (desired_type or '').lower()
    channel = (channel or '').lower()

    # A float consumer of a channelless colour read takes Blender's implicit
    # conversion, linear RGB to gray, which the convert below authors; reading
    # one channel is only right when a Separate node or the Alpha output asked
    # for it.

    if texture_kind != 'normal_texture' and current_type == 'color4':
        base_name = texture_output.GetPrim().GetName() if texture_output.GetPrim() else input_name
        base_name = _sanitize_name(base_name or input_name)
        if channel and channel not in ('rgb', 'rgba'):
            separated = _create_separate4_outputs(
                manifest,
                stage,
                nodegraph_path,
                base_name,
                texture_output,
                diagnostics,
            )
            channel_output = _channel_from_separate(separated, channel)
            if channel_output:
                texture_output = channel_output
                current_type = 'float'
                channel = ''
        elif desired_type in ('color3', 'vector3') and force_separate4:
            separated = _create_separate4_outputs(
                manifest,
                stage,
                nodegraph_path,
                base_name,
                texture_output,
                diagnostics,
            )
            color_output = _create_combine3_output(
                manifest,
                stage,
                nodegraph_path,
                base_name,
                separated,
                diagnostics,
            )
            if color_output:
                texture_output = color_output
                current_type = 'color3'

    if channel and channel not in ('rgb', 'rgba'):
        swizzle_output = _create_swizzle_output(
            manifest,
            stage,
            nodegraph_path,
            input_name,
            texture_output,
            channel,
            desired_type,
            diagnostics,
        )
        if swizzle_output:
            texture_output = swizzle_output
            current_type = 'float'

    if current_type != desired_type:
        texture_output = _create_convert_output(
            manifest,
            stage,
            nodegraph_path,
            input_name,
            texture_output,
            current_type,
            desired_type,
            diagnostics,
        )
        current_type = desired_type

    return texture_output, current_type


class _LazyChannelOutputs(dict):
    """Channel -> float output, authored on first access."""

    def __init__(self, factory):
        super().__init__()
        self._factory = factory

    def __bool__(self):
        # Starts empty by design; callers test truthiness to mean "outputs are
        # available", and an empty dict would read as "none".
        return True

    def __missing__(self, channel):
        if channel not in "rgba":
            raise KeyError(channel)
        value = self._factory(channel)
        self[channel] = value
        return value

    def get(self, channel, default=None):
        try:
            return self[channel]
        except KeyError:
            return default


def _create_separate4_outputs(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    base_name: str,
    source_output,
    diagnostics=None,
):
    """Return one float output per channel of a color4 source.

    Not ``ND_separate4_color4``: each channel is a dot product with a unit mask
    over one shared convert, which is the shape Reality Composer Pro itself
    writes for a channel read and the shape the ``.import`` lane authors, so the
    two output formats agree.

    This replaced ``ND_separate4_color4`` on the belief that it shipped no Metal
    implementation. That was wrong - it is expanded from a ``<nodegraph>``, and
    the repository's own checker passes it. The chain below is kept because it
    matches the editor, not because the alternative is broken.
    """
    if not source_output:
        return None

    convert_path = f"{nodegraph_path}/{_sanitize_name(f'{base_name}_vector4')}"
    existing = stage.GetPrimAtPath(convert_path)
    if existing and existing.IsValid():
        convert = UsdShade.Shader(existing)
    else:
        convert = UsdShade.Shader(stage.DefinePrim(convert_path, "Shader"))
        convert.CreateIdAttr("ND_convert_color4_vector4")
        convert.CreateInput("in", source_output.GetTypeName()).ConnectToSource(
            source_output
        )
    vector = convert.GetOutput("out") or convert.CreateOutput(
        "out", Sdf.ValueTypeNames.Float4
    )

    def component(channel: str):
        index = "rgba".index(channel)
        path = f"{nodegraph_path}/{_sanitize_name(f'{base_name}_{channel}')}"
        found = stage.GetPrimAtPath(path)
        if found and found.IsValid():
            shader = UsdShade.Shader(found)
        else:
            shader = UsdShade.Shader(stage.DefinePrim(path, "Shader"))
            shader.CreateIdAttr("ND_dotproduct_vector4")
            shader.CreateInput("in1", Sdf.ValueTypeNames.Float4).ConnectToSource(vector)
            mask = tuple(1.0 if i == index else 0.0 for i in range(4))
            shader.CreateInput("in2", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*mask))
        return shader.GetOutput("out") or shader.CreateOutput(
            "out", Sdf.ValueTypeNames.Float
        )

    # Lazy: an opacity read consumes one channel, and authoring the other three
    # would leave dead prims in every material that reads an alpha.
    return _LazyChannelOutputs(component)


def _create_combine3_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    base_name: str,
    separated_outputs: Optional[Dict[str, Any]],
    diagnostics=None,
):
    """Create a combine3 node from separated outputs."""
    if not separated_outputs:
        return None

    nodedef_name = select_nodedef_name_for_node(
        manifest,
        "combine3",
        output_type="color3",
    )
    if not nodedef_name:
        if diagnostics:
            diagnostics.add_warning("No combine3 nodedef found for color3 outputs.")
            diagnostics.add_error("No combine3 nodedef found for color3 outputs.")
        return None

    node_name = _sanitize_name(f"{base_name}_combine3")
    node_path = f"{nodegraph_path}/{node_name}"
    existing = stage.GetPrimAtPath(node_path)
    if existing and existing.IsValid():
        shader = UsdShade.Shader(existing)
    else:
        prim = stage.DefinePrim(node_path, "Shader")
        shader = UsdShade.Shader(prim)
        shader.CreateIdAttr(nodedef_name)

    in1 = shader.CreateInput("in1", Sdf.ValueTypeNames.Float)
    in2 = shader.CreateInput("in2", Sdf.ValueTypeNames.Float)
    in3 = shader.CreateInput("in3", Sdf.ValueTypeNames.Float)
    if separated_outputs.get("r"):
        in1.ConnectToSource(separated_outputs["r"])
    if separated_outputs.get("g"):
        in2.ConnectToSource(separated_outputs["g"])
    if separated_outputs.get("b"):
        in3.ConnectToSource(separated_outputs["b"])

    return shader.GetOutput("out") or shader.CreateOutput("out", Sdf.ValueTypeNames.Color3f)


def _channel_from_separate(separated_outputs: Optional[Dict[str, Any]], channel: str):
    """Return the output matching the requested channel from a separate4 node."""
    if not separated_outputs or not channel:
        return None
    channel = channel.lower()
    mapping = {
        "r": "r",
        "g": "g",
        "b": "b",
        "a": "a",
        "x": "r",
        "y": "g",
        "z": "b",
        "w": "a",
    }
    key = mapping.get(channel)
    if not key:
        return None
    return separated_outputs.get(key)


#: Channel order of each USD type a texture read can land on.
_CHANNEL_ORDER_FOR_SDF_TYPE = {
    "color3f": "rgb", "float3": "xyz", "half3": "rgb",
    "color4f": "rgba", "float4": "xyzw", "half4": "rgba",
}


def _create_swizzle_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    texture_output,
    channel: str,
    output_type: str,
    diagnostics=None,
):
    """Author a single-channel float read of a texture output."""
    if channel not in ('r', 'g', 'b', 'a', 'x', 'y', 'z', 'w'):
        if diagnostics:
            diagnostics.add_warning(
                f"Unsupported texture channel '{channel}' for input '{input_name}'."
            )
            diagnostics.add_error(
                f"Unsupported texture channel '{channel}' for input '{input_name}'."
            )
        return None

    # The read is authored as convert-to-vector followed by a dot product with
    # a unit mask rather than a MaterialX `swizzle`: the arithmetic is exact,
    # and it is the one component-read form every kit scene has been imported
    # with. (RealityKit does implement swizzle, under renamed
    # ND_appleinternal_swizzle_* symbols.)
    input_sdf_type = texture_output.GetTypeName()
    order = _CHANNEL_ORDER_FOR_SDF_TYPE.get(str(input_sdf_type))
    if not order:
        if diagnostics:
            diagnostics.add_error(
                f"Cannot read channel '{channel}' of '{input_sdf_type}' on "
                f"input '{input_name}'."
            )
        return None
    index = order.find(channel[:1].lower())
    if index < 0:
        # e.g. alpha asked of a three-channel read.
        if diagnostics:
            diagnostics.add_error(
                f"'{input_sdf_type}' has no channel '{channel}' for input "
                f"'{input_name}'."
            )
        return None

    width = len(order)
    vector_type = {3: "vector3", 4: "vector4"}[width]
    source = texture_output
    if str(input_sdf_type).startswith("color"):
        # Named after the source, not the consumer, so N channel reads of one
        # reader share a single convert instead of authoring one each.
        convert_name = _sanitize_name(
            f"convert_{texture_output.GetPrim().GetName()}_{vector_type}"
        )
        convert_prim = stage.DefinePrim(f"{nodegraph_path}/{convert_name}", "Shader")
        convert_shader = UsdShade.Shader(convert_prim)
        convert_shader.CreateIdAttr(f"ND_convert_color{width}_{vector_type}")
        convert_shader.CreateInput("in", input_sdf_type).ConnectToSource(texture_output)
        source = convert_shader.CreateOutput(
            "out", Sdf.ValueTypeNames.Float3 if width == 3 else Sdf.ValueTypeNames.Float4
        )

    dot_name = _sanitize_name(f"channel_{input_name}_{channel}")
    dot_prim = stage.DefinePrim(f"{nodegraph_path}/{dot_name}", "Shader")
    dot_shader = UsdShade.Shader(dot_prim)
    dot_shader.CreateIdAttr(f"ND_dotproduct_{vector_type}")
    vector_sdf = Sdf.ValueTypeNames.Float3 if width == 3 else Sdf.ValueTypeNames.Float4
    dot_shader.CreateInput("in1", vector_sdf).ConnectToSource(source)
    mask = tuple(1.0 if i == index else 0.0 for i in range(width))
    dot_shader.CreateInput("in2", vector_sdf).Set(
        Gf.Vec3f(*mask) if width == 3 else Gf.Vec4f(*mask)
    )

    return dot_shader.CreateOutput("out", Sdf.ValueTypeNames.Float)
