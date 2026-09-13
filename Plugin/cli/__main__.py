#!/usr/bin/env python3
"""
USD Stage for Blender CLI — command-line interface for the USD Stage for Blender add-on.

Usage::

    python3 <Plugin-path> <command> [options]
    usdstage <command> [options]

All commands output JSON to stdout and status messages to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bridge


class CLIError(RuntimeError):
    """Local CLI validation failure with a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        exit_code: int = 1,
        details: dict | list | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.details = details


class CLIUsageError(CLIError):
    """Argument-parser rejection that follows the public exit-code contract."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INVALID_ARGUMENTS", exit_code=1)


class CLIArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that reports errors through the structured CLI path."""

    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def _error_envelope(
    message: str,
    *,
    code: str,
    command: str | None,
    error_type: str,
    details: dict | list | None = None,
) -> dict:
    error = {
        "code": code,
        "type": error_type,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return {
        "ok": False,
        "schema_version": "1.0",
        "command": command,
        "error": error,
        "context": {},
        "artifacts": {},
    }


def _print_json(data, pretty: bool = True) -> None:
    """Print JSON to stdout."""
    if pretty:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(json.dumps(data, default=str))


def _log(msg: str, quiet: bool = False) -> None:
    """Print status message to stderr."""
    if not quiet:
        print(msg, file=sys.stderr)


def _nonnegative_seconds(value: str) -> int:
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("timeout must be 0 or a positive number of seconds")
    return seconds


#: Commands that do real work run without a time limit unless --timeout sets
#: one; a Lighting & Shadows bake of a large scene takes well over ten minutes.
_UNLIMITED_BY_DEFAULT = frozenset({"export", "bake_export", "support_bundle"})
_DEFAULT_TIMEOUT_SECONDS = 600


def _timeout_for(command: str, requested: int | None) -> int | None:
    """The Blender subprocess time limit in seconds, or None for no limit."""
    if requested is None:
        return None if command in _UNLIMITED_BY_DEFAULT else _DEFAULT_TIMEOUT_SECONDS
    return requested or None


def _run(command: str, args: dict, parsed: argparse.Namespace) -> dict:
    """Run a command through the bridge with common options."""
    timeout = _timeout_for(command, parsed.timeout)
    return bridge.run(
        command=command,
        args=args,
        blend_file=getattr(parsed, "blend_file", None),
        blender_path=parsed.blender,
        timeout=timeout,
        verbose=parsed.verbose,
    )


def _result_exit_code(result: dict) -> int:
    """Map a structured command result to the process exit contract.

    The bridge unwraps the API runner envelope and normally raises for runner
    failures.  Some commands also use an inner ``ok`` field for a completed
    command whose requested operation was rejected (validation is the common
    example).  Those results must still make shell pipelines fail.  Results
    without an ``ok`` field are successful legacy/read-only command payloads.
    """
    return 1 if isinstance(result, dict) and result.get("ok") is False else 0


def _collect_overrides(tokens) -> dict:
    """Parse ``key=value`` override tokens; reject malformed ones loudly."""
    overrides = {}
    for token in tokens or []:
        if "=" not in token:
            raise CLIError(
                f"Invalid override '{token}' (expected key=value, e.g. bake-resolution=1024).",
                code="INVALID_OVERRIDE",
                details={"token": token},
            )
        key, value = token.split("=", 1)
        overrides[key.replace("-", "_")] = value
    return overrides


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_version(parsed: argparse.Namespace) -> int:
    result = _run("version", {}, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_info(parsed: argparse.Namespace) -> int:
    result = _run("info", {}, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_list_objects(parsed: argparse.Namespace) -> int:
    args = {}
    if parsed.type:
        args["type"] = parsed.type
    if parsed.selected:
        args["selected"] = True
    result = _run("list_objects", args, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_list_materials(parsed: argparse.Namespace) -> int:
    args = {}
    if parsed.unused:
        args["unused"] = True
    result = _run("list_materials", args, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_validate(parsed: argparse.Namespace) -> int:
    args = {}
    if parsed.material:
        args["material"] = parsed.material
    if parsed.only_errors:
        args["only_errors"] = True
    if parsed.clamp_specular_tint:
        args["clamp_specular_tint"] = True
    result = _run("validate", args, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_settings_get(parsed: argparse.Namespace) -> int:
    args = {}
    if parsed.keys:
        args["keys"] = [key.replace("-", "_") for key in parsed.keys]
    if parsed.group:
        args["group"] = parsed.group
    result = _run("settings_get", args, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_settings_set(parsed: argparse.Namespace) -> int:
    # Parse key=value pairs
    settings = {}
    for pair in parsed.settings:
        if "=" not in pair:
            raise CLIError(
                f"Invalid setting format: '{pair}'. Expected key=value.",
                code="INVALID_SETTING_FORMAT",
                details={"token": pair},
            )
        key, value = pair.split("=", 1)
        # Hyphens and underscores in a key are interchangeable, as they are for
        # export overrides; the documented contract is the same for both.
        settings[key.strip().replace("-", "_")] = value.strip()
    args = {"settings": settings}
    if parsed.save:
        args["save"] = True
    if parsed.dry_run:
        args["dry_run"] = True
    result = _run("settings_set", args, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_settings_list(parsed: argparse.Namespace) -> int:
    # settings list needs blender but no specific .blend file
    # We pass a dummy blend_file=None, the runner handles it
    result = _run("settings_list", {}, parsed)
    _print_json(result)
    return _result_exit_code(result)



def _print_success_warnings(result: dict) -> None:
    """Surface a successful export's warnings on stderr.

    Not gated by --quiet: that flag suppresses progress, and "your baked
    textures will be black" is not progress. In --json mode the same list is
    already in the payload.
    """
    for warning in result.get("warnings") or []:
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_export(parsed: argparse.Namespace) -> int:
    args = {"filepath": parsed.output}
    output_format = _resolve_output_format(parsed.output, parsed.format)
    if output_format:
        args["format"] = output_format
    if parsed.selected_only:
        args["selected_only"] = True
    if parsed.no_diagnostics:
        args["no_diagnostics"] = True
    if parsed.diagnostics:
        args["diagnostics"] = True

    overrides = _collect_overrides(parsed.overrides)
    if overrides:
        args["overrides"] = overrides

    _log(f"Exporting to {parsed.output}...", parsed.quiet)
    result = _run("export", args, parsed)
    _print_json(result)
    exit_code = _result_exit_code(result)
    if exit_code == 0:
        _print_success_warnings(result)
        _log(f"Done in {result.get('duration_seconds', '?')}s", parsed.quiet)
    return exit_code


def cmd_bake_export(parsed: argparse.Namespace) -> int:
    args = {"filepath": parsed.output}

    output_format = _resolve_output_format(parsed.output, parsed.format)
    if output_format:
        args["format"] = output_format
    if parsed.bake_mode:
        args["bake_mode"] = parsed.bake_mode
    if parsed.resolution:
        args["resolution"] = parsed.resolution
    if parsed.image_format:
        args["image_format"] = parsed.image_format
    if parsed.margin is not None:
        args["margin"] = parsed.margin
    if parsed.selected_only:
        args["selected_only"] = True
    if parsed.no_diagnostics:
        args["no_diagnostics"] = True
    if parsed.diagnostics:
        args["diagnostics"] = True
    if parsed.ibl_source:
        args["ibl_source"] = parsed.ibl_source
    if parsed.ibl_filepath:
        args["ibl_filepath"] = parsed.ibl_filepath
    if parsed.ibl_strength is not None:
        args["ibl_strength"] = parsed.ibl_strength
    if parsed.ibl_rotation is not None:
        args["ibl_rotation"] = parsed.ibl_rotation
    if parsed.isolate_meshes:
        args["isolate_meshes"] = True
    if parsed.no_base_color:
        args["no_base_color"] = True
    if parsed.no_opacity:
        args["no_opacity"] = True
    if parsed.timeout_step is not None:
        args["timeout"] = parsed.timeout_step

    overrides = _collect_overrides(parsed.overrides)
    if parsed.roughness_mode:
        overrides["bake_roughness_mode"] = parsed.roughness_mode
    if overrides:
        args["overrides"] = overrides

    _log(f"Baking & exporting to {parsed.output}...", parsed.quiet)
    result = _run("bake_export", args, parsed)
    _print_json(result)
    exit_code = _result_exit_code(result)
    if exit_code == 0:
        _print_success_warnings(result)
        _log(f"Done in {result.get('duration_seconds', '?')}s", parsed.quiet)
    return exit_code


def cmd_support_bundle(parsed: argparse.Namespace) -> int:
    args = {
        "blend_file": parsed.blend_file,
    }
    if parsed.export_path:
        args["export_path"] = parsed.export_path
    if parsed.bundle_output:
        args["bundle_output"] = parsed.bundle_output
    if parsed.job_dir:
        args["job_dir"] = parsed.job_dir
    if parsed.diagnostics_path:
        args["diagnostics_path"] = parsed.diagnostics_path
    if parsed.include_output:
        args["include_output"] = True
    if parsed.include_blend:
        args["include_blend"] = True
    if parsed.full_log:
        args["full_log"] = True
    if parsed.no_redact:
        args["no_redact"] = True

    _log("Creating support bundle...", parsed.quiet)
    result = _run("support_bundle", args, parsed)
    _print_json(result)
    exit_code = _result_exit_code(result)
    if exit_code == 0:
        _log(f"Support bundle: {result.get('support_bundle_path', '?')}", parsed.quiet)
    return exit_code


def cmd_preferences_get(parsed: argparse.Namespace) -> int:
    result = _run("preferences_get", {}, parsed)
    _print_json(result)
    return _result_exit_code(result)


def cmd_preferences_set(parsed: argparse.Namespace) -> int:
    settings = {}
    for pair in parsed.settings:
        if "=" not in pair:
            raise CLIError(
                f"Invalid preference format: '{pair}'. Expected key=value.",
                code="INVALID_PREFERENCE_FORMAT",
                details={"token": pair},
            )
        key, value = pair.split("=", 1)
        settings[key.strip().replace("-", "_")] = value.strip()
    result = _run("preferences_set", {"settings": settings}, parsed)
    _print_json(result)
    return _result_exit_code(result)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


_OUTPUT_HELP = (
    "Output file path. A .usda, .usdc or .usdz extension chooses the format; "
    "any other name gets the format's extension"
)
_FORMAT_HELP = (
    "Export format (default: from the output extension, else the scene's export_format)"
)
_FORMAT_EXTENSIONS = {".usda": "USDA", ".usdc": "USDC", ".usdz": "USDZ"}


def _add_diagnostics_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--diagnostics", action="store_true",
        help="Keep the diagnostics JSON sidecar after a successful export",
    )
    group.add_argument(
        "--no-diagnostics", action="store_true",
        help="Do not keep success diagnostics (failures always write them)",
    )


def _resolve_output_format(output: str, requested: str | None) -> str | None:
    """The export format: --format, else the output's USD extension, else None.

    A USD extension that disagrees with --format is refused rather than
    rewritten, so `-o scene.usda --format USDZ` cannot silently write a USDZ.
    """
    extension = Path(output).suffix.lower()
    from_extension = _FORMAT_EXTENSIONS.get(extension)
    if requested and from_extension and requested != from_extension:
        raise CLIUsageError(
            f"-o {output} has a {extension} extension but --format is {requested}; "
            "make them agree or drop one",
        )
    return requested or from_extension


def _add_global_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    def default(value):
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument(
        "--blender", default=default(None),
        help="Path to Blender executable (default: $USDSTAGE_BLENDER or 'blender')",
    )
    parser.add_argument(
        "--json", dest="json_only", action="store_true", default=default(False),
        help="Also print failures as a JSON envelope on stdout (implies --quiet); "
             "successful results are always JSON",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=default(False),
        help="Print Blender startup output to stderr (not redacted)",
    )
    parser.add_argument(
        "--quiet", action="store_true", default=default(False),
        help="Suppress progress messages on stderr",
    )
    parser.add_argument(
        "--timeout", dest="timeout", type=_nonnegative_seconds, default=default(None),
        help="Overall Blender subprocess timeout in seconds, 0 for no limit "
             "(default: no limit for export, bake-export and support-bundle, "
             "600 for the other commands)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = CLIArgumentParser(
        prog="usdstage",
        description="USD Stage for Blender CLI — export, bake, validate, and manage settings.",
    )

    _add_global_options(parser, suppress_defaults=False)
    # The same options are accepted after the subcommand too. There they
    # default to SUPPRESS, so a subcommand never resets a value given before it.
    global_options = argparse.ArgumentParser(add_help=False)
    _add_global_options(global_options, suppress_defaults=True)

    subs = parser.add_subparsers(dest="command", required=True)

    # --- version ---
    p = subs.add_parser(
        parents=[global_options],
        name="version", help="Print version information")
    p.set_defaults(func=cmd_version)

    # --- info ---
    p = subs.add_parser(
        parents=[global_options],
        name="info", help="Get scene metadata from a .blend file")
    p.add_argument("blend_file", help="Path to .blend file")
    p.set_defaults(func=cmd_info)

    # --- list-objects ---
    p = subs.add_parser(
        parents=[global_options],
        name="list-objects", help="List objects in the scene")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("--type", action="append", help="Filter by object type (repeatable)")
    p.add_argument("--selected", action="store_true", help="Only selected objects")
    p.set_defaults(func=cmd_list_objects)

    # --- list-materials ---
    p = subs.add_parser(
        parents=[global_options],
        name="list-materials", help="List materials in the file")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("--unused", action="store_true", help="Include unused materials")
    p.set_defaults(func=cmd_list_materials)

    # --- validate ---
    p = subs.add_parser(
        parents=[global_options],
        name="validate", help="Check materials for RealityKit compatibility")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("--material", help="Validate a single material by name")
    p.add_argument("--only-errors", action="store_true", help="Suppress warnings")
    p.add_argument(
        "--clamp-specular-tint",
        action="store_true",
        help=(
            "Validate with Clamp Overbright Specular Tint on: a constant, uncoloured "
            "Specular Tint above 1 is clamped for the export instead of refused"
        ),
    )
    p.set_defaults(func=cmd_validate)

    # --- settings (subcommand group) ---
    settings_parser = subs.add_parser(
        parents=[global_options],
        name="settings", help="Read or modify export settings")
    settings_subs = settings_parser.add_subparsers(dest="settings_command", required=True)

    # settings get
    p = settings_subs.add_parser(
        parents=[global_options],
        name="get", help="Read export settings")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("--keys", nargs="+", help="Return only these keys")
    p.add_argument(
        "--group",
        help=(
            "Return settings from a group: all, general, geometry, "
            "rigging, texture, materials, bake, diagnostics"
        ),
    )
    p.set_defaults(func=cmd_settings_get)

    # settings set
    p = settings_subs.add_parser(
        parents=[global_options],
        name="set", help="Modify export settings")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("settings", nargs="+", metavar="key=value", help="Settings to change")
    p.add_argument("--save", action="store_true", help="Save the .blend file after applying")
    p.add_argument("--dry-run", action="store_true", help="Validate without applying")
    p.set_defaults(func=cmd_settings_set)

    # settings list
    p = settings_subs.add_parser(
        parents=[global_options],
        name="list", help="List all setting keys with schema info")
    p.set_defaults(func=cmd_settings_list)

    # --- export ---
    p = subs.add_parser(
        parents=[global_options],
        name="export", help="Export scene to USDA, USDC or USDZ")
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("-o", "--output", required=True, help=_OUTPUT_HELP)
    p.add_argument(
        "--format",
        choices=["USDA", "USDC", "USDZ"],
        help=_FORMAT_HELP,
    )
    p.add_argument("--selected-only", action="store_true", help="Export selected objects only")
    _add_diagnostics_options(p)
    p.add_argument(
        "overrides", nargs="*", metavar="key=value",
        help="Setting overrides, anywhere after the blend file",
    )
    p.set_defaults(func=cmd_export)

    # --- bake-export ---
    p = subs.add_parser(
        parents=[global_options],
        name="bake-export",
        help="Bake textures and export scene to USDA, USDC or USDZ",
    )
    p.add_argument("blend_file", help="Path to .blend file")
    p.add_argument("-o", "--output", required=True, help=_OUTPUT_HELP)
    p.add_argument(
        "--format",
        choices=["USDA", "USDC", "USDZ"],
        help=_FORMAT_HELP,
    )
    p.add_argument("--bake-mode", choices=["UNLIT_ALBEDO", "LIT_ALBEDO", "LIT_IBL"], help="What to bake: UNLIT_ALBEDO = RealityKit Unlit, Material Color Only; LIT_ALBEDO = RealityKit PBR, Bake Materials; LIT_IBL = RealityKit Unlit, Lighting & Shadows (the default for a new scene)")
    p.add_argument("--resolution", help="Bake/export texture resolution (ORIGINAL, 512, 1024, 2048, 4096, or custom int)")
    p.add_argument("--image-format", choices=["ORIGINAL", "AVIF", "PNG"], help="Baked/exported texture format")
    p.add_argument("--margin", type=int, help="Bake padding in pixels")
    p.add_argument("--selected-only", action="store_true", help="Only bake/export selected objects")
    _add_diagnostics_options(p)
    # Lighting source options for LIT_IBL mode.
    p.add_argument("--ibl-source", choices=["SCENE_WORLD", "HDRI_FILE"], help="Lighting source for LIT_IBL mode")
    p.add_argument("--ibl-filepath", help="Path to HDRI file")
    p.add_argument("--ibl-strength", type=float, help="Lighting strength multiplier")
    p.add_argument("--ibl-rotation", type=float, help="Lighting-source Z rotation in radians")
    p.add_argument("--isolate-meshes", action="store_true", help="Isolate meshes during Lighting & Shadows bake")
    # Channel options
    p.add_argument("--no-base-color", action="store_true", help="Skip base color bake")
    p.add_argument("--no-opacity", action="store_true", help="Skip opacity bake")
    # Advanced
    p.add_argument("--roughness-mode", choices=["TEXTURE", "AVERAGE"], help="LIT_ALBEDO roughness output: full texture or averaged constant")
    p.add_argument(
        "--step-timeout", dest="timeout_step", type=_nonnegative_seconds,
        help="Per-bake/export-step worker timeout in seconds, 0 for no limit "
             "(for the overall Blender process timeout use global --timeout)",
    )
    p.add_argument(
        "overrides", nargs="*", metavar="key=value",
        help="Setting overrides, anywhere after the blend file",
    )
    p.set_defaults(func=cmd_bake_export)

    # --- support-bundle ---
    p = subs.add_parser(
        parents=[global_options],
        name="support-bundle", help="Create a redacted support bundle")
    p.add_argument("blend_file", nargs="?", help="Path to the .blend file that was exported (optional)")
    p.add_argument("--export-path", help="The export to collect details about")
    p.add_argument("-o", "--bundle-output", dest="bundle_output", help="Destination ZIP path")
    p.add_argument("--job-dir", help="Background bake/export job directory")
    p.add_argument("--diagnostics-path", help="Diagnostics JSON to include")
    p.add_argument("--include-output", action="store_true", help="Include exported USD/USDZ and sidecar assets")
    p.add_argument("--include-blend", action="store_true", help="Include the source .blend file")
    p.add_argument("--full-log", action="store_true", help="Include full redacted logs instead of the last 2000 lines")
    p.add_argument("--no-redact", action="store_true", help="Disable support bundle redaction")
    p.set_defaults(func=cmd_support_bundle)

    # --- preferences (subcommand group) ---
    prefs_parser = subs.add_parser(
        parents=[global_options],
        name="preferences", help="Read or modify add-on preferences")
    prefs_subs = prefs_parser.add_subparsers(dest="prefs_command", required=True)

    p = prefs_subs.add_parser(
        parents=[global_options],
        name="get", help="Read add-on preferences")
    p.set_defaults(func=cmd_preferences_get)

    p = prefs_subs.add_parser(
        parents=[global_options],
        name="set", help="Modify add-on preferences")
    p.add_argument("settings", nargs="+", metavar="key=value", help="Preferences to change")
    p.set_defaults(func=cmd_preferences_set)

    return parser


_COMMAND_NAMES = {
    "version",
    "info",
    "list-objects",
    "list-materials",
    "validate",
    "settings",
    "export",
    "bake-export",
    "support-bundle",
    "preferences",
}


def _command_from_argv(argv: list[str]) -> str | None:
    return next((token for token in argv if token in _COMMAND_NAMES), None)


def _report_cli_error(
    exc: CLIError,
    *,
    json_only: bool,
    command: str | None,
    parser: argparse.ArgumentParser | None = None,
) -> int:
    if json_only:
        _print_json(
            _error_envelope(
                str(exc),
                code=exc.code,
                command=command,
                error_type=exc.__class__.__name__,
                details=exc.details,
            )
        )
    else:
        if isinstance(exc, CLIUsageError) and parser is not None:
            parser.print_usage(sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
    return exc.exit_code


def _parse_arguments(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    """Parse argv, accepting ``key=value`` overrides after other options too.

    argparse only collects a positional list where it first starts, so an
    override written after ``-o`` would be rejected. Leftover ``key=value``
    tokens are appended to the overrides of commands that take them.
    """
    parsed, extras = parser.parse_known_args(argv)
    if hasattr(parsed, "overrides"):
        stray = [token for token in extras if "=" in token and not token.startswith("-")]
        parsed.overrides = list(parsed.overrides or []) + stray
        extras = [token for token in extras if token not in stray]
    if extras:
        parser.error(f"unrecognized arguments: {' '.join(extras)}")
    return parsed


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]
    json_requested = "--json" in argv
    command = _command_from_argv(argv)
    try:
        parsed = _parse_arguments(parser, argv)
    except CLIError as exc:
        return _report_cli_error(
            exc,
            json_only=json_requested,
            command=command,
            parser=parser,
        )

    # Handle --json flag: implies --quiet
    if parsed.json_only:
        parsed.quiet = True

    try:
        return parsed.func(parsed)
    except CLIError as exc:
        return _report_cli_error(
            exc,
            json_only=parsed.json_only,
            command=parsed.command,
        )
    except bridge.BridgeError as exc:
        msg = str(exc)
        if parsed.json_only:
            _print_json(exc.to_json())
        else:
            print(f"Error: {msg}", file=sys.stderr)
            if exc.response:
                artifacts = exc.response.get("artifacts") or {}
                diagnostics_path = artifacts.get("diagnostics_path")
                if diagnostics_path:
                    print(f"Diagnostics: {diagnostics_path}", file=sys.stderr)
                support_hint = artifacts.get("support_bundle_hint")
                if support_hint:
                    print(f"Support bundle: {support_hint}", file=sys.stderr)
        if exc.error_code in {"BLENDER_NOT_FOUND", "BLENDER_START_FAILED"}:
            return 2
        if exc.error_code == "ADDON_LOAD_FAILED":
            return 3
        return 1
    except RuntimeError as exc:
        msg = str(exc)
        if parsed.json_only:
            _print_json(
                _error_envelope(
                    msg,
                    code="CLI_RUNTIME_ERROR",
                    command=parsed.command,
                    error_type=exc.__class__.__name__,
                )
            )
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if parsed.json_only:
            _print_json(
                _error_envelope(
                    "Command interrupted by user.",
                    code="INTERRUPTED",
                    command=parsed.command,
                    error_type="KeyboardInterrupt",
                )
            )
        else:
            print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
