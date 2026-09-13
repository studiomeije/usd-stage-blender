"""Muted nodes and links must not be evaluated as if they were live.

Blender bypasses a muted node and treats a muted link as absent. Nothing in the
material pipeline read either flag, so the extractor walked straight through
them and the exported material could silently disagree with the viewport - a
muted Mix or Mapping exported as though the artist had never disabled it.

Rejected rather than emulated: Blender's pass-through semantics vary by node
type and socket layout, so reconstructing them is exactly the kind of guess
this exporter refuses to make elsewhere.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

from Plugin.nodes import validate as validate_module  # noqa: E402


class _Socket:
    def __init__(self, name, default_value=None):
        self.name = name
        self.default_value = default_value
        self.links = []
        self.is_linked = False


class _Node:
    def __init__(self, node_type, name, *, mute=False):
        self.type = node_type
        self.name = name
        self.mute = mute
        self.inputs = {}
        self.outputs = {}
        self.is_active_output = node_type == 'OUTPUT_MATERIAL'


class _Link:
    def __init__(self, from_node, to_node, is_muted=False):
        self.from_node = from_node
        self.to_node = to_node
        self.is_muted = is_muted


def _material(nodes, links):
    tree = types.SimpleNamespace(nodes=nodes, links=links)
    return types.SimpleNamespace(name="M", node_tree=tree)


def _validate(material, monkeypatch, used_nodes):
    monkeypatch.setattr(validate_module, "_collect_used_nodes", lambda m: set(used_nodes))
    return validate_module.validate_material(material, strict=True)


def test_muted_node_is_rejected_in_strict_mode(monkeypatch):
    muted = _Node('MIX', "Mix", mute=True)
    material = _material([muted], [])

    result = _validate(material, monkeypatch, [muted])

    assert result["ok"] is False
    assert any("Muted node" in str(e) for e in result["errors"])


def test_unmuted_node_is_accepted(monkeypatch):
    live = _Node('MIX', "Mix")
    material = _material([live], [])

    result = _validate(material, monkeypatch, [live])

    assert not any("Muted node" in str(e) for e in result["errors"])


def test_muted_link_is_reported(monkeypatch):
    source = _Node('RGB', "RGB")
    target = _Node('BSDF_PRINCIPLED', "Principled")
    link = _Link(source, target, is_muted=True)
    material = _material([source, target], [link])

    result = _validate(material, monkeypatch, [source, target])

    assert any("muted link" in str(e).lower() for e in result["errors"])


def test_muted_link_into_an_unused_node_is_ignored(monkeypatch):
    source = _Node('RGB', "RGB")
    unused = _Node('MIX', "Unused")
    link = _Link(source, unused, is_muted=True)
    material = _material([source, unused], [link])

    result = _validate(material, monkeypatch, [source])

    assert not any("muted link" in str(e).lower() for e in result["errors"])


def test_muted_node_is_a_warning_when_not_strict(monkeypatch):
    """Non-strict callers still get told, without failing the material."""
    muted = _Node('MIX', "Mix", mute=True)
    material = _material([muted], [])
    monkeypatch.setattr(validate_module, "_collect_used_nodes", lambda m: {muted})

    result = validate_module.validate_material(material, strict=False)

    assert any("Muted node" in str(w) for w in result["warnings"])
