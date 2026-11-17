"""Which meshes get the primvars their materials read, finishes included.

The passes that write Object Info Random, the Bump's UV derivatives and
displayColor run before the variants are authored, when a mesh shows only the
material it wears. A finish it can be given counts too; a mesh no finish
targets does not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")

from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export import postprocess_usd  # noqa: E402
from Plugin.export.materials.extract.core import (  # noqa: E402
    OBJECT_RANDOM_PRIMVAR,
    UV_DERIVATIVE_PRIMVARS,
)
from Plugin.export.variants import (  # noqa: E402
    MATERIAL,
    VariantMember,
    VariantPlan,
    VariantSetPlan,
)

PLAIN = "/root/_materials/Plain"
RICH = "/root/_materials/Rich"
MATERIAL_NAMES = {PLAIN: "Fabric_Plain", RICH: "Fabric_Rich"}


def _finish_plan(*targets, offered=("Fabric_Plain", "Fabric_Rich")):
    return VariantPlan(sets=(
        VariantSetPlan(
            name="Finish",
            kind=MATERIAL,
            default_variant="Plain",
            has_target=True,
            target_objects=tuple(SimpleNamespace(name=name) for name in targets),
            variants=tuple(
                VariantMember(name=f"V{index}", collection_name="", has_collection=False,
                              material_name=name, has_material=True)
                for index, name in enumerate(offered)
            ),
        ),
    ))


def _stage(rich_reads):
    """Seat and Bystander wear Plain; Rich reads ``rich_reads``."""
    stage = Usd.Stage.CreateInMemory()
    root = UsdGeom.Xform.Define(stage, "/root")
    stage.SetDefaultPrim(root.GetPrim())
    plain = UsdShade.Material.Define(stage, PLAIN)
    UsdShade.Material.Define(stage, RICH)
    reader = UsdShade.Shader.Define(stage, RICH + "/reader")
    if rich_reads == "geomcolor":
        reader.CreateIdAttr("ND_geomcolor_color4")
    else:
        reader.CreateIdAttr("ND_geompropvalue_float")
        reader.CreateInput("geomprop", Sdf.ValueTypeNames.String).Set(rich_reads)

    for name in ("Seat", "Bystander"):
        xform = UsdGeom.Xform.Define(stage, f"/root/{name}")
        xform.GetPrim().CreateAttribute(
            "userProperties:blender:object_name", Sdf.ValueTypeNames.String).Set(name)
        mesh = UsdGeom.Mesh.Define(stage, f"/root/{name}/Mesh")
        mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
        mesh.CreateFaceVertexCountsAttr([4])
        mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
        primvars = UsdGeom.PrimvarsAPI(mesh)
        primvars.CreatePrimvar(
            "st", Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.faceVarying
        ).Set([(0, 0), (1, 0), (1, 1), (0, 1)])
        primvars.CreatePrimvar(
            "Paint", Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.vertex
        ).Set([(0.8, 0.2, 0.1, 1.0)] * 4)
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(plain)
    return stage


def _object_map(*names):
    return {f"/root/{name}": name for name in names}


def _offered(stage, plan, names=("Seat", "Bystander")):
    return postprocess_usd._finish_materials_by_mesh(
        stage, plan, _object_map(*names), (), MATERIAL_NAMES)


def test_a_finish_offers_its_materials_to_the_mesh_of_each_target():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    offered = _offered(stage, _finish_plan("Seat"))
    assert offered == {Sdf.Path("/root/Seat/Mesh"): frozenset({Sdf.Path(PLAIN), Sdf.Path(RICH)})}


def test_a_target_outside_the_export_and_an_empty_plan_offer_nothing():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    assert _offered(stage, _finish_plan("Seat"), names=("Bystander",)) == {}
    assert _offered(stage, VariantPlan()) == {}
    assert postprocess_usd._finish_materials_by_mesh(
        stage, _finish_plan("Seat"), None, (), MATERIAL_NAMES) == {}


def test_a_child_object_is_not_offered_its_parents_finish():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    child = UsdGeom.Xform.Define(stage, "/root/Seat/Cushion")
    UsdGeom.Mesh.Define(stage, "/root/Seat/Cushion/Mesh")
    object_map = {**_object_map("Seat", "Bystander"), str(child.GetPath()): "Cushion"}
    offered = postprocess_usd._finish_materials_by_mesh(
        stage, _finish_plan("Seat"), object_map, (), MATERIAL_NAMES)
    assert set(offered) == {Sdf.Path("/root/Seat/Mesh")}


def test_a_face_subset_binding_offers_the_finish_to_its_mesh():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/root/Seat/Mesh"))
    UsdShade.MaterialBindingAPI(mesh.GetPrim()).UnbindAllBindings()
    subset = UsdGeom.Subset.Define(stage, "/root/Seat/Mesh/slot1")
    subset.CreateIndicesAttr([0])
    UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(
        UsdShade.Material(stage.GetPrimAtPath(PLAIN)))
    assert set(_offered(stage, _finish_plan("Seat"))) == {Sdf.Path("/root/Seat/Mesh")}


def test_a_finish_with_a_bump_gets_uv_derivatives_only_on_its_targets():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    postprocess_usd._author_uv_derivatives(stage, None, _offered(stage, _finish_plan("Seat")))
    for name in UV_DERIVATIVE_PRIMVARS:
        assert UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Seat/Mesh")).GetPrimvar(name)
        assert not UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Bystander/Mesh")).GetPrimvar(name)


def test_without_the_finish_nothing_reads_a_bump():
    stage = _stage(UV_DERIVATIVE_PRIMVARS[0])
    postprocess_usd._author_uv_derivatives(stage)
    assert not UsdGeom.PrimvarsAPI(
        stage.GetPrimAtPath("/root/Seat/Mesh")).GetPrimvar(UV_DERIVATIVE_PRIMVARS[0])


def test_a_finish_reading_object_random_gets_it():
    stage = _stage(OBJECT_RANDOM_PRIMVAR)
    postprocess_usd._author_object_random(stage, None, _offered(stage, _finish_plan("Seat")))
    assert UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Seat/Mesh")).GetPrimvar(OBJECT_RANDOM_PRIMVAR)
    assert not UsdGeom.PrimvarsAPI(
        stage.GetPrimAtPath("/root/Bystander/Mesh")).GetPrimvar(OBJECT_RANDOM_PRIMVAR)


def test_a_finish_reading_vertex_colours_gets_display_color():
    stage = _stage("geomcolor")
    postprocess_usd._publish_vertex_colors_as_display_color(
        stage, None, None, _offered(stage, _finish_plan("Seat")))
    seat = UsdGeom.PrimvarsAPI(stage.GetPrimAtPath("/root/Seat/Mesh")).GetPrimvar("displayColor")
    assert seat and list(seat.Get()[0]) == pytest.approx([0.8, 0.2, 0.1])
    bystander = UsdGeom.PrimvarsAPI(
        stage.GetPrimAtPath("/root/Bystander/Mesh")).GetPrimvar("displayColor")
    assert not (bystander and bystander.Get())


def test_the_variant_passes_share_one_plan_resolved_for_the_export_scope(monkeypatch):
    calls = []

    def effective(context, settings):
        calls.append((context, settings))
        return VariantPlan()

    monkeypatch.setattr(postprocess_usd.variant_scope, "effective_variant_plan", effective)
    run = postprocess_usd._Run(
        stage=_stage(UV_DERIVATIVE_PRIMVARS[0]), usd_path="", settings=object(),
        context=object(), diagnostics=None, object_map={})
    passes = {name: run_pass for name, run_pass, _keep in postprocess_usd.PASSES}
    for name in ("reserve_variant_materials", "author_uv_derivatives", "author_variants"):
        passes[name](run)
    assert calls == [(run.context, run.settings)]
