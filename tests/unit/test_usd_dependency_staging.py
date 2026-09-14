"""Dependency-closure tests for USD asset staging."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export import postprocess_usd, usd_assets
from Plugin.export.diagnostics import ExportDiagnostics


def _asset_generation(tmp_path: Path, output_name: str = "scene.usda"):
    output_namespace = tmp_path / "assets" / output_name
    generations = [path for path in output_namespace.iterdir() if path.is_dir()]
    assert len(generations) == 1
    generation = generations[0]
    relative = Path("assets") / output_name / generation.name
    return generation, relative.as_posix()


def test_nested_sublayers_are_copied_and_rewritten(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    leaf = nested / "leaf.usda"
    leaf.write_text('#usda 1.0\ndef Xform "Leaf" {}\n')
    sublayer = source / "sub.usda"
    sublayer.write_text(
        '#usda 1.0\n( subLayers = [@nested/leaf.usda@] )\n'
        'def Xform "Sub" {}\n'
    )
    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\n( subLayers = [@source/sub.usda@] )\n'
        'def Xform "Root" {}\n'
    )
    stage = Usd.Stage.Open(str(root))

    usd_assets.prepare_assets(stage, str(root))
    stage.Save()

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    assert (asset_namespace / "sub.usda").is_file()
    assert (asset_namespace / "leaf.usda").is_file()
    assert f"@{relative_namespace}/sub.usda@" in root.read_text()
    assert "@leaf.usda@" in (asset_namespace / "sub.usda").read_text()
    # The staged sublayer now resolves next to its copied leaf layer. Opening
    # in a fresh USD process is covered by strict package validation; avoid
    # relying on this process's Sdf layer cache here.


def test_asset_arrays_and_time_samples_are_staged(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    source = tmp_path / "source"
    source.mkdir()
    first = source / "first.bin"
    second = source / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    root = tmp_path / "scene.usda"
    root.write_text(
        """#usda 1.0
def Xform "Root" {
    asset[] attachments = [@source/first.bin@, @source/second.bin@]
    asset animated.timeSamples = {
        1: @source/first.bin@,
        2: @source/second.bin@,
    }
}
"""
    )
    stage = Usd.Stage.Open(str(root))

    usd_assets.prepare_assets(stage, str(root))
    stage.Save()

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    assert (asset_namespace / "first.bin").read_bytes() == b"first"
    assert (asset_namespace / "second.bin").read_bytes() == b"second"
    stage = Usd.Stage.Open(str(root))
    attachments = stage.GetPrimAtPath("/Root").GetAttribute("attachments").Get()
    assert [item.path for item in attachments] == [
        f"{relative_namespace}/first.bin",
        f"{relative_namespace}/second.bin",
    ]
    animated = stage.GetPrimAtPath("/Root").GetAttribute("animated")
    assert animated.Get(1).path == f"{relative_namespace}/first.bin"
    assert animated.Get(2).path == f"{relative_namespace}/second.bin"


def test_missing_local_asset_is_fatal(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\ndef Xform "Root" {\n asset missing = @missing.bin@\n}\n'
    )
    stage = Usd.Stage.Open(str(root))

    with pytest.raises(RuntimeError, match="Asset dependency not found"):
        usd_assets.prepare_assets(stage, str(root))


def test_value_clip_asset_metadata_is_localized(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    clip = tmp_path / "walk.usda"
    manifest = tmp_path / "manifest.usda"
    clip_payload = tmp_path / "clip-payload.bin"
    clip_payload.write_bytes(b"nested clip dependency")
    clip.write_text(
        '#usda 1.0\ndef Xform "Root" {\n'
        '    asset payload = @clip-payload.bin@\n'
        '}\n'
    )
    manifest.write_text('#usda 1.0\ndef Xform "Root" {}\n')
    root = tmp_path / "scene.usda"
    root.write_text(
        """#usda 1.0
def Xform "Root" (
    clips = {
        dictionary default = {
            asset[] assetPaths = [@walk.usda@]
            asset manifestAssetPath = @manifest.usda@
            string primPath = "/Root"
        }
    }
) {}
"""
    )
    stage = Usd.Stage.Open(str(root))

    usd_assets.prepare_assets(stage, str(root))
    stage.Save()

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    clips = stage.GetPrimAtPath("/Root").GetMetadata("clips")["default"]
    assert [item.path for item in clips["assetPaths"]] == [
        f"{relative_namespace}/walk.usda"
    ]
    assert clips["manifestAssetPath"].path == f"{relative_namespace}/manifest.usda"
    assert (asset_namespace / "walk.usda").is_file()
    assert (asset_namespace / "manifest.usda").is_file()
    localized_payload = asset_namespace / "clip-payload.bin"
    assert localized_payload.read_bytes() == b"nested clip dependency"
    localized_clip = Usd.Stage.Open(str(asset_namespace / "walk.usda"))
    payload = localized_clip.GetPrimAtPath("/Root").GetAttribute("payload").Get()
    assert Path(payload.resolvedPath).resolve() == localized_payload.resolve()


def test_references_and_payloads_are_localized(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    source = tmp_path / "source"
    source.mkdir()
    model = source / "model.usda"
    model.write_text('#usda 1.0\ndef Xform "Model" {}\n')
    root = tmp_path / "scene.usda"
    root.write_text(
        """#usda 1.0
def Xform "Referenced" (
    references = @source/model.usda@</Model>
) {}
def Xform "Payloaded" (
    payload = @source/model.usda@</Model>
) {}
"""
    )
    stage = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)

    usd_assets.prepare_assets(stage, str(root))
    stage.Save()

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    text = root.read_text()
    assert text.count(f"@{relative_namespace}/model.usda@</Model>") == 2
    assert (asset_namespace / "model.usda").is_file()


def test_postprocess_never_mutates_external_layer_and_normalizes_local_copy(
    tmp_path, monkeypatch
):
    pytest.importorskip("pxr")
    from pxr import Usd

    source = tmp_path / "source"
    source.mkdir()
    blob = source / "blob.bin"
    blob.write_bytes(b"source-owned payload")
    sublayer = source / "sub.usda"
    sublayer.write_text(
        '#usda 1.0\ndef Xform "Røød" {\n'
        '    asset blob = @blob.bin@\n'
        '    asset[] attachments = [@blob.bin@]\n'
        '    asset animated.timeSamples = { 1: @blob.bin@ }\n'
        '}\n'
    , encoding="utf-8")
    source_bytes = sublayer.read_bytes()
    root = tmp_path / "scene.usda"
    root.write_text('#usda 1.0\n( subLayers = [@source/sub.usda@] )\n')

    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )
    monkeypatch.setattr(
        postprocess_usd, "_require_realitykit_preflight", lambda *_args: None
    )

    postprocess_usd.process_usd_stage(
        str(root),
        SimpleNamespace(
            root_prim_name="Scene",
            convert_orientation=False,
            allow_unicode=False,
        ),
        context=None,
    )

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    localized = asset_namespace / "sub.usda"
    localized_blob = asset_namespace / "blob.bin"
    assert sublayer.read_bytes() == source_bytes
    assert localized_blob.read_bytes() == b"source-owned payload"
    assert 'def Xform "R__d"' in localized.read_text(encoding="utf-8")
    assert "Røød" not in localized.read_text(encoding="utf-8")
    assert f"@{relative_namespace}/sub.usda@" in root.read_text()

    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    prim = reopened.GetPrimAtPath("/R__d")
    assert prim
    value = prim.GetAttribute("blob").Get()
    assert value.path == "blob.bin"
    assert Path(value.resolvedPath).read_bytes() == b"source-owned payload"
    attachments = prim.GetAttribute("attachments").Get()
    assert [item.path for item in attachments] == ["blob.bin"]
    animated = prim.GetAttribute("animated").Get(1)
    assert animated.path == "blob.bin"
    assert Path(animated.resolvedPath).read_bytes() == b"source-owned payload"


def test_postprocess_normalizes_double_sided_only_in_localized_layer_copy(
    tmp_path, monkeypatch
):
    pytest.importorskip("pxr")
    from pxr import Usd, UsdGeom

    source = tmp_path / "source"
    source.mkdir()
    sublayer = source / "mesh.usda"
    sublayer.write_text(
        """#usda 1.0
def Mesh "ExternalMesh" {
    uniform bool doubleSided = true
    point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    int[] faceVertexCounts = [3]
    int[] faceVertexIndices = [0, 1, 2]
    uniform token subdivisionScheme = "none"
}
"""
    )
    source_bytes = sublayer.read_bytes()
    root = tmp_path / "scene.usda"
    root.write_text(
        """#usda 1.0
(
    defaultPrim = "ExternalMesh"
    metersPerUnit = 1
    upAxis = "Y"
    subLayers = [@source/mesh.usda@]
)
"""
    )

    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )
    diagnostics = ExportDiagnostics()
    postprocess_usd.process_usd_stage(
        str(root),
        SimpleNamespace(
            root_prim_name="ExternalMesh",
            convert_orientation=False,
            allow_unicode=True,
            export_format="USDA",
            export_animation=False,
        ),
        context=None,
        diagnostics=diagnostics,
    )

    assert sublayer.read_bytes() == source_bytes
    localized = _asset_generation(tmp_path)[0] / "mesh.usda"
    assert localized.read_bytes() != source_bytes
    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    localized_mesh = UsdGeom.Mesh(reopened.GetPrimAtPath("/ExternalMesh"))
    assert localized_mesh.GetDoubleSidedAttr().Get() is False
    assert (
        reopened.GetRootLayer().GetObjectAtPath("/ExternalMesh.doubleSided")
        is None
    )
    warnings = [
        warning
        for warning in diagnostics.data.get("info", [])
        if "doubleSided=false" in warning
    ]
    assert len(warnings) == 1
    # USD spells the layer identifier with forward slashes on every platform.
    assert localized.as_posix() in warnings[0]


def test_packaged_unowned_double_sided_mesh_survives_for_strict_rejection(
    tmp_path, monkeypatch
):
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdUtils

    source = tmp_path / "source"
    source.mkdir()
    member = source / "layer.usda"
    member.write_text(
        """#usda 1.0
def Mesh "PackagedMesh" {
    uniform bool doubleSided = true
    point3f[] points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    int[] faceVertexCounts = [3]
    int[] faceVertexIndices = [0, 1, 2]
    uniform token subdivisionScheme = "none"
}
"""
    )
    package = source / "model.usdz"
    assert UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(member)), str(package))
    package_bytes = package.read_bytes()

    root = tmp_path / "scene.usda"
    root.write_text(
        """#usda 1.0
(
    defaultPrim = "PackagedMesh"
    metersPerUnit = 1
    upAxis = "Y"
    subLayers = [@source/model.usdz[layer.usda]@]
)
"""
    )
    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )

    with pytest.raises(RuntimeError, match="DOUBLE_SIDED_GEOMETRY"):
        postprocess_usd.process_usd_stage(
            str(root),
            SimpleNamespace(
                root_prim_name="PackagedMesh",
                convert_orientation=False,
                allow_unicode=True,
                export_format="USDA",
                export_animation=False,
            ),
            context=None,
        )

    assert package.read_bytes() == package_bytes
    localized_package = _asset_generation(tmp_path)[0] / "model.usdz"
    assert localized_package.read_bytes() == package_bytes
    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    assert reopened.GetPrimAtPath("/PackagedMesh").GetAttribute(
        "doubleSided"
    ).Get() is True
    assert reopened.GetRootLayer().GetObjectAtPath(
        "/PackagedMesh.doubleSided"
    ) is None


def test_two_pass_postprocess_keeps_geometry_only_sublayer_normalized(
    tmp_path, monkeypatch
):
    pytest.importorskip("pxr")
    from pxr import Usd

    source = tmp_path / "source"
    source.mkdir()
    sublayer = source / "sub.usda"
    sublayer.write_text('#usda 1.0\ndef Xform "Café" {}\n', encoding="utf-8")
    source_bytes = sublayer.read_bytes()
    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\n( defaultPrim = "Café"; '
        'subLayers = [@source/sub.usda@] )\n'
    , encoding="utf-8")

    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )
    monkeypatch.setattr(
        postprocess_usd, "_require_realitykit_preflight", lambda *_args: None
    )

    postprocess_usd.process_usd_stage(
        str(root),
        SimpleNamespace(
            root_prim_name="Scene",
            convert_orientation=False,
            allow_unicode=False,
        ),
        context=None,
    )

    assert sublayer.read_bytes() == source_bytes
    localized = _asset_generation(tmp_path)[0] / "sub.usda"
    assert 'def Xform "Caf_"' in localized.read_text(encoding="utf-8")
    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    assert reopened.GetDefaultPrim().GetPath().pathString == "/Caf_"
    assert reopened.GetPrimAtPath("/Caf_")


def test_package_relative_composition_arc_stages_outer_package(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd, UsdUtils

    source = tmp_path / "source"
    source.mkdir()
    member = source / "layer.usda"
    member.write_text('#usda 1.0\ndef Xform "Packaged" {}\n')
    package = source / "model.usdz"
    assert UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(member)), str(package))
    package_bytes = package.read_bytes()

    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\n( subLayers = [@source/model.usdz[layer.usda]@] )\n'
    )
    stage = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)

    usd_assets.prepare_assets(stage, str(root))
    stage.Save()

    asset_namespace, relative_namespace = _asset_generation(tmp_path)
    localized = asset_namespace / "model.usdz"
    assert localized.read_bytes() == package_bytes
    assert f"@{relative_namespace}/model.usdz[layer.usda]@" in root.read_text()
    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    assert reopened.GetPrimAtPath("/Packaged")


def test_asset_namespaces_keep_sequential_exports_independent(tmp_path):
    pytest.importorskip("pxr")
    from pxr import Usd

    for output_name, source_name, payload in (
        ("asset-a.usda", "source-a", b"asset A"),
        ("asset-b.usda", "source-b", b"asset B"),
    ):
        source = tmp_path / source_name
        source.mkdir()
        (source / "shared.bin").write_bytes(payload)
        root = tmp_path / output_name
        root.write_text(
            '#usda 1.0\ndef Xform "Root" {\n'
            f'    asset blob = @{source_name}/shared.bin@\n'
            '}\n'
        )
        stage = Usd.Stage.Open(str(root))
        usd_assets.prepare_assets(stage, str(root))
        stage.Save()

    first_namespace, first_relative = _asset_generation(tmp_path, "asset-a.usda")
    second_namespace, second_relative = _asset_generation(tmp_path, "asset-b.usda")
    first = first_namespace / "shared.bin"
    second = second_namespace / "shared.bin"
    assert first.read_bytes() == b"asset A"
    assert second.read_bytes() == b"asset B"
    assert f"@{first_relative}/shared.bin@" in (
        tmp_path / "asset-a.usda"
    ).read_text()
    assert f"@{second_relative}/shared.bin@" in (
        tmp_path / "asset-b.usda"
    ).read_text()


def test_asset_destination_names_are_case_and_unicode_collision_safe():
    used = {}

    first = usd_assets._unique_destination_name(Path("/one/Color.bin"), used)
    case_collision = usd_assets._unique_destination_name(Path("/two/color.bin"), used)
    composed = usd_assets._unique_destination_name(Path("/three/caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"), used)
    decomposed = usd_assets._unique_destination_name(Path("/four/cafe\N{COMBINING ACUTE ACCENT}.bin"), used)

    assert first == "Color.bin"
    assert case_collision != "color.bin"
    assert composed == "caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"
    assert decomposed != "cafe\N{COMBINING ACUTE ACCENT}.bin"
