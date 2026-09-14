"""Lossless USD scene-normalization tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.realitykit_preflight import validate_stage  # noqa: E402
from Plugin.export.usd_scene import (  # noqa: E402
    _normalize_owned_double_sided_mesh_specs,
    normalize_scene,
)


def _settings(**overrides):
    values = {
        "root_prim_name": "Root",
        "convert_orientation": False,
        "allow_unicode": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _author_triangle(mesh):
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)


def test_namespace_normalization_preserves_subtree_data_and_retargets_paths():
    stage = Usd.Stage.CreateInMemory()
    source = stage.DefinePrim("/Røød", "Xform")
    stage.SetDefaultPrim(source)
    # Force a deterministic sibling collision for the ASCII-safe name.
    stage.DefinePrim("/R__d", "Xform")

    child = stage.DefinePrim("/Røød/Chïld", "Xform")
    value = child.CreateAttribute("customValue", Sdf.ValueTypeNames.Float)
    value.Set(1.0)
    value.Set(2.0, 10.0)
    child.SetMetadata("documentation", "preserve me")
    variants = child.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("red")
    variants.SetVariantSelection("red")

    other = stage.DefinePrim("/Other", "Xform")
    relationship = other.CreateRelationship("target")
    relationship.SetTargets(["/Røød/Chïld"])
    connection = other.CreateAttribute("connected", Sdf.ValueTypeNames.Float)
    connection.SetConnections(["/Røød/Chïld.customValue"])

    normalize_scene(
        stage,
        SimpleNamespace(
            root_prim_name="/root",
            convert_orientation=False,
            allow_unicode=False,
        ),
    )

    renamed = stage.GetPrimAtPath("/R__d_2/Ch_ld")
    assert renamed
    assert renamed.GetMetadata("documentation") == "preserve me"
    assert renamed.GetVariantSets().GetVariantSet("look").GetVariantSelection() == "red"
    assert renamed.GetAttribute("customValue").Get() == 1.0
    assert renamed.GetAttribute("customValue").GetTimeSamples() == [10.0]
    assert renamed.GetAttribute("customValue").Get(10.0) == 2.0
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/R__d_2")
    assert relationship.GetTargets() == [Sdf.Path("/R__d_2/Ch_ld")]
    assert connection.GetConnections() == [
        Sdf.Path("/R__d_2/Ch_ld.customValue")
    ]
    assert not stage.GetPrimAtPath("/Røød")


def test_missing_default_prim_uses_a_valid_root_identifier():
    stage = Usd.Stage.CreateInMemory()

    normalize_scene(
        stage,
        SimpleNamespace(
            root_prim_name="/Product/Assembly",
            convert_orientation=False,
            allow_unicode=True,
        ),
    )

    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/Product_Assembly")


def test_normalization_refuses_non_localized_external_layer(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    sublayer = source / "sub.usda"
    sublayer.write_text('#usda 1.0\ndef Xform "Røød" {}\n', encoding="utf-8")
    source_bytes = sublayer.read_bytes()
    root = tmp_path / "scene.usda"
    root.write_text('#usda 1.0\n( subLayers = [@source/sub.usda@] )\n')
    stage = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)

    with pytest.raises(RuntimeError, match="non-localized USD layer"):
        normalize_scene(
            stage,
            SimpleNamespace(
                root_prim_name="Scene",
                convert_orientation=False,
                allow_unicode=False,
            ),
            writable_layer_paths={str(root.resolve())},
        )

    assert sublayer.read_bytes() == source_bytes


def test_owned_mesh_specs_author_false_and_warn_once_per_true_owner(tmp_path):
    root_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(root_path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    blender_mesh = UsdGeom.Mesh.Define(stage, "/Root/BlenderCube")
    _author_triangle(blender_mesh)
    blender_mesh.CreateDoubleSidedAttr(True)

    unauthored_mesh = UsdGeom.Mesh.Define(stage, "/Root/NoOpinion")
    _author_triangle(unauthored_mesh)
    stage.Save()

    diagnostics = ExportDiagnostics()
    normalize_scene(
        stage,
        _settings(),
        writable_layer_paths={str(root_path.resolve())},
        diagnostics=diagnostics,
    )

    assert blender_mesh.GetDoubleSidedAttr().Get() is False
    assert unauthored_mesh.GetDoubleSidedAttr().HasAuthoredValueOpinion()
    assert unauthored_mesh.GetDoubleSidedAttr().Get() is False
    portability_warnings = [
        warning
        for warning in diagnostics.data.get("info", [])
        if "doubleSided=false" in warning
    ]
    assert len(portability_warnings) == 1
    assert "/Root/BlenderCube" in portability_warnings[0]
    assert "Backfaces are unsupported" in portability_warnings[0]
    assert "closed or thick geometry is required" in portability_warnings[0]

    report = validate_stage(stage, root_path, SimpleNamespace(export_format="USDA"))
    assert "DOUBLE_SIDED_GEOMETRY" not in {
        issue.code for issue in report.errors
    }


def test_owned_mesh_normalization_updates_stage_layer_across_macos_var_alias(
    tmp_path,
):
    """Mutate the layer composed by the stage, not a canonical-path duplicate."""
    canonical_dir = str(tmp_path.resolve())
    if not canonical_dir.startswith("/private/var/"):
        pytest.skip("macOS /var to /private/var alias is unavailable")

    lexical_dir = Path(canonical_dir.removeprefix("/private"))
    lexical_path = lexical_dir / "aliased-scene.usda"
    canonical_path = Path(canonical_dir) / lexical_path.name
    stage = Usd.Stage.CreateNew(str(lexical_path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Cube")
    _author_triangle(mesh)
    mesh.CreateDoubleSidedAttr(True)
    stage.Save()

    _normalize_owned_double_sided_mesh_specs(
        {str(canonical_path.resolve())},
        stage=stage,
    )

    assert mesh.GetDoubleSidedAttr().Get() is False
    assert (
        canonical_path.read_text(encoding="utf-8").count("doubleSided = 0") == 1
    )


def test_inactive_variant_mesh_specs_are_normalized_without_changing_selection(
    tmp_path,
):
    root_path = tmp_path / "variants.usda"
    stage = Usd.Stage.CreateNew(str(root_path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    variants = root.GetVariantSets().AddVariantSet("look")

    for name, double_sided in (("portable", False), ("hiddenTwoSided", True)):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            mesh = UsdGeom.Mesh.Define(stage, "/Root/VariantMesh")
            _author_triangle(mesh)
            mesh.CreateDoubleSidedAttr(double_sided)
    variants.SetVariantSelection("portable")
    stage.Save()

    diagnostics = ExportDiagnostics()
    affected = _normalize_owned_double_sided_mesh_specs(
        {str(root_path.resolve())},
        diagnostics=diagnostics,
    )

    assert variants.GetVariantSelection() == "portable"
    double_sided_specs = []
    layer = stage.GetRootLayer()
    layer.Traverse(
        Sdf.Path.absoluteRootPath,
        lambda path: double_sided_specs.append(layer.GetObjectAtPath(path))
        if str(path).endswith(".doubleSided")
        else None,
    )
    assert len(double_sided_specs) == 2
    assert all(spec.default is False for spec in double_sided_specs)
    assert len(affected) == 1
    assert "hiddenTwoSided" in affected[0]
    assert len(
        [
            warning
            for warning in diagnostics.data.get("info", [])
            if "doubleSided=false" in warning
        ]
    ) == 1


def test_inactive_variant_over_inheriting_mesh_type_is_normalized(tmp_path):
    root_path = tmp_path / "variant-over.usda"
    stage = Usd.Stage.CreateNew(str(root_path))
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    _author_triangle(mesh)
    mesh.CreateDoubleSidedAttr(False)

    variants = root.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("portable")
    variants.AddVariant("hiddenTwoSided")
    variants.SetVariantSelection("hiddenTwoSided")
    with variants.GetVariantEditContext():
        variant_over = stage.OverridePrim("/Root/Mesh")
        variant_over.CreateAttribute(
            "doubleSided",
            Sdf.ValueTypeNames.Bool,
            custom=False,
            variability=Sdf.VariabilityUniform,
        ).Set(True)
    variants.SetVariantSelection("portable")
    stage.Save()

    layer = stage.GetRootLayer()
    over_specs = []
    layer.Traverse(
        Sdf.Path.absoluteRootPath,
        lambda path: over_specs.append(layer.GetObjectAtPath(path))
        if path.ContainsPrimVariantSelection()
        and isinstance(layer.GetObjectAtPath(path), Sdf.PrimSpec)
        else None,
    )
    mesh_over = next(
        spec
        for spec in over_specs
        if str(spec.path.StripAllVariantSelections()) == "/Root/Mesh"
    )
    assert str(mesh_over.typeName) == ""
    assert mesh_over.attributes["doubleSided"].default is True

    diagnostics = ExportDiagnostics()
    affected = _normalize_owned_double_sided_mesh_specs(
        {str(root_path.resolve())},
        diagnostics=diagnostics,
    )

    assert variants.GetVariantSelection() == "portable"
    assert mesh_over.attributes["doubleSided"].default is False
    assert len(affected) == 1
    assert "hiddenTwoSided" in affected[0]


def test_used_blender_class_prototype_mesh_is_normalized_in_place(tmp_path):
    root_path = tmp_path / "prototype.usda"
    stage = Usd.Stage.CreateNew(str(root_path))
    stage.CreateClassPrim("/prototypes")
    stage.CreateClassPrim("/prototypes/Collection")
    prototype_mesh_prim = stage.CreateClassPrim(
        "/prototypes/Collection/BlenderCube"
    )
    prototype_mesh_prim.SetTypeName("Mesh")
    prototype_mesh = UsdGeom.Mesh(prototype_mesh_prim)
    _author_triangle(prototype_mesh)
    prototype_mesh.CreateDoubleSidedAttr(True)

    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    instance = stage.DefinePrim("/Root/Instance", "Xform")
    assert instance.GetReferences().AddInternalReference("/prototypes/Collection")
    instance.SetInstanceable(True)
    stage.Save()

    diagnostics = ExportDiagnostics()
    affected = _normalize_owned_double_sided_mesh_specs(
        {str(root_path.resolve())},
        diagnostics=diagnostics,
    )

    assert prototype_mesh.GetDoubleSidedAttr().Get() is False
    assert len(affected) == 1
    assert "/prototypes/Collection/BlenderCube" in affected[0]
    assert len(
        [
            warning
            for warning in diagnostics.data.get("info", [])
            if "doubleSided=false" in warning
        ]
    ) == 1
