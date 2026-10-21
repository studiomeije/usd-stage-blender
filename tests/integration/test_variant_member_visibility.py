"""Integration test — a hidden variant member still reaches the USD.

Blender's USD exporter drops a collection excluded from the view layer, and
drops an object whose ``hide_render`` flag is set. That is exactly how an
artist hides the variant they are not working on, so without staging an export
of a variant-organised scene writes only the variant that happened to be
visible - and the authoring pass would then switch to prims that do not exist.

Fakes cannot prove this: the question is whether clearing ``exclude`` really
changes what Blender writes. So this runs the real exporter over real
datablocks, three times - before staging, during, and after restoring.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Seat/{Fabric,Leather}. Leather is excluded from the view layer and its object
# is hide_render - both ways an artist hides the variant they are not using.
_PROBE = r'''
import bpy, sys
from types import SimpleNamespace

repo, out_dir = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
from Plugin.export.variants import (
    prepare_variant_visibility,
    resolve_variant_plan,
    restore_variant_visibility,
)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def cube(name, collection):
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = name
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    collection.objects.link(obj)
    return obj

seat = bpy.data.collections.new("Seat")
scene.collection.children.link(seat)
fabric = bpy.data.collections.new("Fabric")
leather = bpy.data.collections.new("Leather")
seat.children.link(fabric)
seat.children.link(leather)

cube("SeatFabric", fabric)
leather_object = cube("SeatLeather", leather)
leather_object.hide_render = True

# Hide it the other way too: exclude the collection from the view layer.
layer = bpy.context.view_layer.layer_collection.children["Seat"].children["Leather"]
layer.exclude = True

settings = SimpleNamespace(
    export_variants=True, export_armatures=True, evaluation_mode="RENDER",
)
context = SimpleNamespace(
    scene=SimpleNamespace(usd_stage_variant_sets=[
        SimpleNamespace(
            name="Seat",
            default_variant="Fabric",
            variants=[
                SimpleNamespace(name="Fabric", collection=fabric),
                SimpleNamespace(name="Leather", collection=leather),
            ],
        )
    ]),
    view_layer=bpy.context.view_layer,
)

def export(name):
    bpy.ops.wm.usd_export(filepath=f"{out_dir}/{name}.usda", root_prim_path="/Root")

export("before")

plan = resolve_variant_plan(context, settings)
state = prepare_variant_visibility(context, plan)
export("staged")

restore_variant_visibility(state)
export("restored")

print("EXCLUDE_AFTER_RESTORE", layer.exclude)
print("HIDE_AFTER_RESTORE", leather_object.hide_render)

# Nested case: the parent is excluded too. Blender cascades that to its
# children, so restoring leaf-first and then the parent has to land back on
# the exact same three values - something fakes cannot model.
seat_layer = bpy.context.view_layer.layer_collection.children["Seat"]
fabric_layer = seat_layer.children["Fabric"]
layer.exclude = True
seat_layer.exclude = True
nested_before = (seat_layer.exclude, fabric_layer.exclude, layer.exclude)

nested_state = prepare_variant_visibility(context, resolve_variant_plan(context, settings))
export("nested_staged")
restore_variant_visibility(nested_state)

nested_after = (seat_layer.exclude, fabric_layer.exclude, layer.exclude)
print("NESTED_BEFORE", nested_before)
print("NESTED_AFTER", nested_after)
print("NESTED_RESTORE_EXACT", nested_before == nested_after)
'''


@pytest.fixture(scope="module")
def staged_exports(tmp_path_factory) -> dict:
    workdir = tmp_path_factory.mktemp("variant_visibility")
    script = workdir / "probe.py"
    script.write_text(_PROBE)
    out_dir = workdir / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python", str(script), "--", str(REPO_ROOT), str(out_dir),
        ],
        capture_output=True, text=True, timeout=300,
    )
    for name in ("before", "staged", "restored", "nested_staged"):
        assert (out_dir / f"{name}.usda").exists(), result.stdout + result.stderr
    return {"dir": out_dir, "output": result.stdout}


def _prims(stage: Path) -> str:
    return stage.read_text()


def test_without_staging_the_hidden_variant_is_missing(staged_exports):
    """The control: this is what a variant scene exports as today."""
    text = _prims(staged_exports["dir"] / "before.usda")

    assert "SeatFabric" in text
    assert "SeatLeather" not in text, (
        "the hidden member exported without staging, so this test no longer "
        "proves anything about staging"
    )


def test_staging_brings_the_hidden_variant_into_the_export(staged_exports):
    text = _prims(staged_exports["dir"] / "staged.usda")

    assert "SeatFabric" in text
    assert "SeatLeather" in text, (
        "the hidden variant member was still dropped, so an authored variant "
        "would switch to a prim that does not exist"
    )


def test_restoring_really_puts_the_scene_back(staged_exports):
    """Not just the flags: a later export drops the member again."""
    text = _prims(staged_exports["dir"] / "restored.usda")

    assert "SeatFabric" in text
    assert "SeatLeather" not in text


def test_the_hiding_flags_are_restored(staged_exports):
    assert "EXCLUDE_AFTER_RESTORE True" in staged_exports["output"]
    assert "HIDE_AFTER_RESTORE True" in staged_exports["output"]


def test_an_excluded_parent_collection_is_opened_too(staged_exports):
    """Excluding the parent hides every variant, not just one."""
    text = _prims(staged_exports["dir"] / "nested_staged.usda")

    assert "SeatFabric" in text and "SeatLeather" in text


def test_a_cascaded_exclusion_is_restored_exactly(staged_exports):
    """Blender propagates exclude to children; restore must survive that."""
    assert "NESTED_RESTORE_EXACT True" in staged_exports["output"], (
        "the view layer did not come back as it was: "
        + staged_exports["output"]
    )
