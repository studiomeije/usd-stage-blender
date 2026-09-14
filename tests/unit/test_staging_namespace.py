"""Crash-safe sidecar generation namespace tests."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from Plugin.export import staging_namespace


def test_one_export_reuses_generation_across_asset_and_texture_passes(tmp_path):
    root = tmp_path / "scene.usda"
    root.write_text("#usda 1.0\n")

    first = staging_namespace.output_sidecar_namespace(root)
    second = staging_namespace.output_sidecar_namespace(root)

    assert first == second
    assert first.parts[0] == "scene.usda"
    assert re.fullmatch(r"[0-9a-f]{32}", first.parts[1])


def test_new_export_attempt_gets_an_immutable_new_generation(tmp_path):
    root = tmp_path / "scene.usda"
    root.write_text("#usda 1.0\n")
    first = staging_namespace.output_sidecar_namespace(root)

    shutil.rmtree(tmp_path / staging_namespace.GENERATION_MARKER_DIRECTORY)
    second = staging_namespace.output_sidecar_namespace(root)

    assert second != first


def test_output_filename_is_portable_without_namespace_collisions(tmp_path):
    root = tmp_path / "scene:look.usda"
    root.write_text("#usda 1.0\n")

    namespace = staging_namespace.output_sidecar_namespace(root)

    assert namespace.parts[0].startswith("scene_look.usda-")
    assert len(namespace.parts[0].rsplit("-", 1)[1]) == 8


# ---------------------------------------------------------------------------
# Native texture copy capture
#
# export_textures_mode='NEW' makes wm.usd_export copy packed/generated images
# to <staging>/textures/<basename>. Texture staging supersedes each with a
# content-addressed copy and must delete the flat original, but the originals
# cannot be recognised by path shape: a user's own authoritative texture can
# legitimately sit at textures/<name>.png. The export therefore records what
# appeared while the native operator ran, and staging consumes that record.
# ---------------------------------------------------------------------------


def test_snapshot_reports_only_files_directly_in_textures(tmp_path):
    textures = tmp_path / "textures"
    (textures / "nested").mkdir(parents=True)
    flat = textures / "wood.png"
    flat.write_bytes(b"flat")
    (textures / "nested" / "deep.png").write_bytes(b"deep")

    snapshot = staging_namespace.snapshot_texture_directory(tmp_path)

    assert snapshot == {flat.resolve()}


def test_snapshot_of_missing_textures_dir_is_empty(tmp_path):
    assert staging_namespace.snapshot_texture_directory(tmp_path) == frozenset()


def test_record_captures_only_files_created_during_the_export(tmp_path):
    textures = tmp_path / "textures"
    textures.mkdir()
    pre_existing = textures / "user_asset.png"
    pre_existing.write_bytes(b"authoritative")

    before = staging_namespace.snapshot_texture_directory(tmp_path)
    written_by_blender = textures / "packedtex.png"
    written_by_blender.write_bytes(b"blender copy")
    after = staging_namespace.snapshot_texture_directory(tmp_path)

    created = staging_namespace.record_native_texture_copies(tmp_path, before, after)

    assert created == {written_by_blender.resolve()}
    assert pre_existing.resolve() not in created, (
        "a file that predates the native export is not ours to delete"
    )
    staging_namespace.forget_native_texture_copies(tmp_path)


def test_take_is_destructive_so_a_reused_directory_cannot_inherit(tmp_path):
    textures = tmp_path / "textures"
    textures.mkdir()
    created_file = textures / "packedtex.png"
    created_file.write_bytes(b"x")
    staging_namespace.record_native_texture_copies(
        tmp_path, frozenset(), frozenset({created_file.resolve()})
    )

    assert staging_namespace.take_native_texture_copies(tmp_path) == {
        created_file.resolve()
    }
    assert staging_namespace.take_native_texture_copies(tmp_path) == frozenset()


def test_take_for_an_export_that_never_ran_the_native_exporter_is_empty(tmp_path):
    """Utility callers stage textures without ever invoking wm.usd_export."""
    assert staging_namespace.take_native_texture_copies(tmp_path) == frozenset()


def test_recording_no_new_files_clears_any_previous_entry(tmp_path):
    staging_namespace.record_native_texture_copies(
        tmp_path, frozenset(), frozenset({Path("/stale/thing.png")})
    )
    staging_namespace.record_native_texture_copies(tmp_path, frozenset(), frozenset())

    assert staging_namespace.take_native_texture_copies(tmp_path) == frozenset()
