"""Focused image-staging cache and lifecycle regressions."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export.materials.extract import core
from Plugin.export import postprocess_usd


class _Image:
    def __init__(self, filepath: Path, *, dirty: bool = False):
        self.name = "SessionImage"
        self.filepath = str(filepath)
        self.filepath_raw = str(filepath)
        self.source = "FILE"
        self.is_dirty = dirty
        self.packed_file = None
        self.file_format = "PNG"
        self.is_float = False
        self.size = (1, 1)
        self.library = None

    def as_pointer(self):
        return 4242


def test_reloaded_temp_file_invalidates_cache_within_one_export(tmp_path, monkeypatch):
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(tmp_path))
    source = tmp_path / "source.png"
    source.write_bytes(b"first-file-version")
    image = _Image(source)

    session_dir = core.begin_image_staging_session()
    try:
        first = Path(core._resolve_image_path(image))
        assert first.read_bytes() == b"first-file-version"

        source.write_bytes(b"second-current-file-version")
        os.utime(source, None)
        second = Path(core._resolve_image_path(image))

        assert second.read_bytes() == b"second-current-file-version"
        assert second == first
        assert session_dir in second.parents
    finally:
        assert core.cleanup_image_staging_session()

    assert not session_dir.exists()
    assert not core._STAGED_IMAGE_CACHE
    assert core._STAGED_IMAGE_DIR is None


def test_dirty_to_clean_transition_never_reuses_dirty_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(tmp_path))
    source = tmp_path / "source.png"
    source.write_bytes(b"old-disk-file")
    image = _Image(source, dirty=True)

    def save_current_pixels(_image, destination):
        destination.write_bytes(b"dirty-in-memory-pixels")
        return True

    monkeypatch.setattr(core, "_save_current_image_snapshot_to_path", save_current_pixels)
    session_dir = core.begin_image_staging_session()
    try:
        dirty_snapshot = Path(core._resolve_image_path(image))
        assert dirty_snapshot.read_bytes() == b"dirty-in-memory-pixels"

        source.write_bytes(b"reloaded-current-file")
        image.is_dirty = False
        clean_snapshot = Path(core._resolve_image_path(image))

        assert clean_snapshot.read_bytes() == b"reloaded-current-file"
        assert clean_snapshot == dirty_snapshot
    finally:
        assert core.cleanup_image_staging_session()

    assert not session_dir.exists()
    assert not core._STAGED_IMAGE_CACHE


def test_postprocess_export_boundary_cleans_session_on_failure(monkeypatch):
    events = []
    stage = object()
    monkeypatch.setattr(postprocess_usd, "require_pxr", lambda: None)
    monkeypatch.setattr(
        postprocess_usd,
        "Usd",
        SimpleNamespace(
            Stage=SimpleNamespace(Open=lambda *_args: stage, LoadAll=object()),
        ),
    )
    monkeypatch.setattr(
        postprocess_usd,
        "begin_image_staging_session",
        lambda *_args: events.append("begin"),
    )
    monkeypatch.setattr(
        postprocess_usd,
        "cleanup_image_staging_session",
        lambda *_args: events.append("cleanup"),
    )

    def fail_texture_stage(*_args):
        raise RuntimeError("texture staging failed")

    monkeypatch.setattr(postprocess_usd, "_prepare_assets", fail_texture_stage)

    with pytest.raises(RuntimeError, match="texture staging failed"):
        postprocess_usd.process_usd_stage(
            "/tmp/scene.usdc",
            SimpleNamespace(),
            SimpleNamespace(),
        )

    assert events == ["begin", "cleanup"]
