"""The postprocess must not ship colour-space tokens RCP cannot interpret.

Measured against RealityComposerPro.app 80.0.1.500.1: the CoreRE engine's
alias table knows ``srgb_texture``, ``srgb_rec709_scene``, ``lin_rec709``,
``lin_rec709_scene``, ``raw``/``data``/``none`` — but ``srgb_rec709_display``,
the token Blender 5.2 authors through ColorSpaceAPI on every sRGB texture
prim, appears nowhere in the app. Its decode behaviour is undefined, so the
postprocess retags it to the engine-known equivalent encoding,
``srgb_texture``, before preflight runs.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdShade  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.postprocess_usd import (  # noqa: E402
    _retag_unmapped_color_space_names,
)


def _stage_with_api_color_space(token: str):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)

    texture = UsdShade.Shader.Define(stage, "/Root/Material/Image_Texture")
    texture.CreateIdAttr("UsdUVTexture")
    prim = texture.GetPrim()
    Usd.ColorSpaceAPI.Apply(prim).CreateColorSpaceNameAttr(token)
    return stage, prim


def _name_token(prim) -> str:
    return str(prim.GetAttribute("colorSpace:name").Get())


def test_display_token_is_retagged_to_a_name_usd_can_resolve():
    """`colorSpace:name` must hold a name UsdColorSpaceAPI accepts.

    `srgb_texture` is MaterialX's spelling and is fine as attribute metadata,
    but it is not in the registry `ComputeColorSpaceName` consults: measured on
    a real export, the prim logs "Unknown color space srgb_texture
    encountered." and resolves to the empty token, without inheriting the
    ancestor's opinion, so the texture reaches RealityKit with no colour space.
    """
    stage, prim = _stage_with_api_color_space("srgb_rec709_display")
    diagnostics = ExportDiagnostics()

    _retag_unmapped_color_space_names(stage, diagnostics)

    assert _name_token(prim) == "srgb_rec709_scene"
    assert Usd.ColorSpaceAPI.IsValidColorSpaceName(prim, _name_token(prim), None)


def test_engine_known_tokens_are_left_alone():
    for token in ("srgb_texture", "srgb_rec709_scene", "lin_rec709_scene", "data"):
        stage, prim = _stage_with_api_color_space(token)

        _retag_unmapped_color_space_names(stage, None)

        assert _name_token(prim) == token, token


def test_retag_is_reported_as_info_not_warning():
    """Every sRGB-textured Blender export hits this rewrite; it is expected
    normalization, not a problem the user must react to."""
    stage, _prim = _stage_with_api_color_space("srgb_rec709_display")
    diagnostics = ExportDiagnostics()

    _retag_unmapped_color_space_names(stage, diagnostics)

    warnings = diagnostics.data.get("warnings") or []
    assert not any("srgb_rec709_display" in w for w in warnings)
    infos = diagnostics.data.get("info") or []
    assert any("srgb_rec709_display" in note for note in infos)


def _mesh_with_color_attribute(stage, path, name, values, *, bind_geomcolor=True):
    from pxr import Gf, UsdGeom, UsdShade, Vt
    mesh = UsdGeom.Mesh.Define(stage, path)
    api = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    api.CreatePrimvar(
        name, Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.faceVarying
    ).Set(Vt.Vec4fArray([Gf.Vec4f(*v) for v in values]))
    material = UsdShade.Material.Define(stage, f"{path}_mat")
    if bind_geomcolor:
        reader = UsdShade.Shader.Define(stage, f"{path}_mat/VertexColor")
        reader.CreateIdAttr("ND_geomcolor_color4")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return mesh


def test_vertex_colors_are_published_as_display_color():
    """RealityKit's vertex-colour reader resolves colour set 0, which USD
    spells displayColor. Blender writes the data under the attribute's own
    name and leaves displayColor empty, so the read returns black."""
    from pxr import UsdGeom
    from Plugin.export.postprocess_usd import _publish_vertex_colors_as_display_color

    stage = Usd.Stage.CreateInMemory()
    stage.SetDefaultPrim(stage.DefinePrim("/Root", "Xform"))
    values = [(0.1, 0.2, 0.3, 1.0), (0.4, 0.5, 0.6, 0.5)]
    mesh = _mesh_with_color_attribute(stage, "/Root/Painted", "Paint", values)

    _publish_vertex_colors_as_display_color(stage, None)

    api = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    colors = api.GetPrimvar("displayColor").Get()
    assert [tuple(round(c, 3) for c in v) for v in colors] == [
        (0.1, 0.2, 0.3), (0.4, 0.5, 0.6),
    ]
    assert list(api.GetPrimvar("displayOpacity").Get()) == [1.0, 0.5]
    assert api.GetPrimvar("displayColor").GetInterpolation() == "faceVarying"


def test_meshes_that_do_not_read_vertex_colors_are_untouched():
    from pxr import UsdGeom
    from Plugin.export.postprocess_usd import _publish_vertex_colors_as_display_color

    stage = Usd.Stage.CreateInMemory()
    stage.SetDefaultPrim(stage.DefinePrim("/Root", "Xform"))
    mesh = _mesh_with_color_attribute(
        stage, "/Root/Plain", "Paint", [(1.0, 0.0, 0.0, 1.0)], bind_geomcolor=False
    )

    _publish_vertex_colors_as_display_color(stage, None)

    assert not UsdGeom.PrimvarsAPI(mesh.GetPrim()).GetPrimvar("displayColor").Get()
