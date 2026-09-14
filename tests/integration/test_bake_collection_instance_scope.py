"""Live Blender regression for selected collection-instance bake scope."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_MARKER = "---BAKE_COLLECTION_SCOPE_TEST_JSON---"


DRIVER_SOURCE = r'''
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

repo_root = Path(sys.argv[sys.argv.index("--") + 1])
scratch = Path(sys.argv[sys.argv.index("--") + 2])
marker = sys.argv[sys.argv.index("--") + 3]
sys.path.insert(0, str(repo_root))

bpy.ops.wm.read_factory_settings(use_empty=True)

import Plugin  # noqa: E402

Plugin.register()

from Plugin.api.commands import bake_export as command  # noqa: E402
from Plugin.export import (  # noqa: E402
    asset_preflight,
    bake_finalize,
    bake_textures,
    blender_usd_export,
    postprocess_usd,
    support_bundle,
)

scene = bpy.context.scene

# The source collection is deliberately not scene-linked, matching Blender's
# normal Add > Collection Instance topology.
source = bpy.data.collections.new("PrototypeCollection")
parent = bpy.data.objects.new("PrototypeParent", None)
source.objects.link(parent)
mesh = bpy.data.meshes.new("PrototypeMeshData")
mesh.from_pydata(
    [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)],
    [],
    [(0, 1, 2)],
)
mesh.uv_layers.new(name="UVMap")
prototype = bpy.data.objects.new("PrototypeMesh", mesh)
prototype.parent = parent
material = bpy.data.materials.new("PrototypeMaterial")
prototype.data.materials.append(material)
source.objects.link(prototype)

instance = bpy.data.objects.new("SelectedInstance", None)
instance.instance_type = "COLLECTION"
instance.instance_collection = source
scene.collection.objects.link(instance)
instance.select_set(True)
bpy.context.view_layer.objects.active = instance

settings = scene.usd_stage_export_settings
settings.selected_objects_only = True
settings.export_format = "USDC"
settings.export_animation = False
settings.diagnostics_enabled = False

observed = {
    "preflight_scopes": [],
    "bake_scopes": [],
    "bake_links": [],
    "native_scopes": [],
}
fail_bake = [False]


def names(objects):
    return [obj.name for obj in objects]


def in_scene(obj):
    return scene.objects.get(obj.name) == obj


def fake_preflight(objects, _bpy):
    observed["preflight_scopes"].append(names(objects))
    return []


def fake_bake(_context, _settings, objects, _output, _diag, **_kwargs):
    scope = list(objects)
    observed["bake_scopes"].append(names(scope))
    observed["bake_links"].append({
        obj.name: in_scene(obj) for obj in scope
    })
    if fail_bake[0]:
        raise RuntimeError("intentional bake failure")
    # The real BakeResult, not a SimpleNamespace: this double only overrides
    # *which objects* get baked, so it must satisfy every attribute the command
    # reads afterwards. A bare namespace silently drifts as the command grows
    # new consumers (it did - bake_export now iterates result.baked_images).
    return bake_textures.BakeResult()


def fake_export(context, _settings, _filepath, _diag, **_kwargs):
    observed["native_scopes"].append({
        "selected": sorted(obj.name for obj in context.selected_objects),
        "prototype_linked": in_scene(prototype),
        "parent_linked": in_scene(parent),
    })
    # The real exporter writes the root layer inside the staging directory it
    # is handed, and the bake-output cleanup that follows requires exactly
    # that. Writing to `scratch` instead makes the command fail with
    # "bake texture cleanup requires the USD layer inside its owned staging
    # directory" - a defect in this double, not in the code under test.
    staging = Path(_kwargs["staging_dir"])
    # .usda, not .usdc: the content below is ASCII USD, and downstream stages
    # really open this layer. A crate extension on ASCII bytes fails with
    # "File too small to contain bootstrap structure".
    temp = staging / "native.usda"
    temp.write_text('#usda 1.0\n(\n    defaultPrim = "Root"\n)\n\ndef Xform "Root"\n{\n}\n')
    return str(temp)


asset_preflight.collect_missing_image_files_for_objects = fake_preflight
bake_textures.bake_materials_for_objects = fake_bake
bake_textures.restore_baked_materials = lambda *_args, **_kwargs: None
bake_finalize.apply_force_unlit = lambda _settings: None
support_bundle.collect_environment = lambda _context: {}
support_bundle.collect_scene_snapshot = lambda _context: {}
blender_usd_export._reset_export_staging_dir = (
    lambda path, _diag: Path(path).mkdir(parents=True, exist_ok=True)
)
blender_usd_export.export_blender_scene = fake_export
blender_usd_export.publish_unpacked_export = (
    lambda _source, destination, _diag: Path(destination).write_text("#usda 1.0\n")
)
blender_usd_export.remove_export_staging_dir = lambda *_args, **_kwargs: None
postprocess_usd.process_usd_stage = lambda *_args, **_kwargs: None

output = scratch / "collection-instance.usdc"
result = command.handle({
    "filepath": str(output),
    "format": "USDC",
    "selected_only": True,
    "overrides": {},
})

processing_expected = {
    "SelectedInstance",
    "PrototypeParent",
    "PrototypeMesh",
}
success_scope = observed["bake_scopes"][0]
success = {
    "preflight_dependency_closed": (
        set(observed["preflight_scopes"][0]) == processing_expected
    ),
    "bake_dependency_closed": set(success_scope) == processing_expected,
    "bake_scope_deduplicated": len(success_scope) == len(set(success_scope)),
    "prototype_linked_for_operator": observed["bake_links"][0]["PrototypeMesh"],
    "parent_linked_for_operator": observed["bake_links"][0]["PrototypeParent"],
    "native_selection_exact": (
        observed["native_scopes"][0]["selected"] == ["SelectedInstance"]
    ),
    "native_prototype_unlinked": not observed["native_scopes"][0]["prototype_linked"],
    "native_parent_unlinked": not observed["native_scopes"][0]["parent_linked"],
    "success_cleanup": not in_scene(prototype) and not in_scene(parent),
    "mesh_bake_stat": result["bake_stats"]["objects_baked"] == 1,
}

# A mid-bake exception must run the same unlink/selection transaction.
fail_bake[0] = True
try:
    command.handle({
        "filepath": str(scratch / "collection-instance-failure.usdc"),
        "format": "USDC",
        "selected_only": True,
        "overrides": {},
    })
except Exception as exc:
    failure_raised = "intentional bake failure" in str(exc)
else:
    failure_raised = False

success.update({
    "failure_raised": failure_raised,
    "failure_scope_dependency_closed": (
        set(observed["bake_scopes"][1]) == processing_expected
    ),
    "failure_cleanup": not in_scene(prototype) and not in_scene(parent),
    "selection_restored": (
        instance.select_get()
        and bpy.context.view_layer.objects.active == instance
        and set(obj.name for obj in bpy.context.selected_objects) == {"SelectedInstance"}
    ),
})

print(marker)
print(json.dumps(success, sort_keys=True))
print(marker)
'''


def test_selected_collection_instance_uses_dependency_closed_bake_scope(tmp_path):
    driver = tmp_path / "bake_collection_scope_driver.py"
    driver.write_text(DRIVER_SOURCE)
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    proc = subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(driver),
            "--",
            str(REPO_ROOT),
            str(tmp_path),
            OUTPUT_MARKER,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    chunks = proc.stdout.split(OUTPUT_MARKER)
    assert len(chunks) >= 3, proc.stdout + proc.stderr
    results = json.loads(chunks[-2].strip())
    assert results and all(results.values()), results
