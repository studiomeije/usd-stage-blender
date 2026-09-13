"""Unit tests for RealityKit animation-library metadata authoring."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pxr import Gf, Usd, UsdGeom  # noqa: E402

from Plugin.export.usd_animation_library import author_animation_library  # noqa: E402


class _Diagnostics:
    def __init__(self):
        self.data = {
            "animations": {
                "segments": [
                    {"name": "Idle", "start_frame": 0},
                    {"name": "Run", "start_frame": 30},
                ],
            }
        }
        self.warnings: list[str] = []

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)


def _make_stage():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/root/Character", "SkelRoot")
    stage.DefinePrim("/root/Character/Armature", "Skeleton")
    stage.DefinePrim("/root/Character/Armature/Animation", "SkelAnimation")
    stage.SetStartTimeCode(0)
    stage.SetTimeCodesPerSecond(30)
    return stage


def _make_transform_stage(*, animate_child: bool):
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())
    animated = UsdGeom.Xform.Define(stage, "/root/Child") if animate_child else root
    op = animated.AddTranslateOp()
    op.Set(Gf.Vec3d(0, 0, 0), 0)
    op.Set(Gf.Vec3d(1, 0, 0), 30)
    stage.SetStartTimeCode(0)
    stage.SetEndTimeCode(60)
    stage.SetTimeCodesPerSecond(30)
    return stage


def test_animation_library_is_skipped_by_default():
    stage = _make_stage()
    diagnostics = _Diagnostics()
    settings = SimpleNamespace(export_animation=True)

    author_animation_library(stage, settings, diagnostics)

    assert not stage.GetPrimAtPath("/root/AnimationLibrary")
    # Leaving the opt-in library off is the normal case, not something to fix.
    assert not diagnostics.warnings


def test_skipped_animation_library_removes_existing_metadata():
    stage = _make_stage()
    stage.DefinePrim("/root/AnimationLibrary", "RealityKitComponent")
    diagnostics = _Diagnostics()
    settings = SimpleNamespace(
        export_animation=True,
        author_animation_library=False,
    )

    author_animation_library(stage, settings, diagnostics)

    assert not stage.GetPrimAtPath("/root/AnimationLibrary")


def test_animation_library_opt_in_authors_single_source():
    stage = _make_stage()
    diagnostics = _Diagnostics()
    settings = SimpleNamespace(
        export_animation=True,
        author_animation_library=True,
    )

    author_animation_library(stage, settings, diagnostics)

    library = stage.GetPrimAtPath("/root/AnimationLibrary")
    assert library
    clip_defs = list(library.GetChildren())
    assert len(clip_defs) == 1
    assert clip_defs[0].GetName() == "Clip_default_subtree_animation"
    assert clip_defs[0].GetAttribute("sourceAnimationName").Get() == "default subtree animation"
    assert clip_defs[0].GetAttribute("clipNames").Get() == ["Idle", "Run"]
    assert clip_defs[0].GetAttribute("startTimes").Get() == [0, 1]


def test_root_transform_animation_uses_default_subtree_source():
    stage = _make_transform_stage(animate_child=False)
    diagnostics = _Diagnostics()
    settings = SimpleNamespace(export_animation=True, author_animation_library=True)

    author_animation_library(stage, settings, diagnostics)

    clip = stage.GetPrimAtPath(
        "/root/AnimationLibrary/Clip_default_subtree_animation"
    )
    assert clip
    assert clip.GetAttribute("sourceAnimationName").Get() == "default subtree animation"


def test_child_transform_animation_uses_default_subtree_source():
    stage = _make_transform_stage(animate_child=True)
    diagnostics = _Diagnostics()
    settings = SimpleNamespace(export_animation=True, author_animation_library=True)

    author_animation_library(stage, settings, diagnostics)

    clip = stage.GetPrimAtPath(
        "/root/AnimationLibrary/Clip_default_subtree_animation"
    )
    assert clip
    assert clip.GetAttribute("sourceAnimationName").Get() == "default subtree animation"


def test_clip_offsets_are_relative_to_nonzero_stage_start():
    stage = _make_transform_stage(animate_child=True)
    stage.SetStartTimeCode(1)
    diagnostics = _Diagnostics()
    diagnostics.data["animations"]["segments"] = [
        {"name": "Idle", "start_frame": 1},
        {"name": "Run", "start_frame": 31},
    ]
    settings = SimpleNamespace(export_animation=True, author_animation_library=True)

    author_animation_library(stage, settings, diagnostics)

    clip = stage.GetPrimAtPath(
        "/root/AnimationLibrary/Clip_default_subtree_animation"
    )
    assert clip.GetAttribute("startTimes").Get() == [0, 1]
