"""
USD Stage for Blender headless API runner.

Designed to be invoked by Blender in background mode::

    blender --background [file.blend] --python -m Plugin.api.runner -- '{"command":"...","args":{...}}'

Or as a standalone script::

    blender --background [file.blend] --python /path/to/runner.py -- '{"command":"...","args":{...}}'

Prints a JSON result to stdout delimited by markers so the CLI bridge
can extract it from Blender's noisy startup output.
"""

from __future__ import annotations

import json
import importlib
import importlib.util
import sys
import traceback
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap_module():
    bootstrap_path = _PLUGIN_ROOT / "core" / "package_bootstrap.py"
    spec = importlib.util.spec_from_file_location(
        "_usdstage_package_bootstrap",
        bootstrap_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load USD Stage for Blender package bootstrap: {bootstrap_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bootstrap = _load_bootstrap_module()
_PACKAGE_NAME, _PACKAGE_MODULE = _bootstrap.load_extension_package(_PLUGIN_ROOT)
_addon_loader = importlib.import_module(f"{_PACKAGE_NAME}.api.addon_loader")
_errors = importlib.import_module(f"{_PACKAGE_NAME}.api.errors")

_load_usdstage_addon = _addon_loader.ensure_addon_loaded
CommandError = _errors.CommandError
json_safe = _errors.json_safe

OUTPUT_MARKER = "---USDSTAGE_JSON---"


def _ensure_addon_loaded() -> None:
    """Enable the USD Stage for Blender add-on if it isn't already registered."""
    _load_usdstage_addon()


def _output(data: dict) -> None:
    """Print JSON result wrapped in markers so the bridge can parse it."""
    payload = json_safe(data)
    print(
        f"{OUTPUT_MARKER}{json.dumps(payload, allow_nan=False)}{OUTPUT_MARKER}",
        flush=True,
    )


def _redact_home(text: str) -> str:
    """Replace the user's home directory with ``$HOME``.

    Uses the same placeholder as the support bundle, which already redacts
    before writing anything a user might attach to a public issue.
    """
    try:
        home = str(Path.home())
    except Exception:
        return text
    return text.replace(home, "$HOME") if home else text


def _error_response(command: str | None, exc: Exception, tb: str | None = None) -> dict:
    if isinstance(exc, CommandError):
        error = exc.to_response_error()
        artifacts = exc.artifacts
        context = exc.context
    else:
        error = {
            "code": exc.__class__.__name__.upper(),
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        artifacts = {}
        context = {}
    # A CommandError is a diagnosed, user-facing condition - a bad setting key,
    # a missing texture - not an internal fault, so a Python traceback adds
    # nothing a caller can act on and leaks the install layout. Keep tracebacks
    # for genuine faults only, and strip the home directory from those: this
    # envelope is what users paste into public issues, and unlike the support
    # bundle it was never redacted.
    if tb and not isinstance(exc, CommandError):
        error["traceback"] = _redact_home(tb)
    return json_safe({
        "ok": False,
        "schema_version": "1.0",
        "command": command,
        "error": error,
        "context": context,
        "artifacts": artifacts,
    })


def main() -> int:
    argv = sys.argv
    if "--" not in argv:
        _output({"ok": False, "error": "No request payload. Pass JSON after '--'."})
        return 1

    raw = argv[argv.index("--") + 1]
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, IndexError) as exc:
        _output({"ok": False, "error": f"Invalid JSON payload: {exc}"})
        return 1

    command = request.get("command")
    args = request.get("args", {})

    if not command:
        _output({"ok": False, "error": "Missing 'command' in request."})
        return 1

    # Only version is fully self-contained; the other commands need the addon
    # registered even if they do not require a specific .blend file.
    NO_BLEND_COMMANDS = {"version"}

    if command not in NO_BLEND_COMMANDS:
        try:
            _ensure_addon_loaded()
        except Exception as exc:
            tb = traceback.format_exc()
            load_error = CommandError(
                str(exc),
                code="ADDON_LOAD_FAILED",
                stage="addon_load",
            )
            _output(_error_response(command, load_error, tb))
            print(tb, file=sys.stderr)
            return 1

    try:
        REGISTRY = importlib.import_module(f"{_PACKAGE_NAME}.api.commands").REGISTRY
    except Exception as exc:
        tb = traceback.format_exc()
        load_error = CommandError(
            f"Failed to import command registry: {exc}",
            code="ADDON_LOAD_FAILED",
            stage="command_registry",
        )
        _output(_error_response(command, load_error, tb))
        print(tb, file=sys.stderr)
        return 1

    handler = REGISTRY.get(command)
    if handler is None:
        _output({
            "ok": False,
            "error": f"Unknown command: '{command}'",
            "available": sorted(REGISTRY.keys()),
        })
        return 1

    try:
        result = handler(args)
        _output({"ok": True, "result": result})
        return 0
    except Exception as exc:
        tb = traceback.format_exc()
        _output(_error_response(command, exc, tb))
        print(tb, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
