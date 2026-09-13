"""Integration test — usdstage info."""

import pytest


pytestmark = pytest.mark.integration


class TestSceneInfo:
    def test_has_object_count(self, run_cli, blend_file):
        result = run_cli("info", str(blend_file))
        assert result.ok
        assert "object_count" in result.json
        assert isinstance(result.json["object_count"], int)
        assert result.json["object_count"] > 0

    def test_has_material_count(self, run_cli, blend_file):
        result = run_cli("info", str(blend_file))
        assert result.ok
        assert "material_count" in result.json
        assert isinstance(result.json["material_count"], int)
        assert result.json["material_count"] > 0

    def test_has_scene_name(self, run_cli, blend_file):
        result = run_cli("info", str(blend_file))
        assert result.ok
        assert "scene" in result.json
        assert isinstance(result.json["scene"], str)
        assert len(result.json["scene"]) > 0

    def test_has_frame_range(self, run_cli, blend_file):
        result = run_cli("info", str(blend_file))
        assert result.ok
        assert "frame_range" in result.json

    def test_nonexistent_file(self, run_cli):
        result = run_cli("info", "/nonexistent/file.blend")
        assert not result.ok
