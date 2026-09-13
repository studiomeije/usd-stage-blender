"""Release-contract tests for USD texture dependency staging."""

from __future__ import annotations

import hashlib
import sys
import unicodedata
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export import usd_assets, usd_textures


def test_bake_cleanup_removes_only_unreferenced_owned_source_textures(
    tmp_path,
    monkeypatch,
):
    from pxr import Sdf, Usd

    monkeypatch.setattr(usd_textures, "Sdf", Sdf)
    textures = tmp_path / "textures"
    staged = textures / "scene.usda" / ("a" * 32) / "staged.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"staged")
    superseded = textures / "Baked_baseColor.png"
    superseded.write_bytes(b"source")
    external = tmp_path.parent / "external-bake-source.png"
    external.write_bytes(b"external")
    usd_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(usd_path))
    shader = stage.DefinePrim("/Texture", "Shader")
    shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath(staged.relative_to(tmp_path).as_posix())
    )
    stage.GetRootLayer().Save()

    removed = usd_textures.remove_unreferenced_bake_outputs(
        usd_path,
        tmp_path,
        (superseded, staged, external),
    )

    assert removed == (superseded.resolve(),)
    assert not superseded.exists()
    assert staged.read_bytes() == b"staged"
    assert external.read_bytes() == b"external"


def _texture_generation_dir(usd_path: Path) -> Path:
    return usd_path.parent / "textures" / usd_textures.output_sidecar_namespace(usd_path)


def _texture_asset_path(usd_path: Path, name: str) -> str:
    return (
        Path("textures") / usd_textures.output_sidecar_namespace(usd_path) / name
    ).as_posix()


def _content_name(stem: str, contents: bytes, suffix: str) -> str:
    return f"{stem}-{hashlib.sha256(contents).hexdigest()}{suffix}"


def _stage_with_textures(usd_path: Path, *authored_paths: str):
    """A saved root layer with one Shader per authored texture path."""
    pytest.importorskip("pxr")
    from pxr import Sdf, Usd

    stage = Usd.Stage.CreateNew(str(usd_path))
    for index, authored in enumerate(authored_paths):
        shader = stage.DefinePrim(f"/Texture{index}", "Shader")
        shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
            Sdf.AssetPath(authored)
        )
    stage.GetRootLayer().Save()
    return stage


def _authored_texture(stage, index: int = 0) -> str:
    return stage.GetPrimAtPath(f"/Texture{index}").GetAttribute("inputs:file").Get().path


def _settings():
    return SimpleNamespace(export_texture_settings_enabled=False)


@pytest.mark.parametrize(
    (
        "source_name",
        "alpha_mode",
        "has_premultiplied_alpha",
        "texture_override",
        "should_reject",
    ),
    [
        (
            "albedo.png",
            "premul",
            True,
            {"file_format": "AVIF", "extension": ".avif", "resolution": 0},
            True,
        ),
        # An AVIF source is no longer Apple-safe (Reality Composer Pro 3 refuses
        # it), so ORIGINAL converts it to PNG and a resize takes the safe path.
        (
            "albedo.avif",
            "premul",
            True,
            {"file_format": "ORIGINAL", "extension": "", "resolution": 1024},
            False,
        ),
        ("albedo.avif", "premul", True, None, False),
        (
            "albedo.png",
            "premul",
            True,
            {"file_format": "PNG", "extension": ".png", "resolution": 1024},
            False,
        ),
        (
            "albedo.png",
            "straight",
            False,
            {"file_format": "AVIF", "extension": ".avif", "resolution": 0},
            False,
        ),
    ],
)
def test_premultiplied_alpha_policy_matches_effective_texture_staging(
    tmp_path,
    monkeypatch,
    source_name,
    alpha_mode,
    has_premultiplied_alpha,
    texture_override,
    should_reject,
):
    source = tmp_path / source_name
    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args, **_kwargs: texture_override,
    )

    def validate():
        usd_textures.require_safe_texture_alpha_staging_policy(
            source,
            alpha_mode=alpha_mode,
            has_premultiplied_alpha=has_premultiplied_alpha,
            settings=SimpleNamespace(),
        )

    if should_reject:
        with pytest.raises(
            RuntimeError,
            match=r"Select PNG.*disable the unsafe AVIF/resolution override",
        ):
            validate()
    else:
        validate()


def test_real_usd_sublayer_texture_resolves_against_its_authored_layer(tmp_path, monkeypatch):
    from pxr import Sdf, Usd

    monkeypatch.setattr(usd_textures, "Sdf", Sdf)
    export_dir = tmp_path / "export"
    library_dir = tmp_path / "library"
    texture = library_dir / "maps" / "coat.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"real composed texture")

    look_layer = library_dir / "look.usda"
    look_layer.write_text(
        '#usda 1.0\n\n'
        'def Shader "Texture"\n'
        '{\n'
        '    asset inputs:file = @maps/coat.png@\n'
        '}\n'
    )
    export_dir.mkdir()
    usd_path = export_dir / "scene.usda"
    usd_path.write_text(
        '#usda 1.0\n'
        '(\n'
        '    subLayers = [@../library/look.usda@]\n'
        ')\n'
    )
    stage = Usd.Stage.Open(str(usd_path), Usd.Stage.LoadAll)
    attr = stage.GetPrimAtPath("/Texture").GetAttribute("inputs:file")
    assert Path(attr.Get().resolvedPath) == texture.resolve()

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    name = _content_name("scene-coat", b"real composed texture", ".png")
    staged_path = _texture_generation_dir(usd_path) / name
    assert staged_path.read_bytes() == b"real composed texture"
    # Localizing the sublayer recomposes the stage, so refetch the attribute
    # instead of retaining an expired composed prim handle.
    staged_attr = stage.GetPrimAtPath("/Texture").GetAttribute("inputs:file")
    staged_value = staged_attr.Get()
    assert staged_value.path.endswith(name)
    assert Path(staged_value.resolvedPath) == staged_path.resolve()
    assert "@maps/coat.png@" in look_layer.read_text()


def test_file_url_is_normalized_before_copying(tmp_path):
    source = tmp_path / "source library" / "detail.png"
    source.parent.mkdir()
    source.write_bytes(b"file url texture")
    usd_path = tmp_path / "export" / "scene.usda"
    usd_path.parent.mkdir()
    stage = _stage_with_textures(usd_path, source.as_uri())

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    name = _content_name("scene-detail", b"file url texture", ".png")
    assert (_texture_generation_dir(usd_path) / name).read_bytes() == b"file url texture"
    assert _authored_texture(stage) == _texture_asset_path(usd_path, name)


def test_remote_texture_fails_self_contained_export(tmp_path):
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, "https://assets.example.invalid/material/albedo.png")
    authored = _authored_texture(stage)
    diagnostics = SimpleNamespace(
        failures=[],
        add_texture_failed=lambda path, reason: diagnostics.failures.append((path, reason)),
    )

    with pytest.raises(RuntimeError, match="self-contained export"):
        usd_assets.prepare_assets(stage, str(usd_path), diagnostics, settings=_settings())

    assert diagnostics.failures
    assert _authored_texture(stage) == authored


@pytest.mark.parametrize("create_empty", [False, True])
def test_missing_or_empty_texture_fails_before_authoring_package_path(tmp_path, create_empty):
    source = tmp_path / "source.png"
    if create_empty:
        source.touch()
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    authored = _authored_texture(stage)

    with pytest.raises(RuntimeError, match="not found|empty"):
        usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert _authored_texture(stage) == authored


def test_destination_names_are_collision_safe_under_nfc_and_casefold(tmp_path):
    source_a = tmp_path / "a" / "ÉCLAIR.png"
    source_b = tmp_path / "b" / unicodedata.normalize("NFD", "éclair.png")
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    source_a.write_bytes(b"first")
    source_b.write_bytes(b"second")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source_a), str(source_b))

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    names = [path.name for path in _texture_generation_dir(usd_path).iterdir()]
    keys = [unicodedata.normalize("NFC", name).casefold() for name in names]
    assert len(names) == 2
    assert len(set(keys)) == 2
    assert _authored_texture(stage, 0) != _authored_texture(stage, 1)


def test_unsupported_input_format_is_forced_to_native_size_png(tmp_path, monkeypatch):
    source = tmp_path / "surface.webp"
    source.write_bytes(b"webp source")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    conversions = []

    def convert(source_path, dest_path, texture_override, diagnostics=None):
        conversions.append((source_path, dest_path, dict(texture_override)))
        dest_path.write_bytes(b"nonempty png")
        return True

    monkeypatch.setattr(usd_textures, "_convert_texture", convert)
    monkeypatch.setattr(usd_textures, "_validate_transcoded_texture", lambda *_args: True)

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert conversions[0][2] == {"file_format": "PNG", "extension": ".png", "resolution": 0}
    name = _content_name("scene-surface", b"nonempty png", ".png")
    assert _authored_texture(stage) == _texture_asset_path(usd_path, name)
    assert not (_texture_generation_dir(usd_path) / "scene-surface.webp").exists()


def test_exr_is_preserved_byte_for_byte_even_with_texture_override(tmp_path, monkeypatch):
    source = tmp_path / "lighting.exr"
    source.write_bytes(b"float-openexr-payload")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))

    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args: {"file_format": "PNG", "extension": ".png", "resolution": 1024},
    )
    monkeypatch.setattr(
        usd_textures,
        "_convert_texture",
        lambda *_args, **_kwargs: pytest.fail("EXR must not be transcoded"),
    )

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    name = _content_name("scene-lighting", source.read_bytes(), ".exr")
    staged = _texture_generation_dir(usd_path) / name
    assert staged.read_bytes() == source.read_bytes()
    assert _authored_texture(stage) == _texture_asset_path(usd_path, name)


def test_radiance_hdr_fails_closed_instead_of_losing_float_fidelity(tmp_path, monkeypatch):
    source = tmp_path / "lighting.hdr"
    source.write_bytes(b"radiance-hdr-payload")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    authored = _authored_texture(stage)
    monkeypatch.setattr(
        usd_textures,
        "_convert_texture",
        lambda *_args, **_kwargs: pytest.fail("HDR must fail before PNG conversion"),
    )

    with pytest.raises(RuntimeError, match="convert it to OpenEXR"):
        usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert _authored_texture(stage) == authored


def test_failed_same_path_conversion_never_deletes_or_truncates_source(tmp_path, monkeypatch):
    textures = tmp_path / "textures"
    textures.mkdir()
    source = textures / "scene-surface.png"
    source.write_bytes(b"authoritative-source")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, "textures/scene-surface.png")

    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args: {"file_format": "PNG", "extension": ".png", "resolution": 512},
    )

    def fail_after_partial_write(_source_path, temporary_path, _override, diagnostics=None):
        assert temporary_path != source
        temporary_path.write_bytes(b"partial-output")
        return False

    monkeypatch.setattr(usd_textures, "_convert_texture", fail_after_partial_write)

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert source.read_bytes() == b"authoritative-source"
    name = _content_name("scene-surface", b"authoritative-source", ".png")
    assert _authored_texture(stage) == _texture_asset_path(usd_path, name)
    assert not list(_texture_generation_dir(usd_path).glob(".*.convert-*.png"))


def test_avif_failure_falls_back_to_native_size_png(tmp_path, monkeypatch):
    source = tmp_path / "surface.png"
    source.write_bytes(b"png source")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    conversions = []

    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args: {"file_format": "AVIF", "extension": ".avif", "resolution": 0},
    )

    def convert(_source_path, dest_path, texture_override, diagnostics=None):
        conversions.append(dict(texture_override))
        if texture_override["file_format"] == "AVIF":
            return False
        dest_path.write_bytes(b"native-size png")
        return True

    monkeypatch.setattr(usd_textures, "_convert_texture", convert)
    monkeypatch.setattr(usd_textures, "_validate_transcoded_texture", lambda *_args: True)

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert conversions == [
        {"file_format": "AVIF", "extension": ".avif", "resolution": 0},
        {"file_format": "PNG", "extension": ".png", "resolution": 0},
    ]
    name = _content_name("scene-surface", b"native-size png", ".png")
    assert _authored_texture(stage) == _texture_asset_path(usd_path, name)


def test_empty_transcode_of_unsupported_format_fails_closed(tmp_path, monkeypatch):
    source = tmp_path / "surface.tga"
    source.write_bytes(b"tga source")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    authored = _authored_texture(stage)

    def write_empty(_source_path, dest_path, _texture_override, diagnostics=None):
        dest_path.write_bytes(b"")
        return True

    monkeypatch.setattr(usd_textures, "_convert_texture", write_empty)

    with pytest.raises(RuntimeError, match="could not be transcoded to PNG"):
        usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert _authored_texture(stage) == authored
    assert not (_texture_generation_dir(usd_path) / "scene-surface.png").exists()


def test_content_addressed_leaf_is_deterministic_and_changes_with_bytes(tmp_path):
    leaves = []
    for directory, contents in (
        ("first", b"identical texture"),
        ("second", b"identical texture"),
        ("changed", b"changed texture"),
    ):
        export_dir = tmp_path / directory
        export_dir.mkdir()
        source = export_dir / "source" / "albedo.png"
        source.parent.mkdir()
        source.write_bytes(contents)
        usd_path = export_dir / "scene.usda"
        stage = _stage_with_textures(usd_path, str(source))
        usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())
        leaves.append(Path(_authored_texture(stage)).name)

    assert leaves[0] == leaves[1]
    assert leaves[0] != leaves[2]
    assert leaves[0] == _content_name(
        "scene-albedo",
        b"identical texture",
        ".png",
    )


def test_two_pass_lossy_override_reuses_hash_verified_current_generation(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "albedo.png"
    source.write_bytes(b"original pixels")
    usd_path = tmp_path / "scene.usda"
    stage = _stage_with_textures(usd_path, str(source))
    conversions = []

    monkeypatch.setattr(
        usd_textures,
        "_texture_override_settings",
        lambda *_args: {
            "file_format": "JPEG",
            "extension": ".jpg",
            "resolution": 1024,
        },
    )

    def encode(source_path, dest_path, _override, diagnostics=None):
        conversions.append(source_path.read_bytes())
        dest_path.write_bytes(source_path.read_bytes() + b"|jpeg-encode")
        return True

    monkeypatch.setattr(usd_textures, "_convert_texture", encode)
    monkeypatch.setattr(
        usd_textures,
        "_validate_transcoded_texture",
        lambda *_args: True,
    )

    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())
    first_path = _authored_texture(stage)
    first_bytes = (tmp_path / first_path).read_bytes()
    usd_assets.prepare_assets(stage, str(usd_path), settings=_settings())

    assert conversions == [b"original pixels"]
    assert _authored_texture(stage) == first_path
    assert (tmp_path / _authored_texture(stage)).read_bytes() == first_bytes
    assert first_bytes == b"original pixels|jpeg-encode"


def test_instanceable_reference_localizes_prototype_texture_in_copied_layer(
    tmp_path,
    monkeypatch,
):
    from pxr import Sdf, Usd

    monkeypatch.setattr(usd_textures, "Sdf", Sdf)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    texture = source_dir / "tex.png"
    texture.write_bytes(b"prototype texture")
    model_path = source_dir / "model.usda"
    model_stage = Usd.Stage.CreateNew(str(model_path))
    model = model_stage.DefinePrim("/Model", "Xform")
    model_stage.SetDefaultPrim(model)
    shader = model_stage.DefinePrim("/Model/Texture", "Shader")
    shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
        Sdf.AssetPath("tex.png")
    )
    model_stage.GetRootLayer().Save()
    source_bytes = model_path.read_bytes()

    usd_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(usd_path))
    for name in ("One", "Two"):
        instance = stage.DefinePrim(f"/{name}", "Xform")
        instance.GetReferences().AddReference(str(model_path), "/Model")
        instance.SetInstanceable(True)
    stage.GetRootLayer().Save()

    usd_assets.prepare_assets(
        stage,
        str(usd_path),
        settings=_settings(),
    )
    stage.GetRootLayer().Save()

    assert model_path.read_bytes() == source_bytes
    localized_models = list(
        (tmp_path / "assets" / "scene.usda").glob("*/model.usda")
    )
    assert len(localized_models) == 1
    staged_textures = list(
        (tmp_path / "textures" / "scene.usda").glob("*/*.png")
    )
    assert len(staged_textures) == 1
    assert staged_textures[0].read_bytes() == b"prototype texture"

    reopened = Usd.Stage.Open(str(usd_path), Usd.Stage.LoadAll)
    one = reopened.GetPrimAtPath("/One")
    two = reopened.GetPrimAtPath("/Two")
    assert one.IsInstance() and two.IsInstance()
    assert one.GetPrototype() == two.GetPrototype()
    prototype_texture = one.GetPrototype().GetChild("Texture")
    value = prototype_texture.GetAttribute("inputs:file").Get()
    assert Path(value.resolvedPath) == staged_textures[0].resolve()


def test_inactive_variant_texture_opinions_are_all_localized_without_collapse(
    tmp_path,
    monkeypatch,
):
    from pxr import Sdf, Usd

    monkeypatch.setattr(usd_textures, "Sdf", Sdf)

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "red.png").write_bytes(b"red texture")
    (source_dir / "blue.png").write_bytes(b"blue texture")
    model_path = source_dir / "model.usda"
    model_stage = Usd.Stage.CreateNew(str(model_path))
    model = model_stage.DefinePrim("/Model", "Xform")
    model_stage.SetDefaultPrim(model)
    variants = model.GetVariantSets().AddVariantSet("look")
    for variant_name, texture_name in (("Red", "red.png"), ("Blue", "blue.png")):
        variants.AddVariant(variant_name)
        variants.SetVariantSelection(variant_name)
        with variants.GetVariantEditContext():
            shader = model_stage.DefinePrim("/Model/Texture", "Shader")
            shader.CreateAttribute("inputs:file", Sdf.ValueTypeNames.Asset).Set(
                Sdf.AssetPath(texture_name)
            )
    variants.SetVariantSelection("Red")
    model_stage.GetRootLayer().Save()
    source_bytes = model_path.read_bytes()

    usd_path = tmp_path / "scene.usda"
    stage = Usd.Stage.CreateNew(str(usd_path))
    referenced = stage.DefinePrim("/Referenced", "Xform")
    referenced.GetReferences().AddReference(str(model_path), "/Model")
    stage.GetRootLayer().Save()

    usd_assets.prepare_assets(
        stage,
        str(usd_path),
        settings=_settings(),
    )
    stage.GetRootLayer().Save()

    assert model_path.read_bytes() == source_bytes
    localized_models = list(
        (tmp_path / "assets" / "scene.usda").glob("*/model.usda")
    )
    assert len(localized_models) == 1
    staged_textures = list(
        (tmp_path / "textures" / "scene.usda").glob("*/*.png")
    )
    assert {path.read_bytes() for path in staged_textures} == {
        b"red texture",
        b"blue texture",
    }

    localized_stage = Usd.Stage.Open(str(localized_models[0]), Usd.Stage.LoadAll)
    localized_model = localized_stage.GetPrimAtPath("/Model")
    localized_variants = localized_model.GetVariantSet("look")
    assert localized_variants.GetVariantSelection() == "Red"
    red_value = localized_stage.GetPrimAtPath("/Model/Texture").GetAttribute(
        "inputs:file"
    ).Get()
    assert Path(red_value.resolvedPath).read_bytes() == b"red texture"
    localized_variants.SetVariantSelection("Blue")
    blue_value = localized_stage.GetPrimAtPath("/Model/Texture").GetAttribute(
        "inputs:file"
    ).Get()
    assert Path(blue_value.resolvedPath).read_bytes() == b"blue texture"
    assert red_value.path != blue_value.path


# ---------------------------------------------------------------------------
# Which staging-owned sources may be deleted after being superseded.
#
# Blender's own textures/ copies must go: left behind they are published
# un-namespaced, archived into the USDZ as a second unreferenced payload, and
# claimed by the ownership manifest, which makes every later export to the same
# path fail with "Immutable sidecar collision has different bytes".
#
# They are identified by capture, never by path shape - see
# staging_namespace.record_native_texture_copies.
# ---------------------------------------------------------------------------


def _superseded(source, dest, textures_dir, native=frozenset()):
    marked: set = set()
    usd_textures._mark_superseded_export_texture(
        source, dest, textures_dir, marked, native
    )
    return marked


def _generation_dir(tmp_path, token="a"):
    path = tmp_path / "textures" / "scene.usda" / (token * 32)
    path.mkdir(parents=True)
    return path


def test_captured_native_copy_is_superseded(tmp_path):
    generation = _generation_dir(tmp_path)
    blender_copy = tmp_path / "textures" / "packedtex.png"
    blender_copy.write_bytes(b"written by wm.usd_export")
    staged = generation / "scene-packedtex-deadbeef.png"
    staged.write_bytes(b"content addressed")

    marked = _superseded(
        blender_copy, staged, generation, frozenset({blender_copy.resolve()})
    )

    assert marked == {blender_copy.resolve()}


def test_uncaptured_flat_texture_is_never_superseded(tmp_path):
    """The property that makes capture necessary.

    A user's authoritative texture can sit at exactly textures/<name>.png and
    is byte-for-byte indistinguishable from a copy Blender just wrote. If it
    was not captured during the native export, it is not ours to delete.
    """
    generation = _generation_dir(tmp_path, "b")
    user_asset = tmp_path / "textures" / "wood.png"
    user_asset.write_bytes(b"authoritative source")
    staged = generation / "scene-wood-cafe.png"
    staged.write_bytes(b"content addressed")

    assert _superseded(user_asset, staged, generation, frozenset()) == set()


def test_source_inside_the_generation_dir_is_still_superseded(tmp_path):
    generation = _generation_dir(tmp_path, "c")
    source = generation / "scene-old-1111.png"
    source.write_bytes(b"old")
    dest = generation / "scene-new-2222.png"
    dest.write_bytes(b"new")

    assert _superseded(source, dest, generation) == {source.resolve()}


def test_identical_source_and_destination_is_not_superseded(tmp_path):
    generation = _generation_dir(tmp_path, "d")
    same = generation / "scene-wood-3333.png"
    same.write_bytes(b"staged in place")

    assert _superseded(same, same, generation, frozenset({same.resolve()})) == set()


# ---------------------------------------------------------------------------
# Windows MAX_PATH
# ---------------------------------------------------------------------------

# The directory from the reported failure, verbatim. A Windows user exported to
# their Desktop and the content-addressed rename blew past MAX_PATH.
_REPORTED_TEXTURES_DIR = (
    r"C:\Users\Guest Dev\Desktop\ExportUsingPlugin"
    r"\.usdstage_temp\Export.usdz.68033a94e13f55aed3c4eac7b164ec50"
    r"\textures\Export.usdc\abccfa1b93abba8b8767a1890009eed1"
)
_REPORTED_STEM = "Export-StonePaving_low_DefaultMaterial_BaseColor"
_REPORTED_DIGEST = (
    "e49c93472d6ec7539bf54f8a89e3badde684977c1c1362e60d1fe0a196292f2a"
)


def test_reported_windows_export_now_fits_max_path(monkeypatch):
    """The exact path from the field report must fit, stem shortened as needed.

    Measured before the fix, on Blender 5.2 / Windows 11:

        [WinError 3] Le chemin d'acces specifie est introuvable:
          '...\\Export-StonePaving_low_DefaultMaterial_BaseColor.png'
          -> '...-e49c9347...f2a.png'

    The copy landed at 215 characters and succeeded; appending the 64-character
    digest took the same path to 280 and ``os.replace`` failed. Assert the
    arithmetic against Windows' own limit rather than against a name we chose,
    so this keeps meaning something if the staging layout changes.
    """
    monkeypatch.setattr(usd_textures, "_PATH_LENGTH_IS_CAPPED", True)
    directory = PureWindowsPath(_REPORTED_TEXTURES_DIR)

    budget = usd_textures._content_stem_budget(
        directory, _REPORTED_DIGEST, ".png"
    )
    stem = usd_textures._truncate_utf8_stem(_REPORTED_STEM, budget)
    full = directory / f"{stem}-{_REPORTED_DIGEST}.png"

    assert len(str(full)) <= 260, f"{len(str(full))} chars: {full}"
    # The unshortened name is what actually failed in the field.
    unshortened = directory / f"{_REPORTED_STEM}-{_REPORTED_DIGEST}.png"
    assert len(str(unshortened)) > 260, len(str(unshortened))


def test_short_paths_keep_their_full_texture_name(monkeypatch):
    """Shortening must be reserved for paths that would otherwise fail.

    Truncating names that already fit would rename textures in every export
    that works today, for nothing.
    """
    monkeypatch.setattr(usd_textures, "_PATH_LENGTH_IS_CAPPED", True)
    directory = PureWindowsPath(r"C:\out\textures")
    stem = "StonePaving_low_DefaultMaterial_BaseColor"

    budget = usd_textures._content_stem_budget(
        directory, _REPORTED_DIGEST, ".png"
    )

    assert usd_textures._truncate_utf8_stem(stem, budget) == stem


def test_stem_budget_never_returns_an_unusable_length(monkeypatch):
    """A directory with no room left still asks for a writable name.

    Such a directory is already unusable on Windows but legal on macOS and
    Linux, where these exports work today. Clamp rather than refuse, and let
    the filesystem be the one to say no.
    """
    monkeypatch.setattr(usd_textures, "_PATH_LENGTH_IS_CAPPED", True)
    hopeless = PureWindowsPath("C:\\" + "d" * 300)

    budget = usd_textures._content_stem_budget(
        hopeless, _REPORTED_DIGEST, ".png"
    )

    assert budget >= usd_textures._MIN_CONTENT_STEM_CHARS
    assert usd_textures._truncate_utf8_stem("BaseColor", budget)


def test_long_path_failure_explains_itself(monkeypatch):
    """WinError 3 names a path that exists one directory up. Say what is wrong."""
    monkeypatch.setattr(usd_textures, "_PATH_LENGTH_IS_CAPPED", True)
    hint = usd_textures._long_path_hint(Path("C:\\" + "d" * 240))

    assert "path limit" in hint
    assert "shorter directory" in hint


def test_short_path_failure_does_not_blame_path_length():
    """A texture missing for an ordinary reason must not be mislabelled."""
    assert usd_textures._long_path_hint(Path("/tmp/out/textures")) == ""


def test_posix_keeps_the_flat_stem_cap(monkeypatch):
    """A Windows limit must not shorten names on macOS or Linux.

    Caught by measurement, not by review: budgeting every platform to MAX_PATH
    left 8 bytes for the stem against this repository's own evaluation exports,
    which stage 248 characters deep on macOS. That would have renamed textures
    in exports that work today, to satisfy a limit those platforms do not have.
    """
    monkeypatch.setattr(usd_textures, "_PATH_LENGTH_IS_CAPPED", False)
    deep = Path("/Users/someone/a/very/deep/export/tree/" + "d" * 200)
    stem = "StonePaving_low_DefaultMaterial_BaseColor"

    budget = usd_textures._content_stem_budget(deep, "e" * 64, ".png")

    assert budget == usd_textures._MAX_CONTENT_STEM_UTF8_BYTES
    assert usd_textures._truncate_utf8_stem(stem, budget) == stem
    assert usd_textures._long_path_hint(deep) == ""
