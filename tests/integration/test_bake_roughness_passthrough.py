"""Integration test - a roughness passthrough slot bakes no roughness image.

``LIT_ALBEDO`` carries a transparent material's roughness through from the
source (``_source_roughness_passthrough``), because Cycles' ROUGHNESS pass reads
0 on an alpha-blended surface. The ROUGHNESS pass still runs for the object
when another slot on it needs a baked roughness texture. The passthrough slot
must then get no saved roughness image - nothing wires it in - and the pass
must not write into that slot's base color either.

The scene is one cube with two slots: an opaque material whose roughness is
baked, and a transparent one whose constant roughness passes through.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
bpy.ops.mesh.primitive_cube_add(size=2)
cube = bpy.context.active_object
cube.name = "TwoSlotCube"


def material(name, rgb, roughness, alpha):
    mat = bpy.data.materials.new(name)
    nt = mat.node_tree
    principled = nt.nodes["Principled BSDF"]
    # A linked Base Color keeps the slot out of the flat-material shortcut,
    # so both slots genuinely bake.
    color = nt.nodes.new("ShaderNodeRGB")
    color.outputs["Color"].default_value = (*rgb, 1.0)
    nt.links.new(color.outputs["Color"], principled.inputs["Base Color"])
    principled.inputs["Roughness"].default_value = roughness
    principled.inputs["Alpha"].default_value = alpha
    if alpha < 1.0:
        mat.surface_render_method = 'BLENDED'
    return mat


cube.data.materials.append(material("Opaque", (0.1, 0.6, 0.1), 0.7, 1.0))
cube.data.materials.append(material("Glass", (0.8, 0.1, 0.1), 0.3, 0.4))
for index, polygon in enumerate(cube.data.polygons):
    polygon.material_index = index % 2

bpy.ops.wm.save_as_mainfile(filepath=out)
'''

# The Glass slot's linear base color is (0.8, 0.1, 0.1): red eight times green,
# green equal to blue. A roughness value written into that image is grey.
_GLASS_RED_OVER_GREEN = 8.0


@pytest.fixture(scope="module")
def texture_mode_bake(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("roughness_passthrough")
    script = workdir / "build.py"
    script.write_text(_BUILD)
    blend = workdir / "two_slots.blend"
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(blend)],
        capture_output=True, text=True, timeout=300,
    )
    assert blend.exists(), built.stdout + built.stderr

    out = workdir / "out" / "two_slots.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "bake-export", str(blend), "-o", str(out),
         "--format", "USDA", "--bake-mode", "LIT_ALBEDO", "--resolution", "64",
         "--image-format", "PNG", "--diagnostics"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    diagnostics = json.loads(Path(payload["diagnostics_path"]).read_text())
    return payload, diagnostics, Path(payload["export_path"])


def _generated(diagnostics, role):
    return [
        entry for entry in diagnostics["generated_files"] if entry.get("role") == role
    ]


def test_passthrough_slot_saves_no_roughness_image(texture_mode_bake):
    _payload, diagnostics, _out = texture_mode_bake

    roughness = _generated(diagnostics, "baked_roughness")
    assert sorted(entry["material"] for entry in roughness) == ["Opaque"], (
        f"roughness images saved: {roughness}"
    )
    base = _generated(diagnostics, "baked_base_color")
    assert sorted(entry["material"] for entry in base) == ["Glass", "Opaque"]


def test_passthrough_slot_base_color_survives_the_roughness_pass(texture_mode_bake):
    """Without a target of its own, the ROUGHNESS pass would write the slot's
    roughness into whichever image node is active - its base color."""
    _payload, _diagnostics, out = texture_mode_bake
    Image = pytest.importorskip("PIL.Image", reason="pillow required for pixel checks")

    textures = sorted(
        path for path in (out.parent / "textures").rglob("*Glass*_baseColor*.png")
    )
    assert textures, "no Glass base color texture published"
    rgba = np.asarray(Image.open(textures[0]).convert("RGBA"), dtype=float) / 255.0
    texels = rgba.reshape(-1, 4)
    covered = texels[texels[:, 3] > 0.05]
    assert len(covered) > 0, "Glass base color has no covered texels"
    srgb = np.median(covered[:, :3], axis=0)
    linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    assert linear[0] / linear[1] == pytest.approx(_GLASS_RED_OVER_GREEN, rel=0.15), (
        f"Glass base color {srgb} (sRGB) lost its source hue"
    )
    assert linear[1] == pytest.approx(linear[2], rel=0.05), srgb
    assert np.median(covered[:, 3]) == pytest.approx(0.4, abs=0.02)


def test_passthrough_roughness_is_authored_from_the_source(texture_mode_bake):
    _payload, _diagnostics, out = texture_mode_bake
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(out))
    values = {}
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Material):
            continue
        for descendant in Usd.PrimRange(prim):
            shader = UsdShade.Shader(descendant)
            if not shader:
                continue
            roughness = shader.GetInput("roughness")
            if roughness and not roughness.HasConnectedSource():
                value = roughness.Get()
                if value is not None:
                    values.setdefault(prim.GetName(), []).append(float(value))
    glass = [value for name, found in values.items() if "Glass" in name for value in found]
    assert glass and all(value == pytest.approx(0.3, abs=1e-4) for value in glass), values
