"""Tests for Blender addon auto-loading helpers."""

from __future__ import annotations

from types import SimpleNamespace

from Plugin.api import addon_loader
from Plugin.api.addon_loader import _candidate_module_names, ensure_addon_loaded


def test_candidate_module_names_prefer_discovered_extension_module(monkeypatch):
    fake_addon_utils = SimpleNamespace(
        modules=lambda refresh=True: [
            SimpleNamespace(
                __name__="bl_ext.user_default.usd_stage",
                bl_info={"name": "USD Stage for Blender"},
            )
        ]
    )

    monkeypatch.setitem(__import__("sys").modules, "addon_utils", fake_addon_utils)

    names = _candidate_module_names()

    assert names[0] == "bl_ext.user_default.usd_stage"
    assert names.count("bl_ext.user_default.usd_stage") == 1
    assert "usd_stage" in names


def test_ensure_addon_loaded_enables_discovered_module(monkeypatch):
    scene_type = type("Scene", (), {})
    calls: list[str] = []

    def addon_enable(*, module: str):
        calls.append(module)
        if module == "bl_ext.user_default.usd_stage":
            setattr(scene_type, "usd_stage_export_settings", object())
            return {"FINISHED"}
        raise RuntimeError("module not found")

    fake_addon_utils = SimpleNamespace(
        modules=lambda refresh=True: [
            SimpleNamespace(
                __name__="bl_ext.user_default.usd_stage",
                bl_info={"name": "USD Stage for Blender"},
            )
        ]
    )
    fake_bpy = SimpleNamespace(
        types=SimpleNamespace(Scene=scene_type),
        ops=SimpleNamespace(
            preferences=SimpleNamespace(addon_enable=addon_enable),
        ),
    )

    monkeypatch.setitem(__import__("sys").modules, "addon_utils", fake_addon_utils)
    monkeypatch.setitem(__import__("sys").modules, "bpy", fake_bpy)
    monkeypatch.setattr(addon_loader, "_current_addon_module", lambda: (None, None))

    ensure_addon_loaded()

    assert calls == ["bl_ext.user_default.usd_stage"]
    assert hasattr(scene_type, "usd_stage_export_settings")
