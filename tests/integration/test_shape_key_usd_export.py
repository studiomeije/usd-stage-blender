"""Integration test - shape keys through the real USD export.

Blender does not export an unrigged shape-keyed mesh as an unrigged mesh. Shape
keys travel through USD's skeletal schema, so a scene with no armature still
gets a ``SkelRoot`` wrapper and a synthesized ``Skeleton``: one joint, identity
bind and rest transforms, every vertex fully weighted to it. That skeleton
deforms nothing - it is a carrier for the shapes.

Blender also names the shapes in the ``SkelAnimation``'s ``blendShapes`` when
nothing animates them, leaving ``blendShapeWeights`` with no value at all.
Reality Composer Pro refuses such a file:

    Failed to import blend shape animation: prim_path='.../Skel/Anim'

The export clears that empty declaration, which is what Apple's own shape-keyed
assets look like. The shapes must survive it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1)
obj = bpy.context.active_object
obj.name = "KeyedCube"
obj.data.name = "KeyedCubeMesh"

obj.shape_key_add(name="Basis", from_mix=False)
squash = obj.shape_key_add(name="Squash", from_mix=False)
for vertex in squash.data:
    vertex.co.z *= 0.5

assert not [o for o in bpy.data.objects if o.type == 'ARMATURE']
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


@pytest.fixture(scope="module")
def exported_stage(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("shapekeys")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "Keyed.blend"
    built = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python", str(script), "--", str(blend),
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    stage = workdir / "Keyed.usda"
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "Plugin"), "--json",
            "export", str(blend), "-o", str(stage), "--format", "USDA",
        ],
        capture_output=True, text=True, timeout=900,
    )
    envelope = json.loads(result.stdout)
    assert envelope.get("ok"), envelope
    return stage


def test_an_unanimated_skel_animation_declares_no_blend_shapes(
    exported_stage: Path,
) -> None:
    """The declaration Reality Composer Pro refuses must not be written."""

    from pxr import Usd, UsdSkel

    stage = Usd.Stage.Open(str(exported_stage))
    animations = [
        prim for prim in stage.TraverseAll()
        if prim.GetTypeName() == "SkelAnimation"
    ]
    assert animations, "Blender routes shape keys through a SkelAnimation"

    for prim in animations:
        animation = UsdSkel.Animation(prim)
        assert not animation.GetBlendShapesAttr().HasAuthoredValue(), (
            f"{prim.GetPath()} still claims a blend-shape animation it cannot "
            "supply weights for"
        )
        assert not animation.GetBlendShapeWeightsAttr().HasAuthoredValue()


def test_the_shapes_themselves_survive(exported_stage: Path) -> None:
    """Clearing the animation must not take the shape keys with it."""

    from pxr import Usd, UsdGeom, UsdSkel

    stage = Usd.Stage.Open(str(exported_stage))
    mesh = next(p for p in stage.TraverseAll() if p.IsA(UsdGeom.Mesh))
    binding = UsdSkel.BindingAPI(mesh)

    assert list(binding.GetBlendShapesAttr().Get() or ()) == ["Squash"]
    assert len(binding.GetBlendShapeTargetsRel().GetTargets()) == 1

    shapes = [p for p in stage.TraverseAll() if p.IsA(UsdSkel.BlendShape)]
    assert [p.GetName() for p in shapes] == ["Squash"]

    # The offsets are the movement, not the destination: halving z on a unit
    # cube moves every point a quarter toward the midplane.
    offsets = UsdSkel.BlendShape(shapes[0]).GetOffsetsAttr().Get()
    assert offsets, "the shape carries no displacement"
    for offset in offsets:
        assert abs(offset[0]) < 1e-6 and abs(offset[1]) < 1e-6
        assert abs(abs(offset[2]) - 0.25) < 1e-5


def test_the_carrier_skeleton_deforms_nothing(exported_stage: Path) -> None:
    """One joint, identity transforms, every vertex fully weighted to it.

    If Blender ever stops synthesizing this, the shape-key path changes shape
    and the reasoning behind the fix above no longer applies.
    """

    from pxr import Gf, Usd, UsdSkel

    stage = Usd.Stage.Open(str(exported_stage))
    skeleton_prim = next(
        p for p in stage.TraverseAll() if p.GetTypeName() == "Skeleton"
    )
    skeleton = UsdSkel.Skeleton(skeleton_prim)

    assert len(skeleton.GetJointsAttr().Get() or ()) == 1
    for attribute in (
        skeleton.GetBindTransformsAttr(),
        skeleton.GetRestTransformsAttr(),
    ):
        transforms = attribute.Get() or ()
        assert len(transforms) == 1
        assert Gf.Matrix4d(transforms[0]) == Gf.Matrix4d(1.0)
