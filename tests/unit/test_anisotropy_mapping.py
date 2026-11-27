"""Blender's anisotropy maps onto PBR Surface 2 alpha for alpha.

Cycles: alpha_t = r^2 / s, alpha_b = r^2 s, s = sqrt(1 - 0.9 A).
RealityKit (measured, macOS 27.2.1): alpha_t = r'^2 (1 + L^2),
alpha_b = r'^2 (1 - L^2). See Plugin/export/materials/anisotropy.py.
"""

from __future__ import annotations

import math

import pytest

from Plugin.export.materials.anisotropy import realitykit_anisotropy


def _cycles(r, a):
    s = math.sqrt(1 - 0.9 * a)
    return r * r / s, r * r * s


def _realitykit(r, level):
    return r * r * (1 + level * level), r * r * (1 - level * level)


@pytest.mark.parametrize("a", [0.05, 0.25, 0.5, 0.75, 1.0])
@pytest.mark.parametrize("r", [0.1, 0.3, 0.6])
def test_both_alphas_match_cycles(r, a):
    level, _angle, scale = realitykit_anisotropy(a, 0.0)
    assert _realitykit(r * scale, level) == pytest.approx(_cycles(r, a), rel=1e-9)
    assert 0 < level < 1


def test_rotation_turns_the_other_way():
    """Blender turns counter-clockwise as the camera sees it, RealityKit clockwise."""
    assert realitykit_anisotropy(0.5, 0.125)[1] == pytest.approx(0.875)
    assert realitykit_anisotropy(0.5, 0.0)[1] == 0.0
    assert realitykit_anisotropy(0.5, 0.25)[1] == pytest.approx(0.75)


def test_the_measured_mapping_points():
    """The values the RealityKit render-back was made with."""
    assert realitykit_anisotropy(0.5, 0)[0] == pytest.approx(0.53882, abs=1e-5)
    assert realitykit_anisotropy(1.0, 0)[0] == pytest.approx(0.90453, abs=1e-5)
    assert 0.3 * realitykit_anisotropy(1.0, 0)[2] == pytest.approx(0.39564, abs=1e-5)


def test_a_linked_roughness_under_anisotropy_warns_where_it_clamps():
    from types import SimpleNamespace

    from Plugin.export.materials.anisotropy import roughness_notice

    def socket(value, linked=False):
        return SimpleNamespace(default_value=value, is_linked=linked)

    node = SimpleNamespace(inputs={"Anisotropic": socket(1.0), "Roughness": socket(0.5, True)})
    notice = roughness_notice(node)
    assert "multiplies it by 1.319" in notice and "above 0.758" in notice
    node.inputs["Roughness"] = socket(0.5)
    assert roughness_notice(node) is None
