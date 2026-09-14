"""A refusal's remedy must be one that works.

Every translated PBR material terminates in RealityKit PBR Surface 2, so the
validator refuses only what that surface cannot carry, and the only honest
remedy for those is to bake. This file pins the messages for the two Principled
controls PBR Surface 2 lacks - coat tint and sheen roughness - and the
value-policy refusal for a coloured or overbright specular tint, and it pins
that a stock Principled node raises nothing at all.

It once also pinned that the validator would not send artists to OpenPBR for
controls OpenPBR dropped. OpenPBR is gone, along with the portable surface, so
there is no longer a second profile to route anyone to.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Plugin.nodes imports bpy at module level. Seed the stub the sibling tests
# use, so this file collects on its own and not only behind one that already
# did.
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.nodes import validate  # noqa: E402

# Blender 5.2 Principled defaults, read from the live node RNA. A stub at these
# values raises no issue at all, so every issue below is caused by one override.
_NEUTRAL = {
    "Base Color": (0.8, 0.8, 0.8, 1.0),
    "Metallic": 0.0,
    "Roughness": 0.5,
    "IOR": 1.5,
    "Alpha": 1.0,
    "Thin Wall": False,
    "Normal": (0.0, 0.0, 0.0),
    "Weight": 0.0,
    "Diffuse Roughness": 0.0,
    "Subsurface Weight": 0.0,
    "Subsurface Radius": (1.0, 0.2, 0.1),
    "Subsurface Scale": 0.005,
    "Subsurface IOR": 1.4,
    "Subsurface Anisotropy": 0.0,
    "Specular IOR Level": 0.5,
    "Specular Tint": (1.0, 1.0, 1.0, 1.0),
    "Anisotropic": 0.0,
    "Anisotropic Rotation": 0.0,
    "Tangent": (0.0, 0.0, 0.0),
    "Transmission Weight": 0.0,
    "Coat Weight": 0.0,
    "Coat Roughness": 0.03,
    "Coat IOR": 1.5,
    "Coat Tint": (1.0, 1.0, 1.0, 1.0),
    "Coat Normal": (0.0, 0.0, 0.0),
    "Sheen Weight": 0.0,
    "Sheen Roughness": 0.5,
    "Sheen Tint": (1.0, 1.0, 1.0, 1.0),
    "Emission Color": (1.0, 1.0, 1.0, 1.0),
    "Emission Strength": 0.0,
    "Thin Film Thickness": 0.0,
    "Thin Film IOR": 1.33,
}


def _principled(**overrides):
    inputs = {
        name: SimpleNamespace(default_value=overrides.get(name, value), is_linked=False)
        for name, value in _NEUTRAL.items()
    }
    return SimpleNamespace(inputs=inputs)


def _issues(**overrides) -> list[str]:
    return validate._unsupported_principled_inputs(_principled(**overrides))


def test_a_neutral_node_raises_nothing():
    assert _issues() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"IOR": 1.33},
        {"Diffuse Roughness": 0.5},
        {"Subsurface Weight": 0.5},
        {"Coat Weight": 1.0, "Coat IOR": 1.7},
        {"Sheen Weight": 0.5},
        {"Sheen Weight": 0.5, "Sheen Tint": (1.0, 0.5, 0.5, 1.0)},
    ],
)
def test_controls_pbr_surface_2_carries_are_not_refused(overrides):
    """These used to be refused by the portable surface. PBR Surface 2 has them."""
    assert _issues(**overrides) == [], overrides


def test_coat_tint_says_bake():
    """No RealityKit surface carries a coat tint."""
    (issue,) = [
        i for i in _issues(**{"Coat Weight": 1.0, "Coat Tint": (1.0, 0.2, 0.2, 1.0)})
        if "Coat Tint" in i
    ]
    assert "bake" in issue
    assert "select" not in issue


def test_sheen_roughness_says_bake():
    """PBR Surface 2 has sheenColor but no sheen roughness."""
    (issue,) = [
        i for i in _issues(**{"Sheen Weight": 1.0, "Sheen Roughness": 0.9})
        if "Sheen Roughness" in i
    ]
    assert "bake" in issue
    assert "select" not in issue


def test_coloured_overbright_specular_tint_is_a_value_policy_refusal():
    """A colour the surface's semantics are not verified for; bake or set a value."""
    issues = [
        i for i in _issues(**{"Specular Tint": (1.8, 0.4, 0.4, 1.0)})
        if "Specular Tint" in i
    ]
    assert issues
    for issue in issues:
        assert "bake" in issue
        assert "PBR Surface 2" in issue
