"""`displayColor` must come from the attribute the material reads, on any slot.

Blender stores vertex colours under the attribute's own name and leaves USD's
conventional `displayColor` empty. RealityKit's `ND_geomcolor` resolves colour
set 0, which *is* `displayColor`, so the exporter copies the mesh's colour
attribute across. Two things about that copy were wrong:

- **Which attribute.** USD sorts primvars; Blender does not. Picking "the first
  colour primvar on the prim" meant a mesh carrying `Paint` (Blender-first, and
  the one the material reads) plus `Mask` published `Mask`, because M sorts
  before P. Measured: the cube rendered blue at 25% opacity instead of opaque
  green, with `ok: true` and no warning.

- **Which meshes.** Blender writes a multi-material mesh as one Mesh whose
  direct `material:binding` is slot 0, plus a GeomSubset per additional slot.
  Only the direct binding was inspected, so a vertex-colour material in any slot
  but the first published nothing at all and the object rendered unlit.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt  # noqa: E402

from Plugin.export.postprocess_usd import (  # noqa: E402
    _blender_first_color_attribute,
    _publish_vertex_colors_as_display_color,
)


def _mesh_with_color_attributes(stage, path, attributes, data_name=None):
    """A quad carrying one colour primvar per (name, rgba) pair."""
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(
        [Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(1, 1, 0), Gf.Vec3f(0, 1, 0)]
    )
    mesh.CreateFaceVertexCountsAttr([4])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    api = UsdGeom.PrimvarsAPI(mesh.GetPrim())
    for name, rgba in attributes:
        primvar = api.CreatePrimvar(
            name, Sdf.ValueTypeNames.Color4fArray, UsdGeom.Tokens.vertex
        )
        primvar.Set(Vt.Vec4fArray([Gf.Vec4f(*rgba)] * 4))
    if data_name is not None:
        mesh.GetPrim().CreateAttribute(
            "userProperties:blender:data_name", Sdf.ValueTypeNames.String, True
        ).Set(data_name)
    return mesh.GetPrim()


def _vertex_color_material(stage, path):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/reader")
    shader.CreateIdAttr("ND_geomcolor_color4")
    return material


def _plain_material(stage, path):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/surface")
    shader.CreateIdAttr("ND_realitykit_pbr_surfaceshader")
    return material


def _display(prim):
    api = UsdGeom.PrimvarsAPI(prim)
    color = api.GetPrimvar("displayColor")
    opacity = api.GetPrimvar("displayOpacity")
    return (
        list(color.Get()) if color and color.Get() else None,
        list(opacity.Get()) if opacity and opacity.Get() else None,
    )


class _FakeAttribute:
    def __init__(self, name):
        self.name = name


class _FakeMesh:
    def __init__(self, names):
        self.color_attributes = [_FakeAttribute(name) for name in names]


def test_source_is_the_blender_first_attribute_not_the_alphabetical_first(monkeypatch):
    """Mask sorts before Paint; Paint is what Blender and the material use."""
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(
        stage,
        "/Root/Cube",
        [("Mask", (0, 0, 1, 0.25)), ("Paint", (0, 1, 0, 1.0))],
        data_name="CubeData",
    )
    material = _vertex_color_material(stage, "/Root/Mat")
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)

    import types

    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            meshes={"CubeData": _FakeMesh(["Paint", "Mask"])}
        )
    )
    monkeypatch.setitem(__import__("sys").modules, "bpy", fake_bpy)

    _publish_vertex_colors_as_display_color(stage, None, context=object())

    color, opacity = _display(prim)
    assert color[0] == Gf.Vec3f(0, 1, 0), "published Mask's blue instead of Paint's green"
    assert opacity[0] == 1.0, "published Mask's 0.25 alpha instead of Paint's 1.0"


def test_publishes_when_the_material_is_bound_through_a_geomsubset():
    """Slot 0 is a plain material; the vertex-colour one is on a subset."""
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(stage, "/Root/Cube", [("Paint", (1, 0, 1, 1.0))])
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(_plain_material(stage, "/Root/Plain"))

    subset = UsdGeom.Subset.Define(stage, "/Root/Cube/VCol")
    subset.CreateElementTypeAttr(UsdGeom.Tokens.face)
    subset.CreateIndicesAttr([0])
    UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(
        _vertex_color_material(stage, "/Root/VCol")
    )

    _publish_vertex_colors_as_display_color(stage, None)

    color, _ = _display(prim)
    assert color is not None, "a subset-bound vertex-colour material published nothing"
    assert color[0] == Gf.Vec3f(1, 0, 1)


def test_a_mesh_no_bound_material_reads_is_left_alone():
    """Over-publishing would import an unrequested vertex-colour channel."""
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(stage, "/Root/Cube", [("Paint", (1, 0, 1, 1.0))])
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(_plain_material(stage, "/Root/Plain"))

    _publish_vertex_colors_as_display_color(stage, None)

    assert _display(prim) == (None, None)


def test_an_authored_display_color_is_never_overwritten():
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(stage, "/Root/Cube", [("Paint", (1, 0, 1, 1.0))])
    UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
        "displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.constant
    ).Set(Vt.Vec3fArray([Gf.Vec3f(0.25, 0.25, 0.25)]))
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(
        _vertex_color_material(stage, "/Root/Mat")
    )

    _publish_vertex_colors_as_display_color(stage, None)

    color, _ = _display(prim)
    assert color == [Gf.Vec3f(0.25, 0.25, 0.25)]


def test_ambiguity_is_reported_when_blenders_order_cannot_be_read():
    """Without Blender the positional fallback still publishes, but says so -
    silently guessing between two colour attributes is how this shipped wrong."""
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(
        stage, "/Root/Cube", [("Mask", (0, 0, 1, 1.0)), ("Paint", (0, 1, 0, 1.0))]
    )
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(
        _vertex_color_material(stage, "/Root/Mat")
    )

    class _Diagnostics:
        def __init__(self):
            self.warnings = []

        def add_warning(self, message):
            self.warnings.append(message)

        def add_info(self, message):
            pass

    diagnostics = _Diagnostics()
    _publish_vertex_colors_as_display_color(stage, diagnostics, context=None)

    assert diagnostics.warnings, "two candidates and no Blender order, but nothing said"
    assert "Mask" in diagnostics.warnings[0] and "Paint" in diagnostics.warnings[0]


def test_resolver_returns_none_without_context_or_identity():
    """The no-bpy path must stay safe: callers rely on the positional fallback."""
    stage = Usd.Stage.CreateInMemory()
    prim = _mesh_with_color_attributes(stage, "/Root/Cube", [("Paint", (1, 1, 1, 1))])
    assert _blender_first_color_attribute(prim, None) is None
    assert _blender_first_color_attribute(prim, object()) is None
