"""Which targets each finish material reaches, as the authoring binds them.

A finish nested in a shape set is bound inside each shape variant that offers
it, on every target except the members of the other shapes and their
parented children. The material rewrite checks a finish's UV maps and
attributes against exactly those meshes, so the two must agree.
"""

from __future__ import annotations

from types import SimpleNamespace

from Plugin.export.variants import (
    GEOMETRY,
    MATERIAL,
    VariantMember,
    VariantPlan,
    VariantSetPlan,
    finish_material_targets,
)


def _obj(name, parent=None):
    return SimpleNamespace(name=name, parent=parent)


def _shape(name, *objects):
    return VariantMember(name=name, collection_name=name, has_collection=True,
                         objects=tuple(objects))


def _finish(name, material, applies_to=()):
    return VariantMember(name=name, collection_name="", has_collection=False,
                         material_name=material, has_material=True,
                         applies_to=tuple(applies_to))


def _plan(targets, finishes, shapes=None):
    sets = []
    if shapes is not None:
        sets.append(VariantSetPlan(name="Shape", variants=tuple(shapes),
                                   default_variant=shapes[0].name, kind=GEOMETRY))
    sets.append(VariantSetPlan(
        name="Finish", variants=tuple(finishes), default_variant=finishes[0].name,
        kind=MATERIAL, target_collection_name="Tops", has_target=True,
        target_objects=tuple(targets), parent_set="Shape" if shapes else "",
    ))
    return VariantPlan(sets=tuple(sets))


def _names(reached, material):
    return sorted(obj.name for obj in reached.get(material, ()))


def test_a_standalone_finish_reaches_every_target():
    seat, back = _obj("Seat"), _obj("Back")
    reached = finish_material_targets(
        _plan([seat, back], [_finish("Oak", "OakMat"), _finish("Walnut", "WalnutMat")]))

    assert _names(reached, "OakMat") == ["Back", "Seat"]
    assert _names(reached, "WalnutMat") == ["Back", "Seat"]


def test_a_nested_finish_skips_targets_only_other_shapes_own():
    round_top, square_top = _obj("TopRound"), _obj("TopSquare")
    reached = finish_material_targets(_plan(
        [round_top, square_top],
        [_finish("Oak", "OakMat"), _finish("Walnut", "WalnutMat", ["Square"])],
        shapes=[_shape("Round", round_top), _shape("Square", square_top)],
    ))

    assert _names(reached, "OakMat") == ["TopRound", "TopSquare"]
    assert _names(reached, "WalnutMat") == ["TopSquare"]


def test_a_target_parented_under_another_shape_is_skipped_too():
    round_top, square_top = _obj("TopRound"), _obj("TopSquare")
    inlay = _obj("Inlay", parent=round_top)
    reached = finish_material_targets(_plan(
        [round_top, square_top, inlay],
        [_finish("Oak", "OakMat"), _finish("Walnut", "WalnutMat", ["Square"])],
        shapes=[_shape("Round", round_top), _shape("Square", square_top)],
    ))

    assert _names(reached, "WalnutMat") == ["TopSquare"]


def test_a_frame_no_shape_owns_is_reached_under_every_shape():
    round_top, square_top, frame = _obj("TopRound"), _obj("TopSquare"), _obj("Frame")
    reached = finish_material_targets(_plan(
        [round_top, square_top, frame],
        [_finish("Oak", "OakMat"), _finish("Walnut", "WalnutMat", ["Square"])],
        shapes=[_shape("Round", round_top), _shape("Square", square_top)],
    ))

    assert _names(reached, "WalnutMat") == ["Frame", "TopSquare"]
