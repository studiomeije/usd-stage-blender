"""Unit tests for ``_flat_material_constants`` linked-socket disqualification.

Regression guard: a material with no image textures but a *linked* (procedural)
Alpha chain must NOT be short-circuited as flat - that silently exported
procedurally-transparent materials as fully opaque (alpha coerced to 1.0). In
Lit-PBR mode the same applies to linked Roughness/Metallic chains, whose
variation would otherwise collapse to the Principled defaults.

These are pure attribute-walks - no Blender needed. ``bake_textures`` imports
``bpy`` at module load, so a minimal stub is injected before import.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.bake_textures import _flat_material_constants  # noqa: E402


class FakeSocket:
    def __init__(self, *, is_linked=False, default_value=0.5):
        self.is_linked = is_linked
        self.default_value = default_value


class FakeInputs(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakePrincipled:
    type = 'BSDF_PRINCIPLED'

    def __init__(self, *, alpha_linked=False, roughness_linked=False, metallic_linked=False):
        self.inputs = FakeInputs({
            'Base Color': FakeSocket(default_value=(0.8, 0.2, 0.1, 1.0)),
            'Roughness': FakeSocket(is_linked=roughness_linked, default_value=0.4),
            'Metallic': FakeSocket(is_linked=metallic_linked, default_value=0.0),
            'Alpha': FakeSocket(is_linked=alpha_linked, default_value=1.0),
        })


class FakeNodeTree:
    def __init__(self, nodes):
        self.nodes = nodes


class FakeMaterial:

    def __init__(self, principled):
        surface = types.SimpleNamespace(
            is_linked=True,
            links=[types.SimpleNamespace(from_node=principled)],
        )
        output = types.SimpleNamespace(
            type='OUTPUT_MATERIAL',
            is_active_output=True,
            inputs={'Surface': surface},
        )
        self.node_tree = FakeNodeTree([principled, output])


def test_plain_constant_material_is_flat():
    flat = _flat_material_constants(FakeMaterial(FakePrincipled()), lit=True)
    assert flat is not None
    assert flat["base_color"][:3] == (0.8, 0.2, 0.1)
    assert flat["alpha"] == 1.0


def test_linked_alpha_disqualifies_in_every_mode():
    # Procedural transparency needs the real opacity bake - never a constant 1.0.
    mat = FakeMaterial(FakePrincipled(alpha_linked=True))
    assert _flat_material_constants(mat, lit=True) is None
    assert _flat_material_constants(mat, lit=False) is None


def test_linked_roughness_disqualifies_only_when_lit():
    # Unlit output ignores roughness, so a linked chain doesn't force a bake there.
    mat = FakeMaterial(FakePrincipled(roughness_linked=True))
    assert _flat_material_constants(mat, lit=True) is None
    assert _flat_material_constants(mat, lit=False) is not None


def test_linked_metallic_disqualifies_only_when_lit():
    mat = FakeMaterial(FakePrincipled(metallic_linked=True))
    assert _flat_material_constants(mat, lit=True) is None
    assert _flat_material_constants(mat, lit=False) is not None


def test_image_texture_always_disqualifies():
    tex = types.SimpleNamespace(type='TEX_IMAGE')
    mat = FakeMaterial(FakePrincipled())
    mat.node_tree.nodes.append(tex)
    assert _flat_material_constants(mat, lit=False) is None
