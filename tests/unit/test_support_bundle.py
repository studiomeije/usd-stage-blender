"""Support bundle helper tests."""

from __future__ import annotations

import json
import os
import subprocess
import unicodedata
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import sys

from Plugin.export import support_bundle
from Plugin.export.sidecar_manifest import (
    canonical_output_identity,
    output_sidecar_manifest_path,
)
from Plugin.export.support_bundle import create_support_bundle, _redact_text


def _write_sidecar_manifest(export: Path, sidecars: list[str]) -> Path:
    manifest = output_sidecar_manifest_path(export)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "output": canonical_output_identity(export),
        "sidecars": sidecars,
    }))
    return manifest


@pytest.fixture
def lightweight_bundle_collection(monkeypatch):
    monkeypatch.setattr(support_bundle, "collect_environment", lambda context=None: {})
    monkeypatch.setattr(support_bundle, "collect_scene_snapshot", lambda context=None: {})
    monkeypatch.setattr(
        support_bundle,
        "collect_asset_dependency_snapshot",
        lambda context=None: {},
    )
    monkeypatch.setattr(support_bundle, "collect_validation_snapshot", lambda context=None: {})


def test_support_bundle_redacts_paths_and_includes_manifest(tmp_path: Path):
    blend = tmp_path / "Scene.blend"
    export = tmp_path / "Scene.usdz"
    diagnostics = tmp_path / "Scene.diagnostics.json"
    job_dir = tmp_path / ".usdstage_jobs" / "job"
    job_dir.mkdir(parents=True)

    blend.write_bytes(b"blend")
    export.write_bytes(b"usdz")
    diagnostics.write_text(json.dumps({"filepath": str(export), "errors": []}))
    (job_dir / "status.json").write_text(json.dumps({"log_path": str(job_dir / "log.txt")}))
    (job_dir / "settings.json").write_text(json.dumps({"blend_file": str(blend)}))
    (job_dir / "log.txt").write_text(f"opened {blend}\n")

    result = create_support_bundle(
        blend_file=str(blend),
        export_path=str(export),
        diagnostics_path=str(diagnostics),
        job_dir=str(job_dir),
        bundle_output=str(tmp_path / "support.zip"),
    )

    bundle = Path(result["support_bundle_path"])
    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
        assert any(name.endswith("manifest.json") for name in names)
        diag_name = next(name for name in names if name.endswith("diagnostics/export.diagnostics.json"))
        diag_text = zf.read(diag_name).decode()
        assert str(tmp_path) not in diag_text
        assert "$EXPORT_DIR" in diag_text or "$BLEND_DIR" in diag_text or "$HOME" in diag_text


def test_support_bundle_skips_material_validation_for_bake_jobs(tmp_path: Path):
    blend = tmp_path / "Scene.blend"
    export = tmp_path / "Scene.usdz"
    diagnostics = tmp_path / "Scene.diagnostics.json"
    job_dir = tmp_path / ".usdstage_jobs" / "job"
    job_dir.mkdir(parents=True)

    blend.write_bytes(b"blend")
    export.write_bytes(b"usdz")
    diagnostics.write_text(json.dumps({
        "export_context": {"command": "background_bake_export"},
        "errors": [],
    }))
    (job_dir / "settings.json").write_text(json.dumps({
        "blend_file": str(blend),
        "export_settings": {"bake_mode": "LIT_IBL"},
    }))

    result = create_support_bundle(
        blend_file=str(blend),
        export_path=str(export),
        diagnostics_path=str(diagnostics),
        job_dir=str(job_dir),
        bundle_output=str(tmp_path / "support.zip"),
    )

    with zipfile.ZipFile(result["support_bundle_path"]) as zf:
        names = zf.namelist()
        assert any(name.endswith("diagnostics/assets.json") for name in names)
        assert not any(name.endswith("diagnostics/validate.json") for name in names)


def test_redact_text_handles_json_escaped_windows_paths():
    source = r"C:\Users\artist\Projects\Blender\scene"
    text = json.dumps({
        "export_path": source + r"\scene.usdz",
        "log": f"opened {source}\\scene.blend",
    })

    redacted = _redact_text(text, [(source, "$EXPORT_DIR")])

    assert source not in redacted
    assert source.replace("\\", "\\\\") not in redacted
    assert "$EXPORT_DIR" in redacted


def test_apple_toolchain_is_non_fatal_off_macos(monkeypatch):
    monkeypatch.setattr(support_bundle.platform, "system", lambda: "Linux")

    def unexpected_run(*args, **kwargs):
        raise AssertionError("Apple tools must not be invoked on non-macOS hosts")

    monkeypatch.setattr(support_bundle.subprocess, "run", unexpected_run)

    result = support_bundle._collect_apple_toolchain()

    assert result["available"] is False
    assert "macOS" in result["reason"]


def test_tool_probe_records_missing_and_timed_out_tools(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(support_bundle.subprocess, "run", missing)
    assert support_bundle._run_tool_probe(["missing-tool"])["error"] == "not found"

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5)

    monkeypatch.setattr(support_bundle.subprocess, "run", timed_out)
    assert support_bundle._run_tool_probe(["slow-tool"])["error"] == "timed out"


def test_blender_gpu_info_does_not_initialize_gpu_in_background():
    bpy = SimpleNamespace(app=SimpleNamespace(background=True))
    context = SimpleNamespace(
        preferences=SimpleNamespace(
            system=SimpleNamespace(gpu_backend="METAL"),
            addons={
                "cycles": SimpleNamespace(
                    preferences=SimpleNamespace(compute_device_type="METAL")
                )
            },
        ),
        scene=SimpleNamespace(render=SimpleNamespace(engine="BLENDER_EEVEE_NEXT")),
    )

    result = support_bundle._collect_blender_gpu_info(bpy, context)

    assert result == {
        "backend_preference": "METAL",
        "render_engine": "BLENDER_EEVEE_NEXT",
        "cycles_compute_device_type": "METAL",
        "active_context": "unavailable-in-background",
    }


def test_include_output_uses_only_exact_owned_sidecars(
    tmp_path: Path,
    lightweight_bundle_collection,
):
    export = tmp_path / "Chair.usda"
    other_export = tmp_path / "Table.usda"
    chair_texture = tmp_path / "textures" / "chair" / "base.png"
    table_texture = tmp_path / "textures" / "table" / "private.png"
    unowned_asset = tmp_path / "assets" / "unowned.usda"
    for path, payload in (
        (export, b"chair"),
        (other_export, b"table"),
        (chair_texture, b"chair texture"),
        (table_texture, b"table texture"),
        (unowned_asset, b"unowned"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    chair_manifest = _write_sidecar_manifest(
        export,
        ["textures/chair/base.png"],
    )
    _write_sidecar_manifest(
        other_export,
        ["textures/table/private.png"],
    )

    result = create_support_bundle(
        export_path=str(export),
        bundle_output=str(tmp_path / "support.zip"),
        include_output=True,
    )

    with zipfile.ZipFile(result["support_bundle_path"]) as archive:
        names = archive.namelist()
        assert any(name.endswith("output/Chair.usda") for name in names)
        assert any(name.endswith("output/textures/chair/base.png") for name in names)
        assert any(
            name.endswith(
                f"output/.usdstage_sidecars/{chair_manifest.name}"
            )
            for name in names
        )
        assert not any("Table.usda" in name for name in names)
        assert not any("textures/table/private.png" in name for name in names)
        assert not any("assets/unowned.usda" in name for name in names)


def test_sidecar_manifest_identity_is_casefolded_and_nfc(tmp_path: Path):
    composed = tmp_path / "SCÈNE.usdC"
    decomposed = tmp_path / unicodedata.normalize("NFD", "scène.usdc")

    assert canonical_output_identity(composed) == "scène.usdc"
    assert canonical_output_identity(decomposed) == "scène.usdc"
    assert output_sidecar_manifest_path(composed) == output_sidecar_manifest_path(
        decomposed
    )


def test_casefold_aliases_fail_closed_when_both_files_exist(tmp_path: Path):
    first = tmp_path / "Chair.usda"
    second = tmp_path / "chair.usda"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    if first.samefile(second):
        pytest.skip("filesystem does not permit distinct case aliases")
    _write_sidecar_manifest(second, [])

    with pytest.raises(RuntimeError, match="Ambiguous output filenames"):
        create_support_bundle(
            export_path=str(first),
            bundle_output=str(tmp_path / "support.zip"),
            include_output=True,
        )


@pytest.mark.parametrize(
    "unsafe_entry",
    [
        "../private.txt",
        "textures/../../private.txt",
        "/private.txt",
        "textures\\private.txt",
        "textures//private.txt",
    ],
)
def test_include_output_rejects_malformed_manifest_entries(
    tmp_path: Path,
    unsafe_entry: str,
):
    export = tmp_path / "Scene.usda"
    export.write_bytes(b"scene")
    _write_sidecar_manifest(export, [unsafe_entry])
    bundle = tmp_path / "support.zip"

    with pytest.raises(RuntimeError, match="unsafe export sidecar ownership"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
            include_output=True,
        )

    assert not bundle.exists()


def test_include_output_rejects_symlinked_sidecar_file(
    tmp_path: Path,
):
    output_dir = tmp_path / "exports"
    export = output_dir / "Scene.usda"
    secret = tmp_path / "private.txt"
    sidecar = output_dir / "textures" / "scene" / "base.png"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"scene")
    secret.write_bytes(b"private")
    sidecar.parent.mkdir(parents=True)
    sidecar.symlink_to(secret)
    _write_sidecar_manifest(export, ["textures/scene/base.png"])
    bundle = output_dir / "support.zip"

    with pytest.raises(RuntimeError, match="symlinked sidecar file"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
            include_output=True,
        )

    assert secret.read_bytes() == b"private"
    assert not bundle.exists()


def test_include_output_rejects_symlinked_sidecar_directory(
    tmp_path: Path,
):
    output_dir = tmp_path / "exports"
    export = output_dir / "Scene.usda"
    private_dir = tmp_path / "private"
    private_sidecar = private_dir / "base.png"
    linked_dir = output_dir / "textures" / "scene"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"scene")
    private_dir.mkdir()
    private_sidecar.write_bytes(b"private")
    linked_dir.parent.mkdir(parents=True)
    linked_dir.symlink_to(private_dir, target_is_directory=True)
    _write_sidecar_manifest(export, ["textures/scene/base.png"])
    bundle = output_dir / "support.zip"

    with pytest.raises(RuntimeError, match="symlinked sidecar directory"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
            include_output=True,
        )

    assert private_sidecar.read_bytes() == b"private"
    assert not bundle.exists()


def test_include_output_rejects_hardlinked_out_of_tree_sidecar(tmp_path: Path):
    output_dir = tmp_path / "exports"
    export = output_dir / "Scene.usda"
    secret = tmp_path / "private.txt"
    sidecar = output_dir / "textures" / "scene" / "base.png"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"scene")
    secret.write_bytes(b"private")
    sidecar.parent.mkdir(parents=True)
    os.link(secret, sidecar)
    _write_sidecar_manifest(export, ["textures/scene/base.png"])
    bundle = output_dir / "support.zip"

    with pytest.raises(RuntimeError, match="hard-linked sidecar file"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
            include_output=True,
        )

    assert secret.read_bytes() == b"private"
    assert not bundle.exists()


def test_bundle_output_rejects_direct_input_collisions_without_truncation(
    tmp_path: Path,
):
    blend = tmp_path / "Scene.blend"
    export = tmp_path / "Scene.usda"
    diagnostics = tmp_path / "Scene.diagnostics.json"
    job = tmp_path / "job"
    status = job / "status.json"
    job.mkdir()
    sources = {
        blend: b"blend input",
        export: b"export input",
        diagnostics: b"diagnostics input",
        status: b"job input",
    }
    for path, payload in sources.items():
        path.write_bytes(payload)

    for target, expected in sources.items():
        with pytest.raises(RuntimeError, match="collid|job input"):
            create_support_bundle(
                blend_file=str(blend),
                export_path=str(export),
                diagnostics_path=str(diagnostics),
                job_dir=str(job),
                bundle_output=str(target),
            )
        assert target.read_bytes() == expected


def test_bundle_output_rejects_hardlink_alias_without_truncation(tmp_path: Path):
    export = tmp_path / "Scene.usda"
    bundle = tmp_path / "support.zip"
    export.write_bytes(b"export input")
    os.link(export, bundle)

    with pytest.raises(RuntimeError, match="aliases an input"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
        )

    assert export.read_bytes() == b"export input"
    assert bundle.read_bytes() == b"export input"


def test_bundle_output_rejects_symlink_alias_without_truncation(tmp_path: Path):
    export = tmp_path / "Scene.usda"
    bundle = tmp_path / "support.zip"
    export.write_bytes(b"export input")
    bundle.symlink_to(export)

    with pytest.raises(RuntimeError, match="symlinked support bundle output"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
        )

    assert export.read_bytes() == b"export input"
    assert bundle.read_bytes() == b"export input"


def test_bundle_output_rejects_case_and_unicode_aliases(tmp_path: Path):
    export = tmp_path / "SCÈNE.usdC"
    bundle = tmp_path / unicodedata.normalize("NFD", "scène.usdc")
    export.write_bytes(b"export input")

    with pytest.raises(RuntimeError, match="collides with an input"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
        )

    assert export.read_bytes() == b"export input"


def test_bundle_output_cannot_create_a_directory_at_a_missing_input(tmp_path: Path):
    missing_blend = tmp_path / "Scene.blend"
    bundle = missing_blend / "support.zip"

    with pytest.raises(RuntimeError, match="collides with an input"):
        create_support_bundle(
            blend_file=str(missing_blend),
            bundle_output=str(bundle),
        )

    assert not missing_blend.exists()


def test_bundle_failure_preserves_existing_output_and_removes_temp(
    tmp_path: Path,
    monkeypatch,
):
    export = tmp_path / "Scene.usda"
    bundle = tmp_path / "support.zip"
    export.write_bytes(b"export")
    bundle.write_bytes(b"existing bundle")

    def fail_collection(context=None):
        raise RuntimeError("collection failed")

    monkeypatch.setattr(support_bundle, "collect_environment", fail_collection)

    with pytest.raises(RuntimeError, match="collection failed"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
        )

    assert bundle.read_bytes() == b"existing bundle"
    assert not list(tmp_path.glob(".support.zip.*.tmp"))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the late swap replaces a file the bundle holds open, which Windows refuses",
)
def test_required_sidecar_is_pinned_before_late_symlink_swap(
    tmp_path: Path,
    monkeypatch,
):
    output_dir = tmp_path / "exports"
    export = output_dir / "Scene.usda"
    sidecar = output_dir / "textures" / "scene" / "base.png"
    secret = tmp_path / "outside-secret.txt"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"scene")
    sidecar.parent.mkdir(parents=True)
    sidecar.write_bytes(b"validated sidecar")
    secret.write_bytes(b"SECRET")
    _write_sidecar_manifest(export, ["textures/scene/base.png"])

    def swap_after_binding(context=None):
        sidecar.unlink()
        sidecar.symlink_to(secret)
        return {}

    monkeypatch.setattr(support_bundle, "collect_environment", swap_after_binding)
    monkeypatch.setattr(support_bundle, "collect_scene_snapshot", lambda context=None: {})
    monkeypatch.setattr(
        support_bundle,
        "collect_asset_dependency_snapshot",
        lambda context=None: {},
    )
    monkeypatch.setattr(support_bundle, "collect_validation_snapshot", lambda context=None: {})

    result = create_support_bundle(
        export_path=str(export),
        bundle_output=str(output_dir / "support.zip"),
        include_output=True,
    )

    with zipfile.ZipFile(result["support_bundle_path"]) as archive:
        member = next(
            name
            for name in archive.namelist()
            if name.endswith("output/textures/scene/base.png")
        )
        assert archive.read(member) == b"validated sidecar"
        assert b"SECRET" not in archive.read(member)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses to replace a file that is open; the swap this test stages cannot happen there",
)
def test_mkstemp_path_swap_cannot_truncate_input(
    tmp_path: Path,
    monkeypatch,
    lightweight_bundle_collection,
):
    export = tmp_path / "Scene.usda"
    bundle = tmp_path / "support.zip"
    export.write_bytes(b"ORIGINAL-EXPORT")
    bundle.write_bytes(b"existing bundle")
    real_mkstemp = support_bundle.tempfile.mkstemp

    def swapped_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        temporary_path = Path(name)
        temporary_path.unlink()
        temporary_path.symlink_to(export)
        return descriptor, name

    monkeypatch.setattr(support_bundle.tempfile, "mkstemp", swapped_mkstemp)

    with pytest.raises(RuntimeError, match="temporary file was replaced"):
        create_support_bundle(
            export_path=str(export),
            bundle_output=str(bundle),
        )

    assert export.read_bytes() == b"ORIGINAL-EXPORT"
    assert bundle.read_bytes() == b"existing bundle"


def test_bundle_success_atomically_replaces_existing_output(
    tmp_path: Path,
    lightweight_bundle_collection,
):
    export = tmp_path / "Scene.usda"
    bundle = tmp_path / "support.zip"
    export.write_bytes(b"export")
    bundle.write_bytes(b"existing bundle")

    result = create_support_bundle(
        export_path=str(export),
        bundle_output=str(bundle),
    )

    assert result["support_bundle_path"] == str(bundle)
    assert zipfile.is_zipfile(bundle)
    assert not list(tmp_path.glob(".support.zip.*.tmp"))
