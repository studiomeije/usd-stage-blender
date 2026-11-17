"""Panel warnings and bake guards share the export's closed variant scope."""

from types import SimpleNamespace
import sys

from Plugin.export.variant_scope import effective_variant_plan


def _object(name):
    return SimpleNamespace(name=name, type="MESH")


def _collection(name, *objects):
    return SimpleNamespace(name=name, objects=objects, children=())


def _scene():
    selected, sibling, unrelated = (_object(name) for name in ("Selected", "Sibling", "Other"))
    geometry = SimpleNamespace(
        name="Geometry", kind="GEOMETRY", default_variant="One",
        variants=[
            SimpleNamespace(name="One", collection=_collection("One", selected)),
            SimpleNamespace(name="Two", collection=_collection("Two", sibling)),
        ],
    )
    materials = [
        SimpleNamespace(
            name=name, kind="MATERIAL", default_variant="Surface",
            target_collection=_collection(name, obj), parent_set="",
            variants=[SimpleNamespace(name="Surface", material=None)],
        )
        for name, obj in (("SiblingMaterials", sibling), ("OtherMaterials", unrelated))
    ]
    context = SimpleNamespace(
        scene=SimpleNamespace(usd_stage_variant_sets=[geometry, *materials]),
        selected_objects=[selected],
    )
    return context, (selected, sibling, unrelated)


def _settings(**overrides):
    return SimpleNamespace(export_variants=overrides.get("export_variants", True),
                           selected_objects_only=overrides.get("selected_objects_only", False))


def _collector(monkeypatch, result):
    calls = []

    def collect(context, settings):
        calls.append((context, settings))
        return result

    monkeypatch.setitem(sys.modules, "Plugin.export.animation_export", SimpleNamespace(collect_export_objects=collect))
    return calls


def test_full_scene_keeps_all_sets_without_collecting_selection(monkeypatch):
    context, _objects = _scene()
    calls = _collector(monkeypatch, [])
    plan = effective_variant_plan(context, _settings())
    assert [entry.name for entry in plan.sets] == ["Geometry", "SiblingMaterials", "OtherMaterials"]
    assert not calls


def test_selected_scope_uses_closed_objects_and_excludes_unrelated_materials(monkeypatch):
    context, (selected, sibling, _unrelated) = _scene()
    calls = _collector(monkeypatch, [selected, sibling])
    settings = _settings(selected_objects_only=True)
    plan = effective_variant_plan(context, settings)
    assert [entry.name for entry in plan.sets] == ["Geometry", "SiblingMaterials"]
    assert calls == [(context, settings)]


def test_empty_selected_export_has_no_variant_blockers(monkeypatch):
    context, _objects = _scene()
    _collector(monkeypatch, [])
    assert effective_variant_plan(context, _settings(selected_objects_only=True)).is_empty


def test_disabled_variants_do_not_collect_or_block_selected_export(monkeypatch):
    context, _objects = _scene()
    calls = _collector(monkeypatch, [])
    settings = _settings(export_variants=False, selected_objects_only=True)
    assert effective_variant_plan(context, settings).is_empty
    assert not calls
