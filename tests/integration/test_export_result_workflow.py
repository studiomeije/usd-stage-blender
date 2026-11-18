"""Real operator results remain readable and never reuse stale diagnostics."""

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

repo, output = sys.argv[-2:]
sys.path.insert(0, repo)
import Plugin
from Plugin.ops.export_results import (
    diagnostic_errors, get_latest_result, resolve_diagnostics_path,
)
from Plugin.ops.export_operator import USDSTAGE_OT_export, _display_text, _resolve_diagnostics_path
from Plugin.ui.panel import _existing_diagnostics_path

bpy.ops.wm.read_factory_settings(use_empty=True)
Plugin.register()
settings = bpy.context.scene.usd_stage_export_settings
settings.history_applied = True
settings.export_format = 'USDA'
settings.filepath = str(Path(output) / 'result.usda')
settings.diagnostics_enabled = False
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.object
material = bpy.data.materials.new('ResultMaterial')
material.use_nodes = True
obj.data.materials.append(material)

assert bpy.ops.usdstage.export() == {'FINISHED'}
success = get_latest_result(settings)
assert success['state'] == 'done', success
assert success['export_path'] == settings.filepath
assert Path(settings.filepath).is_file()
assert resolve_diagnostics_path(settings) == ''
assert success['data']['materials']['converted'] >= 1
first_output = Path(settings.filepath).read_bytes()

# An actual material refusal persists both the summary and node-level detail.
nodes = material.node_tree.nodes
output_node = next(node for node in nodes if node.type == 'OUTPUT_MATERIAL')
unsupported = nodes.new('ShaderNodeBsdfGlass')
unsupported.name = 'Unsupported Glass'
material.node_tree.links.new(unsupported.outputs[0], output_node.inputs['Surface'])
try:
    outcome = bpy.ops.usdstage.export()
except RuntimeError:
    # Blender converts an operator ERROR report into a Python exception.
    outcome = {'CANCELLED'}
assert outcome == {'CANCELLED'}
failure = get_latest_result(settings)
assert failure['state'] == 'error', failure
assert failure['time'] >= success['time']
assert failure['errors'] and 'ResultMaterial' in failure['errors'][0]
details = diagnostic_errors(failure['data'])
assert any('Unsupported Glass' in error for error in details), details
diagnostics_path = resolve_diagnostics_path(settings)
assert diagnostics_path and Path(diagnostics_path).is_file()
assert _resolve_diagnostics_path(bpy.context) == diagnostics_path
assert _existing_diagnostics_path(settings) == diagnostics_path
assert Path(settings.filepath).read_bytes() == first_output

# Recovery produces a new success result, even though the old failure sidecar
# still exists at the same path and successful sidecars are disabled.
principled = next(node for node in nodes if node.type == 'BSDF_PRINCIPLED')
material.node_tree.links.new(principled.outputs['BSDF'], output_node.inputs['Surface'])
assert bpy.ops.usdstage.export() == {'FINISHED'}
recovered = get_latest_result(settings)
assert recovered['state'] == 'done'
assert recovered['time'] >= failure['time']
assert recovered['errors'] == []
assert Path(diagnostics_path).is_file()
assert resolve_diagnostics_path(settings) == ''
assert _resolve_diagnostics_path(bpy.context) is None
assert _existing_diagnostics_path(settings) == ''

# Exercise the picker-to-execute handoff on a real Blender-created Operator:
# bpy operators cannot be constructed directly like regular Python classes.
old_path = settings.filepath
chosen_path = str(Path(output) / 'picked.wrong')
class USDSTAGE_OT_picker_handoff_probe(USDSTAGE_OT_export):
    bl_idname = 'usdstage.picker_handoff_probe'
    bl_label = 'Picker Handoff Probe'
    def execute(self, context):
        self._choosing_destination = True
        self.filepath = chosen_path
        return super().execute(context)

bpy.utils.register_class(USDSTAGE_OT_picker_handoff_probe)
try:
    assert bpy.ops.usdstage.picker_handoff_probe() == {'FINISHED'}
finally:
    bpy.utils.unregister_class(USDSTAGE_OT_picker_handoff_probe)
assert settings.filepath == str(Path(output) / 'picked.usda')
assert Path(settings.filepath).is_file()
assert settings.filepath != old_path

# Background callers can still load report text without inventing a window.
report_text = bpy.data.texts.new('Background report')
report_text.write('Retained report text')
assert _display_text(bpy.context, report_text) is False
assert report_text.as_string() == 'Retained report text'

property_definition = settings.bl_rna.properties['last_export_result']
assert property_definition.is_skip_save
# Blender's nested property-group ID values can outlive SKIP_SAVE on reopen;
# the add-on's load handler must reset this explicitly as session feedback.
blend_file = str(Path(output) / 'result.blend')
bpy.ops.wm.save_as_mainfile(filepath=blend_file)
bpy.ops.wm.open_mainfile(filepath=blend_file)
assert bpy.context.scene.usd_stage_export_settings.last_export_result == ''

print('RESULT_JSON', json.dumps({
    'success': success['state'], 'failure': failure['state'],
    'recovered': recovered['state'], 'material_errors': details,
    'skip_save': True,
}))
Plugin.unregister()
'''


def test_direct_export_result_and_recovery(tmp_path):
    script = tmp_path / 'probe.py'
    script.write_text(_PROBE)
    result = subprocess.run(
        [blender_executable(), '--background', '--factory-startup',
         '--python-exit-code', '1', '--python', str(script), '--',
         str(REPO_ROOT), str(tmp_path)],
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith('RESULT_JSON '))
    report = json.loads(line.removeprefix('RESULT_JSON '))
    assert (report['success'], report['failure'], report['recovered']) == ('done', 'error', 'done')
    assert report['skip_save']
