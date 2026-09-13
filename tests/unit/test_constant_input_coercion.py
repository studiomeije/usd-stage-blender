"""A constant squared up to its MaterialX input's type, as Blender converts sockets.

Blender repeats a single value in every component and gives a colour an alpha
of 1. A multi-component constant reaching a scalar input cannot be converted
at authoring time, because whether it was a colour (grey) or a vector (mean)
is gone; its first component is neither, so the export raises instead.
"""

from __future__ import annotations

import pytest

from Plugin.export.materials.conversions import _coerce_value_to_input_type


def _coerce(value, type_name):
    return _coerce_value_to_input_type(value, {"type": type_name})


def test_a_single_value_repeats_in_every_component():
    assert _coerce([0.3], "color3") == [0.3, 0.3, 0.3]
    assert _coerce([0.3], "vector3") == [0.3, 0.3, 0.3]
    assert _coerce([0.3], "vector2") == [0.3, 0.3]
    assert _coerce([0.3], "vector4") == [0.3, 0.3, 0.3, 0.3]
    assert _coerce([0.3], "color4") == [0.3, 0.3, 0.3, 1.0]
    assert _coerce([0.3], "float") == 0.3


def test_short_values_gain_alpha_one_or_zeros_and_long_ones_are_cut():
    assert _coerce([0.1, 0.2, 0.3], "color4") == [0.1, 0.2, 0.3, 1.0]
    assert _coerce([0.1, 0.2, 0.3], "vector4") == [0.1, 0.2, 0.3, 0.0]
    assert _coerce([0.1, 0.2], "vector3") == [0.1, 0.2, 0.0]
    assert _coerce([0.1, 0.2, 0.3, 0.4], "color3") == [0.1, 0.2, 0.3]


def test_a_multi_component_constant_on_a_scalar_input_raises():
    for type_name in ("float", "integer"):
        with pytest.raises(ValueError, match="3-component constant reached"):
            _coerce([0.2, 0.5, 0.1], type_name)
