"""
MaterialX nodedef manifest loader + selectors.

`rk_nodes_manifest.json` is a prebuilt index of all MaterialX nodedefs we can
target (RealityKit + stdlib/pbrlib). It is generated from Apple's `.mtlx`
definition files by `scripts/build_materialx_manifest.py` in the repository.

Important: the Blender add-on does not rebuild this manifest at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.paths import manifest_path as _manifest_path


MANIFEST_SCHEMA_VERSION = "2.0.0"


class ManifestError(RuntimeError):
    pass


def get_manifest_path() -> Path:
    """Return the path to the bundled manifest JSON."""
    return _manifest_path()


def load_manifest() -> Dict[str, Any]:
    """Load the bundled manifest JSON (no rebuild)."""
    manifest_path = get_manifest_path()
    if not manifest_path.exists():
        raise ManifestError(
            f"MaterialX manifest missing: {manifest_path}. Reinstall the USD Stage for Blender extension."
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ManifestError(
            f"Failed to parse MaterialX manifest: {manifest_path}: {exc}. Reinstall the USD Stage for Blender extension."
        ) from exc

    _validate_manifest(manifest, manifest_path)
    return manifest


def get_node_def(manifest: Dict[str, Any], nodedef_name: str) -> Optional[Dict[str, Any]]:
    """Return a nodedef entry by its nodedef name."""
    if not manifest:
        return None
    return manifest.get("nodes", {}).get(nodedef_name)
def _declared_types(node_def: Optional[Dict[str, Any]], key: str) -> set:
    """Types a nodedef declares on the given axis, for constraint checks.

    Inputs marked ``runtime_overlay`` were measured from the installed
    ShaderGraph libraries and appended to a References-era entry (e.g. the
    noise ``style`` or triplanar ``blend`` inputs). They are uniform across
    the type-suffixed variants of a node, so they carry no
    variant-discriminating information — counting them would let e.g. a
    ``float`` request match ND_triplanarprojection_color3 through its added
    ``blend:float`` input. Selection keys off the References-era interface.
    """
    return {
        _normalize_type(entry.get("type"))
        for entry in ((node_def or {}).get(key) or [])
        if entry.get("type") and not entry.get("runtime_overlay")
    }


def _nodedef_satisfies(
    manifest: Dict[str, Any],
    nodedef_name: str,
    input_type: Optional[str],
    output_type: Optional[str],
) -> bool:
    """Return whether a nodedef can honour the types the caller asked for.

    A nodedef with no declared inputs (or no declared outputs) is not rejected
    on that axis: the manifest does not describe every node exhaustively, and
    refusing on missing metadata would fail closed on nodes that are fine.
    """
    node_def = manifest.get("nodes", {}).get(nodedef_name)
    if node_def is None:
        return False
    if input_type:
        declared = _declared_types(node_def, "inputs")
        if declared and _normalize_type(input_type) not in declared:
            return False
    if output_type:
        declared = _declared_types(node_def, "outputs")
        if declared and _normalize_type(output_type) not in declared:
            return False
    return True


def select_nodedef_name_for_node(
    manifest: Dict[str, Any],
    node_name: str,
    input_type: Optional[str] = None,
    output_type: Optional[str] = None,
    signature: Optional[str] = None,
    prefer_non_half: bool = True,
) -> Optional[str]:
    """Select the best nodedef name for a node based on IO signature."""
    if not manifest or not node_name:
        return None

    index = manifest.get("index", {})
    by_node_signature = index.get("by_node_signature", {})
    by_node_io = index.get("by_node_io", {})
    by_node_output = index.get("by_node_output", {})

    candidates: List[str] = []

    if signature:
        candidates = list(by_node_signature.get(node_name, {}).get(signature, []))

    if not candidates and input_type and output_type:
        io_key = f"{_normalize_type(input_type)}->{_normalize_type(output_type)}"
        candidates = list(by_node_io.get(node_name, {}).get(io_key, []))

    if not candidates and output_type:
        candidates = list(by_node_output.get(node_name, {}).get(_normalize_type(output_type), []))

    if not candidates:
        candidates = list(index.get("by_node", {}).get(node_name, []))

    # A type the caller supplied is a constraint, not a hint. The looser
    # indexes above are still consulted - many nodes carry only a by_node entry
    # with type-suffixed variants, e.g. separate4 - but a candidate that cannot
    # accept the value, or cannot produce the requested one, is dropped rather
    # than returned. Callers treat any non-None result as success, so returning
    # a mismatched nodedef means the "no mapping" diagnostic never fires and
    # invalid MaterialX reaches the stage.
    #
    # Measured against the shipped manifest: `convert` has no color3->float
    # entry in by_node_io, and the output-only fallback returned
    # ND_convert_boolean_float (input type boolean), which was authored with a
    # color3f `in` and wired to inputs:roughness. `luminance` has no float
    # output at all, and the by_node fallback returned ND_luminance_color3 for
    # an output_type of float. Both shipped from a plain
    # `Image Texture -> RGB to BW -> Roughness` graph with ok: true.
    candidates = [
        name
        for name in candidates
        if _nodedef_satisfies(manifest, name, input_type, output_type)
        and not _editor_unresolvable(manifest, name)
    ]

    if not candidates:
        return None

    # The library's default version first, then the newest, so a node id with
    # several versions (PBR Surface and PBR Surface 2.0) resolves to the
    # current one.
    candidates = sorted(
        sorted(set(candidates)),
        key=lambda name: node_version_key(manifest, name),
        reverse=True,
    )
    return _pick_nodedef(manifest, candidates, prefer_non_half=prefer_non_half)


def node_version_key(manifest: Dict[str, Any], nodedef_name: str) -> tuple:
    """Sort key ranking a nodedef by default-version flag, then version number."""
    node_def = manifest.get("nodes", {}).get(nodedef_name) or {}
    parts = []
    for part in str(node_def.get("node_version") or "").split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    return (bool(node_def.get("is_default_version")), tuple(parts))


def select_node_def_for_node(
    manifest: Dict[str, Any],
    node_name: str,
    input_type: Optional[str] = None,
    output_type: Optional[str] = None,
    signature: Optional[str] = None,
    prefer_non_half: bool = True,
) -> Optional[Dict[str, Any]]:
    """Return the nodedef entry selected for a node name."""
    nodedef_name = select_nodedef_name_for_node(
        manifest,
        node_name,
        input_type=input_type,
        output_type=output_type,
        signature=signature,
        prefer_non_half=prefer_non_half,
    )
    if not nodedef_name:
        return None
    return get_node_def(manifest, nodedef_name)


def _validate_manifest(manifest: Dict[str, Any], manifest_path: Path) -> None:
    if not isinstance(manifest, dict):
        raise ManifestError(f"Invalid manifest format (expected dict): {manifest_path}")

    metadata = manifest.get("metadata")
    if not isinstance(metadata, dict):
        raise ManifestError(f"Invalid manifest metadata: {manifest_path}")

    version = metadata.get("version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"Unsupported manifest schema {version!r} (expected {MANIFEST_SCHEMA_VERSION!r}): "
            f"{manifest_path}. Reinstall the USD Stage for Blender extension."
        )

    nodes = manifest.get("nodes")
    index = manifest.get("index")
    if not isinstance(nodes, dict) or not isinstance(index, dict):
        raise ManifestError(f"Invalid manifest structure (missing nodes/index): {manifest_path}")


def _normalize_type(type_name: Optional[str]) -> str:
    return (type_name or "").strip().lower()


def _editor_unresolvable(manifest: Dict[str, Any], nodedef_name: str) -> bool:
    """Whether RCP's ShaderGraph editor has no definition for this nodedef.

    Measured against the installed Reality Composer Pro 3: the pbrlib
    closure-domain defs and the arrayappend family ship only in the USD
    parsing libraries — usdMtlx reads them, but the editor cannot resolve
    them and the runtime cannot render them, so selecting one would author an
    id RCP fails to bind.
    """
    node = manifest.get("nodes", {}).get(nodedef_name)
    return bool(node and node.get("policy", {}).get("editor_unresolvable"))


def _pick_nodedef(
    manifest: Dict[str, Any],
    candidates: List[str],
    prefer_non_half: bool = True,
) -> Optional[str]:
    if not candidates:
        return None

    def _policy(name: str) -> Dict[str, Any]:
        return (manifest.get("nodes", {}).get(name) or {}).get("policy", {})

    # Runtime-overlay entries (measured from the installed ShaderGraph
    # libraries, not Apple's public References bundle) are only considered
    # when no References-backed candidate remains: selections that resolved
    # before the overlay landed must keep resolving to the same nodedef.
    references_backed = [
        name for name in candidates if not _policy(name).get("runtime_overlay")
    ]
    if references_backed:
        candidates = references_backed

    if not prefer_non_half:
        return candidates[0]

    for name in candidates:
        if not _policy(name).get("half_type"):
            return name
    return candidates[0]
