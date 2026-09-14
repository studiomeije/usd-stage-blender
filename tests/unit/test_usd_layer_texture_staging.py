"""Regression tests for layer-preserving USD texture localization."""

from __future__ import annotations

import binascii
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("pxr")
from pxr import Sdf, Usd  # noqa: E402

from Plugin.export import postprocess_usd, usd_textures  # noqa: E402


def _settings():
    return SimpleNamespace(
        root_prim_name="Scene",
        convert_orientation=False,
        up_axis="Y",
        allow_unicode=True,
        export_texture_settings_enabled=False,
    )


def _write_png(path: Path, rgb: tuple[int, int, int]) -> None:
    """Write a dependency-free, valid one-pixel RGB PNG."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(
            ">I", binascii.crc32(body) & 0xFFFFFFFF
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk("IHDR".encode(), struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk("IDAT".encode(), zlib.compress(b"\x00" + bytes(rgb)))
        + chunk("IEND".encode(), b"")
    )


def _run_postprocess(root: Path, monkeypatch) -> None:
    monkeypatch.setattr(postprocess_usd, "rewrite_materials", lambda *_args: None)
    monkeypatch.setattr(
        postprocess_usd, "author_animation_library", lambda *_args: None
    )
    monkeypatch.setattr(
        postprocess_usd, "_require_realitykit_preflight", lambda *_args: None
    )
    postprocess_usd.process_usd_stage(
        str(root),
        _settings(),
        context=None,
    )


def _single_generation(root: Path, kind: str) -> Path:
    namespace = root.parent / kind / root.name
    generations = [path for path in namespace.iterdir() if path.is_dir()]
    assert len(generations) == 1
    return generations[0]


def _authored_asset_specs(layer) -> dict[str, Sdf.AssetPath]:
    paths = []
    layer.Traverse(Sdf.Path.absoluteRootPath, lambda path: paths.append(path))
    values = {}
    for path in paths:
        spec = layer.GetObjectAtPath(path)
        if not isinstance(spec, Sdf.AttributeSpec):
            continue
        if spec.typeName != Sdf.ValueTypeNames.Asset or spec.default is None:
            continue
        values[str(path)] = spec.default
    return values


def _resolved_asset(layer, value: Sdf.AssetPath) -> Path:
    return Path(layer.ComputeAbsolutePath(value.path)).resolve()


def test_inactive_variant_textures_are_localized_in_their_external_layer(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    red = source / "red.png"
    blue = source / "blue.png"
    _write_png(red, (255, 0, 0))
    _write_png(blue, (0, 0, 255))

    source_layer_path = source / "looks.usda"
    source_stage = Usd.Stage.CreateNew(str(source_layer_path))
    model = source_stage.DefinePrim("/Model", "Xform")
    look = model.GetVariantSets().AddVariantSet("look")
    for name, texture_name in (("red", red.name), ("blue", blue.name)):
        look.AddVariant(name)
        look.SetVariantSelection(name)
        with look.GetVariantEditContext():
            shader = source_stage.DefinePrim("/Model/Texture", "Shader")
            shader.CreateAttribute(
                "inputs:file", Sdf.ValueTypeNames.Asset
            ).Set(Sdf.AssetPath(texture_name))
    look.SetVariantSelection("red")
    source_stage.GetRootLayer().Save()
    source_bytes = source_layer_path.read_bytes()

    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\n( subLayers = [@source/looks.usda@] )\n',
        encoding="utf-8",
    )

    _run_postprocess(root, monkeypatch)

    asset_generation = _single_generation(root, "assets")
    texture_generation = _single_generation(root, "textures")
    assert asset_generation.name == texture_generation.name
    localized_path = asset_generation / source_layer_path.name
    localized_layer = Sdf.Layer.FindOrOpen(str(localized_path))
    assert localized_layer

    values = _authored_asset_specs(localized_layer)
    red_path = next(path for path in values if "{look=red}" in path)
    blue_path = next(path for path in values if "{look=blue}" in path)
    assert set(values) == {red_path, blue_path}

    localized_red = _resolved_asset(localized_layer, values[red_path])
    localized_blue = _resolved_asset(localized_layer, values[blue_path])
    assert localized_red.parent == texture_generation.resolve()
    assert localized_blue.parent == texture_generation.resolve()
    assert localized_red.read_bytes() == red.read_bytes()
    assert localized_blue.read_bytes() == blue.read_bytes()

    assert source_layer_path.read_bytes() == source_bytes
    root_layer = Sdf.Layer.FindOrOpen(str(root))
    assert root_layer.GetObjectAtPath(Sdf.Path("/Model/Texture.inputs:file")) is None
    assert "inputs:file" not in root.read_text(encoding="utf-8")

    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    reopened.SetEditTarget(reopened.GetSessionLayer())
    reopened_look = reopened.GetPrimAtPath("/Model").GetVariantSet("look")
    assert reopened_look.GetVariantSelection() == "red"
    assert reopened_look.SetVariantSelection("blue")
    blue_value = reopened.GetPrimAtPath("/Model/Texture").GetAttribute(
        "inputs:file"
    ).Get()
    assert Path(blue_value.resolvedPath).resolve() == localized_blue


def test_class_texture_survives_localization_and_instance_prototype_composition(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    class_texture = source / "class.png"
    _write_png(class_texture, (32, 180, 96))

    source_layer_path = source / "class-model.usda"
    source_stage = Usd.Stage.CreateNew(str(source_layer_path))
    source_stage.CreateClassPrim("/_Class")
    shader = source_stage.DefinePrim("/_Class/Texture", "Shader")
    shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath(class_texture.name)
    )
    instance = source_stage.DefinePrim("/Instance", "Xform")
    instance.GetReferences().AddInternalReference("/_Class")
    instance.SetInstanceable(True)
    source_stage.GetRootLayer().Save()
    source_bytes = source_layer_path.read_bytes()

    root = tmp_path / "scene.usda"
    root.write_text(
        '#usda 1.0\n( subLayers = [@source/class-model.usda@] )\n',
        encoding="utf-8",
    )

    _run_postprocess(root, monkeypatch)

    asset_generation = _single_generation(root, "assets")
    texture_generation = _single_generation(root, "textures")
    localized_path = asset_generation / source_layer_path.name
    localized_layer = Sdf.Layer.FindOrOpen(str(localized_path))
    assert localized_layer

    values = _authored_asset_specs(localized_layer)
    assert set(values) == {"/_Class/Texture.inputs:file"}
    localized_texture = _resolved_asset(
        localized_layer, values["/_Class/Texture.inputs:file"]
    )
    assert localized_texture.parent == texture_generation.resolve()
    assert localized_texture.read_bytes() == class_texture.read_bytes()
    assert localized_layer.GetPrimAtPath("/_Class").specifier == Sdf.SpecifierClass

    assert source_layer_path.read_bytes() == source_bytes
    root_layer = Sdf.Layer.FindOrOpen(str(root))
    assert root_layer.GetObjectAtPath(
        Sdf.Path("/_Class/Texture.inputs:file")
    ) is None

    reopened = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    reopened_instance = reopened.GetPrimAtPath("/Instance")
    assert reopened_instance.IsInstance()
    prototypes = reopened.GetPrototypes()
    assert len(prototypes) == 1
    prototype_texture = (
        prototypes[0].GetChild("Texture").GetAttribute("inputs:file").Get()
    )
    assert Path(prototype_texture.resolvedPath).resolve() == localized_texture


def test_texture_staging_state_canonicalizes_a_symlinked_export_path(tmp_path):
    real_export_dir = tmp_path / "real-export"
    real_export_dir.mkdir()
    alias_export_dir = tmp_path / "export-alias"
    alias_export_dir.symlink_to(real_export_dir, target_is_directory=True)
    real_root = real_export_dir / "scene.usda"
    real_root.write_text("#usda 1.0\n", encoding="utf-8")

    state = usd_textures.create_texture_staging_state(
        alias_export_dir / real_root.name,
        _settings(),
    )

    assert state.usd_path == real_root.resolve()
    assert state.usd_dir == real_export_dir.resolve()
    assert state.textures_dir == (
        real_export_dir.resolve() / "textures" / state.sidecar_namespace
    )
    assert state.textures_dir.relative_to(state.usd_dir).parts[:2] == (
        "textures",
        real_root.name,
    )
