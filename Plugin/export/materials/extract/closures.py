"""Shader closures between a Principled BSDF and the Material Output."""

import math
from typing import Any, Dict, List, Optional

from ..graph import (
    CYCLES_CLOSURE_WEIGHT_CUTOFF,
    PBR2_F0_PER_SPECULAR,
    diffuse_roughness_is_lambert,
    pbr2_specular_cap_exceeded,
)

from . import core as _core


# ---------------------------------------------------------------------------
# Shader closures: the shader-level nodes between a Principled BSDF and the
# Material Output that have an exact RealityKit form.
#
# A Mix Shader of a Principled BSDF and a white Transparent BSDF is opacity:
# Blender evaluates out = (1 - fac) * A + fac * B, so the surface's weight is
# the factor's complement when the Transparent BSDF sits on the B side and the
# factor itself when it sits on A. An Add Shader of a Principled BSDF and an
# Emission shader adds the emission to the surface's own. Every other shape is
# refused with its operands named, because a BSDF cannot be scaled or summed on
# RealityKit's one surface.
# ---------------------------------------------------------------------------

_CLOSURE_NODE_TYPES = frozenset({'MIX_SHADER', 'ADD_SHADER'})

_SHADER_LABELS = {
    'BSDF_PRINCIPLED': "Principled BSDF", 'BSDF_TRANSPARENT': "Transparent BSDF",
    'EMISSION': "Emission", 'MIX_SHADER': "Mix Shader", 'ADD_SHADER': "Add Shader",
}


def _shader_source(socket):
    """The node feeding a shader socket, through reroutes; None when unlinked."""
    if socket is None or not getattr(socket, "is_linked", False) or not socket.links:
        return None
    node = getattr(socket.links[0], "from_node", None)
    if node is not None and getattr(node, "type", "") == 'REROUTE':
        inputs = getattr(node, "inputs", None)
        return _shader_source(inputs[0] if inputs else None)
    return node


def _shader_label(node) -> str:
    node_type = getattr(node, "type", "")
    if node_type in _SHADER_LABELS:
        return _SHADER_LABELS[node_type]
    if node_type.startswith("BSDF_"):
        return f"{node_type[5:].replace('_', ' ').title()} BSDF"
    return node_type.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Shaders that reduce to a Principled BSDF.
#
# The Principled extraction reads everything through ``node.inputs``, so a
# shader Cycles evaluates as a particular Principled BSDF is exported through
# a stand-in node carrying that Principled's inputs: the source node's own
# sockets where they correspond, constants elsewhere, and derived sockets whose
# value the resolver builds (a square root, or a blend of two shaders).
# ---------------------------------------------------------------------------

#: Blender 5.2's Principled BSDF inputs and defaults, in socket order.
_PRINCIPLED_DEFAULTS = (
    ('Base Color', (0.8, 0.8, 0.8, 1.0)), ('Metallic', 0.0), ('Roughness', 0.5), ('IOR', 1.5),
    ('Alpha', 1.0), ('Thin Wall', False), ('Normal', (0.0, 0.0, 0.0)), ('Diffuse Roughness', 0.0),
    ('Subsurface Weight', 0.0), ('Subsurface Radius', (1.0, 0.2, 0.1)), ('Subsurface Scale', 0.005),
    ('Subsurface IOR', 1.4), ('Subsurface Anisotropy', 0.0), ('Specular IOR Level', 0.5),
    ('Specular Tint', (1.0, 1.0, 1.0, 1.0)), ('Anisotropic', 0.0), ('Anisotropic Rotation', 0.0),
    ('Tangent', (0.0, 0.0, 0.0)), ('Transmission Weight', 0.0), ('Coat Weight', 0.0),
    ('Coat Roughness', 0.03), ('Coat IOR', 1.5), ('Coat Tint', (1.0, 1.0, 1.0, 1.0)),
    ('Coat Normal', (0.0, 0.0, 0.0)), ('Sheen Weight', 0.0), ('Sheen Roughness', 0.5),
    ('Sheen Tint', (1.0, 1.0, 1.0, 1.0)), ('Emission Color', (1.0, 1.0, 1.0, 1.0)),
    ('Emission Strength', 0.0), ('Thin Film Thickness', 0.0), ('Thin Film IOR', 1.33),
)

#: Shader nodes exported as a Principled BSDF preset.
PRINCIPLED_PRESET_TYPES = frozenset({
    'BSDF_DIFFUSE', 'BSDF_GLOSSY', 'BSDF_METALLIC', 'BSDF_SHEEN', 'SUBSURFACE_SCATTERING',
})


class _ConstantSocket:
    """An unlinked stand-in socket holding a constant."""

    is_linked = False
    links = ()
    enabled = True

    def __init__(self, name, value):
        self.name = name
        self.identifier = name
        self.default_value = value


class _RenamedSocket:
    """A real socket presented under its Principled name."""

    def __init__(self, name, socket):
        object.__setattr__(self, "_socket", socket)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "identifier", name)
        object.__setattr__(self, "enabled", True)

    def __getattr__(self, attribute):
        return getattr(self._socket, attribute)


class _DerivedNode:
    """A value the resolver computes from other sockets, for a stand-in input."""

    type = '_USDSTAGE_DERIVED'

    def __init__(self, name, build, sources):
        self.name = name
        self.build = build
        self.sources = sources
        self.inputs = []
        self.outputs = []


class _DerivedLink:
    def __init__(self, node, name):
        self.from_node = node
        self.from_socket = _ConstantSocket(name, None)


class _DerivedSocket:
    """A linked stand-in socket whose value a ``_DerivedNode`` builds."""

    is_linked = True
    enabled = True

    def __init__(self, name, node, default_value):
        self.name = name
        self.identifier = name
        self.links = [_DerivedLink(node, name)]
        self.default_value = default_value


class _StandInInputs(list):
    def get(self, name):
        for socket in self:
            if socket.name == name:
                return socket
        return None

    def items(self):
        return [(socket.name, socket) for socket in self]


class PrincipledView:
    """A stand-in Principled BSDF for a shader Cycles evaluates as one."""

    type = 'BSDF_PRINCIPLED'

    def __init__(self, source, label, sockets, rename=None, distribution='MULTI_GGX', subsurface_method='RANDOM_WALK'):
        self.source = source
        self.label = label
        self.name = getattr(source, "name", label)
        self.id_data = getattr(source, "id_data", None)
        self.distribution = distribution
        self.subsurface_method = subsurface_method
        #: Principled input name -> the name the artist sees on the source.
        self.rename = dict(rename or {})
        inputs = _StandInInputs()
        for name, default in _PRINCIPLED_DEFAULTS:
            socket = sockets.get(name)
            if socket is None:
                socket = _ConstantSocket(name, default)
            elif not isinstance(socket, (_ConstantSocket, _DerivedSocket, _RenamedSocket)):
                socket = _RenamedSocket(name, socket)
            inputs.append(socket)
        self.inputs = inputs
        self.outputs = []

    def __eq__(self, other):
        return isinstance(other, PrincipledView) and _same_node(self.source, other.source) and self.label == other.label

    def __hash__(self):
        return id(self)


def _socket_operand(socket, expected_type, visited, provenance, cache):
    """A socket's value as an expression of ``expected_type``."""
    if socket is None:
        return None
    if getattr(socket, "is_linked", False):
        expr = _core._resolve_socket_value(
            socket, set(visited or ()), None, list(provenance or ()), cache, expected_type=expected_type,
        )
    else:
        expr = _core._driven_socket_expr(socket) if not isinstance(socket, _ConstantSocket) else None
        if expr is None:
            value = _core._socket_default_value(socket)
            if isinstance(value, bool):
                value = float(value)
            expr = _core._constant_expr(value) if value is not None else None
    if expr is None:
        return None
    if expected_type == "color3":
        return _core._color3_operand_expr(expr)
    if expected_type == "vector3":
        return _core._vector3_operand_expr(expr)
    if expected_type in ("float", None):
        return _core._float_math_input_expr(expr)
    return expr


def _value_type(value) -> str:
    if isinstance(value, (list, tuple)):
        return "color3" if len(value) == 4 else "vector3"
    return "float"


def _preset_sqrt_socket(name, socket):
    """A Principled Roughness equal to the square root of a source roughness."""
    if not getattr(socket, "is_linked", False) and _core._driven_socket_expr(socket) is None:
        try:
            return _ConstantSocket(name, math.sqrt(max(float(socket.default_value), 0.0)))
        except (TypeError, ValueError):
            return None

    def build(visited, provenance, cache, expected_type):
        value = _socket_operand(socket, "float", visited, provenance, cache)
        if not value or value.get("kind") == "unresolved":
            return value
        clamped = _core._make_node_expr(_core._nodedef_for("max", "float"), {"in1": value, "in2": _core._constant_expr(0.0)})
        return _core._make_node_expr(_core._nodedef_for("sqrt", "float"), {"in": clamped})

    return _DerivedSocket(name, _DerivedNode(f"{getattr(socket, 'name', name)} (square root)", build, [socket]), 0.5)


def principled_preset(node) -> Dict[str, Any]:
    """The Principled BSDF a Diffuse, Glossy, Metallic, Sheen or Subsurface
    Scattering shader exports as, from Cycles' ``svm_node_closure_bsdf``.

    - Diffuse: Base Color and Diffuse Roughness, no specular layer (Specular
      IOR Level 0 gives the specular lobe an IOR of 1, which Cycles skips).
      Cycles evaluates the two identically.
    - Metallic (F82 Tint): Metallic 1 with Base Color, Edge Tint as Specular
      Tint, Roughness, anisotropy and thin film. The Principled metallic lobe
      is the same F82 closure.
    - Sheen: a black base with no specular layer under Sheen Weight 1, Sheen
      Tint from Color and Sheen Roughness; the same sheen closure.
    - Glossy: Metallic 1 with Base Color from Color. Glossy has no Fresnel, so
      it is not identical, and the export says so.
    - Subsurface Scattering: Subsurface Weight 1 with its Color, Radius,
      Scale, IOR and Anisotropy, no specular layer, and Roughness as the
      square root of the node's, because Principled squares it for the
      scattering entry. This is not exact in Cycles. The node scatters with
      its own IOR under every method and takes its Color unclamped, where the
      Principled BSDF clamps Base Color to 1 and, at Specular IOR Level 0,
      scatters with an IOR of 1 except under Random Walk (Skin), which alone
      reads Subsurface IOR. The node's IOR maps to Subsurface IOR, and under
      the other methods a notice says the scattering entry differs.
      RealityKit's subsurface has no IOR input under any method.

    Measured by importing ``t35_bsdf_presets`` into Reality Composer Pro 3:
    each preset renders like its Principled control. The
    Diffuse and Subsurface Scattering pairs show it; the reflective pairs were
    black on both sides and are unjudged. That export authored a sheenColor
    on every Principled material, which RealityRenderer measurements since
    show turns any metal black, so the pairs need importing again.

    Returns ``{"view", "refusal", "notices"}``.
    """
    node_type = getattr(node, "type", "")
    label = _shader_label(node)
    name = getattr(node, "name", label)
    inputs = getattr(node, "inputs", None)

    def socket(socket_name):
        return inputs.get(socket_name) if inputs is not None and hasattr(inputs, "get") else None

    result: Dict[str, Any] = {"view": None, "refusal": None, "notices": []}
    distribution = (getattr(node, "distribution", "") or "").upper()
    common = {'Normal': socket('Normal')}
    if node_type == 'BSDF_DIFFUSE':
        sockets = dict(common, **{
            'Base Color': socket('Color'), 'Diffuse Roughness': socket('Roughness'),
            'Specular IOR Level': _ConstantSocket('Specular IOR Level', 0.0),
        })
        rename = {'Base Color': 'Color', 'Diffuse Roughness': 'Roughness'}
    elif node_type == 'BSDF_METALLIC':
        if (getattr(node, "fresnel_type", "F82") or "F82").upper() != 'F82':
            result["refusal"] = (
                f"{label} '{name}' uses Physical Conductor Fresnel, which has no RealityKit input; "
                "switch Fresnel Type to F82 Tint, or bake the material."
            )
            return result
        sockets = dict(common, **{
            'Base Color': socket('Base Color'), 'Specular Tint': socket('Edge Tint'),
            'Roughness': socket('Roughness'), 'Anisotropic': socket('Anisotropy'),
            'Anisotropic Rotation': socket('Rotation'), 'Tangent': socket('Tangent'),
            'Thin Film Thickness': socket('Thin Film Thickness'), 'Thin Film IOR': socket('Thin Film IOR'),
            'Metallic': _ConstantSocket('Metallic', 1.0),
        })
        rename = {'Specular Tint': 'Edge Tint', 'Anisotropic': 'Anisotropy', 'Anisotropic Rotation': 'Rotation'}
        if distribution == 'BECKMANN':
            result["notices"].append(
                f"{label} '{name}' uses the Beckmann distribution; RealityKit's surface is GGX, "
                "so highlights are shaped slightly differently."
            )
    elif node_type == 'BSDF_GLOSSY':
        sockets = dict(common, **{
            'Base Color': socket('Color'), 'Roughness': socket('Roughness'),
            'Anisotropic': socket('Anisotropy'), 'Anisotropic Rotation': socket('Rotation'),
            'Tangent': socket('Tangent'), 'Metallic': _ConstantSocket('Metallic', 1.0),
        })
        rename = {'Base Color': 'Color', 'Anisotropic': 'Anisotropy', 'Anisotropic Rotation': 'Rotation'}
        result["notices"].append(
            f"{label} '{name}' reflects its Color equally at every angle; it exports as a metallic "
            "surface of that colour, which brightens toward white at grazing angles."
        )
        if distribution in ('BECKMANN', 'ASHIKHMIN_SHIRLEY'):
            result["notices"].append(
                f"{label} '{name}' uses the {distribution.replace('_', '-').title()} distribution; "
                "RealityKit's surface is GGX, so highlights are shaped slightly differently."
            )
    elif node_type == 'BSDF_SHEEN':
        sockets = dict(common, **{
            'Base Color': _ConstantSocket('Base Color', (0.0, 0.0, 0.0, 1.0)),
            'Specular IOR Level': _ConstantSocket('Specular IOR Level', 0.0),
            'Sheen Weight': _ConstantSocket('Sheen Weight', 1.0),
            'Sheen Tint': socket('Color'), 'Sheen Roughness': socket('Roughness'),
        })
        rename = {'Sheen Tint': 'Color', 'Sheen Roughness': 'Roughness'}
        if distribution == 'ASHIKHMIN':
            result["notices"].append(
                f"{label} '{name}' uses the Ashikhmin distribution; it exports as the Microfiber "
                "sheen the Principled BSDF uses."
            )
    elif node_type == 'SUBSURFACE_SCATTERING':
        roughness = socket('Roughness')
        derived = _preset_sqrt_socket('Roughness', roughness) if roughness is not None else None
        sockets = dict(common, **{
            'Base Color': socket('Color'), 'Subsurface Weight': _ConstantSocket('Subsurface Weight', 1.0),
            'Subsurface Radius': socket('Radius'), 'Subsurface Scale': socket('Scale'),
            'Subsurface IOR': socket('IOR'), 'Subsurface Anisotropy': socket('Anisotropy'),
            'Specular IOR Level': _ConstantSocket('Specular IOR Level', 0.0), 'Roughness': derived,
        })
        rename = {'Base Color': 'Color', 'Subsurface Radius': 'Radius', 'Subsurface Scale': 'Scale',
                  'Subsurface IOR': 'IOR', 'Subsurface Anisotropy': 'Anisotropy'}
        method = (getattr(node, "falloff", "RANDOM_WALK") or "RANDOM_WALK").upper()
        if method != 'RANDOM_WALK_SKIN':
            result["notices"].append(
                f"{label} '{name}' uses {method.replace('_', ' ').title()}, under which Cycles scatters "
                "with the node's IOR where the equivalent Principled BSDF scatters with an IOR of 1; "
                "RealityKit's subsurface has no IOR, so the scattering at the surface will not match "
                "Cycles. Random Walk (Skin) is the method the export reproduces."
            )
    else:
        return result
    sockets = {key: value for key, value in sockets.items() if value is not None}
    result["view"] = PrincipledView(
        node, label, sockets, rename,
        distribution='GGX' if distribution in ('GGX', 'BECKMANN', 'ASHIKHMIN_SHIRLEY') else 'MULTI_GGX',
        subsurface_method=(getattr(node, "falloff", "RANDOM_WALK") or "RANDOM_WALK").upper(),
    )
    return result


def _sockets_agree(a, b) -> bool:
    """Whether two Principled sockets carry the same value or the same link."""
    if a is None or b is None:
        return a is b
    a_linked, b_linked = getattr(a, "is_linked", False), getattr(b, "is_linked", False)
    if a_linked != b_linked:
        return False
    if a_linked:
        if isinstance(a, _DerivedSocket) or isinstance(b, _DerivedSocket):
            return False
        la, lb = a.links[0], b.links[0]
        return _same_node(la.from_node, lb.from_node) and getattr(la.from_socket, "name", None) == getattr(lb.from_socket, "name", None)
    if _core._driven_socket_expr(a) is not None or _core._driven_socket_expr(b) is not None:
        return False
    return not _values_differ_loose(_core._socket_default_value(a), _core._socket_default_value(b))


def _values_differ_loose(a, b) -> bool:
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if not (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))):
            return True
        return any(abs(float(x) - float(y)) > 1e-6 for x, y in zip(a, b))
    try:
        return abs(float(a) - float(b)) > 1e-6
    except (TypeError, ValueError):
        return a != b


#: Inputs a closure mix carries exactly as a blend of values, when it is the
#: only input that differs.
_LINEAR_MIX_INPUTS = frozenset({'Base Color', 'Metallic'})


def principled_mix(mix, first, second) -> Dict[str, Any]:
    """A Mix Shader of two Principled-like shaders, as one blended Principled BSDF.

    Cycles mixes two surfaces as a weighted sum of their closures with the
    factor clamped to [0, 1]; the export blends each input that differs,
    ``mix(first, second, factor)``. That sum and that blend agree exactly when
    a single input differs and it is Base Color or Metallic, because each
    closure's weight is linear in that one input. Once two inputs differ the
    sum holds products of them (a metallic lobe's weight times its colour) that
    no blend of values reproduces, so the export names every differing input in
    a notice whenever more than one differs or any other input differs. A
    Normal that differs is refused,
    because blending two normal maps is not a normal map. Measured by importing
    ``t35_bsdf_presets``: a red dielectric blended into a blue metal by U
    renders like its control, one Principled BSDF fed by Mix nodes; that
    control is the blend of values, so it shows the translation reached the
    surface, not that the two differing inputs match Cycles' sum.

    Returns ``{"view", "refusal", "notices"}``.
    """
    result: Dict[str, Any] = {"view": None, "refusal": None, "notices": []}
    label = f"Mix Shader '{getattr(mix, 'name', 'Mix Shader')}'"
    differing = []
    sockets = {}
    for name, default in _PRINCIPLED_DEFAULTS:
        a, b = first.inputs.get(name), second.inputs.get(name)
        if a is None and b is None:
            continue
        if a is None:
            a = _ConstantSocket(name, default)
        if b is None:
            b = _ConstantSocket(name, default)
        if _sockets_agree(a, b):
            sockets[name] = a
            continue
        if name in ('Normal', 'Coat Normal', 'Tangent'):
            if getattr(a, "is_linked", False) or getattr(b, "is_linked", False):
                result["refusal"] = (
                    f"{label} blends two shaders whose {name} inputs differ; blending two normals "
                    "is not a normal map. Feed both shaders the same normal, or bake the material."
                )
                return result
        differing.append(name)
        sockets[name] = _mix_socket(mix, name, a, b, default)
    if len(differing) > 1 or any(name not in _LINEAR_MIX_INPUTS for name in differing):
        result["notices"].append(
            f"{label} blends shaders whose {', '.join(differing)} differ. Cycles mixes the two "
            "surfaces; the export blends those values into one surface, which matches only "
            "approximately between the two."
        )
    result["view"] = PrincipledView(
        mix, "Mix Shader", sockets,
        distribution=getattr(first, "distribution", 'MULTI_GGX'),
        subsurface_method=getattr(first, "subsurface_method", 'RANDOM_WALK'),
    )
    result["view"].blended = tuple(differing)
    return result


def _mix_socket(mix, name, a, b, default):
    factor_socket = mix.inputs[0] if getattr(mix, "inputs", None) else None
    value_type = _value_type(default)
    factor_constant = (
        factor_socket is not None and not getattr(factor_socket, "is_linked", False)
        and _core._driven_socket_expr(factor_socket) is None
    )
    constants = all(
        not getattr(s, "is_linked", False) and (isinstance(s, _ConstantSocket) or _core._driven_socket_expr(s) is None)
        for s in (a, b)
    )
    if factor_constant and constants:
        t = min(max(float(factor_socket.default_value), 0.0), 1.0)
        va, vb = _core._socket_default_value(a), _core._socket_default_value(b)
        if isinstance(va, (list, tuple)):
            return _ConstantSocket(name, tuple(x + (y - x) * t for x, y in zip(va, vb)))
        return _ConstantSocket(name, float(va) + (float(vb) - float(va)) * t)

    def build(visited, provenance, cache, expected_type):
        kind = expected_type if expected_type in ("color3", "vector3", "float") else value_type
        ea = _socket_operand(a, kind, visited, provenance, cache)
        eb = _socket_operand(b, kind, visited, provenance, cache)
        fac = _socket_operand(factor_socket, "float", visited, provenance, cache) if factor_socket is not None else _core._constant_expr(0.5)
        for operand in (ea, eb, fac):
            if not operand:
                return None
            if operand.get("kind") == "unresolved":
                return operand
        if _core._constant_value(fac) is not None:
            fac = _core._constant_expr(min(max(float(_core._constant_value(fac)), 0.0), 1.0))
        else:
            fac = _core._make_node_expr(_core._nodedef_for("clamp", "float"), {"in": fac, "low": _core._constant_expr(0.0), "high": _core._constant_expr(1.0)})
        return _core._make_node_expr(_core._nodedef_for("mix", kind), {"fg": eb, "bg": ea, "mix": fac})

    node = _DerivedNode(f"Mix Shader '{getattr(mix, 'name', 'Mix Shader')}' {name}", build, [a, b, factor_socket])
    return _DerivedSocket(name, node, _core._socket_default_value(a))


def _principled_like(node) -> Dict[str, Any]:
    """``{"view", "refusal", "notices"}`` for a Principled BSDF or a preset shader."""
    node_type = getattr(node, "type", "")
    if node_type == 'BSDF_PRINCIPLED':
        return {"view": node, "refusal": None, "notices": []}
    if node_type in PRINCIPLED_PRESET_TYPES:
        return principled_preset(node)
    return {"view": None, "refusal": None, "notices": []}


def resolve_surface_closure(material) -> Dict[str, Any]:
    """Describe the shader wired to the active Material Output.

    ``principled`` is the Principled BSDF the material reduces to, ``closure``
    the Mix or Add Shader between it and the output (None when wired
    directly), ``transparent`` the Transparent BSDF mixed in with
    ``transparent_first`` True when it sits on the factor-0 side, ``emission``
    the added Emission shader, and ``refusal`` why the closure has no
    RealityKit form, or None. Shared by the extractor and the validator.
    """
    info: Dict[str, Any] = {
        "surface": None, "principled": None, "closure": None, "transparent": None,
        "transparent_first": False, "emission": None, "refusal": None, "notices": [],
        "mixed": None,
    }
    surface = _core._get_surface_shader_node(material)
    info["surface"] = surface
    if surface is None:
        return info
    surface_type = getattr(surface, "type", "")
    info.setdefault("notices", [])
    if surface_type == 'BSDF_PRINCIPLED' or surface_type in PRINCIPLED_PRESET_TYPES:
        like = _principled_like(surface)
        info["principled"] = like["view"]
        info["refusal"] = like["refusal"]
        info["notices"] = like["notices"]
        return info
    if surface_type not in _CLOSURE_NODE_TYPES:
        return info
    info["closure"] = surface
    kind = "Mix Shader" if surface_type == 'MIX_SHADER' else "Add Shader"
    label = f"{kind} '{getattr(surface, 'name', kind)}'"
    shader_sockets = [s for s in surface.inputs if getattr(s, "type", "") == 'SHADER']
    sources = [_shader_source(s) for s in shader_sockets]
    if len(sources) != 2 or any(src is None for src in sources):
        info["refusal"] = f"{label} needs both shader inputs connected; connect them, or bake the material."
        return info
    types = [getattr(src, "type", "") for src in sources]
    operands = " and ".join(_shader_label(src) for src in sources)
    likes = [_principled_like(src) for src in sources]
    for like in likes:
        if like["refusal"] is not None:
            info["refusal"] = like["refusal"]
            return info
        info["notices"].extend(like["notices"])
    # From here a preset shader counts as the Principled BSDF it exports as.
    types = ['BSDF_PRINCIPLED' if like["view"] is not None else t for like, t in zip(likes, types)]
    sources = [like["view"] if like["view"] is not None else src for like, src in zip(likes, sources)]
    if surface_type == 'MIX_SHADER':
        if types == ['BSDF_PRINCIPLED', 'BSDF_PRINCIPLED']:
            mixed = principled_mix(surface, sources[0], sources[1])
            if mixed["refusal"] is not None:
                info["refusal"] = mixed["refusal"]
                return info
            info["notices"].extend(mixed["notices"])
            info["principled"] = mixed["view"]
            info["mixed"] = (sources[0], sources[1])
            return info
        if sorted(types) == ['BSDF_PRINCIPLED', 'BSDF_TRANSPARENT']:
            index = types.index('BSDF_TRANSPARENT')
            transparent = sources[index]
            color = transparent.inputs.get('Color') if hasattr(transparent, "inputs") else None
            tinted = False
            if color is not None:
                if getattr(color, "is_linked", False):
                    tinted = True
                else:
                    try:
                        tinted = any(float(c) < 0.999 for c in list(color.default_value)[:3])
                    except (TypeError, ValueError):
                        tinted = False
            if tinted:
                info["refusal"] = (
                    f"Transparent BSDF '{getattr(transparent, 'name', 'Transparent BSDF')}' has a "
                    "tinted or linked Color; RealityKit opacity has no colour. Set it to white; "
                    "no bake mode accepts a Transparent BSDF."
                )
                return info
            info["principled"] = sources[1 - index]
            info["transparent"] = transparent
            info["transparent_first"] = index == 0
            return info
        if sorted(types) == ['BSDF_PRINCIPLED', 'EMISSION']:
            info["refusal"] = (
                f"{label} fades a Principled BSDF against an Emission shader; a surface cannot "
                "be scaled by the factor. Add the emission with an Add Shader, or bake it with "
                "RealityKit Unlit > Lighting & Shadows."
            )
        else:
            info["refusal"] = (
                f"{label} mixes {operands}; only two of Principled, Diffuse, Glossy, Metallic, "
                "Sheen or Subsurface Scattering, or one of them with a white Transparent BSDF, have "
                "a RealityKit form. RealityKit Unlit > Lighting & Shadows can bake it unless a "
                "Transparent BSDF is involved."
            )
        return info
    if sorted(types) == ['BSDF_PRINCIPLED', 'EMISSION']:
        index = types.index('EMISSION')
        principled = sources[1 - index]
        for socket_name in ("Emission Color", "Emission Strength"):
            socket = principled.inputs.get(socket_name) if hasattr(principled, "inputs") else None
            if socket is not None and getattr(socket, "is_linked", False):
                info["refusal"] = (
                    f"{label} adds an Emission shader to a Principled BSDF whose {socket_name} is "
                    "linked; put the emission in one place."
                )
                return info
        if hasattr(principled, "inputs") and _core._principled_alpha_is_transparent(principled):
            info["refusal"] = (
                f"{label} adds an Emission shader to a Principled BSDF whose Alpha is below 1 or "
                "linked. Cycles adds that emission at full strength, but RealityKit dims all of "
                "a surface's emission by its opacity, so the added emission would render dimmed. "
                "Set Alpha to 1, or move the emission into the Principled BSDF's Emission inputs, "
                "which Cycles dims by Alpha too."
            )
            return info
        info["principled"] = principled
        info["emission"] = sources[index]
        return info
    info["refusal"] = (
        f"{label} adds {operands}; only a Principled, Diffuse, Glossy, Metallic, Sheen or "
        "Subsurface Scattering shader plus an Emission shader has a RealityKit form, and no bake "
        "mode accepts an Add Shader."
    )
    return info


def _same_node(a, b) -> bool:
    """Blender hands out a fresh Python wrapper per access, so ``is`` fails
    between two views of one node; ``==`` compares the underlying struct."""
    if a is None or b is None:
        return False
    return a is b or a == b


def closure_issue(closure: Dict[str, Any], node) -> Optional[str]:
    """Why ``node`` (a Mix/Add Shader or Transparent BSDF) is refused, or None."""
    node_type = getattr(node, "type", "")
    label = f"{_shader_label(node)} '{getattr(node, 'name', node_type)}'"
    if node_type == 'BSDF_TRANSPARENT':
        if _same_node(closure.get("transparent"), node):
            return closure.get("refusal")
        return (
            f"{label} is only exported as a Mix Shader operand beside a Principled BSDF on "
            "the Material Output; no bake mode accepts a Transparent BSDF."
        )
    if _same_node(closure.get("closure"), node):
        return closure.get("refusal")
    return (
        f"{label} is not the shader on the Material Output; only one Mix or Add Shader "
        "directly on the output is exported. RealityKit Unlit > Lighting & Shadows can bake "
        "a nested Mix Shader that involves no Transparent BSDF."
    )


def principled_like_issue(closure: Dict[str, Any], node) -> Optional[str]:
    """Why a preset shader node (Diffuse, Glossy, ...) is refused, or None.

    It exports when it is the shader on the Material Output, or an operand of
    the supported Mix or Add Shader there. A refused closure is reported on
    the Mix or Add Shader itself, not again on its operands.
    """
    label = f"{_shader_label(node)} '{getattr(node, 'name', _shader_label(node))}'"
    if _same_node(closure.get("surface"), node):
        return closure.get("refusal")
    mix = closure.get("closure")
    if mix is not None:
        for socket in getattr(mix, "inputs", []) or []:
            if getattr(socket, "type", "") == 'SHADER' and _same_node(_shader_source(socket), node):
                return None
    return (
        f"{label} is not the shader on the Material Output, nor an operand of the Mix or Add "
        "Shader there, so it does not reach the export."
    )


def principled_views(closure: Dict[str, Any]) -> List[Any]:
    """The stand-in Principled BSDFs a resolved closure exports through."""
    views = []
    principled = closure.get("principled")
    if isinstance(principled, PrincipledView):
        views.append(principled)
    for side in closure.get("mixed") or ():
        if isinstance(side, PrincipledView):
            views.append(side)
    return views


def _principled_constant(node, name, default):
    """A Principled input's constant value, or None when it is linked or driven."""
    socket = node.inputs.get(name) if hasattr(node, "inputs") else None
    if socket is None:
        return default
    if getattr(socket, "is_linked", False):
        return None
    if not isinstance(socket, _ConstantSocket) and _core._driven_socket_expr(socket) is not None:
        return None
    value = _core._socket_default_value(socket)
    return default if value is None else value


def principled_notices(principled) -> List[str]:
    """Approximations PBR Surface 2 makes of an exportable Principled BSDF.

    Each is measured with RealityRenderer on macOS 27 and holds for the
    Principled BSDF itself and for the stand-in a preset or a blended Mix
    Shader exports through. The messages name Principled inputs; the
    validator rewords them for a stand-in.
    """
    if principled is None:
        return []
    notices: List[str] = []

    sheen_weight = _principled_constant(principled, 'Sheen Weight', 0.0)
    if sheen_weight is None or float(sheen_weight) > CYCLES_CLOSURE_WEIGHT_CUTOFF:
        base = _principled_constant(principled, 'Base Color', (0.8, 0.8, 0.8, 1.0))
        metallic = _principled_constant(principled, 'Metallic', 0.0)
        level = _principled_constant(principled, 'Specular IOR Level', 0.5)
        exact = (
            base is not None and all(float(c) <= 0.0 for c in list(base)[:3])
            and metallic is not None and float(metallic) <= 0.0
            and level is not None and float(level) <= 0.0
        )
        if not exact:
            notices.append(
                "Principled 'Sheen Weight' exports as RealityKit's sheen, which replaces the "
                "specular lobe instead of layering over it: the metallic reflection and the "
                "dielectric highlight disappear wherever it is on. It matches Cycles only over a "
                "black, non-metallic base with Specular IOR Level 0, as in the Sheen BSDF."
            )

    diffuse_roughness = _principled_constant(principled, 'Diffuse Roughness', 0.0)
    if diffuse_roughness is None or not diffuse_roughness_is_lambert(diffuse_roughness):
        notices.append(
            "Principled 'Diffuse Roughness' is not exported, so the surface shades as Lambert, "
            "slightly brighter at grazing angles than Cycles' rough diffuse. RealityKit's own "
            "rough diffuse lights the diffuse from the environment about a third as brightly "
            "(measured: a 0.5 grey renders like a 0.17 grey), which is further from Blender. "
            "Bake with Lighting & Shadows to keep it."
        )

    metallic = _principled_constant(principled, 'Metallic', 0.0)
    if metallic is None or float(metallic) < 1.0:
        ior = _principled_constant(principled, 'IOR', 1.5)
        level = _principled_constant(principled, 'Specular IOR Level', 0.5)
        if ior is None or level is None:
            notices.append(
                "Principled 'IOR' or 'Specular IOR Level' is linked. RealityKit's surface caps "
                f"the dielectric reflectance at normal incidence (F0) at {PBR2_F0_PER_SPECULAR:g}, "
                "so wherever the linked value gives more (above IOR "
                "1.8 at level 0.5) the highlight renders dimmer than in Cycles."
            )
        else:
            f0 = pbr2_specular_cap_exceeded(float(ior), float(level))
            if f0 is not None:
                notices.append(
                    f"Principled 'IOR' {float(ior):g} at 'Specular IOR Level' {float(level):g} "
                    f"gives a dielectric reflectance at normal incidence (F0) of {f0:.3f}. "
                    f"RealityKit's surface caps it at {PBR2_F0_PER_SPECULAR:g} while Diffuse "
                    "Roughness is 0, so the highlight renders dimmer than in Cycles."
                )
    return notices


def _saturate_float(expr: Dict[str, Any]) -> Dict[str, Any]:
    """Cycles' ``saturatef`` on a float expression."""
    if expr.get("kind") == "constant":
        return _core._constant_expr(min(max(float(expr["value"]), 0.0), 1.0))
    if expr.get("kind") == "unresolved":
        return expr
    return _core._make_node_expr(
        _core._nodedef_for("clamp", "float"),
        {"in": expr, "low": _core._constant_expr(0.0), "high": _core._constant_expr(1.0)},
    )


def _apply_surface_closure(material, closure, principled, data, input_graphs, unresolved_warnings) -> None:
    """Fold a supported closure into the Principled BSDF's extracted data."""
    mix = closure.get("closure")
    if mix is None or closure.get("refusal") is not None:
        return
    visited, provenance, cache = set(), [], {}
    if closure.get("transparent") is not None:
        fac = _core._math_socket_expr(mix, 0, visited, None, provenance, cache)
        if fac is None:
            fac = _core._constant_expr(0.5)
        # Cycles saturates the Mix Shader factor (svm_node_mix_closure) and
        # the Principled Alpha (principled_bsdf_emission).
        fac = _saturate_float(fac)
        weight = fac if closure.get("transparent_first") else _core._fold_float("subtract", _core._constant_expr(1.0), fac)
        alpha = _core._constant_expr(1.0)
        inputs = list(getattr(principled, "inputs", []) or [])
        for index, socket in enumerate(inputs):
            if getattr(socket, "name", "") == 'Alpha':
                alpha = _core._math_socket_expr(principled, index, visited, None, provenance, cache) or alpha
                break
        alpha = _saturate_float(alpha)
        opacity = _core._fold_float("multiply", alpha, weight)
        for key in [k for k in list(data) if k == 'alpha' or k.startswith('alpha_texture')]:
            data.pop(key, None)
        input_graphs.pop('opacity', None)
        if opacity.get("kind") == "constant":
            data['alpha'] = float(opacity["value"])
        elif opacity.get("kind") == "unresolved":
            unresolved_warnings.append(
                f"Material '{material.name}': Unable to resolve the Mix Shader factor for opacity "
                f"through chain: {' -> '.join(opacity.get('provenance') or [])}"
            )
        else:
            input_graphs['opacity'] = opacity
        data['is_transparent'] = True
    emission = closure.get("emission")
    if emission is not None:
        color_socket = emission.inputs.get('Color') if hasattr(emission, "inputs") else None
        if color_socket is not None and getattr(color_socket, "is_linked", False):
            color = _core._resolve_socket_value(color_socket, set(), None, [], cache, expected_type='color3')
        else:
            value = _core._socket_default_value(color_socket) if color_socket is not None else (1.0, 1.0, 1.0)
            color = _core._constant_expr(_core._coerce_constant_value(value, 'color'))
        strength = _core._math_socket_expr(emission, 1, visited, None, provenance, cache) or _core._constant_expr(1.0)
        if color is None or color.get("kind") == "unresolved":
            unresolved_warnings.append(
                f"Material '{material.name}': Unable to resolve the added Emission shader's Color."
            )
            return
        if strength.get("kind") == "constant" and color.get("kind") == "constant":
            added = _core._constant_expr([float(c) * float(strength["value"]) for c in color["value"][:3]])
        elif _core._expr_is_constant(strength, 1.0):
            added = color
        else:
            broadcast = _core._make_node_expr(
                _core._nodedef_for("combine3", "color3"), {"in1": strength, "in2": strength, "in3": strength}
            )
            added = _core._make_node_expr(_core._nodedef_for("multiply", "color3"), {"in1": color, "in2": broadcast})
        base_color = [float(c) for c in (data.get('emission_color') or [0.0, 0.0, 0.0])[:3]]
        base_strength = float(data.get('emission_strength', 1.0))
        base = [c * base_strength for c in base_color]
        if any(abs(c) > 1e-6 for c in base):
            if added.get("kind") == "constant":
                total = _core._constant_expr([a + b for a, b in zip(added["value"], base)])
            else:
                total = _core._make_node_expr(
                    _core._nodedef_for("add", "color3"), {"in1": added, "in2": _core._constant_expr(base)}
                )
        else:
            total = added
        data.pop('emission_color', None)
        data['emission_strength'] = 1.0
        if total.get("kind") == "constant":
            data['emission_color'] = list(total["value"])
        else:
            input_graphs['_emissionColor'] = total
