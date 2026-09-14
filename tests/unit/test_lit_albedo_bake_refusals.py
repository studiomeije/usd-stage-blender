"""Material Color Only keeps, or refuses, the Principled inputs its rebuild drops.

The bake rebuilds each material as a fresh Principled BSDF carrying only the
baked Base Color, Roughness and Alpha and a passed-through Normal and
Metallic. Measured on the audit scene: a direct export authored specularIOR
2, baseDiffuseRoughness 0.5, sheenColor (1, 1, 1) and subsurfaceWeight 1,
and the Material Color Only bake of the same material authored 1.5, 0,
(0, 0, 0) and 0, the fresh node's values, without a word. IOR and Diffuse
Roughness change nothing the colour bake reads, so their constants are copied
onto the rebuild; Sheen and Subsurface are refused like the other dropped
controls, and so is a linked IOR or Diffuse Roughness.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.bake_textures import (  # noqa: E402
    _source_principled_constants,
    _validate_lit_albedo_principled_inputs,
)
from Plugin.export.materials.extract.core import _PRINCIPLED_DEFAULTS  # noqa: E402


class _Socket:
    def __init__(self, name, value):
        self.name = name
        self.default_value = value
        self.is_linked = False
        self.links = []


class _Inputs(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _principled(**overrides):
    """A Principled BSDF at Blender 5.2's defaults, with overrides."""
    inputs = _Inputs()
    for name, value in _PRINCIPLED_DEFAULTS:
        inputs[name] = _Socket(name, overrides.get(name, value))
    inputs["Weight"] = _Socket("Weight", 1.0)
    return types.SimpleNamespace(type="BSDF_PRINCIPLED", name="Principled BSDF", inputs=inputs)


_MATERIAL = types.SimpleNamespace(name="Audit")


def test_a_stock_principled_bakes():
    _validate_lit_albedo_principled_inputs(_MATERIAL, _principled())


@pytest.mark.parametrize("name, value", [
    ("Sheen Weight", 1.0),
    ("Subsurface Weight", 1.0),
])
def test_an_input_the_rebuild_would_reset_is_refused_by_name(name, value):
    with pytest.raises(RuntimeError) as refusal:
        _validate_lit_albedo_principled_inputs(_MATERIAL, _principled(**{name: value}))
    assert f"Principled '{name}' is not preserved by Material Color Only bake" in str(refusal.value)
    assert "Lighting & Shadows" in str(refusal.value)


def test_constant_ior_and_diffuse_roughness_are_carried_onto_the_rebuild():
    principled = _principled(**{"IOR": 2.0, "Diffuse Roughness": 0.5})
    _validate_lit_albedo_principled_inputs(_MATERIAL, principled)
    assert _source_principled_constants(principled) == {"IOR": 2.0, "Diffuse Roughness": 0.5}


@pytest.mark.parametrize("name", ["IOR", "Diffuse Roughness"])
def test_a_linked_carried_input_is_refused(name):
    principled = _principled()
    principled.inputs[name].is_linked = True
    with pytest.raises(RuntimeError, match=f"'{name}' is linked"):
        _validate_lit_albedo_principled_inputs(_MATERIAL, principled)
    assert name not in _source_principled_constants(principled)
