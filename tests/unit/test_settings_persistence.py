"""Last-used export settings seed only scenes that store none of their own."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_prefs_module(monkeypatch):
    bpy = ModuleType("bpy")
    bpy.__path__ = []
    bpy.context = None
    bpy_props = ModuleType("bpy.props")
    bpy_props.StringProperty = lambda **_kwargs: None
    bpy_props.EnumProperty = lambda **_kwargs: None
    bpy_types = ModuleType("bpy.types")
    bpy_types.AddonPreferences = type("AddonPreferences", (), {})
    bpy.types = bpy_types

    plugin_package = ModuleType("Plugin")
    plugin_package.__path__ = [str(_REPO_ROOT / "Plugin")]
    plugin_package.__package__ = "Plugin"

    monkeypatch.setitem(sys.modules, "Plugin", plugin_package)
    monkeypatch.setitem(sys.modules, "bpy", bpy)
    monkeypatch.setitem(sys.modules, "bpy.props", bpy_props)
    monkeypatch.setitem(sys.modules, "bpy.types", bpy_types)
    monkeypatch.delitem(sys.modules, "Plugin.prefs", raising=False)
    return importlib.import_module("Plugin.prefs")


class _FakeSettings:
    """A PropertyGroup stand-in: a value is saved (a key) only once set."""

    DEFAULTS = {
        "export_format": "USDZ",
        "filepath": "",
        "history_applied": False,
        "persist_suspended": False,
    }
    TYPES = {
        "export_format": "ENUM",
        "filepath": "STRING",
        "history_applied": "BOOLEAN",
        "persist_suspended": "BOOLEAN",
    }

    def __init__(self, raw_values=None):
        object.__setattr__(self, "_raw", dict(raw_values or {}))
        properties = [
            SimpleNamespace(
                identifier=key,
                type=prop_type,
                enum_items=[SimpleNamespace(identifier=v) for v in ("USDA", "USDC", "USDZ")]
                if key == "export_format" else [],
            )
            for key, prop_type in self.TYPES.items()
        ]
        object.__setattr__(self, "bl_rna", SimpleNamespace(properties=properties))

    def __getattr__(self, key):
        if key in self.DEFAULTS:
            return self._raw.get(key, self.DEFAULTS[key])
        raise AttributeError(key)

    def __setattr__(self, key, value):
        if key in self.DEFAULTS:
            self._raw[key] = value
            return
        object.__setattr__(self, key, value)

    def keys(self):
        return self._raw.keys()


def _install_preferences(monkeypatch, prefs_module, serialized=""):
    preferences = SimpleNamespace(last_export_settings_json=serialized)
    monkeypatch.setattr(prefs_module, "get_preferences", lambda _context=None: preferences)
    monkeypatch.setattr(prefs_module, "apply_last_export_path", lambda *_args: False)
    monkeypatch.setattr(prefs_module, "set_last_export_path", lambda *_args: None)
    return preferences


def _payload(**values):
    return json.dumps({
        "schema": "usdstage.export-settings",
        "version": 1,
        "profile": "REALITYKIT_OS27",
        "values": values,
    })


def test_a_scene_without_saved_settings_is_seeded_with_the_last_used_values(monkeypatch):
    prefs_module = _load_prefs_module(monkeypatch)
    _install_preferences(monkeypatch, prefs_module, _payload(export_format="USDC"))
    settings = _FakeSettings()

    assert prefs_module.apply_persisted_export_settings(object(), settings) == {"status": "current"}
    assert settings.export_format == "USDC"
    assert settings.history_applied is True


def test_a_scene_with_its_own_settings_keeps_them(monkeypatch):
    prefs_module = _load_prefs_module(monkeypatch)
    _install_preferences(monkeypatch, prefs_module, _payload(export_format="USDC"))
    settings = _FakeSettings({"export_format": "USDA"})

    assert prefs_module.apply_persisted_export_settings(object(), settings) == {"status": "scene_settings"}
    assert settings.export_format == "USDA"


def test_a_scene_is_seeded_once_per_session(monkeypatch):
    prefs_module = _load_prefs_module(monkeypatch)
    _install_preferences(monkeypatch, prefs_module, _payload(export_format="USDC"))
    settings = _FakeSettings({"history_applied": True})

    assert prefs_module.apply_persisted_export_settings(object(), settings) == {"status": "already_applied"}
    assert settings.export_format == "USDZ"


def test_persisted_values_roundtrip_through_the_payload(monkeypatch):
    prefs_module = _load_prefs_module(monkeypatch)
    preferences = _install_preferences(monkeypatch, prefs_module)
    source = _FakeSettings({"export_format": "USDC"})

    assert prefs_module.persist_export_settings(object(), source) is True
    assert json.loads(preferences.last_export_settings_json) == json.loads(_payload(export_format="USDC"))


def test_a_payload_from_another_schema_or_with_missing_keys_is_ignored(monkeypatch):
    prefs_module = _load_prefs_module(monkeypatch)
    for serialized, status in (
        (json.dumps({"export_format": "USDC"}), "invalid"),
        (_payload(), "invalid"),
        ("not json", "invalid"),
        ("", "missing"),
    ):
        _install_preferences(monkeypatch, prefs_module, serialized)
        settings = _FakeSettings()
        assert prefs_module.apply_persisted_export_settings(object(), settings) == {"status": status}
        assert settings.export_format == "USDZ"


def test_panel_and_operator_delegate_to_one_shared_settings_loader():
    panel_source = (_REPO_ROOT / "Plugin/ui/panel.py").read_text()
    operator_source = (_REPO_ROOT / "Plugin/ops/export_operator.py").read_text()

    assert "apply_persisted_export_settings" in panel_source
    assert "apply_persisted_export_settings" in operator_source
    assert "last_export_settings_json" not in panel_source
    assert "last_export_settings_json" not in operator_source
