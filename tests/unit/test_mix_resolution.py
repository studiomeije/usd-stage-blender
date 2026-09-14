"""Unit tests for Mix/MixRGB node classification in material extraction.

Guards the regression where a MULTIPLY mix (e.g. diffuse x AO) was treated as a
plain mix and collapsed to input B (the AO map) at Factor 1.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract.core import (
    _is_supported_mix,
    _mix_node_params,
    _mix_passthrough,
)


class FakeSocket:
    def __init__(self, *, is_linked=False, default_value=0.0):
        self.is_linked = is_linked
        self.default_value = default_value


class FakeInputs(dict):
    def get(self, key, default=None):  # noqa: D401 - dict-like socket map
        return super().get(key, default)


class FakeMixNode:
    type = 'MIX'

    def __init__(self, *, blend_type, factor, a_linked, b_linked):
        self.blend_type = blend_type
        self.inputs = FakeInputs(
            Factor=FakeSocket(default_value=factor),
            A=FakeSocket(is_linked=a_linked),
            B=FakeSocket(is_linked=b_linked),
        )


def _mix(blend_type, factor, a_linked=True, b_linked=True):
    return FakeMixNode(blend_type=blend_type, factor=factor, a_linked=a_linked, b_linked=b_linked)


def test_params_reads_blend_factor_and_sockets():
    blend, fac, a, b = _mix_node_params(_mix('MULTIPLY', 1.0))
    assert blend == 'MULTIPLY'
    assert fac == 1.0
    assert a.is_linked and b.is_linked


def test_factor_zero_is_passthrough_for_every_blend():
    # out = lerp(A, op(A, B), 0) == A regardless of blend mode.
    for blend in ('MIX', 'MULTIPLY', 'ADD', 'SUBTRACT'):
        assert _mix_passthrough(_mix(blend, 0.0)) == "A", blend


def test_plain_mix_factor_one_is_passthrough_of_b():
    assert _mix_passthrough(_mix('MIX', 1.0)) == "B"


def test_multiply_factor_one_is_not_a_passthrough():
    # The regression: MULTIPLY at Factor 1 is A*B, never just B.
    node = _mix('MULTIPLY', 1.0)
    assert _mix_passthrough(node) is None
    # ...but it IS expressible as a real MaterialX multiply node, so no bake.
    assert _is_supported_mix(node)


def test_combining_blends_supported_only_when_both_inputs_linked():
    assert _is_supported_mix(_mix('MULTIPLY', 1.0, a_linked=True, b_linked=True))
    assert not _is_supported_mix(_mix('MULTIPLY', 1.0, a_linked=True, b_linked=False))
    assert _is_supported_mix(_mix('ADD', 0.5))
    assert _is_supported_mix(_mix('SUBTRACT', 0.5))


def test_unhandled_blend_is_not_supported():
    # Blends with no MaterialX op emitter still require baking.
    assert not _is_supported_mix(_mix('DODGE', 1.0))


def test_linked_factor_is_not_a_constant_but_is_supported():
    # A wired Factor cannot be folded to a constant, but ND_mix_*'s ``mix``
    # input accepts a wired float, so the node is expressible without a bake.
    node = _mix('MIX', 1.0)
    node.inputs['Factor'].is_linked = True
    _, fac, _, _ = _mix_node_params(node)
    assert fac is None
    assert _is_supported_mix(node)


def test_linked_factor_is_supported_for_combining_blends():
    for blend in ('MULTIPLY', 'ADD', 'SUBTRACT'):
        node = _mix(blend, 0.5)
        node.inputs['Factor'].is_linked = True
        assert _is_supported_mix(node), blend


def test_linked_factor_does_not_rescue_an_unhandled_blend():
    node = _mix('DODGE', 0.5)
    node.inputs['Factor'].is_linked = True
    assert not _is_supported_mix(node)


def test_linked_factor_allows_constant_inputs_on_a_plain_mix():
    """ND_mix_* takes a wired weight and an unlinked A or B is its socket
    constant, so a Fresnel-driven blend between two colours is authored."""
    node = _mix('MIX', 0.5, a_linked=True, b_linked=False)
    node.inputs['Factor'].is_linked = True
    assert _is_supported_mix(node)
    node = _mix('MIX', 0.5, a_linked=False, b_linked=False)
    node.inputs['Factor'].is_linked = True
    assert _is_supported_mix(node)


def test_linked_factor_still_requires_both_inputs_on_a_combining_blend():
    node = _mix('MULTIPLY', 0.5, a_linked=True, b_linked=False)
    node.inputs['Factor'].is_linked = True
    assert not _is_supported_mix(node)


class _ExprSocket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name


class _ExprLink:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _ExprNode:
    pass


def _rgb_node(color):
    node = _ExprNode()
    node.type = 'RGB'
    node.name = 'RGB'
    node.outputs = {'Color': _ExprSocket(list(color), name='Color')}
    return node


def _value_node(value):
    node = _ExprNode()
    node.type = 'VALUE'
    node.name = 'Value'
    node.outputs = {'Value': _ExprSocket(value, name='Value')}
    return node


def _math_multiply_node(factor_value, multiplier):
    node = _ExprNode()
    node.type = 'MATH'
    node.name = 'Math'
    node.operation = 'MULTIPLY'
    node.use_clamp = False
    value = _value_node(factor_value)
    node.inputs = [
        _ExprSocket(
            linked=True, link=_ExprLink(value, value.outputs['Value'])
        ),
        _ExprSocket(multiplier),
    ]
    return node


def _resolvable_mix_with_linked_factor(blend='MIX'):
    node = _ExprNode()
    node.type = 'MIX'
    node.name = 'Mix'
    node.blend_type = blend
    factor_source = _math_multiply_node(0.5, 0.8)
    a_source = _rgb_node((1.0, 0.0, 0.0, 1.0))
    b_source = _rgb_node((0.0, 0.0, 1.0, 1.0))
    node.inputs = FakeInputs(
        Factor=_ExprSocket(
            linked=True,
            link=_ExprLink(factor_source, _ExprSocket(name='Value')),
        ),
        A=_ExprSocket(
            linked=True,
            link=_ExprLink(a_source, a_source.outputs['Color']),
        ),
        B=_ExprSocket(
            linked=True,
            link=_ExprLink(b_source, b_source.outputs['Color']),
        ),
    )
    return node


def _resolve_expr(node, expected_type='color3'):
    from Plugin.export.materials.extract import core

    output = _ExprSocket(name='Result')
    target = _ExprSocket(linked=True, link=_ExprLink(node, output))
    return core._resolve_socket_value(target, expected_type=expected_type)


def test_linked_factor_resolves_to_a_wired_mix_input():
    # A resolvable factor expression (Math multiply of a Value) must be wired
    # into ND_mix_color3's mix input rather than demanding a bake.
    expr = _resolve_expr(_resolvable_mix_with_linked_factor('MIX'))
    assert expr['kind'] == 'node'
    assert expr['node_id'] == 'ND_mix_color3'
    factor = expr['inputs']['mix']
    assert factor['kind'] == 'node'
    # Clamp Factor is on by default: Cycles saturates the weight.
    assert factor['node_id'] == 'ND_clamp_float'
    assert factor['inputs']['in']['node_id'] == 'ND_multiply_float'


def test_linked_factor_on_combining_blend_authors_lerp_of_the_blend():
    expr = _resolve_expr(_resolvable_mix_with_linked_factor('MULTIPLY'))
    assert expr['kind'] == 'node'
    assert expr['node_id'] == 'ND_mix_color3'
    blended = expr['inputs']['fg']
    assert blended['node_id'] == 'ND_multiply_color3'
    assert expr['inputs']['mix']['node_id'] == 'ND_clamp_float'
    assert expr['inputs']['mix']['inputs']['in']['node_id'] == 'ND_multiply_float'


def test_unresolvable_linked_factor_still_refuses():
    node = _resolvable_mix_with_linked_factor('MIX')
    bad = _ExprNode()
    bad.type = 'LIGHT_PATH'
    bad.name = 'Light Path'
    bad.inputs = FakeInputs()
    node.inputs['Factor'] = _ExprSocket(
        linked=True, link=_ExprLink(bad, _ExprSocket(name='Is Camera Ray'))
    )
    expr = _resolve_expr(node)
    assert expr['kind'] == 'unresolved'


def test_validator_mix_gate_agrees_on_linked_factor():
    # Capability parity: the validator's twin gate must accept and refuse the
    # same shapes the resolver does.
    from Plugin.nodes.validate import _is_supported_mix as validator_mix

    for blend, expected in (
        ('MIX', True),
        ('MULTIPLY', True),
        ('ADD', True),
        ('SUBTRACT', True),
        ('DODGE', False),
    ):
        node = _mix(blend, 0.5)
        node.inputs['Factor'].is_linked = True
        assert validator_mix(node) is expected, blend
        assert _is_supported_mix(node) is expected, blend
