"""Unit tests for guaranteed export-staging cleanup.

`remove_export_staging_dir` is the finally-safe counterpart to
`cleanup_export_staging_dir`: it must remove the `.usdstage_temp/<stem>`
tree after any export attempt (success or failure) so it never lingers in the
user's export directory, while refusing to touch anything outside that tree.

`blender_usd_export` imports `bpy` (and `animation_export`, which also imports
`bpy`) at module load, so a stub is injected before import; the function under
test is pure pathlib/shutil.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export.blender_usd_export import (  # noqa: E402
    cleanup_export_staging_dir,
    create_export_staging_dir,
    get_export_staging_dir,
    remove_export_staging_dir,
)


def _make_staging(tmp_path: Path) -> tuple[Path, Path]:
    """Create a realistic attempt-scoped staging tree with content."""
    final = tmp_path / "Chess" / "ChessboardMarbleExport.usda"
    staging = create_export_staging_dir(final)
    (staging / "textures").mkdir(parents=True)
    (staging / "ChessboardMarbleExport.usda").write_text("#usda 1.0\n")
    (staging / "textures" / "marble.png").write_bytes(b"\x89PNG")
    assert staging.exists()
    return final, staging


def test_removes_staging_tree_and_empty_root(tmp_path):
    final, staging = _make_staging(tmp_path)
    temp_root = staging.parent
    assert temp_root.name == ".usdstage_temp"

    remove_export_staging_dir(final, staging_dir=staging)

    assert not staging.exists()
    # The now-empty .usdstage_temp root is dropped too.
    assert not temp_root.exists()


def test_idempotent_when_already_gone(tmp_path):
    final, staging = _make_staging(tmp_path)
    remove_export_staging_dir(final, staging_dir=staging)
    # Second call (e.g. publish already cleaned it) must not raise.
    remove_export_staging_dir(final, staging_dir=staging)
    assert not staging.exists()


def test_keeps_root_when_a_sibling_export_still_staged(tmp_path):
    final_a, staging_a = _make_staging(tmp_path)
    # A second export staging under the same .usdstage_temp root.
    final_b = tmp_path / "Chess" / "OtherExport.usda"
    staging_b = create_export_staging_dir(final_b)

    remove_export_staging_dir(final_a, staging_dir=staging_a)

    assert not staging_a.exists()
    # The shared root survives because sibling B is still staged inside it.
    assert staging_b.exists()
    assert staging_b.parent.exists()


def test_does_not_touch_paths_outside_usdstage_temp(tmp_path, monkeypatch):
    # If get_export_staging_dir ever resolved to a non-temp dir, refuse to rmtree.
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "keep.txt").write_text("do not delete")

    monkeypatch.setattr(
        "Plugin.export.blender_usd_export._validate_staging_matches_final",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("unsafe staging path")),
    )
    remove_export_staging_dir(
        tmp_path / "whatever.usda",
        staging_dir=victim,
    )

    assert victim.exists()
    assert (victim / "keep.txt").exists()


def test_attempts_are_unique_for_same_output_and_across_extensions(tmp_path):
    usda = tmp_path / "scene.usda"
    usdc = tmp_path / "scene.usdc"

    first = get_export_staging_dir(usda)
    second = get_export_staging_dir(usda)
    binary = get_export_staging_dir(usdc)

    assert first != second
    assert first.name.startswith("scene.usda.")
    assert binary.name.startswith("scene.usdc.")
    assert len({first, second, binary}) == 3


def test_cleanup_removes_only_the_exact_interleaved_attempt(tmp_path):
    final = tmp_path / "scene.usda"
    first = create_export_staging_dir(final)
    second = create_export_staging_dir(final)
    (first / "owner.txt").write_text("first")
    (second / "owner.txt").write_text("second")

    remove_export_staging_dir(final, staging_dir=first)

    assert not first.exists()
    assert (second / "owner.txt").read_text() == "second"
    remove_export_staging_dir(final, staging_dir=second)


def test_allocator_never_reuses_or_resets_preexisting_attempt(tmp_path, monkeypatch):
    final = tmp_path / "scene.usda"
    occupied_token = "0" * 32
    fresh_token = "1" * 32
    occupied = get_export_staging_dir(final, attempt_id=occupied_token)
    occupied.mkdir(parents=True)
    sentinel = occupied / "keep.txt"
    sentinel.write_text("unowned")
    tokens = iter((occupied_token, fresh_token))
    monkeypatch.setattr(
        "Plugin.export.blender_usd_export.secrets.token_hex",
        lambda _size: next(tokens),
    )

    allocated = create_export_staging_dir(final)

    assert allocated == get_export_staging_dir(final, attempt_id=fresh_token)
    assert sentinel.read_text() == "unowned"
    remove_export_staging_dir(final, staging_dir=allocated)


def test_cleanup_rejects_another_outputs_prefix_matching_attempt(tmp_path):
    shorter_output = tmp_path / "scene"
    longer_output = tmp_path / "scene.usda"
    longer_attempt = create_export_staging_dir(longer_output)
    sentinel = longer_attempt / "keep.txt"
    sentinel.write_text("other export")

    remove_export_staging_dir(
        shorter_output,
        staging_dir=longer_attempt,
    )

    assert sentinel.read_text() == "other export"
    remove_export_staging_dir(longer_output, staging_dir=longer_attempt)


def test_cleanup_drops_the_unconsumed_native_texture_record(tmp_path):
    """A failed attempt must not leave its capture behind.

    Texture staging pops the record on the success path. On the failure path
    nothing consumes it, so without this the entry would outlive its staging
    directory for the life of the Blender session.
    """
    from Plugin.export import staging_namespace

    temp_root = tmp_path / ".usdstage_temp"
    staging = temp_root / "scene.usda.0123456789abcdef0123456789abcdef"
    staging.mkdir(parents=True)
    staged_layer = staging / "scene.usda"
    staged_layer.write_text("#usda 1.0\n")

    staging_namespace.record_native_texture_copies(
        staging, frozenset(), frozenset({staging / "textures" / "packedtex.png"})
    )

    cleanup_export_staging_dir(staged_layer)

    assert staging_namespace.take_native_texture_copies(staging) == frozenset()


def test_staging_dir_is_absolute_for_a_relative_output(tmp_path, monkeypatch):
    """A relative ``-o`` must still stage under an absolute directory.

    Measured defect: ``bake-export -o scene.usdz`` wrote its baked textures to
    ``.usdstage_temp/…`` and then died with

        Texture file not found: /.usdstage_temp/…/Cube_Baked_baseColor.png

    against a file it had just written. ``create_texture_staging_state``
    resolves the staging USD to an absolute path and authors baked textures
    relative to it, so a relative staging directory made that a
    relative-against-absolute comparison. The authored asset path came back as
    ``../`` × 7 - enough to walk past the filesystem root, where it clamps -
    and every anchor downstream inherited the ``/``-rooted result.

    Assert the property that prevents it rather than the symptom: the staging
    directory is absolute, and it is inside the output's own directory.
    """
    monkeypatch.chdir(tmp_path)

    staging = get_export_staging_dir("scene.usdz")

    assert staging.is_absolute(), staging
    # Rooted at the output's directory, not at "/".
    assert staging.parent == (tmp_path / ".usdstage_temp").resolve()
    # The ../-walk that produced the defect cannot be built from this.
    assert ".." not in staging.parts


def test_staging_dir_matches_between_relative_and_absolute_spellings(tmp_path, monkeypatch):
    """The same output named two ways must stage in the same place.

    Relative and absolute spellings of one destination diverged before the fix:
    only the absolute spelling produced a usable staging root, which is why the
    Blender panel never hit the defect and the CLI always did.
    """
    monkeypatch.chdir(tmp_path)
    token = "0123456789abcdef0123456789abcdef"

    relative = get_export_staging_dir("out/scene.usdz", attempt_id=token)
    absolute = get_export_staging_dir(tmp_path / "out" / "scene.usdz", attempt_id=token)

    assert relative == absolute


def test_ownership_check_accepts_a_relatively_named_output(tmp_path, monkeypatch):
    """The guard must normalize its expectation the same way staging does.

    Second half of the same defect. Making ``get_export_staging_dir`` absolute
    was not enough: ``_validate_staging_matches_final`` rebuilt its expected
    root from the caller's raw path, so an absolute staging directory failed
    the ownership check against a relative output with

        Export staging directory '/…/.usdstage_temp/scene.usdz.<token>'
        does not belong to 'scene.usdz'.

    Both sides go through one normalizer now, so the two spellings of a
    destination cannot drift apart again.
    """
    from Plugin.export.blender_usd_export import _validate_staging_matches_final

    monkeypatch.chdir(tmp_path)
    # Build the absolute staging directory directly rather than through
    # get_export_staging_dir. Taking both sides from the same function would
    # let them agree while both were wrong, which is not a test.
    token = "0123456789abcdef0123456789abcdef"
    staging = tmp_path / ".usdstage_temp" / f"scene.usdz.{token}"
    staging.mkdir(parents=True)

    # Names the output relatively; the staging directory is absolute.
    assert _validate_staging_matches_final(staging, "scene.usdz") == staging


def test_ownership_check_still_rejects_a_foreign_staging_dir(tmp_path, monkeypatch):
    """Normalizing must not soften the guard it runs inside.

    The check exists to stop one export writing into another's staging
    directory. Absolutizing both sides makes more paths comparable, so prove a
    genuine mismatch is still refused rather than newly accepted.
    """
    from Plugin.export.blender_usd_export import _validate_staging_matches_final

    monkeypatch.chdir(tmp_path)
    other = get_export_staging_dir("somebody_else.usdz")
    other.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="does not belong to"):
        _validate_staging_matches_final(other, "scene.usdz")
