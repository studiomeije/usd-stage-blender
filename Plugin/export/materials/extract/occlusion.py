"""Which node a material's ambient occlusion comes from.

Blender's Principled BSDF has no ambient-occlusion input; RealityKit's PBR
Surface 2 has one, ``ambientOcclusion``, which darkens image-based diffuse
light only (measured on macOS 27.2.1: 0.5 halves it, 0 removes it, direct
light is unchanged). So the material names its AO source explicitly, in one
of two ways, and nothing is inferred from node names:

1. The material custom property ``usd_stage_ambient_occlusion`` names a node
   in the material's own tree, by name or label, optionally with an output:
   ``"ORM Separate:Red"``. Without an output the node's first output is read.
2. Otherwise, a node whose label is exactly ``Ambient Occlusion`` is the
   source, its first output read.

The property wins over a label. Two labelled nodes, or a property that names
no node or an output the node lacks, is refused, naming the fix. The source is read as data, like any
other scalar input: an image's Color becomes Blender's gray conversion, and a
Separate Color output one channel.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

AO_PROPERTY = "usd_stage_ambient_occlusion"
AO_LABEL = "Ambient Occlusion"


class AmbientOcclusionError(ValueError):
    """The material's AO declaration cannot be honoured."""


def _named_node(tree, name: str):
    nodes = list(getattr(tree, "nodes", ()) or ())
    for node in nodes:
        if getattr(node, "name", None) == name:
            return node
    labelled = [node for node in nodes if getattr(node, "label", None) == name]
    if len(labelled) == 1:
        return labelled[0]
    return None


def _output_socket(node, output_name: Optional[str], declared: str):
    outputs = list(getattr(node, "outputs", ()) or ())
    if output_name is None:
        enabled = [socket for socket in outputs if getattr(socket, "enabled", True)]
        if not enabled:
            raise AmbientOcclusionError(
                f"Ambient occlusion source '{declared}' has no output to read."
            )
        return enabled[0]
    for socket in outputs:
        if getattr(socket, "name", None) == output_name and getattr(socket, "enabled", True):
            return socket
    names = ", ".join(str(getattr(s, "name", "?")) for s in outputs)
    raise AmbientOcclusionError(
        f"Ambient occlusion source '{declared}' names output '{output_name}', which node "
        f"'{node.name}' does not have (it has: {names}). Set the material property "
        f"'{AO_PROPERTY}' to 'Node' or 'Node:Output'."
    )


def _from_property(material, tree) -> Optional[Tuple[Any, Any]]:
    try:
        declared = material.get(AO_PROPERTY)
    except Exception:
        declared = None
    if declared is None:
        return None
    if not isinstance(declared, str) or not declared.strip():
        raise AmbientOcclusionError(
            f"Material property '{AO_PROPERTY}' must be a node name, optionally "
            "with ':Output'; delete the property to export no ambient occlusion."
        )
    declared = declared.strip()
    node = _named_node(tree, declared)
    output_name = None
    if node is None and ":" in declared:
        node_name, output_name = declared.rsplit(":", 1)
        node = _named_node(tree, node_name)
    if node is None:
        raise AmbientOcclusionError(
            f"Material property '{AO_PROPERTY}' names '{declared}', but the material has "
            "no node with that name or label. Name the node that holds the ambient "
            "occlusion, or delete the property."
        )
    return node, _output_socket(node, output_name, declared)


def _from_label(tree) -> Optional[Tuple[Any, Any]]:
    labelled = [
        node for node in getattr(tree, "nodes", ()) or () if getattr(node, "label", None) == AO_LABEL
    ]
    if not labelled:
        return None
    if len(labelled) > 1:
        names = ", ".join(sorted(f"'{node.name}'" for node in labelled))
        raise AmbientOcclusionError(
            f"Nodes {names} are all labelled '{AO_LABEL}'; label one, or set the "
            f"material property '{AO_PROPERTY}' to the node to use."
        )
    return labelled[0], _output_socket(labelled[0], None, AO_LABEL)


def ambient_occlusion_source(material) -> Optional[Tuple[Any, Any]]:
    """The (node, output socket) the material declares as its AO, or None.

    Raises ``AmbientOcclusionError`` for a declaration that cannot be honoured.
    """
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    return _from_property(material, tree) or _from_label(tree)


def ambient_occlusion_refusal(material) -> Optional[str]:
    """Why the material's AO declaration cannot export, or None."""
    try:
        ambient_occlusion_source(material)
    except AmbientOcclusionError as error:
        return str(error)
    return None


def upstream_nodes(socket) -> set:
    """Every node an output socket's value depends on, the socket's own included."""
    seen = set()
    pending = [getattr(socket, "node", None)]
    while pending:
        node = pending.pop()
        if node is None or node in seen:
            continue
        seen.add(node)
        for input_socket in getattr(node, "inputs", ()) or ():
            for link in getattr(input_socket, "links", ()) or ():
                pending.append(getattr(link, "from_node", None))
    return seen


class _OutputAsInput:
    """An output socket presented as a linked input, for the socket resolver."""

    is_linked = True

    def __init__(self, socket):
        self.links = [_Link(socket)]
        self.name = "Ambient Occlusion"


class _Link:
    def __init__(self, socket):
        self.from_node = getattr(socket, "node", None)
        self.from_socket = socket


def as_linked_input(socket) -> _OutputAsInput:
    return _OutputAsInput(socket)
