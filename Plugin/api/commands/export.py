"""export command — export scene to USD/USDZ."""

from __future__ import annotations

import os
import time
from pathlib import Path

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

    # Enforce extension
    ext_map = {
        "USDA": ".usda",
        "USDC": ".usdc",
        "USDZ": ".usdz",
    }
    ext = ext_map.get(requested_format, ".usdz")
    filepath = str(Path(filepath).with_suffix(ext))
    settings.filepath = filepath
    import bpy
    from ...export import (
        animation_export,
        blender_usd_export,
        diagnostics,
        pack_usdz,
        postprocess_usd,
    )
    from ...export.support_bundle import collect_environment, collect_scene_snapshot
    from ...nodes import validate as rk_validate

    clamp_specular_tint = bool(
        getattr(settings, "clamp_specular_tint", False)
    )

    diag = diagnostics.ExportDiagnostics()
    success_diagnostics_enabled = (
        bool(getattr(settings, "diagnostics_enabled", False))
        and not suppress_success_diagnostics
    )
    # Failure diagnostics are mandatory. The setting and CLI switches only
    # control whether successful exports retain the same sidecar.
    diagnostics_path = str(Path(filepath).with_suffix(".diagnostics.json"))
    diag.set_export_context(
        command="export",
        requested_path=args.get("filepath"),
        resolved_output_path=filepath,
        export_format=requested_format,
        selected_only=bool(getattr(settings, "selected_objects_only", False)),
        clamp_specular_tint=clamp_specular_tint,
        blend_file=bpy.data.filepath or None,
    )
    diag.set_environment(**collect_environment(bpy.context))
    diag.data["scene"] = collect_scene_snapshot(bpy.context)

    validation_objects = None
    if bool(getattr(settings, "selected_objects_only", False)):
        try:
            export_objects = animation_export.collect_export_objects(
                bpy.context,
                settings,
            )
            if export_objects:
                validation_objects = animation_export.collect_processing_objects(
                    bpy.context,
                    export_objects,
                )
        except Exception as exc:
            _save_diagnostics(diag, diagnostics_path)
            raise CommandError(
                str(exc),
                code="INVALID_EXPORT_SELECTION",
                stage="validation",
                artifacts=_artifacts(
                    diagnostics_path,
                    filepath,
                    bpy.data.filepath,
                ),
            ) from exc
        if not export_objects:
            _save_diagnostics(diag, diagnostics_path)
            raise CommandError(
                "Selection Only is enabled, but no objects are selected.",
                code="NO_EXPORTABLE_OBJECTS",
                stage="validation",
                artifacts=_artifacts(diagnostics_path, filepath, bpy.data.filepath),
            )

    # Validate materials (strict mode — same as the operator)
    materials = (
        _collect_materials_from_objects(validation_objects)
        if validation_objects is not None
        else rk_validate.collect_scene_materials(bpy.context)
    )
    for mat in materials:
        result = rk_validate.validate_material(
            mat,
            strict=True,
            clamp_specular_tint=clamp_specular_tint,
        )
        for issue in result["warnings"]:
            diag.add_validation_issue(mat.name, issue, severity="warning")
            diag.add_warning(
                f"{mat.name}: {issue.get('message', 'Material export warning.')}"
            )
        if result["errors"]:
            for issue in result["errors"]:
                diag.add_validation_issue(mat.name, issue, severity="error")
            error_msgs = [
                f"{e.get('node_name', '?')} ({e.get('node_type', '?')}): {e.get('message', '')}"
                for e in result["errors"][:10]
            ]
            _save_diagnostics(diag, diagnostics_path)
            raise CommandError(
                f"Unsupported nodes in material '{mat.name}'.",
                code="UNSUPPORTED_MATERIAL_NODES",
                stage="validation",
                # Strip the live bpy node objects: they are not JSON
                # serializable, and leaving them in made the CLI die with
                # "Object of type ShaderNodeMixShader is not JSON serializable"
                # instead of reporting this refusal.
                details=[
                    {k: v for k, v in issue.items() if k != "node"}
                    for issue in result["errors"]
                ],
                artifacts=_artifacts(diagnostics_path, filepath, bpy.data.filepath),
                context={"material": mat.name, "errors": error_msgs},
            )

    start_time = time.time()
    temp_usd_path = None
    try:
        # Step 1: Export from Blender to USD
        diag.begin_phase("blender_usd_export", {"output_path": filepath})
        temp_usd_path = blender_usd_export.export_blender_scene(
            bpy.context, settings, filepath, diag
        )
        if not temp_usd_path or not os.path.exists(temp_usd_path):
            raise CommandError(
                "Blender USD export failed.",
                code="BLENDER_USD_EXPORT_FAILED",
                stage="blender_usd_export",
            )
        diag.end_phase(
            "blender_usd_export",
            context={
                "temp_usd_path": temp_usd_path,
                "file_size": Path(temp_usd_path).stat().st_size if Path(temp_usd_path).exists() else None,
            },
        )

        # Step 2: Post-process and enforce the Apple Y-up stage contract.
        diag.begin_phase("postprocess_usd", {"usd_path": temp_usd_path})
        postprocess_usd.process_usd_stage(
            temp_usd_path, settings, bpy.context, diag,
        )
        diag.end_phase("postprocess_usd")

        if diag.data.get("errors"):
            errors = diag.data["errors"][:5]
            _save_diagnostics(diag, diagnostics_path)
            raise CommandError(
                f"Post-processing errors ({len(diag.data['errors'])} total): "
                + "; ".join(str(e) for e in errors),
                code="POSTPROCESS_FAILED",
                stage="postprocess_usd",
                details=diag.data.get("material_issues"),
                artifacts=_artifacts(diagnostics_path, filepath, bpy.data.filepath),
            )

        # Step 3: Package USDZ if needed
        if requested_format == "USDZ":
            diag.begin_phase("pack_usdz", {"output_path": filepath})
            pack_usdz.create_usdz(temp_usd_path, filepath, settings, bpy.context, diag)
            diag.end_phase(
                "pack_usdz",
                context={"file_size": Path(filepath).stat().st_size if Path(filepath).exists() else None},
            )
        else:
            if temp_usd_path != filepath:
                blender_usd_export.publish_unpacked_export(
                    temp_usd_path, filepath, diag
                )
            else:
                diag.add_generated_file("export", filepath)
    except CommandError as exc:
        if exc.stage:
            diag.record_phase_error(exc.stage, exc)
        diag.add_exception(exc, stage=exc.stage or "export")
        _save_diagnostics(diag, diagnostics_path)
        exc.artifacts.update(_artifacts(diagnostics_path, filepath, bpy.data.filepath))
        raise
    except Exception as exc:
        diag.add_exception(exc, stage="export")
        _save_diagnostics(diag, diagnostics_path)
        raise CommandError(
            str(exc),
            code="EXPORT_FAILED",
            stage="export",
            artifacts=_artifacts(diagnostics_path, filepath, bpy.data.filepath),
        ) from exc
    finally:
        # A returned USD path identifies the exact unique attempt to clean.
        # If native export fails before returning, it cleans its own attempt.
        if temp_usd_path:
            try:
                blender_usd_export.remove_export_staging_dir(
                    filepath,
                    diag,
                    staging_dir=Path(temp_usd_path).parent,
                )
            except Exception:
                pass

    duration = time.time() - start_time

    # Save diagnostics
    saved_diagnostics_path = None
    if success_diagnostics_enabled:
        _save_diagnostics(diag, diagnostics_path)
        saved_diagnostics_path = diagnostics_path

    return {
        "ok": True,
        "export_path": filepath,
        "format": requested_format,
        "duration_seconds": round(duration, 2),
        # A successful export can still carry findings the artist must see -
        # measured: a no-light Lighting & Shadows bake produced a black asset
        # with ok: true and the (excellent) warning reachable only inside a
        # sidecar the UI hides. Success payloads now say so directly.
        "warnings": list(diag.data.get("warnings") or []),
        "diagnostics_path": saved_diagnostics_path,
        "support_bundle_hint": _support_hint(bpy.data.filepath, filepath, saved_diagnostics_path),
    }


def _collect_materials_from_objects(objects) -> list:
    """Collect each material referenced by an exact export object closure."""
    materials = []
    seen: set[int] = set()
    for obj in objects or []:
        for slot in getattr(obj, "material_slots", []) or []:
            material = getattr(slot, "material", None)
            if material is None:
                continue
            try:
                identity = int(material.as_pointer())
            except Exception:
                identity = id(material)
            if identity in seen:
                continue
            seen.add(identity)
            materials.append(material)
    return materials


def _save_diagnostics(diag, diagnostics_path: str | None) -> None:
    if not diagnostics_path:
        return
    diag.set_artifact("diagnostics_path", diagnostics_path)
    diag.save(Path(diagnostics_path))


def _artifacts(diagnostics_path: str | None, output_path: str, blend_file: str | None) -> dict:
    return {
        "diagnostics_path": diagnostics_path,
        "support_bundle_hint": _support_hint(blend_file, output_path, diagnostics_path),
    }


def _support_hint(blend_file: str | None, output_path: str | None, diagnostics_path: str | None = None) -> str | None:
    if not blend_file:
        return None
    parts = ["usdstage", "support-bundle", blend_file]
    if output_path:
        parts.extend(["--export-path", output_path])
    if diagnostics_path:
        parts.extend(["--diagnostics-path", diagnostics_path])
    return " ".join(parts)
