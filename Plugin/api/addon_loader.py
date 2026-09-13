"""Add-on loading helpers shared by Blender-hosted entry points."""

from __future__ import annotations


ADDON_ID = "usd_stage"


def _candidate_module_names(addon_id: str = ADDON_ID) -> list[str]:
    """Return likely module names for the installed extension."""
    names: list[str] = []
    seen: set[str] = set()

    def add(name: str | None) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    try:
        import addon_utils

        for module in addon_utils.modules(refresh=True):
            module_name = getattr(module, "__name__", "")
            if module_name == addon_id or module_name.endswith(f".{addon_id}"):
                add(module_name)
    except Exception:
        pass

    try:
        import bpy

        for repository in bpy.context.preferences.extensions.repos:
            add(f"bl_ext.{repository.module}.{addon_id}")
    except Exception:
        add(f"bl_ext.user_default.{addon_id}")
        add(f"bl_ext.blender_local_addons.{addon_id}")
    add(addon_id)
    return names


def _current_addon_module() -> tuple[str | None, object | None]:
    """Return the root package that owns this loader module."""
    import sys

    package_name = __package__ or ""
    suffix = ".api"
    if not package_name.endswith(suffix):
        return None, None
    root_name = package_name[: -len(suffix)]
    return root_name, sys.modules.get(root_name)


def ensure_addon_loaded(
    addon_id: str = ADDON_ID,
    scene_attr: str = "usd_stage_export_settings",
) -> None:
    """Enable the add-on if its scene properties are not registered yet."""
    import bpy
    if hasattr(bpy.types.Scene, scene_attr):
        return

    failures = []
    current_name, current_module = _current_addon_module()
    candidates: list[str] = []
    discovered = (
        _candidate_module_names(addon_id)
        if current_name is None or current_name.startswith("bl_ext.")
        else []
    )
    for module_name in (current_name, *discovered):
        if module_name and module_name not in candidates:
            candidates.append(module_name)

    # Enabling through Blender preserves the canonical extension identity and
    # creates the add-on preferences entry.  This is the normal installed path.
    for module_name in candidates:
        try:
            bpy.ops.preferences.addon_enable(module=module_name)
        except Exception as exc:
            failures.append({"module": module_name, "error": str(exc)})
            continue
        if hasattr(bpy.types.Scene, scene_attr):
            return
        failures.append({"module": module_name, "error": f"'{scene_attr}' was not registered"})

    # A plain source checkout is not necessarily present in a Blender add-on
    # repository.  Register its already-loaded root directly as a final local
    # development fallback; never import the same files under another name.
    register = getattr(current_module, "register", None) if current_module is not None else None
    if callable(register):
        try:
            register()
        except Exception as exc:
            failures.append({"module": current_name, "error": str(exc)})
        else:
            if hasattr(bpy.types.Scene, scene_attr):
                return
            failures.append({"module": current_name, "error": f"'{scene_attr}' was not registered"})

    attempted = ", ".join(str(item["module"]) for item in failures) or addon_id
    raise RuntimeError(
        f"USD Stage for Blender add-on could not be loaded. Attempted: {attempted}. "
        f"Failures: {failures}"
    )
