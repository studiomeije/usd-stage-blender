#!/usr/bin/env python3
"""
Entry point for running USD Stage for Blender as a CLI tool.

Usage::

    python3 /path/to/Plugin <command> [options]
    python3 -m Plugin.cli <command> [options]

This allows the installed Blender addon to double as a CLI tool
without any additional installation.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

_plugin_dir = Path(__file__).resolve().parent
_bootstrap_path = _plugin_dir / "core" / "package_bootstrap.py"
_bootstrap_spec = importlib.util.spec_from_file_location(
    "_usdstage_package_bootstrap",
    _bootstrap_path,
)
if _bootstrap_spec is None or _bootstrap_spec.loader is None:
    raise RuntimeError(f"Could not load USD Stage for Blender package bootstrap: {_bootstrap_path}")
_bootstrap_module = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(_bootstrap_module)
_package_name, _package_module = _bootstrap_module.load_extension_package(_plugin_dir)
main = importlib.import_module(f"{_package_name}.cli.__main__").main

sys.exit(main())
