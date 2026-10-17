"""Leaf primitives the extractor and its node-family modules share.

``core`` walks a Blender node tree, and the node families beside it
(``readers``, ``closures``, ``drivers``, ``hair``, ``displacement``) read the
sockets they own. Both sides need the same handful of builders: make a
constant, make a node expression, resolve a manifest nodedef, read one channel
of a vector.

Those builders used to live in ``core``, which made every node-family module
import ``core`` and made ``core`` import them all back at the bottom of the
file. Nothing here imports from this package, so both layers can depend on it
without a cycle.
"""

from typing import Any, Dict, Optional

from ....manifest.materialx_nodes import select_nodedef_name_for_node

_MANIFEST_CACHE: Optional[Dict[str, Any]] = None


def _get_manifest() -> Dict[str, Any]:
    """Load and cache the MaterialX manifest."""
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        from ....manifest.materialx_nodes import load_manifest

        _MANIFEST_CACHE = load_manifest()
    return _MANIFEST_CACHE


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
