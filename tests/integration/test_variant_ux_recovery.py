"""Real Blender coverage for recoverable variant editing and artist previews."""

import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import bpy, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[-1])
import Plugin
from Plugin.ops.variant_operators import USDSTAGE_OT_variant_preview
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
parent = bpy.data.collections.new('Options');scene.collection.children.link(parent)
left = bpy.data.collections.new('Left');right = bpy.data.collections.new('Right')
parent.children.link(left);parent.children.link(right)
detail = bpy.data.collections.new('Detail');right.children.link(detail)
bpy.ops.mesh.primitive_cube_add();obj = bpy.context.object
for collection in list(obj.users_collection):collection.objects.unlink(obj)
detail.objects.link(obj)
entry = scene.usd_stage_variant_sets.add();entry.name='Geometry';entry.default_variant='Left'
for name, collection in [('Left',left),('Right',right)]:
    variant=entry.variants.add();variant.name=name;variant.collection=collection
layer=bpy.context.view_layer.layer_collection.children['Options']
layer.children['Right'].exclude=True
# A missing source cannot hide the currently visible alternative.
entry.variants[1].collection=None
assert bpy.ops.usdstage.variant_preview(variant='Right')=={'CANCELLED'}
assert not layer.children['Left'].exclude and layer.children['Right'].exclude
entry.variants[1].collection=right
# An excluded ancestor and nested collection are made visible as one preview.
layer.children['Right'].exclude=False
obj.hide_set(True);obj.hide_viewport=True;right.hide_viewport=True
layer.children['Right'].children['Detail'].exclude=True
layer.exclude=True
assert bpy.ops.usdstage.variant_preview(variant='Right')=={'FINISHED'}
assert not layer.exclude and not layer.children['Right'].exclude
assert not layer.children['Right'].children['Detail'].exclude
assert not right.hide_viewport and not obj.hide_viewport and not obj.hide_get()
assert layer.children['Left'].exclude
assert entry.default_variant=='Left'
# A late failure also restores visibility, rather than leaving a half-preview.
layer.children['Left'].exclude=False;layer.children['Right'].exclude=True
before=[child.exclude for child in layer.children]
calls=[]
def fail_update_once():
    calls.append(True)
    if len(calls)==1:raise RuntimeError('view layer refresh failed')
    bpy.context.view_layer.update()
fake_context=SimpleNamespace(scene=scene,view_layer=SimpleNamespace(
    layer_collection=bpy.context.view_layer.layer_collection,update=fail_update_once))
fake_operator=SimpleNamespace(variant='Right',report=lambda *_:None)
assert USDSTAGE_OT_variant_preview.execute(fake_operator,fake_context)=={'CANCELLED'}
assert [child.exclude for child in layer.children]==before
# A default must name a real entry.
assert bpy.ops.usdstage.variant_set_default(variant='Deleted')=={'CANCELLED'}
assert entry.default_variant=='Left'

material=bpy.data.materials.new('Artist Material')
other=bpy.data.materials.new('Another Surface')
obj.data.materials.append(material)
nested=scene.usd_stage_variant_sets.add();nested.name='Materials';nested.kind='MATERIAL'
nested.parent_set='Geometry';nested.target_collection=right;nested.default_variant='Artist'
v=nested.variants.add();v.name='Artist';v.material=material;v.applies_to='Left,Right'
exclusive=nested.variants.add();exclusive.name='Exclusive';exclusive.material=other;exclusive.applies_to='Left'
scene.usd_stage_variant_sets_index=0;entry.variants_index=0
# Do not silently broaden an exclusive material when its geometry is removed.
assert bpy.ops.usdstage.variant_remove()=={'CANCELLED'}
assert len(entry.variants)==2 and exclusive.applies_to=='Left'
exclusive.applies_to='Right'
assert bpy.ops.usdstage.variant_remove()=={'FINISHED'}
assert [v.name for v in entry.variants]==['Right']
assert nested.variants[0].applies_to=='Right' and nested.variants[1].applies_to=='Right'
scene.usd_stage_variant_sets_index=1
# Selecting the existing parent preserves restrictions; invalid parents are refused.
assert bpy.ops.usdstage.variant_set_parent(parent='Geometry')=={'FINISHED'}
assert nested.variants[0].applies_to=='Right'
assert bpy.ops.usdstage.variant_set_parent(parent='Materials')=={'CANCELLED'}
assert bpy.ops.usdstage.variant_set_parent(parent='')=={'FINISHED'}
assert not nested.parent_set and all(not v.applies_to for v in nested.variants)

# Refresh protects custom artist thumbnails, even when a normal thumbnail is refreshed too.
preview=material.preview_ensure();preview.image_size=(2,2)
pixels=[1.0,0.2,0.1,1.0]*4;preview.image_pixels_float=pixels
before=list(preview.image_pixels_float)
assert preview.is_image_custom
assert bpy.ops.usdstage.variant_generate_previews()=={'FINISHED'}
assert list(material.preview.image_pixels_float)==before

# Explicit material picking does not depend on a shared naming prefix.
assert bpy.ops.usdstage.variant_set_add_finish(
    target_name='Right',picker_initialized=True,selection_mode='PICK',
    material_choices=[{'name':mat.name,'material_name':mat.name,'selected':True} for mat in (material,other)]
)=={'FINISHED'}
made=scene.usd_stage_variant_sets[scene.usd_stage_variant_sets_index]
assert [v.material.name for v in made.variants]==['Artist Material','Another Surface']
assert made.name=='Right_Materials'
assert made.default_variant=='Artist_Material'
Plugin.unregister()
print('VARIANT_UX_OK')
'''


def test_variant_recovery_and_explicit_material_picking(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    result = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python-exit-code", "1",
         "--python", str(script), "--", str(REPO_ROOT)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VARIANT_UX_OK" in result.stdout
