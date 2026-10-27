"""Resolution and refusal rules for declared USD variant sets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from Plugin.export import variants as variant_plan
from Plugin.export.variants import (
    ERROR,
    WARNING,
    VariantPlanError,
    require_valid_variant_plan,
    resolve_variant_plan,
    validate_variant_plan,
)


def _object(name: str, object_type: str = "MESH"):
    return SimpleNamespace(name=name, type=object_type)


def _collection(name: str, objects=(), children=()):
    return SimpleNamespace(name=name, objects=list(objects), children=list(children))


def _variant(name: str, collection):
    return SimpleNamespace(name=name, collection=collection)


def _variant_set(name: str, variants, default_variant: str | None = None):
    if default_variant is None:
        default_variant = variants[0].name if variants else ""
    return SimpleNamespace(
        name=name,
        variants=list(variants),
        default_variant=default_variant,
    )


def _context(*variant_sets):
    return SimpleNamespace(
        scene=SimpleNamespace(usd_stage_variant_sets=list(variant_sets))
    )


def _settings(**overrides):
    values = {"export_variants": True, "export_armatures": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _codes(issues, severity=ERROR):
    return [issue.code for issue in issues if issue.severity == severity]


def _plan(*variant_sets, settings=None):
    return resolve_variant_plan(_context(*variant_sets), settings or _settings())


def _seat_plan():
    """A well-formed two-variant set, used as the baseline for most tests."""
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    return _plan(
        _variant_set(
            "Seat",
            [_variant("Fabric", fabric), _variant("Leather", leather)],
        )
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

def test_scene_without_variant_sets_resolves_to_an_empty_plan():
    plan = resolve_variant_plan(_context(), _settings())
    assert plan.is_empty
    assert validate_variant_plan(plan) == []


def test_scene_missing_the_property_entirely_resolves_to_an_empty_plan():
    """The export path runs before the UI registers its property group."""
    context = SimpleNamespace(scene=SimpleNamespace())
    assert resolve_variant_plan(context, _settings()).is_empty


def test_export_variants_off_ignores_declared_sets():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", collection)]),
        settings=_settings(export_variants=False),
    )
    assert plan.is_empty


def test_resolution_collects_objects_from_nested_child_collections():
    inner = _collection("Trim", objects=[_object("TrimPiece")])
    outer = _collection("Fabric", objects=[_object("SeatFabric")], children=[inner])
    other = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", outer), _variant("Leather", other)])
    )
    fabric = plan.sets[0].variants[0]
    assert set(fabric.object_names) == {"SeatFabric", "TrimPiece"}
    assert validate_variant_plan(plan) == []


def test_resolution_drops_object_classes_the_export_never_writes():
    collection = _collection(
        "Fabric",
        objects=[_object("SeatFabric"), _object("KeyLight", "LIGHT")],
    )
    other = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", collection), _variant("Leather", other)])
    )
    assert plan.sets[0].variants[0].object_names == ("SeatFabric",)


def test_armature_membership_follows_the_export_armatures_setting():
    collection = _collection("Rigged", objects=[_object("Rig", "ARMATURE")])
    other = _collection("Plain", objects=[_object("SeatPlain")])
    declared = _variant_set(
        "Seat", [_variant("Rigged", collection), _variant("Plain", other)]
    )

    with_armatures = _plan(declared, settings=_settings(export_armatures=True))
    assert with_armatures.sets[0].variants[0].object_names == ("Rig",)

    without = _plan(declared, settings=_settings(export_armatures=False))
    assert without.sets[0].variants[0].object_names == ()
    assert "VARIANT_EMPTY" in _codes(validate_variant_plan(without))


def test_a_collection_cycle_cannot_hang_resolution():
    left = _collection("Left", objects=[_object("LeftMesh")])
    right = _collection("Right", objects=[_object("RightMesh")], children=[left])
    left.children.append(right)
    other = _collection("Other", objects=[_object("OtherMesh")])
    plan = _plan(
        _variant_set("Seat", [_variant("Looped", left), _variant("Other", other)])
    )
    assert set(plan.sets[0].variants[0].object_names) == {"LeftMesh", "RightMesh"}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_a_well_formed_plan_has_no_findings():
    assert validate_variant_plan(_seat_plan()) == []


@pytest.mark.parametrize("name", ["", "1Seat", "Seat finish", "Seat-finish", "Chaise_Café"])
def test_invalid_variant_set_names_are_refused(name):
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    other = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set(name, [_variant("Fabric", collection), _variant("Leather", other)])
    )
    assert "VARIANT_SET_NAME_INVALID" in _codes(validate_variant_plan(plan))


def test_invalid_variant_names_are_refused():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    other = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set(
            "Seat",
            [_variant("has space", collection), _variant("Leather", other)],
            default_variant="Leather",
        )
    )
    assert "VARIANT_NAME_INVALID" in _codes(validate_variant_plan(plan))


def test_two_sets_with_one_name_are_refused():
    first = _collection("Fabric", objects=[_object("SeatFabric")])
    second = _collection("Leather", objects=[_object("SeatLeather")])
    third = _collection("Oak", objects=[_object("LegOak")])
    fourth = _collection("Steel", objects=[_object("LegSteel")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", first), _variant("Leather", second)]),
        _variant_set("Seat", [_variant("Oak", third), _variant("Steel", fourth)]),
    )
    assert "VARIANT_SET_NAME_DUPLICATE" in _codes(validate_variant_plan(plan))


def test_a_repeated_variant_name_is_refused():
    first = _collection("Fabric", objects=[_object("SeatFabric")])
    second = _collection("Other", objects=[_object("SeatOther")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", first), _variant("Fabric", second)])
    )
    assert "VARIANT_NAME_DUPLICATE" in _codes(validate_variant_plan(plan))


def test_a_deleted_collection_is_refused_rather_than_dropped():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    plan = _plan(
        _variant_set(
            "Seat",
            [_variant("Fabric", collection), _variant("Leather", None)],
        )
    )
    assert "VARIANT_COLLECTION_MISSING" in _codes(validate_variant_plan(plan))


def test_a_variant_with_nothing_exportable_is_refused():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    empty = _collection("Leather", objects=[])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", collection), _variant("Leather", empty)])
    )
    assert "VARIANT_EMPTY" in _codes(validate_variant_plan(plan))


def test_a_set_with_no_variants_is_refused():
    plan = _plan(_variant_set("Seat", []))
    assert "VARIANT_SET_EMPTY" in _codes(validate_variant_plan(plan))


def test_one_object_in_two_variants_of_a_set_is_refused():
    shared = _object("SeatShell")
    first = _collection("Fabric", objects=[shared, _object("SeatFabric")])
    second = _collection("Leather", objects=[shared, _object("SeatLeather")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", first), _variant("Leather", second)])
    )
    assert "VARIANT_MEMBER_SHARED_IN_SET" in _codes(validate_variant_plan(plan))


def test_one_variant_collection_nested_in_another_is_refused_as_shared_membership():
    """Nesting needs no rule of its own: the outer set is a superset."""
    inner = _collection("Leather", objects=[_object("SeatLeather")])
    outer = _collection("Fabric", objects=[_object("SeatFabric")], children=[inner])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", outer), _variant("Leather", inner)])
    )
    assert "VARIANT_MEMBER_SHARED_IN_SET" in _codes(validate_variant_plan(plan))


def test_two_geometry_sets_varying_one_object_are_refused():
    shared = _object("SeatShell")
    first = _collection("Fabric", objects=[shared])
    second = _collection("Leather", objects=[_object("SeatLeather")])
    third = _collection("Oak", objects=[shared])
    fourth = _collection("Steel", objects=[_object("LegSteel")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", first), _variant("Leather", second)]),
        _variant_set("Legs", [_variant("Oak", third), _variant("Steel", fourth)]),
    )
    # Both would author 'active' on the same prim, so one could never win.
    assert "VARIANT_PROPERTY_CONTESTED" in _codes(validate_variant_plan(plan))


def test_a_shared_pair_is_reported_once_however_many_objects_overlap():
    shared = [_object(f"Shell{index}") for index in range(5)]
    first = _collection("Fabric", objects=list(shared))
    second = _collection("Leather", objects=list(shared))
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", first), _variant("Leather", second)])
    )
    codes = _codes(validate_variant_plan(plan))
    assert codes.count("VARIANT_MEMBER_SHARED_IN_SET") == 1


def test_an_unknown_default_selection_is_refused():
    first = _collection("Fabric", objects=[_object("SeatFabric")])
    second = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set(
            "Seat",
            [_variant("Fabric", first), _variant("Leather", second)],
            default_variant="Vinyl",
        )
    )
    assert "VARIANT_DEFAULT_UNKNOWN" in _codes(validate_variant_plan(plan))


def test_a_single_variant_set_warns_without_refusing():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    plan = _plan(_variant_set("Seat", [_variant("Fabric", collection)]))
    issues = validate_variant_plan(plan)
    assert _codes(issues, ERROR) == []
    assert "VARIANT_SET_SINGLE" in _codes(issues, WARNING)


def test_too_many_composed_states_are_refused_before_anything_is_staged():
    declared = []
    # 3 sets of 7 variants compose 343 states, over the preflight's bound.
    for set_index in range(3):
        members = [
            _variant(
                f"V{variant_index}",
                _collection(
                    f"C{set_index}_{variant_index}",
                    objects=[_object(f"O{set_index}_{variant_index}")],
                ),
            )
            for variant_index in range(7)
        ]
        declared.append(_variant_set(f"Set{set_index}", members))
    plan = _plan(*declared)
    assert plan.combination_count > variant_plan.MAX_VARIANT_COMBINATIONS
    assert "VARIANT_COMBINATIONS_EXCEEDED" in _codes(validate_variant_plan(plan))


def test_a_plan_at_the_limit_is_allowed():
    declared = []
    for set_index in range(8):
        members = [
            _variant(
                f"V{variant_index}",
                _collection(
                    f"C{set_index}_{variant_index}",
                    objects=[_object(f"O{set_index}_{variant_index}")],
                ),
            )
            for variant_index in range(2)
        ]
        declared.append(_variant_set(f"Set{set_index}", members))
    plan = _plan(*declared)
    assert plan.combination_count == variant_plan.MAX_VARIANT_COMBINATIONS
    assert _codes(validate_variant_plan(plan)) == []


# --------------------------------------------------------------------------
# require_valid_variant_plan
# --------------------------------------------------------------------------

class _Diagnostics:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def add_error(self, message):
        self.errors.append(message)

    def add_warning(self, message):
        self.warnings.append(message)


def test_require_returns_a_clean_plan_unchanged():
    plan = _seat_plan()
    assert require_valid_variant_plan(plan) is plan


def test_require_raises_with_every_error_at_once():
    first = _collection("Fabric", objects=[_object("SeatFabric")])
    plan = _plan(
        _variant_set(
            "has space",
            [_variant("Fabric", first), _variant("Leather", None)],
            default_variant="Vinyl",
        )
    )
    diagnostics = _Diagnostics()
    with pytest.raises(VariantPlanError) as excinfo:
        require_valid_variant_plan(plan, diagnostics)

    codes = {issue.code for issue in excinfo.value.issues}
    assert codes == {
        "VARIANT_SET_NAME_INVALID",
        "VARIANT_COLLECTION_MISSING",
        "VARIANT_DEFAULT_UNKNOWN",
    }
    # Every refusal reaches the user, not just the first one hit.
    assert len(diagnostics.errors) == 3
    message = str(excinfo.value)
    assert "has space" in message and "Vinyl" in message


def test_require_reports_warnings_without_stopping_the_export():
    collection = _collection("Fabric", objects=[_object("SeatFabric")])
    plan = _plan(_variant_set("Seat", [_variant("Fabric", collection)]))
    diagnostics = _Diagnostics()

    assert require_valid_variant_plan(plan, diagnostics) is plan
    assert diagnostics.errors == []
    assert len(diagnostics.warnings) == 1


# --------------------------------------------------------------------------
# Material variant sets
# --------------------------------------------------------------------------

def _material(name: str):
    return SimpleNamespace(name=name)


def _material_variant(name: str, material):
    return SimpleNamespace(name=name, material=material)


def _material_set(name, variants, target, default_variant=None):
    return SimpleNamespace(
        name=name,
        kind="MATERIAL",
        variants=list(variants),
        target_collection=target,
        default_variant=default_variant or variants[0].name,
    )


def _finish_set(target=None):
    target = target or _collection("Tops", objects=[_object("TableTop")])
    return _material_set(
        "Finish",
        [
            _material_variant("Oak", _material("OakMat")),
            _material_variant("Walnut", _material("WalnutMat")),
        ],
        target,
    )


def test_a_material_set_resolves_its_targets_and_materials():
    plan = _plan(_finish_set())
    variant_set = plan.sets[0]

    assert variant_set.kind == "MATERIAL"
    assert variant_set.varied_property == "material:binding"
    assert variant_set.target_object_names == ("TableTop",)
    assert [v.material_name for v in variant_set.variants] == ["OakMat", "WalnutMat"]
    assert validate_variant_plan(plan) == []


def test_a_material_set_is_not_asked_for_collections():
    """Its variants own a material; the collection rules must not fire."""
    issues = validate_variant_plan(_plan(_finish_set()))

    assert "VARIANT_COLLECTION_MISSING" not in _codes(issues)
    assert "VARIANT_EMPTY" not in _codes(issues)


def test_a_material_set_without_a_target_is_refused():
    plan = _plan(
        _material_set(
            "Finish",
            [
                _material_variant("Oak", _material("OakMat")),
                _material_variant("Walnut", _material("WalnutMat")),
            ],
            None,
        )
    )
    assert "VARIANT_TARGET_MISSING" in _codes(validate_variant_plan(plan))


def test_a_material_set_targeting_nothing_exportable_is_refused():
    plan = _plan(_finish_set(target=_collection("Tops", objects=[])))
    assert "VARIANT_TARGET_EMPTY" in _codes(validate_variant_plan(plan))


def test_a_variant_without_a_material_is_refused():
    plan = _plan(
        _material_set(
            "Finish",
            [
                _material_variant("Oak", _material("OakMat")),
                _material_variant("Walnut", None),
            ],
            _collection("Tops", objects=[_object("TableTop")]),
        )
    )
    assert "VARIANT_MATERIAL_MISSING" in _codes(validate_variant_plan(plan))


def test_two_variants_binding_one_material_warn_without_refusing():
    shared = _material("OakMat")
    plan = _plan(
        _material_set(
            "Finish",
            [_material_variant("Oak", shared), _material_variant("AlsoOak", shared)],
            _collection("Tops", objects=[_object("TableTop")]),
        )
    )
    issues = validate_variant_plan(plan)
    assert _codes(issues, ERROR) == []
    assert "VARIANT_MATERIAL_REPEATED" in _codes(issues, WARNING)


# --------------------------------------------------------------------------
# Shapes and finishes together — the point of the kinds
# --------------------------------------------------------------------------

def test_a_shape_set_and_a_finish_set_may_share_objects():
    """They vary different properties, so neither shadows the other."""
    top = _object("TableTop")
    round_c = _collection("Round", objects=[top])
    square_c = _collection("Square", objects=[_object("TableTopSquare")])
    tops = _collection("Tops", objects=[top])

    plan = _plan(
        _variant_set("Shape", [_variant("Round", round_c), _variant("Square", square_c)]),
        _finish_set(target=tops),
    )

    assert validate_variant_plan(plan) == []
    assert plan.combination_count == 4


def test_two_material_sets_on_one_object_are_refused():
    top = _object("TableTop")
    plan = _plan(
        _finish_set(target=_collection("Tops", objects=[top])),
        _material_set(
            "Trim",
            [
                _material_variant("Brass", _material("BrassMat")),
                _material_variant("Chrome", _material("ChromeMat")),
            ],
            _collection("TopsAgain", objects=[top]),
        ),
    )
    issues = validate_variant_plan(plan)
    assert "VARIANT_PROPERTY_CONTESTED" in _codes(issues)
    assert issues[0].details["property"] == "material:binding"


# --------------------------------------------------------------------------
# Nested finish sets
# --------------------------------------------------------------------------

def _nested_plan(parent="Shape", applies_to=("", "Square"), shape_variants=("Round", "Square")):
    shapes = [
        _variant(name, _collection(name, objects=[_object(f"Top{name}")]))
        for name in shape_variants
    ]
    finish = _material_set(
        "Finish",
        [
            _material_variant("Oak", _material("OakMat")),
            _material_variant("Walnut", _material("WalnutMat")),
        ],
        _collection("Tops", objects=[_object(f"Top{shape_variants[0]}")]),
    )
    finish.parent_set = parent
    for variant, applies in zip(finish.variants, applies_to):
        variant.applies_to = applies
    return _plan(_variant_set("Shape", shapes), finish)


def test_a_well_formed_nested_set_is_accepted():
    assert validate_variant_plan(_nested_plan()) == []


def test_nesting_inside_an_unknown_set_is_refused():
    plan = _nested_plan(parent="Nope")
    assert "VARIANT_PARENT_UNKNOWN" in _codes(validate_variant_plan(plan))


def test_nesting_inside_itself_is_refused():
    plan = _nested_plan(parent="Finish")
    assert "VARIANT_PARENT_SELF" in _codes(validate_variant_plan(plan))


def test_applying_to_a_shape_that_does_not_exist_is_refused():
    plan = _nested_plan(applies_to=("", "Hexagonal"))
    assert "VARIANT_APPLIES_TO_UNKNOWN" in _codes(validate_variant_plan(plan))


def test_a_shape_left_with_no_finish_is_refused():
    """Every shape has to offer something to choose."""
    plan = _nested_plan(applies_to=("Square", "Square"))
    issues = validate_variant_plan(plan)
    assert "VARIANT_PARENT_HAS_NO_FINISH" in _codes(issues)
    assert any(
        issue.details.get("parent_variant") == "Round"
        for issue in issues
        if issue.code == "VARIANT_PARENT_HAS_NO_FINISH"
    )


def test_a_nested_set_does_not_contest_a_set_on_the_root_prim():
    """It is scoped inside one shape variant, so it collides with nothing."""
    plan = _nested_plan()
    assert "VARIANT_PROPERTY_CONTESTED" not in _codes(validate_variant_plan(plan))


def test_a_finish_offered_everywhere_needs_no_applies_to():
    plan = _nested_plan(applies_to=("", ""))
    assert validate_variant_plan(plan) == []


def test_a_nested_set_counts_what_each_shape_offers():
    """Not every finish against every shape: only the ones on offer."""
    # Round offers Oak; Square offers Oak and Walnut. Three states, not four.
    plan = _nested_plan(applies_to=("", "Square"))
    assert plan.combination_count == 3


def test_an_unnested_finish_set_still_multiplies():
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    plan = _plan(
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
        _finish_set(target=_collection("Tops", objects=[_object("TableTop")])),
    )
    assert plan.combination_count == 4


def test_two_nested_finish_sets_on_one_object_are_refused():
    """Nesting does not exempt a set: both would author the binding."""
    top = _object("TableTop")
    shapes = _variant_set(
        "Shape",
        [
            _variant("Round", _collection("Round", objects=[top])),
            _variant("Square", _collection("Square", objects=[_object("TopSquare")])),
        ],
    )
    finish = _finish_set(target=_collection("Tops", objects=[top]))
    finish.parent_set = "Shape"
    trim = _material_set(
        "Trim",
        [
            _material_variant("Brass", _material("BrassMat")),
            _material_variant("Chrome", _material("ChromeMat")),
        ],
        _collection("TopsAgain", objects=[top]),
    )
    trim.parent_set = "Shape"

    issues = validate_variant_plan(_plan(shapes, finish, trim))

    assert "VARIANT_PROPERTY_CONTESTED" in _codes(issues)


def test_a_nested_finish_never_contests_its_own_parent():
    """Its parent varies activation; it varies the binding."""
    assert "VARIANT_PROPERTY_CONTESTED" not in _codes(validate_variant_plan(_nested_plan()))


def test_a_shape_set_ignores_a_parent_left_from_when_it_was_a_finish():
    """Only a finish nests; a stale Nest Inside on a shape must not count."""
    shapes = _variant_set("Shape", [
        _variant("Round", _collection("Round", objects=[_object("TopRound")])),
        _variant("Square", _collection("Square", objects=[_object("TopSquare")])),
    ])
    trims = _variant_set("Trim", [
        _variant("Brass", _collection("Brass", objects=[_object("TrimBrass")])),
        _variant("Steel", _collection("Steel", objects=[_object("TrimSteel")])),
    ])
    trims.parent_set = "Shape"

    plan = _plan(shapes, trims)

    assert plan.set_named("Trim").parent_set == ""
    # Two independent shape sets: 2 x 2 states, not 2 + 2.
    assert plan.combination_count == 4


@pytest.mark.parametrize("material, prefix", [
    ("Fabric_Encre", "Fabric_"),
    ("Wood.Oak", "Wood."),
    ("Plain", ""),
])
def test_a_finish_family_prefix_is_guessed_from_a_material_name(material, prefix):
    from Plugin.export.variants import guess_finish_prefix
    assert guess_finish_prefix(material) == prefix


@pytest.mark.parametrize("material, prefix, name", [
    ("Fabric_Encre", "Fabric_", "Encre"),
    ("Fabric_Bordeaux.001", "Fabric_", "Bordeaux_001"),
    ("Velours bleu", "", "Velours_bleu"),
    ("Fabric_", "Fabric_", "Fabric"),
])
def test_a_finish_is_named_after_its_material_less_the_family(material, prefix, name):
    from Plugin.export.variants import finish_name_from_material
    assert finish_name_from_material(material, prefix) == name


def test_unique_names_count_up_from_two():
    from Plugin.export.variants import unique_name
    assert unique_name(["Oak"], "Walnut") == "Walnut"
    assert unique_name(["Oak", "Oak_2"], "Oak") == "Oak_3"
