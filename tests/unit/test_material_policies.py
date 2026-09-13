import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.material_policies import (
    normalize_extracted_specular_tint,
    safe_overbright_achromatic_specular_tint,
)


def test_safe_policy_accepts_only_constant_achromatic_overbright_rgb():
    assert safe_overbright_achromatic_specular_tint((2.0, 2.0, 2.0, 1.0))
    assert safe_overbright_achromatic_specular_tint((1.0, 1.0, 1.0, 1.0)) is None
    assert safe_overbright_achromatic_specular_tint((2.0, 1.5, 1.0, 1.0)) is None
    assert safe_overbright_achromatic_specular_tint(
        (2.0, 2.0, 2.0, 1.0),
        linked=True,
    ) is None


def test_normalization_changes_only_the_ephemeral_extracted_dictionary():
    extracted = {
        "name": "Meshy",
        "specular_tint": [2.0, 2.0, 2.0],
    }

    audit = normalize_extracted_specular_tint(extracted)

    assert extracted["specular_tint"] == [1.0, 1.0, 1.0]
    assert audit == {
        "input": (2.0, 2.0, 2.0),
        "output": (1.0, 1.0, 1.0),
        "reason": "constant achromatic Specular Tint exceeds the supported [0, 1] range",
    }
