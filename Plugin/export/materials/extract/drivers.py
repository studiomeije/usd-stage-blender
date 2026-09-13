"""Drivers over ``frame``, exported as RealityKit's time reader."""

from typing import Any, Dict, Optional

from . import core as _core


# ---------------------------------------------------------------------------
# Drivers: a socket whose value is a Blender driver expression over ``frame``.
#
# Blender evaluates the expression every frame; in the background process the
# CLI runs, it is never evaluated at all, so a driven socket's stored value is
# not what renders. A driver over ``frame`` maps onto RealityKit's time
# reader: ``frame = time * fps``, where ``ND_time_float`` counts seconds from
# the moment the material starts rendering. That clock is the material's own,
# not the timeline's, which the validator says in its warning. Expressions
# outside the arithmetic below are refused by name; nothing is approximated.
# ---------------------------------------------------------------------------

import ast as _ast  # noqa: E402 - kept beside the driver section it serves

#: Python functions a driver expression may call, and the float math node each
#: becomes. Everything else in a driver refuses.
_DRIVER_FUNCTIONS = {
    "sin": ("sin", 1), "cos": ("cos", 1), "tan": ("tan", 1), "abs": ("absval", 1),
    "floor": ("floor", 1), "ceil": ("ceil", 1), "sqrt": ("sqrt", 1),
    "min": ("min", 2), "max": ("max", 2), "pow": ("power", 2),
}
_DRIVER_BINARY = {_ast.Add: "add", _ast.Sub: "subtract", _ast.Mult: "multiply", _ast.Div: "divide"}
#: Node types whose float input sockets the resolver reads through
#: ``_expr_from_socket``, so a driver on them is authored live. A driver on any
#: other socket (a Principled input, a Mapping component, a texture setting) is
#: folded elsewhere and would be lost, so the validator refuses it.
DRIVEN_INPUT_NODE_TYPES = frozenset({
    'MATH', 'VECT_MATH', 'MIX', 'MIX_RGB', 'CLAMP', 'MAP_RANGE', 'HUE_SAT',
    'BRIGHTCONTRAST', 'INVERT', 'FRESNEL', 'COMBINE_COLOR', 'COMBXYZ',
})


def scene_fps() -> float:
    """The scene's frames per second, 24 when no Blender context is available."""
    try:
        import bpy  # type: ignore
        render = bpy.context.scene.render
        return float(render.fps) / float(render.fps_base or 1.0)
    except Exception:
        return 24.0


def socket_driver(socket, index: int = 0):
    """The driver F-curve on ``socket.default_value[index]``, or None."""
    tree = getattr(socket, "id_data", None)
    animation = getattr(tree, "animation_data", None)
    drivers = getattr(animation, "drivers", None)
    if not drivers:
        return None
    try:
        path = socket.path_from_id("default_value")
    except Exception:
        return None
    for fcurve in drivers:
        if getattr(fcurve, "data_path", None) == path and int(getattr(fcurve, "array_index", 0) or 0) == index:
            return fcurve
    return None


def _socket_is_live(socket) -> bool:
    """True when a socket's value varies: linked, or carrying a driver.

    A driven socket is never a constant, even when the driver is one the
    exporter refuses; the refusal is then reported on the socket itself.
    """
    return bool(getattr(socket, "is_linked", False)) or socket_driver(socket) is not None


def driver_refusal_reason(fcurve) -> Optional[str]:
    """Why a driver cannot be authored, or None when it can."""
    driver = getattr(fcurve, "driver", None)
    if driver is None:
        return "it has no driver"
    if (getattr(driver, "type", "SCRIPTED") or "SCRIPTED").upper() != "SCRIPTED":
        return f"only scripted expressions are exported, not a {driver.type.lower()} driver"
    if getattr(driver, "variables", None):
        names = ", ".join(getattr(v, "name", "?") for v in driver.variables)
        return f"it reads driver variables ({names}); only 'frame' and constants are exported"
    try:
        _driver_tree(str(getattr(driver, "expression", "") or ""))
    except _DriverUnsupported as exc:
        return str(exc)
    return None


class _DriverUnsupported(ValueError):
    pass


def _driver_tree(expression: str):
    try:
        tree = _ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise _DriverUnsupported(f"the expression {expression!r} does not parse") from exc

    def check(node):
        if isinstance(node, _ast.Expression):
            return check(node.body)
        if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return
        if isinstance(node, _ast.Name):
            if node.id == "frame":
                return
            raise _DriverUnsupported(f"the expression uses '{node.id}'; only 'frame' and constants are exported")
        if isinstance(node, _ast.UnaryOp) and isinstance(node.op, (_ast.USub, _ast.UAdd)):
            return check(node.operand)
        if isinstance(node, _ast.BinOp) and type(node.op) in _DRIVER_BINARY:
            check(node.left)
            check(node.right)
            return
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id in _DRIVER_FUNCTIONS:
            wanted = _DRIVER_FUNCTIONS[node.func.id][1]
            if len(node.args) != wanted or node.keywords:
                raise _DriverUnsupported(f"'{node.func.id}' takes {wanted} argument(s) here")
            for arg in node.args:
                check(arg)
            return
        raise _DriverUnsupported(
            f"the expression {expression!r} uses something other than +, -, *, /, "
            f"{', '.join(sorted(_DRIVER_FUNCTIONS))}, 'frame' and constants"
        )

    check(tree)
    return tree


def _frame_expr(fps: float) -> Dict[str, Any]:
    seconds = _core._make_node_expr(_core._nodedef_for("time", "float"), {"fps": _core._constant_expr(float(fps))})
    return _core._float_node("multiply", in1=seconds, in2=_core._constant_expr(float(fps)))


def driver_expression_expr(expression: str, fps: float) -> Dict[str, Any]:
    """Author a driver expression over ``frame`` as float math nodes.

    Constant sub-expressions fold; ``frame`` becomes ``time * fps``. Raises
    ``_DriverUnsupported`` for anything outside the allowed arithmetic.
    """
    tree = _driver_tree(expression)

    def build(node):
        if isinstance(node, _ast.Expression):
            return build(node.body)
        if isinstance(node, _ast.Constant):
            return _core._constant_expr(float(node.value))
        if isinstance(node, _ast.Name):
            return _frame_expr(fps)
        if isinstance(node, _ast.UnaryOp):
            inner = build(node.operand)
            if isinstance(node.op, _ast.UAdd):
                return inner
            if inner.get("kind") == "constant":
                return _core._constant_expr(-float(inner["value"]))
            return _core._float_node("multiply", in1=inner, in2=_core._constant_expr(-1.0))
        if isinstance(node, _ast.BinOp):
            left, right = build(node.left), build(node.right)
            if left.get("kind") == "constant" and right.get("kind") == "constant":
                a, b = float(left["value"]), float(right["value"])
                folded = {_ast.Add: a + b, _ast.Sub: a - b, _ast.Mult: a * b}.get(type(node.op))
                if folded is None:
                    folded = a / b if b != 0.0 else None
                if folded is not None:
                    return _core._constant_expr(folded)
            return _core._float_node(_DRIVER_BINARY[type(node.op)], in1=left, in2=right)
        if isinstance(node, _ast.Call):
            name, arity = _DRIVER_FUNCTIONS[node.func.id]
            args = [build(arg) for arg in node.args]
            if arity == 1:
                return _core._float_node(name, **{"in": args[0]})
            return _core._float_node(name, in1=args[0], in2=args[1])
        raise _DriverUnsupported(expression)

    return build(tree)


def _driven_socket_expr(socket, index: int = 0) -> Optional[Dict[str, Any]]:
    """The live expression for a driven socket, or None when it is not driven.

    An unsupported driver returns an unresolved expression so the refusal
    names the socket; the validator has already said why.
    """
    fcurve = socket_driver(socket, index)
    if fcurve is None:
        return None
    reason = driver_refusal_reason(fcurve)
    if reason is not None:
        return {"kind": "unresolved", "provenance": [f"driver {getattr(fcurve, 'data_path', '?')}: {reason}"]}
    return driver_expression_expr(str(fcurve.driver.expression), scene_fps())
