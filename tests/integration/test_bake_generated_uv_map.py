"""Integration test - a bake whose UV map cannot hold it writes into a generated one.

The plane's UVs run from 0 to 2, so its four quarters wrap onto the same
texels. A cube shades the -X half from a sun straight above. Baked into the
plane's own UV map, both halves would read whichever quarter baked last. Baked
into the generated map, the shaded half stays darker than the lit half.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
texture = sys.argv[sys.argv.index("--") + 2]
shading = sys.argv[sys.argv.index("--") + 3]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.samples = 16

world = bpy.data.worlds.new("Black")
world.use_nodes = True
world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
scene.world = world

bpy.ops.mesh.primitive_plane_add(size=2)
plane = bpy.context.active_object
plane.name = "Tiled"
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.subdivide(number_cuts=1)
bpy.ops.object.mode_set(mode="OBJECT")
uv = plane.data.uv_layers.active
for loop in plane.data.loops:
    x, y, _ = plane.data.vertices[loop.vertex_index].co
    uv.uv[loop.index].vector = (x + 1.0, y + 1.0)

mat = bpy.data.materials.new("Tiled")
nt = mat.node_tree
principled = nt.nodes["Principled BSDF"]
if shading == "noise":
    source = nt.nodes.new("ShaderNodeTexNoise")
else:
    image = bpy.data.images.new("Checker", 8, 8)
    image.generated_type = "COLOR_GRID"
    image.filepath_raw = texture
    image.file_format = "PNG"
    image.save()
    source = nt.nodes.new("ShaderNodeTexImage")
    source.image = image
nt.links.new(source.outputs["Color"], principled.inputs["Base Color"])
principled.inputs["Roughness"].default_value = 1.0
plane.data.materials.append(mat)

bpy.ops.mesh.primitive_cube_add(size=1, location=(-0.5, 0.0, 1.0))
bpy.context.active_object.scale = (1.0, 2.5, 0.05)
sun = bpy.data.lights.new("Sun", "SUN")
sun.energy = 5.0
sun.angle = 0.0
sun_object = bpy.data.objects.new("Sun", sun)
scene.collection.objects.link(sun_object)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


def _build(tmp_path: Path, shading: str) -> Path:
    script = tmp_path / "build.py"
    script.write_text(_BUILD)
    blend = tmp_path / "tiled.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script), "--",
         str(blend), str(tmp_path / "checker.png"), shading],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _bake(blend: Path, out: Path, mode: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json", "bake-export", str(blend), "-o", str(out),
         "--format", "USDA", "--bake-mode", mode, "--resolution", "64", "--image-format", "PNG"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _plane(stage):
    from pxr import UsdGeom

    return next(
        UsdGeom.Mesh(prim) for prim in stage.Traverse()
        if prim.IsA(UsdGeom.Mesh) and prim.GetName() != "Cube" and "Cube" not in str(prim.GetPath())
    )


def _primvar_names(mesh) -> set[str]:
    from pxr import UsdGeom

    return {p.GetPrimvarName() for p in UsdGeom.PrimvarsAPI(mesh).GetPrimvars()}


def _baked_texture(stage, export: Path) -> Path:
    from pxr import UsdShade

    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if shader and str(shader.GetIdAttr().Get()).startswith("ND_image_") and "Tiled" in str(prim.GetPath()):
            asset = shader.GetInput("file").Get()
            return (export.parent / asset.path).resolve()
    raise AssertionError("no baked image for the plane")


def test_lighting_bake_on_tiled_uvs_writes_into_a_generated_uv_map(tmp_path):
    import numpy as np
    from PIL import Image
    from pxr import Usd, UsdGeom, UsdShade

    blend = _build(tmp_path, "image")
    payload = _bake(blend, tmp_path / "out" / "tiled.usda", "LIT_IBL")
    assert any("usdstage_bake" in warning and "Tiled" in warning for warning in payload["warnings"]), payload

    export = Path(payload["export_path"])
    stage = Usd.Stage.Open(str(export))
    mesh = _plane(stage)
    assert {"st", "usdstage_bake"} <= _primvar_names(mesh)
    primvars = UsdGeom.PrimvarsAPI(mesh)
    # The plane's own UVs still tile, and are still what st holds.
    assert np.asarray(primvars.GetPrimvar("st").ComputeFlattened()).max() == pytest.approx(2.0)

    geomprops = [
        UsdShade.Shader(prim).GetInput("geomprop").Get()
        for prim in stage.Traverse()
        if UsdShade.Shader(prim) and UsdShade.Shader(prim).GetInput("geomprop")
    ]
    assert "usdstage_bake" in geomprops

    bake_uv = np.asarray(primvars.GetPrimvar("usdstage_bake").ComputeFlattened(), dtype=float)
    assert bake_uv.min() >= 0.0 and bake_uv.max() <= 1.0
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=float)
    counts = mesh.GetFaceVertexCountsAttr().Get()
    indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get())
    texture = np.asarray(Image.open(_baked_texture(stage, export)).convert("L"), dtype=float)
    size = texture.shape[0]

    shaded, lit = [], []
    corner = 0
    for count in counts:
        face = slice(corner, corner + count)
        corner += count
        centre_uv = bake_uv[face].mean(axis=0)
        x = points[indices[face]][:, 0].mean()
        # USD's v runs up; the PNG's rows run down.
        texel = texture[int((1.0 - centre_uv[1]) * (size - 1)), int(centre_uv[0] * (size - 1))]
        (shaded if x < 0 else lit).append(texel)
    assert max(shaded) < 0.5 * min(lit), (shaded, lit)


def test_colour_bake_of_a_tiling_image_keeps_the_meshs_own_uv_map(tmp_path):
    from pxr import Usd

    blend = _build(tmp_path, "image")
    payload = _bake(blend, tmp_path / "out" / "tiled.usda", "UNLIT_ALBEDO")
    assert not any("usdstage_bake" in warning for warning in payload["warnings"]), payload
    stage = Usd.Stage.Open(payload["export_path"])
    assert "usdstage_bake" not in _primvar_names(_plane(stage))


def test_colour_bake_of_a_procedural_texture_on_tiled_uvs_writes_into_a_generated_uv_map(tmp_path):
    from pxr import Usd

    blend = _build(tmp_path, "noise")
    payload = _bake(blend, tmp_path / "out" / "tiled.usda", "UNLIT_ALBEDO")
    assert any("usdstage_bake" in warning for warning in payload["warnings"]), payload
    stage = Usd.Stage.Open(payload["export_path"])
    assert "usdstage_bake" in _primvar_names(_plane(stage))
