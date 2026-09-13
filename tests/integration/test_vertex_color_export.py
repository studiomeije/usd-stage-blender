"""Integration - Color Attribute exports as a geomcolor primvar read.

Blender's USD exporter writes each mesh color attribute as
``primvars:<attribute name>``, and that primvar is ``color4f[]`` for every
attribute type and domain - measured here against the real export, one cube
per case:

===================  ====================  =================
Blender attribute    USD primvar type      interpolation
===================  ====================  =================
FLOAT_COLOR/CORNER   ``color4f[]``         ``faceVarying``
FLOAT_COLOR/POINT    ``color4f[]``         ``vertex``
BYTE_COLOR/CORNER    ``color4f[]``         ``faceVarying``
BYTE_COLOR/POINT     ``color4f[]``         ``vertex``
===================  ====================  =================

The exact translation is therefore ND_geomcolor_color4 reading the same
name, with an implemented convert adapting it to a narrower consumer. A
color3 read of a color4f primvar type-mismatches, and Reality Composer Pro
3.0 replaces the whole material with its striped placeholder rather than
erroring visibly. The read is only authored when the named attribute actually
reaches the export: a node naming a missing attribute refuses instead of
authoring a dangling read.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

#: (object/attribute name, Blender attribute type, Blender domain).
ATTRIBUTE_CASES = [
    ("FloatCorner", "FLOAT_COLOR", "CORNER"),
    ("FloatPoint", "FLOAT_COLOR", "POINT"),
    ("ByteCorner", "BYTE_COLOR", "CORNER"),
    ("BytePoint", "BYTE_COLOR", "POINT"),
]

_BUILD_MATRIX = r'''
import bpy, json, sys
out = sys.argv[sys.argv.index("--") + 1]
cases = json.loads(sys.argv[sys.argv.index("--") + 2])

bpy.ops.wm.read_factory_settings(use_empty=True)

for index, (name, attr_type, domain) in enumerate(cases):
    bpy.ops.mesh.primitive_cube_add(location=(index * 3.0, 0.0, 0.0))
    obj = bpy.context.active_object
    obj.name = name
    mesh = obj.data
    attribute = mesh.color_attributes.new(
        name=name, type=attr_type, domain=domain
    )
    for item in attribute.data:
        item.color = (0.2, 0.5, 0.8, 0.75)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.uv.smart_project()
    bpy.ops.object.mode_set(mode='OBJECT')
    material = bpy.data.materials.new("M_" + name)
    mesh.materials.append(material)
    tree = material.node_tree
    bsdf = tree.nodes["Principled BSDF"]
    node = tree.nodes.new("ShaderNodeVertexColor")
    node.layer_name = name
    tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])

bpy.ops.wm.save_as_mainfile(filepath=out)
'''

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
layer_name = sys.argv[sys.argv.index("--") + 2]
wire_alpha = sys.argv[sys.argv.index("--") + 3] == "alpha"

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
obj = bpy.context.active_object
mesh = obj.data
attribute = mesh.color_attributes.new(
    name="MyColors", type='BYTE_COLOR', domain='CORNER'
)
for item in attribute.data:
    item.color = (0.2, 0.5, 0.8, 0.75)

bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("VertexColored")
obj.data.materials.append(material)
tree = material.node_tree
bsdf = tree.nodes["Principled BSDF"]
node = tree.nodes.new("ShaderNodeVertexColor")
node.layer_name = layer_name
tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
if wire_alpha:
    material.blend_method = 'BLEND'
    tree.links.new(node.outputs["Alpha"], bsdf.inputs["Alpha"])

bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _run_build(script_body: str, tmp_path: Path, name: str, args: list[str]) -> Path:
    script = tmp_path / f"build_{name}.py"
    script.write_text(script_body)
    blend = tmp_path / f"{name}.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend), *args],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _build_blend(tmp_path: Path, layer_name: str, *, alpha: bool = False) -> Path:
    return _run_build(
        _BUILD,
        tmp_path,
        f"vertex_color_{layer_name}{'_alpha' if alpha else ''}",
        [layer_name, "alpha" if alpha else "color"],
    )


def _export(blend: Path, out_dir: Path, name: str = "vc.usda", fmt: str = "USDA"):
    out_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "export", str(blend), "-o", str(out_dir / name),
         "--format", fmt, "--diagnostics"],
        capture_output=True, text=True, timeout=900,
    )


@pytest.fixture(scope="module")
def vertex_color_export(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("vertex_color")
    blend = _build_blend(workdir, "MyColors")
    result = _export(blend, workdir / "out")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload, workdir / "out" / "vc.usda"


@pytest.fixture(scope="module")
def attribute_matrix_usdz(tmp_path_factory):
    """One USDZ carrying every attribute-type/domain combination."""
    workdir = tmp_path_factory.mktemp("vertex_color_matrix")
    blend = _run_build(
        _BUILD_MATRIX, workdir, "matrix", [json.dumps(ATTRIBUTE_CASES)]
    )
    result = _export(blend, workdir / "out", name="matrix.usdz", fmt="USDZ")
    assert result.returncode == 0, result.stdout + result.stderr
    return workdir / "out" / "matrix.usdz"


def _read_primvars_and_reads(stage_path: Path):
    """(primvar type by attribute name, geomcolor node id by geomprop)."""
    from pxr import Usd, UsdGeom, UsdShade

    stage = Usd.Stage.Open(str(stage_path))
    primvars: dict[str, tuple[str, str]] = {}
    reads: dict[str, str] = {}
    for prim in stage.Traverse():
        if prim.IsA(UsdGeom.Mesh):
            for primvar in UsdGeom.PrimvarsAPI(prim).GetPrimvars():
                name = str(primvar.GetPrimvarName())
                primvars[name] = (
                    str(primvar.GetTypeName()), primvar.GetInterpolation()
                )
        if prim.IsA(UsdShade.Shader):
            shader = UsdShade.Shader(prim)
            node_id = str(shader.GetIdAttr().Get() or "")
            if node_id.startswith("ND_geomcolor"):
                # geomcolor selects a colour set by index, not by name, so
                # attribute the read to its owning material instead.
                owner = prim.GetPath().GetParentPath().name
                reads[owner] = node_id
    return primvars, reads


@pytest.mark.parametrize("name,attr_type,domain", ATTRIBUTE_CASES)
def test_geomcolor_node_type_agrees_with_the_primvar(
    attribute_matrix_usdz, name, attr_type, domain
):
    """The authored read declares the type the primvar actually carries."""
    primvars, reads = _read_primvars_and_reads(attribute_matrix_usdz)

    assert name in primvars, f"mesh primvar missing from the export: {name}"
    primvar_type, interpolation = primvars[name]
    assert primvar_type == "color4f[]", (
        f"{attr_type}/{domain} exported as {primvar_type}; the measured table "
        "in this module's docstring is stale, and the node choice with it"
    )
    assert interpolation == (
        "faceVarying" if domain == "CORNER" else "vertex"
    )

    assert reads.get(f"M_{name}", reads.get(name)) == "ND_geomcolor_color4", (
        f"{name} is a {primvar_type} primvar read by {reads.get(name)}"
    )


def test_attribute_matrix_passes_arkit_strict_usdchecker(attribute_matrix_usdz):
    usdchecker = shutil.which("usdchecker") or "/usr/bin/usdchecker"
    if not Path(usdchecker).exists():
        pytest.skip("usdchecker not available")
    checked = subprocess.run(
        [usdchecker, "--arkit", "--strict", str(attribute_matrix_usdz)],
        capture_output=True, text=True, timeout=300,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_geomcolor_reads_the_exported_primvar(vertex_color_export):
    _payload, stage = vertex_color_export
    text = stage.read_text()

    # Blender's USD exporter names the primvar after the attribute; the
    # geomcolor read must reference exactly that name, at the type the
    # primvar carries.
    assert "primvars:MyColors" in text, "mesh primvar missing from the export"
    assert 'info:id = "ND_geomcolor_color4"' in text
    # geomcolor addresses the colour set by index, not by name; the
    # primvar's presence asserted above is what proves the data is there.
    assert "int inputs:index = 0" in text
    assert 'info:id = "ND_geomcolor_color3"' not in text


def test_color3_consumer_reaches_the_read_through_a_convert(vertex_color_export):
    """Base Color is color3; the four-channel read adapts explicitly."""
    _payload, stage = vertex_color_export
    text = stage.read_text()

    assert 'info:id = "ND_convert_color4_color3"' in text
    assert "ND_swizzle" not in text
    # The convert's input carries the read's own type, not a narrowed one.
    assert "color4f inputs:in.connect" in text


def test_vertex_color_export_fires_no_capability_warning(vertex_color_export):
    payload, _stage = vertex_color_export
    noisy = [
        warning
        for warning in payload.get("warnings") or []
        if any(
            term in warning.lower()
            for term in ("unrecognized", "requires baking", "limited support")
        )
    ]
    assert noisy == [], noisy


def test_alpha_output_exports_as_the_fourth_channel(tmp_path):
    """The Alpha output is exact once the read is four-channel."""
    blend = _build_blend(tmp_path, "MyColors", alpha=True)
    result = _export(blend, tmp_path / "out")
    assert result.returncode == 0, result.stdout + result.stderr

    text = (tmp_path / "out" / "vc.usda").read_text()
    assert 'info:id = "ND_geomcolor_color4"' in text
    assert 'info:id = "ND_dotproduct_vector4"' in text
    # The alpha channel is selected by the dot-product mask, not a channels
    # string.
    assert "float4 inputs:in2 = (0, 0, 0, 1)" in text

    payload = json.loads(result.stdout)
    refusals = [
        warning
        for warning in payload.get("warnings") or []
        if "Alpha output" in warning
    ]
    assert refusals == [], refusals


def test_missing_attribute_refuses_instead_of_dangling(tmp_path):
    blend = _build_blend(tmp_path, "NoSuchAttribute")
    result = _export(blend, tmp_path / "out")
    assert result.returncode != 0, (
        "a Color Attribute naming a missing attribute must refuse, not author "
        "a dangling geomcolor read\n" + result.stdout
    )
    combined = result.stdout + result.stderr
    assert "NoSuchAttribute" in combined
