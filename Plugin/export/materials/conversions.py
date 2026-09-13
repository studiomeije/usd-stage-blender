"""
MaterialX/USD type conversions and conversion-node helpers.
"""

from typing import Any, Dict, Optional

from ..usd_utils import Sdf, Gf, UsdShade, Vt
from ...manifest.materialx_nodes import select_nodedef_name_for_node
from .helpers import _convert_shader_name


def get_usd_type(value: Any):
    """Get USD type from Python value."""
    if isinstance(value, bool):
        return Sdf.ValueTypeNames.Bool
    if isinstance(value, int):
        return Sdf.ValueTypeNames.Int
    if isinstance(value, float):
        return Sdf.ValueTypeNames.Float
    if isinstance(value, list):
        if len(value) == 2:
            return Sdf.ValueTypeNames.Float2
        if len(value) == 3:
            return Sdf.ValueTypeNames.Float3
        if len(value) == 4:
            return Sdf.ValueTypeNames.Float4
    return Sdf.ValueTypeNames.Token


def _map_mtlx_type_to_sdf(type_name: Optional[str]):
    """Map MaterialX type strings to Sdf value types."""
    if not type_name:
        return None

    color4_type = getattr(Sdf.ValueTypeNames, "Color4f", Sdf.ValueTypeNames.Float4)
    half_types = {
        'half': 'Half',
        'half2': 'Half2',
        'half3': 'Half3',
        'half4': 'Half4',
    }
    if type_name in half_types:
        sdf_type = getattr(Sdf.ValueTypeNames, half_types[type_name], None)
        if sdf_type is None:
            raise RuntimeError(
                f"OpenUSD lacks required {half_types[type_name]} support for the OS 27 "
                "RealityKit material contract"
            )
        return sdf_type

    mapping = {
        'boolean': Sdf.ValueTypeNames.Bool,
        'integer': Sdf.ValueTypeNames.Int,
        'float': Sdf.ValueTypeNames.Float,
        'color3': Sdf.ValueTypeNames.Color3f,
        'color4': color4_type,
        'vector2': Sdf.ValueTypeNames.Float2,
        'vector3': Sdf.ValueTypeNames.Float3,
        'vector4': Sdf.ValueTypeNames.Float4,
        # UsdMtlx's spelling of MaterialX matrices, as on RealityKit's
        # surface matrix readers.
        'matrix33': Sdf.ValueTypeNames.Matrix3d,
        'matrix44': Sdf.ValueTypeNames.Matrix4d,
        'vector2array': Sdf.ValueTypeNames.Float2Array,
        'vector3array': Sdf.ValueTypeNames.Float3Array,
        'string': Sdf.ValueTypeNames.String,
        'filename': Sdf.ValueTypeNames.Asset,
        'surfaceshader': Sdf.ValueTypeNames.Token,
        'displacementshader': Sdf.ValueTypeNames.Token,
        'volumeshader': Sdf.ValueTypeNames.Token,
        'material': Sdf.ValueTypeNames.Token,
    }
    return mapping.get(type_name)


def _normalize_mtlx_type(type_name: Optional[str]) -> Optional[str]:
    """Normalize MaterialX types for conversion nodes."""
    if not type_name:
        return None
    type_name = type_name.lower()
    if type_name in ('integer', 'int'):
        return 'integer'
    return type_name


def _sdf_type_to_mtlx(sdf_type):
    """Map Sdf value types back to MaterialX type strings."""
    color4_type = getattr(Sdf.ValueTypeNames, "Color4f", None)
    half_type = getattr(Sdf.ValueTypeNames, 'Half', None)
    half2_type = getattr(Sdf.ValueTypeNames, 'Half2', None)
    half3_type = getattr(Sdf.ValueTypeNames, 'Half3', None)
    half4_type = getattr(Sdf.ValueTypeNames, 'Half4', None)
    if half_type and sdf_type == half_type:
        return 'half'
    if half2_type and sdf_type == half2_type:
        return 'half2'
    if half3_type and sdf_type == half3_type:
        return 'half3'
    if half4_type and sdf_type == half4_type:
        return 'half4'
    if sdf_type == Sdf.ValueTypeNames.Float:
        return 'float'
    if sdf_type == Sdf.ValueTypeNames.Int:
        return 'integer'
    if sdf_type == Sdf.ValueTypeNames.Bool:
        return 'boolean'
    if sdf_type == Sdf.ValueTypeNames.Color3f:
        return 'color3'
    if color4_type and sdf_type == color4_type:
        return 'color4'
    if sdf_type == Sdf.ValueTypeNames.Float2:
        return 'vector2'
    if sdf_type == Sdf.ValueTypeNames.Float3:
        return 'vector3'
    if sdf_type == Sdf.ValueTypeNames.Float4:
        return 'vector4'
    if sdf_type == Sdf.ValueTypeNames.Matrix3d:
        return 'matrix33'
    if sdf_type == Sdf.ValueTypeNames.Matrix4d:
        return 'matrix44'
    return None


def _set_shader_input_value(shader_input, value: Any) -> None:
    """Set a shader input value with basic type coercion."""
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (list, tuple)):
            if Vt is not None:
                if len(value[0]) == 2:
                    shader_input.Set(Vt.Vec2fArray(value))
                    return
                if len(value[0]) == 3:
                    shader_input.Set(Vt.Vec3fArray(value))
                    return
            shader_input.Set(value)
            return
        input_type = shader_input.GetTypeName()
        half2_type = getattr(Sdf.ValueTypeNames, 'Half2', None)
        half3_type = getattr(Sdf.ValueTypeNames, 'Half3', None)
        half4_type = getattr(Sdf.ValueTypeNames, 'Half4', None)
        if len(value) == 2:
            if half2_type and input_type == half2_type:
                if not hasattr(Gf, 'Vec2h'):
                    raise RuntimeError("OpenUSD lacks Gf.Vec2h required by the OS 27 contract")
                shader_input.Set(Gf.Vec2h(*value))
            else:
                shader_input.Set(Gf.Vec2f(*value))
        elif len(value) == 3:
            if half3_type and input_type == half3_type:
                if not hasattr(Gf, 'Vec3h'):
                    raise RuntimeError("OpenUSD lacks Gf.Vec3h required by the OS 27 contract")
                shader_input.Set(Gf.Vec3h(*value))
            else:
                shader_input.Set(Gf.Vec3f(*value))
        elif len(value) == 4:
            if half4_type and input_type == half4_type:
                if not hasattr(Gf, 'Vec4h'):
                    raise RuntimeError("OpenUSD lacks Gf.Vec4h required by the OS 27 contract")
                shader_input.Set(Gf.Vec4h(*value))
            else:
                shader_input.Set(Gf.Vec4f(*value))
        else:
            shader_input.Set(value)
    else:
        shader_input.Set(value)


def _default_value_from_input_def(input_def: Optional[Dict[str, Any]]):
    """Parse a default value from a MaterialX input definition."""
    if not input_def:
        return None

    value = input_def.get('value')
    if value in (None, ""):
        return None

    type_name = (input_def.get('type') or '').lower()

    if type_name in ('boolean',):
        return str(value).lower() in ('true', '1')
    if type_name in ('integer',):
        try:
            return int(value)
        except ValueError:
            return None
    if type_name in ('float', 'half'):
        try:
            return float(value)
        except ValueError:
            return None
    if type_name in ('color3', 'color4', 'vector2', 'vector3', 'vector4', 'half2', 'half3', 'half4'):
        parts = [p.strip() for p in str(value).split(',') if p.strip() != ""]
        try:
            return [float(p) for p in parts]
        except ValueError:
            return None
    if type_name in ('string', 'filename'):
        return str(value)

    if ',' in str(value):
        parts = [p.strip() for p in str(value).split(',') if p.strip() != ""]
        try:
            return [float(p) for p in parts]
        except ValueError:
            return None

    try:
        return float(value)
    except ValueError:
        return str(value)


def _coerce_value_to_input_type(value: Any, input_def: Optional[Dict[str, Any]]):
    """Fit a constant to its MaterialX input's type, as Blender converts sockets.

    Extraction already converts constants to their consumer's type, so this
    only squares up a length. One value repeats in every component, a colour
    gains an alpha of 1, and a vector gains zeros. A list reaching a scalar
    input raises: whether it was a colour (grey) or a vector (mean) is no
    longer known here, and its first component is neither.
    """
    if not input_def:
        return value

    type_name = (input_def.get('type') or '').lower()
    if not isinstance(value, (list, tuple)):
        if type_name in ('float', 'half') and isinstance(value, (bool, int, float)):
            return float(value)
        if type_name in ('integer', 'int') and isinstance(value, (bool, int, float)):
            return int(value)
        if type_name in ('boolean', 'bool') and isinstance(value, (bool, int, float)):
            return bool(value)
        return value

    sizes = {
        'color3': 3, 'vector3': 3, 'half3': 3,
        'color4': 4, 'vector4': 4, 'half4': 4,
        'vector2': 2, 'half2': 2,
    }
    if type_name in ('float', 'half', 'integer', 'int'):
        if len(value) == 1:
            return value[0]
        raise ValueError(
            f"A {len(value)}-component constant reached a {type_name} input; "
            "extraction should have converted it to one value"
        )
    size = sizes.get(type_name)
    if size is None or not value:
        return value
    if len(value) == 1:
        repeated = [value[0]] * size
        if type_name == 'color4':
            repeated[3] = 1.0
        return repeated
    if len(value) >= size:
        return list(value[:size])
    padded = list(value)
    if size == 4 and len(padded) == 3:
        padded.append(1.0 if type_name == 'color4' else 0.0)
    padded.extend([0.0] * (size - len(padded)))
    return padded


def _create_chain_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    source_output,
    *,
    steps,
    diagnostics=None,
):
    """Author a fixed chain of manifest-verified shaders and return its output.

    Every nodedef in ``steps`` is asserted against the manifest before any prim
    is authored, so a typo here fails loudly instead of shipping an unknown
    info:id.
    """
    nodes = manifest.get("nodes", {})
    for nodedef_name, _input, _from, _to, _extra in steps:
        if nodedef_name not in nodes:
            raise ValueError(
                f"Conversion chain nodedef '{nodedef_name}' is not in the "
                "MaterialX manifest."
            )

    current = source_output
    for nodedef_name, input_name_on_node, from_type, to_type, extra in steps:
        shader_name = _convert_shader_name(stage, nodegraph_path, input_name)
        prim = stage.DefinePrim(f"{nodegraph_path}/{shader_name}", "Shader")
        shader = UsdShade.Shader(prim)
        shader.CreateIdAttr(nodedef_name)
        in_type = _map_mtlx_type_to_sdf(from_type) or current.GetTypeName()
        shader.CreateInput(input_name_on_node, in_type).ConnectToSource(current)
        for extra_name, extra_value in extra.items():
            # A step may need a typed constant (a dot-product mask), not only a
            # string. Tuples carry their own Sdf type.
            if isinstance(extra_value, tuple):
                extra_type, value = extra_value
            else:
                extra_type, value = Sdf.ValueTypeNames.String, extra_value
            shader.CreateInput(extra_name, extra_type).Set(value)
        out_type = _map_mtlx_type_to_sdf(to_type) or current.GetTypeName()
        current = shader.CreateOutput("out", out_type)
    if diagnostics:
        chain = " -> ".join(step[0] for step in steps)
        diagnostics.add_warning(
            f"Inserted conversion chain for {input_name}: {chain}."
        )
    return current


def _create_convert_output(
    manifest: Dict[str, Any],
    stage,
    nodegraph_path: str,
    input_name: str,
    source_output,
    from_type: str,
    to_type: str,
    diagnostics=None,
):
    """Create a convert node output between two MaterialX types."""
    from_type = (from_type or '').lower()
    to_type = (to_type or '').lower()
    if from_type == to_type:
        return source_output

    nodedef_name = select_nodedef_name_for_node(
        manifest,
        "convert",
        input_type=from_type,
        output_type=to_type,
    )
    if not nodedef_name:
        # MaterialX has no convert nodedef for every pair, and an invented
        # name such as ND_convert_color3_float exists in no library. Two
        # pairs have real manifest-backed paths; everything else fails the
        # material.
        if (from_type, to_type) in (("color3", "float"), ("color4", "float")):
            # Blender's implicit colour-to-float conversion is Cycles'
            # linear_rgb_to_gray: a dot product with the gray weights the
            # resolver uses. ND_luminance is not used - authored without
            # lumacoeffs it weighs with ACEScg's coefficients.
            from .extract.core import _RGB_TO_GRAY

            steps = []
            if from_type == "color4":
                steps.append(("ND_convert_color4_color3", "in", "color4", "color3", {}))
            steps += [
                ("ND_convert_color3_vector3", "in", "color3", "vector3", {}),
                ("ND_dotproduct_vector3", "in1", "vector3", to_type,
                 {"in2": (Sdf.ValueTypeNames.Float3, Gf.Vec3f(*_RGB_TO_GRAY))}),
            ]
            return _create_chain_output(
                manifest, stage, nodegraph_path, input_name, source_output,
                steps=steps,
                diagnostics=diagnostics,
            )
        if (from_type, to_type) == ("vector3", "float"):
            # Blender's implicit vector-to-float conversion is the mean.
            return _create_chain_output(
                manifest, stage, nodegraph_path, input_name, source_output,
                steps=[
                    ("ND_dotproduct_vector3", "in1", "vector3", to_type,
                     {"in2": (Sdf.ValueTypeNames.Float3, Gf.Vec3f(1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0))}),
                ],
                diagnostics=diagnostics,
            )
        if (from_type, to_type) == ("vector4", "color3"):
            # Drop the fourth channel; channel-preserving and unambiguous.
            # Two implemented converts rather than one unimplemented swizzle.
            return _create_chain_output(
                manifest, stage, nodegraph_path, input_name, source_output,
                steps=[
                    ("ND_convert_vector4_color4", "in", from_type, "color4", {}),
                    ("ND_convert_color4_color3", "in", "color4", to_type, {}),
                ],
                diagnostics=diagnostics,
            )
        raise ValueError(
            f"No MaterialX conversion exists from {from_type} to {to_type} "
            f"for input '{input_name}'. Bake the material, or rewire the "
            "input to a matching type."
        )
    convert_name = _convert_shader_name(stage, nodegraph_path, input_name)
    convert_prim = stage.DefinePrim(f"{nodegraph_path}/{convert_name}", "Shader")
    convert_shader = UsdShade.Shader(convert_prim)
    convert_shader.CreateIdAttr(nodedef_name)

    in_type = _map_mtlx_type_to_sdf(from_type) or source_output.GetTypeName()
    out_type = _map_mtlx_type_to_sdf(to_type) or source_output.GetTypeName()

    in_input = convert_shader.CreateInput("in", in_type)
    in_input.ConnectToSource(source_output)
    if diagnostics:
        diagnostics.add_warning(
            f"Inserted convert node '{nodedef_name}' for {input_name}: {from_type} -> {to_type}."
        )
    return convert_shader.CreateOutput("out", out_type)
