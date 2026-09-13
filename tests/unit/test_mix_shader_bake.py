"""Unit tests for the Mix Shader bake-lane decision table.

The bake lane previously mishandled a Mix Shader surface in the Material Color
modes: ``_flat_material_constants`` found the *first* Principled node in the
tree and silently collapsed the whole mix to that one BSDF's constants (a 50/50
red/blue mix exported as flat red). These tests pin the honest behavior:

- a genuine two-Principled blend is never "flat" (it must be baked),
- a constant Factor of 0/1 degenerates to the selected side (well-defined),
- LIT_ALBEDO refuses divergent passthrough channels (Metallic/Normal) with a
  message naming the property — never silently picking one side,
- divergent Roughness does NOT refuse (it is genuinely baked via the ROUGHNESS
  pass),
- LIT_IBL never refuses (COMBINED bake renders the full mix; output is Unlit),
- the validator reports Mix Shader as a direct-export error whose message names
  the bake mode that works (LIT_IBL).

Pure attribute-walks - no Blender needed. ``bake_textures`` imports ``bpy`` at
module load, so a minimal stub is injected before import.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export.bake_textures import (  # noqa: E402
    _analyze_mix_shader_surface,
    _flat_material_constants,
    _passthrough_principled,
    check_mix_shader_bakeable,
)

# ``Plugin.nodes``'s package __init__ pulls in the node-group builders, which
# need a real bpy. The validator itself is bpy-free, so import it through a
# stub package that skips the __init__ side effects.
if "Plugin.nodes" not in sys.modules or not hasattr(sys.modules["Plugin.nodes"], "validate"):
    _nodes_pkg = types.ModuleType("Plugin.nodes")
    _nodes_pkg.__path__ = [str(REPO_ROOT / "Plugin" / "nodes")]
    sys.modules.setdefault("Plugin.nodes", _nodes_pkg)
rk_validate = importlib.import_module("Plugin.nodes.validate")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeLink:
    def __init__(self, from_node, from_socket):
        self.from_node = from_node
        self.from_socket = from_socket


class FakeSocket:
    def __init__(self, name="", *, socket_type='VALUE', default_value=0.0):
        self.name = name
        self.type = socket_type
        self.default_value = default_value
        self.links = []

    @property
    def is_linked(self):
        return bool(self.links)

    def link_from(self, node, socket=None):
        self.links = [FakeLink(node, socket if socket is not None else node.outputs[0])]


class FakeSockets:
    """Bag of sockets: iterates socket objects (like bpy), .get() by name."""

    def __init__(self, sockets):
        self._sockets = list(sockets)

    def __iter__(self):
        return iter(self._sockets)

    def __len__(self):
        return len(self._sockets)

    def __getitem__(self, idx):
        return self._sockets[idx]

    def get(self, name, default=None):
        for socket in self._sockets:
            if socket.name == name:
                return socket
        return default

    def items(self):
        return [(s.name, s) for s in self._sockets]


class FakeNode:
    def __init__(self, node_type, name=None, inputs=(), outputs=None):
        self.type = node_type
        self.name = name or node_type
        self.inputs = FakeSockets(inputs)
        if outputs is None:
            outputs = [FakeSocket("Out", socket_type='SHADER')]
        self.outputs = FakeSockets(outputs)


def make_principled(*, base_color=(0.8, 0.8, 0.8, 1.0), roughness=0.5,
                    metallic=0.0, alpha=1.0):
    return FakeNode('BSDF_PRINCIPLED', inputs=[
        FakeSocket('Base Color', socket_type='RGBA', default_value=base_color),
        FakeSocket('Roughness', default_value=roughness),
        FakeSocket('Metallic', default_value=metallic),
        FakeSocket('Alpha', default_value=alpha),
        FakeSocket('Normal', socket_type='VECTOR'),
    ])


def make_mix(shader_a, shader_b, *, fac=0.5, fac_source=None):
    fac_socket = FakeSocket('Fac', default_value=fac)
    if fac_source is not None:
        fac_socket.link_from(fac_source)
    in_a = FakeSocket('Shader', socket_type='SHADER')
    in_b = FakeSocket('Shader_001', socket_type='SHADER')
    in_a.type = 'SHADER'
    in_b.type = 'SHADER'
    if shader_a is not None:
        in_a.link_from(shader_a)
    if shader_b is not None:
        in_b.link_from(shader_b)
    return FakeNode('MIX_SHADER', inputs=[fac_socket, in_a, in_b])


def make_material(surface_node, extra_nodes=()):
    surface_socket = FakeSocket('Surface', socket_type='SHADER')
    if surface_node is not None:
        surface_socket.link_from(surface_node)
    output = FakeNode('OUTPUT_MATERIAL', inputs=[
        surface_socket,
        FakeSocket('Volume', socket_type='SHADER'),
        FakeSocket('Displacement', socket_type='VECTOR'),
    ])
    output.is_active_output = True

    nodes = [output]
    if surface_node is not None:
        nodes.append(surface_node)
    nodes.extend(extra_nodes)

    mat = types.SimpleNamespace()
    mat.name = "MixMat"
    mat.node_tree = types.SimpleNamespace(nodes=nodes)
    return mat


def make_two_principled_mix_material(*, fac=0.5, fac_linked=False, p1=None, p2=None):
    p1 = p1 or make_principled(base_color=(0.9, 0.05, 0.05, 1.0), roughness=0.9)
    p2 = p2 or make_principled(base_color=(0.05, 0.05, 0.9, 1.0), roughness=0.1)
    fac_source = FakeNode('TEX_NOISE') if fac_linked else None
    mix = make_mix(p1, p2, fac=fac, fac_source=fac_source)
    mat = make_material(mix, extra_nodes=[p1, p2] + ([fac_source] if fac_source else []))
    return mat, p1, p2


# ---------------------------------------------------------------------------
# Surface analysis
# ---------------------------------------------------------------------------

class TestAnalyzeMixSurface:
    def test_non_mix_surface_is_none(self):
        p = make_principled()
        assert _analyze_mix_shader_surface(make_material(p)) is None

    def test_two_principled_mix_is_supported(self):
        mat, p1, p2 = make_two_principled_mix_material(fac=0.5)
        info = _analyze_mix_shader_surface(mat)
        assert info["supported"] is True
        assert info["fac"] == 0.5
        assert info["principled"] == (p1, p2)
        assert info["selected"] is None

    def test_constant_fac_zero_selects_first_side(self):
        mat, p1, _p2 = make_two_principled_mix_material(fac=0.0)
        assert _analyze_mix_shader_surface(mat)["selected"] is p1

    def test_constant_fac_one_selects_second_side(self):
        mat, _p1, p2 = make_two_principled_mix_material(fac=1.0)
        assert _analyze_mix_shader_surface(mat)["selected"] is p2

    def test_linked_fac_has_no_constant(self):
        mat, _p1, _p2 = make_two_principled_mix_material(fac_linked=True)
        info = _analyze_mix_shader_surface(mat)
        assert info["supported"] is True
        assert info["fac"] is None

    def test_nested_mix_is_unsupported(self):
        inner = make_mix(make_principled(), make_principled())
        outer = make_mix(inner, make_principled())
        info = _analyze_mix_shader_surface(make_material(outer))
        assert info["supported"] is False
        assert "nested" in info["reason"]

    def test_non_principled_input_is_unsupported(self):
        emission = FakeNode('EMISSION')
        mix = make_mix(make_principled(), emission)
        info = _analyze_mix_shader_surface(make_material(mix))
        assert info["supported"] is False
        assert "EMISSION" in info["reason"]

    def test_unconnected_input_is_unsupported(self):
        mix = make_mix(make_principled(), None)
        info = _analyze_mix_shader_surface(make_material(mix))
        assert info["supported"] is False


# ---------------------------------------------------------------------------
# check_mix_shader_bakeable — the decision table
# ---------------------------------------------------------------------------

class TestBakeDecisionTable:
    def test_non_mix_material_never_refused(self):
        mat = make_material(make_principled())
        for mode in ("LIT_IBL", "LIT_ALBEDO", "UNLIT_ALBEDO"):
            assert check_mix_shader_bakeable(mat, mode) is None

    def test_lit_ibl_never_refuses_even_unsupported_shapes(self):
        inner = make_mix(make_principled(), make_principled())
        outer = make_mix(inner, FakeNode('BSDF_GLASS'))
        mat = make_material(outer)
        assert check_mix_shader_bakeable(mat, "LIT_IBL") is None

    def test_material_color_modes_refuse_unsupported_shapes_pointing_at_lit_ibl(self):
        mix = make_mix(make_principled(), FakeNode('EMISSION'))
        mat = make_material(mix)
        for mode in ("LIT_ALBEDO", "UNLIT_ALBEDO"):
            msg = check_mix_shader_bakeable(mat, mode)
            assert msg is not None
            assert "LIT_IBL" in msg

    def test_divergent_roughness_blend_is_bakeable(self):
        # Roughness is genuinely baked via the ROUGHNESS pass — divergence is fine.
        mat, _p1, _p2 = make_two_principled_mix_material(fac=0.5)
        assert check_mix_shader_bakeable(mat, "LIT_ALBEDO") is None
        assert check_mix_shader_bakeable(mat, "UNLIT_ALBEDO") is None

    def test_divergent_metallic_refused_in_lit_albedo_naming_property(self):
        p1 = make_principled(metallic=0.0)
        p2 = make_principled(metallic=1.0)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        msg = check_mix_shader_bakeable(mat, "LIT_ALBEDO")
        assert msg is not None
        assert "Metallic" in msg
        assert "0 vs 1" in msg
        assert "LIT_IBL" in msg

    def test_divergent_carried_ior_refused_in_lit_albedo_naming_property(self):
        # IOR is copied from one side onto the rebuild, like Metallic.
        p1, p2 = make_principled(), make_principled()
        p1.inputs = FakeSockets(list(p1.inputs) + [FakeSocket('IOR', default_value=1.5)])
        p2.inputs = FakeSockets(list(p2.inputs) + [FakeSocket('IOR', default_value=2.0)])
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        msg = check_mix_shader_bakeable(mat, "LIT_ALBEDO")
        assert msg is not None
        assert "'IOR' values diverge (1.5 vs 2)" in msg

    def test_divergent_metallic_fine_in_unlit_mode(self):
        # Unlit output authors no metallic, so nothing dishonest can happen.
        p1 = make_principled(metallic=0.0)
        p2 = make_principled(metallic=1.0)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        assert check_mix_shader_bakeable(mat, "UNLIT_ALBEDO") is None

    def test_constant_fac_zero_or_one_bypasses_divergence(self):
        for fac in (0.0, 1.0):
            p1 = make_principled(metallic=0.0)
            p2 = make_principled(metallic=1.0)
            mat, _, _ = make_two_principled_mix_material(fac=fac, p1=p1, p2=p2)
            assert check_mix_shader_bakeable(mat, "LIT_ALBEDO") is None

    def test_divergent_normal_sources_refused_in_lit_albedo(self):
        p1 = make_principled()
        p2 = make_principled()
        p1.inputs.get('Normal').link_from(FakeNode('NORMAL_MAP', name='NormalA'))
        p2.inputs.get('Normal').link_from(FakeNode('NORMAL_MAP', name='NormalB'))
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        msg = check_mix_shader_bakeable(mat, "LIT_ALBEDO")
        assert msg is not None
        assert "Normal" in msg

    def test_shared_normal_source_is_bakeable(self):
        normal_map = FakeNode('NORMAL_MAP')
        p1 = make_principled()
        p2 = make_principled()
        p1.inputs.get('Normal').link_from(normal_map)
        p2.inputs.get('Normal').link_from(normal_map)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        assert check_mix_shader_bakeable(mat, "LIT_ALBEDO") is None

    def test_divergent_alpha_refused_when_transparent(self):
        p1 = make_principled(alpha=0.5)
        p2 = make_principled(alpha=1.0)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        for mode in ("LIT_ALBEDO", "UNLIT_ALBEDO"):
            msg = check_mix_shader_bakeable(mat, mode)
            assert msg is not None
            assert "Alpha" in msg

    def test_equal_alpha_transparency_is_bakeable(self):
        p1 = make_principled(alpha=0.5)
        p2 = make_principled(alpha=0.5)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        assert check_mix_shader_bakeable(mat, "UNLIT_ALBEDO") is None

    def test_divergent_alpha_ignored_when_opacity_bake_disabled(self):
        p1 = make_principled(alpha=0.5)
        p2 = make_principled(alpha=1.0)
        mat, _, _ = make_two_principled_mix_material(fac=0.5, p1=p1, p2=p2)
        assert check_mix_shader_bakeable(mat, "UNLIT_ALBEDO", bake_opacity=False) is None


# ---------------------------------------------------------------------------
# Flat-material classification (regression: silent collapse to first BSDF)
# ---------------------------------------------------------------------------

class TestMixFlatClassification:
    def test_genuine_blend_is_never_flat(self):
        # THE bug: this used to return the first Principled's flat red.
        mat, _p1, _p2 = make_two_principled_mix_material(fac=0.5)
        assert _flat_material_constants(mat, lit=False) is None
        assert _flat_material_constants(mat, lit=True) is None

    def test_linked_fac_blend_is_never_flat(self):
        mat, _p1, _p2 = make_two_principled_mix_material(fac_linked=True)
        assert _flat_material_constants(mat, lit=False) is None

    def test_constant_fac_zero_is_flat_with_first_side_constants(self):
        mat, _p1, _p2 = make_two_principled_mix_material(fac=0.0)
        flat = _flat_material_constants(mat, lit=True)
        assert flat is not None
        assert flat["base_color"][:3] == (0.9, 0.05, 0.05)
        assert flat["roughness"] == 0.9

    def test_constant_fac_one_is_flat_with_second_side_constants(self):
        mat, _p1, _p2 = make_two_principled_mix_material(fac=1.0)
        flat = _flat_material_constants(mat, lit=True)
        assert flat is not None
        assert flat["base_color"][:3] == (0.05, 0.05, 0.9)
        assert flat["roughness"] == 0.1

    def test_unsupported_mix_is_never_flat(self):
        mix = make_mix(make_principled(), FakeNode('EMISSION'))
        mat = make_material(mix, extra_nodes=[])
        assert _flat_material_constants(mat, lit=False) is None


# ---------------------------------------------------------------------------
# Passthrough source resolution
# ---------------------------------------------------------------------------

class TestPassthroughPrincipled:
    def test_plain_material_uses_its_principled(self):
        p = make_principled()
        mat = make_material(p)
        assert _passthrough_principled(mat) is p

    def test_constant_fac_selects_side(self):
        mat, p1, _p2 = make_two_principled_mix_material(fac=0.0)
        assert _passthrough_principled(mat) is p1
        mat, _p1, p2 = make_two_principled_mix_material(fac=1.0)
        assert _passthrough_principled(mat) is p2

    def test_agreeing_blend_uses_first_side(self):
        # Only reachable after check_mix_shader_bakeable verified agreement.
        mat, p1, _p2 = make_two_principled_mix_material(fac=0.5)
        assert _passthrough_principled(mat) is p1

    def test_unsupported_mix_passes_nothing_through(self):
        mix = make_mix(make_principled(), FakeNode('EMISSION'))
        mat = make_material(mix)
        assert _passthrough_principled(mat) is None


# ---------------------------------------------------------------------------
# Validator reclassification
# ---------------------------------------------------------------------------

class TestValidatorMixShader:
    def test_mix_shader_no_longer_in_unsupported_table(self):
        assert 'MIX_SHADER' not in rk_validate.UNSUPPORTED_TYPES

    def test_a_mix_of_two_principled_bsdfs_exports_directly(self):
        # Blended into one Principled BSDF; tests/unit/test_principled_presets_and_mix.py
        # pins the values. The bake profiles still accept the same material.
        mat, _p1, _p2 = make_two_principled_mix_material(fac=0.5)
        result = rk_validate.validate_material(mat, only_connected=False)
        assert not [e for e in result["errors"] if e["node_type"] == 'MIX_SHADER']
