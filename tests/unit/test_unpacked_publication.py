"""Transactional publication and sidecar-ownership regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", SimpleNamespace())

from Plugin.export import blender_usd_export


def _make_staged_export(
    output_dir: Path,
    name: str,
    usd_bytes: bytes,
    sidecars: dict[str, bytes] | None = None,
) -> tuple[Path, Path]:
    final = output_dir / name
    staging = blender_usd_export.get_export_staging_dir(final)
    staging.mkdir(parents=True, exist_ok=True)
    staged_usd = staging / name
    staged_usd.write_bytes(usd_bytes)
    for relative, contents in (sidecars or {}).items():
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    return staged_usd, final


def _manifest(final: Path) -> dict:
    return json.loads(
        blender_usd_export._output_sidecar_manifest_path(final).read_text(
            encoding="utf-8"
        )
    )


def _assert_no_live_publication_transactions(output_dir: Path) -> None:
    root = output_dir / ".usdstage_publish"
    if not root.exists():
        return
    assert [path.name for path in root.iterdir()] == ["locks"]


def _usd_with_dependency(relative_path: str) -> bytes:
    return (
        '#usda 1.0\n\n'
        'def Xform "Root" {\n'
        f'    asset dependency = @{relative_path}@\n'
        '}\n'
    ).encode("utf-8")


def _resolved_dependency(final: Path) -> tuple[str, Path]:
    from pxr import Usd

    stage = Usd.Stage.Open(str(final), Usd.Stage.LoadAll)
    assert stage
    value = stage.GetPrimAtPath("/Root").GetAttribute("dependency").Get()
    return value.path, Path(value.resolvedPath)


def test_publish_keeps_nested_sidecars_and_records_exact_ownership(tmp_path):
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new usd",
        {
            "assets/chair.usda/shared.bin": b"dependency",
            "textures/chair-albedo.png": b"texture",
        },
    )

    blender_usd_export.publish_unpacked_export(staged, final)

    assert final.read_bytes() == b"new usd"
    assert (tmp_path / "assets/chair.usda/shared.bin").read_bytes() == b"dependency"
    assert (tmp_path / "textures/chair-albedo.png").read_bytes() == b"texture"
    assert _manifest(final)["sidecars"] == [
        "assets/chair.usda/shared.bin",
        "textures/chair-albedo.png",
    ]
    assert not staged.parent.exists()
    _assert_no_live_publication_transactions(tmp_path)


def test_two_outputs_with_same_dependency_basename_keep_independent_namespaces(tmp_path):
    staged_a, final_a = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"chair usd",
        {"assets/chair.usda/shared.bin": b"chair dependency"},
    )
    blender_usd_export.publish_unpacked_export(staged_a, final_a)

    staged_b, final_b = _make_staged_export(
        tmp_path,
        "table.usda",
        b"table usd",
        {"assets/table.usda/shared.bin": b"table dependency"},
    )
    blender_usd_export.publish_unpacked_export(staged_b, final_b)

    assert final_a.read_bytes() == b"chair usd"
    assert final_b.read_bytes() == b"table usd"
    assert (tmp_path / "assets/chair.usda/shared.bin").read_bytes() == b"chair dependency"
    assert (tmp_path / "assets/table.usda/shared.bin").read_bytes() == b"table dependency"
    assert _manifest(final_a)["sidecars"] == ["assets/chair.usda/shared.bin"]
    assert _manifest(final_b)["sidecars"] == ["assets/table.usda/shared.bin"]


def test_same_final_publication_is_fail_closed_while_process_lock_is_held(tmp_path):
    final = tmp_path / "chair.usda"

    with blender_usd_export._output_publication_lock(final):
        with pytest.raises(RuntimeError, match="already publishing"):
            with blender_usd_export._output_publication_lock(final):
                pytest.fail("second publisher must not enter the commit section")


@pytest.mark.parametrize(
    ("first_name", "alias_name"),
    [
        ("Scene.usdc", "scene.usdc"),
        (
            unicodedata.normalize("NFC", "Café.usdc"),
            unicodedata.normalize("NFD", "Café.usdc"),
        ),
    ],
)
def test_publication_lock_serializes_apple_filesystem_aliases(
    tmp_path,
    first_name,
    alias_name,
):
    first = tmp_path / first_name
    alias = tmp_path / alias_name
    assert blender_usd_export._output_sidecar_manifest_path(
        first
    ) == blender_usd_export._output_sidecar_manifest_path(alias)
    assert blender_usd_export._canonical_output_identity(
        first
    ) == blender_usd_export._canonical_output_identity(alias)

    with blender_usd_export._output_publication_lock(first):
        with pytest.raises(RuntimeError, match="already publishing"):
            with blender_usd_export._output_publication_lock(alias):
                pytest.fail("filesystem aliases must share one output lock")


@pytest.mark.parametrize(
    ("checkpoint", "expected_root", "new_sidecar_installed"),
    [
        ("after_transition_manifest", "old", False),
        ("after_sidecars", "old", True),
        ("after_root", "new", True),
    ],
)
def test_hard_exit_keeps_resolvable_generation_and_retry_recovers(
    tmp_path,
    checkpoint,
    expected_root,
    new_sidecar_installed,
):
    old_relative = "assets/chair.usda/old-generation/dependency.bin"
    new_relative = "assets/chair.usda/new-generation/dependency.bin"
    staged_old, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        _usd_with_dependency(old_relative),
        {old_relative: b"old dependency"},
    )
    blender_usd_export.publish_unpacked_export(staged_old, final)

    staged_new, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        _usd_with_dependency(new_relative),
        {new_relative: b"new dependency"},
    )
    repository = Path(__file__).resolve().parents[2]
    script = r'''
import os
import sys
import types

sys.modules.setdefault("bpy", types.ModuleType("bpy"))
sys.path.insert(0, sys.argv[1])
from Plugin.export import blender_usd_export

target = sys.argv[4]
def checkpoint(phase):
    if phase == target:
        os._exit(77)

blender_usd_export._publication_phase_checkpoint = checkpoint
blender_usd_export.publish_unpacked_export(sys.argv[2], sys.argv[3])
raise SystemExit(3)
'''
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(repository),
            str(staged_new),
            str(final),
            checkpoint,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 77, result.stderr

    expected_relative = old_relative if expected_root == "old" else new_relative
    expected_bytes = (
        b"old dependency" if expected_root == "old" else b"new dependency"
    )
    authored, resolved = _resolved_dependency(final)
    assert authored == expected_relative
    assert resolved.read_bytes() == expected_bytes
    assert (tmp_path / new_relative).exists() is new_sidecar_installed
    assert set(_manifest(final)["sidecars"]) == {old_relative, new_relative}

    # The OS releases the process lock on hard exit. A retry recognizes the
    # transition manifest, finishes any missing immutable files, replaces the
    # root last, and removes both the old generation and abandoned transaction.
    blender_usd_export.publish_unpacked_export(staged_new, final)

    authored, resolved = _resolved_dependency(final)
    assert authored == new_relative
    assert resolved.read_bytes() == b"new dependency"
    assert not (tmp_path / old_relative).exists()
    assert _manifest(final)["sidecars"] == [new_relative]
    _assert_no_live_publication_transactions(tmp_path)


def test_usd_asset_namespace_stays_resolvable_after_publication(tmp_path):
    from pxr import Usd
    from Plugin.export import usd_assets

    source = tmp_path / "source" / "shared.bin"
    source.parent.mkdir()
    source.write_bytes(b"composed dependency")
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        (
            '#usda 1.0\n\n'
            'def Xform "Root" {\n'
            f'    asset blob = @{source.as_posix()}@\n'
            '}\n'
        ).encode("utf-8"),
    )
    stage = Usd.Stage.Open(str(staged), Usd.Stage.LoadAll)
    usd_assets.prepare_assets(stage, str(staged))
    stage.Save()

    blender_usd_export.publish_unpacked_export(staged, final)

    reopened = Usd.Stage.Open(str(final), Usd.Stage.LoadAll)
    value = reopened.GetPrimAtPath("/Root").GetAttribute("blob").Get()
    relative = Path(value.path)
    assert relative.parts[0:2] == ("assets", "chair.usda")
    assert len(relative.parts[2]) == 32
    assert relative.name == "shared.bin"
    expected = tmp_path / relative
    assert Path(value.resolvedPath) == expected.resolve()
    assert expected.read_bytes() == b"composed dependency"


def test_reexport_removes_only_exact_stale_sidecars_for_that_output(tmp_path):
    staged_a, final_a = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"chair v1",
        {"assets/chair.usda/old.bin": b"old"},
    )
    blender_usd_export.publish_unpacked_export(staged_a, final_a)
    staged_b, final_b = _make_staged_export(
        tmp_path,
        "chair-v2.usda",
        b"chair v2",
        {"assets/chair-v2.usda/keep.bin": b"keep"},
    )
    blender_usd_export.publish_unpacked_export(staged_b, final_b)

    staged_a2, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"chair v3",
        {"assets/chair.usda/new.bin": b"new"},
    )
    blender_usd_export.publish_unpacked_export(staged_a2, final_a)

    assert not (tmp_path / "assets/chair.usda/old.bin").exists()
    assert (tmp_path / "assets/chair.usda/new.bin").read_bytes() == b"new"
    assert (tmp_path / "assets/chair-v2.usda/keep.bin").read_bytes() == b"keep"
    assert final_b.read_bytes() == b"chair v2"


def test_corrupt_other_output_manifest_fails_closed_before_stale_cleanup(tmp_path):
    old_relative = "assets/chair.usda/old.bin"
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"old root",
        {old_relative: b"old sidecar"},
    )
    blender_usd_export.publish_unpacked_export(staged, final)
    other_manifest = blender_usd_export._output_sidecar_manifest_path(
        tmp_path / "other.usda"
    )
    other_manifest.parent.mkdir(exist_ok=True)
    other_manifest.write_text("{not valid json", encoding="utf-8")
    staged_new, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new root",
        {"assets/chair.usda/new.bin": b"new sidecar"},
    )

    with pytest.raises(RuntimeError, match="Could not read sidecar ownership"):
        blender_usd_export.publish_unpacked_export(staged_new, final)

    assert final.read_bytes() == b"old root"
    assert (tmp_path / old_relative).read_bytes() == b"old sidecar"
    assert staged_new.exists()


def test_final_replace_failure_rolls_back_sidecar_and_manifest(tmp_path, monkeypatch):
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"old usd",
        {"assets/chair.usda/old-shared.bin": b"old dependency"},
    )
    blender_usd_export.publish_unpacked_export(staged, final)
    old_manifest = blender_usd_export._output_sidecar_manifest_path(final).read_bytes()

    staged_new, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new usd",
        {"assets/chair.usda/new-shared.bin": b"new dependency"},
    )
    real_replace = blender_usd_export._replace_publication_file

    def fail_final(source, destination):
        if destination == final:
            raise OSError("injected final move failure")
        real_replace(source, destination)

    monkeypatch.setattr(blender_usd_export, "_replace_publication_file", fail_final)

    with pytest.raises(OSError, match="injected final move failure"):
        blender_usd_export.publish_unpacked_export(staged_new, final)

    assert final.read_bytes() == b"old usd"
    assert (tmp_path / "assets/chair.usda/old-shared.bin").read_bytes() == b"old dependency"
    assert not (tmp_path / "assets/chair.usda/new-shared.bin").exists()
    assert blender_usd_export._output_sidecar_manifest_path(final).read_bytes() == old_manifest
    assert staged_new.exists()
    _assert_no_live_publication_transactions(tmp_path)


def test_second_sidecar_failure_rolls_back_first_sidecar(tmp_path, monkeypatch):
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"old usd",
        {
            "assets/chair.usda/old-a.bin": b"old a",
            "assets/chair.usda/old-b.bin": b"old b",
        },
    )
    blender_usd_export.publish_unpacked_export(staged, final)
    staged_new, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new usd",
        {
            "assets/chair.usda/new-a.bin": b"new a",
            "assets/chair.usda/new-b.bin": b"new b",
        },
    )
    failed_destination = tmp_path / "assets/chair.usda/new-b.bin"
    real_replace = blender_usd_export._replace_publication_file

    def fail_second_sidecar(source, destination):
        if destination == failed_destination:
            raise OSError("injected sidecar failure")
        real_replace(source, destination)

    monkeypatch.setattr(
        blender_usd_export,
        "_replace_publication_file",
        fail_second_sidecar,
    )

    with pytest.raises(OSError, match="injected sidecar failure"):
        blender_usd_export.publish_unpacked_export(staged_new, final)

    assert final.read_bytes() == b"old usd"
    assert (tmp_path / "assets/chair.usda/old-a.bin").read_bytes() == b"old a"
    assert (tmp_path / "assets/chair.usda/old-b.bin").read_bytes() == b"old b"
    assert not (tmp_path / "assets/chair.usda/new-a.bin").exists()
    assert not (tmp_path / "assets/chair.usda/new-b.bin").exists()


def test_timeout_during_commit_rolls_back_and_propagates(tmp_path, monkeypatch):
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"old usd",
        {"assets/chair.usda/old-shared.bin": b"old dependency"},
    )
    blender_usd_export.publish_unpacked_export(staged, final)
    staged_new, _ = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new usd",
        {"assets/chair.usda/new-shared.bin": b"new dependency"},
    )
    real_replace = blender_usd_export._replace_publication_file

    def timeout_on_final(source, destination):
        if destination == final:
            raise TimeoutError("worker deadline reached")
        real_replace(source, destination)

    monkeypatch.setattr(
        blender_usd_export,
        "_replace_publication_file",
        timeout_on_final,
    )

    with pytest.raises(TimeoutError, match="worker deadline reached"):
        blender_usd_export.publish_unpacked_export(staged_new, final)

    assert final.read_bytes() == b"old usd"
    assert (tmp_path / "assets/chair.usda/old-shared.bin").read_bytes() == b"old dependency"
    assert not (tmp_path / "assets/chair.usda/new-shared.bin").exists()
    _assert_no_live_publication_transactions(tmp_path)


def test_preexisting_unowned_sidecar_collision_fails_without_overwrite(tmp_path):
    collision = tmp_path / "assets/chair.usda/shared.bin"
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"unrelated user data")
    staged, final = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"new usd",
        {"assets/chair.usda/shared.bin": b"export dependency"},
    )

    with pytest.raises(RuntimeError, match="unowned sidecar collision"):
        blender_usd_export.publish_unpacked_export(staged, final)

    assert collision.read_bytes() == b"unrelated user data"
    assert not final.exists()


def test_different_output_cannot_replace_another_outputs_owned_sidecar(tmp_path):
    staged_a, final_a = _make_staged_export(
        tmp_path,
        "chair.usda",
        b"chair ascii",
        {"textures/chair-albedo.png": b"chair texture"},
    )
    blender_usd_export.publish_unpacked_export(staged_a, final_a)
    staged_b, final_b = _make_staged_export(
        tmp_path,
        "chair.usdc",
        b"chair binary",
        {"textures/chair-albedo.png": b"different texture"},
    )

    with pytest.raises(RuntimeError, match="Immutable sidecar collision"):
        blender_usd_export.publish_unpacked_export(staged_b, final_b)

    assert final_a.read_bytes() == b"chair ascii"
    assert not final_b.exists()
    assert (tmp_path / "textures/chair-albedo.png").read_bytes() == b"chair texture"


def test_symlinked_assets_destination_is_rejected_without_touching_sentinel(tmp_path):
    output = tmp_path / "output"
    external = tmp_path / "external"
    output.mkdir()
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"do not touch")
    (output / "assets").symlink_to(external, target_is_directory=True)
    staged, final = _make_staged_export(
        output,
        "chair.usda",
        b"new usd",
        {"assets/chair.usda/shared.bin": b"export dependency"},
    )

    with pytest.raises(RuntimeError, match="symlinked sidecar destination root"):
        blender_usd_export.publish_unpacked_export(staged, final)

    assert sentinel.read_bytes() == b"do not touch"
    assert not (external / "chair.usda/shared.bin").exists()
    assert not final.exists()


def test_symlinked_nested_sidecar_directory_is_rejected(tmp_path):
    output = tmp_path / "output"
    external = tmp_path / "external"
    nested_root = output / "assets" / "chair.usda"
    nested_root.mkdir(parents=True)
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"do not touch")
    (nested_root / "generation").symlink_to(
        external,
        target_is_directory=True,
    )
    staged, final = _make_staged_export(
        output,
        "chair.usda",
        b"new usd",
        {"assets/chair.usda/generation/shared.bin": b"dependency"},
    )

    with pytest.raises(RuntimeError, match="symlinked sidecar destination directory"):
        blender_usd_export.publish_unpacked_export(staged, final)

    assert sentinel.read_bytes() == b"do not touch"
    assert not (external / "shared.bin").exists()
    assert not final.exists()


def test_symlinked_staging_root_cannot_redirect_reset_or_cleanup(tmp_path):
    output = tmp_path / "output"
    external = tmp_path / "external"
    output.mkdir()
    external.mkdir()
    external_staging = external / "chair"
    external_staging.mkdir()
    sentinel = external_staging / "sentinel.bin"
    sentinel.write_bytes(b"do not delete")
    (output / ".usdstage_temp").symlink_to(external, target_is_directory=True)
    final = output / "chair.usda"
    staging = blender_usd_export.get_export_staging_dir(final)

    with pytest.raises(RuntimeError, match="symlinked export staging root"):
        blender_usd_export._reset_export_staging_dir(staging)
    blender_usd_export.remove_export_staging_dir(final, staging_dir=staging)

    assert sentinel.read_bytes() == b"do not delete"
    assert (output / ".usdstage_temp").is_symlink()
