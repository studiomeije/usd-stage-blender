"""Authored shader inputs must exist on the nodedef RealityKit will bind.

RealityKit keeps one MaterialX nodedef store per declared version, and the two
disagree about which inputs a nodedef has: ``blend``/``upaxis`` are on 1.39's
triplanar but not 1.38's, ``operationorder`` is on 1.39's place2d but not
1.38's, and ``atan2`` takes ``iny``/``inx`` in both while vanilla upstream
MaterialX says ``in1``/``in2``.

Getting one wrong is not a cosmetic mismatch. Measured with ``realitytool
compile`` on scene 15: authoring ``blend`` and ``operationorder`` cost two of
the four materials their *entire* shader graph - every texture binding included
- replaced by a default-parameter PBR material. ``realitytool`` exited 0 and
printed nothing, and ``usdchecker --arkit --strict`` passed. The only symptom
was an untextured object in Reality Composer Pro.

These tests pin the gate that catches it before the export ships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdShade  # noqa: E402

from Plugin.export import realitykit_preflight  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GAP_TABLE = REPO_ROOT / "Plugin" / "manifest" / "rcp_nodedef_input_gaps.json"


def _material_with_shader(nodedef, inputs, mtlx_version="1.38"):
    """A minimal Material carrying one MaterialX shader."""
    stage = Usd.Stage.CreateInMemory()
    material = UsdShade.Material.Define(stage, "/Root/Mat")
    if mtlx_version is not None:
        material.GetPrim().CreateAttribute(
            "config:mtlx:version", Sdf.ValueTypeNames.String
        ).Set(mtlx_version)
    shader = UsdShade.Shader.Define(stage, "/Root/Mat/Node")
    shader.CreateIdAttr(nodedef)
    for name, (value_type, value) in inputs.items():
        shader.CreateInput(name, value_type).Set(value)
    return stage, shader.GetPrim()


def _run(prim):
    report = realitykit_preflight.RealityKitPreflightReport()
    realitykit_preflight._check_materialx_node_inputs([prim], report)
    return report


def _codes(report):
    return {issue.code for issue in report.issues}


def test_gap_table_is_present_and_names_the_rcp_build_it_was_measured_on():
    table = json.loads(GAP_TABLE.read_text(encoding="utf-8"))
    assert table["_rcp_build"], "the table must record which RCP it was measured against"
    by_version = table["by_version"]
    assert set(by_version) == {"1.38", "1.39"}
    # The two that cost scene 15 its materials.
    assert "blend" in by_version["1.38"]["inputs"]["ND_triplanarprojection_color3"]
    assert "operationorder" in by_version["1.38"]["inputs"]["ND_place2d_vector2"]
    # And the version split is real: 1.39 has them, so they must not be listed.
    assert "ND_triplanarprojection_color3" not in by_version["1.39"]["inputs"]
    assert "ND_place2d_vector2" not in by_version["1.39"]["inputs"]
    # ND_normalmap is the mirror case: fine at 1.38, gone at 1.39.
    assert "ND_normalmap" in by_version["1.39"]["unresolved"]
    assert "ND_normalmap" not in by_version["1.38"]["unresolved"]


def test_triplanar_blend_is_refused_at_the_version_we_declare():
    stage, prim = _material_with_shader(
        "ND_triplanarprojection_color3",
        {"blend": (Sdf.ValueTypeNames.Float, 0.25)},
    )
    report = _run(prim)
    assert "MATERIALX_INPUT_ABSENT_FROM_NODEDEF" in _codes(report)
    issue = next(i for i in report.issues if i.code == "MATERIALX_INPUT_ABSENT_FROM_NODEDEF")
    assert issue.severity == "error"
    assert issue.details["unsupported_inputs"] == ["blend"]
    assert issue.details["mtlx_version"] == "1.38"


def test_place2d_operationorder_is_refused():
    stage, prim = _material_with_shader(
        "ND_place2d_vector2",
        {"operationorder": (Sdf.ValueTypeNames.Int, 0)},
    )
    assert "MATERIALX_INPUT_ABSENT_FROM_NODEDEF" in _codes(_run(prim))


def test_the_same_input_is_accepted_when_the_material_declares_1_39():
    """The gate is version-aware, not a blanket denylist."""
    stage, prim = _material_with_shader(
        "ND_triplanarprojection_color3",
        {"blend": (Sdf.ValueTypeNames.Float, 0.25)},
        mtlx_version="1.39",
    )
    assert _codes(_run(prim)) == set()


def test_a_material_with_no_declared_version_is_judged_as_1_38():
    """RealityKit's own loader says so: libtm-material carries the string
    'unrecognized MaterialX version "%s"; treating as 1.38'."""
    stage, prim = _material_with_shader(
        "ND_triplanarprojection_color3",
        {"blend": (Sdf.ValueTypeNames.Float, 0.25)},
        mtlx_version=None,
    )
    assert "MATERIALX_INPUT_ABSENT_FROM_NODEDEF" in _codes(_run(prim))


def test_declared_inputs_pass_cleanly():
    stage, prim = _material_with_shader(
        "ND_place2d_vector2",
        {
            "rotate": (Sdf.ValueTypeNames.Float, 45.0),
            "offset": (Sdf.ValueTypeNames.Float2, (0.1, 0.2)),
        },
    )
    assert _codes(_run(prim)) == set()


def test_usd_schema_shaders_are_not_judged():
    """A preview network is UsdPreviewSurface/UsdUVTexture - USD schemas, not
    MaterialX nodedefs, and not this gate's business."""
    stage, prim = _material_with_shader(
        "UsdUVTexture",
        {"bias": (Sdf.ValueTypeNames.Float4, (0.0, 0.0, 0.0, 0.0))},
    )
    assert _codes(_run(prim)) == set()


def test_manifest_atan2_matches_the_nodedef_realitykit_binds():
    """The manifest is generated from a vendored vanilla-upstream MaterialX
    copy, which spells atan2's inputs in1/in2. The library RealityKit actually
    loads spells them iny/inx, and the generator now adopts the shipped names.
    """
    for suffix in ("float", "vector2", "vector3", "vector4"):
        entry = load_manifest()["nodes"][f"ND_atan2_{suffix}"]
        assert [i["name"] for i in entry["inputs"]] == ["iny", "inx"], suffix


def test_no_atan2_variant_is_listed_as_having_a_phantom_input():
    """Once the manifest agrees with the shipped nodedef, atan2 must fall out
    of the gap table at every version - otherwise the manifest regressed."""
    table = json.loads(GAP_TABLE.read_text(encoding="utf-8"))
    for version, entries in table["by_version"].items():
        offenders = [
            name for name in entries["inputs"] if name.startswith("ND_atan2_")
        ]
        assert offenders == [], (version, offenders)


def test_a_nodedef_missing_at_the_declared_version_is_refused():
    """ND_normalmap resolves at 1.38 and not at 1.39. A material declaring 1.39
    and authoring it loses its whole shader graph, and neither realitytool nor
    usdchecker says a word - the same class as the input gate above, one level
    up."""
    stage, prim = _material_with_shader(
        "ND_normalmap", {"scale": (Sdf.ValueTypeNames.Float, 0.6)}, mtlx_version="1.39"
    )
    assert "MATERIALX_NODEDEF_ABSENT_AT_DECLARED_VERSION" in _codes(_run(prim))

    stage, prim = _material_with_shader(
        "ND_normalmap", {"scale": (Sdf.ValueTypeNames.Float, 0.6)}, mtlx_version="1.38"
    )
    assert _codes(_run(prim)) == set()
