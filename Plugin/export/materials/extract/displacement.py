"""Vertex displacement, exported as a RealityKit geometry modifier."""

from typing import Any, Dict, List, Optional, Set, Tuple

from ..graph import texture_colorspace_role

from . import core as _core


# ---------------------------------------------------------------------------
# Vertex displacement: the Material Output's Displacement socket, exported as
# RealityKit's geometry modifier.
#
# ``ND_realitykit_geometrymodifier_2_0_vertexshader`` takes a model-space
# ``modelPositionOffset``; its normal and bitangent inputs default to the
# object-space geometry properties ``Nobject`` and ``Bobject`` (measured in
# the shipped nodedef). Blender's Displacement node in Object space is the
# same quantity: the object-space normal times (height - midlevel) * scale,
# so scaling the object scales the offset. World space and tangent-space
# vector displacement have no model-space form this exporter has measured,
# so they are refused with the remedy named. Normals are not recomputed
# after the offset - the modifier moves vertices only - which every export
# says in a warning.
# ---------------------------------------------------------------------------

_DISPLACEMENT_NODE_TYPES = frozenset({'DISPLACEMENT', 'VECTOR_DISPLACEMENT'})


def _active_output_node(material):
    """The Material Output Cycles renders, or None when it has none.

    Cycles takes the active output whose target is All or Cycles
    (``get_output_node('CYCLES')``), so an EEVEE-only output is not it even
    when active. Stand-in trees without that method take the active output.
    """
    node_tree = getattr(material, "node_tree", None)
    get_output_node = getattr(node_tree, "get_output_node", None)
    if callable(get_output_node):
        return get_output_node('CYCLES')
    nodes = getattr(node_tree, "nodes", None)
    if not nodes:
        return None
    output_nodes = [n for n in nodes if getattr(n, "type", "") == 'OUTPUT_MATERIAL']
    if not output_nodes:
        return None
    for node in output_nodes:
        if getattr(node, "is_active_output", False):
            return node
    return output_nodes[0]


def surface_output_refusal(material) -> Optional[str]:
    """Why a material has no surface for Cycles to render, if it has none."""
    output = _active_output_node(material)
    if output is None:
        return (
            "Material has no Material Output that Cycles renders (target All or Cycles), so "
            "there is no surface to export. Add one, or set the output's target to All"
        )
    surface = output.inputs.get('Surface') if hasattr(output, "inputs") else None
    if surface is None or not getattr(surface, "is_linked", False) or not surface.links:
        return (
            f"Nothing is linked to Surface on '{getattr(output, 'name', 'Material Output')}', "
            "so Cycles renders no surface and there is none to export. Link a shader to Surface"
        )
    return None


#: Shader sockets whose value lands on a colour input of the exported surface.
#: Every other shader socket, and the Displacement output, is data.
_COLOR_SHADER_SOCKETS = frozenset(
    {"Base Color", "Color", "Emission Color", "Sheen Tint", "Specular Tint", "Coat Tint", "Edge Tint"}
)


def srgb_data_image_notices(material) -> List[Tuple[Any, str]]:
    """``(image node, notice)`` for each sRGB image whose colour reaches a data input.

    The export reads an sRGB image decoded wherever it lands, as Cycles does,
    but a data map left at Blender's default sRGB is usually mistagged. An
    image is on a data input when a data socket of the rendered surface's
    shaders, or the Displacement output, lies downstream of its colour through
    any nodes; the whole chain takes the role of the surface input it ends in,
    as the exported graph does. The Alpha output is never decoded.
    """
    output = _active_output_node(material)
    if output is None or not hasattr(output, "inputs"):
        return []
    notices: List[Tuple[Any, str]] = []
    reported: Set[int] = set()
    visited: Set[Tuple[int, Optional[str]]] = set()

    def is_shader(node) -> bool:
        return any(getattr(out, "type", "") == 'SHADER' for out in getattr(node, "outputs", ()) or ())

    def visit_socket(socket, role: Optional[str]) -> None:
        if socket is None or not getattr(socket, "is_linked", False):
            return
        for link in getattr(socket, "links", ()) or ():
            if getattr(link, "is_muted", False):
                continue
            node = getattr(link, "from_node", None)
            if node is None:
                continue
            if getattr(node, "type", "") in ('TEX_IMAGE', 'TEX_ENVIRONMENT'):
                from_name = getattr(getattr(link, "from_socket", None), "name", "")
                image = getattr(node, "image", None)
                space = getattr(getattr(image, "colorspace_settings", None), "name", "")
                if role == "data" and from_name != "Alpha" and space == "sRGB" and id(node) not in reported:
                    reported.add(id(node))
                    notices.append((node, (
                        f"Image '{getattr(image, 'name', '?')}' is tagged sRGB and feeds a data input, "
                        "so its values are decoded from sRGB first, as Cycles does. Set it to "
                        "Non-Color if it holds data rather than colour."
                    )))
            visit_node(node, role)

    def visit_node(node, role: Optional[str]) -> None:
        key = (id(node), role)
        if key in visited:
            return
        visited.add(key)
        shader = is_shader(node)
        for socket in getattr(node, "inputs", ()) or ():
            if shader:
                if getattr(socket, "type", "") == 'SHADER':
                    socket_role = None
                else:
                    name = getattr(socket, "name", "")
                    colour = name in _COLOR_SHADER_SOCKETS or texture_colorspace_role(name) == "color"
                    socket_role = "color" if colour else "data"
            else:
                socket_role = role
            visit_socket(socket, socket_role)

    visit_socket(output.inputs.get('Surface'), None)
    visit_socket(output.inputs.get('Displacement'), "data")
    return notices


def displacement_source(material):
    """``(active_output, node feeding its Displacement socket)``; the node is None when unlinked."""
    output = _active_output_node(material)
    if output is None:
        return None, None
    socket = output.inputs.get('Displacement') if hasattr(output, "inputs") else None
    if not socket or not getattr(socket, "is_linked", False) or not socket.links:
        return output, None
    return output, getattr(socket.links[0], "from_node", None)


def displacement_refusal(material) -> Optional[str]:
    """Why the material's Displacement cannot become a geometry modifier, or None."""
    _output, node = _core.displacement_source(material)
    if node is None:
        return None
    method = (getattr(material, "displacement_method", "BUMP") or "BUMP").upper()
    if method == 'BUMP':
        return (
            "Displacement Method is Bump Only, which only shades the surface; set it to "
            "Displacement to move vertices, or bake the material."
        )
    node_type = getattr(node, "type", "")
    label = getattr(node, "name", node_type)
    if node_type not in _DISPLACEMENT_NODE_TYPES:
        return (
            f"Displacement is fed by '{label}' rather than a Displacement or Vector "
            "Displacement node; wire one in, or bake the material."
        )
    space = (getattr(node, "space", "OBJECT") or "OBJECT").upper()
    if node_type == 'DISPLACEMENT':
        if space != 'OBJECT':
            return (
                f"Displacement '{label}' is in World space; RealityKit offsets vertices in "
                "object space. Set Space to Object, or bake the material."
            )
        normal = node.inputs.get('Normal') if hasattr(node, "inputs") else None
        if normal is not None and getattr(normal, "is_linked", False):
            return (
                f"Displacement '{label}' has a linked Normal; the offset follows the mesh "
                "normal only. Unlink it, or bake the material."
            )
        return None
    if space != 'OBJECT':
        return (
            f"Vector Displacement '{label}' is in {space.capitalize()} space; only Object "
            "space has a measured RealityKit form. Set Space to Object, or bake the material."
        )
    return None


def displacement_notices(material) -> List[str]:
    """Warnings an exportable Displacement always carries."""
    _output, node = _core.displacement_source(material)
    if node is None:
        return []
    notices = [
        "Displacement exports as a RealityKit geometry modifier that moves vertices only: "
        "normals are not recomputed, so lighting follows the undisplaced surface, and the "
        "mesh needs enough vertices to show the shape."
    ]
    method = (getattr(material, "displacement_method", "BUMP") or "BUMP").upper()
    if method == 'BOTH':
        notices.append(
            "Displacement Method is Both; the bump half is dropped, only the vertex offset "
            "is exported."
        )
    return notices


def _fold_float(op: str, a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """A float math node, folded when both operands are constants."""
    if (
        isinstance(a, dict) and isinstance(b, dict)
        and a.get("kind") == "constant" and b.get("kind") == "constant"
    ):
        try:
            x, y = float(a["value"]), float(b["value"])
        except (TypeError, ValueError):
            x = y = None
        if x is not None:
            return _core._constant_expr({"subtract": x - y, "multiply": x * y, "add": x + y}[op])
    return _core._make_node_expr(_core._nodedef_for(op, "float"), {"in1": a, "in2": b})


def _vertex_offset_expr(material) -> Optional[Dict[str, Any]]:
    """The model-space vertex offset for the material's Displacement, or None.

    Only called once ``displacement_refusal`` returned None. Height, Midlevel,
    Scale and Vector resolve through the expression tree, so textures, math
    and drivers feed the offset; an unresolvable chain propagates unresolved.
    """
    _output, node = _core.displacement_source(material)
    if node is None:
        return None
    visited, provenance, cache = set(), [], {}
    if node.type == 'DISPLACEMENT':
        height = _core._math_socket_expr(node, 0, visited, None, provenance, cache)
        midlevel = _core._math_socket_expr(node, 1, visited, None, provenance, cache)
        scale = _core._math_socket_expr(node, 2, visited, None, provenance, cache)
        if height is None or midlevel is None or scale is None:
            return {"kind": "unresolved", "provenance": [f"{getattr(node, 'name', 'Displacement')} (DISPLACEMENT)"]}
        amount = _fold_float("multiply", _fold_float("subtract", height, midlevel), scale)
        normal = _core._make_node_expr(
            _core._nodedef_for("normal", "vector3"), {"space": _core._constant_expr("object")}
        )
        return _core._vector_times_float(normal, amount)
    vector = _core._vector_math_operand(node, 0, visited, None, provenance, cache)
    midlevel = _core._math_socket_expr(node, 1, visited, None, provenance, cache)
    scale = _core._math_socket_expr(node, 2, visited, None, provenance, cache)
    if vector is None or midlevel is None or scale is None:
        return {"kind": "unresolved", "provenance": [f"{getattr(node, 'name', 'Vector Displacement')} (VECTOR_DISPLACEMENT)"]}
    if _core._expr_is_constant(midlevel, 0.0):
        centred = vector
    else:
        centred = _core._vector3_node("subtract", in1=vector, in2=_core._vector3_operand_expr(midlevel)
                                if midlevel.get("kind") == "constant"
                                else _core._make_node_expr(_core._nodedef_for("combine3", "vector3"),
                                                     {"in1": midlevel, "in2": midlevel, "in3": midlevel}))
    return _core._vector_times_float(centred, scale)


def extract_vertex_offset(material, data: Dict[str, Any]) -> None:
    """Record the material's vertex offset on ``data``, or the reason it is unresolved."""
    if displacement_refusal(material) is not None:
        return
    offset = _vertex_offset_expr(material)
    if offset is None:
        return
    if offset.get("kind") == "unresolved":
        data.setdefault('unresolved_warnings', []).append(
            f"Material '{material.name}': Displacement requires baking; the linked graph "
            "could not be resolved exactly."
        )
        return
    data['vertex_offset'] = offset
