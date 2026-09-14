"""Image Texture projections: Sphere and Tube refused, Flat kept on the UV path.

Every non-Flat projection used to fall through to the Flat path and export a
UV-sampled image in place of the projection. Sphere and Tube are refused with
bake advice. Box projection is transcribed from Cycles and pinned on values in
``test_texture_nodes_exact``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_bpy_stub = sys.modules.setdefault("bpy", types.ModuleType("bpy"))
if not hasattr(_bpy_stub, "types"):
    _bpy_stub.types = types.SimpleNamespace(NodeTree=object)

import pytest  # noqa: E402

from Plugin.export.materials.extract import core  # noqa: E402



class _Socket:
    def __init__(self, value=None, *, linked=False, link=None, name="Value"):
        self.default_value = value
        self.is_linked = linked
        self.links = [link] if link is not None else []
        self.name = name


class _Link:
    def __init__(self, node, socket):
        self.from_node = node
        self.from_socket = socket


class _Node:
    pass


class _Image:
    def __init__(self):
        self.filepath = "/assets/tex.png"
        self.filepath_raw = "/assets/tex.png"
        self.is_dirty = False
        self.source = "FILE"
        self.packed_file = None
        self.alpha_mode = "STRAIGHT"
        self.colorspace_settings = types.SimpleNamespace(name="sRGB")


def _image_node(projection="BOX", blend=0.4, vector_socket=None):
    node = _Node()
    node.type = "TEX_IMAGE"
    node.name = "Image Texture"
    node.projection = projection
    node.projection_blend = blend
    node.image = _Image()
    node.uv_map = ""
    node.extension = "REPEAT"
    node.interpolation = "Linear"
    node.inputs = {"Vector": vector_socket or _Socket(name="Vector")}
    return node


@pytest.fixture(autouse=True)
def _fake_image_path(monkeypatch):
    monkeypatch.setattr(
        core, "_resolve_image_path", lambda image: getattr(image, "filepath", None)
    )


def _resolve(node, output_name="Color", expected_type="color3"):
    output = _Socket(name=output_name)
    target = _Socket(linked=True, link=_Link(node, output))
    return core._resolve_socket_value(target, expected_type=expected_type)


def test_sphere_and_tube_projections_stay_refused_with_bake_advice():
    for projection in ("SPHERE", "TUBE"):
        expr = _resolve(_image_node(projection=projection))
        assert expr["kind"] == "unresolved", projection
        assert projection in expr["reason"]
        assert "bake" in expr["reason"].lower()


def test_flat_projection_keeps_the_existing_texture_path():
    expr = _resolve(_image_node(projection="FLAT"))
    assert expr["kind"] == "texture"
    assert expr["path"] == "/assets/tex.png"


def test_box_projection_is_no_longer_a_triplanar():
    # Box projection is transcribed from Cycles in test_texture_nodes_exact;
    # MaterialX triplanar samples object position with its own weights.
    expr = _resolve(_image_node())
    assert expr["kind"] == "node"
    assert "triplanarprojection" not in str(expr)
