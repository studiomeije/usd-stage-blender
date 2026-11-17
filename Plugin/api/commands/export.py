"""export command — export scene to USD/USDZ."""

from __future__ import annotations

import time
from pathlib import Path

from ._export_common import artifacts, command_error, save_diagnostics, support_hint

from ._settings_common import (
    INTERNAL_KEYS,
    apply_setting_updates_transactionally,
    attach_early_failure_diagnostics,
    get_settings,
    prepare_setting_update,
    setting_value_issue,
    suspend_setting_persistence,
)
from ..errors import CommandError


def handle(args: dict) -> dict:
    settings = get_settings()
    with suspend_setting_persistence(settings):
        try:
            return _handle(args, settings)
        except CommandError as exc:
            attach_early_failure_diagnostics(
                exc,
                args,
                settings,
                command="export",
            )
            raise


def _handle(args: dict, settings) -> dict:
    filepath = args.get("filepath")
    if not filepath:
        raise ValueError("'filepath' is required (output path).")

    suppress_success_diagnostics = args.get("no_diagnostics", False)

    # Apply overrides without persisting them
    overrides = args.get("overrides", {})
    prop_defs = {prop.identifier: prop for prop in settings.bl_rna.properties}
    prepared_overrides = []
    invalid_keys = []
    invalid_values = []
    for key, value in overrides.items():
        if key in INTERNAL_KEYS:
            invalid_keys.append(
                setting_value_issue(key, value, "internal setting")
            )
            continue
        prop = prop_defs.get(key)
        if prop is None:
            invalid_keys.append(
                setting_value_issue(key, value, "unknown setting")
            )
            continue
        try:
            prepared_overrides.append(
                prepare_setting_update(
                    prop,
                    value,
                    key=key,
                    source="override",
                )
            )
        except Exception as exc:
            invalid_values.append(setting_value_issue(key, value, exc))

    if invalid_keys:
        raise CommandError(
            "Invalid export setting override.",
            code="INVALID_SETTING_OVERRIDE",
            details=invalid_keys + invalid_values,
        )
    if invalid_values:
        raise CommandError(
            "Invalid export setting value.",
            code="INVALID_SETTING_VALUE",
            details=invalid_values,
        )

    assignment_errors = apply_setting_updates_transactionally(
        settings,
        prepared_overrides,
    )
    if assignment_errors:
        raise CommandError(
            "Invalid export setting value.",
            code="INVALID_SETTING_VALUE",
            details=assignment_errors,
        )

    fmt = args.get("format")
    requested_format = (
        str(fmt).upper() if fmt else str(settings.export_format).upper()
    )
    settings.export_format = requested_format

    if args.get("selected_only"):
        settings.selected_objects_only = True

    if args.get("diagnostics"):
        settings.diagnostics_enabled = True

    import bpy
    from ...export import diagnostics, pipeline

    filepath = str(Path(filepath).with_suffix(pipeline.extension_for(requested_format)))
    settings.filepath = filepath
    blend_file = bpy.data.filepath

    diag = diagnostics.ExportDiagnostics()
    success_diagnostics_enabled = (
        bool(getattr(settings, "diagnostics_enabled", False))
        and not suppress_success_diagnostics
    )
    # Failure diagnostics are mandatory. The setting and CLI switches only
    # control whether successful exports retain the same sidecar.
    diagnostics_path = str(Path(filepath).with_suffix(".diagnostics.json"))
    pipeline.describe_export(
        diag, bpy.context, settings,
        command="export",
        requested_path=args.get("filepath"),
        filepath=filepath,
        export_format=requested_format,
        blend_file=blend_file,
    )

    def refused(refusal):
        save_diagnostics(diag, diagnostics_path)
        return command_error(refusal, artifacts=artifacts(diagnostics_path, filepath, blend_file))

    try:
        scope = pipeline.validation_scope(bpy.context, settings)
        pipeline.validate_materials(bpy.context, settings, scope, diag)
    except pipeline.ExportRefused as refusal:
        raise refused(refusal) from refusal.__cause__

    start_time = time.time()
    try:
        pipeline.write_export(bpy.context, settings, filepath, requested_format, diag)
    except pipeline.ExportRefused as refusal:
        diag.record_phase_error(refusal.stage, refusal)
        diag.add_exception(refusal, stage=refusal.stage)
        raise refused(refusal) from refusal
    except Exception as exc:
        diag.add_exception(exc, stage="export")
        save_diagnostics(diag, diagnostics_path)
        raise CommandError(
            str(exc),
            code="EXPORT_FAILED",
            stage="export",
            artifacts=artifacts(diagnostics_path, filepath, blend_file),
        ) from exc
    duration = time.time() - start_time

    saved_diagnostics_path = None
    if success_diagnostics_enabled:
        save_diagnostics(diag, diagnostics_path)
        saved_diagnostics_path = diagnostics_path

    return {
        "ok": True,
        "export_path": filepath,
        "format": requested_format,
        "duration_seconds": round(duration, 2),
        # A successful export can still carry findings the user must see, so
        # the payload lists them rather than leaving them in the sidecar.
        "warnings": list(diag.data.get("warnings") or []),
        "diagnostics_path": saved_diagnostics_path,
        "support_bundle_hint": support_hint(blend_file, filepath, saved_diagnostics_path),
    }
