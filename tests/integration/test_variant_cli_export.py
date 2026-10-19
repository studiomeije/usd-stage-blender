"""Integration test — the shipped CLI exports a variant scene correctly.

Everything else tests a piece. This drives ``usdstage export`` over a .blend
whose variant sets were declared and saved in one Blender process, then
reopened by the CLI in another, where the add-on registers only after the file
is loaded - the whole path a user takes.

The scene: a grey plinth in no variant, and above it either a tall red box
(Fabric) or a blue sphere (Leather) in a ``Seat`` set. Leather is excluded
from the view layer, the way an artist hides the variant they are not using,
so an export that ships both proves the staging pass ran.
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

repo, out = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo)
import Plugin

bpy.ops.wm.read_factory_settings(use_empty=True)
Plugin.register()
scene = bpy.context.scene


def finish(obj, name, colour):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = colour
    obj.data.materials.append(material)


bpy.ops.mesh.primitive_cube_add(size=1.2, location=(0.0, 0.0, 0.05))
plinth = bpy.context.object
plinth.name = "Plinth"
plinth.scale = (1.0, 1.0, 0.08)
finish(plinth, "PlinthGrey", (0.42, 0.42, 0.44, 1.0))

seat = bpy.data.collections.new("Seat")
scene.collection.children.link(seat)
variant_set = scene.usd_stage_variant_sets.add()
variant_set.name = "Seat"
for name, add, colour in (
    ("Fabric", lambda: bpy.ops.mesh.primitive_cube_add(size=0.9, location=(0.0, 0.0, 0.75)),
     (0.75, 0.08, 0.08, 1.0)),
    ("Leather", lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0.0, 0.0, 0.8)),
     (0.07, 0.15, 0.78, 1.0)),
):
    collection = bpy.data.collections.new(name)
    seat.children.link(collection)
    add()
    obj = bpy.context.object
    obj.name = f"Seat{name}"
    for existing in obj.users_collection:
        existing.objects.unlink(obj)
    collection.objects.link(obj)
    finish(obj, f"Seat{name}", colour)
    variant = variant_set.variants.add()
    variant.name = name
    variant.collection = collection
variant_set.default_variant = "Fabric"

bpy.context.view_layer.layer_collection.children["Seat"].children["Leather"].exclude = True
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _usdstage(*args, timeout=600):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), *args],
        capture_output=True, text=True, timeout=timeout,
    )


@pytest.fixture(scope="module")
def scene(tmp_path_factory) -> Path:
    """The variant scene, declared and saved by one Blender process."""
    work = tmp_path_factory.mktemp("variant_scene")
    script = work / "build.py"
    script.write_text(_BUILD)
    blend = work / "seat_variants.blend"
    result = subprocess.run(
        [blender_executable(), "--background", "--factory-startup",
         "--python", str(script), "--", str(REPO_ROOT), str(blend)],
        capture_output=True, text=True, timeout=600,
    )
    assert blend.exists(), result.stdout + result.stderr
    return blend


@pytest.fixture(scope="module")
def exported(scene, tmp_path_factory) -> dict:
    out_dir = tmp_path_factory.mktemp("variant_export")
    output = out_dir / "seat.usda"

    result = _usdstage(
        "export", str(scene), "diagnostics_enabled=true",
        "-o", str(output), "--format", "USDA",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.exists(), result.stdout + result.stderr
    return {
        "stage": output,
        "diagnostics": output.with_suffix(".diagnostics.json"),
    }


def _children(stage_path: Path, selection: str | None) -> list[str]:
    from pxr import Usd

    stage = Usd.Stage.Open(str(stage_path))
    root = stage.GetDefaultPrim()
    if selection is not None:
        root.GetVariantSet("Seat").SetVariantSelection(selection)
    return sorted(child.GetName() for child in root.GetChildren())


def test_the_declared_set_reaches_the_exported_stage(exported):
    pytest.importorskip("pxr")
    from pxr import Usd

    stage = Usd.Stage.Open(str(exported["stage"]))
    root = stage.GetDefaultPrim()

    assert root.GetVariantSets().GetNames() == ["Seat"]
    assert root.GetVariantSet("Seat").GetVariantSelection() == "Fabric"


def test_the_hidden_variant_was_staged_into_the_export(exported):
    """Leather is excluded from the scene's view layer."""
    pytest.importorskip("pxr")

    assert "SeatLeather" in _children(exported["stage"], "Leather")


def test_each_selection_composes_only_its_own_seat(exported):
    pytest.importorskip("pxr")

    fabric = _children(exported["stage"], "Fabric")
    leather = _children(exported["stage"], "Leather")

    assert "SeatFabric" in fabric and "SeatLeather" not in fabric
    assert "SeatLeather" in leather and "SeatFabric" not in leather


def test_the_plinth_belongs_to_no_variant_and_always_ships(exported):
    pytest.importorskip("pxr")

    for selection in ("Fabric", "Leather"):
        assert "Plinth" in _children(exported["stage"], selection)


def test_the_diagnostics_sidecar_records_what_was_authored(exported):
    payload = json.loads(exported["diagnostics"].read_text())

    assert payload["variant_sets"] == [
        {
            "name": "Seat",
            "default_variant": "Fabric",
            "variants": [
                {"name": "Fabric", "collection": "Fabric", "objects": ["SeatFabric"]},
                {
                    "name": "Leather",
                    "collection": "Leather",
                    "objects": ["SeatLeather"],
                },
            ],
        }
    ]


def test_a_clean_variant_export_warns_about_nothing(exported):
    payload = json.loads(exported["diagnostics"].read_text())

    assert payload["errors"] == []
    assert payload["warnings"] == []


def test_info_reports_the_declared_sets_without_exporting(scene):
    result = _usdstage("info", str(scene), "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["variant_sets"] == [
        {
            "name": "Seat",
            "default_variant": "Fabric",
            "variants": [
                {"name": "Fabric", "collection": "Fabric"},
                {"name": "Leather", "collection": "Leather"},
            ],
        }
    ]


def test_turning_variants_off_exports_what_the_viewport_shows(scene, tmp_path):
    """No variant set, and no staging either: the hidden seat stays out."""
    pytest.importorskip("pxr")
    output = tmp_path / "flat.usda"

    result = _usdstage(
        "export", str(scene), "export_variants=false",
        "-o", str(output), "--format", "USDA",
    )
    assert result.returncode == 0, result.stdout + result.stderr

    from pxr import Usd

    stage = Usd.Stage.Open(str(output))
    root = stage.GetDefaultPrim()
    assert root.GetVariantSets().GetNames() == []
    children = sorted(child.GetName() for child in root.GetChildren())
    assert "Plinth" in children
    assert "SeatFabric" in children
    assert "SeatLeather" not in children
