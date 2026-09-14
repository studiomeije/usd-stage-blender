"""Integration test — usdstage validate."""

import pytest


pytestmark = pytest.mark.integration


class TestValidate:
    def test_has_ok_field(self, run_cli, blend_file):
        result = run_cli("validate", str(blend_file))
        assert "ok" in result.json
        assert isinstance(result.json["ok"], bool)

    def test_has_error_count(self, run_cli, blend_file):
        result = run_cli("validate", str(blend_file))
        assert "error_count" in result.json
        assert isinstance(result.json["error_count"], int)
        assert result.json["error_count"] >= 0

    def test_has_warning_count(self, run_cli, blend_file):
        result = run_cli("validate", str(blend_file))
        assert "warning_count" in result.json
        assert isinstance(result.json["warning_count"], int)
        assert result.json["warning_count"] >= 0

    def test_has_materials_list(self, run_cli, blend_file):
        result = run_cli("validate", str(blend_file))
        assert "materials" in result.json
        assert isinstance(result.json["materials"], list)
        assert len(result.json["materials"]) > 0, "RedCube should have at least one material"

    def test_material_filter(self, run_cli, blend_file):
        # First get a material name
        mats = run_cli("list-materials", str(blend_file))
        assert mats.ok, "list-materials failed"
        assert len(mats.json) > 0, "No materials in fixture"
        mat_name = mats.json[0]["name"]
        result = run_cli("validate", str(blend_file), "--material", mat_name)
        assert result.json is not None
        assert len(result.json["materials"]) == 1
        assert result.json["materials"][0]["name"] == mat_name

    def test_nonexistent_material(self, run_cli, blend_file):
        result = run_cli("validate", str(blend_file), "--material", "NONEXISTENT_MATERIAL_xyz")
        # Should either fail or return empty materials list
        if result.ok:
            assert len(result.json["materials"]) == 0

    def test_nonexistent_file(self, run_cli):
        result = run_cli("validate", "/nonexistent/file.blend")
        assert not result.ok
