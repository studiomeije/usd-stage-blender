"""Authoring declared variant sets onto the exported stage.

The plan side of this lives in :mod:`variants`, which imports no OpenUSD. This
is the half that writes: it turns each declared set into a USD ``variantSet``
on the asset's root prim, where every variant activates its own members and
deactivates its siblings.

Two properties make that safe, and both are deliberate:

* ``active`` is authored **only** inside a variant edit context. Variants sit
  low in composition strength, so any opinion in the local layer stack would
  beat them silently - the trap Houdini warns about and the preflight refuses.
  Blender authors no ``active`` of its own, so the variants are unopposed.
* Members are resolved through the exporter's own prim map rather than by name,
  and anything that cannot be placed exactly is refused rather than guessed.
"""

from __future__ import annotations

from .usd_utils import Sdf, UsdShade, require_pxr
from .usd_scene import remap_prim_path
from .variants import MATERIAL, VariantPlan, VariantSetPlan, suggest_identifier


class VariantAuthoringError(RuntimeError):
    """Raised when declared variant sets cannot be written to the stage."""


def author_variant_sets(
    stage,
    plan: VariantPlan,
    object_map: dict | None,
    path_edits=(),
    settings=None,
    diagnostics=None,
) -> tuple[str, ...]:
    """Author every declared variant set on the stage's root prim.

    ``object_map`` is the USDHook's {prim path: object name} capture and
    ``path_edits`` the renames ``normalize_scene`` applied after it; together
    they place each member exactly. Returns the set names authored, which may
    be fewer than declared when a selected-only export left a whole set out.
    """
    if plan.is_empty or all(s.kind == MATERIAL for s in plan.sets):
        return ()
    require_pxr()

    root = stage.GetDefaultPrim()
    if not root:
        raise VariantAuthoringError(
            "Variant sets are authored on the asset's root prim, and this "
            "stage has no default prim."
        )

    if object_map is None:
        raise VariantAuthoringError(
            "Variant sets cannot be authored: this export recorded no prim "
            "map, so there is no exact way to say which prim each member "
            "became. Authoring by name instead would be a guess."
        )

    prim_paths = _member_prim_paths(stage, object_map, path_edits)
    selected_only = bool(getattr(settings, "selected_objects_only", False))

    # Gathered before authoring: once a variant deactivates a member, its
    # subtree is no longer composed and cannot be inspected at all.
    animated = _members_with_animation(stage, prim_paths, settings)

    authored: list[str] = []
    for variant_set in plan.sets:
        if variant_set.kind == MATERIAL:
            # Authored earlier, before the MaterialX rewrite.
            continue
        resolved = _resolve_set(variant_set, prim_paths, selected_only, diagnostics)
        if resolved is None:
            continue
        _require_no_nested_members(variant_set, resolved)
        _author_one_set(stage, root, variant_set, resolved)
        authored.append(variant_set.name)

    if diagnostics and authored:
        _warn_about_animated_members(plan, animated, authored, diagnostics)
    return tuple(authored)


def author_material_variants(
    stage,
    plan: VariantPlan,
    object_map: dict | None,
    path_edits=(),
    settings=None,
    diagnostics=None,
) -> tuple[str, ...]:
    """Author the material sets: each variant rebinds the set's targets.

    This runs *before* the MaterialX rewrite, unlike geometry variants, and
    for two reasons. The rewrite collects the materials it must convert from
    what meshes bind - including bindings inside inactive variants, which it
    already knows how to read - so a finish that no object wears in Blender
    reaches MaterialX only if its binding exists by then. And the rewrite
    authors in place, so converting a material after it is bound keeps every
    binding valid.

    Nothing is deactivated here, so the passes that follow still see every
    prim.
    """
    material_sets = [s for s in plan.sets if s.kind == MATERIAL]
    if not material_sets:
        return ()
    require_pxr()

    root = stage.GetDefaultPrim()
    if not root:
        raise VariantAuthoringError(
            "Material variant sets are authored on the asset's root prim, and "
            "this stage has no default prim."
        )
    if object_map is None:
        raise VariantAuthoringError(
            "Material variant sets cannot be authored: this export recorded no "
            "prim map, so there is no exact way to say which prim each target "
            "became."
        )

    prim_paths = _member_prim_paths(stage, object_map, path_edits)
    selected_only = bool(getattr(settings, "selected_objects_only", False))
    scope = _materials_scope(stage, root)

    authored: list[str] = []
    for variant_set in material_sets:
        targets = [
            prim_paths[name]
            for name in variant_set.target_object_names
            if name in prim_paths
        ]
        if not targets:
            if selected_only:
                if diagnostics:
                    diagnostics.add_warning(
                        f"Material variant set {variant_set.name!r} is not "
                        "authored: none of its targets are in this "
                        "selected-only export."
                    )
                continue
            raise VariantAuthoringError(
                f"Material variant set {variant_set.name!r} targets objects "
                "that are not on the exported stage."
            )

        # Bind where Blender binds. An object becomes an Xform with its mesh
        # beneath it, and the binding sits on the mesh - so binding the Xform
        # would be a weaker inherited opinion that the mesh's own overrides,
        # and the material rewrite, which collects what meshes bind, would
        # never see the new material either.
        bound_paths: list[str] = []
        for path in targets:
            found = _binding_prims(stage, path)
            if len(found) > 1 and diagnostics:
                diagnostics.add_warning(
                    f"Material variant set {variant_set.name!r}: {path} binds "
                    f"{len(found)} materials (GeomSubsets), and every variant "
                    "sets them all to one material."
                )
            bound_paths.extend(found)

        usd_set = root.GetVariantSets().AddVariantSet(variant_set.name)
        for variant in variant_set.variants:
            material = _material_prim(stage, scope, variant.material_name)
            usd_set.AddVariant(variant.name)
            usd_set.SetVariantSelection(variant.name)
            with usd_set.GetVariantEditContext():
                for path in bound_paths:
                    prim = stage.GetPrimAtPath(path)
                    if not prim:
                        raise VariantAuthoringError(
                            f"Target prim disappeared while authoring "
                            f"{variant_set.name!r}: {path}"
                        )
                    UsdShade.MaterialBindingAPI.Apply(prim)
                    UsdShade.MaterialBindingAPI(prim).Bind(material)
        usd_set.SetVariantSelection(variant_set.default_variant)

        # Blender authored a binding on each of those prims in the root layer,
        # and a local opinion beats every variant. Remove it so the variants
        # are the only thing saying what the object wears; each selection
        # supplies one.
        for path in bound_paths:
            _clear_base_binding(stage, path)

        authored.append(variant_set.name)

    return tuple(authored)


def _binding_prims(stage, object_path: str) -> list[str]:
    """The prims under an object that actually carry a material binding.

    Usually one mesh; a multi-material mesh has one per GeomSubset. An object
    with no material at all falls back to its own prim, where a binding still
    reaches the geometry beneath it.
    """
    prim = stage.GetPrimAtPath(object_path)
    if not prim:
        return []

    found: list[str] = []
    stack = [prim]
    while stack:
        current = stack.pop()
        relationship = UsdShade.MaterialBindingAPI(current).GetDirectBindingRel()
        if relationship and relationship.HasAuthoredTargets():
            found.append(str(current.GetPath()))
        stack.extend(current.GetAllChildren())
    return sorted(found) or [str(prim.GetPath())]


def _materials_scope(stage, root):
    """Where this export keeps its Material prims."""
    for prim in stage.TraverseAll():
        if prim.IsA(UsdShade.Material):
            return prim.GetPath().GetParentPath()
    return root.GetPath().AppendChild("_materials")


def _material_prim(stage, scope, material_name: str):
    """The Material prim for a Blender material, created if the export lacks one.

    Blender writes no material prim for a material no face wears, which is
    exactly the case a finish variant exists to offer. Defining an empty one
    here is enough: the MaterialX rewrite that follows finds it through this
    binding and authors the real graph into it, resolving the Blender material
    by the prim's name.
    """
    identifier = suggest_identifier(material_name, "Material")
    for prim in stage.TraverseAll():
        if not prim.IsA(UsdShade.Material):
            continue
        if prim.GetName() == identifier:
            return UsdShade.Material(prim)
    return UsdShade.Material.Define(stage, scope.AppendChild(identifier))


def _clear_base_binding(stage, path: str) -> None:
    """Drop a non-variant ``material:binding`` opinion from the root layer."""
    spec = stage.GetRootLayer().GetPrimAtPath(path)
    if spec is None:
        return
    relationship = spec.relationships.get("material:binding")
    if relationship is not None:
        spec.RemoveProperty(relationship)


def _member_prim_paths(stage, object_map: dict | None, path_edits) -> dict[str, str]:
    """{Blender object name: its prim path on this stage}.

    The map is captured during export and the stage may have been renamed
    since, so every path is translated through the rename edits and then
    confirmed against the stage. A path that no longer resolves is dropped
    here and reported by the caller, which knows which variant wanted it.
    """
    if not object_map:
        return {}

    resolved: dict[str, str] = {}
    for prim_path, object_name in object_map.items():
        current = remap_prim_path(str(prim_path), path_edits)
        if stage.GetPrimAtPath(current):
            resolved[str(object_name)] = current
    return resolved


def _resolve_set(
    variant_set: VariantSetPlan,
    prim_paths: dict[str, str],
    selected_only: bool,
    diagnostics,
) -> dict[str, tuple[str, ...]] | None:
    """{variant name: its member prim paths}, or None to skip the whole set."""
    resolved: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for variant in variant_set.variants:
        found = []
        for object_name in variant.object_names:
            path = prim_paths.get(object_name)
            if path is None:
                missing.append(object_name)
                continue
            found.append(path)
        resolved[variant.name] = tuple(found)

    if not any(resolved.values()):
        # Nothing of this set reached the export at all.
        if selected_only:
            if diagnostics:
                diagnostics.add_warning(
                    f"Variant set {variant_set.name!r} is not authored: none of "
                    "its members are in this selected-only export."
                )
            return None
        raise VariantAuthoringError(
            f"Variant set {variant_set.name!r} has no members on the exported "
            "stage. Every one of its objects is missing from the export."
        )

    empty = [name for name, paths in resolved.items() if not paths]
    if empty:
        raise VariantAuthoringError(
            f"Variants {', '.join(repr(name) for name in sorted(empty))} of set "
            f"{variant_set.name!r} have no members on the exported stage, while "
            "others do. A variant that activates nothing cannot be authored."
        )

    if missing and diagnostics:
        diagnostics.add_warning(
            f"Variant set {variant_set.name!r}: "
            f"{', '.join(repr(name) for name in sorted(set(missing)))} "
            "produced no prim and is left out of its variant."
        )
    return resolved


def _require_no_nested_members(
    variant_set: VariantSetPlan,
    resolved: dict[str, tuple[str, ...]],
) -> None:
    """Refuse a member whose prim encloses another variant's member.

    Blender parents objects, and a parented object's prim is a child of its
    parent's. Deactivating the parent therefore takes the child's subtree with
    it whatever the child's own ``active`` says - so selecting the child's
    variant would compose nothing. Objects alone cannot show this; only the
    prim paths can.
    """
    for name, paths in resolved.items():
        for other_name, other_paths in resolved.items():
            if other_name == name:
                continue
            for path in paths:
                for other in other_paths:
                    if other != path and Sdf.Path(other).HasPrefix(Sdf.Path(path)):
                        raise VariantAuthoringError(
                            f"In variant set {variant_set.name!r}, {path} "
                            f"(variant {name!r}) is a parent of {other} "
                            f"(variant {other_name!r}). Deactivating the parent "
                            "would remove the child as well, so the child's "
                            "variant could never compose. Unparent one of them "
                            "or put both in the same variant."
                        )


def _author_one_set(stage, root, variant_set: VariantSetPlan, resolved) -> None:
    usd_set = root.GetVariantSets().AddVariantSet(variant_set.name)
    for variant in variant_set.variants:
        usd_set.AddVariant(variant.name)
        usd_set.SetVariantSelection(variant.name)
        with usd_set.GetVariantEditContext():
            # Every member of every variant is spoken about in every variant:
            # a prim left unmentioned would keep whatever the previously
            # composed selection gave it.
            for other in variant_set.variants:
                for path in resolved[other.name]:
                    prim = stage.GetPrimAtPath(path)
                    if not prim:
                        raise VariantAuthoringError(
                            f"Variant member prim disappeared while authoring "
                            f"{variant_set.name!r}: {path}"
                        )
                    prim.SetActive(other.name == variant.name)
    usd_set.SetVariantSelection(variant_set.default_variant)


def _members_with_animation(stage, prim_paths: dict[str, str], settings) -> frozenset:
    """Member object names whose prim subtree holds a SkelAnimation.

    Only gathered when a clip library is being written, since that is the only
    thing the answer is used for.
    """
    if not bool(getattr(settings, "export_animation", False)):
        return frozenset()
    if not bool(getattr(settings, "author_animation_library", False)):
        return frozenset()
    return frozenset(
        object_name
        for object_name, path in prim_paths.items()
        if _has_skel_animation(stage, path)
    )


def _warn_about_animated_members(plan, animated: frozenset, authored, diagnostics) -> None:
    """Flag clips whose animation sits inside a member the default hides.

    The clip library is authored before this pass, over a stage where every
    member is still active, so it can name a source animation that the default
    variant selection then deactivates. Reality Composer Pro would look for an
    animation that is not composed.
    """
    if not animated:
        return

    hidden = {
        object_name
        for variant_set in plan.sets
        if variant_set.name in authored
        for variant in variant_set.variants
        if variant.name != variant_set.default_variant
        for object_name in variant.object_names
        if object_name in animated
    }
    if hidden:
        diagnostics.add_warning(
            "The animation library names animations inside variant members the "
            f"default selection deactivates ({', '.join(sorted(hidden))}). "
            "Those clips will not resolve until that variant is selected."
        )


def _has_skel_animation(stage, path: str) -> bool:
    """Whether this prim or anything beneath it is a SkelAnimation."""
    prim = stage.GetPrimAtPath(path)
    if not prim:
        return False
    stack = [prim]
    while stack:
        current = stack.pop()
        if current.GetTypeName() == "SkelAnimation":
            return True
        stack.extend(current.GetAllChildren())
    return False
