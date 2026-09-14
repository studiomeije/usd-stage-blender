"""support_bundle command - create a redacted support ZIP."""

from __future__ import annotations


def handle(args: dict) -> dict:
    import bpy
    from ...export.support_bundle import create_support_bundle

    return create_support_bundle(
        context=bpy.context,
        blend_file=bpy.data.filepath or args.get("blend_file"),
        export_path=args.get("export_path"),
        diagnostics_path=args.get("diagnostics_path"),
        job_dir=args.get("job_dir"),
        bundle_output=args.get("bundle_output"),
        include_output=bool(args.get("include_output", False)),
        include_blend=bool(args.get("include_blend", False)),
        full_log=bool(args.get("full_log", False)),
        redact=not bool(args.get("no_redact", False)),
    )
