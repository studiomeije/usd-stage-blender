"""Preflight checks for source assets used during export and baking."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


_EXTERNAL_IMAGE_SOURCES = frozenset({"FILE", "SEQUENCE", "MOVIE", "TILED"})
_PATH_SCAN_FLAGS = {
    "SKIP_PACKED",
    "SKIP_WEAK_REFERENCES",
    "EXPAND_TOKENS",
    "EXPAND_SEQUENCES",
    "EXPAND_CACHES",
}


def collect_missing_asset_files_for_objects(
    objects: Iterable[Any],
    bpy_module=None,
    *,
    context=None,
    settings=None,
) -> list[dict[str, Any]]:
    """Return missing external dependencies used by the processing scope.

    Blender 5.2's ``BlendData.file_path_foreach(subset=...)`` is the source of
    truth for token, sequence and cache expansion.  The subset is assembled by
    walking only exported/processing objects and their reachable datablocks;
    unrelated images, libraries and caches elsewhere in the file are never
    considered.

    The traversal includes collection-instance prototypes, nested material and
    Geometry Nodes groups, typed Geometry Nodes modifier inputs, classic
    ``Texture.image`` modifiers, transform/mesh caches, linked libraries and
    the scene World only when it contributes to the requested export or bake.
    """
    if bpy_module is None:
        import bpy as bpy_module  # type: ignore

    context = context or getattr(bpy_module, "context", None)
    if settings is None:
        scene = getattr(context, "scene", None)
        settings = getattr(scene, "usd_stage_export_settings", None)

    inventory = _collect_dependency_inventory(
        list(objects or []),
        bpy_module,
        context=context,
        settings=settings,
    )
    scanned_paths = _expanded_dependency_paths(
        bpy_module,
        [
            entry["owner"]
            for entry in inventory["owners"].values()
            if entry.get("asset_type")
            in {"IMAGE", "LIBRARY", "CACHE_FILE", "VOLUME", "FONT"}
        ],
    )
    if scanned_paths is None:
        scanned_paths = _fallback_dependency_paths(
            bpy_module,
            inventory["owners"],
        )
    scanned_paths.extend(inventory["manual_paths"])

    records: dict[tuple[str, str], dict[str, Any]] = {}
    for path_entry in scanned_paths:
        owner = path_entry.get("owner")
        owner_entry = inventory["owners"].get(_identity(owner), {})
        raw_path = str(path_entry.get("filepath") or "")
        resolved_path = str(path_entry.get("resolved_path") or "")
        if not raw_path or raw_path == "<builtin>":
            continue
        if _owner_is_embedded(owner):
            continue

        asset_type = str(
            path_entry.get("asset_type")
            or owner_entry.get("asset_type")
            or _asset_type(owner)
        )
        owner_missing = bool(
            asset_type == "LIBRARY" and getattr(owner, "is_missing", False)
        )
        if not owner_missing and _dependency_path_exists(resolved_path, asset_type):
            continue

        key_path = resolved_path or raw_path
        key = (asset_type, key_path)
        record = records.setdefault(
            key,
            {
                "asset_type": asset_type,
                "datablock": str(getattr(owner, "name", "") or ""),
                "filepath": raw_path,
                "resolved_path": resolved_path,
                "users": [],
            },
        )
        if asset_type == "IMAGE":
            record["image"] = str(getattr(owner, "name", "") or "")
            record["source"] = str(getattr(owner, "source", "") or "")
        elif asset_type == "LIBRARY":
            record["library"] = str(getattr(owner, "name", "") or "")

        for user in (*owner_entry.get("users", []), *path_entry.get("users", [])):
            _append_unique(record["users"], user)

    return sorted(
        records.values(),
        key=lambda item: (
            item.get("resolved_path") or item.get("filepath") or "",
            item.get("asset_type") or "",
        ),
    )


def collect_missing_image_files_for_objects(
    objects: Iterable[Any],
    bpy_module=None,
    *,
    context=None,
    settings=None,
) -> list[dict[str, Any]]:
    """Compatibility entry point for the complete external-asset preflight."""
    return collect_missing_asset_files_for_objects(
        objects,
        bpy_module,
        context=context,
        settings=settings,
    )


def collect_asset_dependency_snapshot(context=None) -> dict[str, Any]:
    """Collect support-bundle friendly source asset dependency state."""
    try:
        import bpy

        context = context or bpy.context
        objects = list(getattr(context.scene, "objects", []) or [])
        settings = getattr(context.scene, "usd_stage_export_settings", None)
        missing = collect_missing_asset_files_for_objects(
            objects,
            bpy,
            context=context,
            settings=settings,
        )
        missing_images = [item for item in missing if item.get("asset_type") == "IMAGE"]
        return {
            "missing_file_count": len(missing),
            "missing_files": missing,
            "missing_image_count": len(missing_images),
            "missing_images": missing_images,
        }
    except Exception as exc:
        return {"error": str(exc)}


def record_missing_image_files(diagnostics, missing_images: list[dict[str, Any]]) -> None:
    """Attach missing image information to diagnostics."""
    if not missing_images:
        return
    image_records = [
        item
        for item in missing_images
        if item.get("asset_type") in {None, "IMAGE"}
    ]
    payload = {
        "missing_file_count": len(missing_images),
        "missing_files": missing_images,
        "missing_image_count": len(image_records),
        "missing_images": image_records,
    }
    diagnostics.data.setdefault("asset_dependencies", {}).update(payload)
    diagnostics.data.setdefault("textures", {}).setdefault("missing", []).extend(image_records)
    diagnostics.add_error(_missing_images_message(missing_images))


def missing_images_status_message(missing_images: list[dict[str, Any]]) -> str:
    return _missing_images_message(missing_images)


def missing_assets_error_code(missing_assets: list[dict[str, Any]]) -> str:
    """Preserve the public texture code while distinguishing other files."""
    asset_types = {
        str(item.get("asset_type") or "IMAGE")
        for item in missing_assets
    }
    if asset_types <= {"IMAGE"}:
        return "MISSING_EXTERNAL_TEXTURES"
    return "MISSING_EXTERNAL_ASSETS"


def _missing_images_message(missing_images: list[dict[str, Any]]) -> str:
    count = len(missing_images)
    asset_types = {
        str(item.get("asset_type") or "IMAGE")
        for item in missing_images
    }
    if asset_types != {"IMAGE"}:
        noun = "file" if count == 1 else "files"
        return (
            f"Missing external asset dependency {noun} ({count}). "
            "Relink libraries/caches and pack or relink textures before "
            "a baked export."
        )
    noun = "file" if count == 1 else "files"
    return (
        f"Missing external image {noun} ({count}). "
        "Pack or relink textures before a baked export."
    )


def _collect_dependency_inventory(objects, bpy_module, *, context, settings) -> dict[str, Any]:
    owners: dict[int, dict[str, Any]] = {}
    manual_paths: list[dict[str, Any]] = []
    object_states: dict[int, bool] = {}
    seen_collections: set[int] = set()
    seen_materials: set[int] = set()
    seen_trees: set[int] = set()
    seen_textures: set[int] = set()

    def register_owner(owner, user=None, *, asset_type=None, include_library=True):
        if owner is None:
            return
        marker = _identity(owner)
        entry = owners.setdefault(
            marker,
            {
                "owner": owner,
                "asset_type": asset_type or _asset_type(owner),
                "users": [],
            },
        )
        if asset_type and entry.get("asset_type") in {None, "DATABLOCK"}:
            entry["asset_type"] = asset_type
        if user:
            _append_unique(entry["users"], _clean_user(user))
        if not include_library:
            return

        library = getattr(owner, "library", None)
        if library is not None:
            visit_library(library, user or {"dependency": _owner_label(owner)})
        override = getattr(owner, "override_library", None)
        reference = getattr(override, "reference", None)
        override_library = getattr(reference, "library", None)
        if override_library is not None:
            visit_library(
                override_library,
                user or {"dependency": _owner_label(owner)},
            )

    def visit_library(library, user=None):
        current = library
        visited: set[int] = set()
        while current is not None and _identity(current) not in visited:
            visited.add(_identity(current))
            register_owner(
                current,
                user,
                asset_type="LIBRARY",
                include_library=False,
            )
            current = getattr(current, "parent", None)

    def add_manual_path(owner, filepath, asset_type, user=None):
        raw_path = str(filepath or "").strip()
        if not raw_path or raw_path == "<builtin>":
            return
        manual_paths.append(
            {
                "owner": owner,
                "asset_type": asset_type,
                "filepath": raw_path,
                "resolved_path": _resolve_path(owner, raw_path, bpy_module),
                "users": [_clean_user(user)] if user else [],
            }
        )

    def visit_value(value, user=None):
        value_type = _id_type(value)
        if value_type == "IMAGE":
            if _is_external_image(value):
                register_owner(value, user, asset_type="IMAGE")
        elif value_type == "MATERIAL":
            visit_material(value, user)
        elif value_type == "OBJECT":
            visit_object(value, content=True)
        elif value_type == "COLLECTION":
            visit_collection(value, user)
        elif value_type == "TEXTURE":
            visit_texture(value, user)
        elif value_type == "CACHEFILE":
            register_owner(value, user, asset_type="CACHE_FILE")
        elif value_type in {"VOLUME", "FONT", "NODETREE", "WORLD", "LIGHT"}:
            register_owner(value, user)
            node_tree = getattr(value, "node_tree", None)
            if node_tree is not None:
                visit_tree(node_tree, user)
        elif value is not None and getattr(value, "library", None) is not None:
            register_owner(value, user)

    def visit_texture(texture, user=None):
        if texture is None or _identity(texture) in seen_textures:
            return
        seen_textures.add(_identity(texture))
        register_owner(texture, user, asset_type="TEXTURE")
        visit_value(getattr(texture, "image", None), user)

    def visit_material(material, user=None):
        if material is None:
            return
        material_user = dict(user or {})
        material_user.setdefault("material", str(getattr(material, "name", "") or ""))
        if _identity(material) in seen_materials:
            register_owner(material, material_user, asset_type="MATERIAL")
            return
        seen_materials.add(_identity(material))
        register_owner(material, material_user, asset_type="MATERIAL")
        visit_tree(getattr(material, "node_tree", None), material_user)

    def visit_tree(node_tree, user=None):
        if node_tree is None:
            return
        register_owner(node_tree, user, asset_type="NODE_TREE")
        if _identity(node_tree) in seen_trees:
            return
        seen_trees.add(_identity(node_tree))

        interface = getattr(node_tree, "interface", None)
        for item in getattr(interface, "items_tree", []) or []:
            visit_value(
                getattr(item, "default_value", None),
                _with_user(user, input=getattr(item, "identifier", None)),
            )

        for node in getattr(node_tree, "nodes", []) or []:
            node_user = _with_user(user, node=getattr(node, "name", ""))
            for attr in (
                "image",
                "material",
                "object",
                "collection",
                "texture",
                "ies",
                "script",
            ):
                visit_value(getattr(node, attr, None), node_user)
            for socket in getattr(node, "inputs", []) or []:
                visit_value(
                    getattr(socket, "default_value", None),
                    _with_user(node_user, input=getattr(socket, "identifier", None)),
                )
            visit_tree(getattr(node, "node_tree", None), node_user)

            node_type = str(
                getattr(node, "bl_idname", "")
                or getattr(node, "type", "")
                or ""
            )
            mode = str(getattr(node, "mode", "") or "")
            filepath = str(getattr(node, "filepath", "") or "")
            if filepath and (
                mode == "EXTERNAL"
                or node_type in {"ShaderNodeTexIES", "TEX_IES", "ShaderNodeScript", "SCRIPT"}
            ):
                add_manual_path(node_tree, filepath, "NODE_FILE", node_user)

    def visit_modifier_inputs(modifier, user):
        values = getattr(getattr(modifier, "properties", None), "inputs", None)
        rna = getattr(values, "bl_rna", None)
        for prop in getattr(rna, "properties", []) or []:
            identifier = str(getattr(prop, "identifier", "") or "")
            if not identifier or identifier in {"rna_type", "name"}:
                continue
            try:
                state = getattr(values, identifier)
            except Exception:
                continue
            visit_value(
                getattr(state, "value", None),
                _with_user(user, input=identifier),
            )

    def visit_modifier(modifier, obj):
        evaluation_mode = str(getattr(settings, "evaluation_mode", "RENDER") or "RENDER")
        visible = (
            getattr(modifier, "show_render", True)
            if evaluation_mode == "RENDER"
            else getattr(modifier, "show_viewport", True)
        )
        if not visible:
            return
        user = {
            "object": str(getattr(obj, "name", "") or ""),
            "modifier": str(getattr(modifier, "name", "") or ""),
        }
        visit_texture(getattr(modifier, "texture", None), user)
        visit_texture(getattr(modifier, "mask_texture", None), user)
        for prop in getattr(getattr(modifier, "bl_rna", None), "properties", []) or []:
            if str(getattr(prop, "type", "") or "") != "POINTER":
                continue
            fixed_type = getattr(prop, "fixed_type", None)
            fixed_identifier = str(getattr(fixed_type, "identifier", "") or "").upper()
            if fixed_identifier not in {"IMAGE", "TEXTURE"}:
                continue
            identifier = str(getattr(prop, "identifier", "") or "")
            if not identifier:
                continue
            try:
                visit_value(getattr(modifier, identifier), user)
            except Exception:
                continue
        visit_tree(getattr(modifier, "node_group", None), user)
        visit_modifier_inputs(modifier, user)
        visit_value(getattr(modifier, "cache_file", None), user)

        modifier_type = str(getattr(modifier, "type", "") or "")
        filepath = str(getattr(modifier, "filepath", "") or "")
        if filepath and modifier_type == "MESH_CACHE":
            add_manual_path(obj, filepath, "MODIFIER_CACHE", user)
        elif (
            filepath
            and modifier_type == "OCEAN"
            and bool(getattr(modifier, "is_cached", False))
        ):
            add_manual_path(obj, filepath, "SIMULATION_CACHE", user)

        domain = getattr(modifier, "domain_settings", None)
        cache_type = str(getattr(domain, "cache_type", "") or "")
        cache_directory = str(getattr(domain, "cache_directory", "") or "")
        if cache_directory and cache_type in {"FINAL", "MODULAR"}:
            add_manual_path(obj, cache_directory, "SIMULATION_CACHE", user)

    def visit_collection(collection, user=None):
        if collection is None:
            return
        register_owner(collection, user, asset_type="COLLECTION")
        if _identity(collection) in seen_collections:
            return
        seen_collections.add(_identity(collection))
        prototypes = getattr(collection, "all_objects", None)
        if prototypes is None:
            prototypes = getattr(collection, "objects", [])
        for prototype in prototypes or []:
            visit_object(prototype, content=True)

    def visit_object(obj, *, content):
        if obj is None:
            return
        marker = _identity(obj)
        previous_content = object_states.get(marker)
        if previous_content is True or (previous_content is False and not content):
            return
        object_states[marker] = bool(content or previous_content)
        object_user = {"object": str(getattr(obj, "name", "") or "")}
        register_owner(obj, object_user, asset_type="OBJECT")

        parent = getattr(obj, "parent", None)
        if parent is not None:
            visit_object(parent, content=False)
        visit_collection(getattr(obj, "instance_collection", None), object_user)

        for constraint in getattr(obj, "constraints", []) or []:
            visit_value(
                getattr(constraint, "cache_file", None),
                _with_user(object_user, constraint=getattr(constraint, "name", "")),
            )

        if not content or not _object_content_enabled(obj, settings):
            return
        data = getattr(obj, "data", None)
        object_type = str(getattr(obj, "type", "") or "")
        if data is not None and not (object_type == "EMPTY"):
            register_owner(data, object_user)
            visit_tree(getattr(data, "node_tree", None), object_user)
            for material in getattr(data, "materials", []) or []:
                visit_material(material, object_user)

        for material in _iter_object_materials(obj):
            visit_material(material, object_user)
        for modifier in getattr(obj, "modifiers", []) or []:
            visit_modifier(modifier, obj)

    for scoped_object in objects:
        visit_object(scoped_object, content=True)

    scene = getattr(context, "scene", None)
    if _world_affects_output(settings):
        world = getattr(scene, "world", None)
        if world is not None:
            world_user = {"world": str(getattr(world, "name", "") or "")}
            register_owner(world, world_user, asset_type="WORLD")
            visit_tree(getattr(world, "node_tree", None), world_user)

    if _explicit_bake_hdri_affects_output(settings):
        raw_hdri = str(getattr(settings, "bake_ibl_filepath", "") or "").strip()
        if raw_hdri:
            add_manual_path(
                scene,
                raw_hdri,
                "HDRI",
                {"setting": "bake_ibl_filepath"},
            )

    return {"owners": owners, "manual_paths": manual_paths}


def _expanded_dependency_paths(bpy_module, owners) -> list[dict[str, Any]] | None:
    """Use Blender 5.2's scoped path visitor for exact token expansion."""
    file_path_foreach = getattr(getattr(bpy_module, "data", None), "file_path_foreach", None)
    if file_path_foreach is None:
        return None
    try:
        subset = set(owners)
    except Exception as exc:
        raise RuntimeError(
            "Blender 5.2 asset preflight could not build its scoped datablock set."
        ) from exc
    if not subset:
        return []

    paths: list[dict[str, Any]] = []

    def visit(owner, filepath, metadata):
        raw_path = str(filepath or "")
        if not raw_path:
            return None
        paths.append(
            {
                "owner": owner,
                "asset_type": _asset_type(owner),
                "filepath": raw_path,
                "resolved_path": _resolve_path(owner, raw_path, bpy_module),
                "expanded": bool(getattr(metadata, "is_expanded", False)),
                "cache": bool(getattr(metadata, "is_cache", False)),
                "users": [],
            }
        )
        return None

    try:
        file_path_foreach(
            visit,
            subset=subset,
            flags=set(_PATH_SCAN_FLAGS),
        )
    except Exception as exc:
        raise RuntimeError(
            "Blender 5.2 asset dependency scanning failed while expanding "
            "UDIM, sequence, or cache paths. Save the scene and relink its "
            "external assets, then retry."
        ) from exc
    return paths


def _fallback_dependency_paths(bpy_module, owners) -> list[dict[str, Any]]:
    """Best-effort fallback for test stubs and a damaged Blender path API."""
    paths: list[dict[str, Any]] = []
    for entry in owners.values():
        owner = entry["owner"]
        asset_type = str(entry.get("asset_type") or _asset_type(owner))
        if asset_type == "IMAGE":
            candidates = _resolved_image_paths(owner, bpy_module)
        else:
            raw_path = str(
                getattr(owner, "filepath", None)
                or getattr(owner, "filepath_raw", None)
                or ""
            )
            candidates = (
                [(raw_path, _resolve_path(owner, raw_path, bpy_module))]
                if raw_path
                else []
            )
        for raw_path, resolved_path in candidates:
            paths.append(
                {
                    "owner": owner,
                    "asset_type": asset_type,
                    "filepath": str(raw_path),
                    "resolved_path": str(resolved_path),
                    "users": [],
                }
            )
    return paths


def _resolve_path(owner, raw_path: str, bpy_module) -> str:
    if not raw_path:
        return ""
    library = getattr(owner, "library", None)
    if _id_type(owner) == "LIBRARY":
        library = getattr(owner, "parent", None)
    try:
        return str(bpy_module.path.abspath(raw_path, library=library))
    except Exception:
        try:
            return str(bpy_module.path.abspath(raw_path))
        except Exception:
            try:
                return str(Path(raw_path).expanduser())
            except Exception:
                return raw_path


def _identity(value) -> int:
    try:
        return int(value.as_pointer())
    except Exception:
        return id(value)


def _id_type(value) -> str:
    if value is None:
        return ""
    explicit = str(getattr(value, "id_type", "") or "").upper()
    if explicit:
        return explicit
    class_name = type(value).__name__.upper()
    aliases = {
        "CACHEFILE": "CACHEFILE",
        "VECTORFONT": "FONT",
        "NODETREE": "NODETREE",
    }
    if class_name in aliases:
        return aliases[class_name]
    if class_name in {
        "IMAGE",
        "MATERIAL",
        "OBJECT",
        "COLLECTION",
        "TEXTURE",
        "VOLUME",
        "WORLD",
        "LIGHT",
        "LIBRARY",
    }:
        return class_name

    # Lightweight test doubles do not have Blender's ID metadata.  Keep the
    # inference intentionally narrow so arbitrary filepath-bearing values are
    # never mistaken for an exported dependency.
    if hasattr(value, "source") and (
        hasattr(value, "packed_file") or hasattr(value, "packed_files")
    ):
        return "IMAGE"
    return ""


def _asset_type(owner) -> str:
    id_type = _id_type(owner)
    return {
        "IMAGE": "IMAGE",
        "LIBRARY": "LIBRARY",
        "CACHEFILE": "CACHE_FILE",
        "VOLUME": "VOLUME",
        "FONT": "FONT",
        "NODETREE": "NODE_TREE",
        "MATERIAL": "MATERIAL",
        "TEXTURE": "TEXTURE",
        "COLLECTION": "COLLECTION",
        "OBJECT": "OBJECT",
        "WORLD": "WORLD",
        "LIGHT": "LIGHT",
    }.get(id_type, "DATABLOCK")


def _is_external_image(image) -> bool:
    if image is None or _image_is_packed(image):
        return False
    return str(getattr(image, "source", "") or "").upper() in _EXTERNAL_IMAGE_SOURCES


def _owner_is_embedded(owner) -> bool:
    if owner is None:
        return False
    if _id_type(owner) == "IMAGE" and not _is_external_image(owner):
        return True
    if getattr(owner, "packed_file", None) is not None:
        return True
    packed_files = getattr(owner, "packed_files", None)
    if packed_files is not None:
        try:
            return len(packed_files) > 0
        except Exception:
            pass
    return False


def _object_content_enabled(obj, settings) -> bool:
    object_type = str(getattr(obj, "type", "") or "")
    # USD Stage for Blender's Apple OS 27 contract deliberately does not pass raw
    # curve, hair/Curves, font, point-cloud, volume or light primitives through
    # to USD. Old saved settings must not make their external inputs
    # release-blocking when the object itself cannot enter the export.
    if object_type in {
        "CURVE",
        "SURFACE",
        "FONT",
        "CURVES",
        "POINTCLOUD",
        "VOLUME",
        "LIGHT",
        "CAMERA",
    }:
        return False
    if settings is None:
        return True
    # Meshes are always exported: there is no export_meshes property on the
    # PropertyGroup, so the old getattr lookup could only ever return its
    # fallback while reading as though a user setting governed it.
    if object_type == "MESH":
        return True
    if object_type == "ARMATURE":
        return bool(getattr(settings, "export_armatures", True))
    return True


def _world_affects_output(settings) -> bool:
    if settings is None:
        return False
    return (
        str(getattr(settings, "bake_mode", "") or "") == "LIT_IBL"
        and str(getattr(settings, "bake_ibl_source", "") or "") == "SCENE_WORLD"
    )


def _explicit_bake_hdri_affects_output(settings) -> bool:
    if settings is None:
        return False
    return (
        str(getattr(settings, "bake_mode", "") or "") == "LIT_IBL"
        and str(getattr(settings, "bake_ibl_source", "") or "") == "HDRI_FILE"
    )


def _clean_user(user) -> dict[str, Any]:
    if not isinstance(user, dict):
        return {"dependency": str(user)}
    return {
        str(key): value
        for key, value in user.items()
        if value not in {None, ""}
    }


def _with_user(user, **values) -> dict[str, Any]:
    merged = dict(user or {})
    merged.update(values)
    return _clean_user(merged)


def _append_unique(items: list, value) -> None:
    if not value:
        return
    if value not in items:
        items.append(value)


def _owner_label(owner) -> str:
    owner_type = _asset_type(owner).lower().replace("_", " ")
    name = str(getattr(owner, "name", "") or "")
    return f"{owner_type} '{name}'" if name else owner_type


def _iter_object_materials(obj) -> Iterable[Any]:
    seen: set[int] = set()
    for slot in getattr(obj, "material_slots", []) or []:
        material = getattr(slot, "material", None)
        if material is None:
            continue
        marker = id(material)
        if marker in seen:
            continue
        seen.add(marker)
        yield material


def _image_is_packed(image) -> bool:
    if getattr(image, "packed_file", None) is not None:
        return True
    packed_files = getattr(image, "packed_files", None)
    if packed_files is None:
        return False
    try:
        return len(packed_files) > 0
    except Exception:
        return False


def _resolved_image_paths(image, bpy_module) -> list[tuple[str, str]]:
    raw_path = str(getattr(image, "filepath", "") or getattr(image, "filepath_raw", "") or "")
    if not raw_path:
        return []
    try:
        resolved_path = str(bpy_module.path.abspath(raw_path, library=getattr(image, "library", None)))
    except Exception:
        try:
            resolved_path = str(bpy_module.path.abspath(raw_path))
        except Exception:
            resolved_path = raw_path

    if "<UDIM>" not in resolved_path:
        return [(raw_path, resolved_path)]

    paths: list[tuple[str, str]] = []
    for tile in getattr(image, "tiles", []) or []:
        number = str(getattr(tile, "number", ""))
        if not number:
            continue
        paths.append((raw_path.replace("<UDIM>", number), resolved_path.replace("<UDIM>", number)))
    return paths or [(raw_path, resolved_path)]


def _dependency_path_exists(path: str, asset_type: str) -> bool:
    if not path:
        return False
    try:
        candidate = Path(path)
        if asset_type == "SIMULATION_CACHE":
            return candidate.is_dir()
        return candidate.is_file()
    except OSError:
        return False
