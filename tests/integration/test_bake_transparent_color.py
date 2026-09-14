"""Integration test - a transparent material bakes straight base color.

The exported material reads ``baseColor`` from the baked PNG's RGB and
``opacity`` from its alpha, with no ``hasPremultipliedAlpha``: RealityKit
blends the straight color by opacity once. Cycles' color bakes weight a
Principled BSDF by its Alpha, so a raw bake of an Alpha 0.4 surface stores the
color already multiplied by 0.4. Measured on Blender 5.2 with a byte target and
linear base color (0.8, 0.1, 0.1): DIFFUSE/COLOR baked linear (0.7682, 0.0953,
0.0953) at Alpha 1 and (0.3095, 0.0382, 0.0382) at Alpha 0.4; COMBINED baked
(0.8148, 0.1329, 0.1329) and (0.3231, 0.0529, 0.0529). Left in the PNG, that
color is dimmed by opacity twice.

The scene is two planes side by side under a white World, one opaque and one
at Alpha 0.4, with the same base color. Neither plane shadows the other, so
the transparent plane's baked texels must match the opaque plane's.
"""

from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

import numpy as np
import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 16
scene.cycles.use_denoising = False
world = bpy.data.worlds.new("White")
world.node_tree.nodes["Background"].inputs["Color"].default_value = (1, 1, 1, 1)
scene.world = world


def plane(name, x, alpha):
    bpy.ops.mesh.primitive_plane_add(size=2, location=(x, 0, 0))
    obj = bpy.context.active_object
    obj.name = name
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    principled = nt.nodes["Principled BSDF"]
    # A linked Base Color keeps the slot out of the flat-material shortcut.
    color = nt.nodes.new("ShaderNodeRGB")
    color.outputs["Color"].default_value = (0.8, 0.1, 0.1, 1.0)
    nt.links.new(color.outputs["Color"], principled.inputs["Base Color"])
    principled.inputs["Alpha"].default_value = alpha
    if alpha < 1.0:
        mat.surface_render_method = 'BLENDED'
    obj.data.materials.append(mat)


plane("Opaque", -1.5, 1.0)
plane("Glass", 1.5, 0.4)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''

# Linear (0.8, 0.1, 0.1) as sRGB.
_SOURCE_SRGB = np.array([0.906, 0.349, 0.349])


@pytest.fixture(scope="module")
def two_planes(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("transparent_color")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "two_planes.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr
    return blend


def _bake(blend: Path, out: Path, *extra: str) -> Path:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "bake-export", str(blend), "-o", str(out),
         "--format", "USDA", "--resolution", "64", "--image-format", "PNG", *extra],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return Path(json.loads(result.stdout)["export_path"])


def _covered_texels(export: Path, material: str) -> np.ndarray:
    Image = pytest.importorskip("PIL.Image", reason="pillow required for pixel checks")
    textures = sorted((export.parent / "textures").rglob(f"*{material}*_baseColor*.png"))
    assert textures, f"no {material} base color published beside {export}"
    rgba = np.asarray(Image.open(textures[0]).convert("RGBA"), dtype=float) / 255.0
    texels = rgba.reshape(-1, 4)
    covered = texels[texels[:, 3] > 0.05]
    assert len(covered) > 0, f"{material} base color has no covered texels"
    return covered


def _assert_straight_color(export: Path, *, source=None) -> None:
    usda = export.read_text()
    assert "hasPremultipliedAlpha" not in usda, "the graph declares premultiplied color"

    opaque = _covered_texels(export, "Opaque")
    glass = _covered_texels(export, "Glass")
    opaque_rgb = np.median(opaque[:, :3], axis=0)
    glass_rgb = np.median(glass[:, :3], axis=0)

    assert np.allclose(glass_rgb, opaque_rgb, atol=0.02), (
        f"Alpha 0.4 baked sRGB {glass_rgb}; the same color opaque baked {opaque_rgb}"
    )
    if source is not None:
        assert np.allclose(glass_rgb, source, atol=0.04), (
            f"Alpha 0.4 baked sRGB {glass_rgb}; source color is {source}"
        )
    assert np.median(glass[:, 3]) == pytest.approx(0.4, abs=0.02)


def test_material_color_bake_stores_straight_color(two_planes, tmp_path):
    export = _bake(two_planes, tmp_path / "unlit.usda", "--bake-mode", "UNLIT_ALBEDO")
    _assert_straight_color(export, source=_SOURCE_SRGB)


def test_material_color_bake_without_opacity_pass_stores_straight_color(two_planes, tmp_path):
    """Without the opacity pass, alpha comes from the color bake's own alpha."""
    export = _bake(
        two_planes, tmp_path / "unlit_no_opacity.usda",
        "--bake-mode", "UNLIT_ALBEDO", "--no-opacity",
    )
    _assert_straight_color(export, source=_SOURCE_SRGB)


def test_lighting_bake_stores_straight_lit_color(two_planes, tmp_path):
    export = _bake(
        two_planes, tmp_path / "lit_ibl.usda",
        "--bake-mode", "LIT_IBL", "--ibl-source", "SCENE_WORLD",
    )
    _assert_straight_color(export)
