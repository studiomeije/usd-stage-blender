"""RealityKit-safe texture-mapping contracts.

RealityKit honors at most one 2D texture transform per material.  Keep the
equivalence rules in one import-safe module so Blender source validation,
MaterialX authoring, and composed-USD preflight agree about what constitutes a
distinct *effective* transform.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional, Tuple


_EPSILON = 1.0e-7
MappingContract = Tuple[
    str,
    Tuple[float, float],
    Tuple[float, float],
    float,
    Tuple[float, float],
    int,
]


def _canonical_number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Texture mapping '{field}' must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"Texture mapping '{field}' must be finite")
    if abs(number) <= _EPSILON:
        return 0.0
    return round(number, 9)


def _canonical_vector2(
    value: Any,
    default: Tuple[float, float],
    *,
    field: str,
) -> Tuple[float, float]:
    if value is None:
        value = default
    try:
        values = list(value)
    except TypeError as exc:
        raise ValueError(
            f"Texture mapping '{field}' must contain two numeric values"
        ) from exc
    if len(values) < 2:
        raise ValueError(
            f"Texture mapping '{field}' must contain two numeric values"
        )
    return (
        _canonical_number(values[0], field=f"{field}.x"),
        _canonical_number(values[1], field=f"{field}.y"),
    )


def effective_texture_mapping_contract(
    mapping: Optional[Mapping[str, Any]],
    texcoord: Optional[str] = None,
) -> Optional[MappingContract]:
    """Return a canonical non-identity mapping contract.

    Omitted values use the MaterialX ``place2d`` defaults.  Identity mappings
    return ``None`` because pivot and operation order have no effect when
    offset, scale, and rotation are neutral; no transform node should be
    authored for that case.  The resolved texture-coordinate set is part of
    the contract because one shared transform cannot consume two different UV
    sets.
    """

    if not mapping:
        return None

    offset = _canonical_vector2(
        mapping.get("offset"), (0.0, 0.0), field="offset"
    )
    scale = _canonical_vector2(
        mapping.get("scale"), (1.0, 1.0), field="scale"
    )
    rotate = _canonical_number(mapping.get("rotate", 0.0), field="rotate")
    pivot = _canonical_vector2(
        mapping.get("pivot"), (0.0, 0.0), field="pivot"
    )
    raw_order = mapping.get("operationorder", 0)
    if raw_order is None:
        raw_order = 0
    try:
        operation_order = int(raw_order)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Texture mapping 'operationorder' must be 0 (SRT) or 1 (TRS)"
        ) from exc
    if operation_order not in {0, 1}:
        raise ValueError(
            "Texture mapping 'operationorder' must be 0 (SRT) or 1 (TRS)"
        )

    if (
        offset == (0.0, 0.0)
        and scale == (1.0, 1.0)
        and rotate == 0.0
    ):
        return None

    resolved_texcoord = str(texcoord or "UV0").strip() or "UV0"
    return (
        resolved_texcoord,
        offset,
        scale,
        rotate,
        pivot,
        operation_order,
    )


def authored_texture_mapping_contract(
    mapping: Mapping[str, Any],
    texcoord: Optional[str] = None,
) -> MappingContract:
    """Return a contract for an explicitly authored transform, including identity.

    An explicit identity place2d/UsdTransform2d still consumes RealityKit's
    single transform slot, so it cannot be discarded like an identity Blender
    Mapping that the exporter has not authored yet.
    """

    contract = effective_texture_mapping_contract(mapping, texcoord)
    if contract is not None:
        return contract
    resolved_texcoord = str(texcoord or "UV0").strip() or "UV0"
    return (
        resolved_texcoord,
        (0.0, 0.0),
        (1.0, 1.0),
        0.0,
        (0.0, 0.0),
        0,
    )
def _graph_texture_mapping_uses(graph: Mapping[str, Any]):
    """Collect generated texture mappings and explicit place2d graph nodes."""

    uses: dict[MappingContract, list[str]] = defaultdict(list)
    nodes = list(graph.get("nodes", []) or [])
    nodes_by_name = {
        str(node.get("name")): node
        for node in nodes
        if node.get("name") is not None
    }
    inbound = {
        (str(connection.get("to_node")), str(connection.get("to_input"))): str(
            connection.get("from_node")
        )
        for connection in graph.get("connections", []) or []
        if connection.get("to_node") is not None
        and connection.get("to_input") is not None
        and connection.get("from_node") is not None
    }
    generated_count = 0
    explicit_count = 0

    for node in nodes:
        node_name = str(node.get("name") or node.get("node_id") or "<node>")
        for input_name, value in (node.get("inputs") or {}).items():
            if not isinstance(value, dict) or not value.get("mapping"):
                continue
            contract = effective_texture_mapping_contract(
                value.get("mapping"),
                value.get("texcoord"),
            )
            if contract is not None:
                uses[contract].append(f"{node_name}.{input_name}")
                generated_count += 1

        node_id = str(node.get("node_id") or "")
        if "place2d" not in node_id.lower():
            continue
        inputs = node.get("inputs") or {}
        source_name = inbound.get((str(node.get("name")), "texcoord"))
        texcoord = _graph_texcoord_semantic(source_name, nodes_by_name)
        mapping = {
            "offset": inputs.get("offset", (0.0, 0.0)),
            "scale": inputs.get("scale", (1.0, 1.0)),
            "rotate": math.radians(float(inputs.get("rotate", 0.0))),
            "pivot": inputs.get("pivot", (0.0, 0.0)),
            "operationorder": inputs.get("operationorder", 0),
        }
        contract = authored_texture_mapping_contract(mapping, texcoord)
        uses[contract].append(f"explicit:{node_name}")
        explicit_count += 1

    return uses, generated_count, explicit_count


def _graph_texcoord_semantic(
    source_name: Optional[str],
    nodes_by_name: Mapping[str, Mapping[str, Any]],
) -> str:
    if not source_name:
        return "<default>"
    source = nodes_by_name.get(source_name) or {}
    node_id = str(source.get("node_id") or "")
    lowered_id = node_id.lower()
    if "texcoord" in lowered_id:
        return "UV0"
    if "geomprop" in lowered_id:
        value = (source.get("inputs") or {}).get("geomprop")
        if value is not None and str(value):
            return str(value)
    return f"{node_id or '<unknown>'}@{source_name}"


def require_realitykit_mapping_contract(
    graph: Mapping[str, Any],
    material_name: str,
) -> Dict[MappingContract, Tuple[str, ...]]:
    """Reject graphs that would require distinct transforms in one material."""

    uses, generated_count, explicit_count = _graph_texture_mapping_uses(graph)
    contracts = {
        contract: tuple(sorted(input_names))
        for contract, input_names in uses.items()
    }
    if explicit_count and generated_count:
        raise ValueError(
            f"Material '{material_name}' combines an explicit MaterialX "
            "place2d node with a Blender texture Mapping transform. RealityKit "
            "honors only the first 2D texture transform per material; use one "
            "explicit transform or bake the texture mappings."
        )
    if explicit_count > 1:
        raise ValueError(
            f"Material '{material_name}' contains {explicit_count} explicit "
            "MaterialX place2d nodes. RealityKit honors only the first 2D "
            "texture transform per material; connect every consumer to one "
            "shared place2d node."
        )
    if len(contracts) <= 1:
        return contracts

    uses = "; ".join(
        ", ".join(input_names)
        for _contract, input_names in sorted(
            contracts.items(), key=lambda item: item[1]
        )
    )
    raise ValueError(
        f"Material '{material_name}' requires {len(contracts)} distinct non-default "
        "texture mappings, but RealityKit honors only the first 2D texture "
        f"transform per material ({uses}). Use identical Mapping values and one "
        "UV set for every transformed texture, or bake the transforms into the "
        "images."
    )


def mapping_contract_details(contract: MappingContract) -> Dict[str, Any]:
    """Return JSON-safe details for diagnostics and USD preflight findings."""

    texcoord, offset, scale, rotate, pivot, operation_order = contract
    return {
        "texcoord": texcoord,
        "offset": list(offset),
        "scale": list(scale),
        "rotate": rotate,
        "pivot": list(pivot),
        "operationorder": operation_order,
    }
