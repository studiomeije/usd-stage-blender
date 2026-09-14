"""Unit tests for the empty blend-shape animation fix.

``_complete_blend_shape_weights`` has three preservation branches that the
integration test cannot reach, because Blender only ever produces one of the
shapes. It also auto-skips wherever Blender is absent, so without these the
behaviour is unpinned on CI.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export.postprocess_usd import (  # noqa: E402
    _complete_blend_shape_weights,
)

pytest.importorskip("pxr")


def _stage_with_animation(tmp_path: Path, body: str):
    from pxr import Usd

    source = tmp_path / "anim.usda"
    source.write_text(
        '#usda 1.0\n(\n    defaultPrim = "root"\n)\n'
        'def Xform "root"\n{\n'
        '    def SkelAnimation "Anim"\n    {\n' + body + "\n    }\n}\n",
        encoding="utf-8",
    )
    return Usd.Stage.Open(str(source))


def _animation(stage):
    from pxr import UsdSkel

    prim = next(
        p for p in stage.TraverseAll() if p.GetTypeName() == "SkelAnimation"
    )
    return UsdSkel.Animation(prim)


def test_an_empty_declaration_is_cleared(tmp_path: Path) -> None:
    """Named shapes, no weights — the shape Reality Composer Pro refuses."""

    stage = _stage_with_animation(
        tmp_path,
        '        uniform token[] blendShapes = ["Squash", "Stretch"]\n'
        "        float[] blendShapeWeights",
    )
    _complete_blend_shape_weights(stage)

    animation = _animation(stage)
    assert not animation.GetBlendShapesAttr().HasAuthoredValue()
    assert not animation.GetBlendShapeWeightsAttr().HasAuthoredValue()


def test_animated_weights_are_left_alone(tmp_path: Path) -> None:
    """Time samples mean a real animation; clearing it would delete work."""

    from pxr import Usd

    stage = _stage_with_animation(
        tmp_path, '        uniform token[] blendShapes = ["Squash"]'
    )
    weights = _animation(stage).CreateBlendShapeWeightsAttr()
    weights.Set([0.0], Usd.TimeCode(1.0))
    weights.Set([1.0], Usd.TimeCode(24.0))

    _complete_blend_shape_weights(stage)

    animation = _animation(stage)
    assert list(animation.GetBlendShapesAttr().Get()) == ["Squash"]
    assert animation.GetBlendShapeWeightsAttr().GetTimeSamples() == [1.0, 24.0]


def test_a_static_non_zero_pose_is_left_alone(tmp_path: Path) -> None:
    """A held pose is a pose, not an absence of animation."""

    stage = _stage_with_animation(
        tmp_path,
        '        uniform token[] blendShapes = ["Squash"]\n'
        "        float[] blendShapeWeights = [0.75]",
    )
    _complete_blend_shape_weights(stage)

    animation = _animation(stage)
    assert list(animation.GetBlendShapesAttr().Get()) == ["Squash"]
    assert list(animation.GetBlendShapeWeightsAttr().Get()) == [0.75]


def test_an_all_zero_static_pose_is_cleared(tmp_path: Path) -> None:
    """All-zero weights say the same thing as no weights: everything at rest."""

    stage = _stage_with_animation(
        tmp_path,
        '        uniform token[] blendShapes = ["Squash", "Stretch"]\n'
        "        float[] blendShapeWeights = [0, 0]",
    )
    _complete_blend_shape_weights(stage)

    assert not _animation(stage).GetBlendShapesAttr().HasAuthoredValue()


def test_an_animation_without_blend_shapes_is_untouched(tmp_path: Path) -> None:
    stage = _stage_with_animation(
        tmp_path, '        uniform token[] joints = ["joint1"]'
    )
    _complete_blend_shape_weights(stage)

    animation = _animation(stage)
    assert list(animation.GetJointsAttr().Get()) == ["joint1"]
    assert not animation.GetBlendShapesAttr().HasAuthoredValue()
