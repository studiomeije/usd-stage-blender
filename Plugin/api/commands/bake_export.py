"""bake_export command — bake textures and export scene.

This runs synchronously inside background Blender (blocking).  The global CLI
timeout is enforced by the bridge, while the per-step timeout is enforced here
inside the Blender worker so every bake/export stage gets its own budget.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ._settings_common import (
    INTERNAL_KEYS,
    PreparedSettingUpdate,
    apply_setting_updates_transactionally,
    attach_early_failure_diagnostics,
    get_settings,
    prepare_setting_update,
    setting_value_issue,
    suspend_setting_persistence,
)
from ..errors import CommandError
from ...cli.bridge import (
    OUTPUT_MARKER,
    StepTimeoutWatchdog,
    write_timeout_diagnostics,
)


def _prepare_positional_setting_overrides(
    prop_defs: dict,
    overrides: dict,
) -> tuple[list[PreparedSettingUpdate], list[dict], list[dict]]:
    """Stage positional ``key=value`` overrides without mutating settings."""
    prepared = []
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
            prepared.append(
                prepare_setting_update(
                    prop,
                    value,
                    key=key,
                    source="override",
                )
            )
        except Exception as exc:
            invalid_values.append(setting_value_issue(key, value, exc))
    return prepared, invalid_keys, invalid_values


def _prepare_direct_setting_overrides(
    prop_defs: dict,
    args: dict,
) -> tuple[list[PreparedSettingUpdate], list[dict]]:
    """Validate and stage all bake-export named arguments as one transaction."""
    prepared: list[PreparedSettingUpdate] = []
    invalid: list[dict] = []

    def stage(
        arg_key: str,
        setting_key: str,
        setting_value,
        *,
        input_value=None,
    ) -> PreparedSettingUpdate | None:
        prop = prop_defs.get(setting_key)
        shown_value = setting_value if input_value is None else input_value
        if prop is None:
            invalid.append(
                setting_value_issue(
                    arg_key,
                    shown_value,
                    "target setting is unavailable",
                    setting_key=setting_key,
                )
            )
            return None
        try:
            update = prepare_setting_update(
                prop,
                setting_value,
                key=setting_key,
                input_key=arg_key,
                input_value=shown_value,
                source="direct",
            )
        except Exception as exc:
            invalid.append(
                setting_value_issue(
                    arg_key,
                    shown_value,
                    exc,
                    setting_key=setting_key,
                )
            )
            return None
        prepared.append(update)
        return update

    direct_settings = {
        "format": "export_format",
        "bake_mode": "bake_mode",
        "image_format": "bake_image_format",
        "margin": "bake_margin",
        "ibl_source": "bake_ibl_source",
        "ibl_filepath": "bake_ibl_filepath",
        "ibl_strength": "bake_ibl_strength",
        "ibl_rotation": "bake_ibl_rotation",
        "isolate_meshes": "bake_isolate_meshes_lit",
        "timeout": "bake_step_timeout_seconds",
    }
    for arg_key, setting_key in direct_settings.items():
        if arg_key in args:
            stage(arg_key, setting_key, args[arg_key])

    if "resolution" in args:
        raw_resolution = args["resolution"]
        normalized = str(raw_resolution).strip().upper().replace("-", "_")
        stage(
            "resolution",
            "export_texture_settings_enabled",
            True,
            input_value=raw_resolution,
        )
        if normalized in {"ORIGINAL", "KEEP_ORIGINAL"}:
            stage(
                "resolution",
                "bake_resolution",
                "ORIGINAL",
                input_value=raw_resolution,
            )
        elif normalized in {"512", "1024", "2048", "4096"}:
            stage(
                "resolution",
                "bake_resolution",
                normalized,
                input_value=raw_resolution,
            )
        else:
            try:
                if isinstance(raw_resolution, float) and not raw_resolution.is_integer():
                    raise ValueError("custom resolution must be a whole number")
                custom_resolution = int(raw_resolution)
            except (TypeError, ValueError) as exc:
                invalid.append(
                    setting_value_issue("resolution", raw_resolution, exc)
                )
            else:
                stage(
                    "resolution",
                    "bake_resolution",
                    "CUSTOM",
                    input_value=raw_resolution,
                )
                stage(
                    "resolution",
                    "bake_resolution_custom",
                    custom_resolution,
                    input_value=raw_resolution,
                )

    for arg_key in ("image_format", "margin"):
        if arg_key in args:
            stage(
                arg_key,
                "export_texture_settings_enabled",
                True,
                input_value=args[arg_key],
            )

    # These are convenience flags rather than full setting replacements.  A
    # false token is a validated no-op, matching argparse's absent flag.
    flag_settings = {
        "selected_only": ("selected_objects_only", True),
        "no_base_color": ("bake_base_color", False),
        "no_opacity": ("bake_opacity", False),
        "diagnostics": ("diagnostics_enabled", True),
    }
    for arg_key, (setting_key, value_when_enabled) in flag_settings.items():
        if arg_key not in args:
            continue
        prop = prop_defs.get(setting_key)
        if prop is None:
            invalid.append(
                setting_value_issue(
                    arg_key,
                    args[arg_key],
                    "target setting is unavailable",
                    setting_key=setting_key,
                )
            )
            continue
        try:
            enabled = prepare_setting_update(
                prop,
                args[arg_key],
                key=setting_key,
                input_key=arg_key,
                source="direct",
            ).value
        except Exception as exc:
            invalid.append(
                setting_value_issue(
                    arg_key,
                    args[arg_key],
                    exc,
                    setting_key=setting_key,
                )
            )
            continue
        if enabled:
            stage(
                arg_key,
                setting_key,
                value_when_enabled,
                input_value=args[arg_key],
            )

    return prepared, invalid


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
                command="bake_export",
            )
            raise


def _handle(args: dict, settings) -> dict:
    filepath = args.get("filepath")
    if not filepath:
        raise ValueError("'filepath' is required (output path).")

    suppress_success_diagnostics = args.get("no_diagnostics", False)

    # Stage positional and named arguments before the first live RNA write.
    overrides = args.get("overrides", {})
    prop_defs = {prop.identifier: prop for prop in settings.bl_rna.properties}
    prepared_overrides, invalid_keys, invalid_values = (
        _prepare_positional_setting_overrides(prop_defs, overrides)
    )
    prepared_direct, invalid_direct = _prepare_direct_setting_overrides(
        prop_defs,
        args,
    )

    if invalid_keys or invalid_direct:
        raise CommandError(
            "Invalid bake-export setting override.",
            code="INVALID_SETTING_OVERRIDE",
            details=invalid_keys + invalid_values + invalid_direct,
        )
    if invalid_values:
        raise CommandError(
            "Invalid bake-export setting value.",
            code="INVALID_SETTING_VALUE",
            details=invalid_values,
        )

    assignment_errors = apply_setting_updates_transactionally(
        settings,
        prepared_overrides + prepared_direct,
    )
    if assignment_errors:
        has_direct_failure = any(
            issue.get("source") in {"direct", "transaction"}
            for issue in assignment_errors
        )
        raise CommandError(
            "Invalid bake-export setting override.",
            code=(
                "INVALID_SETTING_OVERRIDE"
                if has_direct_failure
                else "INVALID_SETTING_VALUE"
            ),
            details=assignment_errors,
        )

    requested_format = str(settings.export_format).upper()

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
    from ...export import diagnostics
    from ...export.support_bundle import collect_environment, collect_scene_snapshot

    diag = diagnostics.ExportDiagnostics()
    success_diagnostics_enabled = (
        bool(getattr(settings, "diagnostics_enabled", False))
        and not suppress_success_diagnostics
    )
    # All failures must leave an actionable report. This path is therefore
    # allocated independently from the success-sidecar preference.
    diagnostics_path = str(Path(filepath).with_suffix(".diagnostics.json"))
    diag.set_export_context(
        command="bake_export",
        requested_path=args.get("filepath"),
        resolved_output_path=filepath,
        export_format=requested_format,
        selected_only=bool(getattr(settings, "selected_objects_only", False)),
        blend_file=bpy.data.filepath or None,
    )
    diag.set_environment(**collect_environment(bpy.context))
    diag.data["scene"] = collect_scene_snapshot(bpy.context)
    diag.data.setdefault("validation", {})["skipped"] = True
    diag.data["validation"]["reason"] = (
        "A baked export bakes source materials first; source material graph "
        "validation applies only to direct exports."
    )

    start_time = time.time()
    try:
        step_timeout_seconds = max(
            0,
            int(getattr(settings, "bake_step_timeout_seconds", 0) or 0),
        )
    except (TypeError, ValueError):
        step_timeout_seconds = 0

    # The timeout callback runs off Blender's main thread. Capture anything
    # that touches bpy now; the callback itself must use immutable Python data.
    timeout_artifacts = _artifacts(
        diagnostics_path,
        filepath,
        bpy.data.filepath,
    )
    timeout_diagnostic_base = json.loads(json.dumps(diag.data, default=str))

    def _step_timed_out(step: str, elapsed: float, limit: float) -> None:
        """Flush a structured response before best-effort diagnostics."""
        limit_seconds = int(limit)
        elapsed_seconds = round(float(elapsed), 2)
        message = (
            f"Bake/export step '{step}' timed out after {limit_seconds}s; "
            "the Blender worker was terminated."
        )
        timeout_details = {
            "code": "BAKE_STEP_TIMEOUT",
            "stage": step,
            "timeout_seconds": limit_seconds,
            "elapsed_seconds": elapsed_seconds,
        }

        response = {
            "ok": False,
            "schema_version": "1.0",
            "command": "bake_export",
            "error": {
                "code": "BAKE_STEP_TIMEOUT",
                "type": "CommandError",
                "message": message,
                "stage": step,
                "details": {
                    "timeout_seconds": limit_seconds,
                    "elapsed_seconds": elapsed_seconds,
                },
            },
            "context": {
                "stage": step,
                "timeout_seconds": limit_seconds,
                "elapsed_seconds": elapsed_seconds,
            },
            "artifacts": timeout_artifacts,
        }
        # This envelope is the CLI contract and must be emitted before any
        # optional diagnostic I/O. The bridge treats BAKE_STEP_TIMEOUT as
        # authoritative even if another response raced with process exit.
        print(
            f"{OUTPUT_MARKER}{json.dumps(response, default=str)}{OUTPUT_MARKER}",
            flush=True,
        )
        print(message, file=sys.stderr, flush=True)

        # Diagnostic persistence is best-effort: a disk error must never hide
        # the already-flushed structured timeout response.
        try:
            write_timeout_diagnostics(
                diagnostics_path,
                timeout_diagnostic_base,
                timeout_details,
                message,
            )
        except Exception as exc:
            print(
                f"Unable to write timeout diagnostics: {exc}",
                file=sys.stderr,
                flush=True,
            )

    step_watchdog = StepTimeoutWatchdog(
        step_timeout_seconds,
        _step_timed_out,
    )
    step_watchdog.start("Preparing bake")

    try:
        outcome = run_bake_export(
            bpy.context,
            settings,
            filepath,
            requested_format,
            diag,
            diagnostics_path=diagnostics_path,
            # bake_textures reports a distinct label before every blocking bpy
            # bake operation, so each one gets a fresh step budget.
            on_step=lambda _progress, message: step_watchdog.enter_step(message),
        )

        saved_diagnostics_path = None
        if success_diagnostics_enabled:
            step_watchdog.enter_step("Writing diagnostics")
            _save_diagnostics(diag, diagnostics_path)
            saved_diagnostics_path = diagnostics_path

        return {
            "ok": True,
            "export_path": filepath,
            "format": requested_format,
            "duration_seconds": round(time.time() - start_time, 2),
            "bake_stats": outcome["bake_stats"],
            "warnings": list(diag.data.get("warnings") or []),
            "diagnostics_path": saved_diagnostics_path,
            "support_bundle_hint": _support_hint(bpy.data.filepath, filepath, saved_diagnostics_path),
        }
    except CommandError as exc:
        if exc.stage:
            diag.record_phase_error(exc.stage, exc)
        diag.add_exception(exc, stage=exc.stage or "bake_export")
        _save_diagnostics(diag, diagnostics_path)
        exc.artifacts.update(_artifacts(diagnostics_path, filepath, bpy.data.filepath))
        raise
    except Exception as exc:
        diag.add_exception(exc, stage="bake_export")
        _save_diagnostics(diag, diagnostics_path)
        raise CommandError(
            str(exc),
            code="BAKE_EXPORT_FAILED",
            stage="bake_export",
            artifacts=_artifacts(diagnostics_path, filepath, bpy.data.filepath),
        ) from exc
    finally:
        # Unconditional: a live watchdog thread reports a timeout that did
        # not happen and calls os._exit(124), masking the real error.
        step_watchdog.stop()


def run_bake_export(
    context,
    settings,
    filepath: str,
    requested_format: str,
    diag,
    *,
    diagnostics_path: str | None,
    on_step=lambda _progress, _message: None,
) -> dict:
    """Bake the scene's materials, export, and restore the scene.

    Shared by the ``bake-export`` command and the sidebar's background job.
    ``on_step(progress, message)`` is called as each step starts, with
    ``progress`` between 0 and 1. Refusals raise ``CommandError`` with the
    public code; other failures propagate. A scene-restore step that fails is
    recorded as a diagnostics error and listed in ``restore_failures`` of the
    returned dict, or of the exception's ``restore_failures`` attribute.
    """
    import bpy
    from ...export import (
        animation_export,
        asset_preflight,
        bake_finalize,
        bake_textures,
        blender_usd_export,
        pack_usdz,
        postprocess_usd,
    )
    from ...ops import bake_export_operator as bake_ops

    # The native USD selection scope stays separate from the processing scope.
    # Blender expands collection instances itself; selecting their prototype
    # objects as well would export those objects twice. Baking and asset
    # preflight still have to inspect those prototype meshes and materials.
    objects_to_export = bake_ops._collect_export_objects(context, settings)
    if not objects_to_export:
        raise CommandError(
            "No exportable objects found.",
            code="NO_EXPORTABLE_OBJECTS",
            stage="validation",
        )

    processing_objects = animation_export.collect_processing_objects(
        context,
        objects_to_export,
    )
    missing_images = asset_preflight.collect_missing_image_files_for_objects(
        processing_objects,
        bpy,
    )
    if missing_images:
        asset_preflight.record_missing_image_files(diag, missing_images)
        raise CommandError(
            asset_preflight.missing_images_status_message(missing_images),
            code=asset_preflight.missing_assets_error_code(missing_images),
            stage="asset_preflight",
            details=missing_images,
        )

    original_selection = list(context.selected_objects)
    original_active = context.view_layer.objects.active
    original_mode = original_active.mode if original_active else "OBJECT"
    original_engine = context.scene.render.engine
    original_force_unlit = getattr(settings, "force_unlit_materials", False)
    processing_link_state = {
        "scene": context.scene,
        "temporary_scene_links": [],
    }
    bake_result = None
    staging_dir = None
    temp_usd_path = None
    restore_failures: list[str] = []
    outcome: dict = {"restore_failures": restore_failures}
    primary_error = None

    try:
        on_step(0.1, "Preparing Blender scene")
        bake_ops._ensure_object_mode(context)
        bake_ops._set_render_engine(context.scene, "CYCLES")
        # Collection prototypes are often not linked into the active scene,
        # which makes them unavailable to the bake operators. Link them only
        # for processing, and unlink them before the native USD export, whose
        # selected-only scope must stay exact.
        animation_export._link_processing_objects_for_bake(
            context,
            processing_objects,
            processing_link_state,
        )

        # One staging attempt, allocated before baking and passed to the native
        # export, so the export keeps the freshly baked textures.
        staging_dir = blender_usd_export.create_export_staging_dir(filepath, diag)
        texture_dir = staging_dir / "textures"
        bake_block = bake_textures.record_bake_diagnostics(
            diag,
            settings,
            object_count=len(processing_objects),
            native_export_object_count=len(objects_to_export),
            texture_dir=texture_dir,
        )

        on_step(0.15, "Baking textures")
        diag.begin_phase("bake_textures", {"texture_dir": str(texture_dir)})
        bake_result = bake_textures.bake_materials_for_objects(
            context,
            settings,
            processing_objects,
            texture_dir,
            diag,
            progress_callback=lambda progress, message: on_step(
                0.15 + 0.35 * max(0.0, min(1.0, float(progress))), message
            ),
        )
        diag.end_phase("bake_textures")

        # Lit PBR only for RealityKit PBR ▸ Bake Materials; every other bake
        # mode authors the unlit surface.
        on_step(0.48, "Finalizing baked materials")
        bake_finalize.apply_force_unlit(settings)

        _unlink_processing_scope(
            animation_export,
            processing_link_state,
            strict=True,
        )
        # Select last, so the bake's own selection changes cannot widen a
        # selected-only export.
        if getattr(settings, "selected_objects_only", False):
            animation_export._set_export_selection(context, objects_to_export)

        on_step(0.5, "Exporting USD")
        diag.begin_phase("blender_usd_export", {"output_path": filepath})
        temp_usd_path = blender_usd_export.export_blender_scene(
            context,
            settings,
            filepath,
            diag,
            reset_staging=False,
            staging_dir=staging_dir,
        )
        if not temp_usd_path or not Path(temp_usd_path).exists():
            raise CommandError(
                "Blender USD export failed.",
                code="BLENDER_USD_EXPORT_FAILED",
                stage="blender_usd_export",
            )
        diag.end_phase(
            "blender_usd_export",
            context={
                "temp_usd_path": temp_usd_path,
                "file_size": Path(temp_usd_path).stat().st_size,
            },
        )

        on_step(0.7, "Post-processing USD")
        diag.begin_phase("postprocess_usd", {"usd_path": temp_usd_path})
        postprocess_usd.process_usd_stage(temp_usd_path, settings, context, diag)
        bake_textures.remove_superseded_bake_outputs(
            temp_usd_path,
            staging_dir,
            bake_result,
            diag,
        )
        diag.end_phase("postprocess_usd")

        if diag.data.get("errors"):
            errors = diag.data["errors"][:5]
            raise CommandError(
                f"Post-processing errors ({len(diag.data['errors'])} total): "
                + "; ".join(str(e) for e in errors),
                code="POSTPROCESS_FAILED",
                stage="postprocess_usd",
                details=diag.data.get("material_issues"),
            )

        if requested_format == "USDZ":
            on_step(0.85, "Packaging USDZ")
            diag.begin_phase("pack_usdz", {"output_path": filepath})
            pack_usdz.create_usdz(temp_usd_path, filepath, settings, context, diag)
            diag.end_phase(
                "pack_usdz",
                context={"file_size": Path(filepath).stat().st_size if Path(filepath).exists() else None},
            )
        else:
            on_step(0.9, "Publishing USD export")
            if temp_usd_path != filepath:
                blender_usd_export.publish_unpacked_export(temp_usd_path, filepath, diag)
            else:
                diag.add_generated_file("export", filepath)

        outcome["bake_stats"] = {
            "objects_baked": sum(
                1 for obj in processing_objects
                if getattr(obj, "type", None) == "MESH"
            ),
            "resolution": bake_block["resolution"],
            "image_format": bake_block["image_format"],
        }
        return outcome
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        on_step(0.98, "Restoring Blender scene")

        # Every restore step is independent: one failure must not skip the
        # rest, and none may replace the primary error.
        def _restore(label: str, action) -> None:
            try:
                action()
            except Exception as exc:
                restore_failures.append(f"{label}: {exc}")
                try:
                    diag.add_error(f"Could not restore {label}: {exc}")
                except Exception:
                    pass

        _restore(
            "the temporary bake dependencies",
            lambda: _unlink_processing_scope(
                animation_export, processing_link_state, strict=True
            ),
        )
        _restore(
            "the unlit material override",
            lambda: setattr(settings, "force_unlit_materials", original_force_unlit),
        )
        _restore(
            "the render engine",
            lambda: setattr(context.scene.render, "engine", original_engine),
        )
        if bake_result is not None:
            _restore(
                "the original material slots",
                lambda: bake_textures.restore_baked_materials(bake_result),
            )
        _restore(
            "the object selection",
            lambda: bake_ops._restore_selection(
                context, original_selection, original_active
            ),
        )
        _restore(
            "the object mode",
            lambda: bake_ops._restore_mode(context, original_active, original_mode),
        )
        # Clean only this attempt: the directory the returned USD proves, or,
        # before the export returned, the one allocated for the bake.
        cleanup_staging_dir = (
            Path(temp_usd_path).parent if temp_usd_path else staging_dir
        )
        if cleanup_staging_dir is not None:
            _restore(
                "the export staging directory",
                lambda: blender_usd_export.remove_export_staging_dir(
                    filepath,
                    diag,
                    staging_dir=cleanup_staging_dir,
                ),
            )
        if primary_error is not None:
            try:
                primary_error.restore_failures = list(restore_failures)
            except Exception:
                pass
        if restore_failures:
            try:
                _save_diagnostics(diag, diagnostics_path)
            except Exception:
                pass


def _unlink_processing_scope(animation_export, state: dict, *, strict: bool) -> None:
    """Unlink temporary processing dependencies and fail closed before USD.

    ``animation_export`` owns the canonical link/unlink implementation.  Its
    cleanup is intentionally best-effort for animation restoration, so this
    bake path adds a verification step: a prototype that remains scene-linked
    would be emitted as a second native USD object during a full-scene export.
    Failed links are put back into the state so the outer ``finally`` retries.
    """
    linked = list(state.get("temporary_scene_links", []) or [])
    if not linked:
        return

    animation_export._unlink_temporary_processing_objects(state)
    scene = state.get("scene")
    remaining = []
    if scene is not None:
        for obj in linked:
            try:
                candidate = scene.objects.get(str(getattr(obj, "name", "")))
                if (
                    candidate is not None
                    and animation_export._rna_identity(candidate)
                    == animation_export._rna_identity(obj)
                ):
                    remaining.append(obj)
            except Exception:
                # If membership cannot be verified, fail closed rather than
                # risking duplicate native export content.
                remaining.append(obj)

    if not remaining:
        return

    state["temporary_scene_links"] = remaining
    if strict:
        names = ", ".join(
            repr(str(getattr(obj, "name", "<unknown>")))
            for obj in remaining[:5]
        )
        if len(remaining) > 5:
            names += f", and {len(remaining) - 5} more"
        raise RuntimeError(
            "Could not restore the native USD scene scope after texture bake; "
            f"temporary dependencies remain linked: {names}."
        )


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
