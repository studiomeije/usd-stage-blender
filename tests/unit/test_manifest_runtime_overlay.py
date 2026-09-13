"""The manifest's runtime overlay: measured ShaderGraph interface facts.

The base manifest is built from Apple's public References bundle
(MaterialX 1.38-era). The installed ShaderGraph.framework ships a
1.38 + 1.39.4 hybrid library tree with several hundred additional
runtime-resolvable nodedefs and a few widened signatures (the noise
``style`` input). ``scripts/build_materialx_manifest.py`` overlays those
interface facts — names, types, defaults only; no .mtlx content is
vendored — and flags every added entry with ``policy.runtime_overlay``.

These tests pin the overlay's contract against the checked-in manifest.
The regeneration/determinism tests need the installed library tree and
skip without it (CI without RCP).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.manifest.materialx_nodes import (  # noqa: E402
    load_manifest,
    select_nodedef_name_for_node,
)
from scripts.build_materialx_manifest import (  # noqa: E402
    _find_runtime_library,
    build_manifest,
)


@pytest.fixture(scope="module")
def manifest():
    return load_manifest()


# ---------------------------------------------------------------------------
# Overlay entries exist and are honestly labelled.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nodedef_name",
    ["ND_fract_float", "ND_safepower_float", "ND_logical_and"],
)
def test_named_runtime_nodedefs_are_present_as_overlay_entries(
    manifest, nodedef_name
):
    """1.39.4 stdlib nodes absent from the References bundle are targetable."""
    entry = manifest["nodes"].get(nodedef_name)
    assert entry is not None, f"{nodedef_name} missing from the manifest"
    assert entry["policy"]["runtime_overlay"] is True
    assert entry["source_file"].startswith("measured:ShaderGraph.framework/")
    assert entry["inputs"], nodedef_name
    assert entry["outputs"], nodedef_name


def test_overlay_entries_record_measured_sources_only(manifest):
    """Every overlay entry points at the framework, never at a repo file —
    and no References-backed entry claims to be measured."""
    for name, entry in manifest["nodes"].items():
        is_overlay = bool(entry["policy"].get("runtime_overlay"))
        is_measured = str(entry["source_file"]).startswith("measured:")
        assert is_overlay == is_measured, (name, entry["source_file"])


def test_no_overlay_entry_is_editor_unresolvable(manifest):
    """Overlay entries come from the editor's own libraries by construction;
    an entry with both flags would be contradictory."""
    contradictory = [
        name
        for name, entry in manifest["nodes"].items()
        if entry["policy"].get("runtime_overlay")
        and entry["policy"].get("editor_unresolvable")
    ]
    assert contradictory == []


def test_metadata_records_the_measured_build(manifest):
    overlay = manifest["metadata"]["runtime_overlay"]
    assert overlay["source"] == "ShaderGraph.framework/Versions/A/Resources/MaterialX"
    assert overlay["reality_composer_pro_build"] == "80.0.6.0.5"
    assert overlay["shadergraph_version"] == "159.1.1"
    assert overlay["entries_added"] == sum(
        1
        for entry in manifest["nodes"].values()
        if entry["policy"].get("runtime_overlay")
    )


# ---------------------------------------------------------------------------
# Widened signatures: shipped inputs are added, never removed or retyped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nodedef_name",
    ["ND_unifiednoise3d_float", "ND_worleynoise3d_float"],
)
def test_noise_style_input_is_present_with_behavior_preserving_default(
    manifest, nodedef_name
):
    """The shipped noise defs gained an integer `style` enum (Distance,Solid).
    Default "0" (Distance) is the pre-1.39.4 behavior, so graphs that do not
    author the input render unchanged."""
    entry = manifest["nodes"][nodedef_name]
    styles = [item for item in entry["inputs"] if item["name"] == "style"]
    assert len(styles) == 1, nodedef_name
    style = styles[0]
    assert style["type"] == "integer"
    assert style["value"] == "0"
    assert style["enum"] == ["Distance", "Solid"]
    # Marked as a measured addition so selection keys off the References-era
    # interface (see materialx_nodes._declared_types).
    assert style["runtime_overlay"] is True
    # The entry itself stays References-backed.
    assert entry["policy"]["runtime_overlay"] is False


def test_updated_entries_only_gained_inputs(manifest):
    """metadata.runtime_overlay.updated_nodedefs lists exactly the inputs the
    overlay appended, and each is flagged on the input dict."""
    updated = manifest["metadata"]["runtime_overlay"]["updated_nodedefs"]
    assert "ND_unifiednoise3d_float" in updated
    assert "ND_worleynoise3d_float" in updated
    for nodedef_name, added_names in updated.items():
        entry = manifest["nodes"][nodedef_name]
        flagged = [
            item["name"]
            for item in entry["inputs"]
            if item.get("runtime_overlay")
        ]
        assert flagged == added_names, nodedef_name


# ---------------------------------------------------------------------------
# Selection stability: overlay entries never displace References-backed picks.
# ---------------------------------------------------------------------------


def test_overlay_entries_lose_selection_ties_to_references_entries(manifest):
    """`fract` resolves to an overlay entry only because no References-backed
    candidate exists; `mix`, which has References-backed defs, must never
    resolve to an overlay entry even though the overlay added variants."""
    fract = select_nodedef_name_for_node(manifest, "fract", output_type="float")
    assert fract == "ND_fract_float"

    for node_name in ("mix", "convert", "image", "normalmap", "combine3"):
        selected = select_nodedef_name_for_node(manifest, node_name)
        if selected is None:
            continue
        entry = manifest["nodes"][selected]
        assert not entry["policy"].get("runtime_overlay"), (node_name, selected)


# ---------------------------------------------------------------------------
# Determinism: same inputs -> byte-identical JSON.
# ---------------------------------------------------------------------------

RUNTIME_LIBRARY = _find_runtime_library()


def test_generation_with_overlay_is_deterministic():
    source = REPO_ROOT / "References" / "MaterialX-definitions"
    first = build_manifest(
        REPO_ROOT, source, include_half=False, runtime_library=RUNTIME_LIBRARY
    )
    second = build_manifest(
        REPO_ROOT, source, include_half=False, runtime_library=RUNTIME_LIBRARY
    )
    assert json.dumps(first, indent=2) == json.dumps(second, indent=2)


def test_checked_in_manifest_reproduces_without_the_libraries_by_carrying_the_overlay():
    """A machine without Reality Composer Pro (every CI runner) cannot measure
    the overlay, but it can replay the one recorded in the checked-in manifest.
    The rebuild must then reproduce the checked-in bytes exactly, which proves
    the vendored part still matches; the overlay itself is verified on a Mac
    by the test below."""
    source = REPO_ROOT / "References" / "MaterialX-definitions"
    checked_in_path = REPO_ROOT / "Plugin" / "manifest" / "rk_nodes_manifest.json"
    built = build_manifest(
        REPO_ROOT,
        source,
        include_half=False,
        runtime_library=None,
        carry_forward_from=checked_in_path,
    )
    assert json.dumps(built, indent=2) == checked_in_path.read_text()


def test_carry_forward_replays_only_what_the_overlay_recorded(tmp_path):
    """Entries the overlay never touched come from the vendored bundle, so a
    stale checked-in copy of one cannot be smuggled back in by the replay."""
    source = REPO_ROOT / "References" / "MaterialX-definitions"
    checked_in = json.loads(
        (REPO_ROOT / "Plugin" / "manifest" / "rk_nodes_manifest.json").read_text()
    )
    tampered = json.loads(json.dumps(checked_in))
    untouched = next(
        name
        for name, entry in tampered["nodes"].items()
        if not entry["policy"].get("runtime_overlay")
        and name not in tampered["metadata"]["runtime_overlay"]["updated_nodedefs"]
        and name not in tampered["metadata"]["runtime_overlay"]["renamed_inputs"]
    )
    tampered["nodes"][untouched]["inputs"].append(
        {"name": "smuggled", "type": "float", "value": "0", "uniform": False}
    )
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered, indent=2))

    built = build_manifest(
        REPO_ROOT, source, include_half=False, runtime_library=None, carry_forward_from=path
    )
    assert "smuggled" not in {i["name"] for i in built["nodes"][untouched]["inputs"]}
    assert built["nodes"][untouched] == checked_in["nodes"][untouched]


@pytest.mark.skipif(
    RUNTIME_LIBRARY is None,
    reason="ShaderGraph.framework MaterialX libraries not installed",
)
def test_checked_in_manifest_reproduces_from_the_installed_libraries():
    """Regenerating against the installed tree must reproduce the checked-in
    bytes exactly; drift means Apple shipped new libraries (rerun the
    generator and review) or the generator changed without regenerating."""
    source = REPO_ROOT / "References" / "MaterialX-definitions"
    built = build_manifest(
        REPO_ROOT, source, include_half=False, runtime_library=RUNTIME_LIBRARY
    )
    checked_in = (
        REPO_ROOT / "Plugin" / "manifest" / "rk_nodes_manifest.json"
    ).read_text()
    assert json.dumps(built, indent=2) == checked_in
