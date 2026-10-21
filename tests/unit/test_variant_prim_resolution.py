"""Resolving a captured object prim path after normalization renamed it.

Variant authoring has to turn a Blender object into the exact prim it became.
The USDHook's prim map answers that, but it is captured during export, before
``normalize_scene`` may rename an identifier it cannot keep - so the two halves
have to agree. These tests pin that contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Usd  # noqa: E402

from Plugin.export import usd_hook  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.usd_scene import normalize_scene, remap_prim_path  # noqa: E402


def _settings(**overrides):
    values = {
        "root_prim_name": "Root",
        "convert_orientation": False,
        "allow_unicode": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# The captured object map
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_capture():
    usd_hook._captured["objects"] = None
    yield
    usd_hook._captured["objects"] = None


def test_consuming_the_object_map_clears_it():
    """A stale map must not leak into a postprocess run on another stage."""
    usd_hook._captured["objects"] = {"/Root/Seat": "Seat"}

    assert usd_hook.consume_captured_object_map() == {"/Root/Seat": "Seat"}
    assert usd_hook.consume_captured_object_map() is None


def test_an_export_without_the_hook_reports_no_object_map():
    assert usd_hook.consume_captured_object_map() is None


# --------------------------------------------------------------------------
# Rename edits published by normalize_scene
# --------------------------------------------------------------------------

def test_normalize_scene_reports_no_edits_when_every_name_is_valid():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Seat", "Xform")

    assert normalize_scene(stage, _settings()) == ()


def test_normalize_scene_publishes_the_edits_it_applied():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Chaise_Café", "Xform")

    edits = normalize_scene(stage, _settings())

    assert [(str(old), str(new)) for old, new in edits] == [
        ("/Root/Chaise_Café", "/Root/Chaise_Caf_")
    ]
    assert stage.GetPrimAtPath("/Root/Chaise_Caf_")


def test_a_captured_path_survives_its_own_rename():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Chaise_Café", "Xform")
    captured = "/Root/Chaise_Café"

    edits = normalize_scene(stage, _settings())

    resolved = remap_prim_path(captured, edits)
    assert resolved == "/Root/Chaise_Caf_"
    assert stage.GetPrimAtPath(resolved)


def test_a_captured_path_survives_a_renamed_ancestor():
    """The object prim keeps its name; its parent does not."""
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Sièges/Seat", "Xform")
    captured = "/Root/Sièges/Seat"

    edits = normalize_scene(stage, _settings())

    resolved = remap_prim_path(captured, edits)
    assert resolved == "/Root/Si_ges/Seat"
    assert stage.GetPrimAtPath(resolved)


def test_remapping_is_identity_when_nothing_was_renamed():
    assert remap_prim_path("/Root/Seat", ()) == "/Root/Seat"


def test_a_path_untouched_by_the_edits_is_returned_unchanged():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Chaise_Café", "Xform")
    stage.DefinePrim("/Root/Seat", "Xform")

    edits = normalize_scene(stage, _settings())

    assert remap_prim_path("/Root/Seat", edits) == "/Root/Seat"


def test_colliding_renames_each_resolve_to_their_own_prim():
    """Two members whose names reduce alike must not collapse onto one prim."""
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Seat_Café", "Xform")
    stage.DefinePrim("/Root/Seat_Cafè", "Xform")

    edits = normalize_scene(stage, _settings())

    first = remap_prim_path("/Root/Seat_Café", edits)
    second = remap_prim_path("/Root/Seat_Cafè", edits)
    assert first != second
    assert stage.GetPrimAtPath(first) and stage.GetPrimAtPath(second)


def test_renames_reach_the_diagnostics_sidecar():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    stage.DefinePrim("/Root/Chaise_Café", "Xform")
    diagnostics = ExportDiagnostics()

    normalize_scene(stage, _settings(), diagnostics=diagnostics)

    assert diagnostics.to_dict()["prim_renames"] == [
        {"from": "/Root/Chaise_Café", "to": "/Root/Chaise_Caf_"}
    ]
    # Routine normalization must not spend a warning slot.
    assert diagnostics.to_dict()["warnings"] == []


def test_an_export_with_no_renames_records_none():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    diagnostics = ExportDiagnostics()

    normalize_scene(stage, _settings(), diagnostics=diagnostics)

    assert "prim_renames" not in diagnostics.to_dict()
