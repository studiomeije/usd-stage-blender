"""Background-worker scene snapshot cleanup regression tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


RUNNER_PATH = Path(__file__).resolve().parents[2] / "Plugin" / "bake_export_runner.py"


def _load_runner(monkeypatch, loaded_blend_file: Path):
    fake_bpy = SimpleNamespace(data=SimpleNamespace(filepath=str(loaded_blend_file)))
    monkeypatch.setitem(__import__("sys").modules, "bpy", fake_bpy)
    spec = importlib.util.spec_from_file_location(
        "_usdstage_snapshot_cleanup_test_runner",
        RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_lock_defers_cleanup_without_failing_worker(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    snapshot = job_dir / "scene_snapshot.blend"
    snapshot.write_bytes(b"BLENDER")
    runner = _load_runner(monkeypatch, snapshot)

    original_unlink = Path.unlink

    def locked_unlink(path, *args, **kwargs):
        if path == snapshot:
            raise PermissionError("sharing violation")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    state = runner._consume_loaded_scene_snapshot(
        {"blend_file": str(snapshot), "source_blend_file": "/source/model.blend"},
        job_dir,
    )

    assert state == {
        "loaded": True,
        "removed": False,
        "cleanup_deferred": True,
        "cleanup_error": "PermissionError: sharing violation",
    }
    assert snapshot.is_file()


def test_snapshot_load_mismatch_fails_closed_and_preserves_snapshot(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    snapshot = job_dir / "scene_snapshot.blend"
    snapshot.write_bytes(b"BLENDER")
    runner = _load_runner(monkeypatch, tmp_path / "different.blend")

    try:
        runner._consume_loaded_scene_snapshot(
            {"blend_file": str(snapshot), "source_blend_file": "/source/model.blend"},
            job_dir,
        )
    except RuntimeError as exc:
        assert "did not load" in str(exc)
    else:
        raise AssertionError("snapshot mismatch did not fail closed")

    assert snapshot.is_file()


def test_worker_replays_settings_with_persistence_suspended(tmp_path, monkeypatch):
    runner = _load_runner(monkeypatch, tmp_path / "scene.blend")

    class Settings:
        def __init__(self):
            object.__setattr__(
                self,
                "bl_rna",
                SimpleNamespace(
                    properties=[
                        SimpleNamespace(identifier="persist_suspended"),
                        SimpleNamespace(identifier="export_format"),
                        SimpleNamespace(identifier="bake_mode"),
                    ]
                ),
            )
            object.__setattr__(self, "persist_suspended", False)
            object.__setattr__(self, "export_format", "USDA")
            object.__setattr__(self, "bake_mode", "LIT_IBL")
            object.__setattr__(self, "assignment_suspension", [])

        def __setattr__(self, key, value):
            if key in {"export_format", "bake_mode"}:
                self.assignment_suspension.append(self.persist_suspended)
            object.__setattr__(self, key, value)

    settings = Settings()
    runner._apply_settings(
        settings,
        {
            "export_format": "USDZ",
            "bake_mode": "UNLIT_ALBEDO",
            "unknown": "ignored",
        },
    )

    assert settings.export_format == "USDZ"
    assert settings.bake_mode == "UNLIT_ALBEDO"
    assert settings.assignment_suspension == [True, True]
    assert settings.persist_suspended is False
    assert not hasattr(settings, "unknown")
