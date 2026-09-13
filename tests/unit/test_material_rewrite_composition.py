"""Composition-safe MaterialX rewrite regressions for Blender 5.2 USD."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export import usd_textures  # noqa: E402
from Plugin.export.materials import rewrite as material_rewrite  # noqa: E402


def _flat_material(name: str, color):
    return SimpleNamespace(
        name=name,
        node_tree=None,
        surface_render_method="DITHERED",
        color=list(color),
    )


@pytest.fixture(autouse=True)
def _flat_emission_extraction(monkeypatch):
    """Stand-in materials extract as flat Emission colours, without a node tree."""
    def extract(material):
        return {
            "name": material.name,
            "type": "emission",
            "base_color": material.color,
            "alpha": 1.0,
        }

    monkeypatch.setattr(material_rewrite, "extract_blender_material_data", extract)


def _context(*materials):
    return SimpleNamespace(blend_data=SimpleNamespace(materials=list(materials)))


def _settings():
    return SimpleNamespace(
        force_unlit_materials=False,
    )


def _native_material(stage, path: str, blender_name: str):
    material = UsdShade.Material.Define(stage, path)
    material.GetPrim().CreateAttribute(
        "userProperties:blender:data_name",
        Sdf.ValueTypeNames.String,
    ).Set(blender_name)
    preview = UsdShade.Shader.Define(stage, f"{path}/PreviewSurface")
    preview.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(
        preview.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )
    return material


def _direct_bound_material_path(prim):
    material = (
        UsdShade.MaterialBindingAPI(prim)
        .GetDirectBinding()
        .GetMaterial()
    )
    return str(material.GetPath()) if material else None


def test_collection_instance_class_material_is_rewritten_once(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)

    stage.CreateClassPrim("/Root/prototypes")
    prototype = stage.DefinePrim("/Root/prototypes/Collection", "Xform")
    mesh = UsdGeom.Mesh.Define(stage, "/Root/prototypes/Collection/Mesh")
    native = _native_material(
        stage,
        "/Root/prototypes/Collection/PrototypeMaterial",
        "PrototypeMaterial",
    )
    UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(native)

    for instance_name in ("InstanceA", "InstanceB"):
        instance = stage.DefinePrim(f"/Root/{instance_name}", "Xform")
        instance.GetReferences().AddInternalReference(prototype.GetPath())
        instance.SetInstanceable(True)

    assert not any(
        "/prototypes/" in str(prim.GetPath()) for prim in stage.Traverse()
    )
    assert len(stage.GetPrototypes()) == 1

    calls = []
    real_create = material_rewrite.create_materialx_material

    def counted_create(stage, material_path, *args, **kwargs):
        calls.append(material_path)
        return real_create(stage, material_path, *args, **kwargs)

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        counted_create,
    )
    diagnostics = ExportDiagnostics()
    material_rewrite.rewrite_materials(
        stage,
        _settings(),
        _context(_flat_material("PrototypeMaterial", (0.8, 0.2, 0.1))),
        diagnostics,
    )

    material_path = "/Root/prototypes/Collection/PrototypeMaterial"
    assert calls == [material_path]
    assert diagnostics.data["materials"]["converted"] == 1
    rewritten = UsdShade.Material(stage.GetPrimAtPath(material_path))
    assert rewritten.GetSurfaceOutput("mtlx").GetConnectedSource()
    shader_ids = {
        UsdShade.Shader(prim).GetIdAttr().Get()
        for prim in stage.TraverseAll()
        if prim.GetPath().HasPrefix(Sdf.Path(material_path))
        and prim.IsA(UsdShade.Shader)
    }
    assert "ND_realitykit_unlit_surfaceshader" in shader_ids


def test_inactive_red_blue_variant_bindings_survive_in_place_rewrite(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    red = _native_material(stage, "/Root/Looks/Red", "Red")
    blue = _native_material(stage, "/Root/Looks/Blue", "Blue")

    look = mesh.GetPrim().GetVariantSets().AddVariantSet("look")
    for name, material in (("red", red), ("blue", blue)):
        look.AddVariant(name)
        look.SetVariantSelection(name)
        with look.GetVariantEditContext():
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    look.SetVariantSelection("red")

    calls = []
    real_create = material_rewrite.create_materialx_material

    def counted_create(stage, material_path, *args, **kwargs):
        calls.append(material_path)
        return real_create(stage, material_path, *args, **kwargs)

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        counted_create,
    )
    material_rewrite.rewrite_materials(
        stage,
        _settings(),
        _context(
            _flat_material("Red", (1.0, 0.0, 0.0)),
            _flat_material("Blue", (0.0, 0.0, 1.0)),
        ),
        ExportDiagnostics(),
    )

    assert set(calls) == {"/Root/Looks/Red", "/Root/Looks/Blue"}
    assert len(calls) == 2
    assert look.GetVariantSelection() == "red"
    assert _direct_bound_material_path(mesh.GetPrim()) == "/Root/Looks/Red"
    look.SetVariantSelection("blue")
    assert _direct_bound_material_path(mesh.GetPrim()) == "/Root/Looks/Blue"
    look.SetVariantSelection("red")
    assert _direct_bound_material_path(mesh.GetPrim()) == "/Root/Looks/Red"

    # No stronger, non-variant binding was introduced by the rewrite.
    assert stage.GetRootLayer().GetObjectAtPath(
        Sdf.Path("/Root/Mesh.material:binding")
    ) is None
    for name in ("red", "blue"):
        spec = stage.GetRootLayer().GetObjectAtPath(
            Sdf.Path(f"/Root/Mesh{{look={name}}}.material:binding")
        )
        assert isinstance(spec, Sdf.RelationshipSpec)
        assert list(spec.targetPathList.GetAppliedItems()) == [
            Sdf.Path(f"/Root/Looks/{name.title()}")
        ]

    for material_path in ("/Root/Looks/Red", "/Root/Looks/Blue"):
        material = UsdShade.Material(stage.GetPrimAtPath(material_path))
        assert material.GetSurfaceOutput("mtlx").GetConnectedSource()


def test_mesh_created_only_by_inactive_variant_rewrites_its_material(monkeypatch):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    blue = _native_material(stage, "/Root/Looks/Blue", "Blue")

    product = root.GetVariantSets().AddVariantSet("product")
    for name in ("good", "bad"):
        product.AddVariant(name)
        product.SetVariantSelection(name)
        with product.GetVariantEditContext():
            if name == "bad":
                mesh = UsdGeom.Mesh.Define(stage, "/Root/VariantMesh")
                UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(blue)
    product.SetVariantSelection("good")

    assert not stage.GetPrimAtPath("/Root/VariantMesh")
    calls = []
    real_create = material_rewrite.create_materialx_material

    def counted_create(stage, material_path, *args, **kwargs):
        calls.append(material_path)
        return real_create(stage, material_path, *args, **kwargs)

    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        counted_create,
    )
    material_rewrite.rewrite_materials(
        stage,
        _settings(),
        _context(_flat_material("Blue", (0.0, 0.0, 1.0))),
        ExportDiagnostics(),
    )

    assert calls == ["/Root/Looks/Blue"]
    assert product.GetVariantSelection() == "good"
    assert not stage.GetPrimAtPath("/Root/VariantMesh")
    material = UsdShade.Material(stage.GetPrimAtPath("/Root/Looks/Blue"))
    assert material.GetSurfaceOutput("mtlx").GetConnectedSource()

    product.SetVariantSelection("bad")
    variant_mesh = stage.GetPrimAtPath("/Root/VariantMesh")
    assert variant_mesh and variant_mesh.IsA(UsdGeom.Mesh)
    assert _direct_bound_material_path(variant_mesh) == "/Root/Looks/Blue"


def test_material_defined_only_inside_inactive_variant_fails_before_rewrite(
    monkeypatch,
):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    mesh = UsdGeom.Mesh.Define(stage, "/Root/Mesh")
    look = mesh.GetPrim().GetVariantSets().AddVariantSet("look")

    for name in ("red", "blue"):
        look.AddVariant(name)
        look.SetVariantSelection(name)
        with look.GetVariantEditContext():
            material = _native_material(
                stage,
                f"/Root/Mesh/Looks/{name.title()}",
                name.title(),
            )
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    look.SetVariantSelection("red")

    calls = []
    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    with pytest.raises(
        RuntimeError,
        match="Cannot safely rewrite inactive variant material binding",
    ):
        material_rewrite.rewrite_materials(
            stage,
            _settings(),
            _context(
                _flat_material("Red", (1.0, 0.0, 0.0)),
                _flat_material("Blue", (0.0, 0.0, 1.0)),
            ),
            ExportDiagnostics(),
        )
    assert calls == []


def test_premultiplied_avif_policy_preflights_all_materials_before_authoring(
    tmp_path,
    monkeypatch,
):
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/Root", "Xform")
    stage.SetDefaultPrim(root)
    safe_mesh = UsdGeom.Mesh.Define(stage, "/Root/SafeMesh")
    unsafe_mesh = UsdGeom.Mesh.Define(stage, "/Root/UnsafeMesh")
    safe_native = _native_material(stage, "/Root/Looks/Safe", "Safe")
    unsafe_native = _native_material(stage, "/Root/Looks/Unsafe", "Unsafe")
    UsdShade.MaterialBindingAPI.Apply(safe_mesh.GetPrim()).Bind(safe_native)
    UsdShade.MaterialBindingAPI.Apply(unsafe_mesh.GetPrim()).Bind(unsafe_native)

    source = tmp_path / "associated-alpha.png"
    source.write_bytes(b"associated-alpha")
    extracted = []

    def extract(material):
        extracted.append(material.name)
        if material.name == "Unsafe":
            return {
                "name": material.name,
                "type": "principled",
                "base_color_texture": str(source),
                "base_color_texture_alpha_mode": "premul",
                "has_premultiplied_alpha": True,
                "is_transparent": True,
            }
        return {
            "name": material.name,
            "type": "emission",
            "base_color": [0.2, 0.3, 0.4],
            "alpha": 1.0,
        }

    monkeypatch.setattr(material_rewrite, "extract_blender_material_data", extract)
    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args, **_kwargs: {
            "file_format": "AVIF",
            "extension": ".avif",
            "resolution": 0,
        },
    )
    authored = []
    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        lambda *args, **kwargs: authored.append((args, kwargs)),
    )
    diagnostics = ExportDiagnostics()

    with pytest.raises(RuntimeError, match=r"Premultiplied.*Select PNG"):
        material_rewrite.rewrite_materials(
            stage,
            _settings(),
            _context(
                _flat_material("Safe", (0.2, 0.3, 0.4)),
                _flat_material("Unsafe", (0.8, 0.1, 0.1)),
            ),
            diagnostics,
        )

    assert set(extracted) == {"Safe", "Unsafe"}
    assert authored == []
    assert not UsdShade.Material(
        stage.GetPrimAtPath("/Root/Looks/Safe")
    ).GetSurfaceOutput("mtlx")
    assert not UsdShade.Material(
        stage.GetPrimAtPath("/Root/Looks/Unsafe")
    ).GetSurfaceOutput("mtlx")
    assert diagnostics.data["materials"]["failed"] == 1
    assert "disable the unsafe AVIF/resolution override" in "\n".join(
        diagnostics.data["errors"]
    )


def test_nested_premultiplied_base_color_avif_is_rejected(monkeypatch):
    nested_material = {
        "name": "NestedPremul",
        "type": "principled",
        "has_premultiplied_alpha": True,
        "input_graphs": {
            "baseColor": {
                "kind": "node",
                "node_id": "ND_multiply_color3",
                "inputs": {
                    "in1": {
                        "kind": "texture",
                        "path": "/tmp/nested-premul.png",
                        "alpha_mode": "premul",
                        "output_type": "color3",
                    },
                    "in2": {
                        "kind": "constant",
                        "value": [0.5, 0.5, 0.5],
                    },
                },
            }
        },
    }
    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args, **_kwargs: {
            "file_format": "AVIF",
            "extension": ".avif",
            "resolution": 0,
        },
    )

    with pytest.raises(RuntimeError, match=r"nested-premul.*Select PNG"):
        material_rewrite._require_safe_material_texture_policy(
            nested_material,
            _settings(),
        )


def test_mixed_base_color_alpha_conventions_fail_before_format_policy(monkeypatch):
    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args, **_kwargs: None,
    )
    mixed_material = {
        "name": "MixedAlpha",
        "type": "principled",
        "base_color_texture_sources": [
            {"path": "/tmp/premul.png", "alpha_mode": "premul"},
            {"path": "/tmp/straight.png", "alpha_mode": "straight"},
        ],
        "base_color_alpha_semantics_error": (
            "Base Color combines textures with incompatible alpha conventions: "
            "premul, straight"
        ),
    }

    with pytest.raises(RuntimeError, match=r"Bake Base Color and Alpha to one PNG"):
        material_rewrite._require_safe_material_texture_policy(
            mixed_material,
            _settings(),
        )
