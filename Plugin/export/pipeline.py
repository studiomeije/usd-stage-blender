"""One export, whichever front end starts it.

The Export button, ``usdstage export`` and the bake export all run the same
steps: describe the export in its diagnostics, decide which objects it covers,
refuse materials that cannot be translated, then write the USD, post-process
it, and package or publish it. Those steps live here, once. A front end adds
only what is its own - how it reports progress, and how it turns a refusal
into a message, an error code or a status line.

A refusal is an :class:`ExportRefused` carrying a stable ``code`` and the
``stage`` it happened in. The codes are public: the command line reports them,
and scripts match on them.

Export modules are imported when a step runs rather than at the top of this
file, so the package keeps loading without Blender and a test can stand in for
any one of them.
"""

from __future__ import annotations

from pathlib import Path


class ExportRefused(RuntimeError):
    """The export stopped on purpose, before anything was published.

    ``code`` and ``stage`` are the public error code and the step it happened
    in. A material refusal also names the ``material`` and lists its
    ``issues``, each a dict with ``node_name``, ``node_type`` and ``message``.
    """

    def __init__(self, message: str, *, code: str, stage: str, details=None,
                 material: str | None = None, issues=()):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details
        self.material = material
        self.issues = list(issues)


#: The steps of :func:`write_export`, in order, for a front end's progress.
STEP_EXPORT = "export"
STEP_POSTPROCESS = "postprocess"
STEP_PACKAGE = "package"
STEP_PUBLISH = "publish"

_EXTENSIONS = {"USDA": ".usda", "USDC": ".usdc", "USDZ": ".usdz"}


def extension_for(export_format: str) -> str:
    """The file extension an export format writes; USDZ for an unknown one."""
    return _EXTENSIONS.get(str(export_format).upper(), ".usdz")


def describe_export(diag, context, settings, *, command: str, filepath: str,
                    export_format: str, blend_file: str | None,
                    requested_path: str | None = None) -> None:
    """Fill in the header every export's diagnostics report starts with."""
    from .support_bundle import collect_environment, collect_scene_snapshot

    header = {
        "command": command,
        "resolved_output_path": filepath,
        "export_format": export_format,
        "selected_only": bool(getattr(settings, "selected_objects_only", False)),
        "clamp_specular_tint": bool(getattr(settings, "clamp_specular_tint", False)),
        "blend_file": blend_file or None,
    }
    if requested_path is not None:
        header["requested_path"] = requested_path
    diag.set_export_context(**header)
    diag.set_environment(**collect_environment(context))
    diag.data["scene"] = collect_scene_snapshot(context)


def validation_scope(context, settings):
    """The objects whose materials this export must accept, or None for all.

    A selected-only export covers the selection and what it depends on -
    armatures, collection prototypes, the other members of a variant set - so
    it is validated against exactly that, and an unrelated material elsewhere
    in the scene does not stop it. A full export covers every material.
    """
    if not bool(getattr(settings, "selected_objects_only", False)):
        return None
    from . import animation_export

    try:
        export_objects = animation_export.collect_export_objects(context, settings)
        processing_objects = (
            animation_export.collect_processing_objects(context, export_objects)
            if export_objects else None
        )
    except Exception as exc:
        raise ExportRefused(
            str(exc), code="INVALID_EXPORT_SELECTION", stage="validation"
        ) from exc
    if not export_objects:
        raise ExportRefused(
            "Selection Only is enabled, but no objects are selected.",
            code="NO_EXPORTABLE_OBJECTS",
            stage="validation",
        )
    return processing_objects


def materials_of(objects) -> list:
    """Every material the objects' slots hold, each once, in order."""
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


def validate_materials(context, settings, objects, diag) -> None:
    """Refuse the first material with a node the export cannot translate.

    Warnings are recorded and do not stop the export. ``objects`` is what
    :func:`validation_scope` returned.
    """
    from ..nodes import validate as rk_validate

    clamp_specular_tint = bool(getattr(settings, "clamp_specular_tint", False))
    materials = (
        materials_of(objects) if objects is not None
        else rk_validate.collect_scene_materials(context)
    )
    for material in materials:
        result = rk_validate.validate_material(
            material, strict=True, clamp_specular_tint=clamp_specular_tint,
        )
        for issue in result["warnings"]:
            diag.add_validation_issue(material.name, issue, severity="warning")
            diag.add_warning(
                f"{material.name}: {issue.get('message', 'Material export warning.')}"
            )
        if result["errors"]:
            for issue in result["errors"]:
                diag.add_validation_issue(material.name, issue, severity="error")
            raise ExportRefused(
                f"Unsupported nodes in material '{material.name}'.",
                code="UNSUPPORTED_MATERIAL_NODES",
                stage="validation",
                material=material.name,
                issues=result["errors"],
            )


def write_export(context, settings, filepath: str, export_format: str, diag, *,
                 staging_dir=None, after_postprocess=None, on_step=None) -> str:
    """Write the USD, post-process it, then package or publish it.

    Returns the staged USD path. Nothing is published unless every step
    succeeds: the previous file at ``filepath`` is left as it was.

    ``staging_dir`` is an attempt directory the caller already allocated, as
    the bake export does so the export keeps its baked textures; the caller
    then owns its cleanup. Without one, this allocates its own attempt and
    always removes it. ``after_postprocess(usd_path)`` runs once the stage is
    post-processed and saved, still inside that phase. ``on_step(step)`` is
    called as each of the ``STEP_*`` steps starts.
    """
    from . import blender_usd_export, pack_usdz, postprocess_usd

    step = on_step or (lambda _step: None)
    usd_path = None
    try:
        step(STEP_EXPORT)
        diag.begin_phase("blender_usd_export", {"output_path": filepath})
        extra = {} if staging_dir is None else {"reset_staging": False, "staging_dir": staging_dir}
        usd_path = blender_usd_export.export_blender_scene(context, settings, filepath, diag, **extra)
        if not usd_path or not Path(usd_path).exists():
            raise ExportRefused(
                "Blender USD export failed.",
                code="BLENDER_USD_EXPORT_FAILED",
                stage="blender_usd_export",
            )
        diag.end_phase("blender_usd_export", context={
            "temp_usd_path": usd_path, "file_size": _file_size(usd_path),
        })

        step(STEP_POSTPROCESS)
        diag.begin_phase("postprocess_usd", {"usd_path": usd_path})
        postprocess_usd.process_usd_stage(usd_path, settings, context, diag)
        if after_postprocess is not None:
            after_postprocess(usd_path)
        diag.end_phase("postprocess_usd")

        errors = diag.data.get("errors") or []
        if errors:
            raise ExportRefused(
                f"Post-processing errors ({len(errors)} total): "
                + "; ".join(str(error) for error in errors[:5]),
                code="POSTPROCESS_FAILED",
                stage="postprocess_usd",
                details=diag.data.get("material_issues"),
            )

        if str(export_format).upper() == "USDZ":
            step(STEP_PACKAGE)
            diag.begin_phase("pack_usdz", {"output_path": filepath})
            pack_usdz.create_usdz(usd_path, filepath, settings, context, diag)
            diag.end_phase("pack_usdz", context={"file_size": _file_size(filepath)})
        else:
            step(STEP_PUBLISH)
            if usd_path != filepath:
                blender_usd_export.publish_unpacked_export(usd_path, filepath, diag)
            else:
                diag.add_generated_file("export", filepath)
        return usd_path
    finally:
        # Only an attempt allocated here. A returned path names exactly that
        # attempt; a native export that raised has already cleaned its own.
        if staging_dir is None and usd_path:
            try:
                blender_usd_export.remove_export_staging_dir(
                    filepath, diag, staging_dir=Path(usd_path).parent,
                )
            except Exception:
                pass


def _file_size(path) -> int | None:
    try:
        return Path(path).stat().st_size
    except OSError:
        return None
