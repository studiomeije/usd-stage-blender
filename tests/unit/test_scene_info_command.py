"""The ``info`` command's own output, without Blender.

Every other CLI command has a test that reaches its ``handle``; this one was
only ever exercised through the CLI front end with a fake Blender, which
checks the exit code and never looks at the payload. The material count is
the only real logic in it: it counts distinct materials actually in a slot,
not slots.

``handle`` imports ``bpy`` when called rather than at module import, so a stub
module is enough to run it here.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from Plugin.api.commands import scene_info


def _object(name, *material_names):
    return SimpleNamespace(
        name=name,
        material_slots=[
            SimpleNamespace(material=None if m is None else SimpleNamespace(name=m))
            for m in material_names
        ],
    )


def _scene(objects, **overrides):
    scene = SimpleNamespace(
        name=overrides.get("name", "Scene"),
        objects=objects,
        frame_start=overrides.get("frame_start", 1),
        frame_end=overrides.get("frame_end", 250),
        render=SimpleNamespace(fps=overrides.get("fps", 24)),
        unit_settings=SimpleNamespace(
            system=overrides.get("system", "METRIC"),
            scale_length=overrides.get("scale_length", 1.0),
        ),
    )
    return scene


@pytest.fixture
def fake_bpy(monkeypatch):
    """Install a stub ``bpy`` for the duration of one test."""
    def install(scene, filepath=""):
        module = SimpleNamespace(
            context=SimpleNamespace(scene=scene),
            data=SimpleNamespace(filepath=filepath),
        )
        monkeypatch.setitem(sys.modules, "bpy", module)
        return module

    return install


def test_scene_metadata_is_reported(fake_bpy):
    fake_bpy(
        _scene(
            [_object("Cube", "Red")],
            name="Shot010",
            frame_start=5,
            frame_end=40,
            fps=30,
            system="METRIC",
            scale_length=0.01,
        ),
        filepath="/projects/shot.blend",
    )

    assert scene_info.handle({}) == {
        "file": "/projects/shot.blend",
        "scene": "Shot010",
        "frame_range": [5, 40],
        "fps": 30,
        "unit_system": "METRIC",
        "unit_scale": 0.01,
        "object_count": 1,
        "material_count": 1,
        "variant_sets": [],
    }


def test_an_unsaved_file_reports_no_path(fake_bpy):
    fake_bpy(_scene([]), filepath="")

    assert scene_info.handle({})["file"] is None


def test_a_material_on_several_objects_is_counted_once(fake_bpy):
    fake_bpy(_scene([_object("A", "Shared"), _object("B", "Shared")]))

    info = scene_info.handle({})

    assert info["object_count"] == 2
    assert info["material_count"] == 1


def test_empty_slots_are_not_counted(fake_bpy):
    fake_bpy(_scene([_object("Cube", "Red", None, None)]))

    assert scene_info.handle({})["material_count"] == 1


def test_every_distinct_material_is_counted(fake_bpy):
    fake_bpy(_scene([_object("A", "Red", "Blue"), _object("B", "Green")]))

    assert scene_info.handle({})["material_count"] == 3


def test_an_object_without_material_slots_is_tolerated(fake_bpy):
    """Lights and cameras have no ``material_slots`` at all."""
    camera = SimpleNamespace(name="Camera")
    fake_bpy(_scene([camera, _object("Cube", "Red")]))

    info = scene_info.handle({})

    assert info["object_count"] == 2
    assert info["material_count"] == 1


def test_a_scene_with_no_objects_counts_nothing(fake_bpy):
    fake_bpy(_scene([]))

    info = scene_info.handle({})

    assert info["object_count"] == 0
    assert info["material_count"] == 0


def _variant_set(name, variants, default_variant):
    return SimpleNamespace(
        name=name,
        default_variant=default_variant,
        variants=[
            SimpleNamespace(
                name=variant_name,
                collection=(
                    None if collection is None else SimpleNamespace(name=collection)
                ),
            )
            for variant_name, collection in variants
        ],
    )


def test_declared_variant_sets_are_reported(fake_bpy):
    scene = _scene([_object("Cube", "Red")])
    scene.usd_stage_variant_sets = [
        _variant_set("Seat", [("Fabric", "Fabric"), ("Leather", "Leather")], "Fabric")
    ]
    fake_bpy(scene)

    assert scene_info.handle({})["variant_sets"] == [
        {
            "name": "Seat",
            "kind": "GEOMETRY",
            "default_variant": "Fabric",
            "variants": [
                {"name": "Fabric", "collection": "Fabric"},
                {"name": "Leather", "collection": "Leather"},
            ],
        }
    ]


def test_a_variant_whose_collection_was_deleted_is_reported_not_hidden(fake_bpy):
    """``info`` says what the file declares, not what would survive an export."""
    scene = _scene([_object("Cube", "Red")])
    scene.usd_stage_variant_sets = [
        _variant_set("Seat", [("Fabric", "Fabric"), ("Leather", None)], "Fabric")
    ]
    fake_bpy(scene)

    variants = scene_info.handle({})["variant_sets"][0]["variants"]
    assert variants[1] == {"name": "Leather", "collection": None}


def test_a_finish_set_reports_its_materials_not_null_collections(fake_bpy):
    """A finish owns materials; null collections would read as broken."""
    scene = _scene([_object("Cube", "Red")])
    scene.usd_stage_variant_sets = [
        SimpleNamespace(
            name="Colour",
            kind="MATERIAL",
            default_variant="Navy",
            target_collection=SimpleNamespace(name="Seats"),
            parent_set="Seat",
            variants=[
                SimpleNamespace(name="Navy", material=SimpleNamespace(name="Fabric_Navy"),
                                collection=None, applies_to=""),
                SimpleNamespace(name="Red", material=None, collection=None,
                                applies_to="Leather"),
            ],
        )
    ]
    fake_bpy(scene)

    assert scene_info.handle({})["variant_sets"] == [
        {
            "name": "Colour",
            "kind": "MATERIAL",
            "default_variant": "Navy",
            "target_collection": "Seats",
            "nested_in": "Seat",
            "variants": [
                {"name": "Navy", "material": "Fabric_Navy"},
                {"name": "Red", "material": None, "offered_on": ["Leather"]},
            ],
        }
    ]
