"""Load this extension under one package name in standalone entry points.

Blender extensions are imported below a repository namespace such as
``bl_ext.user_default.usd_stage``.  The CLI and background worker scripts
are also executable by file path, where Python does not provide a package
context.  This helper finds the package that owns a specific extension root or
loads it once under a private fallback name.  It intentionally never aliases
the package to ``Plugin``: aliases can cause Python to load every child module
twice under two different names.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _module_init_path(module: ModuleType) -> Path | None:
    """Return the resolved package initializer for ``module`` when available."""
    module_file = getattr(module, "__file__", None)
    module_path = getattr(module, "__path__", None)
    if not module_file or module_path is None:
        return None
    try:
        return Path(module_file).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _matching_loaded_package(init_path: Path) -> tuple[str, ModuleType] | None:
    for name, module in tuple(sys.modules.items()):
        if module is None:
            continue
        if _module_init_path(module) == init_path:
            return name, module
    return None


def _configured_extension_names(root: Path) -> list[str]:
    """Return canonical ``bl_ext`` names whose repository entry owns ``root``."""
    try:
        import bpy  # type: ignore
    except Exception:
        return []

    try:
        repositories = tuple(bpy.context.preferences.extensions.repos)
    except Exception:
        return []

    root_resolved = root.resolve()
    names: list[str] = []
    for repository in repositories:
        repository_module = str(getattr(repository, "module", "") or "")
        repository_directory = getattr(repository, "directory", None)
        if not repository_module or not repository_directory:
            continue
        try:
            entries = tuple(Path(repository_directory).iterdir())
        except (OSError, TypeError, ValueError):
            continue
        for entry in entries:
            try:
                owns_root = entry.is_dir() and entry.resolve() == root_resolved
            except (OSError, RuntimeError):
                owns_root = False
            if owns_root:
                names.append(f"bl_ext.{repository_module}.{entry.name}")
    return names


def load_extension_package(root: str | Path) -> tuple[str, ModuleType]:
    """Load and return the package rooted at ``root`` without module aliases."""
    package_root = Path(root).resolve()
    init_path = package_root / "__init__.py"
    if not init_path.is_file():
        raise RuntimeError(f"USD Stage for Blender package initializer is missing: {init_path}")
    init_path = init_path.resolve()

    loaded = _matching_loaded_package(init_path)
    if loaded is not None:
        return loaded

    # Installed Blender extensions must retain the repository namespace that
    # Blender uses for enabling, preferences, and class ownership.  Matching by
    # resolved directory also supports development symlinks into a repository.
    for package_name in _configured_extension_names(package_root):
        try:
            module = importlib.import_module(package_name)
        except Exception:
            continue
        if _module_init_path(module) == init_path:
            return package_name, module

    # A source checkout is intentionally named ``Plugin``.  Import it normally
    # so existing development commands keep their natural package identity.
    source_init = package_root.parent / "Plugin" / "__init__.py"
    if source_init.is_file() and source_init.resolve() == init_path:
        parent = str(package_root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        module = importlib.import_module("Plugin")
        return "Plugin", module

    # A copied, non-installed extension folder has no canonical import name.
    # Give that exact root a stable private name instead of impersonating the
    # source package or a Blender repository namespace.
    digest = hashlib.sha256(str(package_root).encode("utf-8")).hexdigest()[:12]
    package_name = f"_usdstage_runtime_{digest}"
    existing = sys.modules.get(package_name)
    if existing is not None:
        if _module_init_path(existing) != init_path:
            raise RuntimeError(
                f"Runtime package name collision for USD Stage for Blender root: {package_root}"
            )
        return package_name, existing

    spec = importlib.util.spec_from_file_location(
        package_name,
        init_path,
        submodule_search_locations=[str(package_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not create a package loader for {package_root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(package_name, None)
        raise
    return package_name, module
