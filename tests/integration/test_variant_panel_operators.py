"""Integration test — the Variants panel registers and its operators work.

UI code is only proved by Blender registering it: a bad property type, a
PointerProperty to an ID that operators may not hold, or a UIList naming a
property that does not exist all pass every unit test and fail on register.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r'''
import bpy, json, sys

repo = sys.argv[-1]
sys.path.insert(0, repo)
import Plugin

bpy.ops.wm.read_factory_settings(use_empty=True)
Plugin.register()

result = {}
scene = bpy.context.scene
result["property_registered"] = hasattr(scene, "usd_stage_variant_sets")
result["panel_registered"] = hasattr(bpy.types, "USDSTAGE_PT_export_variants")
result["uilists_registered"] = all(
    hasattr(bpy.types, name)
    for name in ("USDSTAGE_UL_variant_sets", "USDSTAGE_UL_variants")
)
result["setting_registered"] = hasattr(
    scene.usd_stage_export_settings, "export_variants"
)

# A collection whose name is not a USD identifier, to prove the panel
# sanitizes on creation rather than leaving a set that export would refuse.
seat = bpy.data.collections.new("Chaise Café")
scene.collection.children.link(seat)
for name in ("Fabric", "Leather"):
    child = bpy.data.collections.new(name)
    seat.children.link(child)
    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    obj.name = f"Seat{name}"
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    child.objects.link(obj)

# Hide Fabric so Leather is the visible one; the default should follow.
layer = bpy.context.view_layer.layer_collection.children["Chaise Café"]
layer.children["Fabric"].exclude = True

bpy.ops.usdstage.variant_set_add_from_collection(collection_name="Chaise Café")

sets = scene.usd_stage_variant_sets
result["set_count"] = len(sets)
result["set_name"] = sets[0].name if len(sets) else None
result["variant_names"] = [v.name for v in sets[0].variants] if len(sets) else []
result["collections"] = (
    [v.collection.name if v.collection else None for v in sets[0].variants]
    if len(sets) else []
)
result["default_variant"] = sets[0].default_variant if len(sets) else None

# Preview the other variant: its collection comes in, the sibling goes out.
scene.usd_stage_variant_sets_index = 0
bpy.ops.usdstage.variant_preview(variant="Fabric")
result["preview_fabric_excluded"] = layer.children["Fabric"].exclude
result["preview_leather_excluded"] = layer.children["Leather"].exclude

# Removing the default variant must leave a default that still exists.
sets[0].variants_index = [v.name for v in sets[0].variants].index(
    sets[0].default_variant
)
bpy.ops.usdstage.variant_remove()
result["default_after_remove"] = sets[0].default_variant
result["variants_after_remove"] = [v.name for v in sets[0].variants]

bpy.ops.usdstage.variant_set_remove()
result["set_count_after_remove"] = len(scene.usd_stage_variant_sets)

bpy.ops.usdstage.variant_set_add_empty()
result["empty_set_name"] = scene.usd_stage_variant_sets[0].name

# Exercise the two operators the flow above does not reach, so every idname
# the panel draws is known to resolve and run.
bpy.ops.usdstage.variant_add()
bpy.ops.usdstage.variant_add()
empty_set = scene.usd_stage_variant_sets[0]
result["added_variants"] = [v.name for v in empty_set.variants]
result["default_after_first_add"] = empty_set.default_variant
bpy.ops.usdstage.variant_set_default(variant="Variant_2", set_index=0)
result["default_after_set"] = empty_set.default_variant

# Removing a shape set must not leave a finish set nesting in a ghost.
nested = scene.usd_stage_variant_sets.add()
nested.name = "Finish"
nested.kind = 'MATERIAL'
nested.parent_set = "VariantSet"
scene.usd_stage_variant_sets_index = [
    s.name for s in scene.usd_stage_variant_sets
].index("VariantSet")
bpy.ops.usdstage.variant_set_remove()
result["parent_after_removal"] = scene.usd_stage_variant_sets[0].parent_set
result["sets_after_removal"] = [s.name for s in scene.usd_stage_variant_sets]

# Every operator the panel references, resolvable by idname.
result["operators_exist"] = sorted(
    name for name in (
        "variant_set_add_from_collection", "variant_set_add_empty",
        "variant_set_remove", "variant_add", "variant_remove",
        "variant_set_default", "variant_preview",
    )
    if hasattr(bpy.ops.usdstage, name)
)

Plugin.unregister()
result["unregistered_cleanly"] = not hasattr(
    bpy.context.scene, "usd_stage_variant_sets"
)

print("RESULT_JSON", json.dumps(result))
'''


@pytest.fixture(scope="module")
def panel_probe() -> dict:
    result = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python-expr", _PROBE.replace("sys.argv[-1]", repr(str(REPO_ROOT))),
        ],
        capture_output=True, text=True, timeout=300,
    )
    marker = "RESULT_JSON "
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith(marker)), None
    )
    assert line, result.stdout + result.stderr
    return json.loads(line[len(marker):])


def test_everything_registers(panel_probe):
    assert panel_probe["property_registered"]
    assert panel_probe["panel_registered"]
    assert panel_probe["uilists_registered"]
    assert panel_probe["setting_registered"]


def test_a_set_is_built_from_the_collections_children(panel_probe):
    assert panel_probe["set_count"] == 1
    assert panel_probe["variant_names"] == ["Fabric", "Leather"]
    assert panel_probe["collections"] == ["Fabric", "Leather"]


def test_a_name_that_is_not_a_usd_identifier_is_adjusted_on_creation(panel_probe):
    """Sanitized here, where the user can see and edit it; strict at export."""
    assert panel_probe["set_name"] == "Chaise_Caf_"


def test_the_visible_variant_becomes_the_default(panel_probe):
    assert panel_probe["default_variant"] == "Leather"


def test_preview_shows_one_variant_and_hides_its_siblings(panel_probe):
    assert panel_probe["preview_fabric_excluded"] is False
    assert panel_probe["preview_leather_excluded"] is True


def test_removing_the_default_variant_leaves_a_valid_default(panel_probe):
    assert panel_probe["variants_after_remove"] == ["Fabric"]
    assert panel_probe["default_after_remove"] == "Fabric"


def test_sets_can_be_removed_and_added_empty(panel_probe):
    assert panel_probe["set_count_after_remove"] == 0
    assert panel_probe["empty_set_name"] == "VariantSet"


def test_adding_variants_keeps_a_default_selection(panel_probe):
    assert panel_probe["added_variants"] == ["Variant", "Variant_2"]
    # The first variant added to an empty set becomes the default.
    assert panel_probe["default_after_first_add"] == "Variant"
    assert panel_probe["default_after_set"] == "Variant_2"


def test_every_operator_the_panel_draws_resolves(panel_probe):
    assert panel_probe["operators_exist"] == sorted((
        "variant_set_add_from_collection", "variant_set_add_empty",
        "variant_set_remove", "variant_add", "variant_remove",
        "variant_set_default", "variant_preview",
    ))


def test_unregistering_removes_the_scene_property(panel_probe):
    assert panel_probe["unregistered_cleanly"]


def test_removing_a_set_clears_what_nested_inside_it(panel_probe):
    """Otherwise the finish points at a name that no longer exists."""
    assert panel_probe["sets_after_removal"] == ["Finish"]
    assert panel_probe["parent_after_removal"] == ""
