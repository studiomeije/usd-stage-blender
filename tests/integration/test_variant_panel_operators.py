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

# The Offered On toggles: narrowing, widening, and the refusal to leave a
# finish offered on nothing.
finish = scene.usd_stage_variant_sets.add()
finish.name = "Finish"
finish.kind = 'MATERIAL'
finish.parent_set = "VariantSet"
for name in ("Oak", "Walnut"):
    finish.variants.add().name = name
finish.default_variant = "Oak"
names = lambda: [s.name for s in scene.usd_stage_variant_sets]
scene.usd_stage_variant_sets_index = names().index("Finish")
finish.variants_index = 1

bpy.ops.usdstage.variant_toggle_shape(shape="Variant")
result["offered_after_narrowing"] = finish.variants[1].applies_to
bpy.ops.usdstage.variant_toggle_shape(shape="Variant")
result["offered_after_widening"] = finish.variants[1].applies_to

bpy.ops.usdstage.variant_toggle_shape(shape="Variant")
result["last_shape_refused"] = bpy.ops.usdstage.variant_toggle_shape(
    shape="Variant_2"
) == {'CANCELLED'}
result["offered_after_refusal"] = finish.variants[1].applies_to
scene.usd_stage_variant_sets.remove(names().index("Finish"))

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

# Renaming carries the new name to everything that pointed at the old one.
seat = scene.usd_stage_variant_sets.add()
seat.name = "Seat"
for name in ("Fabric", "Leather"):
    seat.variants.add().name = name
seat.default_variant = "Leather"
colour = scene.usd_stage_variant_sets.add()
colour.name = "Colour"
colour.kind = 'MATERIAL'
colour.parent_set = "Seat"
navy = colour.variants.add()
navy.name = "Navy"
navy.applies_to = "Leather"
seat.variants[1].name = "Cuir"
result["default_after_rename"] = seat.default_variant
result["offered_after_rename"] = navy.applies_to
seat.name = "Assise"
result["parent_after_set_rename"] = colour.parent_set
# Only a finish nests: turning it into a shape drops the nesting.
colour.kind = 'GEOMETRY'
result["parent_after_kind_change"] = colour.parent_set

# Picking a collection or material names a variant still called Variant.
picks = scene.usd_stage_variant_sets.add()
picks.name = "Picks"
picks.kind = 'MATERIAL'
picked = picks.variants.add()
picked.name = "Variant"
picked.material = bpy.data.materials.new("Fabric_Encre")
kept = picks.variants.add()
kept.name = "Kept"
kept.material = bpy.data.materials["Fabric_Encre"]
shaped = seat.variants.add()
shaped.name = "Variant_2"
shaped.collection = bpy.data.collections["Leather"]
result["named_from_material"] = picked.name
result["named_by_hand_is_kept"] = kept.name
result["named_from_collection"] = shaped.name

# A finish set in one step: a target collection and a family of materials.
seats = bpy.data.collections.new("Seats")
scene.collection.children.link(seats)
bpy.ops.mesh.primitive_cube_add()
worn = bpy.context.object
for existing in worn.users_collection:
    existing.objects.unlink(worn)
seats.objects.link(worn)
worn.data.materials.append(bpy.data.materials.new("Fabric_Bordeaux"))
for name in ("Fabric_Navy", "Fabric_Mousse"):
    bpy.data.materials.new(name)
bpy.ops.usdstage.variant_set_add_finish(target_name="Seats", prefix="Fabric_")
made = scene.usd_stage_variant_sets[scene.usd_stage_variant_sets_index]
result["finish_set"] = {
    "name": made.name,
    "kind": made.kind,
    "target": made.target_collection.name if made.target_collection else None,
    "variants": [[v.name, v.material.name] for v in made.variants],
    "default": made.default_variant,
}
# An operator that reports an error raises when called from a script.
sets_before = len(scene.usd_stage_variant_sets)
try:
    bpy.ops.usdstage.variant_set_add_finish(target_name="Seats", prefix="Nothing_")
    refused = False
except RuntimeError as error:
    refused = "No material starts with" in str(error)
result["finish_without_match_cancelled"] = (
    refused and len(scene.usd_stage_variant_sets) == sets_before
)

# The panel lists the export's own findings for the selected set.
from Plugin.ui.variants_panel import _set_issues
broken = scene.usd_stage_variant_sets.add()
broken.name = "Broken"
broken.variants.add().name = "Lonely"
broken.default_variant = "Lonely"
result["panel_issue_codes"] = sorted(i.code for i in _set_issues(bpy.context, broken))

# Every operator the panel references, resolvable by idname.
result["operators_exist"] = sorted(
    name for name in (
        "variant_set_add_from_collection", "variant_set_add_finish", "variant_set_add_empty",
        "variant_set_remove", "variant_add", "variant_remove",
        "variant_set_default", "variant_preview", "variant_toggle_shape",
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
        "variant_set_add_from_collection", "variant_set_add_finish", "variant_set_add_empty",
        "variant_set_remove", "variant_add", "variant_remove",
        "variant_set_default", "variant_preview", "variant_toggle_shape",
    ))


def test_unregistering_removes_the_scene_property(panel_probe):
    assert panel_probe["unregistered_cleanly"]


def test_removing_a_set_clears_what_nested_inside_it(panel_probe):
    """Otherwise the finish points at a name that no longer exists."""
    assert panel_probe["sets_after_removal"] == ["Finish"]
    assert panel_probe["parent_after_removal"] == ""


def test_the_offered_on_toggles_narrow_and_widen_a_finish(panel_probe):
    """Picking shapes by toggle is why no name can be mistyped."""
    assert panel_probe["offered_after_narrowing"] == "Variant_2"
    # Toggling the last one back stores empty, so a shape added later is
    # offered the finish rather than quietly left out.
    assert panel_probe["offered_after_widening"] == ""


def test_a_finish_cannot_be_offered_on_nothing(panel_probe):
    assert panel_probe["last_shape_refused"]
    assert panel_probe["offered_after_refusal"] == "Variant_2"


def test_renaming_a_variant_keeps_the_default_and_the_offered_shapes(panel_probe):
    """Names are references; a rename must not leave one pointing nowhere."""
    assert panel_probe["default_after_rename"] == "Cuir"
    assert panel_probe["offered_after_rename"] == "Cuir"


def test_renaming_a_set_keeps_what_nests_inside_it(panel_probe):
    assert panel_probe["parent_after_set_rename"] == "Assise"


def test_a_shape_set_keeps_no_nesting(panel_probe):
    assert panel_probe["parent_after_kind_change"] == ""


def test_a_new_variant_takes_its_name_from_what_it_is_given(panel_probe):
    assert panel_probe["named_from_material"] == "Fabric_Encre"
    assert panel_probe["named_from_collection"] == "Leather"
    # A name the artist typed is theirs.
    assert panel_probe["named_by_hand_is_kept"] == "Kept"


def test_a_finish_set_is_made_from_a_material_family_in_one_step(panel_probe):
    assert panel_probe["finish_set"] == {
        "name": "Fabric",
        "kind": "MATERIAL",
        "target": "Seats",
        "variants": [
            ["Bordeaux", "Fabric_Bordeaux"],
            ["Encre", "Fabric_Encre"],
            ["Mousse", "Fabric_Mousse"],
            ["Navy", "Fabric_Navy"],
        ],
        # What the seats already wear ships by default.
        "default": "Bordeaux",
    }
    assert panel_probe["finish_without_match_cancelled"]


def test_the_panel_lists_what_the_export_would_refuse(panel_probe):
    assert panel_probe["panel_issue_codes"] == [
        "VARIANT_COLLECTION_MISSING", "VARIANT_SET_SINGLE",
    ]
