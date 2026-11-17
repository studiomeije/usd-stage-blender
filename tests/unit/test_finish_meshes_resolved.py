"""Validation outside an export resolves the finish meshes at most once.

Each node that reads a UV map or attribute asks which meshes a finish offers
its material. Without a scope every ask would resolve the variant plan;
inside :func:`finish_meshes_resolved` it is resolved on the first ask and
kept, an inner scope - one per validated material - reuses the outer one,
and a pass in which nothing reads mesh data resolves nothing.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

# The validator imports bpy at module level; outside Blender a stub will do.
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.export import pipeline, variant_scope  # noqa: E402
from Plugin.export import variants as variants_module  # noqa: E402
from Plugin.export.materials.extract import readers  # noqa: E402
from Plugin.export.variants import VariantPlan  # noqa: E402
from Plugin.nodes import validate as rk_validate  # noqa: E402


def _context():
    return SimpleNamespace(scene=SimpleNamespace(usd_stage_export_settings=SimpleNamespace()))


@pytest.fixture
def resolutions(monkeypatch) -> list:
    calls = []

    def resolve(context, settings):
        calls.append(context)
        return VariantPlan()

    monkeypatch.setattr(variant_scope, "effective_variant_plan", resolve)
    return calls


def test_without_a_scope_every_lookup_resolves_the_plan(resolutions):
    context = _context()

    for _ in range(3):
        readers._finish_target_meshes(context, [])

    assert len(resolutions) == 3


def test_a_scope_resolves_the_plan_once_for_every_lookup(resolutions):
    context = _context()

    with readers.finish_meshes_resolved(context):
        for _ in range(3):
            readers._finish_target_meshes(context, [])

    assert len(resolutions) == 1


def test_an_inner_scope_reuses_the_outer_one(resolutions):
    context = _context()

    with readers.finish_meshes_resolved(context):
        for _ in range(3):  # one per validated material
            with readers.finish_meshes_resolved(context):
                readers._finish_target_meshes(context, [])

    assert len(resolutions) == 1
    assert readers._FINISH_MESHES.get() is None


def test_a_scope_nothing_reads_mesh_data_in_resolves_nothing(resolutions):
    with readers.finish_meshes_resolved(_context()):
        pass

    assert resolutions == []


def test_an_unworkable_export_scope_falls_back_to_every_declared_set(monkeypatch):
    """Selected-only with a broken rig: the export refuses on its own, but
    validation still has to answer, so it checks against every set."""
    def broken(context, settings):
        raise RuntimeError("Armature modifier on 'Seat' has no target")

    full = []
    monkeypatch.setattr(variant_scope, "effective_variant_plan", broken)
    monkeypatch.setattr(variants_module, "resolve_variant_plan",
                        lambda context, settings: full.append(1) or VariantPlan())

    assert readers._finish_target_meshes(_context(), []) == []
    assert full == [1]


def _reading_validator(reads: int):
    """A stand-in for the validator body: ``reads`` nodes that read mesh data."""
    def validate(material, only_connected, strict, clamp_specular_tint):
        for _ in range(reads):
            readers._finish_target_meshes(None, [])
        return {"material": material.name, "ok": True, "errors": [], "warnings": []}
    return validate


def test_validate_material_resolves_once_however_many_nodes_read(monkeypatch, resolutions):
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(context=_context()))
    monkeypatch.setattr(rk_validate, "_validate_material", _reading_validator(4))

    rk_validate.validate_material(SimpleNamespace(name="Painted"))

    assert len(resolutions) == 1


def test_a_material_reading_no_mesh_data_resolves_nothing(monkeypatch, resolutions):
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(context=_context()))
    monkeypatch.setattr(rk_validate, "_validate_material", _reading_validator(0))

    rk_validate.validate_material(SimpleNamespace(name="Plain"))

    assert resolutions == []


def test_the_export_validation_pass_resolves_once_for_every_material(monkeypatch, resolutions):
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(context=_context()))
    monkeypatch.setattr(rk_validate, "_validate_material", _reading_validator(3))
    materials = [SimpleNamespace(name=f"Mat{index}") for index in range(5)]
    objects = [SimpleNamespace(material_slots=[SimpleNamespace(material=m) for m in materials])]

    pipeline.validate_materials(_context(), SimpleNamespace(), objects, SimpleNamespace())

    assert len(resolutions) == 1


# --- naming what lacks a read ----------------------------------------------

def _scene(monkeypatch, objects):
    scene = SimpleNamespace(objects=objects)
    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(context=SimpleNamespace(scene=scene)))


def _meshes(monkeypatch, meshes):
    monkeypatch.setattr(readers._core, "_material_meshes", lambda node: meshes)


def _lacks(mesh) -> bool:
    return False


def test_one_object_is_named_in_the_singular(monkeypatch):
    mesh = SimpleNamespace(name="Cube.001")
    _scene(monkeypatch, [SimpleNamespace(name="Back", data=mesh)])
    _meshes(monkeypatch, [mesh])

    assert readers._lacking_meshes(None, _lacks) == (
        "object 'Back', which uses this material or is offered it by a finish variant"
    )


def test_many_objects_are_capped_and_take_the_plural(monkeypatch):
    meshes = [SimpleNamespace(name=f"Mesh{index}") for index in range(6)]
    _scene(monkeypatch, [SimpleNamespace(name=f"Chair{index}", data=mesh)
                         for index, mesh in enumerate(meshes)])
    _meshes(monkeypatch, meshes)

    assert readers._lacking_meshes(None, _lacks) == (
        "objects 'Chair0', 'Chair1', 'Chair2' and 3 more, "
        "which use this material or are offered it by a finish variant"
    )


def test_a_mesh_no_scene_object_uses_is_named_by_its_data(monkeypatch):
    mesh = SimpleNamespace(name="Orphan")
    elsewhere = SimpleNamespace(name="Other")
    _scene(monkeypatch, [SimpleNamespace(name="Unrelated", data=elsewhere)])
    _meshes(monkeypatch, [mesh])

    assert readers._lacking_meshes(None, _lacks).startswith("mesh 'Orphan', which uses")


def test_no_mesh_using_the_material_is_said_plainly(monkeypatch):
    _meshes(monkeypatch, [])

    assert readers._lacking_meshes(None, _lacks) == "any mesh using this material"
