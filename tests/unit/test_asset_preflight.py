"""Asset preflight helper tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from Plugin.export.asset_preflight import (
    _expanded_dependency_paths,
    collect_missing_asset_files_for_objects,
    collect_missing_image_files_for_objects,
    missing_assets_error_code,
)


class _FakePath:
    @staticmethod
    def abspath(path, library=None):
        return str(path).replace("//", "/project/")


def _image(name, filepath, *, source="FILE", packed=False):
    return SimpleNamespace(
        id_type="IMAGE",
        name=name,
        filepath=str(filepath),
        filepath_raw=str(filepath),
        source=source,
        packed_file=object() if packed else None,
        packed_files=[],
        library=None,
    )


def _object(name, **values):
    defaults = {
        "id_type": "OBJECT",
        "name": name,
        "type": "MESH",
        "data": None,
        "parent": None,
        "instance_collection": None,
        "material_slots": [],
        "modifiers": [],
        "constraints": [],
        "library": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_collect_missing_image_files_finds_nested_node_group_image(tmp_path):
    existing = tmp_path / "ok.png"
    existing.write_bytes(b"png")
    missing = tmp_path / "missing.png"

    image_ok = SimpleNamespace(
        name="Existing",
        filepath=str(existing),
        filepath_raw=str(existing),
        source="FILE",
        packed_file=None,
        packed_files=[],
        library=None,
    )
    image_missing = SimpleNamespace(
        name="Missing",
        filepath=str(missing),
        filepath_raw=str(missing),
        source="FILE",
        packed_file=None,
        packed_files=[],
        library=None,
    )
    group_tree = SimpleNamespace(nodes=[
        SimpleNamespace(type="TEX_IMAGE", image=image_missing, name="Nested Image"),
    ])
    root_tree = SimpleNamespace(nodes=[
        SimpleNamespace(type="TEX_IMAGE", image=image_ok, name="Root Image"),
        SimpleNamespace(type="GROUP", node_tree=group_tree, name="Group"),
    ])
    material = SimpleNamespace(name="Material", node_tree=root_tree)
    obj = SimpleNamespace(name="Object", material_slots=[SimpleNamespace(material=material)])

    result = collect_missing_image_files_for_objects([obj], SimpleNamespace(path=_FakePath()))

    assert len(result) == 1
    assert result[0]["image"] == "Missing"
    assert result[0]["users"] == [{"object": "Object", "material": "Material", "node": "Nested Image"}]


def test_blender_52_path_expansion_is_scoped_and_includes_cache_dependencies():
    captured = {}

    class _Owner:
        id_type = "IMAGE"
        library = None

    class _FakeData:
        @staticmethod
        def file_path_foreach(callback, *, subset, flags):
            captured["subset"] = subset
            captured["flags"] = flags

    owner = _Owner()
    result = _expanded_dependency_paths(
        SimpleNamespace(data=_FakeData(), path=_FakePath()),
        [owner],
    )

    assert result == []
    assert captured["subset"] == {owner}
    assert {
        "EXPAND_TOKENS",
        "EXPAND_SEQUENCES",
        "EXPAND_CACHES",
    } <= captured["flags"]


def test_live_blender_path_scanner_failure_does_not_downgrade_to_singular_paths():
    class _Owner:
        id_type = "IMAGE"
        library = None

    class _BrokenData:
        @staticmethod
        def file_path_foreach(*_args, **_kwargs):
            raise ValueError("RNA scanner unavailable")

    with pytest.raises(RuntimeError, match="UDIM, sequence, or cache"):
        _expanded_dependency_paths(
            SimpleNamespace(data=_BrokenData(), path=_FakePath()),
            [_Owner()],
        )


def test_geometry_nodes_nested_groups_and_typed_modifier_images_are_scoped(tmp_path):
    nested_image = _image("Nested GN", tmp_path / "nested-gn.png")
    override_image = _image("Modifier Override", tmp_path / "override.png")
    unrelated_image = _image("Unrelated", tmp_path / "unrelated.png")

    nested_tree = SimpleNamespace(
        id_type="NODETREE",
        name="Nested Geometry",
        nodes=[SimpleNamespace(name="Image Info", image=nested_image, inputs=[])],
        interface=None,
        library=None,
    )
    root_tree = SimpleNamespace(
        id_type="NODETREE",
        name="Root Geometry",
        nodes=[SimpleNamespace(name="Nested Group", node_tree=nested_tree, inputs=[])],
        interface=None,
        library=None,
    )
    typed_inputs = SimpleNamespace(
        bl_rna=SimpleNamespace(
            properties=[
                SimpleNamespace(identifier="rna_type"),
                SimpleNamespace(identifier="Image_2"),
            ]
        ),
        Image_2=SimpleNamespace(value=override_image),
    )
    modifier = SimpleNamespace(
        name="GeometryNodes",
        type="NODES",
        node_group=root_tree,
        properties=SimpleNamespace(inputs=typed_inputs),
        show_render=True,
        texture=None,
        cache_file=None,
    )
    obj = _object("Scoped", modifiers=[modifier])
    bpy_module = SimpleNamespace(
        path=_FakePath(),
        data=SimpleNamespace(images=[unrelated_image]),
    )

    result = collect_missing_asset_files_for_objects([obj], bpy_module)

    assert {item["image"] for item in result} == {"Nested GN", "Modifier Override"}
    override = next(item for item in result if item["image"] == "Modifier Override")
    assert override["users"] == [
        {"object": "Scoped", "modifier": "GeometryNodes", "input": "Image_2"}
    ]


def test_classic_modifier_texture_includes_external_but_not_packed_or_generated(tmp_path):
    external = _image("Displace", tmp_path / "displace.png")
    mask = _image("Weight Mask", tmp_path / "weight-mask.png")
    rna_image = _image("RNA Pointer", tmp_path / "rna-pointer.png")
    packed = _image("Packed", tmp_path / "packed.png", packed=True)
    generated = _image("Generated", tmp_path / "generated.png", source="GENERATED")

    def modifier(name, image):
        return SimpleNamespace(
            name=name,
            type="DISPLACE",
            texture=SimpleNamespace(
                id_type="TEXTURE",
                name=f"{name} Texture",
                image=image,
                library=None,
            ),
            node_group=None,
            properties=None,
            cache_file=None,
            show_render=True,
        )

    obj = _object(
        "Terrain",
        modifiers=[
            modifier("External Displace", external),
            SimpleNamespace(
                name="Weight Mask",
                type="VERTEX_WEIGHT_EDIT",
                texture=None,
                mask_texture=SimpleNamespace(
                    id_type="TEXTURE",
                    name="Weight Mask Texture",
                    image=mask,
                    library=None,
                ),
                node_group=None,
                properties=None,
                cache_file=None,
                show_render=True,
            ),
            SimpleNamespace(
                name="RNA Image Resource",
                type="TEST",
                texture=None,
                mask_texture=None,
                image_resource=rna_image,
                bl_rna=SimpleNamespace(
                    properties=[
                        SimpleNamespace(
                            type="POINTER",
                            identifier="image_resource",
                            fixed_type=SimpleNamespace(identifier="Image"),
                        )
                    ]
                ),
                node_group=None,
                properties=None,
                cache_file=None,
                show_render=True,
            ),
            modifier("Packed Displace", packed),
            modifier("Generated Displace", generated),
        ],
    )

    result = collect_missing_asset_files_for_objects(
        [obj],
        SimpleNamespace(path=_FakePath()),
    )

    assert {item["image"] for item in result} == {
        "Displace",
        "Weight Mask",
        "RNA Pointer",
    }
    displace_result = next(item for item in result if item["image"] == "Displace")
    assert displace_result["users"] == [
        {"object": "Terrain", "modifier": "External Displace"}
    ]
    mask_result = next(item for item in result if item["image"] == "Weight Mask")
    assert mask_result["users"] == [
        {"object": "Terrain", "modifier": "Weight Mask"}
    ]


def test_collection_instance_prototype_material_dependency_is_included(tmp_path):
    image = _image("Prototype Image", tmp_path / "prototype.png")
    tree = SimpleNamespace(
        id_type="NODETREE",
        name="Prototype Tree",
        nodes=[SimpleNamespace(name="Image", image=image, inputs=[])],
        interface=None,
        library=None,
    )
    material = SimpleNamespace(
        id_type="MATERIAL",
        name="Prototype Material",
        node_tree=tree,
        library=None,
    )
    prototype = _object(
        "Prototype",
        material_slots=[SimpleNamespace(material=material)],
    )
    collection = SimpleNamespace(
        id_type="COLLECTION",
        name="Prototype Collection",
        all_objects=[prototype],
        library=None,
    )
    instancer = _object(
        "Instancer",
        type="EMPTY",
        instance_collection=collection,
    )

    result = collect_missing_asset_files_for_objects(
        [instancer],
        SimpleNamespace(path=_FakePath()),
    )

    assert len(result) == 1
    assert result[0]["image"] == "Prototype Image"
    assert result[0]["users"] == [
        {
            "object": "Prototype",
            "material": "Prototype Material",
            "node": "Image",
        }
    ]


def test_scene_world_is_checked_only_when_it_affects_lit_ibl_bake(tmp_path):
    world_image = _image("World HDRI", tmp_path / "world.exr")
    world_tree = SimpleNamespace(
        id_type="NODETREE",
        name="World Tree",
        nodes=[SimpleNamespace(name="Environment", image=world_image, inputs=[])],
        interface=None,
        library=None,
    )
    world = SimpleNamespace(
        id_type="WORLD",
        name="Studio World",
        node_tree=world_tree,
        library=None,
    )
    scene = SimpleNamespace(world=world)
    context = SimpleNamespace(scene=scene)
    bpy_module = SimpleNamespace(path=_FakePath(), context=context)
    obj = _object("Mesh")

    unlit = SimpleNamespace(
        bake_mode="UNLIT_ALBEDO",
        bake_ibl_source="SCENE_WORLD",
        export_meshes=True,
        evaluation_mode="RENDER",
    )
    assert collect_missing_asset_files_for_objects(
        [obj], bpy_module, context=context, settings=unlit
    ) == []

    lit_ibl = SimpleNamespace(
        bake_mode="LIT_IBL",
        bake_ibl_source="SCENE_WORLD",
        export_meshes=True,
        evaluation_mode="RENDER",
    )
    result = collect_missing_asset_files_for_objects(
        [obj], bpy_module, context=context, settings=lit_ibl
    )
    assert len(result) == 1
    assert result[0]["image"] == "World HDRI"
    assert result[0]["users"] == [
        {"world": "Studio World", "node": "Environment"}
    ]


def test_explicit_bake_hdri_replaces_scene_world_dependency(tmp_path):
    world_image = _image("Unused World", tmp_path / "unused.exr")
    world = SimpleNamespace(
        id_type="WORLD",
        name="Unused",
        node_tree=SimpleNamespace(
            id_type="NODETREE",
            name="World Tree",
            nodes=[SimpleNamespace(name="Environment", image=world_image, inputs=[])],
            interface=None,
            library=None,
        ),
        library=None,
    )
    context = SimpleNamespace(scene=SimpleNamespace(world=world))
    settings = SimpleNamespace(
        bake_mode="LIT_IBL",
        bake_ibl_source="HDRI_FILE",
        bake_ibl_filepath=str(tmp_path / "explicit.hdr"),
        export_meshes=True,
        evaluation_mode="RENDER",
    )

    result = collect_missing_asset_files_for_objects(
        [_object("Mesh")],
        SimpleNamespace(path=_FakePath(), context=context),
        context=context,
        settings=settings,
    )

    assert len(result) == 1
    assert result[0]["asset_type"] == "HDRI"
    assert result[0]["users"] == [{"setting": "bake_ibl_filepath"}]


def test_scoped_linked_library_and_cache_files_fail_closed_without_global_scan(tmp_path):
    scoped_library = SimpleNamespace(
        id_type="LIBRARY",
        name="Scoped Library",
        filepath=str(tmp_path / "scoped.blend"),
        parent=None,
        is_missing=True,
    )
    unrelated_library = SimpleNamespace(
        id_type="LIBRARY",
        name="Unrelated Library",
        filepath=str(tmp_path / "unrelated.blend"),
        parent=None,
        is_missing=True,
    )
    cache = SimpleNamespace(
        id_type="CACHEFILE",
        name="Character Cache",
        filepath=str(tmp_path / "character.abc"),
        library=None,
    )
    modifier = SimpleNamespace(
        name="Mesh Sequence",
        type="MESH_SEQUENCE_CACHE",
        cache_file=cache,
        texture=None,
        node_group=None,
        properties=None,
        show_render=True,
    )
    obj = _object("Linked Mesh", library=scoped_library, modifiers=[modifier])
    bpy_module = SimpleNamespace(
        path=_FakePath(),
        data=SimpleNamespace(libraries=[scoped_library, unrelated_library]),
    )

    result = collect_missing_asset_files_for_objects([obj], bpy_module)

    assert {item["asset_type"] for item in result} == {"LIBRARY", "CACHE_FILE"}
    assert {item["datablock"] for item in result} == {
        "Scoped Library",
        "Character Cache",
    }
    assert missing_assets_error_code(result) == "MISSING_EXTERNAL_ASSETS"


def test_image_only_failures_keep_the_existing_error_code(tmp_path):
    image = _image("Image", tmp_path / "missing.png")
    tree = SimpleNamespace(
        id_type="NODETREE",
        name="Tree",
        nodes=[SimpleNamespace(name="Image", image=image, inputs=[])],
        interface=None,
        library=None,
    )
    material = SimpleNamespace(
        id_type="MATERIAL",
        name="Material",
        node_tree=tree,
        library=None,
    )
    result = collect_missing_asset_files_for_objects(
        [_object("Mesh", material_slots=[SimpleNamespace(material=material)])],
        SimpleNamespace(path=_FakePath()),
    )

    assert missing_assets_error_code(result) == "MISSING_EXTERNAL_TEXTURES"


def test_removed_raw_object_types_do_not_create_dependency_false_positives(tmp_path):
    image = _image("Unused Raw Input", tmp_path / "unused.png")
    modifier = SimpleNamespace(
        name="Unused Modifier",
        type="DISPLACE",
        texture=SimpleNamespace(
            id_type="TEXTURE",
            name="Unused Texture",
            image=image,
            library=None,
        ),
        node_group=None,
        properties=None,
        cache_file=None,
        show_render=True,
    )
    legacy_settings = SimpleNamespace(
        export_curves=True,
        export_points=True,
        export_volumes=True,
        export_lights=True,
        convert_world_material=True,
        export_cameras=True,
        evaluation_mode="RENDER",
        bake_mode="UNLIT_ALBEDO",
        bake_ibl_source="SCENE_WORLD",
    )

    for object_type in (
        "CURVE",
        "SURFACE",
        "FONT",
        "CURVES",
        "POINTCLOUD",
        "VOLUME",
        "LIGHT",
        "CAMERA",
    ):
        result = collect_missing_asset_files_for_objects(
            [_object("Unsupported", type=object_type, modifiers=[modifier])],
            SimpleNamespace(path=_FakePath()),
            settings=legacy_settings,
        )
        assert result == [], object_type

    world = SimpleNamespace(
        id_type="WORLD",
        name="Removed Raw World",
        node_tree=SimpleNamespace(
            id_type="NODETREE",
            name="Removed World Tree",
            nodes=[SimpleNamespace(name="Environment", image=image, inputs=[])],
            interface=None,
            library=None,
        ),
        library=None,
    )
    context = SimpleNamespace(scene=SimpleNamespace(world=world))
    assert collect_missing_asset_files_for_objects(
        [_object("Mesh")],
        SimpleNamespace(path=_FakePath(), context=context),
        context=context,
        settings=legacy_settings,
    ) == []


def test_directory_at_image_filepath_is_still_missing(tmp_path):
    directory = tmp_path / "looks-like-an-image.png"
    directory.mkdir()
    image = _image("Directory Image", directory)
    tree = SimpleNamespace(
        id_type="NODETREE",
        name="Material Tree",
        nodes=[SimpleNamespace(name="Image", image=image, inputs=[])],
        interface=None,
        library=None,
    )
    material = SimpleNamespace(
        id_type="MATERIAL",
        name="Material",
        node_tree=tree,
        library=None,
    )

    result = collect_missing_asset_files_for_objects(
        [_object("Mesh", material_slots=[SimpleNamespace(material=material)])],
        SimpleNamespace(path=_FakePath()),
    )

    assert len(result) == 1
    assert result[0]["image"] == "Directory Image"
