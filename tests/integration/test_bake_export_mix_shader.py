"""Integration tests — Mix Shader materials through the bake lane.

Reproduces the original hole: a material whose surface is a Mix Shader over two
Principled BSDFs used to be silently flat-collapsed to the FIRST Principled's
constants by the Material Color bake modes (a 50/50 red/blue mix exported as
flat red), while direct export died on a JSON-serialization crash instead of a
clean refusal.

Pinned behavior:
- LIT_IBL bakes the genuine blend (baked base color differs from both flat
  BSDF colors).
- LIT_ALBEDO bakes divergent roughness as a real, spatially-varying roughness
  texture (never collapses it to one side's constant).
- LIT_ALBEDO refuses divergent Metallic (a non-baked passthrough channel) with
  a message naming the property — and pointing at LIT_IBL.
- Direct export refuses cleanly, with advice naming the bake mode that works.
- A transparent mix whose two sides agree on Alpha bakes its opacity through
  the Mix Shader instead of failing after validation accepted it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pytest


pytestmark = pytest.mark.integration


_SCENE_BUILDER = '''
import sys
import bpy

OUT_DIR = sys.argv[sys.argv.index("--") + 1]


def build(name, *, fac_noise, m1=0.0, m2=0.0, r1=0.9, r2=0.1, alpha=1.0):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add(size=2)
    cube = bpy.context.active_object
    cube.name = "MixCube"

    mat = bpy.data.materials.new("MixShaderMat")
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    mix = nt.nodes.new("ShaderNodeMixShader")
    p1 = nt.nodes.new("ShaderNodeBsdfPrincipled")
    p2 = nt.nodes.new("ShaderNodeBsdfPrincipled")
    p1.inputs["Base Color"].default_value = (0.9, 0.05, 0.05, 1.0)  # red, rough
    p1.inputs["Roughness"].default_value = r1
    p1.inputs["Metallic"].default_value = m1
    p2.inputs["Base Color"].default_value = (0.05, 0.05, 0.9, 1.0)  # blue, glossy
    p2.inputs["Roughness"].default_value = r2
    p2.inputs["Metallic"].default_value = m2
    if alpha < 1.0:
        # The same Alpha on both sides: the one transparent blend validation
        # accepts, since the opacity bake has a single honest source.
        p1.inputs["Alpha"].default_value = alpha
        p2.inputs["Alpha"].default_value = alpha
        mat.surface_render_method = 'BLENDED'
    nt.links.new(p1.outputs["BSDF"], mix.inputs[1])
    nt.links.new(p2.outputs["BSDF"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    if fac_noise:
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 4.0
        nt.links.new(noise.outputs["Fac"], mix.inputs["Fac"])
    else:
        mix.inputs["Fac"].default_value = 0.5
    cube.data.materials.append(mat)

    world = bpy.data.worlds.new("World")
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
        bg.inputs["Strength"].default_value = 1.0
    bpy.context.scene.world = world
    bpy.ops.wm.save_as_mainfile(filepath=f"{OUT_DIR}/{name}.blend")


# 50/50 red/blue mix of two Principled BSDFs, constant factor.
build("mix_const", fac_noise=False)
# Divergent roughness (0.9 vs 0.1) with a noise-driven factor.
build("mix_noise_roughness", fac_noise=True)
# Divergent metallic (0 vs 1) with a noise-driven factor.
build("mix_noise_metallic", fac_noise=True, m1=0.0, m2=1.0, r1=0.5, r2=0.5)
# 50/50 mix whose two sides agree on a transparent Alpha of 0.4.
build("mix_transparent", fac_noise=False, alpha=0.4)
'''

# The two flat BSDF base colors as displayed sRGB, for "differs from either
# flat color" pixel assertions (linear 0.9 / 0.05 -> sRGB).
_FLAT_RED_SRGB = np.array([0.954, 0.250, 0.250])
_FLAT_BLUE_SRGB = np.array([0.250, 0.250, 0.954])


@pytest.fixture(scope="module")
def mix_scene_dir(tmp_path_factory) -> Path:
    """Build the Mix Shader .blend fixtures once per module with real Blender."""
    out_dir = tmp_path_factory.mktemp("mix_scenes")
    script = out_dir / "build_scenes.py"
    script.write_text(_SCENE_BUILDER)
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    proc = subprocess.run(
        [blender, "--background", "--python", str(script), "--", str(out_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (out_dir / "mix_const.blend").exists(), (
        f"Scene build failed:\n{proc.stdout}\n{proc.stderr}"
    )
    return out_dir


def _lit_pixels(image_path: Path) -> np.ndarray:
    """Baked texels as float RGB in [0,1], with the unbaked black margin dropped."""
    Image = pytest.importorskip("PIL.Image", reason="pillow required for pixel checks")
    im = np.asarray(Image.open(image_path).convert("RGB"), dtype=float) / 255.0
    px = im.reshape(-1, 3)
    lit = px[px.sum(axis=1) > 0.05]
    assert len(lit) > 0, f"Baked texture {image_path} is entirely black"
    return lit


def _find_texture(output_path: Path, suffix: str) -> Path:
    # Staged textures are content-addressed and nested per output
    # (textures/<output>/<id>/<name>_<suffix>-<sha256>.png).
    textures = sorted(
        candidate
        for candidate in (output_path.parent / "textures").rglob(f"*_{suffix}*")
        if candidate.is_file()
    )
    assert textures, f"No baked {suffix} texture next to {output_path}"
    return textures[0]


class TestMixShaderBakeExport:
    def test_lit_ibl_bakes_the_actual_blend(self, run_cli, mix_scene_dir, tmp_output):
        """Constant-0.5 mix through LIT_IBL: succeeds, and the baked base color
        is a genuine blend — not either BSDF's flat color."""
        out = tmp_output / "mix_ibl.usda"
        result = run_cli(
            "bake-export", str(mix_scene_dir / "mix_const.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "LIT_IBL",
            "--resolution", "256",
            "--image-format", "PNG",
            timeout=300,
        )
        assert result.ok, f"LIT_IBL bake-export refused a Mix Shader material: {result.stdout}\n{result.stderr}"
        actual = Path(result.json["export_path"])
        assert actual.exists()

        base_tex = _find_texture(actual, "baseColor")
        lit = _lit_pixels(base_tex)
        mean = lit.mean(axis=0)
        # A blend has substantial red AND blue; each flat color fails one side.
        assert mean[0] > 0.3 and mean[2] > 0.3, f"Baked mean {mean} lost one side of the mix"
        assert np.linalg.norm(mean - _FLAT_RED_SRGB) > 0.2, (
            f"Baked base color {mean} collapsed to the red BSDF's flat color"
        )
        assert np.linalg.norm(mean - _FLAT_BLUE_SRGB) > 0.2, (
            f"Baked base color {mean} collapsed to the blue BSDF's flat color"
        )

    def test_albedo_mode_bakes_blend_not_first_bsdf(self, run_cli, mix_scene_dir, tmp_output):
        """Regression: UNLIT_ALBEDO used to flat-collapse the mix to the first
        Principled's constants (flat red, no texture at all)."""
        out = tmp_output / "mix_unlit.usda"
        result = run_cli(
            "bake-export", str(mix_scene_dir / "mix_const.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "UNLIT_ALBEDO",
            "--resolution", "256",
            "--image-format", "PNG",
            timeout=300,
        )
        assert result.ok, f"UNLIT_ALBEDO bake-export failed: {result.stdout}\n{result.stderr}"
        actual = Path(result.json["export_path"])
        usda_text = actual.read_text()
        assert "diffuseColor = (0.9, 0.05, 0.05)" not in usda_text, (
            "Mix silently collapsed to the first BSDF's flat red"
        )
        lit = _lit_pixels(_find_texture(actual, "baseColor"))
        mean = lit.mean(axis=0)
        assert np.linalg.norm(mean - _FLAT_RED_SRGB) > 0.2
        assert np.linalg.norm(mean - _FLAT_BLUE_SRGB) > 0.2

    def test_lit_albedo_bakes_divergent_roughness_as_texture(self, run_cli, mix_scene_dir, tmp_output):
        """Divergent roughness (0.9 vs 0.1) with a varying factor: LIT_ALBEDO
        bakes a real, spatially-varying roughness texture via the ROUGHNESS
        pass — it does not refuse and does not collapse to one constant."""
        out = tmp_output / "mix_rough.usda"
        result = run_cli(
            "bake-export", str(mix_scene_dir / "mix_noise_roughness.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "LIT_ALBEDO",
            "--resolution", "256",
            "--image-format", "PNG",
            timeout=300,
        )
        assert result.ok, f"LIT_ALBEDO bake-export failed: {result.stdout}\n{result.stderr}"
        actual = Path(result.json["export_path"])
        assert "roughness.connect" in actual.read_text(), "Roughness not authored as a texture"
        rough = _lit_pixels(_find_texture(actual, "roughness"))
        spread = rough[:, 0].max() - rough[:, 0].min()
        assert spread > 0.05, (
            f"Roughness bake is flat (spread {spread:.3f}) — mixed 0.9/0.1 roughness was collapsed"
        )

    def test_lit_albedo_refuses_divergent_metallic_precisely(self, run_cli, mix_scene_dir, tmp_output):
        """Metallic is a non-baked passthrough: a 0-vs-1 divergence with a
        varying factor must refuse, naming the property and the working mode —
        never silently pick one BSDF's value."""
        out = tmp_output / "mix_metal.usda"
        result = run_cli(
            "--json",  # errors are only machine-readable in JSON mode
            "bake-export", str(mix_scene_dir / "mix_noise_metallic.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "LIT_ALBEDO",
            "--resolution", "256",
            timeout=300,
        )
        assert not result.ok, "Divergent-metallic mix must be refused in LIT_ALBEDO"
        message = (result.json or {}).get("error", {}).get("message", "")
        assert "Metallic" in message, f"Refusal does not name the diverging property: {message}"
        assert "diverge" in message
        assert "LIT_IBL" in message, f"Refusal does not point at the working bake mode: {message}"

    def test_lit_ibl_accepts_divergent_metallic(self, run_cli, mix_scene_dir, tmp_output):
        """The same divergent-metallic material bakes fine in LIT_IBL — the
        COMBINED pass renders the true mixed shading and the output is Unlit."""
        out = tmp_output / "mix_metal_ibl.usda"
        result = run_cli(
            "bake-export", str(mix_scene_dir / "mix_noise_metallic.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "LIT_IBL",
            "--resolution", "256",
            timeout=300,
        )
        assert result.ok, f"LIT_IBL refused a mix it can render: {result.stdout}\n{result.stderr}"

    def test_transparent_mix_with_agreed_alpha_bakes_opacity(self, run_cli, mix_scene_dir, tmp_output):
        """Validation accepts a transparent blend of two Principled BSDFs whose
        Alpha agrees; the opacity bake must then read that Alpha through the
        Mix Shader and merge it into the base color's alpha channel."""
        Image = pytest.importorskip("PIL.Image", reason="pillow required for pixel checks")
        out = tmp_output / "mix_transparent.usda"
        result = run_cli(
            "--json",
            "bake-export", str(mix_scene_dir / "mix_transparent.blend"), "-o", str(out),
            "--format", "USDA",
            "--bake-mode", "UNLIT_ALBEDO",
            "--resolution", "256",
            "--image-format", "PNG",
            timeout=300,
        )
        assert result.ok, (
            "a transparent mix validation accepted failed to bake: "
            f"{result.stdout}\n{result.stderr}"
        )
        actual = Path(result.json["export_path"])
        rgba = np.asarray(
            Image.open(_find_texture(actual, "baseColor")).convert("RGBA"), dtype=float
        ) / 255.0
        alpha = rgba.reshape(-1, 4)[:, 3]
        covered = alpha[alpha > 0.05]
        assert len(covered) > 0, "baked base color carries no opacity"
        assert np.median(covered) == pytest.approx(0.4, abs=0.02), (
            f"baked opacity {np.median(covered):.3f} is not the agreed Alpha 0.4"
        )

    def test_direct_export_blends_the_two_shaders(self, run_cli, mix_scene_dir, tmp_output):
        """A Mix Shader of two Principled BSDFs exports directly as one blended
        surface; the bake profiles above still accept the same material."""
        out = tmp_output / "mix_direct.usda"
        result = run_cli(
            "--json",
            "export", str(mix_scene_dir / "mix_const.blend"), "-o", str(out),
            "--format", "USDA",
            timeout=300,
        )
        assert result.ok, result.stdout + result.stderr
        assert "ND_realitykit_pbr_surfaceshader_2_0" in out.read_text()
