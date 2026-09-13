"""Surface readers: nodes that read the surface, the view or the mesh."""

import math
import re
from typing import Any, Dict, Optional

from . import core as _core


# ---------------------------------------------------------------------------
# Surface readers: Blender input nodes that read the surface, the view, or the
# UV set, authored as MaterialX geometric readers RealityKit implements.
#
# Every reader here is a standard MaterialX node RealityKit implements, except
# the view direction, which uses Apple's ``ND_realitykit_surface_view_direction``:
# it is implemented in both nodedef stores, and the t25_surface_readers import
# measured its direction and length.
# Apple's own ``ND_realitykit_surface_uv0`` / ``surface_model_position`` /
# ``surface_world_position`` / ``surface_time`` are deprecated by the platform
# with notices pointing at the standard readers used below.
# ---------------------------------------------------------------------------

_READER_NODE_TYPES = frozenset({'TEX_COORD', 'NEW_GEOMETRY', 'FRESNEL', 'LAYER_WEIGHT'})



def _node_expr_output_type(expr: Dict[str, Any]) -> Optional[str]:
    """The declared type of a node expression's output, from the manifest."""
    entry = _core._get_manifest().get("nodes", {}).get(expr.get("node_id"))
    if not entry:
        return None
    wanted = expr.get("output") or "out"
    for output in entry.get("outputs", []):
        if output.get("name") == wanted:
            return output.get("type")
    return None


def _float_node(name: str, **inputs: Any) -> Dict[str, Any]:
    return _core._make_node_expr(_core._nodedef_for(name, "float"), inputs)


def _world_reader_expr(node_name: str, **extra: Any) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {"space": _core._constant_expr("world")}
    inputs.update({key: _core._constant_expr(value) for key, value in extra.items()})
    return _core._make_node_expr(_core._nodedef_for(node_name, "vector3"), inputs)


def _view_direction_expr() -> Dict[str, Any]:
    """Apple's surface view direction, normalised to Blender's Incoming.

    Measured by importing ``t25_surface_readers`` into Reality Composer Pro 3:
    the reader points from the surface toward the viewer,
    as Blender's Incoming does, and is not unit length. The normalize below
    is what makes it Blender's vector.
    """
    reader = _core._make_node_expr(
        _core._nodedef_for("realitykit_surface_view_direction", "vector3"),
        {},
        output="viewDirection",
    )
    # Measured by import: the reader is not unit length. Unnormalised, the
    # probe sphere in t25_surface_readers rendered flat white and the two rim
    # spheres kept only a hairline rim; Blender's Incoming is a unit vector.
    return _core._make_node_expr(_core._nodedef_for("normalize", "vector3"), {"in": reader})


def _front_facing_float_expr() -> Dict[str, Any]:
    front = _core._make_node_expr(_core._nodedef_for("realitykit_is_front_facing", "boolean"), {})
    return _core._make_node_expr(_core._nodedef_for("convert", "float", input_type="boolean"), {"in": front})


def _texture_coordinate_expr(output_name: str) -> Optional[Dict[str, Any]]:
    if output_name == 'UV':
        return _core._make_node_expr(_core._nodedef_for("texcoord", "vector3"), {})
    if output_name in ('Object', 'Generated'):
        # Generated is object-space position without Blender's bounding-box
        # normalization; collect_material_warnings names the approximation.
        return _core._make_node_expr(
            _core._nodedef_for("position", "vector3"), {"space": _core._constant_expr("object")}
        )
    return None


#: The only Blender working space whose scene-linear values the export's
#: ``lin_rec709_scene`` colour-space tag describes, by its Color Interop
#: Forum id.
EXPORTED_WORKING_SPACE_INTEROP_ID = "lin_rec709_scene"


def working_color_space_refusal(blend_data=None) -> Optional[str]:
    """Why the blend file's working colour space cannot be exported, or None.

    Blender 5.2 lets a file keep its scene-linear colours in Linear Rec.709,
    Linear Rec.2020 or ACEScg (``bpy.data.colorspace.working_space``). Every
    material constant, vertex colour and linear texture read is authored
    unconverted under the ``lin_rec709_scene`` tag, so a Rec.2020 or ACEScg
    file would render with shifted hues. Outside Blender there is nothing to
    read and nothing is refused.
    """
    if blend_data is None:
        try:
            import bpy
            blend_data = bpy.data
        except Exception:
            return None
    colorspace = getattr(blend_data, "colorspace", None)
    if colorspace is None:
        return None
    interop_id = str(getattr(colorspace, "working_space_interop_id", "") or "")
    if interop_id == EXPORTED_WORKING_SPACE_INTEROP_ID:
        return None
    name = str(getattr(colorspace, "working_space", "") or interop_id or "unknown")
    return (
        f"The blend file's working color space is '{name}', but the export writes every "
        "color as Linear Rec.709 (lin_rec709_scene) and does not convert. Set the file's "
        "Working Space to Linear Rec.709 in the Color Management settings before exporting"
    )


def reader_refusal(node, output_name: str) -> Optional[str]:
    """Why a Texture Coordinate or Geometry output cannot be exported, or None.

    The resolver and the validator both ask here, so they name one reason.
    """
    node_type = getattr(node, "type", "")
    if node_type == 'TEX_COORD' and output_name == 'Object' and getattr(node, "object", None) is not None:
        # Cycles' NODE_TEXCO_OBJECT_WITH_TRANSFORM: inverse(reference world
        # matrix) times the world position. The export has only the shaded
        # mesh's own object space, and a material shared by several meshes
        # has no single constant for the reference's transform.
        return (
            f"Texture Coordinate 'Object' uses the object "
            f"'{getattr(node.object, 'name', '?')}' as its space, which the export cannot "
            "express (it has only the shaded mesh's own object space); clear the node's "
            "Object field or bake the material"
        )
    if node_type == 'NEW_GEOMETRY' and output_name == 'Tangent':
        # Cycles' primitive_tangent: without a UV map it is the radial tangent
        # around object Z derived from Generated coordinates, not the UV
        # tangent RealityKit's tangent reader returns, and Generated itself
        # has no exact reader.
        return (
            "Geometry 'Tangent' is Blender's tangent around the object's Z axis, built "
            "from Generated coordinates, not the UV tangent RealityKit reads; bake the "
            "material"
        )
    return None


def blender_world_vector(expr: Dict[str, Any]) -> Dict[str, Any]:
    """A RealityKit world-space vector in Blender's world axes.

    The export's root prim turns Blender's Z-up world into RealityKit's Y-up
    one with a -90 degree rotation about X, so RealityKit's world readers
    return ``(x, z, -y)`` of Blender's vector and Blender's is ``(x, -z, y)``
    of theirs. Blender math on a world-space vector (a Vector Math constant, a
    colour read of a normal) only agrees with Blender after this turn back.
    Measured by importing ``t26_vector_math`` and ``t36_bump``: unconverted,
    a world normal read green on top and blue toward the viewer; converted,
    t26 shows Blender's colours (lavender on top, purple facing the viewer)
    and its reflected sphere swaps top and bottom as Blender does. Dot products
    and lengths do not change, so Fresnel, Layer Weight and Camera Data read
    the platform's axes directly.
    """
    parts = [_vector_dot(expr, _core._constant_expr(mask)) for mask in ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))]
    return _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {"in1": parts[0], "in2": parts[1], "in3": parts[2]})


def _geometry_reader_expr(output_name: str) -> Optional[Dict[str, Any]]:
    if output_name == 'Position':
        return blender_world_vector(_world_reader_expr("position"))
    if output_name == 'Normal':
        return blender_world_vector(_world_reader_expr("normal"))
    if output_name == 'Incoming':
        return blender_world_vector(_view_direction_expr())
    if output_name == 'Backfacing':
        return _float_node("subtract", in1=_core._constant_expr(1.0), in2=_front_facing_float_expr())
    return None


def _fresnel_dielectric_cos_expr(cosi: Dict[str, Any], eta: Dict[str, Any]) -> Dict[str, Any]:
    """Cycles' ``fresnel_dielectric_cos``, node for node.

    Transcribed from ``intern/cycles/kernel/closure/bsdf_util.h``::

        float c = fabsf(cosi);
        float g = eta * eta - 1.0f + c * c;
        if (g > 0.0f) {
            g = sqrtf(g);
            float A = (g - c) / (g + c);
            float B = (c * (g + c) - 1.0f) / (c * (g - c) + 1.0f);
            return 0.5f * A * A * (1.0f + B * B);
        }
        return 1.0f;  /* total internal reflection */

    The branch is an ``ifgreater`` on the un-rooted ``g``; the root is taken of
    ``max(g, 0)`` so the unselected branch never evaluates ``sqrt`` of a
    negative on the GPU.
    """
    one = _core._constant_expr(1.0)
    c = _float_node("absval", **{"in": cosi})
    g0 = _float_node(
        "add",
        in1=_float_node("subtract", in1=_float_node("multiply", in1=eta, in2=eta), in2=one),
        in2=_float_node("multiply", in1=c, in2=c),
    )
    g = _float_node("sqrt", **{"in": _float_node("max", in1=g0, in2=_core._constant_expr(0.0))})
    a = _float_node(
        "divide",
        in1=_float_node("subtract", in1=g, in2=c),
        in2=_float_node("add", in1=g, in2=c),
    )
    b = _float_node(
        "divide",
        in1=_float_node("subtract", in1=_float_node("multiply", in1=c, in2=_float_node("add", in1=g, in2=c)), in2=one),
        in2=_float_node("add", in1=_float_node("multiply", in1=c, in2=_float_node("subtract", in1=g, in2=c)), in2=one),
    )
    value = _float_node(
        "multiply",
        in1=_float_node("multiply", in1=_core._constant_expr(0.5), in2=_float_node("multiply", in1=a, in2=a)),
        in2=_float_node("add", in1=one, in2=_float_node("multiply", in1=b, in2=b)),
    )
    return _float_node("ifgreater", value1=g0, value2=_core._constant_expr(0.0), in1=value, in2=one)


def _eta_by_facing_expr(front_eta: Dict[str, Any], back_eta: Dict[str, Any]) -> Dict[str, Any]:
    """``backfacing() ? back : front`` as a mix driven by the front-facing flag."""
    return _float_node("mix", fg=front_eta, bg=back_eta, mix=_front_facing_float_expr())


def _cos_view_normal_expr() -> Dict[str, Any]:
    return _core._make_node_expr(
        _core._nodedef_for("dotproduct", "float", input_type="vector3"),
        {"in1": _view_direction_expr(), "in2": _world_reader_expr("normal")},
    )


def _fresnel_node_expr(node, visited, channel, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Fresnel node (``node_fresnel.osl``)::

        float f = max(IOR, 1e-5);
        float eta = backfacing() ? 1.0 / f : f;
        Fac = fresnel_dielectric_cos(dot(I, Normal), eta);

    A linked Normal is refused by the validator: the exporter decodes normal
    maps in tangent space, which cannot be dotted with a world-space view
    vector without the basis this graph does not carry.
    """
    normal_socket = node.inputs.get('Normal') if hasattr(node, "inputs") else None
    if normal_socket is not None and getattr(normal_socket, "is_linked", False):
        return None
    ior = _core._expr_from_socket(
        node.inputs.get('IOR') if hasattr(node, "inputs") else None,
        visited, channel, provenance, cache, default=1.45,
    )
    if ior is None or (isinstance(ior, dict) and ior.get("kind") == "unresolved"):
        return ior
    f = _float_node("max", in1=ior, in2=_core._constant_expr(1e-5))
    eta = _eta_by_facing_expr(f, _float_node("divide", in1=_core._constant_expr(1.0), in2=f))
    return _fresnel_dielectric_cos_expr(_cos_view_normal_expr(), eta)


def _layer_weight_expr(node, output_name: str, visited, channel, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Layer Weight node (``node_layer_weight.osl``)::

        float cosNI = dot(I, Normal);
        // Fresnel
        float eta = max(1.0 - Blend, 1e-5);
        eta = backfacing() ? eta : 1.0 / eta;
        Fresnel = fresnel_dielectric_cos(cosNI, eta);
        // Facing
        float facing = fabs(cosNI);
        if (Blend != 0.5) {
            Blend = clamp(Blend, 0.0, 1.0 - 1e-5);
            Blend = (Blend < 0.5) ? 2.0 * Blend : 0.5 / (1.0 - Blend);
            facing = pow(facing, Blend);
        }
        Facing = 1.0 - facing;

    Only a constant Blend is authored (the validator refuses a linked one), so
    the exponent and the two etas fold to constants here.
    """
    normal_socket = node.inputs.get('Normal') if hasattr(node, "inputs") else None
    if normal_socket is not None and getattr(normal_socket, "is_linked", False):
        return None
    blend_socket = node.inputs.get('Blend') if hasattr(node, "inputs") else None
    if blend_socket is None or getattr(blend_socket, "is_linked", False):
        return None
    try:
        blend = float(blend_socket.default_value)
    except (TypeError, ValueError):
        return None
    cos_ni = _cos_view_normal_expr()
    if output_name == 'Fresnel':
        eta = max(1.0 - blend, 1e-5)
        return _fresnel_dielectric_cos_expr(
            cos_ni, _eta_by_facing_expr(_core._constant_expr(1.0 / eta), _core._constant_expr(eta))
        )
    if output_name == 'Facing':
        facing = _float_node("absval", **{"in": cos_ni})
        if blend != 0.5:
            clamped = min(max(blend, 0.0), 1.0 - 1e-5)
            exponent = 2.0 * clamped if clamped < 0.5 else 0.5 / (1.0 - clamped)
            facing = _float_node("power", in1=facing, in2=_core._constant_expr(exponent))
        return _float_node("subtract", in1=_core._constant_expr(1.0), in2=facing)
    return None


#: Blender Vector Math operations that are one MaterialX vector3 node: their
#: Cycles functions are the bare componentwise functions on every input.
#: NORMALIZE (safe_normalize) and ROUND (floor(a + 0.5)) are composed.
_VECTOR_MATH_SINGLE_INPUT_OPS = {
    'ABSOLUTE': 'absval',
    'SIGN': 'sign',
    'FLOOR': 'floor',
    'CEIL': 'ceil',
    'SINE': 'sin',
    'COSINE': 'cos',
    'TANGENT': 'tan',
}
_VECTOR_MATH_TWO_INPUT_OPS = {
    'ADD': 'add',
    'SUBTRACT': 'subtract',
    'MULTIPLY': 'multiply',
    'MINIMUM': 'min',
    'MAXIMUM': 'max',
    'CROSS_PRODUCT': 'crossproduct',
}
#: Operations authored as exact compositions of implemented nodes, transcribed
#: from Cycles' ``svm_vector_math`` (intern/cycles/kernel/svm/math_util.h) and
#: intern/cycles/util/math_float3.h: DIVIDE is the per-component safe_divide,
#: POWER the per-component safe_powf, NORMALIZE, REFLECT and REFRACT use
#: safe_normalize, REFRACT is zero only for k < 0, and ROUND is floor(a + 0.5).
_VECTOR_MATH_COMPOSED_OPS = frozenset({
    'MULTIPLY_ADD', 'PROJECT', 'REFLECT', 'REFRACT', 'FACEFORWARD',
    'DOT_PRODUCT', 'DISTANCE', 'LENGTH', 'SCALE', 'FRACTION',
    'DIVIDE', 'POWER', 'NORMALIZE', 'ROUND',
})
#: Operations whose Blender semantics cannot be authored exactly today.
#: MODULO truncates where MaterialX's floors, as with the scalar Math node;
#: WRAP and SNAP guard a per-component division by zero, and MaterialX has no
#: per-component select to reproduce that guard.
_VECTOR_MATH_REFUSED_OPS = {
    'MODULO': "its truncated modulo differs from MaterialX's floored one",
    'WRAP': "its per-component zero-range guard has no MaterialX node",
    'SNAP': "its per-component safe divide has no MaterialX node",
}
_VECTOR_MATH_VALUE_OUTPUT_OPS = frozenset({'DOT_PRODUCT', 'DISTANCE', 'LENGTH'})

#: Letters that name one channel of a texture read.
_SINGLE_CHANNELS = frozenset("rgbaxyzw")


def _vector3_node(name: str, **inputs: Any) -> Dict[str, Any]:
    return _core._make_node_expr(_core._nodedef_for(name, "vector3", input_type="vector3"), inputs)


def _texture_is_scalar(expr: Dict[str, Any]) -> bool:
    """Whether a texture expression reads one channel (a float) of its file."""
    channel = (expr.get("channel") or "").lower()
    return len(channel) == 1 and channel in _SINGLE_CHANNELS


def _vector3_operand_expr(expr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce a resolved expression to a vector3, as Blender's sockets do.

    A float broadcasts to all three components; a colour is read as a vector;
    a texture is read as colour and converted, or as its one channel and
    broadcast. Anything unresolved passes through so the refusal names the
    source.
    """
    if not isinstance(expr, dict):
        return None
    kind = expr.get("kind")
    if kind == "unresolved":
        return expr
    if kind == "constant":
        value = expr.get("value")
        if isinstance(value, (list, tuple)):
            parts = [float(v) for v in value][:3]
            while len(parts) < 3:
                parts.append(parts[-1] if parts else 0.0)
            return _core._constant_expr(tuple(parts))
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return _core._constant_expr((f, f, f))
    if kind == "texture":
        texture = dict(expr)
        if _texture_is_scalar(texture):
            texture["output_type"] = "float"
            return _core._make_node_expr(
                _core._nodedef_for("convert", "vector3", input_type="float"), {"in": texture}
            )
        texture["output_type"] = "color3"
        return _core._make_node_expr(
            _core._nodedef_for("convert", "vector3", input_type="color3"), {"in": texture}
        )
    if kind == "node":
        out_type = _node_expr_output_type(expr) or "vector3"
        if out_type == "vector3":
            return expr
        if out_type == "color4":
            expr = _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="color4"), {"in": expr})
            out_type = "color3"
        if out_type in ("float", "color3"):
            return _core._make_node_expr(
                _core._nodedef_for("convert", "vector3", input_type=out_type), {"in": expr}
            )
    return None


def _vector_math_operand(node, index, visited, channel, provenance, cache) -> Optional[Dict[str, Any]]:
    inputs = getattr(node, "inputs", None)
    if inputs is None or len(inputs) <= index:
        return None
    return _vector_socket_expr(inputs[index], visited, provenance, cache)


def _vector_socket_expr(socket, visited, provenance, cache, default=(0.0, 0.0, 0.0)) -> Optional[Dict[str, Any]]:
    """A vector socket's value as a vector3 expression: its link, else its value."""
    if socket is None:
        return _core._constant_expr(tuple(default))
    if getattr(socket, "is_linked", False):
        resolved = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache,
            expected_type="vector3",
        )
        return _vector3_operand_expr(resolved)
    value = _core._socket_default_value(socket)
    if value is None:
        return None
    return _vector3_operand_expr(_core._constant_expr(value))


def _float_socket_expr(socket, visited, provenance, cache, default: float = 0.0) -> Optional[Dict[str, Any]]:
    """A scalar socket's value as a float expression: its link (converted as
    Blender converts), its driver, else its value."""
    if socket is None:
        return _core._constant_expr(float(default))
    if getattr(socket, "is_linked", False):
        resolved = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache,
            expected_type="float",
        )
        return _core._float_math_input_expr(resolved)
    driven = _core._driven_socket_expr(socket)
    if driven is not None:
        return driven
    value = _core._socket_default_value(socket)
    try:
        return _core._constant_expr(float(value))
    except (TypeError, ValueError):
        return _core._constant_expr(float(default))


def _vector_math_scalar_operand(node, socket_name, visited, channel, provenance, cache):
    inputs = getattr(node, "inputs", None)
    socket = inputs.get(socket_name) if inputs is not None and hasattr(inputs, "get") else None
    if socket is None:
        # Blender exposes Scale as the last socket of the node.
        socket = inputs[-1] if inputs is not None and len(inputs) else None
    if socket is None:
        return None
    return _float_socket_expr(socket, visited, provenance, cache)


def _vector_times_float(v: Dict[str, Any], s: Dict[str, Any]) -> Dict[str, Any]:
    """vector3 * float, authored as vector3 * broadcast(float).

    MaterialX has ``ND_multiply_vector3FA`` for exactly this, and it renders
    correctly with a constant float. Measured by import (probe_b_scale_by_u,
    Reality Composer Pro 3): with a *connected* float it
    multiplies by the vector feeding the float's producer instead, so a tint
    scaled by U came out as tint times (U, V, 0). Broadcasting the float into
    a vector with ``combine3`` and multiplying vector by vector renders as
    Blender does in both cases; a constant float folds to a constant vector.
    """
    if isinstance(s, dict) and s.get("kind") == "constant":
        try:
            f = float(s["value"])
        except (TypeError, ValueError):
            f = None
        if f is not None:
            return _vector3_node("multiply", in1=v, in2=_core._constant_expr((f, f, f)))
    broadcast = _core._make_node_expr(
        _core._nodedef_for("combine3", "vector3"), {"in1": s, "in2": s, "in3": s}
    )
    return _vector3_node("multiply", in1=v, in2=broadcast)


def _vector_dot(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    return _core._make_node_expr(
        _core._nodedef_for("dotproduct", "float", input_type="vector3"), {"in1": a, "in2": b}
    )


def _per_component_vector3(function, *vectors) -> Dict[str, Any]:
    """Apply a float function to each component of vector3 expressions.

    Constant vectors fold their components; the results are combined back
    into a vector3.
    """
    for vector in vectors:
        if isinstance(vector, dict) and vector.get("kind") == "unresolved":
            return vector
    parts = []
    for axis, index in (("x", 0), ("y", 1), ("z", 2)):
        operands = []
        for vector in vectors:
            value = _constant_value(vector)
            if isinstance(value, (list, tuple)):
                operands.append(_core._constant_expr(float(value[index])))
            else:
                operands.append(_core._component_expr(vector, "vector3", axis))
        parts.append(function(*operands))
    constants = [_core._constant_scalar(part) for part in parts]
    if all(value is not None for value in constants):
        return {"kind": "constant", "value": tuple(constants), "value_type": "vector3"}
    return _core._make_node_expr(
        _core._nodedef_for("combine3", "vector3"), {"in1": parts[0], "in2": parts[1], "in3": parts[2]}
    )


def _vector_math_expr(node, from_socket, visited, channel, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Vector Math node as exact MaterialX vector nodes.

    Semantics are Cycles' (``svm_vector_math`` in
    ``intern/cycles/kernel/svm/math_util.h``): the two-vector operations are
    componentwise, ``divide`` and ``power`` guard each component like the
    scalar Math node, ``project`` guards a zero-length second vector,
    ``normalize``, ``reflect`` and ``refract`` use safe_normalize,
    ``faceforward`` follows GLSL's argument order, ``fraction`` is
    ``a - floor(a)`` and ``round`` is ``floor(a + 0.5)``. Returns None for an
    operation the table refuses so the validator's message, which names the
    operation, is what the artist sees.
    """
    operation = (getattr(node, "operation", "") or "").upper()
    output_name = getattr(from_socket, "name", "") or ""
    wants_value = operation in _VECTOR_MATH_VALUE_OUTPUT_OPS
    if output_name and output_name != ('Value' if wants_value else 'Vector'):
        return None
    if operation in _VECTOR_MATH_REFUSED_OPS:
        return None

    def operand(index):
        return _vector_math_operand(node, index, visited, channel, provenance, cache)

    def scalar(name):
        return _vector_math_scalar_operand(node, name, visited, channel, provenance, cache)

    def fnode(name, **ins):
        return _core._make_node_expr(_core._nodedef_for(name, "float"), ins)

    def vscale(v, s):
        return _vector_times_float(v, s)

    if operation in _VECTOR_MATH_SINGLE_INPUT_OPS:
        a = operand(0)
        return _vector3_node(_VECTOR_MATH_SINGLE_INPUT_OPS[operation], **{"in": a}) if a else None
    if operation in _VECTOR_MATH_TWO_INPUT_OPS:
        a, b = operand(0), operand(1)
        return _vector3_node(_VECTOR_MATH_TWO_INPUT_OPS[operation], in1=a, in2=b) if a and b else None
    if operation == 'DIVIDE':
        a, b = operand(0), operand(1)
        return _safe_divide_vector(a, b) if a and b else None
    if operation == 'POWER':
        a, b = operand(0), operand(1)
        return _per_component_vector3(_core._safe_power_float, a, b) if a and b else None
    if operation == 'NORMALIZE':
        a = operand(0)
        return _safe_normalize(a) if a else None
    if operation == 'ROUND':
        a = operand(0)
        return _vector3_node("floor", **{"in": _vector3_node("add", in1=a, in2=_core._constant_expr((0.5, 0.5, 0.5)))}) if a else None
    if operation == 'MULTIPLY_ADD':
        a, b, c = operand(0), operand(1), operand(2)
        return _vector3_node("add", in1=_vector3_node("multiply", in1=a, in2=b), in2=c) if a and b and c else None
    if operation == 'DOT_PRODUCT':
        a, b = operand(0), operand(1)
        return _vector_dot(a, b) if a and b else None
    if operation == 'DISTANCE':
        a, b = operand(0), operand(1)
        return _core._make_node_expr(_core._nodedef_for("magnitude", "float", input_type="vector3"), {"in": _vector3_node("subtract", in1=a, in2=b)}) if a and b else None
    if operation == 'LENGTH':
        a = operand(0)
        return _core._make_node_expr(_core._nodedef_for("magnitude", "float", input_type="vector3"), {"in": a}) if a else None
    if operation == 'SCALE':
        a, s = operand(0), scalar('Scale')
        return vscale(a, s) if a and s else None
    if operation == 'FRACTION':
        a = operand(0)
        return _vector3_node("subtract", in1=a, in2=_vector3_node("floor", **{"in": a})) if a else None
    if operation == 'PROJECT':
        a, b = operand(0), operand(1)
        if not (a and b):
            return None
        len_sq = _vector_dot(b, b)
        projected = vscale(b, fnode("divide", in1=_vector_dot(a, b), in2=len_sq))
        return _core._make_node_expr(
            _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
            {"value1": len_sq, "value2": _core._constant_expr(0.0), "in1": projected, "in2": _core._constant_expr((0.0, 0.0, 0.0))},
        )
    if operation == 'REFLECT':
        a, b = operand(0), operand(1)
        if not (a and b):
            return None
        n = _safe_normalize(b)
        two_dot = fnode("multiply", in1=_core._constant_expr(2.0), in2=_vector_dot(a, n))
        return _vector3_node("subtract", in1=a, in2=vscale(n, two_dot))
    if operation == 'REFRACT':
        a, b, eta = operand(0), operand(1), scalar('Scale')
        if not (a and b and eta):
            return None
        n = _safe_normalize(b)
        cos_i = _vector_dot(n, a)
        k = fnode("subtract", in1=_core._constant_expr(1.0), in2=fnode("multiply", in1=fnode("multiply", in1=eta, in2=eta), in2=fnode("subtract", in1=_core._constant_expr(1.0), in2=fnode("multiply", in1=cos_i, in2=cos_i))))
        refracted = _vector3_node(
            "subtract",
            in1=vscale(a, eta),
            in2=vscale(n, fnode("add", in1=fnode("multiply", in1=eta, in2=cos_i), in2=fnode("sqrt", **{"in": fnode("max", in1=k, in2=_core._constant_expr(0.0))}))),
        )
        # Cycles' refract returns zero only for k < 0; k == 0 refracts.
        return _core._make_node_expr(
            _core._nodedef_for("ifgreatereq", "vector3", input_type="vector3"),
            {"value1": k, "value2": _core._constant_expr(0.0), "in1": refracted, "in2": _core._constant_expr((0.0, 0.0, 0.0))},
        )
    if operation == 'FACEFORWARD':
        a, b, c = operand(0), operand(1), operand(2)
        if not (a and b and c):
            return None
        return _core._make_node_expr(
            _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
            {"value1": _core._constant_expr(0.0), "value2": _vector_dot(c, b), "in1": a, "in2": vscale(a, _core._constant_expr(-1.0))},
        )
    return None


#: Fixed axes of the Vector Rotate node's single-axis rotation types.
_VECTOR_ROTATE_AXES = {
    'X_AXIS': (1.0, 0.0, 0.0),
    'Y_AXIS': (0.0, 1.0, 0.0),
    'Z_AXIS': (0.0, 0.0, 1.0),
}


def _constant_value(expr):
    if isinstance(expr, dict) and expr.get("kind") == "constant":
        return expr.get("value")
    return None


def _named_vector_operand(node, name, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """A whole-vector operand read from a named, enabled input socket.

    Blender's ``inputs.get`` returns None for a socket its node has disabled,
    so a mode's unused socket is never read.
    """
    inputs = getattr(node, "inputs", None)
    socket = inputs.get(name) if inputs is not None and hasattr(inputs, "get") else None
    if socket is None:
        return None
    if getattr(socket, "is_linked", False):
        resolved = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache,
            expected_type="vector3",
        )
        return _vector3_operand_expr(resolved)
    value = _core._socket_default_value(socket)
    if value is None:
        return None
    return _vector3_operand_expr(_core._constant_expr(value))


def _negated_float(expr: Dict[str, Any]) -> Dict[str, Any]:
    value = _constant_value(expr)
    if value is not None:
        return _core._constant_expr(-float(value))
    return _core._make_node_expr(_core._nodedef_for("multiply", "float"), {"in1": expr, "in2": _core._constant_expr(-1.0)})


def _rotate_about_unit_axis(p: Dict[str, Any], axis: Dict[str, Any], angle: Dict[str, Any]) -> Dict[str, Any]:
    """Rotate ``p`` counterclockwise by ``angle`` radians about a unit ``axis``.

    Rodrigues' formula, ``p cos + (axis x p) sin + axis (axis . p)(1 - cos)``,
    is the matrix Cycles' ``rotate_around_axis`` expands. It is built from
    dot, cross and trigonometry nodes rather than MaterialX ``rotate3d``,
    whose ``amount`` is in degrees and whose reference implementation applies
    the transposed matrix, so neither its unit nor its direction would match.
    A constant angle folds its cosine and sine.
    """
    value = _constant_value(angle)
    if value is not None:
        cos = _core._constant_expr(math.cos(float(value)))
        sin = _core._constant_expr(math.sin(float(value)))
        one_minus_cos = _core._constant_expr(1.0 - math.cos(float(value)))
    else:
        cos = _core._make_node_expr(_core._nodedef_for("cos", "float"), {"in": angle})
        sin = _core._make_node_expr(_core._nodedef_for("sin", "float"), {"in": angle})
        one_minus_cos = _core._make_node_expr(
            _core._nodedef_for("subtract", "float"), {"in1": _core._constant_expr(1.0), "in2": cos}
        )
    along_axis = _core._make_node_expr(
        _core._nodedef_for("multiply", "float"), {"in1": _vector_dot(axis, p), "in2": one_minus_cos}
    )
    return _vector3_node(
        "add",
        in1=_vector3_node(
            "add",
            in1=_vector_times_float(p, cos),
            in2=_vector_times_float(_vector3_node("crossproduct", in1=axis, in2=p), sin),
        ),
        in2=_vector_times_float(axis, along_axis),
    )


def _vector_rotate_expr(node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Vector Rotate node, transcribed from Cycles' ``svm_vector_rotate``.

    Every rotation type turns ``Vector - Center`` and adds ``Center`` back.
    The single-axis types and Axis Angle rotate by Angle about their axis,
    and Invert negates the angle; Axis Angle normalises its axis and passes
    Vector through unchanged when the axis has zero length. Euler XYZ applies
    X, then Y, then Z about the fixed axes, and Invert applies the transposed
    rotation: Z, then Y, then X, each negated.

    Measured by importing ``t31_vector_rotate`` into Reality Composer Pro 3:
    Z Axis, an inverted Axis Angle with a linked angle,
    and Euler XYZ each render identical to their U/V control cube.
    """
    p = _named_vector_operand(node, 'Vector', visited, provenance, cache)
    center = _named_vector_operand(node, 'Center', visited, provenance, cache)
    if not (p and center):
        return None
    rotation_type = (getattr(node, "rotation_type", "") or "AXIS_ANGLE").upper()
    invert = bool(getattr(node, "invert", False))
    offset = _vector3_node("subtract", in1=p, in2=center)

    if rotation_type == 'EULER_XYZ':
        rotation = _named_vector_operand(node, 'Rotation', visited, provenance, cache)
        if not rotation:
            return None
        return _vector3_node("add", in1=_euler_xyz_rotate(offset, rotation, inverse=invert), in2=center)

    angle = _vector_math_scalar_operand(node, 'Angle', visited, None, provenance, cache)
    if not angle:
        return None
    if invert:
        angle = _negated_float(angle)
    if rotation_type in _VECTOR_ROTATE_AXES:
        rotated = _rotate_about_unit_axis(offset, _core._constant_expr(_VECTOR_ROTATE_AXES[rotation_type]), angle)
        return _vector3_node("add", in1=rotated, in2=center)
    if rotation_type != 'AXIS_ANGLE':
        return None
    axis = _named_vector_operand(node, 'Axis', visited, provenance, cache)
    if not axis:
        return None
    value = _constant_value(axis)
    if value is not None:
        length = math.sqrt(sum(float(component) ** 2 for component in value))
        if length == 0.0:
            return p
        unit = _core._constant_expr(tuple(float(component) / length for component in value))
        rotated = _rotate_about_unit_axis(offset, unit, angle)
        return _vector3_node("add", in1=rotated, in2=center)
    unit = _vector3_node("normalize", **{"in": axis})
    rotated = _vector3_node("add", in1=_rotate_about_unit_axis(offset, unit, angle), in2=center)
    return _core._make_node_expr(
        _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
        {"value1": _vector_dot(axis, axis), "value2": _core._constant_expr(0.0), "in1": rotated, "in2": p},
    )


def _euler_xyz_rotate(p: Dict[str, Any], rotation: Dict[str, Any], inverse: bool = False) -> Dict[str, Any]:
    """Rotate ``p`` by a Blender Euler XYZ ``rotation`` (a vector3 of radians).

    Cycles' ``euler_to_transform`` is Rz * Ry * Rx, so X turns first, then Y,
    then Z, each about the fixed axis. ``inverse`` applies the transpose: Z,
    then Y, then X, each negated. A constant zero angle is skipped.
    """
    value = _constant_value(rotation)
    if value is not None:
        angles = [_core._constant_expr(float(component)) for component in list(value)[:3]]
    else:
        angles = [_core._component_expr(rotation, "vector3", axis) for axis in "xyz"]
    steps = list(zip(_VECTOR_ROTATE_AXES.values(), angles))
    if inverse:
        steps = [(axis, _negated_float(angle)) for axis, angle in reversed(steps)]
    for axis, angle in steps:
        constant = _constant_value(angle)
        if constant is not None and float(constant) == 0.0:
            continue
        p = _rotate_about_unit_axis(p, _core._constant_expr(axis), angle)
    return p


def _color3_operand_expr(expr: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Coerce a resolved expression to color3, as a Blender colour socket does."""
    if not isinstance(expr, dict):
        return None
    kind = expr.get("kind")
    if kind == "unresolved":
        return expr
    if kind == "constant":
        value = expr.get("value")
        if isinstance(value, (list, tuple)):
            parts = [float(v) for v in value][:3]
            while len(parts) < 3:
                parts.append(parts[-1] if parts else 0.0)
            return _core._constant_expr(tuple(parts))
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return _core._constant_expr((f, f, f))
    if kind == "texture":
        texture = dict(expr)
        if _texture_is_scalar(texture):
            # One channel of the file is a float; a colour socket repeats it.
            texture["output_type"] = "float"
            return _core._make_node_expr(
                _core._nodedef_for("convert", "color3", input_type="float"), {"in": texture}
            )
        texture["output_type"] = "color3"
        return texture
    if kind == "node":
        out_type = _node_expr_output_type(expr) or "color3"
        if out_type == "color3":
            return expr
        if out_type in ("float", "vector3", "color4"):
            return _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type=out_type), {"in": expr})
    return None


def _coerce_output(expr: Optional[Dict[str, Any]], expected_type: Optional[str], channel: Optional[str] = None):
    """Apply Blender's implicit socket conversion to a node expression's output.

    Colour to float is linear RGB to grey, vector to float is the mean, and
    colours and vectors convert component for component. A channel request
    from Separate XYZ or Separate Color keeps the whole value so the separate
    node reads the component itself.
    """
    if not isinstance(expr, dict) or expr.get("kind") != "node" or not expected_type:
        return expr
    out_type = _node_expr_output_type(expr)
    if not out_type or out_type == expected_type:
        return expr
    if expected_type in ("float", "half"):
        if channel or out_type == "float":
            return expr
        if out_type == "color4":
            expr = _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="color4"), {"in": expr})
            out_type = "color3"
        if out_type == "color3":
            as_vector = _core._make_node_expr(_core._nodedef_for("convert", "vector3", input_type="color3"), {"in": expr})
            return _vector_dot(as_vector, _core._constant_expr(_core._RGB_TO_GRAY))
        if out_type == "vector3":
            return _vector_dot(expr, _core._constant_expr(_core._VECTOR_MEAN))
        return expr
    if expected_type in ("color3", "vector3") and out_type in ("color3", "vector3", "float", "color4"):
        if out_type == "color4" and expected_type == "vector3":
            expr = _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="color4"), {"in": expr})
            out_type = "color3"
        return _core._make_node_expr(_core._nodedef_for("convert", expected_type, input_type=out_type), {"in": expr})
    return expr


def _float_to_expected(expr: Optional[Dict[str, Any]], expected_type: Optional[str]):
    """A float expression delivered to a consumer of ``expected_type``.

    A colour or vector consumer repeats the float in every component. A
    constant broadcasts at resolve time, so the authored input carries the
    consumer's own type; a texture or node gets an explicit convert.
    """
    if not isinstance(expr, dict) or expected_type not in ("color3", "vector3", "color4"):
        return expr
    kind = expr.get("kind")
    if kind == "constant":
        value = _core._constant_scalar(expr)
        if value is None:
            return expr
        return _core._constant_expr((value,) * (4 if expected_type == "color4" else 3))
    if kind == "texture":
        texture = dict(expr)
        texture["output_type"] = "float"
        return _core._make_node_expr(
            _core._nodedef_for("convert", expected_type, input_type="float"), {"in": texture}
        )
    if kind == "node":
        return _coerce_output(expr, expected_type)
    return expr


def _typed_result(expr: Optional[Dict[str, Any]], expected_type: Optional[str], channel: Optional[str]):
    """A node branch's natively typed result converted for its consumer.

    Constants broadcast (a float to a colour) or reduce (a colour to a float,
    unless a Separate node asked for one channel and reads it itself).
    """
    if not isinstance(expr, dict):
        return expr
    if expr.get("kind") == "constant" and expected_type:
        value = expr.get("value")
        if isinstance(value, (list, tuple)):
            if expected_type in ("float", "half") and not channel:
                return _core._float_math_input_expr(expr)
            return expr
        return _float_to_expected(expr, expected_type)
    if expr.get("kind") == "node":
        return _coerce_output(expr, expected_type, channel)
    return expr


def _named_socket(node, name):
    inputs = getattr(node, "inputs", None)
    return inputs.get(name) if inputs is not None and hasattr(inputs, "get") else None


def _named_color_operand(node, name, visited, provenance, cache, default=(0.0, 0.0, 0.0)):
    socket = _named_socket(node, name)
    if socket is None:
        return _core._constant_expr(tuple(default))
    if getattr(socket, "is_linked", False):
        resolved = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache, expected_type="color3",
        )
        return _color3_operand_expr(resolved)
    value = _core._socket_default_value(socket)
    return _color3_operand_expr(_core._constant_expr(value if value is not None else default))


def _named_float_operand(node, name, visited, provenance, cache, default: float = 0.0):
    socket = _named_socket(node, name)
    return _float_socket_expr(socket, visited, provenance, cache, default=default)


def _color3_node(name: str, **inputs: Any) -> Dict[str, Any]:
    return _core._make_node_expr(_core._nodedef_for(name, "color3", input_type="color3"), inputs)


def _broadcast_color3(value: Dict[str, Any]) -> Dict[str, Any]:
    """A float repeated into a color3, folded when it is a constant."""
    scalar = _core._constant_scalar(value)
    if scalar is not None:
        return _core._constant_expr((scalar, scalar, scalar))
    return _core._make_node_expr(_core._nodedef_for("combine3", "color3"), {"in1": value, "in2": value, "in3": value})


def _component_of(expr: Dict[str, Any], channel: str) -> Dict[str, Any]:
    """One channel of a resolved expression, as a Separate node reads it.

    A colour or vector constant folds to its component and a float constant is
    its own component; a colour, vector or color4 node gets a component read;
    a float node is every component of itself. A texture keeps the channel
    hint for the texture authoring path.
    """
    if not isinstance(expr, dict):
        return expr
    kind = expr.get("kind")
    letter = (channel or "")[:1].lower()
    index = max("rgba".find(letter), "xyzw".find(letter))
    if kind == "constant":
        value = expr.get("value")
        if isinstance(value, (list, tuple)):
            if 0 <= index < len(value):
                return _core._constant_expr(float(value[index]))
            return _core._constant_expr(1.0 if index == 3 else 0.0)
        return expr
    if kind == "node":
        out_type = _node_expr_output_type(expr)
        if out_type in ("color3", "vector3", "color4", "vector4"):
            return _core._component_expr(expr, out_type, letter)
        return expr
    if kind == "texture":
        texture = dict(expr)
        if not _texture_is_scalar(texture) and 0 <= index < 4:
            # The texture authoring path names channels by colour letter.
            texture["channel"] = "rgba"[index]
            texture["output_type"] = "float"
        return texture
    return expr


def _gamma_expr(node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Gamma node, transcribed from Cycles' ``svm_math_gamma_color``.

    Each channel above zero is raised to Gamma and the rest pass through, and
    a Gamma of exactly zero returns white. Measured by importing
    ``t32_gamma_checker`` into Reality Composer Pro 3: the
    Gamma cube matches its Math Power control face for face.
    """
    color = _named_color_operand(node, 'Color', visited, provenance, cache)
    gamma = _vector_math_scalar_operand(node, 'Gamma', visited, None, provenance, cache)
    if not color or not gamma:
        return None
    for operand in (color, gamma):
        if operand.get("kind") == "unresolved":
            return operand
    gamma_value = _constant_value(gamma)
    color_value = _constant_value(color)
    if gamma_value is not None and float(gamma_value) == 0.0:
        return _core._constant_expr((1.0, 1.0, 1.0))
    if gamma_value is not None and color_value is not None:
        g = float(gamma_value)
        return _core._constant_expr(tuple(c ** g if c > 0.0 else c for c in (float(v) for v in color_value)))
    channels = []
    for index, name in enumerate("rgb"):
        if color_value is not None:
            component = _core._constant_expr(float(color_value[index]))
        else:
            component = _core._component_expr(color, "color3", name)
        raised = _core._make_node_expr(_core._nodedef_for("power", "float"), {"in1": component, "in2": gamma})
        if color_value is not None:
            channels.append(raised if float(color_value[index]) > 0.0 else component)
        else:
            channels.append(_core._make_node_expr(
                _core._nodedef_for("ifgreater", "float"),
                {"value1": component, "value2": _core._constant_expr(0.0), "in1": raised, "in2": component},
            ))
    result = _core._make_node_expr(
        _core._nodedef_for("combine3", "color3"), {"in1": channels[0], "in2": channels[1], "in3": channels[2]}
    )
    if gamma_value is None:
        result = _core._make_node_expr(
            "ND_ifequal_color3",
            {"value1": gamma, "value2": _core._constant_expr(0.0), "in1": _core._constant_expr((1.0, 1.0, 1.0)), "in2": result},
        )
    return result


def _checker_expr(node, from_socket, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Checker Texture, transcribed from Cycles' ``svm_checker``.

    Cycles nudges the scaled coordinate by ``(p + 1e-6) * 0.999999``, floors
    each axis, and returns Color1 when ``(|x| % 2 == |y| % 2) == |z| % 2``.
    That test is true exactly when the sum of the three floors is odd, so
    Fac is the floored modulo of that sum by 2 and Color mixes Color2 toward
    Color1 by it. An unwired Vector samples the Generated stand-in, as the
    other procedural textures do. Measured by importing ``t32_gamma_checker``:
    four by four checks per face, with matching colours in opposite corners.
    """
    coordinate = _vector3_operand_expr(_core._procedural_vector_expr(node, visited, None, provenance, cache))
    scale = _vector_math_scalar_operand(node, 'Scale', visited, None, provenance, cache)
    if not coordinate or not scale:
        return None
    for operand in (coordinate, scale):
        if operand.get("kind") == "unresolved":
            return operand
    p = _vector_times_float(coordinate, scale)
    p = _vector3_node(
        "multiply",
        in1=_vector3_node("add", in1=p, in2=_core._constant_expr((1e-6, 1e-6, 1e-6))),
        in2=_core._constant_expr((0.999999, 0.999999, 0.999999)),
    )
    floors = _vector3_node("floor", **{"in": p})
    fac = _core._make_node_expr(
        _core._nodedef_for("modulo", "float"),
        {"in1": _vector_dot(floors, _core._constant_expr((1.0, 1.0, 1.0))), "in2": _core._constant_expr(2.0)},
    )
    if getattr(from_socket, "name", "") == 'Fac':
        return fac
    color1 = _named_color_operand(node, 'Color1', visited, provenance, cache, default=(0.8, 0.8, 0.8))
    color2 = _named_color_operand(node, 'Color2', visited, provenance, cache, default=(0.2, 0.2, 0.2))
    if not color1 or not color2:
        return None
    return _core._make_node_expr(_core._nodedef_for("mix", "color3"), {"fg": color1, "bg": color2, "mix": fac})


def _camera_data_expr(output_name: str) -> Optional[Dict[str, Any]]:
    """Blender's Camera Data node, from Cycles' ``svm_node_camera``.

    Cycles transforms the shading point into its camera space, which looks
    down +Z, and returns that vector normalised (View Vector), its z (View Z
    Depth) and its length (View Distance). RealityKit's world-to-view matrix
    looks down -Z, so z is negated. Measured by importing
    ``t34_camera_attribute_random`` into Reality Composer Pro 3:
    View Distance draws rings around the camera and View Z Depth
    straight bands parallel to the screen.

    The view-space point is the model-to-view matrix applied to the
    object-space position. Both are true frames; the world-space position
    reader returns anchor space while the world-to-view matrix takes the true
    world, so pairing those two (as this once did) only agreed while the
    entity's anchor was the identity, as it was in t34.
    """
    if output_name not in ('View Vector', 'View Z Depth', 'View Distance'):
        return None
    object_position = _core._make_node_expr(
        _core._nodedef_for("position", "vector3"), {"space": _core._constant_expr("object")}
    )
    model_to_view = _core._make_node_expr(
        "ND_realitykit_surface_model_to_view", {}, output="modelToView"
    )
    view = _core._make_node_expr(
        "ND_transformmatrix_vector3M4", {"in": object_position, "mat": model_to_view}
    )
    camera = _vector3_node("multiply", in1=view, in2=_core._constant_expr((1.0, 1.0, -1.0)))
    if output_name == 'View Distance':
        return _core._make_node_expr(_core._nodedef_for("magnitude", "float", input_type="vector3"), {"in": camera})
    if output_name == 'View Z Depth':
        return _core._component_expr(camera, "vector3", "z")
    return _vector3_node("normalize", **{"in": camera})


def _safe_divide_vector(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Cycles' ``safe_divide``: per component, zero wherever the divisor is zero."""
    b_value = _constant_value(b)
    if b_value is not None:
        reciprocal = tuple(1.0 / float(v) if float(v) != 0.0 else 0.0 for v in list(b_value)[:3])
        if reciprocal == (1.0, 1.0, 1.0):
            return a
        return _vector3_node("multiply", in1=a, in2=_core._constant_expr(reciprocal))
    parts = []
    for axis in "xyz":
        numerator = _core._component_expr(a, "vector3", axis)
        denominator = _core._component_expr(b, "vector3", axis)
        parts.append(_core._make_node_expr(
            _core._nodedef_for("ifequal", "float"),
            {
                "value1": denominator,
                "value2": _core._constant_expr(0.0),
                "in1": _core._constant_expr(0.0),
                "in2": _core._make_node_expr(_core._nodedef_for("divide", "float"), {"in1": numerator, "in2": denominator}),
            },
        ))
    return _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {"in1": parts[0], "in2": parts[1], "in3": parts[2]})


def _safe_normalize(v: Dict[str, Any]) -> Dict[str, Any]:
    """Cycles' ``safe_normalize``: a zero vector passes through."""
    return _core._make_node_expr(
        _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
        {"value1": _vector_dot(v, v), "value2": _core._constant_expr(0.0),
         "in1": _vector3_node("normalize", **{"in": v}), "in2": v},
    )


def _mapping_vector_expr(node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """Blender's Mapping node as vector math, from Cycles' ``svm_mapping``.

    Point: ``R(v * S) + L``. Texture: ``safe_divide(R^T (v - L), S)``.
    Vector: ``R(v * S)``. Normal: ``safe_normalize(R safe_divide(v, S))``.
    R is the Euler XYZ rotation. An unlinked Vector is its socket value, as in
    Blender. This is the path for a Mapping node that feeds anything other
    than an Image Texture's UV transform.
    """
    mapping_type = (getattr(node, "vector_type", "POINT") or "POINT").upper()
    v = _named_vector_operand(node, 'Vector', visited, provenance, cache)
    rotation = _named_vector_operand(node, 'Rotation', visited, provenance, cache)
    scale = _named_vector_operand(node, 'Scale', visited, provenance, cache)
    if not (v and rotation and scale):
        return None
    for operand in (v, rotation, scale):
        if operand.get("kind") == "unresolved":
            return operand
    location = None
    if mapping_type in ('POINT', 'TEXTURE'):
        location = _named_vector_operand(node, 'Location', visited, provenance, cache)
        if not location:
            return None
        if location.get("kind") == "unresolved":
            return location
    if mapping_type in ('POINT', 'VECTOR'):
        result = _euler_xyz_rotate(_vector3_node("multiply", in1=v, in2=scale), rotation)
        if mapping_type == 'POINT':
            result = _vector3_node("add", in1=result, in2=location)
        return result
    if mapping_type == 'TEXTURE':
        moved = _vector3_node("subtract", in1=v, in2=location)
        return _safe_divide_vector(_euler_xyz_rotate(moved, rotation, inverse=True), scale)
    if mapping_type == 'NORMAL':
        return _safe_normalize(_euler_xyz_rotate(_safe_divide_vector(v, scale), rotation))
    return None


def image_uses_uv_transform(image_node) -> bool:
    """Whether a Flat Image Texture's coordinates are UVs, optionally through
    one constant Point Mapping node that ``place2d`` can carry.

    Those images keep RealityKit's UV reader and single texture transform,
    the path the kit's texture-transform scene verifies. Every other
    coordinate - Vector Math, Generated, a Texture, Vector or Normal Mapping,
    a Mapping with linked sockets, X or Y rotation or a zero X or Y scale, or
    one fed by something other than UVs - is computed in the graph and read
    through ``_computed_image_expr``.

    A Texture Mapping is ``safe_divide(R^T (v - L), S)``: it translates
    before it rotates and scales. ``place2d`` says that only through its TRS
    ``operationorder``, which RealityKit's nodedef does not declare, so the
    default SRT order ran and the image sampled the wrong texels.
    """
    socket = _named_socket(image_node, "Vector")
    if socket is None or not getattr(socket, "is_linked", False):
        return True
    node, output = _core._upstream_through_reroutes(socket)
    if getattr(node, "type", "") == 'MAPPING':
        if (getattr(node, "vector_type", "POINT") or "POINT").upper() != 'POINT':
            return False
        for name in ('Location', 'Rotation', 'Scale'):
            component = _named_socket(node, name)
            if component is not None and getattr(component, "is_linked", False):
                return False
        rotation = getattr(_named_socket(node, 'Rotation'), "default_value", (0.0, 0.0, 0.0))
        scale = getattr(_named_socket(node, 'Scale'), "default_value", (1.0, 1.0, 1.0))
        if abs(float(rotation[0])) > 1e-6 or abs(float(rotation[1])) > 1e-6:
            return False
        if abs(float(scale[0])) <= 1e-8 or abs(float(scale[1])) <= 1e-8:
            return False
        mapping_vector = _named_socket(node, "Vector")
        if mapping_vector is None or not getattr(mapping_vector, "is_linked", False):
            return False
        node, output = _core._upstream_through_reroutes(mapping_vector)
    node_type = getattr(node, "type", "")
    if node_type == 'UVMAP':
        return not getattr(node, "from_instancer", False)
    return node_type == 'TEX_COORD' and getattr(output, "name", "") == 'UV'


def _image_file_spec(image_node) -> Optional[Dict[str, Any]]:
    """The file an Image Texture reads, as a filename input for a graph node."""
    image = getattr(image_node, "image", None)
    texture_path = _core._resolve_image_path(image)
    if not texture_path:
        return None
    try:
        colorspace = _core._normalize_colorspace(image.colorspace_settings.name) if image else None
    except Exception:
        colorspace = None
    spec = {"kind": "file_asset", "type": "file_asset", "path": texture_path, "colorspace": colorspace}
    try:
        mode = str(getattr(image, "alpha_mode", "") or "").upper()
    except Exception:
        mode = ""
    spec["alpha_mode"] = {"PREMUL": "premul", "STRAIGHT": "straight"}.get(mode, mode.lower() or None)
    spec.update(_core._image_source_alpha(image, texture_path))
    return spec


def _image_coordinate_expr(image_node, visited, provenance, cache) -> Optional[Dict[str, Any]]:
    """The coordinate a computed image read samples: the linked Vector, or UVs."""
    socket = _named_socket(image_node, "Vector")
    if socket is not None and getattr(socket, "is_linked", False):
        resolved = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache, expected_type="vector3",
        )
        return _vector3_operand_expr(resolved)
    return _core._make_node_expr(_core._nodedef_for("texcoord", "vector3"), {})


def _sample_image(image_node, file_spec, coordinate, read_type, alpha_only=False) -> Dict[str, Any]:
    file_input = dict(file_spec)
    if alpha_only:
        # Alpha is scalar data whatever the file's RGB colour space; the
        # colour-space policy exempts an alpha read, as it does on the UV path.
        file_input["channel"] = "a"
    inputs: Dict[str, Any] = {
        "file": file_input,
        "texcoord": _core._make_node_expr(_core._nodedef_for("convert", "vector2", input_type="vector3"), {"in": coordinate}),
    }
    for name, value in sorted((_core._image_node_sampling(image_node) or {}).items()):
        inputs[name] = _core._constant_expr(value)
    return _core._make_node_expr(_core._nodedef_for("image", read_type), inputs)


def _alpha_output_linked(image_node) -> bool:
    for socket in getattr(image_node, "outputs", None) or []:
        if getattr(socket, "name", "") == "Alpha":
            return bool(getattr(socket, "is_linked", False))
    return False


def image_premultiplies_color(image_node) -> Optional[str]:
    """How Cycles premultiplies an image's Color output, or None when it does not.

    Cycles stores every image with associated alpha and divides it back out
    only when the node's Alpha output is used. So a straight-alpha image whose
    Alpha output is unused hands Color already multiplied by alpha. For an
    sRGB image the multiply happens on the encoded values, before the sRGB
    decode, for 8-bit and float files alike; for a linear image it happens on
    the linear values. Measured by baking a PNG texel (230, 51, 26, 128) in
    Cycles on Blender 5.2: Color reads decode(encoded x alpha) = 0.1729 at 16
    bits (0.1714 at 8 bits, which quantizes the product), where the plain
    decode is 0.7913. Non-Color, Channel Packed and alpha-mode None images are
    never premultiplied, and an image without an alpha channel has nothing to
    multiply. EEVEE differs (it premultiplies after the decode); the export
    follows Cycles.

    Returns ``"encoded"`` for an sRGB image, ``"linear"`` otherwise.
    """
    image = getattr(image_node, "image", None)
    if image is None or _alpha_output_linked(image_node):
        return None
    mode = str(getattr(image, "alpha_mode", "") or "").upper()
    if mode != "STRAIGHT":
        return None
    try:
        colorspace = _core._normalize_colorspace(image.colorspace_settings.name)
    except Exception:
        return None
    if colorspace in (None, "raw") or str(colorspace).startswith("unsupported:"):
        return None
    try:
        path = _core._resolve_image_path(image)
    except Exception:
        path = None
    if _core._image_source_alpha(image, path).get("source_has_alpha") is not True:
        return None
    return "encoded" if colorspace == "srgb" else "linear"


def _srgb_decode_color3(expr: Dict[str, Any]) -> Dict[str, Any]:
    """The sRGB transfer function Blender's colour config applies, per channel."""
    channels = []
    for channel in "rgb":
        c = _core._component_expr(expr, "color3", channel)
        linear_part = _core._make_node_expr(_core._nodedef_for("divide", "float"), {"in1": c, "in2": _core._constant_expr(12.92)})
        curve = _core._make_node_expr(_core._nodedef_for("power", "float"), {
            "in1": _core._make_node_expr(_core._nodedef_for("divide", "float"), {
                "in1": _core._make_node_expr(_core._nodedef_for("add", "float"), {"in1": c, "in2": _core._constant_expr(0.055)}),
                "in2": _core._constant_expr(1.055),
            }),
            "in2": _core._constant_expr(2.4),
        })
        channels.append(_core._make_node_expr(
            _core._nodedef_for("ifgreater", "float"),
            {"value1": c, "value2": _core._constant_expr(0.04045), "in1": curve, "in2": linear_part},
        ))
    return _core._make_node_expr(_core._nodedef_for("combine3", "color3"), {"in1": channels[0], "in2": channels[1], "in3": channels[2]})


def _sample_color(image_node, file_spec, coordinate) -> Dict[str, Any]:
    """An image's Color output at ``coordinate``, premultiplied as Cycles hands it."""
    premultiply = image_premultiplies_color(image_node)
    if premultiply is None:
        return _sample_image(image_node, file_spec, coordinate, "color3")
    spec = dict(file_spec)
    if premultiply == "encoded":
        # Read the encoded values, multiply, then decode as Cycles does.
        spec["colorspace"] = "raw"
        spec["colorspace_role"] = "data"
        spec["channel"] = "a"
    rgba = _sample_image(image_node, spec, coordinate, "color4")
    rgb = _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="color4"), {"in": rgba})
    alpha = _core._component_expr(rgba, "color4", "a")
    premultiplied = _core._make_node_expr(
        _core._nodedef_for("multiply", "color3"),
        {"in1": rgb, "in2": _core._make_node_expr(_core._nodedef_for("combine3", "color3"), {"in1": alpha, "in2": alpha, "in3": alpha})},
    )
    return _srgb_decode_color3(premultiplied) if premultiply == "encoded" else premultiplied


def _image_read_refusal(file_spec, provenance, image_node=None) -> Optional[Dict[str, Any]]:
    if str(file_spec.get("alpha_mode") or "") == "premul" and (image_node is None or _alpha_output_linked(image_node)):
        return {
            "kind": "unresolved",
            "provenance": list(provenance),
            "reason": (
                "a premultiplied image whose Alpha output is used would have to be "
                "unassociated as Blender does, which the computed read does not do; set the "
                "image's Alpha to Straight or bake the material"
            ),
        }
    return None


def _computed_image_expr(image_node, from_socket, visited, provenance, cache, expected_type, channel,
                         coordinate=None):
    """An Flat Image Texture sampled at a coordinate computed in the graph.

    Cycles samples ``co.xy`` of the Vector input; unwired, that is the UVs.
    The Alpha output reads the fourth channel, and a file without one reads
    1, as Blender does. Measured by importing ``t33_computed_image_coordinates``
    into Reality Composer Pro 3: coordinates from Vector
    Math, and from a Mapping with a linked Location, show the same grid as the
    UV transform control. ``coordinate`` overrides the Vector input, for a
    node that projects it first.
    """
    file_spec = _image_file_spec(image_node)
    if not file_spec:
        return None
    refusal = _image_read_refusal(file_spec, provenance, image_node)
    if refusal:
        return refusal
    if coordinate is None:
        coordinate = _image_coordinate_expr(image_node, visited, provenance, cache)
    if not coordinate:
        return None
    if coordinate.get("kind") == "unresolved":
        return coordinate
    if _core._image_channel_from_output_socket(from_socket) == "a":
        if file_spec.get("source_has_alpha") is False:
            return _core._constant_expr((1.0, 1.0, 1.0) if expected_type in ("color3", "vector3") else 1.0)
        return _core._component_expr(_sample_image(image_node, file_spec, coordinate, "color4", alpha_only=True), "color4", "a")
    return _coerce_output(_sample_color(image_node, file_spec, coordinate), expected_type, channel)


def _environment_texture_expr(node, from_socket, visited, provenance, cache, expected_type, channel):
    """Blender's Environment Texture on a surface, from Cycles' ``svm_node_tex_environment``.

    Cycles normalises the Vector input (unwired, the world-space position:
    the socket is ``LINK_POSITION``) and maps the direction to image
    coordinates with ``direction_to_equirectangular``,
    ``u = (atan2(y, x) - pi) / -2pi`` and ``v = (acos(z) - pi) / -pi`` (a zero
    vector maps to (0, 0)), or ``direction_to_mirrorball``. The image always
    repeats. Before this it was sampled by UV, as if it were an Image Texture.
    """
    socket = _named_socket(node, "Vector")
    if socket is not None and getattr(socket, "is_linked", False):
        co = _vector3_operand_expr(_core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache, expected_type="vector3",
        ))
        if not co or co.get("kind") == "unresolved":
            return co
    else:
        co = _geometry_reader_expr('Position')
    direction = _safe_normalize(co)

    def component(axis):
        return _core._component_expr(direction, "vector3", axis)

    def f(op, a, b):
        return _core._make_node_expr(_core._nodedef_for(op, "float"), {"in1": a, "in2": b})

    projection = str(getattr(node, "projection", "EQUIRECTANGULAR") or "EQUIRECTANGULAR").upper()
    if projection == 'EQUIRECTANGULAR':
        z = _core._make_node_expr(_core._nodedef_for("clamp", "float"), {
            "in": component("z"), "low": _core._constant_expr(-1.0), "high": _core._constant_expr(1.0),
        })
        # compatible_atan2: straight up or down, x and y are both 0 and Cycles
        # reads u = 0.5 where RealityKit's bare atan2 gives NaN.
        u = f("divide", f("subtract", _core._compatible_atan2_float(component("y"), component("x")), _core._constant_expr(math.pi)),
              _core._constant_expr(-2.0 * math.pi))
        v = f("divide", f("subtract", _core._make_node_expr(_core._nodedef_for("acos", "float"), {"in": z}),
                          _core._constant_expr(math.pi)), _core._constant_expr(-math.pi))
        uv = _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {"in1": u, "in2": v, "in3": _core._constant_expr(0.0)})
        coordinate = _core._make_node_expr(
            _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
            {"value1": _vector_dot(co, co), "value2": _core._constant_expr(0.0), "in1": uv, "in2": _core._constant_expr((0.0, 0.0, 0.0))},
        )
    elif projection == 'MIRROR_BALL':
        shifted = _vector3_node("subtract", in1=direction, in2=_core._constant_expr((0.0, 1.0, 0.0)))
        y = _core._component_expr(shifted, "vector3", "y")
        root = _core._make_node_expr(_core._nodedef_for("sqrt", "float"), {
            "in": _core._make_node_expr(_core._nodedef_for("max", "float"), {
                "in1": f("multiply", _core._constant_expr(-0.5), y), "in2": _core._constant_expr(0.0),
            }),
        })
        div = f("multiply", _core._constant_expr(2.0), root)
        scaled = _core._make_node_expr(
            _core._nodedef_for("ifgreater", "vector3", input_type="vector3"),
            {"value1": div, "value2": _core._constant_expr(0.0),
             "in1": _vector_times_float(shifted, f("divide", _core._constant_expr(1.0), div)), "in2": shifted},
        )
        coordinate = _vector3_node(
            "multiply",
            in1=_vector3_node("add", in1=scaled, in2=_core._constant_expr((1.0, 1.0, 1.0))),
            in2=_core._constant_expr((0.5, 0.5, 0.5)),
        )
        # (u, v) = 0.5 (x + 1, z + 1)
        coordinate = _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {
            "in1": _core._component_expr(coordinate, "vector3", "x"),
            "in2": _core._component_expr(coordinate, "vector3", "z"),
            "in3": _core._constant_expr(0.0),
        })
    else:
        return {
            "kind": "unresolved",
            "provenance": list(provenance),
            "reason": f"Environment Texture projection {projection} is not exported; bake the material",
        }
    return _computed_image_expr(node, from_socket, visited, provenance, cache, expected_type, channel,
                                coordinate=coordinate)


def _box_projection_expr(image_node, from_socket, visited, provenance, cache, expected_type, channel):
    """A Box-projected Image Texture, transcribed from Cycles' ``svm_node_tex_image_box``.

    The object-space shading normal, made absolute and normalised to sum to
    one, picks a weight per axis: a single side inside the corner zones, and,
    with Projection Blend above zero, a linear blend of two or three sides in
    between. Each side samples the image at two components of the Vector
    input (UVs when unwired), flipped on the sides Cycles flips so no side
    is mirrored. The weighted samples are summed, alpha included. Measured by
    importing ``t33_computed_image_coordinates``: the box-projected cube and
    sphere place every quadrant and blended seam as a Cycles render of the
    scene does.
    """
    file_spec = _image_file_spec(image_node)
    if not file_spec:
        return None
    refusal = _image_read_refusal(file_spec, provenance, image_node)
    if refusal:
        return refusal
    co = _image_coordinate_expr(image_node, visited, provenance, cache)
    if not co:
        return None
    if co.get("kind") == "unresolved":
        return co
    blend = float(getattr(image_node, "projection_blend", 0.0) or 0.0)
    limit = 0.5 * (1.0 + blend)

    signed = _core._make_node_expr(_core._nodedef_for("normal", "vector3"), {"space": _core._constant_expr("object")})
    absolute = _vector3_node("absval", **{"in": signed})
    total = _vector_dot(absolute, _core._constant_expr((1.0, 1.0, 1.0)))
    n = _vector3_node("divide", in1=absolute, in2=_core._make_node_expr(
        _core._nodedef_for("combine3", "vector3"), {"in1": total, "in2": total, "in3": total}))
    nx, ny, nz = (_core._component_expr(n, "vector3", axis) for axis in "xyz")
    sx, sy, sz = (_core._component_expr(signed, "vector3", axis) for axis in "xyz")

    def f(op, a, b):
        return _core._fold_float(op, a, b) if op in ("add", "subtract", "multiply") else _core._make_node_expr(
            _core._nodedef_for(op, "float"), {"in1": a, "in2": b})

    def c(value):
        return _core._constant_expr(float(value))

    def greater(a, b, when_true, when_false, kind="vector3"):
        return _core._make_node_expr(
            _core._nodedef_for("ifgreater", kind, input_type=kind if kind != "float" else None),
            {"value1": a, "value2": b, "in1": when_true, "in2": when_false},
        )

    def both_greater(a1, b1, a2, b2, when_true, when_false):
        return greater(a1, b1, greater(a2, b2, when_true, when_false), when_false)

    def weights(wx, wy, wz):
        return _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {"in1": wx, "in2": wy, "in3": wz})

    def saturate(x):
        return _core._make_node_expr(_core._nodedef_for("clamp", "float"), {"in": x, "low": c(0.0), "high": c(1.0)})

    zero, one = c(0.0), c(1.0)
    if blend > 0.0:
        def two_way(a, b):
            ratio = f("divide", a, f("add", a, b))
            return saturate(f("divide", f("subtract", ratio, c(0.5 * (1.0 - blend))), c(blend)))

        wxy = two_way(nx, ny)
        wyz = two_way(ny, nz)
        wxz = two_way(nx, nz)

        def three(ni):
            return f("divide", f("add", f("multiply", c(2.0 - limit), ni), c(limit - 1.0)), c(2.0 * limit - 1.0))

        edge = 1.0 - limit
        between = greater(
            f("multiply", c(edge), f("add", ny, nx)), nz,
            weights(wxy, f("subtract", one, wxy), zero),
            greater(
                f("multiply", c(edge), f("add", ny, nz)), nx,
                weights(zero, wyz, f("subtract", one, wyz)),
                greater(
                    f("multiply", c(edge), f("add", nx, nz)), ny,
                    weights(wxz, zero, f("subtract", one, wxz)),
                    weights(three(nx), three(ny), three(nz)),
                ),
            ),
        )
    else:
        between = _core._constant_expr((1.0, 0.0, 0.0))

    weight = both_greater(
        nx, f("multiply", c(limit), f("add", nx, ny)), nx, f("multiply", c(limit), f("add", nx, nz)),
        _core._constant_expr((1.0, 0.0, 0.0)),
        both_greater(
            ny, f("multiply", c(limit), f("add", nx, ny)), ny, f("multiply", c(limit), f("add", ny, nz)),
            _core._constant_expr((0.0, 1.0, 0.0)),
            both_greater(
                nz, f("multiply", c(limit), f("add", nx, nz)), nz, f("multiply", c(limit), f("add", ny, nz)),
                _core._constant_expr((0.0, 0.0, 1.0)),
                between,
            ),
        ),
    )
    cx, cy, cz = (_core._component_expr(co, "vector3", axis) for axis in "xyz")

    def flipped(sign_test_positive, sign, value):
        mirrored = f("subtract", one, value)
        if sign_test_positive:
            return greater(sign, zero, mirrored, value, kind="float")
        return greater(zero, sign, mirrored, value, kind="float")

    def uv(u, v):
        return weights(u, v, zero)

    sides = (
        ("x", uv(flipped(False, sx, cy), cz)),
        ("y", uv(flipped(True, sy, cx), cz)),
        ("z", uv(flipped(True, sz, cy), cx)),
    )
    wants_alpha = _core._image_channel_from_output_socket(from_socket) == "a"
    if wants_alpha and file_spec.get("source_has_alpha") is False:
        return _core._constant_expr(1.0)
    read_type = "color4" if wants_alpha else "color3"
    summed = None
    for axis, coordinate in sides:
        w = _core._component_expr(weight, "vector3", axis)
        broadcast = _core._make_node_expr(
            _core._nodedef_for("combine4" if read_type == "color4" else "combine3", read_type),
            {"in1": w, "in2": w, "in3": w, **({"in4": w} if read_type == "color4" else {})},
        )
        sample = (
            _sample_image(image_node, file_spec, coordinate, read_type, alpha_only=True)
            if wants_alpha else _sample_color(image_node, file_spec, coordinate)
        )
        term = _core._make_node_expr(_core._nodedef_for("multiply", read_type), {"in1": sample, "in2": broadcast})
        summed = term if summed is None else _core._make_node_expr(_core._nodedef_for("add", read_type), {"in1": summed, "in2": term})
    if wants_alpha:
        return _core._component_expr(summed, "color4", "a")
    return _coerce_output(summed, expected_type, channel)


#: The primvar the export writes on every mesh that reads Object Info >
#: Random, holding Cycles' per-object random number (see
#: ``postprocess_usd._author_object_random``).
OBJECT_RANDOM_PRIMVAR = "blenderObjectRandom"


def cycles_object_random(object_name: str) -> float:
    """Cycles' Object Info Random for a non-instanced object, as a float32.

    ``hash_uint2(hash_string(name), 0) / 0xFFFFFFFF``: Jenkins' lookup3 final
    mix over a ``i * 37 + byte`` string hash of the UTF-8 name. Measured
    against Cycles by baking the node on objects with ASCII and non-ASCII
    names; the string hash reads bytes unsigned.
    """
    import struct

    mask = 0xFFFFFFFF

    def rot(x, k):
        return ((x << k) | (x >> (32 - k))) & mask

    key = 0
    for byte in object_name.encode("utf-8"):
        key = (key * 37 + byte) & mask
    a = b = c = (0xDEADBEEF + (2 << 2) + 13) & mask
    a = (a + key) & mask
    c ^= b; c = (c - rot(b, 14)) & mask  # noqa: E702
    a ^= c; a = (a - rot(c, 11)) & mask  # noqa: E702
    b ^= a; b = (b - rot(a, 25)) & mask  # noqa: E702
    c ^= b; c = (c - rot(b, 16)) & mask  # noqa: E702
    a ^= c; a = (a - rot(c, 4)) & mask  # noqa: E702
    b ^= a; b = (b - rot(a, 14)) & mask  # noqa: E702
    c ^= b; c = (c - rot(b, 24)) & mask  # noqa: E702

    def f32(x):
        return struct.unpack("f", struct.pack("f", x))[0]

    return f32(f32(float(c)) * f32(1.0 / 0xFFFFFFFF))


def _object_info_expr(from_socket, provenance) -> Dict[str, Any]:
    """Object Info > Random reads the per-mesh primvar the export writes.

    Measured by importing ``t34_camera_attribute_random``: four cubes sharing
    one material show four greys in the order Cycles' values put them.

    The other outputs have no value the export carries and are refused by name.
    """
    output_name = getattr(from_socket, "name", "") or ""
    if output_name == 'Random':
        return _core._make_node_expr(
            _core._nodedef_for("geompropvalue", "float"),
            {"geomprop": _core._constant_expr(OBJECT_RANDOM_PRIMVAR), "default": _core._constant_expr(0.0)},
        )
    return {
        "kind": "unresolved",
        "provenance": list(provenance),
        "reason": f"Object Info's {output_name} output is not exported; only Random is, so bake the material",
    }


#: Blender attribute types the Attribute node exports, and the primvar read.
_ATTRIBUTE_PRIMVAR_READS = {"FLOAT": "float", "FLOAT_VECTOR": "vector3"}
_ATTRIBUTE_COLOR_TYPES = frozenset({"FLOAT_COLOR", "BYTE_COLOR"})
#: Domains Blender writes as vertex or faceVarying primvars. A face-domain
#: attribute becomes a uniform primvar, which no import has verified.
_ATTRIBUTE_READABLE_DOMAINS = frozenset({"POINT", "CORNER"})


def _material_meshes(node):
    """Meshes that use the material owning ``node``; None outside Blender."""
    try:
        import bpy
        meshes = bpy.data.meshes
        materials = bpy.data.materials
    except Exception:
        return None
    node_tree = getattr(node, "id_data", None)
    owners = [material for material in materials if getattr(material, "node_tree", None) == node_tree]
    found = []
    for mesh in meshes:
        mesh_materials = [m for m in (getattr(mesh, "materials", []) or []) if m is not None]
        if any(any(candidate == owner for owner in owners) for candidate in mesh_materials):
            found.append(mesh)
    return found


def _attribute_description(node, name: str):
    """What ``name`` is on the meshes using this material.

    ``("uv", None, "CORNER")``, ``("color", data_type, domain)``,
    ``("generic", data_type, domain)``, or None when no such mesh carries it.
    Outside a live Blender session the graph is trusted as a float attribute.
    """
    meshes = _core._material_meshes(node)
    if meshes is None:
        return ("generic", "FLOAT", "POINT")
    for mesh in meshes:
        uv_layers = getattr(mesh, "uv_layers", None)
        if uv_layers is not None and uv_layers.get(name) is not None:
            return ("uv", None, "CORNER")
        attributes = getattr(mesh, "attributes", None)
        attribute = attributes.get(name) if attributes is not None else None
        if attribute is None:
            continue
        data_type = (getattr(attribute, "data_type", "") or "").upper()
        domain = (getattr(attribute, "domain", "") or "").upper()
        kind = "color" if data_type in _ATTRIBUTE_COLOR_TYPES else "generic"
        return (kind, data_type, domain)
    return None


def _attribute_node_expr(node, from_socket, provenance) -> Dict[str, Any]:
    """Blender's Attribute node for Geometry attributes, from Cycles' ``svm_node_attr``.

    Color and Vector carry the attribute's three components (a float
    repeats), Fac is the float itself or the mean of three components (the
    first for a UV map), and Alpha is a colour's fourth channel and 1 for
    everything else. Colour attributes read through RealityKit's vertex-colour
    reader, as the Color Attribute node does; floats and vectors read their
    primvar by name. Measured by importing ``t34_camera_attribute_random``: a
    float point attribute matches its object-position control.
    """
    output_name = getattr(from_socket, "name", "") or ""
    name = (getattr(node, "attribute_name", "") or "").strip()
    attribute_type = (getattr(node, "attribute_type", "GEOMETRY") or "GEOMETRY").upper()

    def refuse(reason):
        return {"kind": "unresolved", "provenance": list(provenance), "reason": reason}

    if attribute_type != 'GEOMETRY':
        return refuse(
            f"Attribute node type {attribute_type.title()} has no value in the export; "
            "only Geometry attributes are exported, so bake the material"
        )
    if not name:
        return refuse("Attribute node names no attribute; type the attribute name on the node")
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        return refuse(
            f"Attribute name '{name}' is not a valid USD primvar identifier (letters, digits, "
            "underscore; not starting with a digit); rename the attribute"
        )
    found = _core._attribute_description(node, name)
    if found is None:
        return refuse(
            f"Attribute '{name}' was not found on a mesh using this material, so the export "
            f"carries no primvars:{name}; add the attribute or fix the name"
        )
    kind, data_type, domain = found
    one = _core._constant_expr(1.0)
    if kind == "uv":
        uv_name, refusal = _core.uv_set_resolution(node, name)
        if refusal is not None:
            return refuse(refusal)
        value = _core._uv_set_expr(uv_name)
        fac, alpha = _core._component_expr(value, "vector3", "x"), one
    elif kind == "color":
        index = _core._color_attribute_set_index(node, name)
        if index is None:
            return refuse(
                f"Color attribute '{name}' is not the mesh's first colour attribute. RealityKit "
                "addresses vertex colours by set index, so only the first is exportable; reorder "
                "the attributes or bake the material"
            )
        read = _core._make_node_expr(_core._nodedef_for("geomcolor", "color4"), {"index": _core._constant_expr(index)})
        value = _core._make_node_expr(
            _core._nodedef_for("convert", "vector3", input_type="color3"),
            {"in": _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="color4"), {"in": read})},
        )
        fac = _vector_dot(value, _core._constant_expr((1 / 3, 1 / 3, 1 / 3)))
        alpha = _core._component_expr(read, "color4", "a")
    else:
        if domain not in _ATTRIBUTE_READABLE_DOMAINS:
            return refuse(
                f"Attribute '{name}' is stored on the {domain.title()} domain, which exports as a "
                "primvar RealityKit has not been shown to read; store it on Point or Face Corner, "
                "or bake the material"
            )
        read_type = _ATTRIBUTE_PRIMVAR_READS.get(data_type)
        if read_type is None:
            return refuse(
                f"Attribute '{name}' is a {data_type.replace('_', ' ').title()} attribute; only "
                "Float, Vector and Color attributes are exported, so convert it or bake the material"
            )
        read = _core._make_node_expr(_core._nodedef_for("geompropvalue", read_type), {"geomprop": _core._constant_expr(name)})
        if read_type == "float":
            value = _core._make_node_expr(_core._nodedef_for("combine3", "vector3"), {"in1": read, "in2": read, "in3": read})
            fac = read
        else:
            value = read
            fac = _vector_dot(read, _core._constant_expr((1 / 3, 1 / 3, 1 / 3)))
        alpha = one
    if output_name == 'Fac':
        return fac
    if output_name == 'Alpha':
        return alpha
    if output_name == 'Color':
        return _core._make_node_expr(_core._nodedef_for("convert", "color3", input_type="vector3"), {"in": value})
    return value
