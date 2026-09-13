"""Select/Remove Offending Nodes must not probe bpy collections with nodes.

``bpy_prop_collection.__contains__`` accepts only string keys (or tuples of
strings). ``node in node_tree.nodes`` with a node object therefore raises
TypeError in Blender, crashing the "Select Offending Nodes" operator the
moment validation returned any offender. The fake collection below enforces
the same contract so a regression fails these tests immediately.
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


class _Node:
    def __init__(self, name):
        self.name = name
        self.select = True


class _NodeCollection:
    """Mimics bpy_prop_collection: string-keyed membership only."""

    def __init__(self, nodes):
        self._nodes = list(nodes)
        self.active = None

    def __iter__(self):
        return iter(self._nodes)

    def __contains__(self, key):
        if not isinstance(key, (str, tuple)):
            raise TypeError(
                "bpy_prop_collection.__contains__: expected a string or a tuple of strings"
            )
        return any(node.name == key for node in self._nodes)

    def remove(self, node):
        self._nodes.remove(node)


def _material(nodes):
    tree = types.SimpleNamespace(nodes=_NodeCollection(nodes))
    return types.SimpleNamespace(name="M", node_tree=tree)


def _issues(*nodes, warning_nodes=()):
    return {
        "offending_nodes": [{"node": node, "node_name": node.name} for node in nodes],
        "warning_nodes": [
            {"node": node, "node_name": node.name} for node in warning_nodes
        ],
    }


def test_select_offending_nodes_selects_exactly_the_offenders():
    good = _Node("Good")
    bad = _Node("Bad")
    warned = _Node("Warned")
    material = _material([good, bad, warned])

    selected = validate_module.select_offending_nodes(
        material, _issues(bad, warning_nodes=[warned])
    )

    assert selected == 2
    assert bad.select is True
    assert warned.select is True
    assert good.select is False
    assert material.node_tree.nodes.active is warned


def test_select_offending_nodes_counts_duplicate_entries_once():
    bad = _Node("Bad")
    material = _material([bad])

    selected = validate_module.select_offending_nodes(material, _issues(bad, bad))

    assert selected == 1
    assert bad.select is True


def test_select_offending_nodes_skips_nodes_outside_the_tree():
    inside = _Node("Inside")
    outside = _Node("Outside")
    material = _material([inside])

    selected = validate_module.select_offending_nodes(
        material, _issues(inside, outside)
    )

    assert selected == 1
    assert inside.select is True
    assert material.node_tree.nodes.active is inside
