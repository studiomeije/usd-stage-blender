"""Derivative and filtered-sampling nodes pass preflight.

Measured on macOS 27.2.1 (26B131) with the RealityKit material probe harness:
``ND_MTL_dfdx/dfdy/fwidth`` compile from a USD-loaded ShaderGraph and return
exact screen-space derivatives, in float and half, and
``RealityKitTexture2DGradient`` and ``RealityKitTexture2DLOD`` honour the
derivatives and level of detail they are given. A graph that uses them must
pass the exporter's preflight. ``ND_hextiledimage`` renders on macOS 27.2 but
Reality Composer Pro 3's ShaderGraph build has no RealityKit implementation of
it, which the preflight reports.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Sdf, UsdShade  # noqa: E402

from Plugin.export.realitykit_preflight import validate_stage  # noqa: E402
from Plugin.manifest.materialx_nodes import load_manifest  # noqa: E402
from tests.unit.test_realitykit_preflight import _codes, _stage_with_mesh  # noqa: E402

DERIVATIVES = [
    f"ND_MTL_{op}_{kind}"
    for op in ("dfdx", "dfdy", "fwidth")
    for kind in ("float", "half", "vector2", "half2", "vector3", "half3", "vector4", "half4")
]
FILTERED = [
    f"ND_{family}_{kind}"
    for family in ("RealityKitTexture2DGradient", "RealityKitTexture2DLOD")
    for kind in ("color3", "color4", "vector4")
]


def _material_with(stage, nodedef):
    material = UsdShade.Material.Define(stage, "/Root/Material")
    material.GetPrim().CreateAttribute("config:mtlx:version", Sdf.ValueTypeNames.String).Set("1.39")
    surface = UsdShade.Shader.Define(stage, "/Root/Material/Surface")
    surface.CreateIdAttr("ND_realitykit_unlit_surfaceshader")
    material.CreateSurfaceOutput("mtlx").ConnectToSource(surface.ConnectableAPI(), "out")
    node = UsdShade.Shader.Define(stage, "/Root/Material/Node")
    node.CreateIdAttr(nodedef)
    return node


@pytest.mark.parametrize("nodedef", DERIVATIVES + FILTERED)
def test_the_manifest_declares_the_measured_nodes(nodedef):
    node = load_manifest()["nodes"][nodedef]
    assert not node["policy"]["editor_unresolvable"]


def test_derivatives_take_their_declared_input_and_type():
    nodes = load_manifest()["nodes"]
    assert [i["name"] for i in nodes["ND_MTL_fwidth_vector3"]["inputs"]] == ["p"]
    assert nodes["ND_MTL_dfdx_half"]["policy"]["half_type"]
    assert not nodes["ND_MTL_dfdx_float"]["policy"]["half_type"]


@pytest.mark.parametrize("nodedef", DERIVATIVES + FILTERED)
def test_a_graph_using_the_node_passes_preflight(nodedef):
    stage, _mesh = _stage_with_mesh()
    _material_with(stage, nodedef)

    codes = _codes(validate_stage(stage))

    assert "UNKNOWN_MATERIALX_NODEDEF" not in codes
    assert "MATERIALX_NODEDEF_ABSENT_AT_DECLARED_VERSION" not in codes
    assert "MATERIALX_NODEDEF_UNIMPLEMENTED_IN_SOME_BUILDS" not in codes or nodedef.startswith("ND_MTL_")


@pytest.mark.parametrize(
    "nodedef", ["ND_hextiledimage_color3", "ND_hextiledimage_color4", "ND_hextilednormalmap_vector3"]
)
def test_hex_tiling_warns_that_reality_composer_pro_cannot_build_it(nodedef):
    stage, _mesh = _stage_with_mesh()
    _material_with(stage, nodedef)

    report = validate_stage(stage)
    issue = next(i for i in report.issues if i.code == "MATERIALX_NODEDEF_UNIMPLEMENTED_IN_SOME_BUILDS")

    assert issue.severity == "warning"
    assert "159.0.5" in issue.message
    assert "UNKNOWN_MATERIALX_NODEDEF" not in _codes(report)


def test_per_build_availability_is_recorded_in_the_manifest():
    manifest = load_manifest()
    policy = manifest["nodes"]["ND_hextiledimage_color3"]["policy"]
    assert policy["unimplemented_in_shadergraph_builds"] == ["159.0.5"]
    assert "159.40.2" in manifest["metadata"]["shadergraph_builds"]
