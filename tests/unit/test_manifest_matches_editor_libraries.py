"""The manifest's editor_unresolvable flags must match the installed RCP.

The manifest is built from Apple's public definition bundle, which includes
pbrlib closure-domain nodedefs (BSDF/EDF/VDF, displacement, ND_surface, the
arrayappend family) that RCP's ShaderGraph editor cannot resolve — they ship
in the app only inside USD parsing libraries. The generator flags them with
``policy.editor_unresolvable`` from a measured literal set; this module
recomputes that set from the installed app's ShaderGraph.framework libraries
so an RCP update that adds or removes definitions fails loudly here instead
of silently drifting.

Skips when the app is not installed (CI without RCP).
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402

SHADERGRAPH_LIBRARIES = Path(
    "/Applications/RealityComposerPro.app/Contents/SystemFrameworks/"
    "ShaderGraph.framework/Versions/A/Resources/MaterialX"
)

pytestmark = pytest.mark.skipif(
    not SHADERGRAPH_LIBRARIES.is_dir(),
    reason="RealityComposerPro.app ShaderGraph libraries not installed",
)

# `name` is not always the first attribute (apple_integer_ops.mtlx orders
# apple_availability first), so match it anywhere inside the tag.
_NODEDEF_RE = re.compile(r'<nodedef\b[^>]*?\bname="(ND_[^"]+)"')


@pytest.fixture(scope="module")
def editor_nodedefs() -> frozenset:
    names: set[str] = set()
    for library in SHADERGRAPH_LIBRARIES.rglob("*.mtlx"):
        names.update(_NODEDEF_RE.findall(library.read_text(errors="ignore")))
    assert len(names) > 1500, "editor library sweep looks incomplete"
    return frozenset(names)


def test_editor_unresolvable_flags_match_the_installed_app(editor_nodedefs):
    manifest = load_manifest()["nodes"]

    measured = {name for name in manifest if name not in editor_nodedefs}
    flagged = {
        name
        for name, node in manifest.items()
        if (node.get("policy") or {}).get("editor_unresolvable")
    }

    assert flagged == measured, (
        "flag drift vs the installed RCP — regenerate via "
        "scripts/build_materialx_manifest.py after updating "
        f"EDITOR_UNRESOLVABLE_NODEDEFS\nonly flagged: {sorted(flagged - measured)}\n"
        f"only measured: {sorted(measured - flagged)}"
    )


def test_every_flag_names_a_pbrlib_or_arrayappend_def():
    """The measured set has a clean shape; a flag outside it deserves a
    fresh look rather than silent acceptance."""
    manifest = load_manifest()["nodes"]
    for name, node in manifest.items():
        if not (node.get("policy") or {}).get("editor_unresolvable"):
            continue
        source = str(node.get("source_file") or "")
        assert source.endswith(("pbrlib_defs.mtlx", "stdlib_defs.mtlx")), (
            name,
            source,
        )
