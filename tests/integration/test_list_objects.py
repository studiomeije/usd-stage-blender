"""Integration test — usdstage list-objects."""

import pytest


pytestmark = pytest.mark.integration


class TestListObjects:
    def test_returns_list(self, run_cli, blend_file):
        result = run_cli("list-objects", str(blend_file))
        assert result.ok
        assert isinstance(result.json, list)

    def test_has_mesh(self, run_cli, blend_file):
        result = run_cli("list-objects", str(blend_file))
        assert result.ok
        types = [obj["type"] for obj in result.json]
        assert "MESH" in types

    def test_type_filter_mesh(self, run_cli, blend_file):
        result = run_cli("list-objects", str(blend_file), "--type", "MESH")
        assert result.ok
        assert len(result.json) > 0, "MESH filter returned empty list — test would pass trivially"
        for obj in result.json:
            assert obj["type"] == "MESH"

    def test_type_filter_camera(self, run_cli, blend_file):
        result = run_cli("list-objects", str(blend_file), "--type", "CAMERA")
        assert result.ok
        # t22_red_cube.blend should have at least one camera
        assert len(result.json) > 0, "CAMERA filter returned empty list — test would pass trivially"
        for obj in result.json:
            assert obj["type"] == "CAMERA"

    def test_objects_have_name_and_type(self, run_cli, blend_file):
        result = run_cli("list-objects", str(blend_file))
        assert result.ok
        assert len(result.json) > 0
        for obj in result.json:
            assert "name" in obj
            assert isinstance(obj["name"], str)
            assert "type" in obj
            assert isinstance(obj["type"], str)

    def test_nonexistent_file(self, run_cli):
        result = run_cli("list-objects", "/nonexistent/file.blend")
        assert not result.ok
