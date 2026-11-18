"""Blender 5.3 native USD features exercised through USD Stage's export path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r'''
import bpy, json, sys
from pathlib import Path
from mathutils import Matrix
from pxr import Usd, UsdGeom

repo, work = map(Path, sys.argv[-2:])
sys.path.insert(0, str(repo))
import Plugin
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)

# The standard export automatically inherits Blender 5.3's normals and
# matrix primvars. The plug-in must preserve them through post-processing.
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
for face in obj.data.polygons:
    face.use_smooth = True
attribute = obj.data.attributes.new('source_transform', 'FLOAT4X4', 'POINT')
for item in attribute.data:
    item.value = Matrix.Identity(4)
scene = bpy.context.scene
scene.render.fps = 30
scene.frame_start, scene.frame_end = 1, 2
obj.keyframe_insert(data_path='location', frame=1)
obj.location.x = 1
obj.keyframe_insert(data_path='location', frame=2)
settings = scene.usd_stage_export_settings
settings.export_format = 'USDA'
settings.export_animation = True
assert bpy.ops.usdstage.export(filepath=str(work / 'native.usda')) == {'FINISHED'}

# A collection importer is a persistent external source, not a second export
# implementation. Reload it, reopen the .blend, and run our normal exporter.
bpy.ops.wm.read_factory_settings(use_empty=True)
source = work / 'source.usda'
def write_source(offset):
    stage = Usd.Stage.CreateNew(str(source))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    root = UsdGeom.Xform.Define(stage, '/Asset')
    stage.SetDefaultPrim(root.GetPrim())
    mesh = UsdGeom.Mesh.Define(stage, '/Asset/Triangle')
    mesh.CreatePointsAttr([(offset, 0, 0), (offset + 1, 0, 0), (offset, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    stage.GetRootLayer().Save()

write_source(0)
collection = bpy.data.collections.new('SourceAsset')
bpy.context.scene.collection.children.link(collection)
with bpy.context.temp_override(collection=collection):
    assert bpy.ops.collection.importer_add(
        name='IO_FH_usd', filepath=str(source)
    ) == {'FINISHED'}
    collection.importer.import_properties.import_materials = False
    assert bpy.ops.collection.importer_import() == {'FINISHED'}
write_source(4)
with bpy.context.temp_override(collection=collection):
    assert bpy.ops.collection.importer_reload() == {'FINISHED'}
bpy.ops.wm.save_as_mainfile(filepath=str(work / 'ingest.blend'))
bpy.ops.wm.open_mainfile(filepath=str(work / 'ingest.blend'))
collection = bpy.data.collections['SourceAsset']
meshes = [obj for obj in collection.all_objects if obj.type == 'MESH']
report = {
    'source': collection.importer.filepath,
    'import_materials': collection.importer.import_properties.import_materials,
    'linked': [obj.library is not None for obj in meshes],
    'points': [tuple(v.co) for v in meshes[0].data.vertices],
}
settings = bpy.context.scene.usd_stage_export_settings
settings.export_format = 'USDA'
assert bpy.ops.usdstage.export(filepath=str(work / 'reexport.usda')) == {'FINISHED'}
(work / 'report.json').write_text(json.dumps(report))
'''


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    work = tmp_path_factory.mktemp("blender53_usd")
    script = work / "probe.py"
    script.write_text(_PROBE)
    result = subprocess.run(
        [
            blender_executable(), "--background", "--factory-startup",
            "--python-exit-code", "1", "--python", str(script), "--",
            str(REPO_ROOT), str(work),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return work


def _mesh(stage):
    from pxr import UsdGeom

    return next(UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh))


def test_smooth_normals_remain_per_vertex(outputs):
    from pxr import Usd

    stage = Usd.Stage.Open(str(outputs / "native.usda"))
    mesh = _mesh(stage)
    assert mesh.GetNormalsInterpolation() == "vertex"
    assert len(mesh.GetNormalsAttr().Get()) == len(mesh.GetPointsAttr().Get()) == 8


def test_matrix_primvars_survive_postprocessing(outputs):
    from pxr import Gf, Usd, UsdGeom

    stage = Usd.Stage.Open(str(outputs / "native.usda"))
    primvar = UsdGeom.PrimvarsAPI(_mesh(stage)).GetPrimvar("source_transform")
    assert str(primvar.GetTypeName()) == "matrix4d[]"
    assert list(primvar.Get()) == [Gf.Matrix4d(1)] * 8


def test_animation_playback_rate_is_preserved(outputs):
    from pxr import Usd

    stage = Usd.Stage.Open(str(outputs / "native.usda"))
    assert stage.GetFramesPerSecond() == stage.GetTimeCodesPerSecond() == 30


def test_collection_importer_reloads_and_persists_settings(outputs):
    report = json.loads((outputs / "report.json").read_text())
    assert Path(report["source"]) == outputs / "source.usda"
    assert report["import_materials"] is False
    assert report["linked"] == [True]
    assert report["points"] == [[4, 0, 0], [5, 0, 0], [4, 1, 0]]


def test_collection_importer_geometry_can_be_exported(outputs):
    from pxr import Usd

    stage = Usd.Stage.Open(str(outputs / "reexport.usda"))
    assert [tuple(point) for point in _mesh(stage).GetPointsAttr().Get()] == [
        (4, 0, 0), (5, 0, 0), (4, 1, 0),
    ]
