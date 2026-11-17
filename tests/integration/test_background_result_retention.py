"""Cancel and dismiss preserve the newest result after detaching the worker."""

import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import bpy, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[-1])
import Plugin
from Plugin.ops import bake_export_operator as jobs
from Plugin.ops.export_results import get_latest_result, record_result, resolve_diagnostics_path
Plugin.register()
bpy.ops.wm.read_factory_settings(use_empty=True)
settings = bpy.context.scene.usd_stage_export_settings
settings.history_applied = True

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    job = root / 'job'; job.mkdir()
    report = root / 'report.json'; report.write_text('{"warnings": ["current"]}')
    def attach(status):
        record_result(settings, state='done', export_path='old.usdz', timestamp=1)
        settings.background_job_dir = str(job)
        settings.background_job_pid = 123
        (job/'status.json').write_text(json.dumps(status))
    current = {'state':'done', 'time':2, 'pid':123, 'export_path':'bake.usdz',
               'diagnostics_path':str(report), 'log_path':'bake.log', 'warnings':['current']}
    attach(current)
    assert bpy.ops.usdstage.clear_bake_job() == {'FINISHED'}
    assert settings.background_job_dir == ''
    retained = get_latest_result(settings)
    assert retained == current, retained
    assert resolve_diagnostics_path(settings) == str(report)
    # An older terminal background job must not replace a later direct export.
    attach(current)
    record_result(settings, state='done', export_path='new.usdz', timestamp=3)
    assert bpy.ops.usdstage.clear_bake_job() == {'FINISHED'}
    assert get_latest_result(settings)['export_path'] == 'new.usdz'
    # A stale worker becomes an error, never the old direct success.
    attach({**current, 'state':'running'})
    jobs.pid_is_running = lambda *_: False
    assert bpy.ops.usdstage.clear_bake_job() == {'FINISHED'}
    assert get_latest_result(settings)['state'] == 'error'
    # Canceling an active worker preserves its metadata and all warning text.
    attach({**current, 'state':'running'})
    alive = {123}
    jobs.pid_is_running = lambda pid: pid in alive
    jobs._terminate_process = lambda pid: alive.discard(pid)
    assert bpy.ops.usdstage.cancel_bake_export() == {'FINISHED'}
    canceled = get_latest_result(settings)
    assert canceled['state'] == 'canceled', canceled
    assert canceled['warnings'] == ['current']
    assert canceled['export_path'] == 'bake.usdz'
    assert canceled['log_path'] == 'bake.log'
    assert canceled['diagnostics_path'] == str(report)
    assert canceled['diagnostics_available'] is False
    assert resolve_diagnostics_path(settings) == ''
    saved_status = json.loads((job/'status.json').read_text())
    assert saved_status['warnings'] == ['current']
    assert saved_status['export_path'] == 'bake.usdz'
    assert saved_status['log_path'] == 'bake.log'
    assert saved_status['diagnostics_available'] is False
    # An unkillable active worker remains attached; neither action may clear it.
    attach({**current, 'state':'running'})
    jobs.pid_is_running = lambda *_: True
    jobs._terminate_process = lambda *_: None
    for operation in (bpy.ops.usdstage.cancel_bake_export, bpy.ops.usdstage.clear_bake_job):
        try:
            outcome = operation()
        except RuntimeError:
            outcome = {'CANCELLED'}
        assert outcome == {'CANCELLED'}
        assert settings.background_job_dir == str(job)
        assert settings.background_job_pid == 123
    # Missing status does not authorize detaching a still-running worker.
    (job/'status.json').unlink()
    for operation in (bpy.ops.usdstage.cancel_bake_export, bpy.ops.usdstage.clear_bake_job):
        try:
            outcome = operation()
        except RuntimeError:
            outcome = {'CANCELLED'}
        assert outcome == {'CANCELLED'}
        assert settings.background_job_dir == str(job)
        assert settings.background_job_pid == 123

Plugin.unregister()
print('BACKGROUND_RESULT_RETENTION_OK')
'''


def test_background_result_retention(tmp_path):
    script = tmp_path / 'probe.py'
    script.write_text(PROBE)
    result = subprocess.run(
        [blender_executable(), '--background', '--factory-startup',
         '--python-exit-code', '1', '--python', str(script), '--', str(REPO_ROOT)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'BACKGROUND_RESULT_RETENTION_OK' in result.stdout
