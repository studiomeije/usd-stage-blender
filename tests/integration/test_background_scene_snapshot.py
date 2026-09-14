"""Live Blender 5.2 coverage for background-export scene snapshots."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_MARKER = "---BACKGROUND_SNAPSHOT_TEST_JSON---"


DRIVER_SOURCE = r'''
import json
import sys
import types
from pathlib import Path

import bpy

repo_root = Path(sys.argv[sys.argv.index("--") + 1])
scratch = Path(sys.argv[sys.argv.index("--") + 2])
marker = sys.argv[sys.argv.index("--") + 3]
sys.path.insert(0, str(repo_root))

from Plugin.ops.bake_export_operator import (  # noqa: E402
    _cleanup_scene_snapshot,
    _create_scene_snapshot,
    _serialize_settings,
)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def add_object(name):
    mesh = bpy.data.meshes.new(name + "Mesh")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.ops.ed.undo_push(message=name)
    return obj


def snapshot_object_names(snapshot):
    with bpy.data.libraries.load(str(snapshot), link=True) as (source, _target):
        return set(source.objects)


def snapshot_image_path(snapshot, image_name):
    with bpy.data.libraries.load(str(snapshot), link=True) as (source, target):
        if image_name not in source.images:
            raise AssertionError(f"snapshot omitted image {image_name}")
        target.images = [image_name]
    image = target.images[0]
    try:
        return Path(bpy.path.abspath(image.filepath, library=image.library))
    finally:
        bpy.data.images.remove(image)


results = {}

# A never-saved scene has no base for // paths and must fail explicitly.
reset()
image = bpy.data.images.new("Relative", width=1, height=1)
image.source = "FILE"
image.filepath = "//relative.png"
job = scratch / "never_saved_relative_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job)
except RuntimeError as exc:
    results["never_saved_relative_rejected"] = "relative external assets" in str(exc)
else:
    results["never_saved_relative_rejected"] = False
_cleanup_scene_snapshot(job)

# Never-saved scenes must snapshot without becoming the active mainfile.
reset()
add_object("NeverSavedEdit")
job = scratch / "never_saved_job"
job.mkdir(parents=True, exist_ok=True)
before_path = bpy.data.filepath
before_dirty = bpy.data.is_dirty
if not before_dirty:
    raise AssertionError("test setup did not mark never-saved scene dirty")
snapshot = _create_scene_snapshot(bpy.context, job)
results["never_saved_preserved"] = (
    bpy.data.filepath == before_path == "" and bpy.data.is_dirty == before_dirty
)
results["never_saved_captured"] = "NeverSavedEdit" in snapshot_object_names(snapshot)
_cleanup_scene_snapshot(job)
results["never_saved_cleanup"] = not snapshot.exists()

# Saved scenes must include later unsaved edits while keeping their active path.
reset()
original = scratch / "original.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(original), check_existing=False)
texture = scratch / "texture.png"
generated = bpy.data.images.new("GeneratedTexture", width=1, height=1)
generated.pixels[:] = [1.0, 0.0, 0.0, 1.0]
generated.filepath_raw = str(texture)
generated.file_format = "PNG"
generated.save()
bpy.data.images.remove(generated)
image = bpy.data.images.load(str(texture))
image.name = "SnapshotTexture"
image.use_fake_user = True
image.filepath = "//texture.png"
add_object("UnsavedAfterSave")
before_path = bpy.data.filepath
before_dirty = bpy.data.is_dirty
if not before_dirty:
    raise AssertionError("test setup did not mark saved scene dirty")
job = scratch / "saved_job"
job.mkdir(parents=True, exist_ok=True)
snapshot = _create_scene_snapshot(bpy.context, job)
results["saved_preserved"] = (
    bpy.data.filepath == before_path == str(original)
    and bpy.data.is_dirty == before_dirty
)
results["saved_captured"] = "UnsavedAfterSave" in snapshot_object_names(snapshot)
resolved_texture = snapshot_image_path(snapshot, "SnapshotTexture")
results["saved_relative_resolved"] = (
    resolved_texture.resolve() == texture.resolve() and resolved_texture.exists()
)

# Background settings must pin Blender-relative HDRI paths to the source file,
# not the private job directory that the worker loads later.
hdri = scratch / "studio.hdr"
hdri.write_bytes(b"HDR")
fake_settings = types.SimpleNamespace(
    bake_mode="LIT_IBL",
    bake_ibl_source="HDRI_FILE",
    bake_ibl_filepath="//studio.hdr",
    bl_rna=types.SimpleNamespace(
        properties=[
            types.SimpleNamespace(identifier="bake_mode"),
            types.SimpleNamespace(identifier="bake_ibl_source"),
            types.SimpleNamespace(identifier="bake_ibl_filepath"),
        ]
    ),
)
serialized = _serialize_settings(fake_settings, context=bpy.context)
results["background_hdri_path_pinned"] = (
    Path(serialized["bake_ibl_filepath"]).resolve() == hdri.resolve()
)
fake_settings.bake_mode = "LIT_ALBEDO"
fake_settings.bake_ibl_filepath = "//unused-missing.hdr"
serialized_unused = _serialize_settings(fake_settings, context=bpy.context)
results["unused_hdri_path_not_required"] = (
    serialized_unused["bake_ibl_filepath"] == "//unused-missing.hdr"
)
serialized_ui_bake = _serialize_settings(
    fake_settings,
    context=bpy.context,
    enable_texture_settings=True,
)
results["ui_bake_enables_visible_texture_settings"] = (
    serialized_ui_bake["export_texture_settings_enabled"] is True
)
_cleanup_scene_snapshot(job)
results["saved_cleanup"] = not snapshot.exists()

# Save Copy cannot serialize dirty pixels from an unpacked external image.
image = bpy.data.images["SnapshotTexture"]
image.pixels[:] = [0.0, 1.0, 0.0, 1.0]
image.update()
if not image.is_dirty:
    raise AssertionError("test setup did not mark external image dirty")
job = scratch / "dirty_image_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job)
except RuntimeError as exc:
    results["dirty_external_image_rejected"] = "Dirty image" in str(exc)
else:
    results["dirty_external_image_rejected"] = False
_cleanup_scene_snapshot(job)

image.pack()
image.pixels[:] = [0.0, 0.0, 1.0, 1.0]
image.update()
if not image.is_dirty or image.packed_file is None:
    raise AssertionError("test setup did not create a dirty packed image")
job = scratch / "dirty_packed_image_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job)
except RuntimeError as exc:
    results["dirty_packed_image_rejected"] = "Dirty image" in str(exc)
else:
    results["dirty_packed_image_rejected"] = False
_cleanup_scene_snapshot(job)

bpy.data.images.remove(image)
generated = bpy.data.images.new("DirtyGenerated", width=1, height=1)
generated.pixels[:] = [1.0, 1.0, 0.0, 1.0]
generated.update()
if not generated.is_dirty:
    raise AssertionError("test setup did not create a dirty generated image")

# Scoped snapshots may ignore an unrelated dirty buffer, but Blender 5.2's
# typed Geometry Nodes modifier input makes the same image export-reachable.
scoped_object = bpy.data.objects["UnsavedAfterSave"]
job = scratch / "dirty_unreferenced_image_job"
job.mkdir(parents=True, exist_ok=True)
snapshot = _create_scene_snapshot(bpy.context, job, objects=[scoped_object])
results["dirty_unreferenced_image_allowed"] = snapshot.is_file()
_cleanup_scene_snapshot(job)

legacy_texture = bpy.data.textures.new("DirtyDisplaceTexture", type="IMAGE")
legacy_texture.image = generated
displace = scoped_object.modifiers.new("DirtyImageDisplace", "DISPLACE")
displace.texture = legacy_texture
job = scratch / "dirty_displace_image_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job, objects=[scoped_object])
except RuntimeError as exc:
    results["dirty_displace_image_rejected"] = "DirtyGenerated" in str(exc)
else:
    results["dirty_displace_image_rejected"] = False
_cleanup_scene_snapshot(job)
scoped_object.modifiers.remove(displace)
bpy.data.textures.remove(legacy_texture)

world = bpy.data.worlds.new("DirtyImageWorld")
environment = world.node_tree.nodes.new("ShaderNodeTexEnvironment")
environment.image = generated
bpy.context.scene.world = world

unlit_settings = types.SimpleNamespace(
    bake_mode="UNLIT_ALBEDO",
    bake_ibl_source="SCENE_WORLD",
)
job = scratch / "dirty_unused_world_unlit_job"
job.mkdir(parents=True, exist_ok=True)
snapshot = _create_scene_snapshot(
    bpy.context,
    job,
    objects=[scoped_object],
    settings=unlit_settings,
)
results["dirty_world_ignored_for_unlit"] = snapshot.is_file()
_cleanup_scene_snapshot(job)

explicit_hdri_settings = types.SimpleNamespace(
    bake_mode="LIT_IBL",
    bake_ibl_source="HDRI_FILE",
)
job = scratch / "dirty_unused_world_explicit_hdri_job"
job.mkdir(parents=True, exist_ok=True)
snapshot = _create_scene_snapshot(
    bpy.context,
    job,
    objects=[scoped_object],
    settings=explicit_hdri_settings,
)
results["dirty_world_ignored_for_explicit_hdri"] = snapshot.is_file()
_cleanup_scene_snapshot(job)

scene_world_settings = types.SimpleNamespace(
    bake_mode="LIT_IBL",
    bake_ibl_source="SCENE_WORLD",
)
job = scratch / "dirty_active_world_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(
        bpy.context,
        job,
        objects=[scoped_object],
        settings=scene_world_settings,
    )
except RuntimeError as exc:
    results["dirty_active_world_rejected"] = "DirtyGenerated" in str(exc)
else:
    results["dirty_active_world_rejected"] = False
_cleanup_scene_snapshot(job)
bpy.context.scene.world = None
bpy.data.worlds.remove(world)

node_group = bpy.data.node_groups.new("DirtyImageGeometry", "GeometryNodeTree")
node_group.interface.new_socket(
    name="Geometry",
    in_out="INPUT",
    socket_type="NodeSocketGeometry",
)
image_socket = node_group.interface.new_socket(
    name="Image",
    in_out="INPUT",
    socket_type="NodeSocketImage",
)
modifier = scoped_object.modifiers.new("DirtyImageGeometry", "NODES")
modifier.node_group = node_group
getattr(modifier.properties.inputs, image_socket.identifier).value = generated
job = scratch / "dirty_geometry_nodes_image_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job, objects=[scoped_object])
except RuntimeError as exc:
    results["dirty_geometry_nodes_image_rejected"] = "DirtyGenerated" in str(exc)
else:
    results["dirty_geometry_nodes_image_rejected"] = False
_cleanup_scene_snapshot(job)

job = scratch / "dirty_generated_image_job"
job.mkdir(parents=True, exist_ok=True)
try:
    _create_scene_snapshot(bpy.context, job)
except RuntimeError as exc:
    results["dirty_generated_image_rejected"] = "DirtyGenerated" in str(exc)
else:
    results["dirty_generated_image_rejected"] = False
_cleanup_scene_snapshot(job)

print(marker)
print(json.dumps(results, sort_keys=True))
print(marker)
'''


def test_background_snapshot_preserves_active_scene_and_unsaved_edits(tmp_path):
    driver = tmp_path / "snapshot_driver.py"
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
    assert results == {
        "never_saved_cleanup": True,
        "never_saved_captured": True,
        "never_saved_preserved": True,
        "never_saved_relative_rejected": True,
        "saved_cleanup": True,
        "saved_captured": True,
        "saved_preserved": True,
        "saved_relative_resolved": True,
        "background_hdri_path_pinned": True,
        "unused_hdri_path_not_required": True,
        "ui_bake_enables_visible_texture_settings": True,
        "dirty_active_world_rejected": True,
        "dirty_displace_image_rejected": True,
        "dirty_external_image_rejected": True,
        "dirty_packed_image_rejected": True,
        "dirty_generated_image_rejected": True,
        "dirty_geometry_nodes_image_rejected": True,
        "dirty_unreferenced_image_allowed": True,
        "dirty_world_ignored_for_explicit_hdri": True,
        "dirty_world_ignored_for_unlit": True,
    }
