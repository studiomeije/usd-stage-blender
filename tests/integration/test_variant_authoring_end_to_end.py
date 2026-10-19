"""Integration test — a real export, authored into a real variant set.

The unit tests build stages by hand. This one runs Blender's own exporter,
takes the prim map its USDHook recorded, and authors variants from it, so the
join between the two halves is exercised on real output rather than on a
fixture that happens to agree with it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r'''
import bpy, sys
from types import SimpleNamespace

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
from pxr import Usd
from Plugin.export import usd_hook
from Plugin.export.variant_authoring import author_variant_sets
from Plugin.export.variants import (
    prepare_variant_visibility, resolve_variant_plan, restore_variant_visibility,
)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

def cube(name, collection, location):
    bpy.ops.mesh.primitive_cube_add(location=location)
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

cube("SeatFabric", fabric, (0, 0, 0))
cube("SeatLeather", leather, (0, 0, 0))
cube("Plinth", scene.collection, (0, 0, -2))   # in no variant: always present

# Hide the variant the artist is not using, the way they would.
bpy.context.view_layer.layer_collection.children["Seat"].children["Leather"].exclude = True

settings = SimpleNamespace(
    export_variants=True, export_armatures=True, evaluation_mode="RENDER",
    selected_objects_only=False, root_prim_name="Root", allow_unicode=False,
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

plan = resolve_variant_plan(context, settings)
state = prepare_variant_visibility(context, plan)
try:
    with usd_hook.capture_prim_map():
        bpy.ops.wm.usd_export(filepath=out, root_prim_path="/Root")
finally:
    restore_variant_visibility(state)

object_map = usd_hook.consume_captured_object_map()
print("OBJECT_MAP", object_map)

stage = Usd.Stage.Open(out)
authored = author_variant_sets(stage, plan, object_map, (), settings)
stage.GetRootLayer().Save()
print("AUTHORED", authored)
'''


@pytest.fixture(scope="module")
def authored_stage(tmp_path_factory) -> dict:
    workdir = tmp_path_factory.mktemp("variant_authoring")
    script = workdir / "probe.py"
    script.write_text(_PROBE)
    out = workdir / "seat.usda"

    result = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python", str(script), "--", str(REPO_ROOT), str(out),
        ],
        capture_output=True, text=True, timeout=300,
    )
    assert out.exists(), result.stdout + result.stderr
    assert "AUTHORED ('Seat',)" in result.stdout, result.stdout + result.stderr
    return {"path": out, "output": result.stdout}


def _selection_children(path: Path, selection: str) -> list[str]:
    from pxr import Usd

    stage = Usd.Stage.Open(str(path))
    root = stage.GetDefaultPrim()
    root.GetVariantSet("Seat").SetVariantSelection(selection)
    return sorted(child.GetName() for child in root.GetChildren())


def test_the_variant_set_reaches_the_exported_stage(authored_stage):
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(authored_stage["path"]))
    root = stage.GetDefaultPrim()

    assert root.GetVariantSets().GetNames() == ["Seat"]
    assert sorted(root.GetVariantSet("Seat").GetVariantNames()) == ["Fabric", "Leather"]
    assert root.GetVariantSet("Seat").GetVariantSelection() == "Fabric"


def test_each_selection_composes_its_own_member(authored_stage):
    pytest.importorskip("pxr")

    fabric = _selection_children(authored_stage["path"], "Fabric")
    leather = _selection_children(authored_stage["path"], "Leather")

    assert "SeatFabric" in fabric and "SeatLeather" not in fabric
    assert "SeatLeather" in leather and "SeatFabric" not in leather


def test_an_object_in_no_variant_is_present_either_way(authored_stage):
    pytest.importorskip("pxr")

    for selection in ("Fabric", "Leather"):
        assert "Plinth" in _selection_children(authored_stage["path"], selection)


def test_the_hidden_member_was_exported_at_all(authored_stage):
    """Without staging the prim map would never have held SeatLeather."""
    assert "SeatLeather" in authored_stage["output"]


@pytest.mark.skipif(
    shutil.which("usdchecker") is None, reason="usdchecker not installed"
)
def test_the_authored_stage_passes_arkit_compliance(authored_stage):
    """Apple's checker walks each variant selection in turn."""
    result = subprocess.run(
        ["usdchecker", "--arkit", str(authored_stage["path"])],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Failed" not in result.stdout
