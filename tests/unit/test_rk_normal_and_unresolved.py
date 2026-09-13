"""Two extraction defects that produced silently wrong or silently missing data.

1. The two RK extraction paths disagreed about normal textures.
   _extract_group_inputs decided by socket name and produced a
   normal_map_decode; _build_rk_node_graph never decided at all and authored a
   raw colour->vector convert. The same RK PBR Surface group therefore exported
   different normals depending on which path ran.

2. A nested unresolved sub-expression vanished. Only the top-level `kind` was
   checked for "unresolved", so a resolver branch that wrapped a failed child
   returned a node expression, the graph builder dropped the bad child, and the
   input silently fell back to a nodedef default.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export.materials.extract import core  # noqa: E402


# --- 1. normal semantics come from the nodedef ------------------------------

RK_PBR = "ND_realitykit_pbr_surfaceshader"
RK_PBR2 = "ND_realitykit_pbr_surfaceshader_2_0"


@pytest.mark.parametrize(
    "node_id,input_name",
    [(RK_PBR, "normal"), (RK_PBR, "clearcoatNormal"), (RK_PBR2, "bentNormal")],
)
def test_vector3_unit_z_inputs_expect_a_decoded_normal(node_id, input_name):
    assert core._input_expects_decoded_normal(node_id, input_name) is True


@pytest.mark.parametrize(
    "input_name", ["baseColor", "roughness", "metallic", "opacity"]
)
def test_other_inputs_do_not_expect_a_decoded_normal(input_name):
    # A resolved nodedef is authoritative: these stay raw however their socket
    # is spelled upstream.
    assert core._input_expects_decoded_normal(RK_PBR, input_name) is False


def test_unresolvable_nodedef_falls_back_to_the_socket_name():
    """A user node group has no manifest entry; the name is all there is."""
    assert core._input_expects_decoded_normal(None, "Normal Map") is True
    assert core._input_expects_decoded_normal(None, "Base Color") is False


def test_a_unit_z_default_alone_is_not_a_normal_socket():
    """``ND_transformnormal_vector3.in`` defaults to the unit Z vector but
    receives an ordinary direction, not an encoded normal map. The manifest
    spells it "0.0, 0.0, 1.0" while the surface normals use "0, 0, 1", so a
    literal string comparison got the right answer by formatting coincidence —
    any manifest regeneration that normalizes number formatting would have
    silently started decoding normals into transformnormal. The rule must
    judge the value numerically AND require the socket name to say normal."""
    assert core._input_expects_decoded_normal("ND_transformnormal_vector3", "in") is False
    # The same socket judged numerically: still unit Z.
    assert core._is_unit_z_vector(
        core._input_mtlx_default("ND_transformnormal_vector3", "in")
    )


def test_formatting_variants_of_unit_z_are_equivalent():
    for spelling in ("0, 0, 1", "0.0, 0.0, 1.0", "0,0,1", " 0.0 ,0 , 1 "):
        assert core._is_unit_z_vector(spelling), spelling
    for spelling in ("0, 1, 0", "0.5, 0.5, 1.0", "", "0, 0", "a, b, c", None):
        assert not core._is_unit_z_vector(spelling), spelling


# --- 2. unresolved children propagate ---------------------------------------


def _unresolved(reason="unsupported"):
    return {"kind": "unresolved", "reason": reason, "provenance": ["TEX_NOISE"]}


def test_a_node_with_an_unresolved_input_is_itself_unresolved():
    expr = core._make_node_expr("ND_mix_color3", {"fg": _unresolved(), "bg": 1.0})

    assert expr["kind"] == "unresolved"


def test_the_childs_provenance_survives():
    """The warning must name the node that failed, not the one wrapping it."""
    expr = core._make_node_expr("ND_mix_color3", {"fg": _unresolved("noise")})

    assert expr["reason"] == "noise"
    assert expr["provenance"] == ["TEX_NOISE"]


def test_a_fully_resolved_node_is_unchanged():
    expr = core._make_node_expr(
        "ND_mix_color3", {"fg": {"kind": "constant", "value": 1.0}, "bg": 0.0}
    )

    assert expr["kind"] == "node"
    assert expr["node_id"] == "ND_mix_color3"


def test_a_node_with_no_inputs_is_unchanged():
    assert core._make_node_expr("ND_normalmap", {})["kind"] == "node"
