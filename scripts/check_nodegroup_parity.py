#!/usr/bin/env python3
"""Verify the shipped node-group library matches a fresh Blender 5.2 build.

This script must be run by Blender so it can inspect the semantic contents of
``.blend`` libraries.  It deliberately compares normalized graph signatures,
not the binary files: Blender may rewrite binary bookkeeping while preserving
the same node interfaces and evaluation graph.

Run from the repository root:

    blender --background --factory-startup --python-exit-code 1 \
      --python scripts/check_nodegroup_parity.py

Arguments for this script go after ``--``.  For example:

    blender --background --factory-startup --python-exit-code 1 \
      --python scripts/check_nodegroup_parity.py -- \
      --library Plugin/assets/nodegroups.blend
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


REQUIRED_BLENDER_SERIES = (5, 2)

# Properties that affect node evaluation.  Socket defaults and graph links are
# captured separately.  Keeping this list explicit avoids unstable editor-only
# RNA state (selection, dimensions, preview visibility, and screen position).
SEMANTIC_NODE_PROPERTIES = (
    "base",
    "blend_type",
    "clamp",
    "clamp_factor",
    "clamp_result",
    "convention",
    "data_type",
    "distribution",
    "factor_mode",
    "interpolation_type",
    "invert",
    "is_active_output",
    "mute",
    "normal_space",
    "operation",
    "projection",
    "space",
    "subsurface_method",
    "target",
    "use_alpha",
    "use_clamp",
    "uv_map",
)

INTERFACE_SOCKET_PROPERTIES = (
    "attribute_domain",
    "default_attribute_name",
    "default_input",
    "default_value",
    "description",
    "force_non_field",
    "hide_in_modifier",
    "hide_value",
    "in_out",
    "max_value",
    "min_value",
    "socket_type",
    "structure_type",
)

NODE_SOCKET_PROPERTIES = (
    "attribute_domain",
    "default_attribute",
    "default_value",
    "enabled",
    "hide_value",
    "type",
)

TEXTURE_MAPPING_PROPERTIES = (
    "mapping",
    "mapping_x",
    "mapping_y",
    "mapping_z",
    "max",
    "min",
    "rotation",
    "scale",
    "translation",
    "use_max",
    "use_min",
    "vector_type",
)

COLOR_MAPPING_PROPERTIES = (
    "blend_color",
    "blend_factor",
    "blend_type",
    "brightness",
    "contrast",
    "saturation",
    "use_color_ramp",
)

COLOR_RAMP_PROPERTIES = (
    "color_mode",
    "hue_interpolation",
    "interpolation",
)


class ParityError(RuntimeError):
    """Raised when a library cannot be inspected unambiguously."""


def _normalized_float(value: float) -> Any:
    """Return a JSON-safe, stable representation of a Blender float."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return 0.0
    # Blender node properties are single-precision in the cases represented by
    # this library.  Nine significant digits preserve a float32 value exactly.
    return float(format(value, ".9g"))


def normalize_value(value: Any) -> Any:
    """Convert Blender/Python values to deterministic JSON primitives."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return _normalized_float(value)
    if isinstance(value, Mapping):
        return {
            str(key): normalize_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, Sequence):
        return [normalize_value(item) for item in value]

    # mathutils vectors/colors and Blender RNA arrays are iterable, but are not
    # registered as collections.abc.Sequence.
    try:
        iterator = iter(value)
    except TypeError:
        pass
    else:
        return [normalize_value(item) for item in iterator]

    # ID pointers are not expected in this generated library.  If one appears,
    # record its stable type/name rather than a process-specific address.
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return {
            "id_type": type(value).__name__,
            "name": name,
        }
    raise TypeError(f"Unsupported semantic value type: {type(value).__name__}")


def semantic_sha256(value: Any) -> str:
    """Hash a normalized semantic value using canonical JSON."""
    payload = json.dumps(
        normalize_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def diff_values(expected: Any, actual: Any, *, limit: int = 20) -> List[str]:
    """Return a bounded list of human-readable semantic differences."""
    differences: List[str] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append(
                f"{path}: type {type(left).__name__} != {type(right).__name__}"
            )
            return
        if isinstance(left, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                differences.append(f"{path}.{key}: missing from shipped")
                if len(differences) >= limit:
                    return
            for key in sorted(right_keys - left_keys):
                differences.append(f"{path}.{key}: unexpected in shipped")
                if len(differences) >= limit:
                    return
            for key in sorted(left_keys & right_keys):
                visit(left[key], right[key], f"{path}.{key}")
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(f"{path}: length {len(left)} != {len(right)}")
                if len(differences) >= limit:
                    return
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(left_item, right_item, f"{path}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append(f"{path}: {left!r} != {right!r}")

    visit(normalize_value(expected), normalize_value(actual), "$")
    return differences


def _property_signature(value: Any, property_names: Iterable[str]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    for name in property_names:
        if not hasattr(value, name):
            continue
        try:
            properties[name] = normalize_value(getattr(value, name))
        except (AttributeError, ReferenceError, TypeError, ValueError) as exc:
            raise ParityError(
                f"Cannot normalize semantic property {type(value).__name__}.{name}: {exc}"
            ) from exc
    return properties


def _interface_signature(node_tree: Any) -> List[Dict[str, Any]]:
    interface = getattr(node_tree, "interface", None)
    if interface is None:
        raise ParityError(f"{node_tree.name}: missing Blender 5.2 node interface")

    signature: List[Dict[str, Any]] = []
    for item in interface.items_tree:
        item_type = str(getattr(item, "item_type", ""))
        entry: Dict[str, Any] = {
            "item_type": item_type,
            "name": str(getattr(item, "name", "")),
        }
        parent = getattr(item, "parent", None)
        if parent is not None:
            parent_path = []
            while parent is not None:
                parent_path.append(str(getattr(parent, "name", "")))
                parent = getattr(parent, "parent", None)
            entry["parent_path"] = list(reversed(parent_path))
        if item_type == "SOCKET":
            entry["properties"] = _property_signature(
                item, INTERFACE_SOCKET_PROPERTIES
            )
        elif item_type == "PANEL":
            entry["properties"] = _property_signature(
                item, ("description", "default_closed")
            )
        signature.append(entry)
    return signature


def _socket_signature(socket: Any) -> Dict[str, Any]:
    return {
        "socket_idname": str(getattr(socket, "bl_idname", "")),
        "properties": _property_signature(socket, NODE_SOCKET_PROPERTIES),
    }


def _color_ramp_signature(ramp: Any) -> Dict[str, Any]:
    return {
        "properties": _property_signature(ramp, COLOR_RAMP_PROPERTIES),
        "elements": [
            {
                "position": normalize_value(element.position),
                "color": normalize_value(element.color),
            }
            for element in ramp.elements
        ],
    }


def _node_signature(node: Any) -> Dict[str, Any]:
    signature = {
        "node_idname": str(node.bl_idname),
        "properties": _property_signature(node, SEMANTIC_NODE_PROPERTIES),
        "inputs": [_socket_signature(socket) for socket in node.inputs],
        "outputs": [_socket_signature(socket) for socket in node.outputs],
    }

    texture_mapping = getattr(node, "texture_mapping", None)
    if texture_mapping is not None:
        signature["texture_mapping"] = _property_signature(
            texture_mapping, TEXTURE_MAPPING_PROPERTIES
        )

    color_mapping = getattr(node, "color_mapping", None)
    if color_mapping is not None:
        signature["color_mapping"] = _property_signature(
            color_mapping, COLOR_MAPPING_PROPERTIES
        )
        if bool(getattr(color_mapping, "use_color_ramp", False)):
            ramp = getattr(color_mapping, "color_ramp", None)
            if ramp is None:
                raise ParityError(
                    f"{node.bl_idname}: color mapping enables a missing color ramp"
                )
            signature["color_mapping"]["color_ramp"] = _color_ramp_signature(ramp)

    return signature


def _socket_index(sockets: Any, target: Any) -> int:
    for index, socket in enumerate(sockets):
        if socket == target:
            return index
    raise ParityError(f"Cannot resolve socket {getattr(target, 'name', '<unknown>')}")


def group_signature(group: Any) -> Dict[str, Any]:
    """Return the semantic signature of one ShaderNodeTree."""
    custom_properties = {
        str(key): normalize_value(group[key])
        for key in sorted(group.keys(), key=str)
    }

    source_nodes = list(group.nodes)
    pointers = [node.as_pointer() for node in source_nodes]
    node_by_pointer = dict(zip(pointers, source_nodes))
    source_order = {pointer: index for index, pointer in enumerate(pointers)}
    base_signatures = {
        pointer: _node_signature(node)
        for pointer, node in node_by_pointer.items()
    }

    source_links = []
    for link in group.links:
        entry = {
            "from_pointer": link.from_node.as_pointer(),
            "from_socket": _socket_index(link.from_node.outputs, link.from_socket),
            "to_pointer": link.to_node.as_pointer(),
            "to_socket": _socket_index(link.to_node.inputs, link.to_socket),
            "is_muted": bool(getattr(link, "is_muted", False)),
        }
        if hasattr(link, "multi_input_sort_id"):
            entry["multi_input_sort_id"] = int(link.multi_input_sort_id)
        source_links.append(entry)

    # Refine a name-independent structural label for each node.  The labels are
    # used only to make traversal/order deterministic; process pointers never
    # enter the serialized signature.
    labels = {
        pointer: semantic_sha256(base_signatures[pointer])
        for pointer in pointers
    }
    for _iteration in range(len(source_nodes) + 1):
        refined = {}
        for pointer in pointers:
            incoming = sorted(
                (
                    link["to_socket"],
                    link.get("multi_input_sort_id", 0),
                    link["from_socket"],
                    labels[link["from_pointer"]],
                    link["is_muted"],
                )
                for link in source_links
                if link["to_pointer"] == pointer
            )
            outgoing = sorted(
                (
                    link["from_socket"],
                    link["to_socket"],
                    labels[link["to_pointer"]],
                    link["is_muted"],
                )
                for link in source_links
                if link["from_pointer"] == pointer
            )
            refined[pointer] = semantic_sha256(
                {
                    "base": base_signatures[pointer],
                    "incoming": incoming,
                    "outgoing": outgoing,
                }
            )
        labels = refined

    incoming_by_pointer = {pointer: [] for pointer in pointers}
    for link in source_links:
        incoming_by_pointer[link["to_pointer"]].append(link)
    for incoming in incoming_by_pointer.values():
        incoming.sort(
            key=lambda link: (
                link["to_socket"],
                link.get("multi_input_sort_id", 0),
                link["from_socket"],
                labels[link["from_pointer"]],
            )
        )

    canonical_pointers: List[int] = []
    visited = set()

    def visit(pointer: int) -> None:
        if pointer in visited:
            return
        visited.add(pointer)
        canonical_pointers.append(pointer)
        for link in incoming_by_pointer[pointer]:
            visit(link["from_pointer"])

    output_roots = [
        pointer
        for pointer in pointers
        if base_signatures[pointer]["node_idname"] == "NodeGroupOutput"
    ]
    for pointer in sorted(
        output_roots,
        key=lambda item: (
            labels[item],
            semantic_sha256(base_signatures[item]),
            source_order[item],
        ),
    ):
        visit(pointer)

    # Unreachable preview helpers are still part of the shipped library's
    # semantics.  Stable structural labels keep them independent of localized
    # Blender display names; source order is only a tie-breaker for truly
    # symmetric nodes.
    for pointer in sorted(
        (item for item in pointers if item not in visited),
        key=lambda item: (
            labels[item],
            semantic_sha256(base_signatures[item]),
            source_order[item],
        ),
    ):
        visit(pointer)

    canonical_ids = {
        pointer: index for index, pointer in enumerate(canonical_pointers)
    }
    nodes = [base_signatures[pointer] for pointer in canonical_pointers]
    links = []
    for source_link in source_links:
        link = {
            "from_node": canonical_ids[source_link["from_pointer"]],
            "from_socket": source_link["from_socket"],
            "to_node": canonical_ids[source_link["to_pointer"]],
            "to_socket": source_link["to_socket"],
            "is_muted": source_link["is_muted"],
        }
        if "multi_input_sort_id" in source_link:
            link["multi_input_sort_id"] = source_link["multi_input_sort_id"]
        links.append(link)
    links.sort(
        key=lambda item: (
            item["from_node"],
            item["from_socket"],
            item["to_node"],
            item["to_socket"],
            item.get("multi_input_sort_id", 0),
        )
    )
    return {
        "name": str(group.name),
        "tree_idname": str(group.bl_idname),
        "custom_properties": custom_properties,
        "interface": _interface_signature(group),
        "nodes": nodes,
        "links": links,
    }


def _clear_node_groups(bpy: Any) -> None:
    for group in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(group, do_unlink=True)


def _load_library_signatures(bpy: Any, path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.is_file():
        raise ParityError(f"Node-group library does not exist: {path}")

    _clear_node_groups(bpy)
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        source_names = list(data_from.node_groups)
        data_to.node_groups = source_names

    loaded_groups = [group for group in data_to.node_groups if group is not None]
    if len(loaded_groups) != len(source_names):
        raise ParityError(
            f"{path}: loaded {len(loaded_groups)} of {len(source_names)} node groups"
        )

    signatures: Dict[str, Dict[str, Any]] = {}
    for group in loaded_groups:
        node_id = group.get("rk_id")
        if not isinstance(node_id, str) or not node_id:
            raise ParityError(f"{path}: {group.name!r} is missing string rk_id metadata")
        if node_id in signatures:
            raise ParityError(f"{path}: duplicate rk_id {node_id!r}")
        signatures[node_id] = group_signature(group)
    return signatures


def _parse_script_args(argv: Sequence[str]) -> argparse.Namespace:
    script_argv = list(argv[argv.index("--") + 1 :]) if "--" in argv else []
    parser = argparse.ArgumentParser(
        description="Compare shipped and freshly generated node-group semantics."
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=Path("Plugin/assets/nodegroups.blend"),
        help="Shipped .blend library (default: %(default)s).",
    )
    parser.add_argument(
        "--fresh-output",
        type=Path,
        help="Optional path at which to retain the freshly generated library.",
    )
    parser.add_argument(
        "--diff-limit",
        type=int,
        default=12,
        help="Maximum detailed differences reported per changed group.",
    )
    return parser.parse_args(script_argv)


def _compare_signatures(
    fresh: Dict[str, Dict[str, Any]],
    shipped: Dict[str, Dict[str, Any]],
    *,
    diff_limit: int,
) -> Dict[str, Any]:
    fresh_ids = set(fresh)
    shipped_ids = set(shipped)
    changed = []
    for node_id in sorted(fresh_ids & shipped_ids):
        fresh_hash = semantic_sha256(fresh[node_id])
        shipped_hash = semantic_sha256(shipped[node_id])
        if fresh_hash == shipped_hash:
            continue
        changed.append(
            {
                "id": node_id,
                "fresh_sha256": fresh_hash,
                "shipped_sha256": shipped_hash,
                "differences": diff_values(
                    fresh[node_id], shipped[node_id], limit=diff_limit
                ),
            }
        )

    missing = sorted(fresh_ids - shipped_ids)
    unexpected = sorted(shipped_ids - fresh_ids)

    def count_items(signatures: Dict[str, Dict[str, Any]], key: str) -> int:
        return sum(len(group[key]) for group in signatures.values())

    return {
        "fresh_group_count": len(fresh),
        "shipped_group_count": len(shipped),
        "fresh_interface_item_count": count_items(fresh, "interface"),
        "shipped_interface_item_count": count_items(shipped, "interface"),
        "fresh_node_count": count_items(fresh, "nodes"),
        "shipped_node_count": count_items(shipped, "nodes"),
        "fresh_link_count": count_items(fresh, "links"),
        "shipped_link_count": count_items(shipped, "links"),
        "fresh_sha256": semantic_sha256(fresh),
        "shipped_sha256": semantic_sha256(shipped),
        "missing_in_shipped": missing,
        "unexpected_in_shipped": unexpected,
        "changed_group_count": len(changed),
        "changed_groups": changed,
        "ok": not missing and not unexpected and not changed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        import bpy
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised by Blender
        raise SystemExit(
            "check_nodegroup_parity.py must run inside Blender 5.2"
        ) from exc

    args = _parse_script_args(sys.argv if argv is None else argv)
    if tuple(bpy.app.version[:2]) != REQUIRED_BLENDER_SERIES:
        raise SystemExit(
            "Node-group parity requires Blender 5.2.x; "
            f"found {bpy.app.version_string}"
        )

    repo_root = Path(__file__).resolve().parents[1]
    library_path = args.library
    if not library_path.is_absolute():
        library_path = repo_root / library_path

    sys.path.insert(0, str(repo_root))
    from Plugin.nodes import metadata
    from scripts import nodegroups as rk_nodegroups

    catalog = metadata.get_node_catalog()
    catalog_ids = [str(entry["id"]) for entry in catalog]
    if len(catalog_ids) != len(set(catalog_ids)):
        raise ParityError("Current node catalog contains duplicate IDs")

    temporary_directory = None
    if args.fresh_output:
        fresh_path = args.fresh_output
        if not fresh_path.is_absolute():
            fresh_path = repo_root / fresh_path
    else:
        temporary_directory = tempfile.TemporaryDirectory(
            prefix="usdstage-nodegroups-"
        )
        fresh_path = Path(temporary_directory.name) / "nodegroups-fresh.blend"

    try:
        _clear_node_groups(bpy)
        rk_nodegroups.save_nodegroup_library(fresh_path)
        fresh = _load_library_signatures(bpy, fresh_path)
        shipped = _load_library_signatures(bpy, library_path)

        catalog_id_set = set(catalog_ids)
        if set(fresh) != catalog_id_set:
            raise ParityError(
                "Fresh library IDs do not match the catalog: "
                f"missing={sorted(catalog_id_set - set(fresh))}, "
                f"unexpected={sorted(set(fresh) - catalog_id_set)}"
            )
        stale_schema_ids = sorted(
            node_id
            for node_id, signature in fresh.items()
            if signature["custom_properties"].get("rk_version")
            != metadata.RK_NODE_VERSION
        )
        if stale_schema_ids:
            raise ParityError(
                "Fresh library does not carry the current node schema "
                f"{metadata.RK_NODE_VERSION}: {stale_schema_ids}"
            )

        result = _compare_signatures(
            fresh,
            shipped,
            diff_limit=max(1, args.diff_limit),
        )
        result.update(
            {
                "blender_version": bpy.app.version_string,
                "catalog_group_count": len(catalog_ids),
                "node_schema_version": metadata.RK_NODE_VERSION,
                "fresh_library": str(fresh_path),
                "shipped_library": str(library_path),
            }
        )
        print("USDSTAGE_NODEGROUP_PARITY=" + json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    finally:
        _clear_node_groups(bpy)
        if temporary_directory is not None:
            temporary_directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
