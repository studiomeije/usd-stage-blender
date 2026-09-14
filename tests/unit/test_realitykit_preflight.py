"""Strict RealityKit Apple OS 27 stage-preflight tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.postprocess_usd import _require_realitykit_preflight  # noqa: E402
from Plugin.export import postprocess_usd, realitykit_preflight  # noqa: E402
from Plugin.export.materials.textures import (  # noqa: E402
    _create_texture_connection,
    _materialx_file_colorspace,
)
from Plugin.export.realitykit_preflight import validate_stage  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402


def _stage_with_mesh():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    uv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
    )
    uv.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)])
    return stage, mesh


def _codes(report):
    return {issue.code for issue in report.issues}


def _author_materialx_texture(
    stage,
    *,
    texture_name,
    texture_filename,
    consumer_name,
    consumer_type,
    output_type,
    file_color_space=None,
    api_color_space=None,
):
    """Author a real UsdShade/MaterialX texture network for preflight tests."""
    material = UsdShade.Material.Define(stage, "/Root/Material")
    material_prim = material.GetPrim()
    material_prim.AddAppliedSchema("MaterialXConfigAPI")
    Usd.ColorSpaceAPI.Apply(material_prim).CreateColorSpaceNameAttr(
        "lin_rec709_scene"
    )
    material_prim.CreateAttribute(
        "config:mtlx:version", Sdf.ValueTypeNames.String
    ).Set("1.39")

    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )

    texture = UsdShade.Shader.Define(stage, f"/Root/Material/{texture_name}")
    texture.CreateIdAttr(
        "ND_image_color3" if output_type == Sdf.ValueTypeNames.Color3f else "ND_image_float"
    )
    file_input = texture.CreateInput("file", Sdf.ValueTypeNames.Asset)
    file_input.Set(Sdf.AssetPath(texture_filename))
    if file_color_space:
        file_input.GetAttr().SetColorSpace(file_color_space)
    if api_color_space:
        Usd.ColorSpaceAPI.Apply(texture.GetPrim()).CreateColorSpaceNameAttr(
            api_color_space
        )

    output = texture.CreateOutput("out", output_type)
    surface.CreateInput(consumer_name, consumer_type).ConnectToSource(output)
    return texture, surface


def _author_exported_color_texture(
    stage,
    *,
    texture_filename,
    colorspace,
    diagnostics=None,
):
    """Author a base-color texture through the real exporter authoring path.

    Uses ``_create_texture_connection`` rather than a hand-written network so
    the color-space token under test is the one the exporter actually emits.
    """
    material = UsdShade.Material.Define(stage, "/Root/Material")
    material_prim = material.GetPrim()
    material_prim.AddAppliedSchema("MaterialXConfigAPI")
    Usd.ColorSpaceAPI.Apply(material_prim).CreateColorSpaceNameAttr(
        "lin_rec709_scene"
    )
    material_prim.CreateAttribute(
        "config:mtlx:version", Sdf.ValueTypeNames.String
    ).Set("1.39")

    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )

    texture_output = _create_texture_connection(
        stage,
        "/Root/Material",
        "base_color",
        {
            "path": texture_filename,
            "output_type": "color3",
            "type": "texture",
            "channel": "rgb",
            "colorspace_role": "color",
            "colorspace": colorspace,
        },
        load_manifest(),
        "Material",
        diagnostics=diagnostics,
    )
    if texture_output:
        surface.CreateInput(
            "base_color", Sdf.ValueTypeNames.Color3f
        ).ConnectToSource(texture_output)
    return texture_output


def _author_bound_place2d_material(
    stage,
    mesh,
    offsets,
    material_path="/Root/MappedMaterial",
):
    material = UsdShade.Material.Define(stage, material_path)
    surface = UsdShade.Shader.Define(stage, f"{material_path}/Surface")
    surface.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )
    texcoord = UsdShade.Shader.Define(
        stage, f"{material_path}/TextureCoordinates"
    )
    texcoord.CreateIdAttr("ND_texcoord_vector2")
    texcoord_output = texcoord.CreateOutput("out", Sdf.ValueTypeNames.Float2)

    transforms = []
    for index, offset in enumerate(offsets):
        transform = UsdShade.Shader.Define(
            stage, f"{material_path}/Transform{index}"
        )
        transform.CreateIdAttr("ND_place2d_vector2")
        transform.CreateInput("texcoord", Sdf.ValueTypeNames.Float2).ConnectToSource(
            texcoord_output
        )
        transform.CreateInput("offset", Sdf.ValueTypeNames.Float2).Set(offset)
        transform.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set((0.5, 0.5))
        transform.CreateInput("rotate", Sdf.ValueTypeNames.Float).Set(15.0)
        transform.CreateInput("pivot", Sdf.ValueTypeNames.Float2).Set((0.0, 0.0))
        transform.CreateInput("operationorder", Sdf.ValueTypeNames.Int).Set(0)
        transformed = transform.CreateOutput("out", Sdf.ValueTypeNames.Float2)

        texture = UsdShade.Shader.Define(
            stage, f"{material_path}/Texture{index}"
        )
        texture.CreateIdAttr("ND_image_color3")
        texture.CreateInput("texcoord", Sdf.ValueTypeNames.Float2).ConnectToSource(
            transformed
        )
        output = texture.CreateOutput("out", Sdf.ValueTypeNames.Color3f)
        surface.CreateInput(
            f"mappedColor{index}", Sdf.ValueTypeNames.Color3f
        ).ConnectToSource(output)
        transforms.append(transform)

    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return transforms


def test_composed_preflight_allows_identical_effective_place2d_contracts():
    stage, mesh = _stage_with_mesh()
    _author_bound_place2d_material(
        stage,
        mesh,
        ((0.1, 0.2), (0.1, 0.2)),
    )

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" not in _codes(report)


def test_composed_preflight_rejects_distinct_place2d_contracts_once_per_material():
    stage, mesh = _stage_with_mesh()
    _author_bound_place2d_material(
        stage,
        mesh,
        ((0.1, 0.2), (0.3, 0.2)),
    )

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.errors
        if issue.code == "MATERIAL_TEXTURE_TRANSFORM_CONFLICT"
    ]
    assert len(matching) == 1
    assert matching[0].prim_path == "/Root/MappedMaterial"
    assert matching[0].details["distinct_transform_count"] == 2
    assert len(matching[0].details["mappings"]) == 2


def test_composed_identity_transform_cannot_hide_nondefault_transform():
    stage, mesh = _stage_with_mesh()
    transforms = _author_bound_place2d_material(
        stage,
        mesh,
        ((0.0, 0.0), (0.3, 0.2)),
    )
    transforms[0].GetInput("scale").Set((1.0, 1.0))
    transforms[0].GetInput("rotate").Set(0.0)

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" in _codes(report)


def test_composed_identity_transforms_on_different_uv_sets_are_distinct():
    stage, mesh = _stage_with_mesh()
    transforms = _author_bound_place2d_material(
        stage,
        mesh,
        ((0.0, 0.0), (0.0, 0.0)),
    )
    for transform in transforms:
        transform.GetInput("scale").Set((1.0, 1.0))
        transform.GetInput("rotate").Set(0.0)

    uv1 = UsdShade.Shader.Define(stage, "/Root/MappedMaterial/UV1")
    uv1.CreateIdAttr("ND_geompropvalue_vector2")
    uv1.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set("UV1")
    uv1_output = uv1.CreateOutput("out", Sdf.ValueTypeNames.Float2)
    transforms[1].GetInput("texcoord").ConnectToSource(uv1_output)

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" in _codes(report)


def test_composed_usdtransform2d_network_is_covered_by_backstop():
    stage, mesh = _stage_with_mesh()
    transforms = _author_bound_place2d_material(
        stage,
        mesh,
        ((0.0, 0.0), (0.0, 0.0)),
    )
    texcoord_output = UsdShade.Shader(
        stage.GetPrimAtPath("/Root/MappedMaterial/TextureCoordinates")
    ).GetOutput("out")
    for index, transform in enumerate(transforms):
        transform.CreateIdAttr("UsdTransform2d").Set("UsdTransform2d")
        transform.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
            texcoord_output
        )
        transform.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set(
            (0.1 + index * 0.2, 0.2)
        )
        transform.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(0.0)
        transform.GetInput("scale").Set((1.0, 1.0))

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" in _codes(report)


def test_dynamic_texture_transform_input_fails_closed():
    stage, mesh = _stage_with_mesh()
    transforms = _author_bound_place2d_material(
        stage,
        mesh,
        ((0.1, 0.2),),
    )
    texcoord_output = UsdShade.Shader(
        stage.GetPrimAtPath("/Root/MappedMaterial/TextureCoordinates")
    ).GetOutput("out")
    transforms[0].GetInput("offset").ConnectToSource(texcoord_output)

    report = validate_stage(stage)

    assert "TEXTURE_TRANSFORM_UNINSPECTABLE" in _codes(report)


def test_inactive_variant_texture_transform_conflict_is_validated():
    stage, mesh = _stage_with_mesh()
    transforms = _author_bound_place2d_material(
        stage,
        mesh,
        ((0.1, 0.2), (0.1, 0.2)),
    )
    # A local base opinion is stronger than its enclosing variant opinions;
    # clear it so each branch is the authoritative transform contract.
    transforms[1].GetInput("offset").GetAttr().Clear()
    look = stage.GetPrimAtPath("/Root").GetVariantSets().AddVariantSet("mapping")
    for name, offset in (
        ("shared", (0.1, 0.2)),
        ("distinct", (0.4, 0.2)),
    ):
        look.AddVariant(name)
        look.SetVariantSelection(name)
        with look.GetVariantEditContext():
            transforms[1].GetInput("offset").Set(offset)
    look.SetVariantSelection("shared")

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.errors
        if issue.code == "MATERIAL_TEXTURE_TRANSFORM_CONFLICT"
    ]
    assert len(matching) == 1
    assert any(
        entry["variant_set"] == "mapping"
        and entry["selection"] == "distinct"
        for context in matching[0].details["variant_contexts"]
        for entry in context
    )
    assert look.GetVariantSelection() == "shared"


def test_instance_prototype_texture_transform_conflict_is_reported_once(tmp_path):
    asset_path = tmp_path / "mapped-prototype.usda"
    asset_stage = Usd.Stage.CreateNew(str(asset_path))
    asset_root = asset_stage.DefinePrim("/Asset", "Xform")
    asset_stage.SetDefaultPrim(asset_root)
    prototype_mesh = UsdGeom.Mesh.Define(asset_stage, "/Asset/Mesh")
    _author_bound_place2d_material(
        asset_stage,
        prototype_mesh,
        ((0.1, 0.2), (0.4, 0.2)),
        material_path="/Asset/Material",
    )
    asset_stage.GetRootLayer().Save()

    stage, _mesh = _stage_with_mesh()
    for instance_name in ("InstanceA", "InstanceB"):
        instance = stage.DefinePrim(f"/Root/{instance_name}", "Xform")
        instance.GetReferences().AddReference(str(asset_path), "/Asset")
        instance.SetInstanceable(True)

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.errors
        if issue.code == "MATERIAL_TEXTURE_TRANSFORM_CONFLICT"
    ]
    assert len(matching) == 1
    assert matching[0].prim_path.startswith("/__Prototype_")


def test_valid_static_mesh_has_no_preflight_errors():
    stage, _mesh = _stage_with_mesh()

    report = validate_stage(
        stage, settings=SimpleNamespace(export_format="USDC")
    )

    assert report.ok
    assert not report.errors
    assert "LIGHTMAP_UV_MISSING" in _codes(report)
    assert "ACCESSIBILITY_METADATA_MISSING" in _codes(report)


def test_stage_metadata_errors_are_structured_and_recorded_in_diagnostics():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    diagnostics = ExportDiagnostics()

    report = validate_stage(stage, diagnostics=diagnostics)

    assert {"UP_AXIS_NOT_Y", "METERS_PER_UNIT_UNAUTHORED"} <= _codes(report)
    assert diagnostics.data["realitykit_preflight"]["ok"] is False
    assert diagnostics.data["validation"]["realitykit"]["counts"]["errors"] == 2
    assert any("[UP_AXIS_NOT_Y]" in message for message in diagnostics.data["errors"])


def test_more_than_two_uv_sets_is_an_error_and_uv2_is_reported():
    stage, mesh = _stage_with_mesh()
    primvars = UsdGeom.PrimvarsAPI(mesh)
    for name in ("st1", "st2"):
        uv = primvars.CreatePrimvar(
            name, Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
        )
        uv.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)])

    report = validate_stage(stage)

    assert "TOO_MANY_UV_SETS" in _codes(report)
    assert "LIGHTMAP_UV_PRESENT" in _codes(report)


def test_skinned_mesh_requires_binding_api_and_one_skeleton_target():
    stage, mesh = _stage_with_mesh()
    prim = mesh.GetPrim()
    prim.CreateAttribute(
        "primvars:skel:jointIndices", Sdf.ValueTypeNames.IntArray
    ).Set([0, 0, 0])
    prim.CreateAttribute(
        "primvars:skel:jointWeights", Sdf.ValueTypeNames.FloatArray
    ).Set([1.0, 1.0, 1.0])

    report = validate_stage(stage)

    assert "SKEL_BINDING_API_MISSING" in _codes(report)
    assert "SKELETON_BINDING_INVALID" in _codes(report)


def test_usdz_texture_format_and_linear_color_role_are_enforced(tmp_path):
    stage, _mesh = _stage_with_mesh()
    texture_path = tmp_path / "roughness.tga"
    texture_path.write_bytes(b"texture")

    material = UsdShade.Material.Define(stage, "/Root/Material")
    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )

    texture = UsdShade.Shader.Define(stage, "/Root/Material/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("roughness.tga")
    )
    texture.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
    output = texture.CreateOutput("r", Sdf.ValueTypeNames.Float)
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(output)

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDZ"),
    )

    assert "USDZ_TEXTURE_FORMAT_UNSUPPORTED" in _codes(report)
    assert "TEXTURE_COLOR_SPACE_MISMATCH" in _codes(report)
    assert "TEXTURE_ASSET_MISSING" not in _codes(report)


def test_materialx_linear_rec709_is_valid_for_perceptual_color(tmp_path):
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "base-color.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="LinearColor",
        texture_filename="base-color.png",
        consumer_name="base_color",
        consumer_type=Sdf.ValueTypeNames.Color3f,
        output_type=Sdf.ValueTypeNames.Color3f,
        file_color_space="lin_rec709",
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert report.ok
    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)


def test_non_color_image_on_base_color_survives_the_exporters_own_preflight(tmp_path):
    """A Non-Color albedo must not be authored into a token preflight rejects.

    Blender reads a Non-Color image with no transfer function, so a perceptual
    color input consumes its texels as scene-linear values. Authoring ``raw``
    made the exporter fail its own postprocess gate on a material the strict
    validator had already accepted.
    """

    stage, _mesh = _stage_with_mesh()
    (tmp_path / "albedo.exr").write_bytes(b"texture")
    surface = _author_exported_color_texture(
        stage,
        texture_filename="albedo.exr",
        colorspace="Non-Color",
    )
    assert surface is not None

    authored = stage.GetRootLayer().ExportToString()
    assert 'colorSpace = "lin_rec709"' in authored
    assert 'colorSpace = "raw"' not in authored

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)
    assert report.ok
    _require_realitykit_preflight(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
        ExportDiagnostics(),
    )


def test_non_color_translation_is_reported_and_confined_to_color_inputs(tmp_path):
    """Only perceptual color inputs are relabelled, and never silently."""

    stage, _mesh = _stage_with_mesh()
    diagnostics = ExportDiagnostics()
    _author_exported_color_texture(
        stage,
        texture_filename="albedo.exr",
        colorspace="Non-Color",
        diagnostics=diagnostics,
    )
    assert any(
        "lin_rec709" in warning for warning in diagnostics.data["warnings"]
    )

    # Raw and working-space data author no color space — an absent MaterialX
    # color space is the no-transform contract — and an sRGB-tagged data
    # texture is decoded, as Cycles decodes it (validation warns about it).
    for colorspace in ("Non-Color", "Linear Rec.709"):
        assert (
            _materialx_file_colorspace(
                {"colorspace_role": "data", "colorspace": colorspace}, "roughness"
            )
            == ""
        )
    assert (
        _materialx_file_colorspace(
            {"colorspace_role": "data", "colorspace": "sRGB"}, "roughness"
        )
        == "srgb_texture"
    )
    # An sRGB base color whose alpha drives opacity is not a tagging mistake:
    # the file's RGB really is sRGB. It must not fail the export, and it must
    # not carry a color space of its own either.
    assert (
        _materialx_file_colorspace(
            {"colorspace_role": "data", "colorspace": "sRGB", "channel": "a"},
            "opacity",
        )
        == ""
    )


def test_blender_52_colorspace_api_opinion_is_resolved(tmp_path):
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "base-color.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="BlenderNativeColor",
        texture_filename="base-color.png",
        consumer_name="base_color",
        consumer_type=Sdf.ValueTypeNames.Color3f,
        output_type=Sdf.ValueTypeNames.Color3f,
        api_color_space="srgb_rec709_scene",
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert report.ok
    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)


def test_unmapped_ocio_display_token_flags(tmp_path):
    """``srgb_rec709_display`` appears nowhere in RCP 80.0.1.500.1 — the
    engine's alias table has no mapping for it, so its decode behaviour is
    undefined. The postprocess retags it to ``srgb_texture`` before preflight
    runs; a stage still carrying it means that rewrite regressed."""
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "base-color.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="BlenderNativeColor",
        texture_filename="base-color.png",
        consumer_name="base_color",
        consumer_type=Sdf.ValueTypeNames.Color3f,
        output_type=Sdf.ValueTypeNames.Color3f,
        api_color_space="srgb_rec709_display",
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_SPACE_MISMATCH" in _codes(report)


def test_linear_rec709_is_valid_for_scalar_data(tmp_path):
    """The working space applies no transform, so data reads unchanged."""
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "roughness.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="LinearRoughness",
        texture_filename="roughness.png",
        consumer_name="roughness",
        consumer_type=Sdf.ValueTypeNames.Float,
        output_type=Sdf.ValueTypeNames.Float,
        file_color_space="lin_rec709",
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert report.ok
    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)


@pytest.mark.parametrize(
    ("file_color_space", "api_color_space"),
    [("raw", None), (None, "data")],
)
def test_raw_and_blender_data_spaces_are_valid_for_scalar_data(
    tmp_path, file_color_space, api_color_space
):
    """Both tokens still satisfy the *data* contract.

    ``raw`` on a MaterialX reader is separately rejected as a token RealityKit
    cannot map (see ``test_raw_color_space_token_on_a_materialx_reader_is_rejected``);
    it must not also be reported as a color-space mismatch, because the value
    it names is the right one.
    """
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "roughness.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="RawRoughness",
        texture_filename="roughness.png",
        consumer_name="roughness",
        consumer_type=Sdf.ValueTypeNames.Float,
        output_type=Sdf.ValueTypeNames.Float,
        file_color_space=file_color_space,
        api_color_space=api_color_space,
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)
    if file_color_space == "raw":
        assert "TEXTURE_COLOR_SPACE_UNSUPPORTED_TOKEN" in _codes(report)
    else:
        assert report.ok


def test_one_materialx_reader_may_feed_color_and_data(tmp_path):
    """Cycles decodes the image once and every consumer reads that result."""
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "packed.png").write_bytes(b"texture")
    texture, surface = _author_materialx_texture(
        stage,
        texture_name="PackedColorAndData",
        texture_filename="packed.png",
        consumer_name="base_color",
        consumer_type=Sdf.ValueTypeNames.Color3f,
        output_type=Sdf.ValueTypeNames.Color3f,
        file_color_space="lin_rec709",
    )
    data_output = texture.CreateOutput("roughness", Sdf.ValueTypeNames.Float)
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
        data_output
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_ROLES_CONFLICT" not in _codes(report)
    assert report.ok


def test_camera_is_rejected_by_portable_realitykit_profile():
    stage, _mesh = _stage_with_mesh()
    UsdGeom.Camera.Define(stage, "/Root/Camera")

    report = validate_stage(stage)

    assert not report.ok
    assert "UNSUPPORTED_REALITYKIT_PRIM_TYPE" in _codes(report)


def test_double_sided_mesh_is_a_portability_error():
    stage, mesh = _stage_with_mesh()
    mesh.CreateDoubleSidedAttr(True)

    report = validate_stage(stage)

    matching = [
        issue for issue in report.errors if issue.code == "DOUBLE_SIDED_GEOMETRY"
    ]
    assert len(matching) == 1


def test_shared_postprocess_normalizes_blender_authored_open_mesh_and_warns(
    tmp_path, monkeypatch
):
    """Blender's default true opinion is an owned authoring default, not fatal."""
    root_path = tmp_path / "blender-open-mesh.usda"
    stage = Usd.Stage.CreateNew(str(root_path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/OpenTriangle")
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    mesh.CreateDoubleSidedAttr(True)
    stage.Save()

    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )
    diagnostics = ExportDiagnostics()
    postprocess_usd.process_usd_stage(
        str(root_path),
        SimpleNamespace(
            root_prim_name="Root",
            convert_orientation=False,
            allow_unicode=True,
            export_format="USDA",
            export_animation=False,
        ),
        context=None,
        diagnostics=diagnostics,
    )

    reopened = Usd.Stage.Open(str(root_path), Usd.Stage.LoadAll)
    assert (
        UsdGeom.Mesh(reopened.GetPrimAtPath("/Root/OpenTriangle"))
        .GetDoubleSidedAttr()
        .Get()
        is False
    )
    assert diagnostics.data["realitykit_preflight"]["ok"] is True
    warnings = [
        warning
        for warning in diagnostics.data.get("info", [])
        if "doubleSided=false" in warning
    ]
    assert len(warnings) == 1
    assert "closed or thick geometry is required" in warnings[0]


def test_shared_gate_rejects_surviving_unowned_double_sided_mesh():
    stage, mesh = _stage_with_mesh()
    mesh.CreateDoubleSidedAttr(True)

    with pytest.raises(RuntimeError, match="DOUBLE_SIDED_GEOMETRY"):
        _require_realitykit_preflight(
            stage,
            "scene.usdc",
            SimpleNamespace(export_format="USDC"),
        )

    assert mesh.GetDoubleSidedAttr().Get() is True


def test_inactive_nested_variant_is_validated_and_source_selection_is_unchanged():
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    product = root.GetVariantSets().AddVariantSet("product")

    for product_name in ("basic", "configurable"):
        product.AddVariant(product_name)
        product.SetVariantSelection(product_name)
        with product.GetVariantEditContext():
            if product_name != "configurable":
                continue
            branch = stage.DefinePrim("/Root/Branch", "Xform")
            detail = branch.GetVariantSets().AddVariantSet("detail")
            for detail_name in ("good", "bad"):
                detail.AddVariant(detail_name)
                detail.SetVariantSelection(detail_name)
                with detail.GetVariantEditContext():
                    if detail_name == "bad":
                        UsdGeom.PointInstancer.Define(
                            stage, "/Root/Branch/HiddenInstancer"
                        )
            detail.SetVariantSelection("good")

    product.SetVariantSelection("basic")
    root_layer_before = stage.GetRootLayer().ExportToString()
    session_layer_before = stage.GetSessionLayer().ExportToString()

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.issues
        if issue.code == "UNSUPPORTED_REALITYKIT_PRIM_TYPE"
        and issue.prim_path == "/Root/Branch/HiddenInstancer"
    ]
    assert len(matching) == 1
    contexts = matching[0].details["variant_contexts"]
    assert any(
        {
            entry["prim_path"]: (
                entry["variant_set"],
                entry["selection"],
            )
            for entry in context
        }
        == {
            "/Root": ("product", "configurable"),
            "/Root/Branch": ("detail", "bad"),
        }
        for context in contexts
    )
    assert "/Root{product=configurable}" in matching[0].format()
    assert "/Root/Branch{detail=bad}" in matching[0].format()
    assert product.GetVariantSelection() == "basic"
    assert stage.GetRootLayer().ExportToString() == root_layer_before
    assert stage.GetSessionLayer().ExportToString() == session_layer_before


def test_variant_findings_are_deduplicated_with_all_exposing_contexts():
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    content = root.GetVariantSets().AddVariantSet("content")
    for variant_name in ("bad_a", "bad_b"):
        content.AddVariant(variant_name)
        content.SetVariantSelection(variant_name)
        with content.GetVariantEditContext():
            UsdGeom.PointInstancer.Define(stage, "/Root/HiddenInstancer")
    content.SetVariantSelection("bad_a")

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.issues
        if issue.code == "UNSUPPORTED_REALITYKIT_PRIM_TYPE"
        and issue.prim_path == "/Root/HiddenInstancer"
    ]
    assert len(matching) == 1
    exposed_selections = {
        entry["selection"]
        for context in matching[0].details["variant_contexts"]
        for entry in context
        if entry["variant_set"] == "content"
    }
    assert exposed_selections == {"bad_a", "bad_b"}


def test_same_prim_variant_sets_cover_the_full_cartesian_product(monkeypatch):
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    first = root.GetVariantSets().AddVariantSet("first")
    second = root.GetVariantSets().AddVariantSet("second")
    for variant_set in (first, second):
        variant_set.AddVariant("0")
        variant_set.AddVariant("1")
        variant_set.SetVariantSelection("0")

    observed: set[tuple[str, str]] = set()
    original_check = realitykit_preflight._check_prim_types

    def record_combinations(prims, report):
        composed_root = next(
            prim for prim in prims if str(prim.GetPath()) == "/Root"
        )
        sets = composed_root.GetVariantSets()
        combination = (
            str(sets.GetVariantSet("first").GetVariantSelection()),
            str(sets.GetVariantSet("second").GetVariantSelection()),
        )
        observed.add(combination)
        if combination == ("1", "1"):
            report.add(
                "error",
                "SYNTHETIC_COMBINATION_DEFECT",
                "Only the combined variant state exposes this defect.",
                composed_root.GetPath(),
            )
        original_check(prims, report)

    monkeypatch.setattr(
        realitykit_preflight,
        "_check_prim_types",
        record_combinations,
    )

    report = validate_stage(stage)

    assert observed == {("0", "0"), ("0", "1"), ("1", "0"), ("1", "1")}
    assert "SYNTHETIC_COMBINATION_DEFECT" in _codes(report)


def test_nested_external_prototype_variant_fails_closed_instead_of_raising(
    tmp_path,
):
    asset_path = tmp_path / "nested-variant.usda"
    asset_stage = Usd.Stage.CreateNew(str(asset_path))
    asset_root = asset_stage.DefinePrim("/Asset", "Xform")
    asset_stage.SetDefaultPrim(asset_root)
    child = asset_stage.DefinePrim("/Asset/Child", "Xform")
    content = child.GetVariantSets().AddVariantSet("content")
    for variant_name in ("good", "bad"):
        content.AddVariant(variant_name)
        content.SetVariantSelection(variant_name)
        with content.GetVariantEditContext():
            if variant_name == "bad":
                UsdGeom.PointInstancer.Define(
                    asset_stage, "/Asset/Child/HiddenInstancer"
                )
    content.SetVariantSelection("good")
    asset_stage.GetRootLayer().Save()

    stage, _mesh = _stage_with_mesh()
    for instance_name in ("InstanceA", "InstanceB"):
        instance = stage.DefinePrim(f"/Root/{instance_name}", "Xform")
        instance.GetReferences().AddReference(str(asset_path), "/Asset")
        instance.SetInstanceable(True)

    report = validate_stage(stage)

    matching = [
        issue for issue in report.errors if issue.code == "VARIANT_SET_UNINSPECTABLE"
    ]
    assert len(matching) == 1
    assert matching[0].prim_path == "/Root/InstanceA/Child"
    assert matching[0].details["variant_set"] == "content"
    assert matching[0].details["variant_names"] == ["bad", "good"]


def test_successful_variant_validation_leaves_source_layers_unchanged():
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    content = root.GetVariantSets().AddVariantSet("content")
    for variant_name in ("a", "b"):
        content.AddVariant(variant_name)
    content.SetVariantSelection("a")
    root_layer_before = stage.GetRootLayer().ExportToString()
    session_layer_before = stage.GetSessionLayer().ExportToString()
    edit_target_before = stage.GetEditTarget().GetLayer()

    report = validate_stage(stage)

    assert report.ok
    assert content.GetVariantSelection() == "a"
    assert stage.GetEditTarget().GetLayer() is edit_target_before
    assert stage.GetRootLayer().ExportToString() == root_layer_before
    assert stage.GetSessionLayer().ExportToString() == session_layer_before


def test_variant_validation_restores_source_when_a_composed_check_raises(monkeypatch):
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    content = root.GetVariantSets().AddVariantSet("content")
    for variant_name in ("good", "bad"):
        content.AddVariant(variant_name)
        content.SetVariantSelection(variant_name)
        with content.GetVariantEditContext():
            if variant_name == "bad":
                UsdGeom.PointInstancer.Define(stage, "/Root/HiddenInstancer")
    content.SetVariantSelection("good")
    root_layer_before = stage.GetRootLayer().ExportToString()
    session_layer_before = stage.GetSessionLayer().ExportToString()
    edit_target_before = stage.GetEditTarget().GetLayer()
    original_check = realitykit_preflight._check_prim_types

    def raise_for_inactive_bad_variant(prims, report):
        if any(str(prim.GetTypeName()) == "PointInstancer" for prim in prims):
            raise RuntimeError("synthetic composed-check failure")
        original_check(prims, report)

    monkeypatch.setattr(
        realitykit_preflight,
        "_check_prim_types",
        raise_for_inactive_bad_variant,
    )

    with pytest.raises(RuntimeError, match="synthetic composed-check failure"):
        validate_stage(stage)

    assert content.GetVariantSelection() == "good"
    assert stage.GetEditTarget().GetLayer() is edit_target_before
    assert stage.GetRootLayer().ExportToString() == root_layer_before
    assert stage.GetSessionLayer().ExportToString() == session_layer_before


def test_variant_cartesian_limit_fails_closed_without_mutating_source():
    stage, _mesh = _stage_with_mesh()
    root = stage.GetPrimAtPath("/Root")
    content = root.GetVariantSets().AddVariantSet("content")
    for index in range(realitykit_preflight.MAX_VARIANT_COMBINATIONS + 1):
        content.AddVariant(f"choice_{index:03d}")
    content.SetVariantSelection("choice_000")
    root_layer_before = stage.GetRootLayer().ExportToString()
    session_layer_before = stage.GetSessionLayer().ExportToString()
    edit_target_before = stage.GetEditTarget().GetLayer()

    report = validate_stage(stage)

    matching = [
        issue for issue in report.errors if issue.code == "VARIANT_VALIDATION_LIMIT"
    ]
    assert len(matching) == 1
    assert matching[0].details == {
        "limit": realitykit_preflight.MAX_VARIANT_COMBINATIONS,
        "discovered_combinations": realitykit_preflight.MAX_VARIANT_COMBINATIONS
        + 1,
    }
    assert content.GetVariantSelection() == "choice_000"
    assert stage.GetEditTarget().GetLayer() is edit_target_before
    assert stage.GetRootLayer().ExportToString() == root_layer_before
    assert stage.GetSessionLayer().ExportToString() == session_layer_before


def test_instance_prototype_is_validated_once_for_shared_instances(tmp_path):
    asset_path = tmp_path / "prototype.usda"
    asset_stage = Usd.Stage.CreateNew(str(asset_path))
    asset_root = asset_stage.DefinePrim("/Prototype", "Xform")
    asset_stage.SetDefaultPrim(asset_root)

    # Structural finding that an ordinary Stage.Traverse() cannot see below an
    # instance root.
    UsdGeom.BasisCurves.Define(asset_stage, "/Prototype/UnsupportedCurves")

    mesh = UsdGeom.Mesh.Define(asset_stage, "/Prototype/Mesh")
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)

    material = UsdShade.Material.Define(asset_stage, "/Prototype/Material")
    surface = UsdShade.Shader.Define(asset_stage, "/Prototype/Material/Surface")
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )
    texture = UsdShade.Shader.Define(asset_stage, "/Prototype/Material/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("missing-roughness.tga")
    )
    texture.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
    roughness = texture.CreateOutput("r", Sdf.ValueTypeNames.Float)
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
        roughness
    )

    # Author the relationship without applying MaterialBindingAPI so the
    # material-binding contract supplies a separate prototype-only finding.
    mesh.GetPrim().CreateRelationship("material:binding").SetTargets(
        [material.GetPath()]
    )
    asset_stage.GetRootLayer().Save()

    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    for instance_name in ("InstanceA", "InstanceB"):
        instance = stage.DefinePrim(f"/Root/{instance_name}", "Xform")
        instance.GetReferences().AddReference(str(asset_path), "/Prototype")
        instance.SetInstanceable(True)

    # The public namespace exposes only the two instance roots; their shared
    # descendants exist solely in one prototype.
    assert not any(
        "UnsupportedCurves" in str(prim.GetPath()) for prim in stage.Traverse()
    )
    assert len(stage.GetPrototypes()) == 1

    report = validate_stage(
        stage,
        tmp_path / "scene.usdz",
        SimpleNamespace(export_format="USDZ"),
    )

    expected = {
        "UNSUPPORTED_REALITYKIT_PRIM_TYPE",
        "MATERIAL_BINDING_API_MISSING",
        "USDZ_TEXTURE_FORMAT_UNSUPPORTED",
        "TEXTURE_ASSET_MISSING",
        "TEXTURE_COLOR_SPACE_MISMATCH",
    }
    for code in expected:
        matching = [issue for issue in report.issues if issue.code == code]
        assert len(matching) == 1, (code, [issue.to_dict() for issue in matching])
        assert matching[0].prim_path.startswith("/__Prototype_")


def test_blender_class_prototype_contract_is_validated_once(tmp_path):
    stage, _ordinary_mesh = _stage_with_mesh()
    stage.CreateClassPrim("/Root/prototypes")
    prototype = stage.DefinePrim("/Root/prototypes/SharedAsset", "Xform")
    UsdGeom.BasisCurves.Define(
        stage, "/Root/prototypes/SharedAsset/UnsupportedCurves"
    )

    mesh = UsdGeom.Mesh.Define(stage, "/Root/prototypes/SharedAsset/Mesh")
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    primvars = UsdGeom.PrimvarsAPI(mesh)
    for uv_name in ("st", "st1", "st2"):
        uv = primvars.CreatePrimvar(
            uv_name,
            Sdf.ValueTypeNames.TexCoord2fArray,
            UsdGeom.Tokens.faceVarying,
        )
        uv.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)])

    material = UsdShade.Material.Define(
        stage, "/Root/prototypes/SharedAsset/Material"
    )
    surface = UsdShade.Shader.Define(
        stage, "/Root/prototypes/SharedAsset/Material/Surface"
    )
    surface.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )
    texture = UsdShade.Shader.Define(
        stage, "/Root/prototypes/SharedAsset/Material/Texture"
    )
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("missing-class-roughness.tga")
    )
    texture.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB")
    roughness = texture.CreateOutput("r", Sdf.ValueTypeNames.Float)
    surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
        roughness
    )
    mesh.GetPrim().CreateRelationship("material:binding").SetTargets(
        [material.GetPath()]
    )

    for instance_name in ("ClassInstanceA", "ClassInstanceB"):
        instance = stage.DefinePrim(f"/Root/{instance_name}", "Xform")
        instance.GetReferences().AddInternalReference(prototype.GetPath())
        instance.SetInstanceable(True)

    # Inactive/unused class definitions are not part of the renderable asset
    # and must not create a false positive merely because TraverseAll sees them.
    stage.CreateClassPrim("/Root/unusedPrototypes")
    unused_points = UsdGeom.Points.Define(stage, "/Root/unusedPrototypes/Points")
    unused_points.GetPrim().SetActive(False)

    assert not any(
        "/prototypes/" in str(prim.GetPath()) for prim in stage.Traverse()
    )
    assert any(
        "/prototypes/" in str(prim.GetPath()) for prim in stage.TraverseAll()
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdz",
        SimpleNamespace(export_format="USDZ"),
    )

    expected = {
        "UNSUPPORTED_REALITYKIT_PRIM_TYPE",
        "TOO_MANY_UV_SETS",
        "MATERIAL_BINDING_API_MISSING",
        "USDZ_TEXTURE_FORMAT_UNSUPPORTED",
        "TEXTURE_ASSET_MISSING",
        "TEXTURE_COLOR_SPACE_MISMATCH",
    }
    for code in expected:
        matching = [issue for issue in report.issues if issue.code == code]
        assert len(matching) == 1, (code, [issue.to_dict() for issue in matching])
        assert matching[0].prim_path.startswith("/Root/prototypes/SharedAsset")


@pytest.mark.parametrize(
    "define_unsupported_prim",
    [
        lambda stage: UsdGeom.BasisCurves.Define(stage, "/Root/HairCurves"),
        lambda stage: UsdGeom.Points.Define(stage, "/Root/PointCloud"),
        lambda stage: stage.DefinePrim("/Root/Fog", "Volume"),
        lambda stage: stage.DefinePrim("/Root/FogField", "OpenVDBAsset"),
        lambda stage: stage.DefinePrim("/Root/KeyLight", "SphereLight"),
        lambda stage: stage.DefinePrim("/Root/WorldLight", "DomeLight"),
    ],
)
def test_raw_geometry_and_lights_remain_rejected(define_unsupported_prim):
    stage, _mesh = _stage_with_mesh()
    define_unsupported_prim(stage)

    report = validate_stage(stage)

    assert "UNSUPPORTED_REALITYKIT_PRIM_TYPE" in _codes(report)


def test_shared_postprocess_gate_fails_before_packaging_on_strict_findings():
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    diagnostics = ExportDiagnostics()

    with pytest.raises(RuntimeError, match="RealityKit OS 27 preflight failed"):
        _require_realitykit_preflight(
            stage,
            "scene.usdc",
            SimpleNamespace(export_format="USDC"),
            diagnostics,
        )

    assert diagnostics.data["realitykit_preflight"]["ok"] is False


def test_shared_postprocess_gate_returns_report_for_valid_stage():
    stage, _mesh = _stage_with_mesh()

    report = _require_realitykit_preflight(
        stage,
        "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert report.ok


@pytest.mark.parametrize(
    ("type_name", "message_fragment"),
    [
        ("ParticleField", "RealityKit runtime APIs"),
        ("TetMesh", "polygon surface mesh"),
    ],
)
def test_os27_schemas_without_realitykit_usd_import_are_rejected(
    type_name, message_fragment
):
    stage, _mesh = _stage_with_mesh()
    prim = stage.DefinePrim(f"/Root/{type_name}", type_name)

    report = validate_stage(stage)

    matching = [
        issue
        for issue in report.errors
        if issue.code == "UNSUPPORTED_REALITYKIT_PRIM_TYPE"
        and issue.prim_path == str(prim.GetPath())
    ]
    assert len(matching) == 1
    assert matching[0].details["prim_type"] == type_name
    assert message_fragment in matching[0].message


# ---------------------------------------------------------------------------
# A preview network must not conflict with the MaterialX network.
#
# A material can carry two networks: the MaterialX ShaderGraph RealityKit
# consumes, and a UsdPreviewSurface network. Our exports no longer keep one,
# but referenced layers can, and exports used to. One Blender Mapping node then appears
# twice - as a MaterialX place2d (texcoord UV0, reciprocal SRT scale) and as a
# UsdTransform2d (texcoord st, direct scale). Same transform, different
# conventions, never equal.
#
# Counting both networks together meant ANY non-identity Mapping node produced
# distinct_transform_count == 2 and failed the export. Measured: a cube with
# one texture and Mapping Scale 3 - validate() clean, export dead at preflight,
# and the message told the artist to "use one identical transform ... or bake"
# for a conflict the exporter created between its own two networks.
# ---------------------------------------------------------------------------


def _author_preview_transform_chain(stage, material_path, scale, name="Mapping"):
    """A Blender-shaped preview chain: UsdTransform2d -> UsdUVTexture."""
    material = UsdShade.Material.Get(stage, material_path)
    if not material:
        material = UsdShade.Material.Define(stage, material_path)

    preview = UsdShade.Shader.Define(stage, f"{material_path}/PreviewSurface")
    preview.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        preview.ConnectableAPI(), "surface"
    )

    primvar = UsdShade.Shader.Define(stage, f"{material_path}/uvmap")
    primvar.CreateIdAttr("UsdPrimvarReader_float2")
    primvar.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    primvar_out = primvar.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    transform = UsdShade.Shader.Define(stage, f"{material_path}/{name}")
    transform.CreateIdAttr("UsdTransform2d")
    transform.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(
        primvar_out
    )
    transform.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(scale)
    transform.CreateInput("translation", Sdf.ValueTypeNames.Float2).Set((0.0, 0.0))
    transform.CreateInput("rotation", Sdf.ValueTypeNames.Float).Set(0.0)
    transformed = transform.CreateOutput("result", Sdf.ValueTypeNames.Float2)

    texture = UsdShade.Shader.Define(stage, f"{material_path}/PreviewTexture{name}")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(transformed)
    rgb = texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
    preview.CreateInput(
        f"diffuseColor{name}", Sdf.ValueTypeNames.Color3f
    ).ConnectToSource(rgb)
    return transform


def test_retained_preview_transform_does_not_conflict_with_materialx():
    """One Blender Mapping node -> two representations -> no conflict."""
    stage, mesh = _stage_with_mesh()
    _author_bound_place2d_material(stage, mesh, offsets=[(0.25, 0.5)])
    # Blender's retained network describes the SAME mapping in its own
    # convention: st texcoord, direct (non-reciprocal) scale.
    _author_preview_transform_chain(stage, "/Root/MappedMaterial", scale=(2.0, 2.0))

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" not in _codes(report)


def test_two_distinct_materialx_transforms_still_conflict():
    """The fix must not blind the check to genuine MaterialX conflicts."""
    stage, mesh = _stage_with_mesh()
    _author_bound_place2d_material(stage, mesh, offsets=[(0.25, 0.5), (0.75, 0.1)])
    _author_preview_transform_chain(stage, "/Root/MappedMaterial", scale=(2.0, 2.0))

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" in _codes(report)


def test_hand_authored_usdtransform2d_only_is_still_judged():
    """With no MaterialX transform, the preview network is the effective one.

    That is the hand-authored-USD case this check was originally written for;
    excluding UsdTransform2d outright would silently stop validating it.
    """
    stage, mesh = _stage_with_mesh()
    material = UsdShade.Material.Define(stage, "/Root/PreviewOnly")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    _author_preview_transform_chain(
        stage, "/Root/PreviewOnly", scale=(2.0, 2.0), name="MappingA"
    )
    _author_preview_transform_chain(
        stage, "/Root/PreviewOnly", scale=(5.0, 5.0), name="MappingB"
    )

    report = validate_stage(stage)

    assert "MATERIAL_TEXTURE_TRANSFORM_CONFLICT" in _codes(report)


# ---------------------------------------------------------------------------
# The closing gate on nodedef validity.
#
# Measured before the gate: an RGB-to-BW -> Roughness graph exported ok: true
# while authoring ND_convert_color3_float and ND_convert_vector4_color3 -
# info:id values existing in no MaterialX library, fabricated by string
# formatting after the hardened selector correctly returned None. Nothing
# downstream ever loaded the manifest to check.
# ---------------------------------------------------------------------------


def test_unknown_materialx_nodedef_is_rejected():
    stage, mesh = _stage_with_mesh()
    material = UsdShade.Material.Define(stage, "/Root/Material")
    shader = UsdShade.Shader.Define(stage, "/Root/Material/Bogus")
    shader.CreateIdAttr("ND_convert_color3_float")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    report = validate_stage(stage)

    assert "UNKNOWN_MATERIALX_NODEDEF" in _codes(report)


def test_manifest_backed_nodedefs_are_accepted():
    stage, mesh = _stage_with_mesh()
    _author_bound_place2d_material(stage, mesh, offsets=[(0.25, 0.5)])

    report = validate_stage(stage)

    assert "UNKNOWN_MATERIALX_NODEDEF" not in _codes(report)


def test_editor_unresolvable_nodedefs_are_rejected():
    """ND_dielectric_bsdf is in the manifest (Apple's public bundle carries
    the pbrlib closures) but RCP's ShaderGraph editor has no definition for
    it — measured against the installed build 80.0.1.500.1, it ships only in
    the USD parsing libraries. Authoring it is as broken as a fabricated id
    and must not pass just because the manifest knows the name."""
    stage, mesh = _stage_with_mesh()
    material = UsdShade.Material.Define(stage, "/Root/Material")
    shader = UsdShade.Shader.Define(stage, "/Root/Material/Closure")
    shader.CreateIdAttr("ND_dielectric_bsdf")
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    report = validate_stage(stage)

    assert "UNKNOWN_MATERIALX_NODEDEF" in _codes(report)


def test_reader_nodes_rcp_cannot_instantiate_are_rejected():
    """These ids are in the manifest but RCP 3.0 (80.0.1.500.1) substitutes a
    striped placeholder for the whole material when it meets them. The
    manifest cannot catch this class of defect on its own — it flags none of
    them — so preflight is the closing gate."""
    for nodedef in (
        "ND_image_vector4",
        "ND_swizzle_vector4_float",
        "ND_extract_vector4",
        "ND_separate4_vector4",
    ):
        stage, mesh = _stage_with_mesh()
        material = UsdShade.Material.Define(stage, "/Root/Material")
        shader = UsdShade.Shader.Define(stage, "/Root/Material/Reader")
        shader.CreateIdAttr(nodedef)
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

        report = validate_stage(stage)

        assert "MATERIALX_NODEDEF_UNSUPPORTED_BY_RCP" in _codes(report), nodedef


def test_raw_color_space_token_on_a_materialx_reader_is_rejected(tmp_path):
    """No shipping RealityKit package authors the lowercase ``raw`` token."""
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "roughness.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="RawRoughness",
        texture_filename="roughness.png",
        consumer_name="roughness",
        consumer_type=Sdf.ValueTypeNames.Float,
        output_type=Sdf.ValueTypeNames.Float,
        file_color_space="raw",
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_SPACE_UNSUPPORTED_TOKEN" in _codes(report)


def test_a_data_texture_with_no_authored_color_space_is_accepted(tmp_path):
    """An absent MaterialX color space is the data contract, not a mismatch.

    The material prim carries ``colorSpace:name = "lin_rec709_scene"``, so the
    gate must not resolve the ancestor opinion into a data-texture verdict.
    """
    stage, _mesh = _stage_with_mesh()
    (tmp_path / "roughness.png").write_bytes(b"texture")
    _author_materialx_texture(
        stage,
        texture_name="UntaggedRoughness",
        texture_filename="roughness.png",
        consumer_name="roughness",
        consumer_type=Sdf.ValueTypeNames.Float,
        output_type=Sdf.ValueTypeNames.Float,
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_COLOR_SPACE_MISMATCH" not in _codes(report)
    assert report.ok


def test_four_channel_reader_over_a_three_channel_png_warns(tmp_path):
    """The measured defect: a 4-channel read of a 3-channel file."""
    rgb_png = tmp_path / "orm.png"
    rgb_png.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (8).to_bytes(4, "big")
        + (8).to_bytes(4, "big")
        + bytes([8, 2, 0, 0, 0])
    )

    stage, _mesh = _stage_with_mesh()
    material = UsdShade.Material.Define(stage, "/Root/Material")
    material_prim = material.GetPrim()
    material_prim.AddAppliedSchema("MaterialXConfigAPI")
    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )
    texture = UsdShade.Shader.Define(stage, "/Root/Material/Image")
    texture.CreateIdAttr("ND_image_color4")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("orm.png")
    )
    surface.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(
        texture.CreateOutput("out", Sdf.ValueTypeNames.Color4f)
    )

    report = validate_stage(
        stage,
        tmp_path / "scene.usdc",
        SimpleNamespace(export_format="USDC"),
    )

    assert "TEXTURE_ALPHA_SOURCE_MISSING" in _codes(report)


def test_usd_schema_ids_are_not_judged_as_nodedefs():
    """A preview network's ids are USD schemas, not MaterialX."""
    stage, mesh = _stage_with_mesh()
    material = UsdShade.Material.Define(stage, "/Root/Preview")
    for name, shader_id in (
        ("Surface", "UsdPreviewSurface"),
        ("Texture", "UsdUVTexture"),
        ("Transform", "UsdTransform2d"),
    ):
        UsdShade.Shader.Define(stage, f"/Root/Preview/{name}").CreateIdAttr(shader_id)
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)

    report = validate_stage(stage)

    assert "UNKNOWN_MATERIALX_NODEDEF" not in _codes(report)


def _author_geompropvalue_material(
    stage, mesh, *, geomprop, nodedef, output_type
):
    """Bind a material whose base colour is a geometric-property read."""
    material = UsdShade.Material.Define(stage, "/Root/GeomPropMaterial")
    surface = UsdShade.Shader.Define(stage, "/Root/GeomPropMaterial/Surface")
    surface.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(
        surface.ConnectableAPI(), "surface"
    )
    read = UsdShade.Shader.Define(stage, "/Root/GeomPropMaterial/Read")
    read.CreateIdAttr(nodedef)
    read.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set(geomprop)
    read_output = read.CreateOutput("out", output_type)
    surface.CreateInput("baseColor", output_type).ConnectToSource(read_output)
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return material


def test_color3_read_of_a_color4f_primvar_is_an_error():
    """The measured RCP placeholder cause: a color3 read of color4f data.

    Blender writes every mesh color attribute as a color4f primvar, and a
    material reading it with ND_geompropvalue_color3 is replaced wholesale by
    Reality Composer Pro's striped placeholder.
    """
    stage, mesh = _stage_with_mesh()
    paint = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "Paint", Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.faceVarying
    )
    paint.Set([Gf.Vec4f(0.2, 0.5, 0.8, 1.0)] * 3)
    _author_geompropvalue_material(
        stage,
        mesh,
        geomprop="Paint",
        nodedef="ND_geompropvalue_color3",
        output_type=Sdf.ValueTypeNames.Color3f,
    )

    report = validate_stage(stage)

    assert "GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH" in _codes(report)


def test_color4_read_of_a_color4f_primvar_is_clean():
    stage, mesh = _stage_with_mesh()
    paint = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "Paint", Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.faceVarying
    )
    paint.Set([Gf.Vec4f(0.2, 0.5, 0.8, 1.0)] * 3)
    _author_geompropvalue_material(
        stage,
        mesh,
        geomprop="Paint",
        nodedef="ND_geompropvalue_color4",
        output_type=Sdf.ValueTypeNames.Color4f,
    )

    report = validate_stage(stage)

    assert "GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH" not in _codes(report)


def test_vector2_read_of_a_texcoord2f_primvar_is_clean():
    """texCoord2f and float2 hold the same two floats; only the role differs.

    A UV-set read is the other geometric-property read the exporter authors,
    and it must not be swept up by a check about channel counts.
    """
    stage, mesh = _stage_with_mesh()
    uv = UsdGeom.PrimvarsAPI(mesh).CreatePrimvar(
        "UVMap2", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
    )
    uv.Set([Gf.Vec2f(0, 0), Gf.Vec2f(1, 0), Gf.Vec2f(0, 1)])
    _author_geompropvalue_material(
        stage,
        mesh,
        geomprop="UVMap2",
        nodedef="ND_geompropvalue_vector2",
        output_type=Sdf.ValueTypeNames.Float2,
    )

    report = validate_stage(stage)

    assert "GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH" not in _codes(report)


def test_a_geomprop_no_bound_geometry_carries_is_not_judged_here():
    """A dangling read is a different defect; this check measures types only."""
    stage, mesh = _stage_with_mesh()
    _author_geompropvalue_material(
        stage,
        mesh,
        geomprop="NoSuchPrimvar",
        nodedef="ND_geompropvalue_color3",
        output_type=Sdf.ValueTypeNames.Color3f,
    )

    report = validate_stage(stage)

    assert "GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH" not in _codes(report)
