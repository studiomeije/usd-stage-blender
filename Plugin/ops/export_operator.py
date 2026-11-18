"""
Export operator for USD Stage for Blender
"""

from __future__ import annotations

import bpy
import json
from pathlib import Path
from datetime import datetime
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from .. import prefs as addon_prefs
from .export_results import (
    diagnostic_errors, get_latest_result, record_result,
    resolve_diagnostics_path, result_diagnostics,
)

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
        if not str(getattr(settings, "filepath", "") or "").strip():
            self.filename_ext = self._format_extension(export_format)
            self._choosing_destination = True
            return ExportHelper.invoke(self, context, event)
        try:
            self.filepath = _resolve_output_path_from_settings(context, settings, export_format)
        except OutputPathError as exc:
            self.report({'ERROR'}, str(exc))
            record_result(settings, state="error", message=str(exc), data={"errors": [str(exc)]})
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
        if getattr(self, "_choosing_destination", False):
            settings.filepath = self.filepath
            self._choosing_destination = False
        try:
            self.filepath = _resolve_output_path_from_settings(
                context,
                settings,
                export_format,
                fallback=getattr(self, "filepath", ""),
            )
        except OutputPathError as exc:
            self.report({'ERROR'}, str(exc))
            record_result(settings, state="error", message=str(exc), data={"errors": [str(exc)]})
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

        diagnostics_saved = False

        def keep_diagnostics():
            nonlocal diagnostics_saved
            try:
                _save_diagnostics(diag, diag_path)
                diagnostics_saved = True
                settings.last_diagnostics_path = str(diag_path)
            except Exception as exc:
                # Feedback must survive an unwritable diagnostics destination,
                # especially when this is already handling an export failure.
                message = f"Could not save diagnostics: {exc}"
                diag.data.setdefault("warnings", []).append(message)
                self.report({'WARNING'}, message)

        def finish_result(state, message):
            record_result(
                settings, state=state, message=message, export_path=self.filepath,
                diagnostics_path=str(diag_path) if diagnostics_saved else "",
                data=diag.data,
            )

        try:
            scope = pipeline.validation_scope(context, settings)
            pipeline.validate_materials(context, settings, scope, diag)
        except pipeline.ExportRefused as refusal:
            self._report_refusal(refusal)
            diag.add_error(str(refusal))
            keep_diagnostics()
            finish_result("error", str(refusal))
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
            finish_result("error", str(refusal))
            return {'CANCELLED'}
        except Exception as e:
            import traceback
            diag.add_exception(e, stage="ui_export")
            try:
                keep_diagnostics()
            except Exception:
                pass
            self.report({'ERROR'}, f"Export failed: {str(e)}")
            finish_result("error", f"Export failed: {e}")
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
        finish_result("done", "Export complete")
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
    filepath: StringProperty(name="Diagnostics Path", subtype='FILE_PATH')
    use_last_result: BoolProperty(default=False, options={'HIDDEN'})

    _diag_path: str | None = None
    _diag_data: dict | None = None
    _result_metadata: dict | None = None

    def invoke(self, context, event):
        """Show diagnostics in a dialog."""
        result = _latest_result(context)
        self._result_metadata = result if not self.filepath or self.filepath == result.get("diagnostics_path") else {}
        diag_path = self.filepath or (
            _resolve_diagnostics_path(context) if result or not self.use_last_result else None
        )
        if not diag_path and result:
            self._diag_data = result_diagnostics(result)
            self._diag_path = ""
            return context.window_manager.invoke_props_dialog(self, width=560)
        if not diag_path:
            self.report({'ERROR'}, "No diagnostics file found. Run an export first.")
            return {'CANCELLED'}

        try:
            self._diag_data = json.loads(Path(diag_path).read_text(encoding="utf-8"))
        except Exception as exc:
            self.report({'ERROR'}, f"Failed to read diagnostics: {exc}")
            return {'CANCELLED'}

        if self._result_metadata.get("state") == "error" and result.get("message"):
            errors = self._diag_data.setdefault("errors", [])
            if result["message"] not in errors:
                errors.append(result["message"])

        self._diag_path = diag_path
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        layout = self.layout
        data = self._diag_data or {}
        layout.label_multiline(text=f"Diagnostics: {self._diag_path}" if self._diag_path else "Latest export result")
        metadata = self._result_metadata or {}
        output = metadata.get("export_path") or (data.get("export_context") or {}).get("resolved_output_path")
        if output:
            layout.label_multiline(text=f"Output: {output}", icon='FILE')
        stamp = metadata.get("time")
        if stamp:
            try:
                layout.label(text=f"Exported: {datetime.fromtimestamp(float(stamp)).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
            except (ValueError, TypeError, OverflowError, OSError):
                pass
        elif data.get("timestamp"):
            layout.label_multiline(text=f"Recorded: {data['timestamp']}")

        if 'materials' in data or 'textures' in data:
            summary = layout.box()
            summary.label(text="Summary")
            if 'materials' in data:
                materials = data['materials']
                summary.label(text=f"Materials converted: {materials.get('converted', 0)}")
                summary.label(text=f"Materials failed: {materials.get('failed', 0)}")
            if 'textures' in data:
                textures = data['textures']
                summary.label(text=f"Textures copied: {textures.get('copied', 0)}")
                summary.label(text=f"Textures converted: {textures.get('converted', 0)}")

        from ..ui.draw_utils import draw_issue_list

        errors = diagnostic_errors(data)
        warnings = data.get('warnings', []) or []
        draw_issue_list(layout, errors, title="Errors", icon='ERROR', alert=True)
        draw_issue_list(layout, warnings, title="Warnings", icon='INFO')

        details = layout.operator(
            "usdstage.open_result_text", text="Open Full Report in Text Editor", icon='TEXT',
        )
        details.text_json = json.dumps({"result": metadata, "diagnostics": data}, indent=2, default=str)

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
        shown = _display_text(context, text_block)
        self.report({'INFO'}, f"{'Opened' if shown else 'Loaded text datablock'}: {text_name}")
        return {'FINISHED'}


class USDSTAGE_OT_open_result_text(Operator):
    """Show all feedback even when no diagnostics sidecar was saved."""
    bl_idname = "usdstage.open_result_text"
    bl_label = "Open Full Export Report"
    text_json: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def execute(self, context):
        content = self.text_json
        if not content:
            result = _latest_result(context)
            content = json.dumps({"result": result, "diagnostics": result_diagnostics(result)}, indent=2, default=str)
        text_name = "USD Stage Export Report"
        text_block = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
        text_block.clear()
        text_block.write(content)
        shown = _display_text(context, text_block)
        self.report({'INFO'}, f"{'Opened' if shown else 'Loaded text datablock'}: {text_name}")
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
        shown = _display_text(context, text_block)
        self.report({'INFO'}, f"{'Opened' if shown else 'Loaded text datablock'}: {self.text_name}")
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
    from ..ui.panel import _read_background_job_status
    settings = context.scene.usd_stage_export_settings
    return resolve_diagnostics_path(settings, _read_background_job_status(settings) or {}) or None


def _latest_result(context):
    """Match the sidebar's process-aware result instead of raw worker status."""
    from ..ui.panel import _read_background_job_status
    settings = context.scene.usd_stage_export_settings
    return get_latest_result(settings, _read_background_job_status(settings) or {})


def _display_text(context, text_block):
    """Show support content without replacing the artist's current workspace."""
    previous = {window.as_pointer() for window in context.window_manager.windows}
    if context.area is not None:
        try:
            bpy.ops.screen.area_dupli('INVOKE_DEFAULT')
            windows = [window for window in context.window_manager.windows if window.as_pointer() not in previous]
            if windows:
                area = windows[-1].screen.areas[0]
                area.type = 'TEXT_EDITOR'
                area.spaces.active.text = text_block
                return True
        except Exception:
            pass
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces.active.text = text_block
                area.tag_redraw()
                return True
    # Background/API invocation has no window. Its caller can still retrieve
    # the Text datablock; do not claim a window was opened.
    return False


class USDSTAGE_OT_reveal_export(Operator):
    """Reveal the delivered file's folder in the system file manager."""
    bl_idname = "usdstage.reveal_export"
    bl_label = "Reveal Export"
    filepath: StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        path = Path(self.filepath or _latest_result(context).get('export_path') or '')
        if not path.is_file():
            self.report({'ERROR'}, "The exported file is no longer available at this path.")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=str(path.parent))
        return {'FINISHED'}


class USDSTAGE_OT_use_translate_materials(Operator):
    """Use the material route that supports material variants."""
    bl_idname = "usdstage.use_translate_materials"
    bl_label = "Use Translate Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from ..export_profile import MATERIAL_TYPE_PBR, PBR_PROCESSING_TRANSLATE
        settings = context.scene.usd_stage_export_settings
        settings.ui_material_type = MATERIAL_TYPE_PBR
        settings.ui_pbr_processing = PBR_PROCESSING_TRANSLATE
        return {'FINISHED'}


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
    bpy.utils.register_class(USDSTAGE_OT_open_result_text)
    bpy.utils.register_class(USDSTAGE_OT_open_support_text)
    bpy.utils.register_class(USDSTAGE_OT_create_support_bundle)
    bpy.utils.register_class(USDSTAGE_OT_reveal_export)
    bpy.utils.register_class(USDSTAGE_OT_use_translate_materials)


def unregister():
    """Unregister operators"""
    bpy.utils.unregister_class(USDSTAGE_OT_use_translate_materials)
    bpy.utils.unregister_class(USDSTAGE_OT_reveal_export)
    bpy.utils.unregister_class(USDSTAGE_OT_create_support_bundle)
    bpy.utils.unregister_class(USDSTAGE_OT_open_support_text)
    bpy.utils.unregister_class(USDSTAGE_OT_open_result_text)
    bpy.utils.unregister_class(USDSTAGE_OT_open_diagnostics_text)
    bpy.utils.unregister_class(USDSTAGE_OT_show_diagnostics)
    bpy.utils.unregister_class(USDSTAGE_OT_export)
