"""
RealityKit node catalog metadata.

Builds NodeGroup entries from the prebuilt MaterialX nodedef manifest.
"""

from typing import Dict, List, Optional

from ..manifest.materialx_nodes import load_manifest, node_version_key

#: Stored on every shipped node group as ``rk_version``; bump it when the
#: generated group interfaces change, then rebuild the library.
RK_NODE_VERSION = "3.0"

_CATALOG_CACHE: Optional[List[Dict[str, object]]] = None

PREVIEW_OVERRIDES = {
    "realitykit_pbr_surfaceshader": {
        "id": "rk_pbr",
        "label": "PBR Surface (RealityKit)",
        "group_name": "PBR Surface (RealityKit)",
        "preview": "pbr",
        "section": "Surfaces",
    },
    "realitykit_unlit_surfaceshader": {
        "id": "rk_unlit",
        "label": "Unlit Surface (RealityKit)",
        "group_name": "Unlit Surface (RealityKit)",
        "preview": "unlit",
        "section": "Surfaces",
    },
}

# Display labels aligned to ShaderGraph documentation names.
SHADERGRAPH_LABELS = {
    "RealityKitTexture2D": "Image 2D (RealityKit)",
    "RealityKitTexture2DGradient": "Image 2D Gradient (RealityKit)",
    "RealityKitTexture2DLOD": "Image 2D LOD (RealityKit)",
    "RealityKitTexture2DPixel": "Image 2D Pixel (RealityKit)",
    "RealityKitTexture2DPixelGradient": "Image 2D Gradient Pixel (RealityKit)",
    "RealityKitTexture2DPixelLOD": "Image 2D LOD Pixel (RealityKit)",
    "RealityKitTextureCube": "Cube Image (RealityKit)",
    "RealityKitTextureCubeGradient": "Cube Image Gradient (RealityKit)",
    "RealityKitTextureCubeLOD": "Cube Image LOD (RealityKit)",
    "RealityKitTextureRead": "Image 2D Read (RealityKit)",
    "absval": "Abs",
    "acos": "Acos",
    "add": "Add",
    "and": "And (RealityKit)",
    "asin": "Asin",
    "atan2": "Atan2",
    "burn": "Burn",
    "ceil": "Ceiling",
    "clamp": "Clamp",
    "combine3": "Combine 3",
    "constant": "Constant",
    "convert": "Convert",
    "cos": "Cos",
    "crossproduct": "Cross Product",
    "difference": "Difference",
    "divide": "Divide",
    "dodge": "Dodge",
    "dot": "Dot",
    "dotproduct": "Dot Product",
    "exp": "Exp",
    "floor": "Floor",
    "fractional": "Fractional (RealityKit)",
    "ifequal": "If Equal",
    "ifgreater": "If Greater",
    "ifgreatereq": "If Greater Or Equal",
    "image": "Image",
    "inside": "Inside",
    "ln": "Natural Log",
    "magnitude": "Magnitude",
    "max": "Max",
    "min": "Min",
    "minus": "Subtractive Mix",
    "mix": "Mix",
    "modulo": "Modulo",
    "multiply": "Multiply",
    "normal_map_decode": "Normal Map Decode",
    "normalize": "Normalize",
    "not": "Not (RealityKit)",
    "oneminus": "One Minus (RealityKit)",
    "or": "Or (RealityKit)",
    "outside": "Outside",
    "overlay": "Overlay",
    "plus": "Additive Mix",
    "power": "Power",
    "ramplr": "Ramp Horizontal",
    "ramptb": "Ramp Vertical",
    "realitykit_cameraposition": "Camera Position (RealityKit)",
    "realitykit_environment_radiance": "Environment Radiance (RealityKit)",
    "realitykit_occlusion": "Occlusion Surface (RealityKit)",
    "realitykit_pbr_surfaceshader": "PBR Surface (RealityKit)",
    "realitykit_reflect": "Reflect (RealityKit)",
    "realitykit_refract": "Refract (RealityKit)",
    "realitykit_shadowreceiver": "Shadow Receiving Occlusion Surface (RealityKit)",
    "realitykit_unlit_surfaceshader": "Unlit Surface (RealityKit)",
    "realitykit_viewdirection": "View Direction (RealityKit)",
    "remap": "Remap",
    "round": "Round",
    "safepower": "Safe Power",
    "screen": "Screen",
    "sign": "Sign",
    "sin": "Sin",
    "smoothstep": "Smooth Step",
    "splitlr": "Split Horizontal",
    "splittb": "Split Vertical",
    "sqrt": "Square Root",
    "step": "Step (RealityKit)",
    "subtract": "Subtract",
    "switch": "Switch",
    "swizzle": "Swizzle",
    "tan": "Tan",
    "tiledimage": "Tiled Image",
    "xor": "XOR (RealityKit)",
}

REALITYKIT_SUFFIX = " (RealityKit)"


def get_node_catalog() -> List[Dict[str, object]]:
    """Return the list of RealityKit node catalog entries."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None or not _CATALOG_CACHE:
        _CATALOG_CACHE = _build_catalog(include_half=True)
    return list(_CATALOG_CACHE)


def get_node_catalog_map() -> Dict[str, Dict[str, object]]:
    """Return a mapping of node id to catalog entry."""
    return {entry["id"]: entry for entry in get_node_catalog()}


def get_group_names() -> List[str]:
    """Return the list of NodeGroup names defined by the catalog."""
    return [entry["group_name"] for entry in get_node_catalog()]


def find_entry(node_id: str) -> Optional[Dict[str, object]]:
    """Look up a catalog entry by node id."""
    return get_node_catalog_map().get(node_id)


def is_catalog_group_name(name: str) -> bool:
    """Return True if a group name matches the catalog (dot prefix ignored)."""
    normalized = (name or "").lstrip(".")
    for group_name in get_group_names():
        if normalized == str(group_name).lstrip("."):
            return True
    return False


def _build_catalog(include_half: bool) -> List[Dict[str, object]]:
    try:
        manifest = load_manifest()
    except Exception:
        return []

    entries: List[Dict[str, object]] = []
    allowed_ids = set(SHADERGRAPH_LABELS.keys()) | set(PREVIEW_OVERRIDES.keys())

    node_defs_by_id: Dict[str, Dict[str, object]] = {}
    nodes = manifest.get("nodes", {}) or {}
    if not include_half:
        nodes = {
            name: node_def
            for name, node_def in nodes.items()
            if not bool(node_def.get("policy", {}).get("half_type"))
        }

    for nodedef_name in sorted(nodes.keys()):
        node_def = nodes[nodedef_name]
        node_id = str(node_def.get("node_id") or "")
        if not node_id:
            continue

        if node_id not in allowed_ids and not node_id.startswith("realitykit_") and not node_id.startswith("RealityKit"):
            continue

        existing = node_defs_by_id.get(node_id)
        if not existing:
            node_defs_by_id[node_id] = node_def
            continue

        existing_half = bool(existing.get("policy", {}).get("half_type"))
        candidate_half = bool(node_def.get("policy", {}).get("half_type"))
        if existing_half != candidate_half:
            if existing_half:
                node_defs_by_id[node_id] = node_def
            continue
        # The newest version of a node id is the one the export authors.
        if node_version_key(manifest, nodedef_name) > node_version_key(
            manifest, str(existing.get("nodedef_name") or "")
        ):
            node_defs_by_id[node_id] = node_def

    for node_id, node_def in node_defs_by_id.items():
        override = PREVIEW_OVERRIDES.get(node_id, {})
        entry_id = override.get("id", node_id)
        label = _with_realitykit_suffix(override.get("label") or _label_for_node(node_id))
        group_name = override.get("group_name", f".RK_{node_id}")
        section = override.get("section") or _section_for_node(node_def)

        entry = {
            "id": entry_id,
            "label": label,
            "group_name": group_name,
            "section": section,
            "export_id": node_id,
            "preview": override.get("preview"),
            "io": _io_from_manifest(node_def),
            "policy": node_def.get("policy", {}),
        }
        entries.append(entry)

    entries.sort(key=lambda item: (item.get("section") or "", item.get("label") or ""))
    return entries


def _label_for_node(node_id: str) -> str:
    label = SHADERGRAPH_LABELS.get(node_id)
    if label:
        return label
    base = node_id.replace("realitykit_", "").replace("_", " ").strip()
    return f"RK {base.title()}" if base else "RK Node"


def _with_realitykit_suffix(label: str) -> str:
    if not label:
        return f"Node{REALITYKIT_SUFFIX}"
    if label.endswith(REALITYKIT_SUFFIX):
        return label
    return f"{label}{REALITYKIT_SUFFIX}"


_SECTION_LABELS = {
    "pbr": "PBR",
    "procedural2d": "Procedural 2D",
    "procedural3d": "Procedural 3D",
    "texture2d": "Texture 2D",
    "texture3d": "Texture 3D",
    "realitykit": "RealityKit",
}


def _section_for_node(node_def: Dict[str, object]) -> str:
    nodegroup = str(node_def.get("nodegroup") or "Misc")
    return _SECTION_LABELS.get(nodegroup.lower(), nodegroup.replace("_", " ").title())


def _io_from_manifest(node_def: Dict[str, object]) -> Dict[str, List[Dict[str, object]]]:
    inputs: List[Dict[str, object]] = []
    outputs: List[Dict[str, object]] = []

    for input_def in node_def.get("inputs", []):
        mtlx_type = input_def.get("type")
        inputs.append(
            {
                "name": input_def.get("name"),
                "socket_type": _mtlx_type_to_socket(mtlx_type),
                "default": _default_value_from_manifest(mtlx_type, input_def.get("value")),
                "mtlx_type": mtlx_type,
            }
        )

    for output_def in node_def.get("outputs", []):
        mtlx_type = output_def.get("type")
        outputs.append(
            {
                "name": output_def.get("name"),
                "socket_type": _mtlx_type_to_socket(mtlx_type, is_output=True),
                "mtlx_type": mtlx_type,
            }
        )

    if not outputs:
        outputs.append({"name": "out", "socket_type": "NodeSocketFloat", "mtlx_type": None})

    return {"inputs": inputs, "outputs": outputs}


def _mtlx_type_to_socket(type_name: Optional[str], is_output: bool = False) -> str:
    if not type_name:
        return "NodeSocketFloat"

    type_name = type_name.lower()
    if type_name in {"surfaceshader", "volumeshader", "displacementshader", "material"}:
        return "NodeSocketShader"
    if type_name in {"color3", "color4"}:
        return "NodeSocketColor"
    if type_name in {"vector2", "vector3"}:
        return "NodeSocketVector"
    if type_name in {"vector4"}:
        return "NodeSocketColor"
    if type_name in {"float2", "float3"}:
        return "NodeSocketVector"
    if type_name in {"float4"}:
        return "NodeSocketColor"
    if type_name in {"float", "half"}:
        return "NodeSocketFloat"
    if type_name in {"half2"}:
        return "NodeSocketVector"
    if type_name in {"half3"}:
        return "NodeSocketColor"
    if type_name in {"half4"}:
        return "NodeSocketColor"
    if type_name in {"integer", "int"}:
        return "NodeSocketInt"
    if type_name in {"integer2", "int2"}:
        return "NodeSocketVector"
    if type_name in {"integer3", "int3"}:
        return "NodeSocketVector"
    if type_name in {"integer4", "int4"}:
        return "NodeSocketColor"
    if type_name in {"boolean", "bool"}:
        return "NodeSocketBool"
    if type_name in {"string", "filename"}:
        return "NodeSocketString"
    return "NodeSocketFloat"


def _default_value_from_manifest(type_name: Optional[str], value: Optional[str]):
    if value in (None, ""):
        return None
    if not type_name:
        return None

    type_name = type_name.lower()

    if type_name in {"boolean", "bool"}:
        return str(value).lower() in {"true", "1"}
    if type_name in {"integer", "int"}:
        try:
            return int(value)
        except ValueError:
            return None
    if type_name in {"float", "half"}:
        try:
            return float(value)
        except ValueError:
            return None

    if type_name in {"integer2", "integer3", "integer4", "int2", "int3", "int4"}:
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        try:
            values = [int(p) for p in parts]
        except ValueError:
            return None
        if type_name in {"integer2", "int2"}:
            return tuple(values[:2])
        if type_name in {"integer3", "int3"}:
            return tuple(values[:3])
        if type_name in {"integer4", "int4"}:
            return tuple(values[:4])
        return tuple(values)

    if type_name in {
        "color3",
        "color4",
        "vector2",
        "vector3",
        "vector4",
        "float2",
        "float3",
        "float4",
        "half2",
        "half3",
        "half4",
    }:
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None
        if type_name in {"color3", "vector3", "float3", "half3"}:
            return tuple(values[:3])
        if type_name in {"color4", "vector4", "float4", "half4"}:
            return tuple(values[:4])
        if type_name in {"vector2", "float2", "half2"}:
            return tuple(values[:2])
        return tuple(values)

    return value
