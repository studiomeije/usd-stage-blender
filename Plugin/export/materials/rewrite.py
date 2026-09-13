"""
Material rewrite orchestration for USD stages.
"""

from ..usd_utils import Usd, UsdShade, Sdf, UsdGeom
from ..usd_hook import consume_captured_material_map
from ..usd_textures import require_safe_texture_alpha_staging_policy
from ...material_policies import (
    normalize_extracted_specular_tint,
    specular_tint_normalization_message,
)
from ...manifest.materialx_nodes import load_manifest
from .graph import MaterialXGraphBuilder
from .extract import extract_blender_material_data, collect_material_warnings
from .extract.core import working_color_space_refusal
from .author import create_materialx_material
from .helpers import _get_blender_data_name
from .mapping import require_realitykit_mapping_contract


def rewrite_materials(stage, settings, context, diagnostics=None) -> None:
    """Rewrite materials to MaterialX graphs (Pass 2)."""
    manifest = load_manifest()
    builder = MaterialXGraphBuilder(
        manifest,
        diagnostics,
    )
    force_unlit = bool(getattr(settings, "force_unlit_materials", False))
    clamp_specular_tint = bool(
        getattr(settings, "clamp_specular_tint", False)
    )

    blender_materials = {
        material.name: material
        for material in context.blend_data.materials
        if material
    }
    # Exact {material prim path: Blender material name} recorded by the
    # USDHook during export; empty when the export ran without the hook.
    hook_material_map = consume_captured_material_map() or {}

    failures = []
    rewrite_targets = []
    for material_prim in _bound_material_prims(stage, diagnostics):
        material_name = material_prim.GetName()
        blender_name = _get_blender_data_name(material_prim) or material_name
        material_key = str(material_prim.GetPath())

        mapped_name = hook_material_map.get(material_key)
        blender_material = (
            blender_materials.get(mapped_name) if mapped_name else None
        )
        if blender_material:
            blender_name = mapped_name
        else:
            blender_material = (
                blender_materials.get(blender_name)
                or blender_materials.get(material_name)
            )
        if not blender_material:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                "No Blender material mapping was available for this bound "
                "USD Material.",
            )
            continue
        rewrite_targets.append(
            (material_prim, blender_material, blender_name, material_key)
        )

    # Resolve, validate, extract, and build every used material before the
    # stage receives its first MaterialX opinion. Graph failures therefore
    # cannot leave an earlier material partially converted.
    prepared_targets = []
    color_space_refusal = working_color_space_refusal(
        getattr(context, "blend_data", None)
    )
    for (
        material_prim,
        blender_material,
        blender_name,
        material_key,
    ) in rewrite_targets:
        if color_space_refusal is not None:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                color_space_refusal + ".",
            )
            continue
        try:
            _require_safe_material_path(material_prim, blender_name)
        except Exception as exc:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                str(exc),
            )
            continue

        try:
            warnings = collect_material_warnings(blender_material)
            if diagnostics:
                for warning in warnings:
                    diagnostics.add_warning(warning)
            material_data = extract_blender_material_data(blender_material)
            if clamp_specular_tint:
                normalization = normalize_extracted_specular_tint(material_data)
                if normalization is not None and diagnostics:
                    message = specular_tint_normalization_message(normalization)
                    warning = f"{blender_name}: {message}"
                    if warning not in diagnostics.data.get("warnings", []):
                        diagnostics.add_warning(warning)
                    diagnostics.add_material_issue(
                        "normalizations",
                        material=blender_name,
                        input="Specular Tint",
                        source_value=normalization["input"],
                        exported_value=normalization["output"],
                        source_blend_unchanged=True,
                        message=message,
                    )
        except Exception as exc:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                f"Material extraction failed: {exc}",
            )
            continue

        unresolved = material_data.get("unresolved_warnings") or []
        if unresolved:
            reason = "; ".join(str(warning) for warning in unresolved)
            if diagnostics:
                for warning in unresolved:
                    diagnostics.add_warning(str(warning))
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                f"Material graph contains unresolved input(s): {reason}",
            )
            continue

        try:
            _require_safe_material_texture_policy(
                material_data,
                settings,
                diagnostics,
            )
        except Exception as exc:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                str(exc),
            )
            continue

        try:
            graph = _build_material_graph(
                builder,
                material_data,
                force_unlit=force_unlit,
            )
            if not graph:
                raise ValueError(
                    f"Material type '{material_data.get('type')}' could not be "
                    "mapped to a RealityKit graph."
                )
            require_realitykit_mapping_contract(graph, blender_name)
        except Exception as exc:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                f"MaterialX graph construction failed: {exc}",
            )
            continue

        prepared_targets.append(
            (blender_name, material_key, material_data, graph)
        )

    if failures:
        _raise_material_rewrite_failures(failures)
    if not prepared_targets:
        return

    edit_layer = stage.GetEditTarget().GetLayer()
    if not edit_layer:
        raise RuntimeError(
            "MaterialX rewrite failed: the USD stage has no writable edit layer."
        )
    backup_layer = Sdf.Layer.CreateAnonymous(
        "USDStage-material-rewrite-backup.usda"
    )
    backup_layer.TransferContent(edit_layer)

    authored_materials = []
    for blender_name, material_key, material_data, graph in prepared_targets:
        try:
            new_material = create_materialx_material(
                stage,
                material_key,
                blender_name,
                graph,
                manifest,
                diagnostics,
            )
            _require_authored_materialx_surface(
                new_material,
                material_key,
                blender_name,
            )
            _remove_preview_network(stage, new_material)
            authored_materials.append(blender_name)
        except Exception as exc:
            _record_material_failure(
                diagnostics,
                failures,
                blender_name,
                material_key,
                f"MaterialX authoring failed: {exc}",
            )

    if failures:
        try:
            edit_layer.TransferContent(backup_layer)
        except Exception as rollback_exc:
            raise RuntimeError(
                _format_material_rewrite_failures(failures)
                + f"\nUSD material rewrite rollback also failed: {rollback_exc}"
            ) from rollback_exc
        _raise_material_rewrite_failures(failures)

    if diagnostics:
        for blender_name in authored_materials:
            diagnostics.add_material_converted(blender_name)

    # create_materialx_material authors at the existing Material path. The
    # original binding therefore remains valid. Rebinding here would author a
    # stronger relationship outside a variant edit context and silently
    # override every inactive Red/Blue (or similar) selection.


def _build_material_graph(builder, material_data, *, force_unlit):
    """Build one graph without mutating the USD stage."""
    if force_unlit and material_data['type'] in {
        'principled',
        'emission',
        'hair',
    }:
        return builder.build_unlit_material(material_data)
    if material_data['type'] == 'principled':
        return builder.build_pbr_material(material_data)
    if material_data['type'] == 'hair':
        return builder.build_hair_material(material_data)
    if material_data['type'] == 'emission':
        return builder.build_unlit_material(material_data)
    if material_data['type'] == 'rk_graph':
        return builder.build_rk_graph(material_data.get('rk_graph'))
    if material_data['type'] == 'rk_group':
        return builder.build_rk_material(
            material_data.get('rk_node_id'),
            material_data.get('rk_inputs', {}),
        )
    return None


def _require_authored_materialx_surface(material, material_path, blender_name):
    """Verify one authoring call produced the in-place MaterialX surface."""
    if not material:
        raise RuntimeError("the author returned no Material")
    if str(material.GetPath()) != material_path:
        raise RuntimeError(
            f"the author returned {material.GetPath()} instead of {material_path}"
        )
    output = material.GetSurfaceOutput("mtlx")
    if not output or not output.GetConnectedSource():
        raise RuntimeError(
            f"Material '{blender_name}' has no connected mtlx surface output"
        )


def _record_material_failure(
    diagnostics,
    failures,
    material_name,
    material_path,
    reason,
):
    """Record one used-material failure in diagnostics and the fatal batch."""
    reason = str(reason)
    failures.append((str(material_name), str(material_path), reason))
    if diagnostics:
        diagnostics.add_material_failed(str(material_name), reason)


def _format_material_rewrite_failures(failures):
    lines = [
        f"MaterialX rewrite failed for {len(failures)} used material(s):"
    ]
    lines.extend(
        f"- {material_name} ({material_path}): {reason}"
        for material_name, material_path, reason in failures
    )
    return "\n".join(lines)


def _raise_material_rewrite_failures(failures):
    raise RuntimeError(_format_material_rewrite_failures(failures))


def _require_safe_material_texture_policy(
    material_data,
    settings,
    diagnostics=None,
) -> None:
    semantic_error = material_data.get("base_color_alpha_semantics_error")
    sources = list(material_data.get("base_color_texture_sources") or [])
    if not sources and material_data.get("base_color_texture"):
        sources.append(
            {
                "path": material_data["base_color_texture"],
                "alpha_mode": material_data.get(
                    "base_color_texture_alpha_mode"
                ),
            }
        )

    # Extraction normally records this recursive closure. Retain a defensive
    # walk for callers constructing material payloads directly and for future
    # expression node types: only baseColor/color branches carry the
    # surface-wide premultiplied-alpha semantic.
    def texture_leaves(value):
        if not isinstance(value, dict):
            return []
        leaves = []
        if (
            value.get("kind") == "texture"
            or value.get("type") in {"texture", "normal_texture"}
        ) and value.get("path"):
            leaves.append(
                {
                    "path": value["path"],
                    "alpha_mode": value.get("alpha_mode"),
                }
            )
        for child in (value.get("inputs") or {}).values():
            leaves.extend(texture_leaves(child))
        return leaves

    if not sources:
        for input_name, expression in (
            material_data.get("input_graphs") or {}
        ).items():
            normalized = str(input_name).replace("_", "").lower()
            if normalized in {"basecolor", "color"}:
                sources.extend(texture_leaves(expression))
        for input_name, value in (material_data.get("rk_inputs") or {}).items():
            normalized = str(input_name).replace("_", "").lower()
            if normalized in {"basecolor", "color"}:
                sources.extend(texture_leaves(value))
        rk_graph = material_data.get("rk_graph")
        if rk_graph:
            from .extract import core as extract_core

            sources.extend(
                extract_core._rk_graph_base_color_texture_sources(rk_graph)
            )

    unique_sources = []
    seen = set()
    for source in sources:
        path = str(source.get("path") or "")
        if not path:
            continue
        alpha_mode = str(source.get("alpha_mode") or "").strip().lower() or None
        key = (path, alpha_mode)
        if key in seen:
            continue
        seen.add(key)
        unique_sources.append({"path": path, "alpha_mode": alpha_mode})

    modes = {
        source["alpha_mode"]
        for source in unique_sources
        if source.get("alpha_mode")
    }
    if semantic_error or (
        "premul" in modes and any(mode != "premul" for mode in modes)
    ):
        raise RuntimeError(
            f"{semantic_error or 'Base Color mixes incompatible alpha conventions.'} "
            "RealityKit has one material-level hasPremultipliedAlpha flag. "
            "Bake Base Color and Alpha to one PNG, or make every contributing "
            "texture use the same straight-alpha convention."
        )

    material_is_premultiplied = bool(
        material_data.get("has_premultiplied_alpha")
    )
    for source in unique_sources:
        source_is_premultiplied = source["alpha_mode"] == "premul"
        if not source_is_premultiplied:
            # A single legacy/direct source may carry only the material flag.
            source_is_premultiplied = (
                material_is_premultiplied
                and len(unique_sources) == 1
                and source["alpha_mode"] is None
            )
        if not source_is_premultiplied:
            continue
        require_safe_texture_alpha_staging_policy(
            source["path"],
            alpha_mode=source["alpha_mode"],
            has_premultiplied_alpha=True,
            settings=settings,
            diagnostics=diagnostics,
        )


def _bound_material_prims(stage, diagnostics=None):
    """Return every used, directly bound Material prim exactly once.

    Ordinary stage traversal omits Blender 5.2's abstract ``prototypes`` class
    and stops at instance roots.  Visit referenced class definitions and any
    remaining OpenUSD prototypes, while suppressing the read-only prototype
    generated from an already visited class definition.  Inactive variant
    relationship specs are inspected at the Sdf layer level so their material
    targets are rewritten without changing the active selection.
    """
    prims = _material_namespace_prims(stage)
    owners = [
        prim
        for prim in prims
        if prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Subset)
    ]

    materials = {}
    for prim in owners:
        bound_material = (
            UsdShade.MaterialBindingAPI(prim)
            .GetDirectBinding()
            .GetMaterial()
        )
        if not bound_material:
            continue
        material_prim = bound_material.GetPrim()
        materials.setdefault(str(material_prim.GetPath()), material_prim)

    try:
        variant_materials = _variant_bound_material_prims(stage, owners, prims)
    except RuntimeError as exc:
        if diagnostics:
            diagnostics.add_error(str(exc))
        raise
    for material_prim in variant_materials:
        materials.setdefault(str(material_prim.GetPath()), material_prim)

    return list(materials.values())


def _material_namespace_prims(stage):
    """Collect ordinary, used-class, and non-duplicated prototype prims."""
    prims = []
    seen_paths = set()

    def append_range(prim_range):
        for prim in prim_range:
            path = str(prim.GetPath())
            if path in seen_paths:
                continue
            seen_paths.add(path)
            prims.append(prim)

    append_range(list(stage.Traverse()))
    class_backed_prototypes = set()

    if hasattr(stage, "TraverseAll"):
        all_namespace_prims = [
            prim
            for prim in stage.TraverseAll()
            if prim.IsActive() and prim.IsLoaded() and prim.IsDefined()
        ]
        abstract_roots, class_backed_prototypes = _referenced_abstract_roots(
            stage,
            all_namespace_prims,
        )
        for abstract_root in abstract_roots:
            append_range(
                prim
                for prim in all_namespace_prims
                if prim.IsAbstract()
                and prim.GetPath().HasPrefix(abstract_root.GetPath())
            )

    if Usd is not None and hasattr(stage, "GetPrototypes"):
        for prototype in stage.GetPrototypes():
            if str(prototype.GetPath()) in class_backed_prototypes:
                continue
            append_range(Usd.PrimRange(prototype))

    return prims


def _referenced_abstract_roots(stage, prims):
    """Find active internal references to Blender's abstract prototypes."""
    roots = {}
    class_backed_prototypes = set()
    for prim in prims:
        if prim.IsAbstract():
            continue
        try:
            references = prim.GetMetadata("references")
            items = list(references.GetAddedOrExplicitItems()) if references else []
        except Exception:
            items = []
        for reference in items:
            if str(getattr(reference, "assetPath", "") or ""):
                continue
            target_path = getattr(reference, "primPath", None)
            if not target_path or bool(getattr(target_path, "isEmpty", False)):
                continue
            target = stage.GetPrimAtPath(target_path)
            if not target or not target.IsAbstract():
                continue
            if not (target.IsActive() and target.IsLoaded() and target.IsDefined()):
                continue
            roots[str(target.GetPath())] = target
            if prim.IsInstance():
                prototype = prim.GetPrototype()
                if prototype:
                    class_backed_prototypes.add(str(prototype.GetPath()))
    return list(roots.values()), class_backed_prototypes


def _variant_bound_material_prims(stage, owners, namespace_prims):
    """Resolve direct material targets authored in every variant branch."""
    if Sdf is None or not hasattr(stage, "GetUsedLayers"):
        return []

    eligible_owners = set()
    for owner in owners:
        for spec in owner.GetPrimStack():
            eligible_owners.add(
                (
                    str(spec.layer.identifier),
                    str(spec.path.StripAllVariantSelections()),
                )
            )

    material_specs = {}
    for prim in namespace_prims:
        if not prim.IsA(UsdShade.Material):
            continue
        for spec in prim.GetPrimStack():
            material_specs.setdefault(
                (
                    str(spec.layer.identifier),
                    str(spec.path.StripAllVariantSelections()),
                ),
                prim,
            )

    found = {}
    unresolved = []
    for layer in stage.GetUsedLayers():
        if not hasattr(layer, "Traverse"):
            continue
        spec_paths = []
        layer.Traverse(
            Sdf.Path.absoluteRootPath,
            lambda path: spec_paths.append(path),
        )
        layer_id = str(layer.identifier)
        for spec_path in spec_paths:
            if not spec_path.ContainsPrimVariantSelection():
                continue
            spec = layer.GetObjectAtPath(spec_path)
            if not isinstance(spec, Sdf.RelationshipSpec):
                continue
            if str(spec.name) != "material:binding":
                continue
            owner_path = spec.path.GetPrimPath().StripAllVariantSelections()
            owner_is_composed = (layer_id, str(owner_path)) in eligible_owners
            owner_spec = layer.GetObjectAtPath(spec.path.GetPrimPath())
            owner_type = (
                str(owner_spec.typeName)
                if isinstance(owner_spec, Sdf.PrimSpec)
                else ""
            )
            # A Mesh or GeomSubset created only by an inactive ancestor
            # variant has no composed Usd.Prim and therefore cannot appear in
            # ``owners``.  Its authored PrimSpec still carries the exact schema
            # type, so include that relationship without broadening discovery
            # to arbitrary inactive Xforms or relationship owners.
            if not owner_is_composed and owner_type not in {"Mesh", "GeomSubset"}:
                continue
            try:
                targets = list(spec.targetPathList.GetAddedOrExplicitItems())
            except Exception:
                targets = []
            for target in targets:
                if not target.IsAbsolutePath():
                    target = target.MakeAbsolutePath(owner_path)
                target = target.GetPrimPath().StripAllVariantSelections()
                material_prim = stage.GetPrimAtPath(target)
                if not material_prim or not material_prim.IsA(UsdShade.Material):
                    material_prim = material_specs.get((layer_id, str(target)))
                if not material_prim or not material_prim.IsA(UsdShade.Material):
                    unresolved.append(f"{spec.path} -> {target}")
                    continue
                found.setdefault(str(material_prim.GetPath()), material_prim)

    if unresolved:
        details = "; ".join(dict.fromkeys(unresolved))
        raise RuntimeError(
            "Cannot safely rewrite inactive variant material binding(s): "
            f"{details}. Define each Material outside the variant and vary only "
            "the material:binding relationship."
        )
    return list(found.values())


def _require_safe_material_path(material_prim, blender_name):
    """Reject composed material paths that cannot be edited in place safely."""
    if material_prim.IsInPrototype() or material_prim.IsInstanceProxy():
        raise RuntimeError(
            f"Material '{blender_name}' at {material_prim.GetPath()} exists only "
            "inside a read-only OpenUSD prototype. Author it in a Blender 5.2 "
            "class prototype or a writable referenced layer before export."
        )
    if any(
        spec.path.ContainsPrimVariantSelection()
        for spec in material_prim.GetPrimStack()
    ):
        raise RuntimeError(
            f"Material '{blender_name}' at {material_prim.GetPath()} is defined "
            "inside a variant. Define the Material outside the variant and vary "
            "only the material:binding relationship."
        )


def _connected_material_network_paths(stage, output, material_path: str):
    """Collect prims reachable from one material output inside the material."""
    if not output:
        return set()
    prefix = f"{material_path.rstrip('/')}/"
    pending = list(output.GetAttr().GetConnections())
    paths = set()
    while pending:
        property_path = pending.pop()
        prim_path = str(property_path.GetPrimPath())
        if not prim_path.startswith(prefix) or prim_path in paths:
            continue
        prim = stage.GetPrimAtPath(prim_path)
        if not prim or not prim.IsValid():
            continue
        paths.add(prim_path)
        for attr in prim.GetAttributes():
            pending.extend(attr.GetConnections())
    return paths


#: The USD preview-surface schema family Blender's exporter authors beside our
#: graph. Nothing in an export may keep one.
_PREVIEW_SHADER_IDS = frozenset({
    "UsdPreviewSurface",
    "UsdUVTexture",
    "UsdTransform2d",
    "UsdPrimvarReader_float",
    "UsdPrimvarReader_float2",
    "UsdPrimvarReader_float3",
    "UsdPrimvarReader_float4",
    "UsdPrimvarReader_int",
    "UsdPrimvarReader_string",
    "UsdPrimvarReader_normal",
    "UsdPrimvarReader_point",
    "UsdPrimvarReader_vector",
    "UsdPrimvarReader_matrix",
})


def _remove_preview_network(stage, material) -> None:
    """Delete Blender's UsdPreviewSurface network from a translated material.

    Blender's USD exporter writes one beside the MaterialX graph, on the
    universal render context. RealityKit and Reality Composer Pro never read
    it, and keeping it causes harm: a texture on its displacement input makes
    the editor discard the whole material, its readers' colour spaces fail
    preflight, and every import prints a line per material.

    Everything reachable from the MaterialX surface or the geometry modifier is
    kept, whatever its id, so a shared prim can never be lost.
    """
    from ..usd_utils import UsdShade

    material_path = str(material.GetPath())
    keep = _connected_material_network_paths(
        stage, material.GetSurfaceOutput("mtlx"), material_path
    )
    keep |= _connected_material_network_paths(
        stage, material.GetOutput("realitykit:vertex"), material_path
    )

    doomed = set()
    material_prim = material.GetPrim()
    for output_name in ("outputs:surface", "outputs:displacement", "outputs:volume"):
        attribute = material_prim.GetAttribute(output_name)
        if not attribute:
            continue
        output = UsdShade.Output(attribute)
        doomed |= _connected_material_network_paths(stage, output, material_path)
        attribute.ClearConnections()
        material_prim.RemoveProperty(output_name)

    # Blender also leaves readers no terminal reaches; the id says what they are.
    pending = list(material_prim.GetAllChildren())
    while pending:
        prim = pending.pop()
        pending.extend(prim.GetAllChildren())
        shader = UsdShade.Shader(prim)
        if shader and str(shader.GetIdAttr().Get() or "") in _PREVIEW_SHADER_IDS:
            doomed.add(str(prim.GetPath()))

    for prim_path in sorted(doomed - keep, key=lambda value: value.count('/'), reverse=True):
        if stage.GetPrimAtPath(prim_path):
            stage.RemovePrim(prim_path)
