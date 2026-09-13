"""
Shared helpers for MaterialX USD authoring.
"""

import re
from typing import Any, Dict, Optional, List

from ...manifest.materialx_nodes import select_node_def_for_node


def _get_blender_data_name(material_prim) -> Optional[str]:
    """Get Blender's original material name from custom data, if present."""
    attr = material_prim.GetAttribute("userProperties:blender:data_name")
    if not attr:
        return None
    try:
        if not attr.HasAuthoredValueOpinion():
            return None
    except Exception:
        pass
    value = attr.Get()
    if isinstance(value, str) and value:
        return value
    return None


def _get_node_def(manifest: Dict[str, Any], node_id: str) -> Optional[Dict[str, Any]]:
    """Get node definition from manifest."""
    node_def = select_node_def_for_node(manifest, node_id)
    if not node_def and isinstance(node_id, str) and node_id.startswith("ND_"):
        node_def = manifest.get("nodes", {}).get(node_id)
    return node_def


def _get_nodedef_name(node_id: str, node_def: Optional[Dict[str, Any]]) -> str:
    """Resolve MaterialX nodedef name for a node.

    A node the manifest has no entry for is refused rather than given an
    invented ``ND_`` name, which would exist in no MaterialX library.
    """
    if node_def and node_def.get('nodedef_name'):
        return node_def['nodedef_name']
    raise ValueError(
        f"No MaterialX nodedef is known for node id '{node_id}'; refusing to "
        "author an unverifiable info:id."
    )


def _get_input_def(node_def: Optional[Dict[str, Any]], input_name: str) -> Optional[Dict[str, Any]]:
    """Get input definition for a nodedef."""
    if not node_def:
        return None
    for input_def in node_def.get('inputs', []):
        if input_def.get('name') == input_name:
            return input_def
    return None


def _get_output_def(node_def: Optional[Dict[str, Any]], output_name: str) -> Optional[Dict[str, Any]]:
    """Get output definition for a nodedef."""
    if not node_def:
        return None
    for output_def in node_def.get('outputs', []):
        if output_def.get('name') == output_name:
            return output_def
    return None


def _sanitize_name(value: str) -> str:
    """Sanitize a name for USD prims."""
    if not value:
        return "node"
    sanitized = re.sub(r'[^A-Za-z0-9_]', '_', value)
    if not sanitized[0].isalpha() and sanitized[0] != '_':
        sanitized = f"n_{sanitized}"
    return sanitized


def _assign_graph_node_names(stage, nodegraph_path: str, nodes: List[Dict[str, Any]]) -> Dict[str, str]:
    """Assign unique USD-safe names for graph nodes."""
    name_map: Dict[str, str] = {}
    used = set()
    for node in nodes:
        original = node.get("name") or "node"
        base = _sanitize_name(original)
        candidate = base
        suffix = 1
        while candidate in used or stage.GetPrimAtPath(f"{nodegraph_path}/{candidate}"):
            suffix += 1
            candidate = f"{base}_{suffix}"
        used.add(candidate)
        name_map[original] = candidate
    return name_map


def _collect_connected_inputs(connections: List[Dict[str, str]]) -> Dict[str, set]:
    """Collect input names that are linked in the graph."""
    connected: Dict[str, set] = {}
    for connection in connections:
        to_node = connection.get("to_node")
        to_input = connection.get("to_input")
        if not to_node or not to_input:
            continue
        connected.setdefault(to_node, set()).add(to_input)
    return connected


def _unique_shader_name(stage, nodegraph_path: str, base_name: str) -> str:
    """Return a unique shader prim name under the given nodegraph path."""
    candidate = base_name
    suffix = 1
    while stage.GetPrimAtPath(f"{nodegraph_path}/{candidate}"):
        suffix += 1
        candidate = f"{base_name}_{suffix}"
    return candidate


def _image_shader_name(stage, nodegraph_path: str, input_name: str) -> str:
    """Pick a stable image shader name aligned with RCP defaults."""
    base_name = "Image"
    if not stage.GetPrimAtPath(f"{nodegraph_path}/{base_name}"):
        return base_name
    index = 1
    while stage.GetPrimAtPath(f"{nodegraph_path}/{base_name}_{index}"):
        index += 1
    return f"{base_name}_{index}"


def _convert_shader_name(stage, nodegraph_path: str, input_name: str) -> str:
    """Pick a stable convert shader name aligned with RCP defaults."""
    base_name = "Convert"
    if stage.GetPrimAtPath(f"{nodegraph_path}/{base_name}"):
        sanitized = _sanitize_name(input_name)
        base_name = f"Convert_{sanitized}"
    return _unique_shader_name(stage, nodegraph_path, base_name)
