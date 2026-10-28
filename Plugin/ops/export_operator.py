"""
Export operator for USD Stage for Blender
"""

from __future__ import annotations

import bpy
import json
from pathlib import Path
from bpy.props import StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from .. import prefs as addon_prefs

def _active_background_job_message(settings) -> str:
    """Return an error message when a background bake job is still running.

    The background bake stages into the same output directory this export
    publishes to, so running both at once lets the foreground export delete
    the staging tree the runner is baking into. Both the background operator
    and this one refuse to start while a job is active.
    """
    from .background_jobs import (
        ACTIVE_JOB_STATES,
        pid_is_running,
        read_job_status,
        status_pid,
    )

    job_dir = str(getattr(settings, "background_job_dir", "") or "")
    if not job_dir:
        return ""
    status = read_job_status(job_dir)
    if not status or status.get("state") not in ACTIVE_JOB_STATES:
        return ""
    pid = status_pid(status)
    if pid is None or not pid_is_running(pid):
        return ""
    return (
        "A background bake job is still running and is writing to this output "
        "directory. Wait for it to finish, or cancel it, before exporting."
    )


class USDSTAGE_OT_export(Operator, ExportHelper):
    """Export scene to RealityKit-compatible USD/USDZ"""
    bl_idname = "usdstage.export"
    bl_label = "Export USD"
    bl_description = "Translate Blender materials to RealityKit MaterialX and export. Stops with an error on nodes it cannot translate"
    bl_options = {'REGISTER', 'UNDO'}

    # ExportHelper properties
    filename_ext = ".usdz"
    filter_glob: StringProperty(
        default="*.usdz;*.usda;*.usdc",
        options={'HIDDEN'}
    )

    @staticmethod
    def _format_extension(export_format: str) -> str:
        """Map export format to a file extension."""
        return {
            'USDA': '.usda',
            'USDC': '.usdc',
            'USDZ': '.usdz',
        }.get(export_format, '.usdz')

    @classmethod
    def _enforce_extension(cls, filepath: str, export_format: str) -> str:
        """Ensure filepath matches the chosen export format extension."""
        extension = cls._format_extension(export_format)
        path_obj = Path(filepath)
        if path_obj.suffix.lower() == extension:
            return str(path_obj)
        return str(path_obj.with_suffix(extension))

    def invoke(self, context, event):
        """Called when operator is invoked"""
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
        """Execute the export"""

        # Get settings
        settings = context.scene.usd_stage_export_settings
        blocked = _active_background_job_message(settings)
        if blocked:
            self.report({'ERROR'}, blocked)
            return {'CANCELLED'}
        _apply_persisted_settings(context, settings)
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

        from ..export import diagnostics, pipeline

        diag = diagnostics.ExportDiagnostics()
        success_diagnostics_enabled = bool(
            getattr(settings, "diagnostics_enabled", False)
        )
        # Failed exports always persist this report. The setting only controls
        # whether successful exports keep it as well.
        diag_path = Path(self.filepath).with_suffix('.diagnostics.json')
        if not success_diagnostics_enabled:
            settings.last_diagnostics_path = ""
        pipeline.describe_export(
            diag, context, settings,
            command="ui_export",
            filepath=self.filepath,
            export_format=export_format,
            blend_file=context.blend_data.filepath,
        )

        def keep_diagnostics():
            _save_diagnostics(diag, diag_path)
            settings.last_diagnostics_path = str(diag_path)

        try:
            scope = pipeline.validation_scope(context, settings)
            pipeline.validate_materials(context, settings, scope, diag)
        except pipeline.ExportRefused as refusal:
            self._report_refusal(refusal)
            keep_diagnostics()
            return {'CANCELLED'}

        progress = {
            pipeline.STEP_EXPORT: "Exporting from Blender...",
            pipeline.STEP_POSTPROCESS: "Rewriting materials to RealityKit ShaderGraph...",
            pipeline.STEP_PACKAGE: "Packaging USDZ...",
        }
        try:
            pipeline.write_export(
                context, settings, self.filepath, settings.export_format, diag,
                on_step=lambda step: step in progress and self.report({'INFO'}, progress[step]),
            )
        except pipeline.ExportRefused as refusal:
            if refusal.code == "POSTPROCESS_FAILED":
                errors = diag.data.get('errors') or []
                for error in errors[:5]:
                    self.report({'ERROR'}, str(error))
                if len(errors) > 5:
                    self.report({'ERROR'}, f"{len(errors) - 5} more errors (see diagnostics)")
            else:
                diag.add_error(str(refusal))
                self.report({'ERROR'}, str(refusal))
            keep_diagnostics()
            return {'CANCELLED'}
        except Exception as e:
            import traceback
            diag.add_exception(e, stage="ui_export")
            try:
                keep_diagnostics()
            except Exception:
                pass
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            traceback.print_exc()
            return {'CANCELLED'}

        if success_diagnostics_enabled:
            keep_diagnostics()

        warnings = diag.data.get('warnings') or []
        for warning in warnings[:5]:
            self.report({'WARNING'}, warning)
        if len(warnings) > 5:
            suffix = " (see diagnostics)" if success_diagnostics_enabled else ""
            self.report({'WARNING'}, f"{len(warnings) - 5} more warnings{suffix}")

        self.report({'INFO'}, f"Export completed: {self.filepath}")
        _store_last_export_settings(context, settings)
        return {'FINISHED'}

    def _report_refusal(self, refusal) -> None:
        """Say why validation stopped the export, a few issues at a time."""
        if not refusal.issues:
            self.report({'ERROR'}, str(refusal))
            return
        count = len(refusal.issues)
        self.report(
            {'ERROR'},
            f"Unsupported nodes found in material '{refusal.material}' ({count}).",
        )
        for issue in refusal.issues[:6]:
            node_name = issue.get("node_name") or "<unknown>"
            node_type = issue.get("node_type") or "?"
            message = issue.get("message") or "Unsupported node."
            self.report({'ERROR'}, f"{node_name} ({node_type}): {message}")
        if count > 6:
            self.report({'ERROR'}, f"{count - 6} more errors in '{refusal.material}'.")


class USDSTAGE_OT_show_diagnostics(Operator):
    """Show export diagnostics"""
    bl_idname = "usdstage.show_diagnostics"
    bl_label = "Show Diagnostics"
    bl_description = "Show last export diagnostics"
    bl_options = {'REGISTER'}

    _diag_path: str | None = None
    _diag_data: dict | None = None

    def invoke(self, context, event):
        """Show diagnostics in a dialog."""
        diag_path = _resolve_diagnostics_path(context)
        if not diag_path:
            self.report({'ERROR'}, "No diagnostics file found. Run an export first.")
            return {'CANCELLED'}

        try:
            self._diag_data = json.loads(Path(diag_path).read_text(encoding="utf-8"))
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to read diagnostics: {exc}")
            return {'CANCELLED'}

        self._diag_path = diag_path
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        data = self._diag_data or {}
        layout.label(text=f"Diagnostics: {self._diag_path}")

        summary = layout.box()
        summary.label(text="Summary")
        materials = data.get('materials', {})
        textures = data.get('textures', {})
        summary.label(text=f"Materials converted: {materials.get('converted', 0)}")
        summary.label(text=f"Materials failed: {materials.get('failed', 0)}")
        summary.label(text=f"Textures copied: {textures.get('copied', 0)}")
        summary.label(text=f"Textures converted: {textures.get('converted', 0)}")

        from ..ui.draw_utils import draw_issue_list

        errors = data.get('errors', []) or []
        warnings = data.get('warnings', []) or []
        draw_issue_list(layout, errors, title="Errors", icon='ERROR', alert=True)
        draw_issue_list(layout, warnings, title="Warnings", icon='INFO')

        if self._diag_path:
            op = layout.operator(
                "usdstage.open_diagnostics_text",
                text="Open Diagnostics JSON in Text Editor",
                icon='TEXT',
            )
            op.filepath = self._diag_path

    def execute(self, context):
        """Dialog confirmed."""
        return {'FINISHED'}


class USDSTAGE_OT_open_diagnostics_text(Operator):
    """Load diagnostics JSON into a Text datablock."""
    bl_idname = "usdstage.open_diagnostics_text"
    bl_label = "Open Diagnostics JSON"
    bl_description = "Load diagnostics JSON into Blender's Text Editor"

    filepath: StringProperty(
        name="Diagnostics Path",
        description="Path to diagnostics JSON",
        subtype='FILE_PATH'
    )

    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No diagnostics path provided.")
            return {'CANCELLED'}
        path = Path(self.filepath)
        if not path.exists():
            self.report({'ERROR'}, f"Diagnostics file not found: {path}")
            return {'CANCELLED'}
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to read diagnostics: {exc}")
            return {'CANCELLED'}

        text_name = "USD Stage Diagnostics"
        text_block = bpy.data.texts.get(text_name)
        if text_block is None:
            text_block = bpy.data.texts.new(text_name)
        text_block.clear()
        text_block.write(content)
        self.report({'INFO'}, f"Loaded diagnostics into Text Editor: {text_name}")
        return {'FINISHED'}


class USDSTAGE_OT_open_support_text(Operator):
    """Load a support text file into a Text datablock."""
    bl_idname = "usdstage.open_support_text"
    bl_label = "Open Support File"
    bl_description = "Load a support log/status file into Blender's Text Editor"

    filepath: StringProperty(
        name="File Path",
        description="Path to support text file",
        subtype='FILE_PATH'
    )

    text_name: StringProperty(
        name="Text Name",
        description="Blender Text datablock name",
        default="USD Stage Support"
    )

    def execute(self, context):
        if not self.filepath:
            self.report({'ERROR'}, "No file path provided.")
            return {'CANCELLED'}
        path = Path(self.filepath)
        if not path.exists():
            self.report({'ERROR'}, f"Support file not found: {path}")
            return {'CANCELLED'}
        try:
            content = path.read_text(errors="replace")
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to read support file: {exc}")
            return {'CANCELLED'}

        text_block = bpy.data.texts.get(self.text_name)
        if text_block is None:
            text_block = bpy.data.texts.new(self.text_name)
        text_block.clear()
        text_block.write(content)
        self.report({'INFO'}, f"Loaded support file into Text Editor: {self.text_name}")
        return {'FINISHED'}


class USDSTAGE_OT_create_support_bundle(Operator):
    """Create a redacted support bundle for the latest export."""
    bl_idname = "usdstage.create_support_bundle"
    bl_label = "Create Support Bundle"
    bl_description = "Create a redacted support ZIP for sharing USD Stage for Blender diagnostics"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.scene.usd_stage_export_settings
        try:
            from ..export.support_bundle import create_support_bundle

            result = create_support_bundle(
                context=context,
                blend_file=bpy.data.filepath or None,
                export_path=getattr(settings, "filepath", "") or None,
                diagnostics_path=_resolve_diagnostics_path(context),
                job_dir=getattr(settings, "background_job_dir", "") or None,
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to create support bundle: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Support bundle created: {result.get('support_bundle_path')}")
        return {'FINISHED'}


def _resolve_diagnostics_path(context) -> str | None:
    settings = context.scene.usd_stage_export_settings
    candidates = []
    job_candidates = []
    job_dir = getattr(settings, "background_job_dir", "")
    if job_dir:
        status_path = Path(job_dir) / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
        except Exception:
            status = {}
        if status.get("diagnostics_path"):
            job_candidates.append(status["diagnostics_path"])
    if getattr(settings, "filepath", ""):
        current_diag = str(Path(settings.filepath).with_suffix('.diagnostics.json'))
        candidates.append(current_diag)
        if job_dir:
            job_candidates.append(current_diag)
    if job_candidates:
        for candidate in job_candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return job_candidates[0]
    if getattr(settings, "last_diagnostics_path", ""):
        candidates.append(settings.last_diagnostics_path)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    search_dirs = []
    if getattr(settings, "filepath", ""):
        search_dirs.append(Path(settings.filepath).parent)
    if bpy.data.filepath:
        search_dirs.append(Path(bpy.data.filepath).parent)
    search_dirs.append(Path.cwd())

    latest = None
    latest_mtime = -1.0
    for directory in search_dirs:
        if not directory or not directory.exists():
            continue
        for path in directory.glob("*.diagnostics.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest = path
    return str(latest) if latest else None


def _save_diagnostics(diag, diag_path: Path | None) -> None:
    if diag_path is None:
        return
    diag.set_artifact("diagnostics_path", str(diag_path))
    diag.save(diag_path)


def _store_last_export_settings(context, settings) -> None:
    addon_prefs.persist_export_settings(context, settings)


def _apply_persisted_settings(context, settings) -> None:
    addon_prefs.apply_persisted_export_settings(context, settings)


class OutputPathError(ValueError):
    """The Output Path does not name a file the export can write."""


def _resolve_output_path_from_settings(context, settings, export_format: str, fallback: str = "") -> str:
    filepath = str(getattr(settings, "filepath", "") or fallback or "").strip()
    if not filepath:
        raise OutputPathError("Set Output Path before exporting.")

    blend_file = getattr(getattr(context, "blend_data", None), "filepath", "")
    if not blend_file and (
        filepath.startswith("//") or not Path(filepath).expanduser().is_absolute()
    ):
        raise OutputPathError(
            "Save the .blend first, or set an absolute Output Path: "
            "a relative path needs a saved file to be relative to."
        )

    path = Path(bpy.path.abspath(filepath)).expanduser()
    if not path.is_absolute():
        path = Path(blend_file).parent / path

    return USDSTAGE_OT_export._enforce_extension(str(path), export_format)


def register():
    """Register operators"""
    bpy.utils.register_class(USDSTAGE_OT_export)
    bpy.utils.register_class(USDSTAGE_OT_show_diagnostics)
    bpy.utils.register_class(USDSTAGE_OT_open_diagnostics_text)
    bpy.utils.register_class(USDSTAGE_OT_open_support_text)
    bpy.utils.register_class(USDSTAGE_OT_create_support_bundle)


def unregister():
    """Unregister operators"""
    bpy.utils.unregister_class(USDSTAGE_OT_create_support_bundle)
    bpy.utils.unregister_class(USDSTAGE_OT_open_support_text)
    bpy.utils.unregister_class(USDSTAGE_OT_open_diagnostics_text)
    bpy.utils.unregister_class(USDSTAGE_OT_show_diagnostics)
    bpy.utils.unregister_class(USDSTAGE_OT_export)
