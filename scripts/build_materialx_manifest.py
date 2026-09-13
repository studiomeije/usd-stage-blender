#!/usr/bin/env python3
"""
Build the MaterialX nodedef manifest used by USD Stage for Blender.

This script reads Apple's `.mtlx` definition files and produces a single JSON
index consumed by the add-on at runtime:

  `Plugin/manifest/rk_nodes_manifest.json`

The add-on intentionally does NOT rebuild this manifest inside Blender.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional


# Nodedefs in Apple's public definition bundle that the RCP ShaderGraph editor
# cannot resolve. Measured against the installed RealityComposerPro.app 3.0
# (build 80.0.1.500.1): these 56 ids appear in the app only inside the
# CoreRealityIO / USD-loader parsing libraries (usdMtlx can read them), never
# in ShaderGraph.framework's shading libraries — the editor has no definition
# and the runtime cannot render them. All are pbrlib closure-domain nodes
# (BSDF/EDF/VDF, displacement, ND_surface/ND_volume, roughness helpers) plus
# the stdlib arrayappend family. The exporter never authors them; the flag
# exists so selection and preflight refuse them instead of green-lighting an
# id RCP would fail to bind. tests/unit/test_manifest_matches_editor_libraries.py
# recomputes this set from the installed app and fails on drift.
EDITOR_UNRESOLVABLE_NODEDEFS = {
    "ND_absorption_vdf",
    "ND_add_bsdf",
    "ND_add_edf",
    "ND_add_vdf",
    "ND_anisotropic_vdf",
    "ND_arrayappend_color3_color3array",
    "ND_arrayappend_color3array_color3array",
    "ND_arrayappend_color4_color4array",
    "ND_arrayappend_color4array_color4array",
    "ND_arrayappend_float_floatarray",
    "ND_arrayappend_floatarray_floatarray",
    "ND_arrayappend_integer_integerarray",
    "ND_arrayappend_integerarray_integerarray",
    "ND_arrayappend_string_stringarray",
    "ND_arrayappend_stringarray_stringarray",
    "ND_arrayappend_vector2_vector2array",
    "ND_arrayappend_vector2array_vector2array",
    "ND_arrayappend_vector3_vector3array",
    "ND_arrayappend_vector3array_vector3array",
    "ND_arrayappend_vector4_vector4array",
    "ND_arrayappend_vector4array_vector4array",
    "ND_artistic_ior",
    "ND_blackbody",
    "ND_burley_diffuse_bsdf",
    "ND_conductor_bsdf",
    "ND_conical_edf",
    "ND_dielectric_bsdf",
    "ND_displacement_float",
    "ND_displacement_vector3",
    "ND_generalized_schlick_bsdf",
    "ND_generalized_schlick_edf",
    "ND_glossiness_anisotropy",
    "ND_layer_bsdf",
    "ND_layer_vdf",
    "ND_light",
    "ND_measured_edf",
    "ND_mix_bsdf",
    "ND_mix_edf",
    "ND_mix_vdf",
    "ND_multiply_bsdfC",
    "ND_multiply_bsdfF",
    "ND_multiply_edfC",
    "ND_multiply_edfF",
    "ND_multiply_vdfC",
    "ND_multiply_vdfF",
    "ND_oren_nayar_diffuse_bsdf",
    "ND_roughness_anisotropy",
    "ND_roughness_dual",
    "ND_sheen_bsdf",
    "ND_subsurface_bsdf",
    "ND_surface",
    "ND_thin_film_bsdf",
    "ND_thin_surface",
    "ND_translucent_bsdf",
    "ND_uniform_edf",
    "ND_volume",
}


# The generated manifest is a checked-in interoperability contract, not a
# snapshot of whichever SDK happens to be installed on a contributor's Mac.
MATERIALX_PROFILE = "1.39"
MATERIALX_REFERENCE_RELEASE = "1.39.4"
REALITY_COMPOSER_PRO_VERSION = "3.0"
# Verified 2026-07-30 against the installed app: every nodedef in the
# References bundle is signature-identical (modulo whitespace) in build
# 80.0.1.500.1's shipped libraries, and no manifest nodedef was removed.
REALITY_COMPOSER_PRO_BUILD = "80.0.1.500.1"
APPLE_PLATFORM_GENERATION = "27.0"

# ---------------------------------------------------------------------------
# Runtime overlay
#
# The installed OS/RCP ShaderGraph.framework ships a MaterialX library tree
# (a 1.38 + 1.39.4 hybrid) that is a strict superset of Apple's public
# References bundle. An optional overlay stage measures that tree and
# (a) adds inputs the shipped signatures gained (e.g. the noise `style`
# input), and (b) records nodedefs the runtime can resolve that the
# References bundle omits. Only interface facts (names, types, defaults)
# are recorded — no .mtlx file is copied into the repository.
#
# Overlay entries carry policy.runtime_overlay = true and a
# "measured:ShaderGraph.framework/<relpath>" source_file marker.
# ---------------------------------------------------------------------------

# Prefer the OS copy; the RCP.app copy is byte-identical (verified 2026-07-30).
RUNTIME_LIBRARY_ROOTS = (
    Path(
        "/System/Library/SubFrameworks/ShaderGraph.framework/"
        "Versions/A/Resources/MaterialX"
    ),
    Path(
        "/Applications/RealityComposerPro.app/Contents/SystemFrameworks/"
        "ShaderGraph.framework/Versions/A/Resources/MaterialX"
    ),
)

RUNTIME_OVERLAY_SOURCE = "ShaderGraph.framework/Versions/A/Resources/MaterialX"
# Measured from RealityComposerPro 3.0 build 80.0.6.0.5. The OS copy of
# ShaderGraph.framework (CFBundleVersion 159.1.1, macOS 27.0 26A5416b) and the
# copy inside the app (159.9.1) ship byte-identical MaterialX library trees;
# the generator overlays the OS copy when both are present.
RUNTIME_OVERLAY_RCP_BUILD = "80.0.6.0.5"
RUNTIME_OVERLAY_SHADERGRAPH_VERSION = "159.1.1"

# Directory ranking for choosing the canonical declaration when the runtime
# tree declares one nodedef in several files. Self-contained declarations
# (no `inherit`) always beat inherit-based stubs; then newer trees win.
_RUNTIME_DIR_RANKS = (
    ("MaterialX-1.39.4/", 0),
    ("Apple/apple_nodedefs_overrides/", 1),
    ("Apple/", 2),
    ("MaterialX-1.38/", 3),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build rk_nodes_manifest.json from MaterialX .mtlx files.")
    parser.add_argument(
        "--source",
        default="References/MaterialX-definitions",
        help="Folder containing Apple MaterialX .mtlx definition files.",
    )
    parser.add_argument(
        "--output",
        default="Plugin/manifest/rk_nodes_manifest.json",
        help="Output JSON path (inside the add-on).",
    )
    parser.add_argument(
        "--include-half",
        action="store_true",
        help="Include .mtlx files with 'half' in their filename (RealityKit half libraries).",
    )
    parser.add_argument(
        "--runtime-library",
        default=None,
        help=(
            "Installed ShaderGraph MaterialX library tree to overlay "
            "(default: auto-detect the OS copy, then the RCP.app copy)."
        ),
    )
    parser.add_argument(
        "--no-runtime-overlay",
        action="store_true",
        help="Skip the runtime overlay stage even if a ShaderGraph library tree is installed.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    source_dir = Path(args.source)
    if not source_dir.is_absolute():
        source_dir = repo_root / source_dir
    source_dir = source_dir.resolve()

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = repo_root / output_path
    output_path = output_path.resolve()

    runtime_library: Optional[Path] = None
    if not args.no_runtime_overlay:
        if args.runtime_library:
            runtime_library = Path(args.runtime_library).resolve()
            if not runtime_library.is_dir():
                raise SystemExit(f"Runtime library tree not found: {runtime_library}")
        else:
            runtime_library = _find_runtime_library()

    carry_forward_from: Optional[Path] = None
    if runtime_library is None and not args.no_runtime_overlay and output_path.is_file():
        carry_forward_from = output_path

    manifest = build_manifest(
        repo_root,
        source_dir,
        include_half=bool(args.include_half),
        runtime_library=runtime_library,
        carry_forward_from=carry_forward_from,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2))

    overlay = manifest.get("metadata", {}).get("runtime_overlay")
    if overlay and runtime_library is None:
        print(
            f"Runtime overlay: carried forward from the existing manifest "
            f"({overlay.get('entries_added')} entries, build "
            f"{overlay.get('reality_composer_pro_build')}); nothing was measured on this machine"
        )
    elif overlay:
        print(
            f"Runtime overlay: {overlay['entries_added']} nodedefs added, "
            f"{len(overlay['updated_nodedefs'])} updated from {overlay['source']}"
        )
    else:
        print("Runtime overlay: skipped (no ShaderGraph library tree)")
    print(f"Wrote {len(manifest.get('nodes', {}))} nodedefs -> {output_path}")
    return 0


def build_manifest(
    repo_root: Path,
    source_dir: Path,
    include_half: bool,
    runtime_library: Optional[Path] = None,
    carry_forward_from: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the manifest from the vendored definitions.

    ``runtime_library`` overlays the installed ShaderGraph libraries (a Mac
    with Reality Composer Pro). ``carry_forward_from`` is the alternative for a
    machine without them: the overlay recorded in that existing manifest is
    replayed verbatim, so a rebuild reproduces the checked-in bytes exactly
    when, and only when, the vendored part still matches. The two are
    mutually exclusive; the overlay always wins when both are given.
    """
    if not source_dir.exists():
        raise SystemExit(f"MaterialX source directory not found: {source_dir}")

    mtlx_files = sorted([p for p in source_dir.rglob("*.mtlx") if p.is_file()])
    if not include_half:
        # PBR Surface 2 has real half inputs, so its scalar conversion contract
        # is mandatory even in the compact/default manifest. Implementation
        # nodegraphs remain optional; RCP3 supplies them at runtime.
        required_half_defs = {"realitykit_half_defs.mtlx"}
        mtlx_files = [
            p
            for p in mtlx_files
            if "half" not in p.name.lower() or p.name in required_half_defs
        ]

    manifest: Dict[str, Any] = {
        "nodes": {},
        "index": {
            "by_node": {},
            "by_node_signature": {},
            "by_node_io": {},
            "by_node_output": {},
        },
        "metadata": {
            "version": "2.0.0",
            "profile": "realitykit-os27",
            "materialx_version": MATERIALX_PROFILE,
            "materialx_reference_release": MATERIALX_REFERENCE_RELEASE,
            "reality_composer_pro": {
                "version": REALITY_COMPOSER_PRO_VERSION,
                "build": REALITY_COMPOSER_PRO_BUILD,
            },
            "apple_platform_generation": APPLE_PLATFORM_GENERATION,
            "source_provenance": (
                "Repo-vendored, licensed nodedefs verified against Reality Composer Pro 3 "
                "and MaterialX 1.39.4; no machine-local SDK paths are embedded."
            ),
            "source_files": [],
            "source_sha256": {},
            "document_versions": {},
        },
    }

    for mtlx_file in mtlx_files:
        _parse_mtlx_file(repo_root, manifest, mtlx_file)
        source_path = _format_source_path(repo_root, mtlx_file)
        manifest["metadata"]["source_files"].append(source_path)
        manifest["metadata"]["source_sha256"][source_path] = hashlib.sha256(
            mtlx_file.read_bytes()
        ).hexdigest()
        document_version = _materialx_document_version(mtlx_file)
        if document_version:
            manifest["metadata"]["document_versions"][source_path] = document_version

    if runtime_library is not None:
        _apply_runtime_overlay(manifest, runtime_library)
    elif carry_forward_from is not None:
        _carry_forward_overlay(manifest, carry_forward_from)

    # Index at the end: the overlay may append inputs to existing entries,
    # which changes their signatures.
    for node_info in manifest["nodes"].values():
        _index_node(manifest, node_info)

    return manifest


def _parse_mtlx_file(repo_root: Path, manifest: Dict[str, Any], filepath: Path) -> None:
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        ns_uri = _get_namespace_uri(root.tag)
        ns = {"mx": ns_uri} if ns_uri else None

        nodedefs = root.findall(".//mx:nodedef", ns) if ns else root.findall(".//nodedef")
        for nodedef in nodedefs:
            node_info = _extract_nodedef_info(repo_root, nodedef, ns, filepath)
            if not node_info:
                continue
            nodedef_name = node_info["nodedef_name"]
            existing = manifest["nodes"].get(nodedef_name)
            if existing is not None and existing != node_info:
                raise RuntimeError(
                    f"Conflicting MaterialX nodedef '{nodedef_name}' in "
                    f"{existing.get('source_file')} and {_format_source_path(repo_root, filepath)}"
                )
            manifest["nodes"][nodedef_name] = node_info
    except ET.ParseError as exc:
        print(f"Warning: Failed to parse {filepath}: {exc}")


def _parse_io_from_element(nodedef, ns) -> tuple:
    """Return (inputs, outputs) lists parsed from a <nodedef> element."""
    inputs: List[Dict[str, Any]] = []
    input_elems = nodedef.findall(".//mx:input", ns) if ns else nodedef.findall(".//input")
    for input_elem in input_elems:
        input_info: Dict[str, Any] = {
            "name": input_elem.get("name", ""),
            "type": input_elem.get("type", ""),
            "value": input_elem.get("value", ""),
            "uniform": (input_elem.get("uniform", "false") or "").lower() == "true",
        }
        enum = input_elem.get("enum", "")
        if enum:
            input_info["enum"] = enum.split(",")
        inputs.append(input_info)

    outputs: List[Dict[str, Any]] = []
    output_elems = nodedef.findall(".//mx:output", ns) if ns else nodedef.findall(".//output")
    for output_elem in output_elems:
        outputs.append(
            {
                "name": output_elem.get("name", ""),
                "type": output_elem.get("type", ""),
            }
        )
    return inputs, outputs


def _nodedef_info_from_element(
    nodedef,
    ns,
    source_file: str,
    is_half: bool,
    runtime_overlay: bool,
) -> Optional[Dict[str, Any]]:
    nodedef_name = nodedef.get("name", "") or ""
    if not nodedef_name:
        return None

    node_name = (nodedef.get("node", "") or "").strip()
    nodegroup = (nodedef.get("nodegroup", "") or "").strip()
    node_version = (nodedef.get("version", "") or "").strip()
    target = (nodedef.get("target", "") or "").strip()
    availability = (nodedef.get("available", "") or "").strip()
    apple_availability = (nodedef.get("apple_availability", "") or "").strip()
    is_default_version = (nodedef.get("isdefaultversion", "false") or "").lower() == "true"

    inputs, outputs = _parse_io_from_element(nodedef, ns)

    signature = _signature_from_io(inputs, outputs)
    node_id = node_name or nodedef_name.replace("ND_", "")
    node_key = node_name or node_id

    result = {
        "nodedef_name": nodedef_name,
        "node_id": node_id,
        "node_name": node_key,
        "nodegroup": nodegroup,
        "inputs": inputs,
        "outputs": outputs,
        "signature": signature,
        "policy": {
            "half_type": is_half,
            "editor_unresolvable": (
                not runtime_overlay and nodedef_name in EDITOR_UNRESOLVABLE_NODEDEFS
            ),
            "runtime_overlay": runtime_overlay,
        },
        "source_file": source_file,
    }
    if node_version:
        result["node_version"] = node_version
    if target:
        result["target"] = target
    if availability:
        result["availability"] = availability
    if apple_availability:
        result["apple_availability"] = apple_availability
    if is_default_version:
        result["is_default_version"] = True
    return result


def _extract_nodedef_info(
    repo_root: Path,
    nodedef,
    ns,
    filepath: Path,
) -> Optional[Dict[str, Any]]:
    return _nodedef_info_from_element(
        nodedef,
        ns,
        # Keep stable paths (avoid machine-specific absolute paths).
        source_file=_format_source_path(repo_root, filepath),
        is_half="half" in filepath.name.lower(),
        runtime_overlay=False,
    )


# ---------------------------------------------------------------------------
# Runtime overlay stage
# ---------------------------------------------------------------------------


def _find_runtime_library() -> Optional[Path]:
    """Return the installed ShaderGraph MaterialX tree, preferring the OS copy."""
    for root in RUNTIME_LIBRARY_ROOTS:
        if root.is_dir():
            return root
    return None


def _runtime_file_excluded(relpath: str) -> bool:
    """Files whose declarations are never overlay material."""
    lower = relpath.lower()
    if "private" in lower or "internal" in lower:
        return True
    if "_old" in Path(relpath).name.lower():
        return True
    if "apple_metal" in Path(relpath).name.lower():
        return True
    return False


def _runtime_name_excluded(nodedef_name: str) -> bool:
    return (
        nodedef_name.startswith("_")
        or "ND_Internal" in nodedef_name
        or "ND_MTL_" in nodedef_name
        or "apple_metal" in nodedef_name
    )


def _runtime_nodegroup_excluded(decl: Dict[str, Any]) -> bool:
    """Implementation-only nodegroups (realitykit_private, realitykit_internal)
    are not authorable interface — the RCP editor never surfaces them."""
    nodegroup = (decl["info"].get("nodegroup") or "").lower()
    return "private" in nodegroup or "internal" in nodegroup


def _decl_has_half_types(decl: Dict[str, Any]) -> bool:
    types = [item.get("type") for item in decl["info"].get("inputs", [])]
    types += [item.get("type") for item in decl["info"].get("outputs", [])]
    return any("half" in _normalize_type(t) for t in types)


def _decl_rank(decl: Dict[str, Any]) -> tuple:
    """Deterministic canonical-declaration ordering (lower wins).

    Self-contained declarations beat `inherit` stubs (the stubs list only the
    child's own inputs; this script does not resolve inheritance), then the
    1.39.4 tree beats Apple's override/extension files, which beat the 1.38
    tree, with the relative path as the final tiebreak.
    """
    dir_rank = 4
    for prefix, rank in _RUNTIME_DIR_RANKS:
        if decl["relpath"].startswith(prefix):
            dir_rank = rank
            break
    return (1 if decl["inherit"] else 0, dir_rank, decl["relpath"])


def _parse_runtime_nodedefs(runtime_root: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Parse every nodedef declaration in the installed library tree.

    Returns name -> list of declaration records sorted by relative path.
    Only interface facts are retained; nothing is copied into the repo.
    """
    decls: Dict[str, List[Dict[str, Any]]] = {}
    for mtlx_file in sorted(runtime_root.rglob("*.mtlx")):
        relpath = mtlx_file.relative_to(runtime_root).as_posix()
        try:
            root = ET.parse(mtlx_file).getroot()
        except ET.ParseError as exc:
            print(f"Warning: Failed to parse runtime library {mtlx_file}: {exc}")
            continue
        ns_uri = _get_namespace_uri(root.tag)
        ns = {"mx": ns_uri} if ns_uri else None
        nodedefs = root.findall(".//mx:nodedef", ns) if ns else root.findall(".//nodedef")
        for nodedef in nodedefs:
            info = _nodedef_info_from_element(
                nodedef,
                ns,
                source_file=f"measured:ShaderGraph.framework/{relpath}",
                is_half=False,
                runtime_overlay=True,
            )
            if not info:
                continue
            decls.setdefault(info["nodedef_name"], []).append(
                {
                    "info": info,
                    "relpath": relpath,
                    "inherit": bool((nodedef.get("inherit", "") or "").strip()),
                    "deprecated": "deprecated"
                    in (nodedef.get("apple_availability", "") or ""),
                }
            )
    return decls


def _renamed_shipped_inputs(
    entry: Dict[str, Any],
    decl: Dict[str, Any],
) -> List[tuple] | None:
    """Detect a pure positional rename of an entry's inputs, or return None.

    The References bundle is vanilla upstream MaterialX; the library
    ShaderGraph actually loads is Apple's, and the two disagree on at least
    one interface. ``atan2`` is the measured case: upstream 1.38 says
    ``in1``/``in2`` while every library RealityKit binds says ``iny``/``inx``.

    That difference is not cosmetic. Measured with ``realitytool compile``, an
    input the bound nodedef does not declare makes the compiler discard the
    material's whole shader graph and substitute default PBR, silently. So
    where the shipped declaration has the same inputs in the same order with
    the same types under different names, the shipped names win - they are the
    ones RealityKit will look for.

    Returns the ``(old, new)`` pairs, or None when this is not a clean rename.
    """
    manifest_inputs = entry.get("inputs", [])
    shipped_inputs = decl["info"].get("inputs", [])
    if len(manifest_inputs) != len(shipped_inputs):
        return None
    renames = []
    for mine, theirs in zip(manifest_inputs, shipped_inputs):
        if _normalize_type(mine["type"]) != _normalize_type(theirs["type"]):
            return None
        if mine["name"] != theirs["name"]:
            renames.append((mine["name"], theirs["name"]))
    return renames or None


def _merge_shipped_inputs(
    entry: Dict[str, Any],
    decl: Dict[str, Any],
    conflicts: List[tuple],
    renamed: Dict[str, List[tuple]] | None = None,
) -> List[str]:
    """Add inputs the shipped declaration gained; never remove or retype.

    The declaration must be a strict superset of the manifest entry: every
    manifest input/output present with the same type. A type disagreement on
    a shared name is recorded as a conflict and the References version is
    left untouched.

    One exception: when the shipped declaration is not a superset only because
    it *renamed* inputs, the shipped names are adopted. See
    ``_renamed_shipped_inputs`` for why silently keeping ours is unsafe.
    """
    manifest_inputs = {item["name"]: item for item in entry.get("inputs", [])}
    manifest_outputs = {item["name"]: item for item in entry.get("outputs", [])}
    shipped_inputs = {item["name"]: item for item in decl["info"].get("inputs", [])}
    shipped_outputs = {item["name"]: item for item in decl["info"].get("outputs", [])}

    typed_conflicts = [
        (name, manifest_inputs[name]["type"], shipped_inputs[name]["type"])
        for name in manifest_inputs
        if name in shipped_inputs
        and _normalize_type(manifest_inputs[name]["type"])
        != _normalize_type(shipped_inputs[name]["type"])
    ] + [
        (name, manifest_outputs[name]["type"], shipped_outputs[name]["type"])
        for name in manifest_outputs
        if name in shipped_outputs
        and _normalize_type(manifest_outputs[name]["type"])
        != _normalize_type(shipped_outputs[name]["type"])
    ]
    if typed_conflicts:
        conflicts.append((entry["nodedef_name"], decl["relpath"], typed_conflicts))
        return []

    if any(name not in shipped_inputs for name in manifest_inputs):
        # Not a superset. A pure positional rename is the one recoverable
        # shape, and adopting the shipped names is mandatory rather than
        # optional - authoring a name the bound nodedef lacks silently costs
        # the whole material. Anything else stays ambiguous and is skipped.
        renames = _renamed_shipped_inputs(entry, decl)
        if renames is None:
            return []
        rename_map = dict(renames)
        for item in entry["inputs"]:
            if item["name"] in rename_map:
                item["name"] = rename_map[item["name"]]
        entry["signature"] = _signature_from_io(entry["inputs"], entry["outputs"])
        entry["source_file"] = decl["relpath"]
        if renamed is not None:
            renamed[entry["nodedef_name"]] = renames
        return []
    if any(name not in shipped_outputs for name in manifest_outputs):
        return []

    added: List[str] = []
    for item in decl["info"].get("inputs", []):
        if item["name"] in manifest_inputs:
            continue
        merged = dict(item)
        # Mark measured additions so selection keeps keying off the
        # References-era interface (see materialx_nodes._declared_types).
        merged["runtime_overlay"] = True
        entry["inputs"].append(merged)
        manifest_inputs[item["name"]] = merged
        added.append(item["name"])
    return added


def _carry_forward_overlay(manifest: Dict[str, Any], existing_path: Path) -> None:
    """Replay the overlay recorded in an existing manifest.

    The overlay touches entries in exactly three ways, and records each:
    entries it added carry ``policy.runtime_overlay``; entries whose shipped
    signature gained inputs are listed in ``updated_nodedefs``; entries whose
    inputs were renamed to the shipped spelling are listed in
    ``renamed_inputs``. Copying those entries and the overlay metadata from
    the existing manifest, in the existing order, reproduces its bytes when
    the vendored part is unchanged. Nothing is measured here: a machine that
    can measure runs the real overlay instead.
    """
    import copy

    existing = json.loads(existing_path.read_text())
    record = existing.get("metadata", {}).get("runtime_overlay")
    if not record:
        return
    touched = set(record.get("updated_nodedefs", {})) | set(record.get("renamed_inputs", {}))
    for name, entry in existing.get("nodes", {}).items():
        if entry.get("policy", {}).get("runtime_overlay") or name in touched:
            manifest["nodes"][name] = copy.deepcopy(entry)
    manifest["metadata"]["runtime_overlay"] = copy.deepcopy(record)


def _apply_runtime_overlay(manifest: Dict[str, Any], runtime_root: Path) -> None:
    decls = _parse_runtime_nodedefs(runtime_root)
    conflicts: List[tuple] = []

    # (a) Update existing entries whose shipped signature gained inputs.
    updated: Dict[str, List[str]] = {}
    renamed: Dict[str, List[tuple]] = {}
    for nodedef_name, entry in manifest["nodes"].items():
        for decl in decls.get(nodedef_name, []):
            if _runtime_file_excluded(decl["relpath"]) or decl["deprecated"]:
                continue
            added = _merge_shipped_inputs(entry, decl, conflicts, renamed)
            if added:
                updated.setdefault(nodedef_name, [])
                updated[nodedef_name].extend(
                    name for name in added if name not in updated[nodedef_name]
                )
        if nodedef_name in updated:
            entry["signature"] = _signature_from_io(entry["inputs"], entry["outputs"])

    for nodedef_name, relpath, details in conflicts:
        print(
            f"Warning: type conflict for {nodedef_name} in {relpath}: {details}; "
            f"keeping the References signature"
        )
    for nodedef_name, pairs in sorted(renamed.items()):
        spelled = ", ".join(f"{old} -> {new}" for old, new in pairs)
        print(
            f"Adopted shipped input names for {nodedef_name}: {spelled} "
            f"(the References bundle disagrees with the library RealityKit binds)"
        )

    # (b) Add runtime-resolvable nodedefs the References bundle omits.
    added_names: List[str] = []
    for nodedef_name in sorted(decls):
        if nodedef_name in manifest["nodes"]:
            continue
        if _runtime_name_excluded(nodedef_name):
            continue
        kept = [
            decl
            for decl in decls[nodedef_name]
            if not _runtime_file_excluded(decl["relpath"])
            and not decl["deprecated"]
            and not _decl_has_half_types(decl)
            and not _runtime_nodegroup_excluded(decl)
        ]
        if not kept:
            continue
        canonical = min(kept, key=_decl_rank)
        manifest["nodes"][nodedef_name] = canonical["info"]
        added_names.append(nodedef_name)

    manifest["metadata"]["runtime_overlay"] = {
        "source": RUNTIME_OVERLAY_SOURCE,
        "reality_composer_pro_build": RUNTIME_OVERLAY_RCP_BUILD,
        "shadergraph_version": RUNTIME_OVERLAY_SHADERGRAPH_VERSION,
        "entries_added": len(added_names),
        "updated_nodedefs": {name: updated[name] for name in sorted(updated)},
        "renamed_inputs": {
            name: [list(pair) for pair in pairs] for name, pairs in sorted(renamed.items())
        },
        "provenance": (
            "Interface facts (names, types, defaults) measured from the installed "
            "ShaderGraph.framework MaterialX libraries; no .mtlx content is vendored."
        ),
    }


def _index_node(manifest: Dict[str, Any], node_info: Dict[str, Any]) -> None:
    """Index a nodedef by node name and signature for lookup."""
    index = manifest.get("index", {})
    by_node = index.get("by_node", {})
    by_node_signature = index.get("by_node_signature", {})
    by_node_io = index.get("by_node_io", {})
    by_node_output = index.get("by_node_output", {})

    node_name = node_info.get("node_name") or node_info.get("node_id")
    if not node_name:
        return

    nodedef_name = node_info.get("nodedef_name")
    if not nodedef_name:
        return

    by_node.setdefault(node_name, [])
    if nodedef_name not in by_node[node_name]:
        by_node[node_name].append(nodedef_name)

    signature = node_info.get("signature")
    if signature:
        by_node_signature.setdefault(node_name, {}).setdefault(signature, [])
        if nodedef_name not in by_node_signature[node_name][signature]:
            by_node_signature[node_name][signature].append(nodedef_name)

    inputs = node_info.get("inputs", [])
    outputs = node_info.get("outputs", [])
    if len(inputs) == 1 and len(outputs) == 1:
        input_type = _normalize_type(inputs[0].get("type"))
        output_type = _normalize_type(outputs[0].get("type"))
        if input_type and output_type:
            io_key = f"{input_type}->{output_type}"
            by_node_io.setdefault(node_name, {}).setdefault(io_key, [])
            if nodedef_name not in by_node_io[node_name][io_key]:
                by_node_io[node_name][io_key].append(nodedef_name)

    if len(outputs) == 1:
        output_type = _normalize_type(outputs[0].get("type"))
        if output_type:
            by_node_output.setdefault(node_name, {}).setdefault(output_type, [])
            if nodedef_name not in by_node_output[node_name][output_type]:
                by_node_output[node_name][output_type].append(nodedef_name)


def _get_namespace_uri(tag: str) -> Optional[str]:
    if tag and tag.startswith("{") and "}" in tag:
        return tag[1 : tag.find("}")]
    return None


def _format_source_path(repo_root: Path, path: Path) -> str:
    try:
        # Forward slashes on every platform: the manifest is a checked-in
        # contract and must reproduce byte for byte on Windows too.
        return path.resolve().relative_to(repo_root).as_posix()
    except Exception as exc:
        raise ValueError(
            f"MaterialX sources must be vendored below the repository root: {path}"
        ) from exc


def _materialx_document_version(path: Path) -> Optional[str]:
    """Return a MaterialX document version without retaining SDK-local data."""
    try:
        return (ET.parse(path).getroot().get("version", "") or "").strip() or None
    except ET.ParseError:
        return None


def _normalize_type(type_name: Optional[str]) -> str:
    return (type_name or "").strip().lower()


def _signature_from_io(inputs: List[Dict[str, Any]], outputs: List[Dict[str, Any]]) -> str:
    input_sig = ",".join(f"{item.get('name')}:{_normalize_type(item.get('type'))}" for item in inputs)
    output_sig = ",".join(f"{item.get('name')}:{_normalize_type(item.get('type'))}" for item in outputs)
    return f"in[{input_sig}]|out[{output_sig}]"


if __name__ == "__main__":
    raise SystemExit(main())
