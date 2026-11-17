"""Integration test — repeated export to one output path.

``_build_export_kwargs`` sets ``export_textures_mode='NEW'`` whenever any image
in the blend is packed or generated, so ``wm.usd_export`` copies those images to
``<staging>/textures/<basename>``. Texture staging replaces each with a
content-addressed copy under ``textures/<output>/<generation>/``; the flat
original used to be left behind, published un-namespaced, and claimed by the
ownership manifest. The next export to the same path then found a file it owned
whose bytes had changed and aborted with "Immutable sidecar collision has
different bytes" - permanently, until the user deleted it by hand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# Builds a scene whose only texture is a generated image, which is what forces
# Blender's exporter into export_textures_mode='NEW'.
_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
color = tuple(float(v) for v in sys.argv[sys.argv.index("--") + 2].split(","))

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add()
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.uv.smart_project()
bpy.ops.object.mode_set(mode='OBJECT')

material = bpy.data.materials.new("M")
image = bpy.data.images.new("packedtex", 8, 8)
image.generated_color = color
node = material.node_tree.nodes.new("ShaderNodeTexImage")
node.image = image
material.node_tree.links.new(
    node.outputs["Color"],
    material.node_tree.nodes["Principled BSDF"].inputs["Base Color"],
)
obj.data.materials.append(material)
bpy.ops.wm.save_as_mainfile(filepath=out)
'''


@pytest.fixture(scope="module")
def packed_blends(tmp_path_factory) -> tuple[Path, Path]:
    """Two .blend files identical but for their generated texture colour."""
    workdir = tmp_path_factory.mktemp("packed_blends")
    script = workdir / "build.py"
    script.write_text(_BUILD)

    made = []
    for name, color in (("red.blend", "1,0,0,1"), ("blue.blend", "0,0,1,1")):
        target = workdir / name
        proc = subprocess.run(
            [
                blender_executable(), "--background", "--factory-startup",
                "--python", str(script), "--", str(target), color,
            ],
            capture_output=True, text=True, timeout=300,
        )
        assert target.exists(), proc.stdout + proc.stderr
        made.append(target)
    return made[0], made[1]


def _export(blend: Path, output: Path, *overrides: str, format: str = "USDA"):
    import sys
    return subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "Plugin"),
            "export", str(blend), *overrides, "-o", str(output), "--format", format,
        ],
        capture_output=True, text=True, timeout=600,
    )


def test_reexport_after_texture_change_succeeds(packed_blends, tmp_path):
    """The regression: second export aborted on its own leftover sidecar."""
    red, blue = packed_blends
    output = tmp_path / "scene.usda"

    first = _export(red, output)
    assert first.returncode == 0, first.stdout + first.stderr

    second = _export(blue, output)
    assert second.returncode == 0, (
        "re-export after the texture bytes changed failed:\n"
        + second.stdout + second.stderr
    )

    third = _export(red, output)
    assert third.returncode == 0, third.stdout + third.stderr


def test_no_un_namespaced_texture_is_published(packed_blends, tmp_path):
    """Blender's flat textures/<basename> copy must not reach the output."""
    red, _ = packed_blends
    output = tmp_path / "scene.usda"

    result = _export(red, output)
    assert result.returncode == 0, result.stdout + result.stderr

    textures = tmp_path / "textures"
    flat = [entry for entry in textures.iterdir() if entry.is_file()]
    assert flat == [], (
        f"un-namespaced textures published: {[e.name for e in flat]}. These are "
        "claimed by the ownership manifest and break the next export."
    )


def _payload_arc(output: Path) -> str:
    from pxr import Sdf

    layer = Sdf.Layer.OpenAsAnonymous(str(output))
    assert layer.defaultPrim, "the interface has no default prim"
    root = layer.GetPrimAtPath(f"/{layer.defaultPrim}")
    items = root.payloadList.explicitItems
    assert len(items) == 1, f"expected one payload arc, found {items}"
    return items[0].assetPath


def _base_colour_texture(output: Path) -> Path:
    from pxr import Usd, UsdShade

    stage = Usd.Stage.Open(str(output), Usd.Stage.LoadAll)
    textures = [
        Path(attr.Get().resolvedPath)
        for prim in stage.Traverse()
        if prim.IsA(UsdShade.Shader)
        for attr in prim.GetAttributes()
        if str(attr.GetTypeName()) == "asset" and attr.Get() and attr.Get().resolvedPath
    ]
    assert len(textures) == 1, textures
    return textures[0]


@pytest.mark.parametrize("format", ["USDA", "USDC"])
def test_reexport_with_payload_geometry_replaces_the_payload(packed_blends, tmp_path, format):
    """The payload layer used to sit at one fixed path below ``assets/``.

    Publication never rewrites a file there in place, so the second export to
    an output aborted on its own payload from the first. Each export now
    writes its payload into its own sidecar generation.
    """
    red, blue = packed_blends
    output = tmp_path / f"scene.{format.lower()}"

    first = _export(red, output, "payload_geometry=true", format=format)
    assert first.returncode == 0, first.stdout + first.stderr
    first_arc = _payload_arc(output)
    first_texture = _base_colour_texture(output).read_bytes()

    second = _export(blue, output, "payload_geometry=true", format=format)
    assert second.returncode == 0, (
        "re-export with payload_geometry failed:\n" + second.stdout + second.stderr
    )
    second_arc = _payload_arc(output)

    # The interface points at a new payload, and the payload it composes is
    # the second export's: the texture it binds carries the new colour.
    assert second_arc != first_arc
    assert (tmp_path / second_arc).is_file()
    assert _base_colour_texture(output).read_bytes() != first_texture

    # Nothing from the first export is left: one payload on disk, the one in
    # use, and the ownership manifest claims exactly that one.
    payloads = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in (tmp_path / "assets").rglob(f"*_payload.{format.lower()}")
    )
    assert payloads == [second_arc.removeprefix("./")]
    assert not (tmp_path / first_arc).parent.exists()
    manifest = json.loads(
        (tmp_path / ".usdstage_sidecars" / f"{output.name}.json").read_text()
    )
    assert [e for e in manifest["sidecars"] if "_payload." in e] == payloads

    third = _export(red, output, "payload_geometry=true", format=format)
    assert third.returncode == 0, third.stdout + third.stderr
    assert _base_colour_texture(output).read_bytes() == first_texture
