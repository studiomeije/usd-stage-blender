"""
Background bake-and-export operator for USD Stage for Blender.
"""

from __future__ import annotations

import os
import json
import subprocess
import time
import tempfile
import shutil
import signal
from pathlib import Path

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from .background_jobs import (
    ACTIVE_JOB_STATES,
    pid_is_running,
    read_job_status,
    remember_background_process,
    safe_int,
    status_pid,
)
from .export_operator import (
    OutputPathError,
    _apply_persisted_settings,
    _resolve_output_path_from_settings,
    _store_last_export_settings,
)

_WORKER_TIMEOUT_GRACE_SECONDS = 15
_SERIALIZED_SETTINGS_SKIP_KEYS = {
    "rna_type",
    "name",
    "history_applied",
    "last_diagnostics_path",
    "persist_suspended",
    "background_job_dir",
    "background_job_pid",
    "filepath",
    "ui_material_type",
    "ui_pbr_processing",
    "ui_unlit_appearance",
    "ui_source_texture_resolution",
    "ui_source_texture_format",
}


def _write_prelaunch_failure_diagnostics(
    context,
    settings,
    message: str,
    *,
    code: str,
    details=None,
) -> str | None:
    """Best-effort diagnostics for failures before the worker can start."""
    filepath = str(getattr(settings, "filepath", "") or "").strip()
    if not filepath:
        return None
    diagnostics_path = Path(filepath).with_suffix(".diagnostics.json")
    try:
        from ..export.diagnostics import ExportDiagnostics
        from ..export.support_bundle import collect_environment, collect_scene_snapshot

        diag = ExportDiagnostics()
        diag.set_export_context(
            command="ui_background_bake_export",
            resolved_output_path=filepath,
            export_format=getattr(settings, "export_format", None),
            selected_only=bool(getattr(settings, "selected_objects_only", False)),
            blend_file=getattr(getattr(context, "blend_data", None), "filepath", None),
        )
        diag.set_environment(**collect_environment(context))
        diag.data["scene"] = collect_scene_snapshot(context)
        diag.data["prelaunch_failure"] = {
            "code": code,
            "message": str(message),
            "details": details,
        }
        diag.add_error(str(message))
        diag.set_artifact("diagnostics_path", str(diagnostics_path))
        diag.save(diagnostics_path)
        settings.last_diagnostics_path = str(diagnostics_path)
        return str(diagnostics_path)
    except Exception:
        return None


class USDSTAGE_OT_bake_export_background(Operator, ExportHelper):
    """Bake textures and export scene in a background Blender process."""
    bl_idname = "usdstage.bake_export_background"
    bl_label = "Bake and Export (Background)"
    bl_description = "Bake Blender materials into textures before exporting."
    bl_options = {'REGISTER'}

    filename_ext = ".usdz"
    filter_glob: StringProperty(
        default="*.usdz;*.usda;*.usdc",
        options={'HIDDEN'}
    )
    apply_ui_profile: BoolProperty(
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'},
    )

    def _apply_ui_profile(self, settings) -> bool:
        if not self.apply_ui_profile:
            return True
        from ..export_profile import PIPELINE_BAKE, resolve_ui_export_route

        route = resolve_ui_export_route(settings)
        if route.pipeline != PIPELINE_BAKE or not route.bake_mode:
            self.report(
                {'ERROR'},
                "The selected material type does not use texture baking.",
            )
            return False
        settings.bake_mode = route.bake_mode
        return True

    def invoke(self, context, event):
        settings = context.scene.usd_stage_export_settings
        _apply_persisted_settings(context, settings)
        export_format = settings.export_format
        try:
            self.filepath = _resolve_output_path_from_settings(context, settings, export_format)
        except OutputPathError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return self.execute(context)

    def execute(self, context):
        settings = context.scene.usd_stage_export_settings
        _apply_persisted_settings(context, settings)
        if not self._apply_ui_profile(settings):
            return {'CANCELLED'}

        export_format = settings.export_format
        try:
            self.filepath = _resolve_output_path_from_settings(
                context,
                settings,
                export_format,
                fallback=getattr(self, "filepath", ""),
            )
        except OutputPathError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        settings.filepath = self.filepath

        if getattr(settings, "background_job_dir", ""):
            status = read_job_status(settings.background_job_dir)
            if status and status.get("state") in {"queued", "running"}:
                self.report({'ERROR'}, "A background job is already running. Cancel it first.")
                return {'CANCELLED'}

        try:
            objects_to_export = _collect_export_objects(context, settings)
        except Exception as exc:
            message = str(exc)
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="INVALID_EXPORT_SELECTION"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        if not objects_to_export:
            message = "No exportable objects found"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="NO_EXPORTABLE_OBJECTS"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        from ..export.animation_export import collect_processing_objects

        snapshot_objects = collect_processing_objects(context, objects_to_export)

        # Resolve source-relative settings while the user's original .blend is
        # still the active file. The worker loads a private copy from the job
        # directory, so forwarding a raw Blender ``//`` path would otherwise
        # retarget it to that directory.
        try:
            serialized_settings = _serialize_settings(
                settings,
                context=context,
                enable_texture_settings=self.apply_ui_profile,
            )
        except Exception as exc:
            message = f"Invalid background export settings: {exc}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="INVALID_EXPORT_SETTINGS"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        # The native selection can depend on non-selected parents and collection
        # prototypes. Preflight that processing closure before launching a worker
        # so missing source pixels cannot turn into a successful but incomplete
        # bake. The dependency walker is shared with CLI/API bake export.
        from ..export import asset_preflight

        try:
            missing_images = asset_preflight.collect_missing_image_files_for_objects(
                snapshot_objects,
                bpy,
            )
        except Exception as exc:
            message = f"Could not validate source image dependencies: {exc}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="ASSET_PREFLIGHT_FAILED"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        if missing_images:
            message = asset_preflight.missing_images_status_message(missing_images)
            _write_prelaunch_failure_diagnostics(
                context,
                settings,
                message,
                code=asset_preflight.missing_assets_error_code(missing_images),
                details=missing_images,
            )
            self.report(
                {'ERROR'},
                message,
            )
            return {'CANCELLED'}

        export_dir = Path(self.filepath).parent
        _cleanup_stale_scene_snapshots(export_dir)
        job_dir = _create_job_dir(export_dir)
        try:
            snapshot_path = _create_scene_snapshot(
                context,
                job_dir,
                objects=snapshot_objects,
                settings=settings,
            )
        except Exception as exc:
            _cleanup_scene_snapshot(job_dir)
            message = f"Could not snapshot the current scene: {exc}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="SCENE_SNAPSHOT_FAILED"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        status_path = job_dir / "status.json"
        log_path = job_dir / "log.txt"
        success_diagnostics_enabled = bool(
            getattr(settings, "diagnostics_enabled", False)
        )
        # The worker always receives a failure-report destination. This
        # preference controls only whether a successful job keeps a sidecar.
        diagnostics_path = str(Path(self.filepath).with_suffix(".diagnostics.json"))
        if not success_diagnostics_enabled:
            settings.last_diagnostics_path = ""

        selection_names = []
        if getattr(settings, "selected_objects_only", False):
            selection_names = [obj.name for obj in objects_to_export]

        payload = {
            "job_dir": str(job_dir),
            "blend_file": str(snapshot_path),
            "source_blend_file": context.blend_data.filepath or None,
            "export_path": self.filepath,
            "export_settings": serialized_settings,
            "selected_only": bool(getattr(settings, "selected_objects_only", False)),
            "selection": selection_names,
            "diagnostics_path": diagnostics_path,
            "success_diagnostics_enabled": success_diagnostics_enabled,
        }
        settings_path = job_dir / "settings.json"
        try:
            settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            _cleanup_scene_snapshot(job_dir)
            message = f"Could not write background job settings: {exc}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="JOB_SETTINGS_WRITE_FAILED"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        _write_status(
            status_path,
            state="queued",
            progress=0.0,
            message="Queued background export",
            log_path=str(log_path),
            export_path=self.filepath,
            diagnostics_path=diagnostics_path,
        )

        blender_bin = bpy.app.binary_path
        runner_path = Path(__file__).resolve().parents[1] / "bake_export_runner.py"
        if not runner_path.exists():
            _cleanup_scene_snapshot(job_dir)
            message = f"Missing runner script: {runner_path}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="BACKGROUND_RUNNER_MISSING"
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log_file:
                proc = subprocess.Popen(
                    [
                        blender_bin,
                        "--background",
                        "--factory-startup",
                        str(snapshot_path),
                        "--python",
                        str(runner_path),
                        "--",
                        str(settings_path),
                    ],
                    stdout=log_file,
                    stderr=log_file,
                )
        except Exception as exc:
            _cleanup_scene_snapshot(job_dir)
            message = f"Failed to launch background Blender: {exc}"
            _write_prelaunch_failure_diagnostics(
                context, settings, message, code="BACKGROUND_LAUNCH_FAILED"
            )
            _write_status(
                status_path,
                state="error",
                progress=1.0,
                message=message,
                log_path=str(log_path),
                export_path=self.filepath,
                diagnostics_path=diagnostics_path,
            )
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        _write_status(
            status_path,
            state="queued",
            progress=0.0,
            message="Queued background export",
            log_path=str(log_path),
            export_path=self.filepath,
            diagnostics_path=diagnostics_path,
            pid=proc.pid,
        )
        settings.background_job_dir = str(job_dir)
        settings.background_job_pid = proc.pid
        remember_background_process(proc)
        _store_last_export_settings(context, settings)
        try:
            bpy.ops.usdstage.watch_bake_export_job('INVOKE_DEFAULT')
        except Exception:
            pass

        self.report({'INFO'}, f"Background export started (PID {proc.pid}).")
        return {'FINISHED'}


class USDSTAGE_OT_cancel_bake_export(Operator):
    """Cancel the active background bake/export job."""
    bl_idname = "usdstage.cancel_bake_export"
    bl_label = "Cancel Background Bake"
    bl_description = "Cancel the background bake/export job"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.usd_stage_export_settings
        pid = int(getattr(settings, "background_job_pid", 0))
        job_dir = getattr(settings, "background_job_dir", "")
        if not pid or not job_dir:
            self.report({'ERROR'}, "No background job to cancel.")
            return {'CANCELLED'}

        status_path = Path(job_dir) / "status.json"
        status = read_job_status(job_dir)
        if not status or status.get("state") not in ACTIVE_JOB_STATES:
            _cleanup_scene_snapshot(job_dir)
            settings.background_job_pid = 0
            settings.background_job_dir = ""
            self.report({'INFO'}, "Cleared stale background job state.")
            return {'FINISHED'}

        recorded_pid = status_pid(status)
        if recorded_pid is None or recorded_pid != pid:
            _cleanup_scene_snapshot(job_dir)
            settings.background_job_pid = 0
            settings.background_job_dir = ""
            self.report({'INFO'}, "Cleared stale background job state.")
            return {'FINISHED'}

        if pid_is_running(pid):
            _terminate_process(pid)
        else:
            _cleanup_scene_snapshot(job_dir)
            settings.background_job_pid = 0
            settings.background_job_dir = ""
            self.report({'INFO'}, "Cleared stale background job state.")
            return {'FINISHED'}

        _write_status(
            status_path,
            state="canceled",
            progress=1.0,
            message="Canceled by user",
            pid=pid,
        )

        settings.background_job_pid = 0
        settings.background_job_dir = ""
        _cleanup_scene_snapshot(job_dir)
        self.report({'INFO'}, "Background job canceled.")
        return {'FINISHED'}


class USDSTAGE_OT_clear_bake_job(Operator):
    """Clear background bake/export job state."""
    bl_idname = "usdstage.clear_bake_job"
    bl_label = "Clear Bake Job"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.usd_stage_export_settings
        job_dir = getattr(settings, "background_job_dir", "")
        status = read_job_status(job_dir)
        pid = status_pid(status or {})
        if (
            status
            and status.get("state") in ACTIVE_JOB_STATES
            and pid is not None
            and pid_is_running(pid)
        ):
            self.report({'ERROR'}, "Background job is active. Cancel it instead.")
            return {'CANCELLED'}
        if job_dir:
            _cleanup_scene_snapshot(job_dir)
        settings.background_job_dir = ""
        settings.background_job_pid = 0
        self.report({'INFO'}, "Cleared background job state.")
        return {'FINISHED'}


class USDSTAGE_OT_watch_bake_export_job(Operator):
    """Modal watcher that keeps the panel refreshed and handles timeout/failure detection."""
    bl_idname = "usdstage.watch_bake_export_job"
    bl_label = "Watch Bake Export Job"
    bl_options = {'INTERNAL'}

    _timer = None

    def invoke(self, context, event):
        settings = context.scene.usd_stage_export_settings
        if not getattr(settings, "background_job_dir", ""):
            return {'CANCELLED'}
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        settings = context.scene.usd_stage_export_settings
        job_dir = getattr(settings, "background_job_dir", "")
        if not job_dir:
            self._stop(context)
            return {'CANCELLED'}

        status_path = Path(job_dir) / "status.json"
        status = read_job_status(job_dir)
        if status is None and not status_path.exists():
            _cleanup_scene_snapshot(job_dir)
            settings.background_job_pid = 0
            settings.background_job_dir = ""
            _tag_export_ui_redraw()
            self._stop(context)
            return {'FINISHED'}
        status = status or {}
        state = status.get("state")
        pid = int(getattr(settings, "background_job_pid", 0))

        _tag_export_ui_redraw()

        if state in {"done", "error", "canceled"}:
            _cleanup_scene_snapshot(job_dir)
            settings.background_job_pid = 0
            self._stop(context)
            return {'FINISHED'}

        if pid > 0 and not pid_is_running(pid):
            _write_status(
                status_path,
                state="error",
                progress=1.0,
                message="Background job exited unexpectedly.",
                log_path=status.get("log_path"),
                export_path=status.get("export_path") or getattr(settings, "filepath", ""),
                diagnostics_path=status.get("diagnostics_path"),
            )
            settings.background_job_pid = 0
            _cleanup_scene_snapshot(job_dir)
            _tag_export_ui_redraw()
            self._stop(context)
            return {'FINISHED'}

        timeout_seconds = int(getattr(settings, "bake_step_timeout_seconds", 0) or 0)
        if timeout_seconds > 0 and state in {"queued", "running"} and pid > 0:
            step_elapsed = _extract_step_elapsed_seconds(status)
            failsafe_deadline = timeout_seconds + _WORKER_TIMEOUT_GRACE_SECONDS
            if step_elapsed is not None and step_elapsed >= failsafe_deadline:
                # The worker owns timeout enforcement and writes the detailed
                # BAKE_STEP_TIMEOUT result. Re-read immediately before the UI
                # failsafe acts so a terminal worker status wins this race.
                latest = read_job_status(job_dir) or {}
                if latest.get("state") in {"done", "error", "canceled"}:
                    _cleanup_scene_snapshot(job_dir)
                    settings.background_job_pid = 0
                    self._stop(context)
                    return {'FINISHED'}
                _terminate_process(pid)
                _write_status(
                    status_path,
                    state="error",
                    progress=1.0,
                    message=(
                        f"Background worker did not exit within "
                        f"{_WORKER_TIMEOUT_GRACE_SECONDS}s after the "
                        f"{timeout_seconds}s step timeout; process terminated."
                    ),
                    log_path=status.get("log_path"),
                    export_path=status.get("export_path") or getattr(settings, "filepath", ""),
                    diagnostics_path=status.get("diagnostics_path"),
                )
                settings.background_job_pid = 0
                _cleanup_scene_snapshot(job_dir)
                _tag_export_ui_redraw()
                self._stop(context)
                return {'FINISHED'}

        return {'PASS_THROUGH'}

    def cancel(self, context):
        self._stop(context)

    def _stop(self, context):
        if self._timer is not None:
            try:
                context.window_manager.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None


def _tag_export_ui_redraw() -> None:
    for wm in bpy.data.window_managers:
        for window in wm.windows:
            screen = getattr(window, "screen", None)
            if screen is None:
                continue
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                area.tag_redraw()
                for region in area.regions:
                    if region.type in {'UI', 'WINDOW'}:
                        region.tag_redraw()


def _terminate_process(pid: int) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not pid_is_running(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _extract_step_elapsed_seconds(status: dict) -> int | None:
    if not isinstance(status, dict) or status.get("step_elapsed_seconds") is None:
        return None
    return safe_int(status.get("step_elapsed_seconds"))


def _collect_export_objects(context, settings):
    # Centralized with the direct USD export path so selected-only behavior is
    # identical for UI, CLI, and background jobs.  In particular an empty
    # selection stays empty (and fails) rather than exporting the whole scene,
    # while selected skinned meshes include their deforming armature.
    from ..export.animation_export import collect_export_objects

    return collect_export_objects(context, settings)


def _collect_materials_from_objects(objects):
    materials = []
    seen = set()
    for obj in objects:
        for slot in getattr(obj, "material_slots", []):
            mat = slot.material
            if mat and mat not in seen:
                seen.add(mat)
                materials.append(mat)
    return materials


def _ensure_object_mode(context) -> None:
    active = context.view_layer.objects.active
    if active and active.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def _set_render_engine(scene, engine: str) -> None:
    try:
        scene.render.engine = engine
    except Exception:
        pass


def _restore_selection(context, selection, active) -> None:
    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        pass
    for obj in selection:
        try:
            obj.select_set(True)
        except Exception:
            pass
    if active:
        try:
            context.view_layer.objects.active = active
        except Exception:
            pass
def _serialize_settings(
    settings,
    *,
    context=None,
    enable_texture_settings: bool = False,
) -> dict:
    data = {}
    for prop in settings.bl_rna.properties:
        key = prop.identifier
        if key in _SERIALIZED_SETTINGS_SKIP_KEYS:
            continue
        try:
            data[key] = getattr(settings, key)
        except Exception:
            continue

    if enable_texture_settings:
        data["export_texture_settings_enabled"] = True

    if (
        str(data.get("bake_mode") or "LIT_IBL") == "LIT_IBL"
        and str(data.get("bake_ibl_source") or "") == "HDRI_FILE"
    ):
        from ..export.bake_textures import _resolve_hdri_filepath

        resolver_kwargs = {}
        if context is not None:
            resolver_kwargs["blend_file"] = str(
                getattr(getattr(context, "blend_data", None), "filepath", "") or ""
            )
        data["bake_ibl_filepath"] = str(
            _resolve_hdri_filepath(settings, **resolver_kwargs)
        )
    return data


#: How many finished job directories to keep beside an export. Each holds a
#: status file, a log and any diagnostics, which are worth having for the last
#: few runs, but without a limit the directory would grow next to the asset
#: the user ships.
_KEPT_FINISHED_JOB_DIRS = 5


def _create_job_dir(export_dir: Path) -> Path:
    root = export_dir / ".usdstage_jobs"
    root.mkdir(parents=True, exist_ok=True)
    _prune_finished_job_dirs(root)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    job_dir = Path(tempfile.mkdtemp(prefix=f"bake_export_{stamp}_", dir=root))
    return job_dir


def _prune_finished_job_dirs(root: Path) -> None:
    """Delete all but the newest few *finished* job directories.

    Only directories whose status.json reports a terminal state are eligible; a
    queued or running job, or one whose status cannot be read, is always kept,
    so this can never delete the tree a live runner is writing into.
    """
    try:
        candidates = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return

    finished = []
    for entry in candidates:
        status = read_job_status(str(entry))
        if not status:
            # Unreadable or still being set up: not ours to remove.
            continue
        if status.get("state") in ACTIVE_JOB_STATES:
            continue
        try:
            finished.append((entry.stat().st_mtime, entry))
        except OSError:
            continue

    finished.sort(reverse=True)
    for _mtime, entry in finished[_KEPT_FINISHED_JOB_DIRS:]:
        try:
            shutil.rmtree(entry)
        except Exception:
            pass


_SCENE_SNAPSHOT_NAME = "scene_snapshot.blend"


def _create_scene_snapshot(context, job_dir: Path, objects=None, settings=None) -> Path:
    """Save the current in-memory scene without changing the user's file.

    ``copy=True`` is Blender's supported "Save Copy" path: it includes unsaved
    edits (and works when ``bpy.data.filepath`` is empty) while leaving the
    active mainfile and dirty state untouched. ``relative_remap=True`` keeps
    ``//`` external assets rooted at an existing saved file. A never-saved
    file has no base directory, so relative external dependencies fail closed
    with an actionable error instead of becoming broken job-relative paths.
    """
    snapshot_path = Path(job_dir) / _SCENE_SNAPSHOT_NAME
    original_filepath = str(getattr(context.blend_data, "filepath", "") or "")
    original_dirty = bool(getattr(context.blend_data, "is_dirty", False))

    dirty_images = _dirty_image_buffers(context, objects, settings=settings)
    if dirty_images:
        examples = ", ".join(repr(name) for name in dirty_images[:3])
        if len(dirty_images) > 3:
            examples += f", and {len(dirty_images) - 3} more"
        raise RuntimeError(
            "Dirty image pixels are not serialized by "
            f"Blender Save Copy ({examples}). Save them, or pack/repack "
            "them, before background export."
        )

    if not original_filepath:
        relative_paths = _relative_external_paths()
        if relative_paths:
            examples = ", ".join(repr(path) for path in relative_paths[:3])
            if len(relative_paths) > 3:
                examples += f", and {len(relative_paths) - 3} more"
            raise RuntimeError(
                "Never-saved scenes cannot resolve relative external assets "
                f"({examples}). Save the .blend, pack those assets, or use "
                "absolute paths before background export."
            )

    result = bpy.ops.wm.save_as_mainfile(
        filepath=str(snapshot_path),
        check_existing=False,
        copy=True,
        relative_remap=True,
    )
    if "FINISHED" not in set(result or []):
        raise RuntimeError(f"Blender Save Copy returned {result!r}.")
    if not snapshot_path.is_file():
        raise RuntimeError("Blender reported success but did not create the snapshot.")

    current_filepath = str(getattr(context.blend_data, "filepath", "") or "")
    current_dirty = bool(getattr(context.blend_data, "is_dirty", False))
    if current_filepath != original_filepath or current_dirty != original_dirty:
        raise RuntimeError(
            "Blender Save Copy unexpectedly changed the active file or dirty state."
        )
    return snapshot_path


def _relative_external_paths() -> list[str]:
    """Return unpacked ``//`` dependencies that need a saved-file base."""
    paths: set[str] = set()
    collections = (
        "images",
        "libraries",
        "movieclips",
        "sounds",
        "fonts",
        "volumes",
        "cache_files",
    )
    for collection_name in collections:
        for datablock in getattr(bpy.data, collection_name, []) or []:
            # A linked datablock's // path is relative to its library. The
            # library datablock itself is checked separately below.
            if (
                collection_name != "libraries"
                and getattr(datablock, "library", None) is not None
            ):
                continue
            if collection_name == "images":
                if getattr(datablock, "packed_file", None) is not None:
                    continue
                try:
                    if len(getattr(datablock, "packed_files", []) or []) > 0:
                        continue
                except Exception:
                    pass
                if str(getattr(datablock, "source", "")) not in {
                    "FILE",
                    "SEQUENCE",
                    "MOVIE",
                    "TILED",
                }:
                    continue
            path = str(
                getattr(datablock, "filepath", None)
                or getattr(datablock, "filepath_raw", None)
                or ""
            )
            if path.startswith("//"):
                paths.add(path)
    return sorted(paths)


def _dirty_image_buffers(context=None, objects=None, *, settings=None) -> list[str]:
    candidates = (
        list(getattr(bpy.data, "images", []) or [])
        if objects is None
        else _images_used_by_export(context, objects, settings=settings)
    )
    names: list[str] = []
    for image in candidates:
        if not bool(getattr(image, "is_dirty", False)):
            continue
        names.append(str(getattr(image, "name", "<unnamed>")))
    return sorted(set(names))


def _images_used_by_export(context, objects, *, settings=None) -> list:
    images: list = []
    seen_images: set[int] = set()
    seen_trees: set[int] = set()
    seen_textures: set[int] = set()

    def identity(value) -> int:
        try:
            return int(value.as_pointer())
        except Exception:
            return id(value)

    def add_image(image) -> None:
        if image is None or identity(image) in seen_images:
            return
        seen_images.add(identity(image))
        images.append(image)

    def add_image_value(value) -> None:
        """Add *value* only when it is a Blender Image datablock."""
        image_type = getattr(getattr(bpy, "types", None), "Image", None)
        try:
            if image_type is not None and isinstance(value, image_type):
                add_image(value)
                return
        except TypeError:
            pass
        if str(getattr(value, "id_type", "")) == "IMAGE":
            add_image(value)

    def visit_tree(node_tree) -> None:
        if node_tree is None or identity(node_tree) in seen_trees:
            return
        seen_trees.add(identity(node_tree))
        for node in getattr(node_tree, "nodes", []) or []:
            add_image_value(getattr(node, "image", None))
            for socket in getattr(node, "inputs", []) or []:
                add_image_value(getattr(socket, "default_value", None))
            visit_tree(getattr(node, "node_tree", None))

    def visit_texture(texture) -> None:
        """Collect images behind Blender's legacy Texture datablocks."""
        if texture is None:
            return
        texture_type = getattr(getattr(bpy, "types", None), "Texture", None)
        is_texture = str(getattr(texture, "id_type", "")) == "TEXTURE"
        if texture_type is not None:
            try:
                is_texture = is_texture or isinstance(texture, texture_type)
            except TypeError:
                pass
        if not is_texture:
            return
        marker = identity(texture)
        if marker in seen_textures:
            return
        seen_textures.add(marker)
        add_image_value(getattr(texture, "image", None))
        visit_tree(getattr(texture, "node_tree", None))

    def visit_resource(value) -> None:
        add_image_value(value)
        visit_texture(value)

    def visit_modifier_resources(modifier) -> None:
        """Inspect image/texture pointer properties that affect evaluation.

        Displace, Wave, Warp, and vertex-weight modifiers can read legacy
        ``Texture`` datablocks without a Geometry Nodes tree. Walk their pointer
        properties generically, while explicit names keep this working for test
        doubles and any modifier whose RNA metadata is unavailable.
        """
        for identifier in ("texture", "mask_texture"):
            try:
                visit_resource(getattr(modifier, identifier, None))
            except Exception:
                continue

        for prop in getattr(getattr(modifier, "bl_rna", None), "properties", []) or []:
            if str(getattr(prop, "type", "")) != "POINTER":
                continue
            identifier = str(getattr(prop, "identifier", ""))
            if not identifier or identifier == "rna_type":
                continue
            try:
                visit_resource(getattr(modifier, identifier))
            except Exception:
                continue

    def visit_geometry_nodes_modifier_inputs(modifier) -> None:
        """Inspect Blender 5.2's typed per-modifier node-group inputs.

        Blender 5.2 moved exposed Geometry Nodes values away from modifier
        IDProperties. Image overrides now live at
        ``modifier.properties.inputs.<socket identifier>.value`` and can alter
        evaluated export geometry without appearing on a node itself.
        """
        inputs = getattr(
            getattr(getattr(modifier, "properties", None), "inputs", None),
            "bl_rna",
            None,
        )
        input_values = getattr(getattr(modifier, "properties", None), "inputs", None)
        for prop in getattr(inputs, "properties", []) or []:
            identifier = str(getattr(prop, "identifier", ""))
            if not identifier or identifier in {"rna_type", "name"}:
                continue
            try:
                socket_state = getattr(input_values, identifier)
            except Exception:
                continue
            visit_resource(getattr(socket_state, "value", None))

    seen_materials: set[int] = set()
    for obj in objects or []:
        for modifier in getattr(obj, "modifiers", []) or []:
            # Geometry Nodes image inputs can change evaluated geometry even
            # when the image is not referenced by a surface material.
            visit_tree(getattr(modifier, "node_group", None))
            visit_geometry_nodes_modifier_inputs(modifier)
            visit_modifier_resources(modifier)
        for material_slot in getattr(obj, "material_slots", []) or []:
            material = getattr(material_slot, "material", None)
            if material is None or identity(material) in seen_materials:
                continue
            seen_materials.add(identity(material))
            visit_tree(getattr(material, "node_tree", None))

    if _bake_uses_scene_world(settings):
        world = getattr(getattr(context, "scene", None), "world", None)
        visit_tree(getattr(world, "node_tree", None))
    return images


def _bake_uses_scene_world(settings) -> bool:
    """Whether the current bake reads the scene World's image pixels.

    A missing settings object keeps the lower-level snapshot helper
    conservative for callers that have not declared their bake contract.
    """
    if settings is None:
        return True
    bake_mode = str(getattr(settings, "bake_mode", "LIT_IBL") or "LIT_IBL")
    ibl_source = str(
        getattr(settings, "bake_ibl_source", "SCENE_WORLD") or "SCENE_WORLD"
    )
    return bake_mode == "LIT_IBL" and ibl_source == "SCENE_WORLD"


def _cleanup_scene_snapshot(job_dir: str | Path) -> None:
    """Remove only the private scene copy; retain status/log diagnostics."""
    snapshot_path = Path(job_dir) / _SCENE_SNAPSHOT_NAME
    try:
        snapshot_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        # Snapshot cleanup must not hide the terminal export result. The job
        # directory remains user-inspectable and can be removed manually.
        pass


def _cleanup_stale_scene_snapshots(export_dir: Path) -> None:
    """Best-effort janitor for snapshots left by a UI/process crash."""
    jobs_root = Path(export_dir) / ".usdstage_jobs"
    if not jobs_root.is_dir():
        return
    try:
        job_dirs = list(jobs_root.iterdir())
    except Exception:
        return
    for job_dir in job_dirs:
        if not job_dir.is_dir():
            continue
        snapshot = job_dir / _SCENE_SNAPSHOT_NAME
        if not snapshot.exists():
            continue
        status = read_job_status(str(job_dir)) or {}
        pid = status_pid(status)
        if (
            status.get("state") in ACTIVE_JOB_STATES
            and pid is not None
            and pid_is_running(pid)
        ):
            continue
        _cleanup_scene_snapshot(job_dir)


def _write_status(
    path: Path,
    state: str,
    progress: float | None = None,
    message: str | None = None,
    log_path: str | None = None,
    export_path: str | None = None,
    diagnostics_path: str | None = None,
    pid: int | None = None,
) -> None:
    payload = {
        "state": state,
        "time": time.time(),
    }
    if progress is not None:
        payload["progress"] = progress
    if message:
        payload["message"] = message
    if log_path:
        payload["log_path"] = log_path
    if export_path:
        payload["export_path"] = export_path
    if diagnostics_path:
        payload["diagnostics_path"] = diagnostics_path
    if pid is not None:
        payload["pid"] = int(pid)
    try:
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        pass


def _restore_mode(context, active, mode: str) -> None:
    if not active:
        return
    try:
        context.view_layer.objects.active = active
    except Exception:
        return
    try:
        if mode and active.mode != mode:
            bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass


def register():
    bpy.utils.register_class(USDSTAGE_OT_bake_export_background)
    bpy.utils.register_class(USDSTAGE_OT_watch_bake_export_job)
    bpy.utils.register_class(USDSTAGE_OT_cancel_bake_export)
    bpy.utils.register_class(USDSTAGE_OT_clear_bake_job)


def unregister():
    bpy.utils.unregister_class(USDSTAGE_OT_clear_bake_job)
    bpy.utils.unregister_class(USDSTAGE_OT_cancel_bake_export)
    bpy.utils.unregister_class(USDSTAGE_OT_watch_bake_export_job)
    bpy.utils.unregister_class(USDSTAGE_OT_bake_export_background)
