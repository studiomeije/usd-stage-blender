"""Integration test — usdstage list-materials."""

import pytest


pytestmark = pytest.mark.integration


class TestListMaterials:
    def test_returns_list(self, run_cli, blend_file):
        result = run_cli("list-materials", str(blend_file))
        assert result.ok
        assert isinstance(result.json, list)

    def test_material_has_required_keys(self, run_cli, blend_file):
        result = run_cli("list-materials", str(blend_file))
        assert result.ok
        assert len(result.json) > 0
        for mat in result.json:
            assert "name" in mat, f"Material missing 'name': {mat}"
            assert isinstance(mat["name"], str)
            assert "users" in mat, f"Material missing 'users': {mat}"
            assert isinstance(mat["users"], int)

    def test_nonexistent_file(self, run_cli):
        result = run_cli("list-materials", "/nonexistent/file.blend")
        assert not result.ok
