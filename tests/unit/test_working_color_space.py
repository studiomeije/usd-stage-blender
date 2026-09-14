"""The blend file's working colour space must be the one the export tags.

Blender 5.2 stores scene-linear colours in the file's working space
(``bpy.data.colorspace``), one of Linear Rec.709 (interop id
``lin_rec709_scene``), Linear Rec.2020 (``lin_rec2020_scene``) or ACEScg
(``lin_ap1_scene``), as read from Blender 5.2.0 LTS. The export authors every
colour unconverted under ``lin_rec709_scene``, so any other working space is
refused, by the validator and by the material rewrite.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export.materials.extract import core  # noqa: E402
from Plugin.nodes import validate  # noqa: E402

WORKING_SPACES = {
    "Linear Rec.709": "lin_rec709_scene",
    "Linear Rec.2020": "lin_rec2020_scene",
    "ACEScg": "lin_ap1_scene",
}


def _blend_data(name, **extra):
    colorspace = SimpleNamespace(working_space=name, working_space_interop_id=WORKING_SPACES[name])
    return SimpleNamespace(colorspace=colorspace, **extra)


@pytest.mark.parametrize("name", sorted(WORKING_SPACES))
def test_only_linear_rec709_is_exported(name):
    refusal = core.working_color_space_refusal(_blend_data(name))
    if name == "Linear Rec.709":
        assert refusal is None
    else:
        assert f"'{name}'" in refusal and "Linear Rec.709" in refusal


def test_the_validator_refuses_a_material_in_another_working_space(monkeypatch):
    monkeypatch.setattr(_bpy_stub, "data", _blend_data("ACEScg"), raising=False)
    material = SimpleNamespace(name="Paint", node_tree=None)
    result = validate.validate_material(material, strict=True)
    assert result["ok"] is False
    assert any("'ACEScg'" in error["message"] for error in result["errors"])

    monkeypatch.setattr(_bpy_stub, "data", _blend_data("Linear Rec.709"), raising=False)
    assert validate.validate_material(material, strict=True)["ok"] is True


def test_the_material_rewrite_refuses_another_working_space():
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdGeom, UsdShade

    from Plugin.export.materials import rewrite as material_rewrite

    stage = Usd.Stage.CreateInMemory()
    stage.SetDefaultPrim(stage.DefinePrim("/Root", "Xform"))
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    looks = UsdShade.Material.Define(stage, "/Root/Looks/Paint")
    looks.GetPrim().CreateAttribute("userProperties:blender:data_name", Sdf.ValueTypeNames.String).Set("Paint")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(looks)
    before = stage.GetRootLayer().ExportToString()
    material = SimpleNamespace(name="Paint", node_tree=None, surface_render_method="DITHERED",
                               diffuse_color=(0.2, 0.3, 0.4, 1.0))
    context = SimpleNamespace(blend_data=_blend_data("Linear Rec.2020", materials=[material]))

    with pytest.raises(RuntimeError, match="'Linear Rec.2020'"):
        material_rewrite.rewrite_materials(stage, SimpleNamespace(force_unlit_materials=False), context, None)
    assert stage.GetRootLayer().ExportToString() == before
