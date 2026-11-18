"""Staging every variant member into the export, and putting the scene back.

An artist hides the variant they are not working on. Blender's USD exporter
then leaves it out - a collection excluded from the view layer is skipped, and
so is anything the active evaluation mode sees as hidden. Every variant has to
be present for the authoring pass to switch between them, so the export opens
each member's path first and restores it afterwards, exactly.
"""

from __future__ import annotations

from types import SimpleNamespace

from Plugin.export.variants import (
    prepare_variant_visibility,
    resolve_variant_plan,
    restore_variant_visibility,
)


def _object(name: str, *, hide_render=False, hide_viewport=False, object_type="MESH"):
    return SimpleNamespace(
        name=name,
        type=object_type,
        hide_render=hide_render,
        hide_viewport=hide_viewport,
    )


def _collection(name: str, objects=(), children=(), hide_render=False, hide_viewport=False):
    return SimpleNamespace(
        name=name,
        objects=list(objects),
        children=list(children),
        hide_render=hide_render,
        hide_viewport=hide_viewport,
    )


class _LayerCollection:
    """A layer collection that cascades ``exclude`` the way Blender 5.3 does.

    Setting ``exclude`` sets the same value on the whole subtree. Each layer's
    own value still decides whether its objects are in the view layer, so a
    child can be included under an excluded parent; the constructor allows
    any such state.
    """

    def __init__(self, collection, children=(), exclude=False):
        self.collection = collection
        self.children = list(children)
        self._exclude = exclude
        self.writes = 0

    @property
    def exclude(self):
        return self._exclude

    @exclude.setter
    def exclude(self, value):
        self.writes += 1
        stack = [self]
        while stack:
            layer = stack.pop()
            layer._exclude = bool(value)
            stack.extend(layer.children)


def _layer(collection, children=(), exclude=False):
    return _LayerCollection(collection, children, exclude)


def _layers(root):
    stack, found = [root], []
    while stack:
        layer = stack.pop()
        found.append(layer)
        stack.extend(layer.children)
    return found


def _excludes(root):
    return {layer.collection.name: layer.exclude for layer in _layers(root)[1:]}


class _ViewLayer:
    def __init__(self, layer_collection):
        self.layer_collection = layer_collection
        self.updates = 0

    def update(self):
        self.updates += 1


def _scene_objects(view_layer) -> list:
    """Every object in the layer tree, once, the way ``scene.objects`` lists them."""
    objects, seen = [], set()
    stack = [getattr(view_layer, "layer_collection", None)]
    while stack:
        layer = stack.pop()
        if layer is None:
            continue
        for obj in getattr(layer.collection, "objects", ()):
            if id(obj) not in seen:
                seen.add(id(obj))
                objects.append(obj)
        stack.extend(getattr(layer, "children", ()))
    return objects


def _context(view_layer, *variant_sets):
    return SimpleNamespace(
        scene=SimpleNamespace(
            usd_stage_variant_sets=list(variant_sets),
            objects=_scene_objects(view_layer),
            usd_stage_export_settings=SimpleNamespace(evaluation_mode="RENDER"),
        ),
        view_layer=view_layer,
    )


def _settings(**overrides):
    values = {"export_variants": True, "export_armatures": True}
    values.update(overrides)
    return SimpleNamespace(**values)


def _variant(name, collection):
    return SimpleNamespace(name=name, collection=collection)


def _variant_set(name, variants, default_variant=None):
    return SimpleNamespace(
        name=name,
        variants=list(variants),
        default_variant=default_variant or variants[0].name,
    )


def _seat_scene(*, leather_excluded=False, leather_hidden=False):
    """Seat/{Fabric,Leather}, with Leather hidden the way an artist would."""
    fabric_object = _object("SeatFabric")
    leather_object = _object("SeatLeather", hide_render=leather_hidden)
    fabric = _collection("Fabric", objects=[fabric_object])
    leather = _collection("Leather", objects=[leather_object])
    seat = _collection("Seat", children=[fabric, leather])
    root = _layer(
        _collection("Scene", children=[seat]),
        children=[
            _layer(
                seat,
                children=[
                    _layer(fabric),
                    _layer(leather, exclude=leather_excluded),
                ],
            )
        ],
    )
    view_layer = _ViewLayer(root)
    context = _context(
        view_layer,
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
    )
    layers = {
        "seat": root.children[0],
        "fabric": root.children[0].children[0],
        "leather": root.children[0].children[1],
    }
    return context, view_layer, layers, {
        "fabric_object": fabric_object,
        "leather_object": leather_object,
        "leather": leather,
    }


def _stage(context, settings=None):
    settings = settings or _settings()
    plan = resolve_variant_plan(context, settings)
    return plan, prepare_variant_visibility(context, plan)


# --------------------------------------------------------------------------
# Staging
# --------------------------------------------------------------------------

def test_an_excluded_member_collection_is_included_for_the_export():
    context, _view_layer, layers, _objects = _seat_scene(leather_excluded=True)
    assert layers["leather"].exclude is True

    _plan, state = _stage(context)

    assert layers["leather"].exclude is False
    restore_variant_visibility(state)
    assert layers["leather"].exclude is True


def test_a_render_hidden_member_object_is_shown_for_the_export():
    context, _view_layer, _layers, objects = _seat_scene(leather_hidden=True)

    _plan, state = _stage(context)

    assert objects["leather_object"].hide_render is False
    restore_variant_visibility(state)
    assert objects["leather_object"].hide_render is True


def test_both_hide_flags_are_cleared_whatever_the_evaluation_mode():
    """Staging must not depend on which flag the current mode consults."""
    context, _view_layer, _layers, objects = _seat_scene()
    objects["leather_object"].hide_render = True
    objects["leather_object"].hide_viewport = True

    _plan, state = _stage(context, _settings(evaluation_mode="VIEWPORT"))

    assert objects["leather_object"].hide_render is False
    assert objects["leather_object"].hide_viewport is False
    restore_variant_visibility(state)
    assert objects["leather_object"].hide_render is True
    assert objects["leather_object"].hide_viewport is True


def test_a_hidden_member_collection_is_shown_and_restored():
    context, _view_layer, _layers, objects = _seat_scene()
    objects["leather"].hide_render = True

    _plan, state = _stage(context)

    assert objects["leather"].hide_render is False
    restore_variant_visibility(state)
    assert objects["leather"].hide_render is True


def test_an_excluded_ancestor_is_left_alone_while_its_members_are_included():
    """Excluding the parent cascades to both variants; only they come back.

    An included collection under an excluded parent still contributes its
    objects, so re-including the parent would only pull the parent's own
    objects into the export.
    """
    context, _view_layer, layers, _objects = _seat_scene()
    layers["seat"].exclude = True
    assert layers["fabric"].exclude is True and layers["leather"].exclude is True

    _plan, state = _stage(context)

    assert layers["seat"].exclude is True
    assert layers["fabric"].exclude is False
    assert layers["leather"].exclude is False
    restore_variant_visibility(state)
    assert (layers["seat"].exclude, layers["fabric"].exclude, layers["leather"].exclude) == (
        True, True, True,
    )


def test_a_collection_linked_in_two_places_has_both_chains_opened():
    shared_object = _object("SeatLeather")
    leather = _collection("Leather", objects=[shared_object])
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    first_parent = _collection("Seat", children=[fabric, leather])
    second_parent = _collection("Spare", children=[leather])
    first_leather_layer = _layer(leather, exclude=True)
    second_leather_layer = _layer(leather, exclude=True)
    root = _layer(
        _collection("Scene", children=[first_parent, second_parent]),
        children=[
            _layer(first_parent, children=[_layer(fabric), first_leather_layer]),
            _layer(second_parent, children=[second_leather_layer]),
        ],
    )
    context = _context(
        _ViewLayer(root),
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
    )

    _plan, state = _stage(context)

    assert first_leather_layer.exclude is False
    assert second_leather_layer.exclude is False
    restore_variant_visibility(state)
    assert first_leather_layer.exclude is True
    assert second_leather_layer.exclude is True


def _grouped_scene():
    """Variants/{Fabric,Leather,Props}: the group excluded, Fabric re-included.

    Fabric and Leather are the variants; Props is not a member. Variants also
    holds a loose object of its own, and Props a nested Box collection.
    """
    fabric = _collection("Fabric", objects=[_object("FabricSeat")])
    leather = _collection("Leather", objects=[_object("LeatherSeat")])
    box = _collection("Box", objects=[_object("BoxProp")])
    props = _collection("Props", objects=[_object("Prop")], children=[box])
    group = _collection(
        "Variants", objects=[_object("Loose")], children=[fabric, leather, props]
    )
    group_layer = _layer(
        group,
        exclude=True,
        children=[
            _layer(fabric, exclude=False),
            _layer(leather, exclude=True),
            _layer(props, exclude=True, children=[_layer(box, exclude=True)]),
        ],
    )
    root = _layer(_collection("Scene", children=[group]), children=[group_layer])
    context = _context(
        _ViewLayer(root),
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
    )
    return context, root


def test_staging_includes_only_the_member_collections():
    context, root = _grouped_scene()

    _plan, state = _stage(context)

    assert _excludes(root) == {
        "Variants": True,
        "Fabric": False,
        "Leather": False,
        "Props": True,
        "Box": True,
    }
    restore_variant_visibility(state)


def test_restore_returns_every_exclude_to_its_original_value():
    context, root = _grouped_scene()
    before = _excludes(root)

    _plan, state = _stage(context)
    restore_variant_visibility(state)

    assert _excludes(root) == before


def test_a_non_member_child_reincluded_by_the_cascade_is_excluded_again():
    """Including a collection that holds a member cascades into its children.

    A variant's own child collections are members, but a member object can
    also be linked into another collection whose other children are not.
    """
    seat_leather = _object("SeatLeather")
    leather = _collection("Leather", objects=[seat_leather])
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    extra = _collection("Extra", objects=[_object("ExtraProp")])
    display = _collection("Display", objects=[seat_leather], children=[extra])
    leather_layer = _layer(leather, exclude=True)
    extra_layer = _layer(extra, exclude=True)
    display_layer = _layer(display, exclude=True, children=[extra_layer])
    root = _layer(
        _collection("Scene", children=[fabric, leather, display]),
        children=[_layer(fabric), leather_layer, display_layer],
    )
    context = _context(
        _ViewLayer(root),
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
    )
    before = _excludes(root)

    _plan, state = _stage(context)

    assert leather_layer.exclude is False
    assert display_layer.exclude is False
    assert extra_layer.exclude is True
    restore_variant_visibility(state)
    assert _excludes(root) == before


def test_a_member_under_an_excluded_non_member_is_still_included():
    """Restoring a non-member parent must not cascade over a member inside it."""
    leather = _collection("Leather", objects=[_object("SeatLeather")])
    fabric = _collection("Fabric", objects=[_object("SeatFabric")])
    shelf = _collection("Shelf", objects=[_object("ShelfProp")], children=[leather])
    leather_layer = _layer(leather, exclude=True)
    shelf_layer = _layer(shelf, exclude=True, children=[leather_layer])
    root = _layer(
        _collection("Scene", children=[fabric, shelf]),
        children=[_layer(fabric), shelf_layer],
    )
    context = _context(
        _ViewLayer(root),
        _variant_set("Seat", [_variant("Fabric", fabric), _variant("Leather", leather)]),
    )

    _plan, state = _stage(context)

    assert shelf_layer.exclude is True
    assert leather_layer.exclude is False
    restore_variant_visibility(state)
    assert (shelf_layer.exclude, leather_layer.exclude) == (True, True)


def test_layers_already_as_needed_are_never_written():
    context, root = _grouped_scene()
    root.children[0].children[1].exclude = False  # Leather already included
    for layer in _layers(root):
        layer.writes = 0

    _plan, state = _stage(context)
    restore_variant_visibility(state)

    assert all(layer.writes == 0 for layer in _layers(root))


def test_a_failure_part_way_through_staging_puts_the_scene_back():
    context, root = _grouped_scene()
    before = _excludes(root)

    class _Exploding:
        name = "Exploding"
        type = "MESH"
        hide_viewport = False

        @property
        def hide_render(self):
            return True

        @hide_render.setter
        def hide_render(self, _value):
            raise KeyboardInterrupt  # not swallowed like an ordinary error

    leather = root.children[0].children[1].collection
    leather.objects.append(_Exploding())

    try:
        _stage(context)
    except KeyboardInterrupt:
        pass
    else:  # pragma: no cover - the fake always raises
        raise AssertionError("staging should have raised")

    assert _excludes(root) == before


def test_the_view_layer_is_refreshed_on_the_way_in_and_out():
    context, view_layer, _layers, _objects = _seat_scene(leather_excluded=True)

    _plan, state = _stage(context)
    assert view_layer.updates == 1

    restore_variant_visibility(state)
    assert view_layer.updates == 2


# --------------------------------------------------------------------------
# Restoring
# --------------------------------------------------------------------------

def test_nothing_already_visible_is_recorded_for_restore():
    """A scene needing no staging must leave no restore work behind."""
    context, _view_layer, _layers, _objects = _seat_scene()

    _plan, state = _stage(context)

    assert state["restores"] == []


def test_a_scene_with_no_variant_sets_is_untouched():
    context = _context(_ViewLayer(_layer(_collection("Scene"))))

    _plan, state = _stage(context)

    assert state["restores"] == []


def test_restoring_twice_is_harmless():
    context, _view_layer, layers, _objects = _seat_scene(leather_excluded=True)
    _plan, state = _stage(context)

    restore_variant_visibility(state)
    restore_variant_visibility(state)

    assert layers["leather"].exclude is True


def test_restore_puts_back_every_flag_it_changed():
    context, _view_layer, layers, objects = _seat_scene(
        leather_excluded=True, leather_hidden=True
    )
    objects["leather"].hide_viewport = True
    layers["seat"].exclude = True
    before = {
        "leather_layer": layers["leather"].exclude,
        "seat_layer": layers["seat"].exclude,
        "leather_collection": objects["leather"].hide_viewport,
        "leather_object": objects["leather_object"].hide_render,
    }

    _plan, state = _stage(context)
    assert state["restores"], "staging should have had work to do"
    restore_variant_visibility(state)

    assert layers["leather"].exclude == before["leather_layer"]
    assert layers["seat"].exclude == before["seat_layer"]
    assert objects["leather"].hide_viewport == before["leather_collection"]
    assert objects["leather_object"].hide_render == before["leather_object"]


def test_restoring_an_empty_state_is_harmless():
    restore_variant_visibility({})
    restore_variant_visibility({"restores": []})


# --------------------------------------------------------------------------
# Selection closure
# --------------------------------------------------------------------------

def test_a_touched_set_offers_all_of_its_members():
    context, _view_layer, _layers, objects = _seat_scene()
    plan = resolve_variant_plan(context, _settings())
    from Plugin.export.object_scope import rna_identity

    touched = plan.sets_touching({rna_identity(objects["fabric_object"])})

    assert [variant_set.name for variant_set in touched] == ["Seat"]
    members = {
        name
        for variant in touched[0].variants
        for name in variant.object_names
    }
    assert members == {"SeatFabric", "SeatLeather"}


def test_an_untouched_set_is_not_pulled_into_the_selection():
    oak = _collection("Oak", objects=[_object("LegOak")])
    steel = _collection("Steel", objects=[_object("LegSteel")])
    context, _view_layer, _layers, objects = _seat_scene()
    context.scene.usd_stage_variant_sets.append(
        _variant_set("Legs", [_variant("Oak", oak), _variant("Steel", steel)])
    )
    plan = resolve_variant_plan(context, _settings())
    from Plugin.export.object_scope import rna_identity

    touched = plan.sets_touching({rna_identity(objects["fabric_object"])})

    assert [variant_set.name for variant_set in touched] == ["Seat"]


def test_member_objects_lists_each_object_once():
    context, _view_layer, _layers, _objects = _seat_scene()
    plan = resolve_variant_plan(context, _settings())

    names = [str(obj.name) for obj in plan.member_objects()]

    assert names == ["SeatFabric", "SeatLeather"]
