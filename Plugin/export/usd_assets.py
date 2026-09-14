"""USD dependency staging utilities.

Localize non-texture asset attributes and composition arcs so a later USDZ
packaging pass can include a complete, portable dependency closure.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from . import usd_textures
from .staging_namespace import output_sidecar_namespace
from .usd_utils import Sdf
from .usd_textures import _is_texture_path


NON_FILE_PREFIXES = (
    "anon:",
    "mem:",
    "http:",
    "https:",
    "data:",
    "blob:",
)

_USD_LAYER_EXTENSIONS = {".usd", ".usda", ".usdc"}


def prepare_assets(
    stage,
    usd_path: str,
    diagnostics=None,
    *,
    settings=None,
) -> frozenset[str]:
    """Stage local non-texture dependencies and author portable paths.

    This covers all composition arcs (subLayers, references and payloads),
    scalar/array asset attributes, and default/time-sampled attribute values.
    Missing local dependencies are fatal: silently rewriting them to a path
    that was never copied would create a package that only appears portable.

    Every copied dependency is placed below an output-specific namespace.  In
    addition to preventing one unpacked export from overwriting another
    export's equally named sidecars, this lets callers treat the returned
    layer paths as the exact set of layers they may safely mutate.
    """
    usd_dir = Path(usd_path).resolve().parent
    assets_dir = usd_dir / "assets" / output_sidecar_namespace(usd_path)
    seen_sources: Dict[Path, Path] = {}
    seen_names: Dict[str, Path] = {}
    failures: List[str] = []
    writable_layer_paths: set[str] = set()
    texture_state = (
        usd_textures.create_texture_staging_state(
            usd_path,
            settings,
            diagnostics,
        )
        if settings is not None
        else None
    )

    try:
        if Sdf is not None and hasattr(stage, "GetRootLayer"):
            _stage_composition_dependencies(
                stage,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                writable_layer_paths,
                texture_state,
                diagnostics,
            )
        else:
            # Keep the small fake-stage contract used by non-USD unit tests.
            # Real stages take the layer-aware path above so relative values
            # are always resolved against the layer that authored them.
            _stage_asset_attributes(
                stage,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                diagnostics,
            )
            _stage_asset_metadata(
                stage,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                diagnostics,
            )
    finally:
        if texture_state is not None:
            usd_textures.finish_texture_staging(
                texture_state,
                stage=stage,
                diagnostics=diagnostics,
            )

    if failures:
        unique_failures = list(dict.fromkeys(failures))
        details = "\n".join(f"- {failure}" for failure in unique_failures)
        raise RuntimeError(f"Failed to stage USD dependencies:\n{details}")

    return frozenset(writable_layer_paths)


def _stage_composition_dependencies(
    stage,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    writable_layer_paths: set[str],
    texture_state,
    diagnostics=None,
) -> None:
    if Sdf is None or not hasattr(stage, "GetRootLayer"):
        return
    root_layer = stage.GetRootLayer()
    if not root_layer or not hasattr(root_layer, "GetExternalReferences"):
        return

    processed = set()
    _stage_layer_dependencies(
        source_layer=root_layer,
        destination_layer=root_layer,
        destination_path=usd_dir / Path(str(root_layer.identifier)).name,
        assets_dir=assets_dir,
        seen_sources=seen_sources,
        seen_names=seen_names,
        processed=processed,
        failures=failures,
        writable_layer_paths=writable_layer_paths,
        texture_state=texture_state,
        diagnostics=diagnostics,
        save_destination=False,
    )


def _stage_layer_dependencies(
    *,
    source_layer,
    destination_layer,
    destination_path: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    processed: set,
    failures: List[str],
    writable_layer_paths: set[str],
    texture_state,
    diagnostics=None,
    save_destination: bool = False,
) -> None:
    """Copy and rewrite a layer's composition dependencies recursively."""
    source_identifier = _layer_filesystem_path(source_layer)
    destination_key = destination_path.resolve()
    key = (source_identifier, destination_key)
    if key in processed:
        return
    processed.add(key)

    writable_layer_paths.add(str(destination_key))

    changed = _stage_layer_authored_assets(
        source_layer=source_layer,
        destination_layer=destination_layer,
        destination_path=destination_path,
        assets_dir=assets_dir,
        seen_sources=seen_sources,
        seen_names=seen_names,
        failures=failures,
        texture_state=texture_state,
        diagnostics=diagnostics,
    )

    # Asset-valued metadata is also allowed to name USD layers. Value clips are
    # the important case: their assetPaths/manifestAssetPath entries are not
    # reported by Sdf.Layer.GetExternalReferences(), so merely copying those
    # files leaves any dependencies authored inside the clip layers external.
    # Recurse through every newly staged USD-valued asset before the parent is
    # repointed, using the same source-layer resolver context and destination
    # ownership rules as ordinary composition arcs.
    _stage_asset_layer_dependencies(
        assets_dir=assets_dir,
        seen_sources=seen_sources,
        seen_names=seen_names,
        processed=processed,
        failures=failures,
        writable_layer_paths=writable_layer_paths,
        texture_state=texture_state,
        diagnostics=diagnostics,
    )

    try:
        authored_references = list(source_layer.GetExternalReferences())
    except Exception as exc:
        failures.append(f"Could not inspect layer dependencies for {source_identifier}: {exc}")
        return

    for authored_path in authored_references:
        authored_path = str(authored_path or "")
        if not authored_path:
            continue
        if _is_non_file_asset(authored_path):
            message = f"Non-file composition dependency cannot be packaged: {authored_path}"
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
            continue
        outer_authored_path, package_member = _split_package_relative_path(
            authored_path
        )

        source_path = _resolve_layer_dependency(source_layer, outer_authored_path)
        if source_path is None or not source_path.is_file():
            message = (
                f"Composition dependency not found: {authored_path} "
                f"(from {source_identifier})"
            )
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
            continue

        destination = seen_sources.get(source_path)
        if destination is None:
            destination_name = _unique_destination_name(
                source_path,
                seen_names,
                diagnostics,
                "composition dependency",
            )
            destination = assets_dir / destination_name
            try:
                assets_dir.mkdir(parents=True, exist_ok=True)
                if source_path.resolve() != destination.resolve():
                    shutil.copy2(source_path, destination)
                seen_sources[source_path] = destination
            except Exception as exc:
                message = f"Failed to stage composition dependency '{source_path}': {exc}"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
                continue

        relative_path = Path(
            os.path.relpath(destination, start=destination_path.parent)
        ).as_posix()
        if package_member:
            relative_path = f"{relative_path}{package_member}"

        # Finish localizing a copied layer before pointing its parent at it.
        # Updating the parent first can trigger immediate stage recomposition
        # while the copied layer still contains broken source-relative paths,
        # temporarily dropping composed prims before attribute staging runs.
        if not package_member and source_path.suffix.lower() in _USD_LAYER_EXTENSIONS:
            try:
                source_dependency_layer = Sdf.Layer.FindOrOpen(str(source_path))
                destination_dependency_layer = Sdf.Layer.FindOrOpen(str(destination))
            except Exception as exc:
                message = f"Could not open staged layer '{source_path}': {exc}"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
                continue
            if not source_dependency_layer or not destination_dependency_layer:
                message = f"Could not open staged layer '{source_path}'"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
                continue
            _stage_layer_dependencies(
                source_layer=source_dependency_layer,
                destination_layer=destination_dependency_layer,
                destination_path=destination,
                assets_dir=assets_dir,
                seen_sources=seen_sources,
                seen_names=seen_names,
                processed=processed,
                failures=failures,
                writable_layer_paths=writable_layer_paths,
                texture_state=texture_state,
                diagnostics=diagnostics,
                save_destination=True,
            )

        # Re-authoring an identical arc can still force stage recomposition.
        # A copied layer may hold unsaved namespace edits at this point, so an
        # unnecessary reload would replace the normalized in-memory layer with
        # its pre-normalization bytes from disk.
        if authored_path == relative_path:
            continue

        try:
            updated = destination_layer.UpdateExternalReference(
                authored_path,
                relative_path,
            )
        except Exception as exc:
            updated = False
            message = (
                f"Failed to rewrite composition dependency '{authored_path}' "
                f"in {destination_path}: {exc}"
            )
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
        if not updated:
            if not any(authored_path in failure for failure in failures):
                message = (
                    f"Could not rewrite composition dependency '{authored_path}' "
                    f"in {destination_path}"
                )
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
            continue
        changed = True

    if changed and save_destination:
        try:
            destination_layer.Save()
        except Exception as exc:
            message = f"Failed to save staged layer '{destination_path}': {exc}"
            failures.append(message)
            _diagnostic_warning(diagnostics, message)


def _stage_asset_layer_dependencies(
    *,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    processed: set,
    failures: List[str],
    writable_layer_paths: set[str],
    texture_state,
    diagnostics=None,
) -> None:
    """Recursively localize USD layers discovered in asset-valued opinions."""
    for source_path, destination in list(seen_sources.items()):
        if source_path.suffix.lower() not in _USD_LAYER_EXTENSIONS:
            continue
        dependency_key = (source_path, destination.resolve())
        if dependency_key in processed:
            continue
        try:
            source_dependency_layer = Sdf.Layer.FindOrOpen(str(source_path))
            destination_dependency_layer = Sdf.Layer.FindOrOpen(str(destination))
        except Exception as exc:
            message = f"Could not open staged asset layer '{source_path}': {exc}"
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
            continue
        if not source_dependency_layer or not destination_dependency_layer:
            message = f"Could not open staged asset layer '{source_path}'"
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
            continue
        _stage_layer_dependencies(
            source_layer=source_dependency_layer,
            destination_layer=destination_dependency_layer,
            destination_path=destination,
            assets_dir=assets_dir,
            seen_sources=seen_sources,
            seen_names=seen_names,
            processed=processed,
            failures=failures,
            writable_layer_paths=writable_layer_paths,
            texture_state=texture_state,
            diagnostics=diagnostics,
            save_destination=True,
        )


def _stage_layer_authored_assets(
    *,
    source_layer,
    destination_layer,
    destination_path: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    texture_state,
    diagnostics=None,
) -> bool:
    """Rewrite asset-valued specs into an output-owned layer copy.

    Reading from ``source_layer`` is essential: once a parent composition arc
    is repointed at ``destination_layer``, an authored ``@blob.bin@`` no longer
    has the source layer's directory as its resolver anchor.  We therefore
    resolve every direct opinion while that original context is still known,
    and write only to the destination layer.
    """
    if Sdf is None or not hasattr(source_layer, "Traverse"):
        return False

    spec_paths = []
    try:
        source_layer.Traverse(
            Sdf.Path.absoluteRootPath,
            lambda path: spec_paths.append(path),
        )
    except Exception as exc:
        failures.append(
            f"Could not inspect authored assets in layer "
            f"'{getattr(source_layer, 'identifier', destination_path)}': {exc}"
        )
        return False

    changed = False
    destination_base = destination_path.resolve().parent
    asset_type = getattr(getattr(Sdf, "ValueTypeNames", None), "Asset", None)
    asset_array_type = getattr(
        getattr(Sdf, "ValueTypeNames", None), "AssetArray", None
    )

    for spec_path in spec_paths:
        source_spec = source_layer.GetObjectAtPath(spec_path)
        destination_spec = destination_layer.GetObjectAtPath(spec_path)
        if source_spec is None or destination_spec is None:
            continue

        if isinstance(source_spec, Sdf.AttributeSpec) and source_spec.typeName in {
            asset_type,
            asset_array_type,
        }:
            default_value = getattr(source_spec, "default", None)
            if default_value is not None:
                if source_spec.typeName == asset_array_type:
                    rewritten, value_changed = _rewrite_asset_array(
                        default_value,
                        destination_path.resolve().parent,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        authoring_layer=source_layer,
                        relative_to=destination_base,
                        texture_state=texture_state,
                        destination_layer_path=destination_path,
                    )
                else:
                    rewritten, value_changed = _rewrite_asset_value(
                        default_value,
                        destination_path.resolve().parent,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        authoring_layer=source_layer,
                        relative_to=destination_base,
                        texture_state=texture_state,
                        destination_layer_path=destination_path,
                    )
                if value_changed:
                    try:
                        destination_spec.default = rewritten
                        changed = True
                    except Exception as exc:
                        message = (
                            f"Failed to rewrite asset default at {spec_path} "
                            f"in '{destination_path}': {exc}"
                        )
                        failures.append(message)
                        _diagnostic_warning(diagnostics, message)

            try:
                sample_times = list(source_layer.ListTimeSamplesForPath(spec_path))
            except Exception as exc:
                sample_times = []
                message = f"Could not inspect time samples for {spec_path}: {exc}"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
            for sample_time in sample_times:
                value = source_layer.QueryTimeSample(spec_path, sample_time)
                if source_spec.typeName == asset_array_type:
                    rewritten, value_changed = _rewrite_asset_array(
                        value,
                        destination_path.resolve().parent,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        authoring_layer=source_layer,
                        relative_to=destination_base,
                        texture_state=texture_state,
                        destination_layer_path=destination_path,
                    )
                else:
                    rewritten, value_changed = _rewrite_asset_value(
                        value,
                        destination_path.resolve().parent,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        authoring_layer=source_layer,
                        relative_to=destination_base,
                        texture_state=texture_state,
                        destination_layer_path=destination_path,
                    )
                if value_changed:
                    try:
                        destination_layer.SetTimeSample(
                            spec_path, sample_time, rewritten
                        )
                        changed = True
                    except Exception as exc:
                        message = (
                            f"Failed to rewrite asset time sample at {spec_path} "
                            f"in '{destination_path}': {exc}"
                        )
                        failures.append(message)
                        _diagnostic_warning(diagnostics, message)

        # Asset paths may also appear recursively in authored metadata (value
        # clips are the important example). Composition list-ops are not
        # Sdf.AssetPath values and remain owned by the dedicated arc pass.
        try:
            info_keys = list(source_spec.ListInfoKeys())
        except Exception:
            info_keys = []
        for info_key in info_keys:
            if info_key in {"default", "timeSamples"}:
                continue
            try:
                value = source_spec.GetInfo(info_key)
            except Exception:
                # Some registered fields (notably subLayerOffsets in Blender's
                # OpenUSD build) have no Python by-value converter. They cannot
                # contain Sdf.AssetPath values and are left byte-for-byte as
                # copied in the destination layer.
                continue
            try:
                rewritten, value_changed = _rewrite_metadata_value(
                    value,
                    destination_path.resolve().parent,
                    assets_dir,
                    seen_sources,
                    seen_names,
                    failures,
                    diagnostics,
                    authoring_layer=source_layer,
                    relative_to=destination_base,
                    texture_state=texture_state,
                    destination_layer_path=destination_path,
                )
                if value_changed:
                    destination_spec.SetInfo(info_key, rewritten)
                    changed = True
            except Exception as exc:
                message = (
                    f"Failed to rewrite asset metadata '{info_key}' at "
                    f"{spec_path} in '{destination_path}': {exc}"
                )
                failures.append(message)
                _diagnostic_warning(diagnostics, message)

    return changed


def _stage_asset_attributes(
    stage,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    diagnostics=None,
) -> None:
    asset_type = getattr(getattr(Sdf, "ValueTypeNames", None), "Asset", None)
    asset_array_type = getattr(getattr(Sdf, "ValueTypeNames", None), "AssetArray", None)

    for prim in _traverse_all_prims(stage):
        for attr in prim.GetAttributes():
            type_name = attr.GetTypeName()
            if type_name not in {asset_type, asset_array_type}:
                continue

            samples: List[Tuple[Optional[object], object]] = [(None, attr.Get())]
            if hasattr(attr, "GetTimeSamples"):
                try:
                    samples.extend((time, attr.Get(time)) for time in attr.GetTimeSamples())
                except Exception as exc:
                    message = f"Could not read time samples for {attr.GetPath()}: {exc}"
                    failures.append(message)
                    _diagnostic_warning(diagnostics, message)

            for time_code, value in samples:
                if value is None:
                    continue
                if type_name == asset_array_type:
                    rewritten, changed = _rewrite_asset_array(
                        value,
                        usd_dir,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        owner=attr,
                        relative_to=usd_dir,
                    )
                else:
                    rewritten, changed = _rewrite_asset_value(
                        value,
                        usd_dir,
                        assets_dir,
                        seen_sources,
                        seen_names,
                        failures,
                        diagnostics,
                        owner=attr,
                        relative_to=usd_dir,
                    )
                if not changed:
                    continue
                try:
                    if time_code is None:
                        authored = attr.Set(rewritten)
                    else:
                        authored = attr.Set(rewritten, time_code)
                    if authored is False:
                        raise RuntimeError("USD rejected the authored value")
                except Exception as exc:
                    message = f"Failed to rewrite asset value at {attr.GetPath()}: {exc}"
                    failures.append(message)
                    _diagnostic_warning(diagnostics, message)


def _stage_asset_metadata(
    stage,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    diagnostics=None,
) -> None:
    """Localize asset paths embedded in authored metadata, including clips."""
    prims = list(_traverse_all_prims(stage))
    if hasattr(stage, "GetPseudoRoot"):
        prims.insert(0, stage.GetPseudoRoot())

    for prim in prims:
        metadata = {}
        if hasattr(prim, "GetAllAuthoredMetadata"):
            try:
                metadata.update(prim.GetAllAuthoredMetadata())
            except Exception as exc:
                message = f"Could not inspect metadata for {prim.GetPath()}: {exc}"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)
                continue

        # `clips` is registered metadata and is not returned by
        # GetAllAuthoredMetadata in current OpenUSD builds.
        if hasattr(prim, "HasMetadata") and prim.HasMetadata("clips"):
            clips = prim.GetMetadata("clips")
            if clips is not None:
                metadata["clips"] = clips

        for key, value in metadata.items():
            rewritten, changed = _rewrite_metadata_value(
                value,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                diagnostics,
                owner=prim,
                relative_to=usd_dir,
            )
            if not changed:
                continue
            try:
                authored = prim.SetMetadata(key, rewritten)
                if authored is False:
                    raise RuntimeError("USD rejected the authored metadata")
            except Exception as exc:
                message = f"Failed to rewrite metadata '{key}' at {prim.GetPath()}: {exc}"
                failures.append(message)
                _diagnostic_warning(diagnostics, message)


def _rewrite_metadata_value(
    value,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    diagnostics=None,
    *,
    owner=None,
    authoring_layer=None,
    relative_to: Optional[Path] = None,
    texture_state=None,
    destination_layer_path: Optional[Path] = None,
):
    if Sdf is not None and isinstance(value, Sdf.AssetPath):
        return _rewrite_asset_value(
            value,
            usd_dir,
            assets_dir,
            seen_sources,
            seen_names,
            failures,
            diagnostics,
            owner=owner,
            authoring_layer=authoring_layer,
            relative_to=relative_to,
            texture_state=texture_state,
            destination_layer_path=destination_layer_path,
        )

    asset_array_class = getattr(Sdf, "AssetPathArray", None)
    if asset_array_class and isinstance(value, asset_array_class):
        return _rewrite_asset_array(
            value,
            usd_dir,
            assets_dir,
            seen_sources,
            seen_names,
            failures,
            diagnostics,
            owner=owner,
            authoring_layer=authoring_layer,
            relative_to=relative_to,
            texture_state=texture_state,
            destination_layer_path=destination_layer_path,
        )

    if isinstance(value, dict):
        rewritten = {}
        changed = False
        for key, item in value.items():
            new_item, item_changed = _rewrite_metadata_value(
                item,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                diagnostics,
                owner=owner,
                authoring_layer=authoring_layer,
                relative_to=relative_to,
                texture_state=texture_state,
                destination_layer_path=destination_layer_path,
            )
            rewritten[key] = new_item
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if isinstance(value, (list, tuple)):
        rewritten_items = []
        changed = False
        for item in value:
            new_item, item_changed = _rewrite_metadata_value(
                item,
                usd_dir,
                assets_dir,
                seen_sources,
                seen_names,
                failures,
                diagnostics,
                owner=owner,
                authoring_layer=authoring_layer,
                relative_to=relative_to,
                texture_state=texture_state,
                destination_layer_path=destination_layer_path,
            )
            rewritten_items.append(new_item)
            changed = changed or item_changed
        if not changed:
            return value, False
        return (tuple(rewritten_items) if isinstance(value, tuple) else rewritten_items), True

    return value, False


def _rewrite_asset_array(
    value: Sequence,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    diagnostics=None,
    *,
    owner=None,
    authoring_layer=None,
    relative_to: Optional[Path] = None,
    texture_state=None,
    destination_layer_path: Optional[Path] = None,
):
    rewritten = []
    changed = False
    for item in value:
        new_item, item_changed = _rewrite_asset_value(
            item,
            usd_dir,
            assets_dir,
            seen_sources,
            seen_names,
            failures,
            diagnostics,
            owner=owner,
            authoring_layer=authoring_layer,
            relative_to=relative_to,
            texture_state=texture_state,
            destination_layer_path=destination_layer_path,
        )
        rewritten.append(new_item)
        changed = changed or item_changed
    if not changed:
        return value, False
    asset_array_class = getattr(Sdf, "AssetPathArray", None)
    return (asset_array_class(rewritten) if asset_array_class else rewritten), True


def _rewrite_asset_value(
    value,
    usd_dir: Path,
    assets_dir: Path,
    seen_sources: Dict[Path, Path],
    seen_names: Dict[str, Path],
    failures: List[str],
    diagnostics=None,
    *,
    owner=None,
    authoring_layer=None,
    relative_to: Optional[Path] = None,
    texture_state=None,
    destination_layer_path: Optional[Path] = None,
):
    authored_path, resolved_path = _asset_value_paths(value)
    if not authored_path:
        return value, False
    outer_authored_path, package_member = _split_package_relative_path(
        authored_path
    )
    outer_resolved_path, _resolved_member = _split_package_relative_path(
        resolved_path
    )
    if _is_texture_path(outer_authored_path):
        if texture_state is not None and authoring_layer is not None:
            return usd_textures.stage_layer_texture_asset(
                authored_path,
                resolved_path,
                authoring_layer=authoring_layer,
                destination_layer_path=(destination_layer_path or usd_dir),
                state=texture_state,
                diagnostics=diagnostics,
            )
        return value, False
    if _is_non_file_asset(outer_authored_path):
        message = f"Non-file asset dependency cannot be packaged: {authored_path}"
        failures.append(message)
        _diagnostic_warning(diagnostics, message)
        return value, False

    source_path = _resolve_asset_dependency(
        owner=owner,
        authoring_layer=authoring_layer,
        authored_path=outer_authored_path,
        resolved_path=outer_resolved_path,
        fallback_dir=usd_dir,
    )

    if not source_path.name:
        return value, False
    if not source_path.is_file():
        message = f"Asset dependency not found: {source_path}"
        failures.append(message)
        _diagnostic_warning(diagnostics, message)
        return value, False

    destination = seen_sources.get(source_path)
    if destination is None:
        destination_name = _unique_destination_name(
            source_path,
            seen_names,
            diagnostics,
            "asset",
        )
        destination = assets_dir / destination_name
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
            if source_path.resolve() != destination.resolve():
                shutil.copy2(source_path, destination)
            seen_sources[source_path] = destination
        except Exception as exc:
            message = f"Failed to stage asset '{source_path}': {exc}"
            failures.append(message)
            _diagnostic_warning(diagnostics, message)
            return value, False

    relative_path = Path(
        os.path.relpath(destination, start=(relative_to or usd_dir))
    ).as_posix()
    if package_member:
        relative_path = f"{relative_path}{package_member}"
    if authored_path == relative_path:
        return value, False
    return Sdf.AssetPath(relative_path), True


def _asset_value_paths(value) -> Tuple[str, str]:
    if Sdf is not None and isinstance(value, Sdf.AssetPath):
        return str(value.path or ""), str(value.resolvedPath or "")
    if value:
        return str(value), ""
    return "", ""


def _resolve_asset_dependency(
    *,
    owner,
    authoring_layer,
    authored_path: str,
    resolved_path: str,
    fallback_dir: Path,
) -> Path:
    """Resolve an asset using its authored layer before the export root.

    ``Sdf.AssetPath.resolvedPath`` is authoritative when populated.  Direct
    layer-spec traversal supplies ``authoring_layer``; the fake/composed-stage
    fallback instead derives candidate layers from the owning property/prim.
    """
    if resolved_path:
        candidate = Path(_normalize_file_url(resolved_path))
        if not candidate.is_absolute():
            candidate = fallback_dir / candidate
        return candidate.resolve()

    candidate_layers = []
    if authoring_layer is not None:
        candidate_layers.append(authoring_layer)
    if owner is not None:
        for stack_method in ("GetPropertyStack", "GetPrimStack"):
            method = getattr(owner, stack_method, None)
            if not callable(method):
                continue
            try:
                specs = list(method())
            except Exception:
                continue
            for spec in specs:
                layer = getattr(spec, "layer", None)
                if layer is not None and layer not in candidate_layers:
                    candidate_layers.append(layer)

    normalized = _normalize_file_url(authored_path)
    fallback_candidate = None
    for layer in candidate_layers:
        try:
            computed = layer.ComputeAbsolutePath(normalized) or normalized
        except Exception:
            computed = normalized
        candidate = Path(_normalize_file_url(str(computed)))
        if not candidate.is_absolute():
            layer_path = _layer_filesystem_path(layer)
            if layer_path is not None:
                candidate = layer_path.parent / candidate
        candidate = candidate.resolve()
        if fallback_candidate is None:
            fallback_candidate = candidate
        if candidate.is_file():
            return candidate

    if fallback_candidate is not None:
        return fallback_candidate
    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = fallback_dir / candidate
    return candidate.resolve()


def _split_package_relative_path(asset_path: str) -> Tuple[str, str]:
    """Return the package file and bracketed member suffix of a USD path."""
    value = str(asset_path or "")
    bracket = value.find("[")
    if bracket <= 0 or not value.endswith("]"):
        return value, ""
    return value[:bracket], value[bracket:]


def _resolve_layer_dependency(layer, authored_path: str) -> Optional[Path]:
    normalized = _normalize_file_url(authored_path)
    try:
        if hasattr(layer, "ComputeAbsolutePath"):
            normalized = layer.ComputeAbsolutePath(normalized) or normalized
    except Exception:
        pass
    candidate = Path(normalized)
    if not candidate.is_absolute():
        layer_path = _layer_filesystem_path(layer)
        if layer_path:
            candidate = layer_path.parent / candidate
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _layer_filesystem_path(layer) -> Optional[Path]:
    for field in ("realPath", "resolvedPath", "identifier"):
        value = getattr(layer, field, None)
        if value and not _is_non_file_asset(str(value)):
            normalized = _normalize_file_url(str(value))
            if "[" not in normalized:
                return Path(normalized).resolve()
    return None


def _is_non_file_asset(asset_path: str) -> bool:
    """Return True if the asset path is not a local file path."""
    return str(asset_path).lower().startswith(NON_FILE_PREFIXES)


def _normalize_file_url(asset_path: str) -> str:
    """Convert file:// URLs to filesystem paths when needed."""
    if asset_path.startswith("file://"):
        parsed = urlparse(asset_path)
        return unquote(url2pathname(parsed.path))
    return asset_path


def _unique_destination_name(path: Path, used: dict, diagnostics=None, label: str = "asset") -> str:
    """Return a deterministic unique filename, avoiding collisions."""
    name = path.name
    name_key = _destination_name_key(name)
    existing = used.get(name_key)
    if existing is None or existing == path:
        used[name_key] = path
        return name

    stem = path.stem
    suffix = path.suffix
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    candidate = f"{stem}_{digest}{suffix}"
    counter = 1
    candidate_key = _destination_name_key(candidate)
    while candidate_key in used and used[candidate_key] != path:
        candidate = f"{stem}_{digest}_{counter}{suffix}"
        candidate_key = _destination_name_key(candidate)
        counter += 1
    used[candidate_key] = path

    if diagnostics:
        diagnostics.add_warning(
            f"Renamed {label} '{name}' to '{candidate}' to avoid a name collision."
        )

    return candidate


def _destination_name_key(name: str) -> str:
    """Match default Apple filesystem case and Unicode normalization rules."""
    return unicodedata.normalize("NFC", name).casefold()


def _diagnostic_warning(diagnostics, message: str) -> None:
    if diagnostics:
        diagnostics.add_warning(message)


def _traverse_all_prims(stage):
    """Include inactive/undefined prims when the OpenUSD build supports it."""
    if hasattr(stage, "TraverseAll"):
        return stage.TraverseAll()
    return stage.Traverse()
