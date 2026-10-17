"""Two Blender material names reducing to one USD prim name must refuse.

Blender's exporter turns a material name into a USD prim name, and two names
can reduce to the same one: "My Material" and "My-Material" both become
``My_Material``. The exporter then authors a single Material prim and binds
both meshes to it, so one material is dropped and the mesh that owned it
renders with the other. Its prim map records only the surviving material, and
which one that is depends on export order, so the collapse has to be detected
from the Blender side.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: E402

from Plugin.export import usd_hook  # noqa: E402
from Plugin.export.diagnostics import ExportDiagnostics  # noqa: E402
from Plugin.export.materials import rewrite as material_rewrite  # noqa: E402


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_colliding_claims_refuse_and_name_both_materials():
    claims = {"/root/_materials/My_Material": ["My Material", "My-Material"]}

    with pytest.raises(RuntimeError) as excinfo:
        material_rewrite._require_distinct_material_prims(claims)

    message = str(excinfo.value)
    assert "'My Material'" in message
    assert "'My-Material'" in message
    assert "/root/_materials/My_Material" in message


def test_refusal_is_recorded_in_diagnostics():
    diagnostics = ExportDiagnostics()
    claims = {"/root/_materials/Mat_001": ["Mat.001", "Mat 001"]}

    with pytest.raises(RuntimeError):
        material_rewrite._require_distinct_material_prims(claims, diagnostics)

    errors = diagnostics.data["errors"]
    assert len(errors) == 1
    assert "/root/_materials/Mat_001" in errors[0]


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({}, id="no-claims"),
        pytest.param(
            {"/root/_materials/Red": ["Red"], "/root/_materials/Blue": ["Blue"]},
            id="one-material-each",
        ),
        pytest.param(
            {"/root/_materials/Shared": ["Shared"]},
            id="one-material-claimed-twice",
        ),
    ],
)
def test_unambiguous_claims_are_allowed(claims):
    material_rewrite._require_distinct_material_prims(claims)


# ---------------------------------------------------------------------------
# Building the claims from the stage
# ---------------------------------------------------------------------------


def _stage_with_meshes(*, bindings):
    """A stage of meshes under /root, each bound to the named material prim."""
    stage = Usd.Stage.CreateInMemory()
    root = stage.DefinePrim("/root", "Xform")
    stage.SetDefaultPrim(root)

    materials = {}
    for mesh_name, material_name in bindings.items():
        mesh = UsdGeom.Mesh.Define(stage, f"/root/{mesh_name}")
        if material_name is None:
            continue
        path = f"/root/_materials/{material_name}"
        material = materials.get(path)
        if material is None:
            material = UsdShade.Material.Define(stage, path)
            materials[path] = material
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
    return stage


def test_two_single_material_meshes_sharing_a_prim_collide():
    stage = _stage_with_meshes(
        bindings={"Cube": "My_Material", "Cube_001": "My_Material"}
    )
    mesh_material_map = {
        "/root/Cube": ["My Material"],
        "/root/Cube_001": ["My-Material"],
    }

    claims = material_rewrite._material_prim_claims(stage, mesh_material_map)

    assert claims == {
        "/root/_materials/My_Material": ["My Material", "My-Material"]
    }
    with pytest.raises(RuntimeError):
        material_rewrite._require_distinct_material_prims(claims)


def test_one_material_bound_to_two_meshes_does_not_collide():
    stage = _stage_with_meshes(
        bindings={"Cube": "Shared", "Cube_001": "Shared"}
    )
    mesh_material_map = {
        "/root/Cube": ["Shared"],
        "/root/Cube_001": ["Shared"],
    }

    claims = material_rewrite._material_prim_claims(stage, mesh_material_map)

    assert claims == {"/root/_materials/Shared": ["Shared"]}
    material_rewrite._require_distinct_material_prims(claims)


def test_multi_material_mesh_is_not_mistaken_for_a_collision():
    """A mesh using several materials binds GeomSubsets, not one prim.

    Its own direct binding names only the first of them, so attributing the
    whole slot list to that prim would refuse an export that loses nothing.
    """
    stage = _stage_with_meshes(bindings={"Cube": "AA"})
    mesh_material_map = {"/root/Cube": ["AA", "BB"]}

    claims = material_rewrite._material_prim_claims(stage, mesh_material_map)

    assert claims == {}
    material_rewrite._require_distinct_material_prims(claims)


def test_mesh_missing_from_the_prim_map_is_skipped():
    stage = _stage_with_meshes(bindings={"Cube": "AA"})

    assert material_rewrite._material_prim_claims(stage, {"/root/Other": ["AA"]}) == {}


def test_unbound_mesh_is_skipped():
    stage = _stage_with_meshes(bindings={"Cube": None})

    assert material_rewrite._material_prim_claims(stage, {"/root/Cube": ["AA"]}) == {}


def test_no_captured_map_skips_the_check_entirely():
    """Without the hook there is no Blender side to compare, so nothing refuses."""
    stage = _stage_with_meshes(
        bindings={"Cube": "My_Material", "Cube_001": "My_Material"}
    )

    assert material_rewrite._material_prim_claims(stage, {}) == {}


# ---------------------------------------------------------------------------
# The whole rewrite refuses before authoring anything
# ---------------------------------------------------------------------------


def test_rewrite_refuses_before_authoring_any_material(monkeypatch):
    stage = _stage_with_meshes(
        bindings={"Cube": "My_Material", "Cube_001": "My_Material"}
    )
    material = UsdShade.Material.Get(stage, "/root/_materials/My_Material")
    material.GetPrim().CreateAttribute(
        "userProperties:blender:data_name",
        Sdf.ValueTypeNames.String,
    ).Set("My Material")

    monkeypatch.setattr(
        material_rewrite,
        "consume_captured_mesh_material_map",
        lambda: {
            "/root/Cube": ["My Material"],
            "/root/Cube_001": ["My-Material"],
        },
    )
    authored = []
    monkeypatch.setattr(
        material_rewrite,
        "create_materialx_material",
        lambda *args, **kwargs: authored.append(args),
    )

    context = SimpleNamespace(
        blend_data=SimpleNamespace(
            materials=[
                SimpleNamespace(name="My Material", node_tree=None),
                SimpleNamespace(name="My-Material", node_tree=None),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="export as the same USD material prim"):
        material_rewrite.rewrite_materials(
            stage,
            SimpleNamespace(force_unlit_materials=False),
            context,
        )

    assert authored == []


# ---------------------------------------------------------------------------
# Which Blender materials a mesh's faces actually use
# ---------------------------------------------------------------------------


class _Polygons(list):
    """Stand-in for ``mesh.polygons``, optionally without ``foreach_get``."""

    def __init__(self, indices, support_foreach=True):
        super().__init__(
            SimpleNamespace(material_index=index) for index in indices
        )
        self._indices = list(indices)
        self._support_foreach = support_foreach

    def foreach_get(self, attribute, sequence):
        if not self._support_foreach:
            raise AttributeError(attribute)
        assert attribute == "material_index"
        sequence[:] = self._indices


def _mesh(slot_names, face_indices, support_foreach=True):
    return SimpleNamespace(
        materials=[
            None if name is None else SimpleNamespace(name=name)
            for name in slot_names
        ],
        polygons=_Polygons(face_indices, support_foreach=support_foreach),
    )


def test_single_slot_mesh_reports_its_material():
    assert usd_hook._used_material_names(_mesh(["Red"], [0, 0, 0])) == ["Red"]


def test_slot_no_face_uses_is_left_out():
    """Blender exports no material prim for an unused slot."""
    mesh = _mesh(["CC", "DD"], [0, 0, 0])

    assert usd_hook._used_material_names(mesh) == ["CC"]


def test_every_used_slot_is_reported():
    mesh = _mesh(["AA", "BB"], [0, 0, 1, 1])

    assert usd_hook._used_material_names(mesh) == ["AA", "BB"]


def test_used_slots_are_read_without_foreach_get():
    mesh = _mesh(["AA", "BB"], [0, 1], support_foreach=False)

    assert usd_hook._used_material_names(mesh) == ["AA", "BB"]


def test_mesh_without_faces_uses_no_material():
    assert usd_hook._used_material_names(_mesh(["Red"], [])) == []


def test_mesh_without_slots_uses_no_material():
    assert usd_hook._used_material_names(_mesh([], [0, 0])) == []


def test_empty_slot_is_ignored():
    mesh = _mesh([None, "BB"], [0, 1])

    assert usd_hook._used_material_names(mesh) == ["BB"]


def test_out_of_range_face_index_is_ignored():
    mesh = _mesh(["AA"], [0, 7])

    assert usd_hook._used_material_names(mesh) == ["AA"]
