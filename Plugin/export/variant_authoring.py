"""Writing declared variant sets onto the exported stage.

The planning side lives in :mod:`variants` and imports no OpenUSD. This is the
half that writes, and it writes two kinds:

* a **shape** set activates its own members and deactivates its siblings;
* a **finish** set leaves every object present and rebinds its material.

They vary different USD properties - ``active`` and ``material:binding`` - so
both may act on the same object. A finish set may also *nest* inside a shape
set, which is how a finish is offered on some shapes and not others.

Two properties make this safe, and both are deliberate.

Opinions are authored **only** inside a variant edit context. Variants sit low
in composition strength, so anything in the local layer stack beats them
silently; Blender authors no ``active`` of its own, and the binding it does
author is removed here so the variants are unopposed.

Members resolve through the exporter's own prim map, translated by any renames
normalization applied. Anything that cannot be placed exactly is refused
rather than guessed.
"""

from __future__ import annotations

from .usd_scene import remap_prim_path
from .usd_utils import Sdf, UsdShade, require_pxr
from .variants import MATERIAL, VariantPlan, VariantSetPlan, suggest_identifier


class VariantAuthoringError(RuntimeError):
    """Raised when declared variant sets cannot be written to the stage."""


# ---------------------------------------------------------------------------
# Materials a finish offers
# ---------------------------------------------------------------------------

def reserve_variant_materials(stage, plan: VariantPlan) -> tuple:
    """Define a Material prim for every finish, before the rewrite runs.

    Blender writes no material prim for a material no face wears, which is
    exactly what a finish variant exists to offer. Defining empty ones here
    lets the MaterialX rewrite convert them like any other - it is given them
    outright rather than having to find them bound, because a finish is bound
    later, inside the variant that offers it.
    """
    if plan.is_empty:
        return ()
    require_pxr()

    root = stage.GetDefaultPrim()
    if not root:
        return ()

    materials = _Materials(stage, root)
    reserved = {}
    for variant_set in plan.sets:
        if variant_set.kind != MATERIAL:
            continue
        for variant in variant_set.variants:
            if not variant.has_material:
                continue
            material = materials.get(variant.material_name)
            reserved.setdefault(str(material.GetPath()), material.GetPrim())
    return tuple(reserved.values())


class _Materials:
    """The stage's Material prims by name, found once rather than per lookup.

    A nested finish set asks for the same materials again under every shape
    variant, and each ask was walking the whole stage.
    """

    def __init__(self, stage, root):
        self._stage = stage
        self._by_name = {}
        self._scope = None
        for prim in stage.TraverseAll():
            if not prim.IsA(UsdShade.Material):
                continue
            self._by_name.setdefault(prim.GetName(), prim)
            if self._scope is None:
                self._scope = prim.GetPath().GetParentPath()
        if self._scope is None:
            self._scope = root.GetPath().AppendChild("_materials")

    def get(self, material_name: str):
        """The Material for a Blender material, defined if the export lacks one."""
        identifier = suggest_identifier(material_name, "Material")
        existing = self._by_name.get(identifier)
        if existing is not None:
            return UsdShade.Material(existing)
        material = UsdShade.Material.Define(
            self._stage, self._scope.AppendChild(identifier)
        )
        self._by_name[identifier] = material.GetPrim()
        return material


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------

def author_variants(
    stage,
    plan: VariantPlan,
    object_map: dict | None,
    path_edits=(),
    settings=None,
    diagnostics=None,
) -> tuple[str, ...]:
    """Author every declared set, shapes first so finishes can nest in them.

    Returns the set names authored, which may be fewer than declared when a
    selected-only export left a whole set out.
    """
    if plan.is_empty:
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

    paths = _prim_paths(stage, object_map, path_edits)
    selected_only = bool(getattr(settings, "selected_objects_only", False))
    animated = _animated_members(stage, paths, settings)
    materials = _Materials(stage, root)

    authored: list[str] = []
    for variant_set in plan.sets:
        if variant_set.kind == MATERIAL:
            continue
        resolved = _resolve_shape_set(variant_set, paths, selected_only, diagnostics)
        if resolved is None:
            continue
        _require_no_nested_members(variant_set, resolved)
        _author_shape_set(stage, root, variant_set, resolved)
        authored.append(variant_set.name)

    for variant_set in plan.sets:
        if variant_set.kind != MATERIAL:
            continue
        targets = [
            paths[name] for name in variant_set.target_object_names if name in paths
        ]
        if not targets:
            if selected_only:
                _warn(diagnostics,
                      f"Variant set {variant_set.name!r} is not authored: none "
                      "of its targets are in this selected-only export.")
                continue
            raise VariantAuthoringError(
                f"Variant set {variant_set.name!r} targets objects that are "
                "not on the exported stage."
            )
        parent = plan.set_named(variant_set.parent_set)
        if variant_set.parent_set and parent is None:
            raise VariantAuthoringError(
                f"Variant set {variant_set.name!r} nests inside "
                f"{variant_set.parent_set!r}, which is not authored."
            )
        if parent is None or parent.name not in authored:
            _author_finish_set(stage, root, variant_set, targets, materials, diagnostics)
        else:
            _author_nested_finish_set(
                stage, root, variant_set, parent, targets, materials, diagnostics
            )
        authored.append(variant_set.name)

    _warn_about_animated_members(plan, animated, authored, diagnostics)
    return tuple(authored)


def _author_shape_set(stage, root, variant_set: VariantSetPlan, resolved) -> None:
    usd_set = root.GetVariantSets().AddVariantSet(variant_set.name)
    for variant in variant_set.variants:
        usd_set.AddVariant(variant.name)
        usd_set.SetVariantSelection(variant.name)
        with usd_set.GetVariantEditContext():
            # Every member of every variant is spoken about in every variant: a
            # prim left unmentioned keeps whatever the last selection gave it.
            for other in variant_set.variants:
                for path in resolved[other.name]:
                    _require_prim(stage, path, variant_set.name).SetActive(
                        other.name == variant.name
                    )
    usd_set.SetVariantSelection(variant_set.default_variant)


def _author_finish_set(
    stage, root, variant_set: VariantSetPlan, targets, materials, diagnostics
) -> None:
    bound = _binding_paths(stage, targets, variant_set.name, diagnostics)
    usd_set = root.GetVariantSets().AddVariantSet(variant_set.name)
    for variant in variant_set.variants:
        material = materials.get(variant.material_name)
        usd_set.AddVariant(variant.name)
        usd_set.SetVariantSelection(variant.name)
        with usd_set.GetVariantEditContext():
            _bind(stage, bound, material, variant_set.name)
    usd_set.SetVariantSelection(variant_set.default_variant)
    _unbind_outside_variants(stage, bound)


def _author_nested_finish_set(
    stage, root, variant_set: VariantSetPlan, parent: VariantSetPlan,
    targets, materials, diagnostics,
) -> None:
    """Author the finish set inside each shape variant that offers it.

    Inside a shape variant only that shape's prims compose, so the finishes
    bind exactly what is present - which is the point of nesting rather than
    a detail of it.
    """
    parent_set = root.GetVariantSets().GetVariantSet(parent.name)
    for parent_variant in parent.variants:
        offered = [
            variant for variant in variant_set.variants
            if variant.applies_to_parent(parent_variant.name)
        ]
        if not offered:
            continue
        parent_set.SetVariantSelection(parent_variant.name)
        with parent_set.GetVariantEditContext():
            present = [path for path in targets if stage.GetPrimAtPath(path)]
            bound = _binding_paths(stage, present, variant_set.name, diagnostics)
            nested = root.GetVariantSets().AddVariantSet(variant_set.name)
            for variant in offered:
                material = materials.get(variant.material_name)
                nested.AddVariant(variant.name)
                nested.SetVariantSelection(variant.name)
                with nested.GetVariantEditContext():
                    _bind(stage, bound, material, variant_set.name)
            default = variant_set.default_for(parent_variant.name, offered)
            nested.SetVariantSelection(default)
            _unbind_outside_variants(stage, bound)
    parent_set.SetVariantSelection(parent.default_variant)


def _bind(stage, paths, material, set_name: str) -> None:
    for path in paths:
        prim = _require_prim(stage, path, set_name)
        UsdShade.MaterialBindingAPI.Apply(prim)
        UsdShade.MaterialBindingAPI(prim).Bind(material)


def _binding_paths(stage, targets, set_name: str, diagnostics) -> list[str]:
    """Bind where Blender binds: the mesh, not the Xform above it.

    An object becomes an Xform with its mesh beneath, and the binding sits on
    the mesh. Binding the Xform is a weaker inherited opinion the mesh's own
    overrides, and the material rewrite - which collects what meshes bind -
    would not see it either.
    """
    paths: list[str] = []
    for target in targets:
        found = _bound_descendants(stage, target)
        if len(found) > 1:
            _warn(diagnostics,
                  f"Variant set {set_name!r}: {target} binds {len(found)} "
                  "materials (GeomSubsets), and every variant sets them all "
                  "to one material.")
        paths.extend(found)
    return paths


def _bound_descendants(stage, object_path: str) -> list[str]:
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


def _unbind_outside_variants(stage, paths) -> None:
    """Drop the binding Blender left in the root layer.

    A local opinion beats every variant, so leaving it would freeze the
    finish. Each selection supplies one, and the preflight refuses a stray.
    """
    for path in paths:
        spec = stage.GetRootLayer().GetPrimAtPath(path)
        if spec is None:
            continue
        relationship = spec.relationships.get("material:binding")
        if relationship is not None:
            spec.RemoveProperty(relationship)


def _require_prim(stage, path: str, set_name: str):
    prim = stage.GetPrimAtPath(path)
    if not prim:
        raise VariantAuthoringError(
            f"Prim disappeared while authoring {set_name!r}: {path}"
        )
    return prim


# ---------------------------------------------------------------------------
# Resolving members to prims
# ---------------------------------------------------------------------------

def _prim_paths(stage, object_map: dict, path_edits) -> dict[str, str]:
    """{Blender object name: its prim path on this stage}.

    The map is captured during export and the stage may have been renamed
    since, so every path is translated and then confirmed against the stage.
    """
    resolved: dict[str, str] = {}
    for prim_path, object_name in object_map.items():
        current = remap_prim_path(str(prim_path), path_edits)
        if stage.GetPrimAtPath(current):
            resolved[str(object_name)] = current
    return resolved


def _resolve_shape_set(
    variant_set: VariantSetPlan, paths: dict, selected_only: bool, diagnostics
) -> dict | None:
    """{variant name: its member prim paths}, or None to skip the whole set."""
    resolved = {
        variant.name: tuple(
            paths[name] for name in variant.object_names if name in paths
        )
        for variant in variant_set.variants
    }
    missing = sorted({
        name for variant in variant_set.variants
        for name in variant.object_names if name not in paths
    })

    if not any(resolved.values()):
        if selected_only:
            _warn(diagnostics,
                  f"Variant set {variant_set.name!r} is not authored: none of "
                  "its members are in this selected-only export.")
            return None
        raise VariantAuthoringError(
            f"Variant set {variant_set.name!r} has no members on the exported "
            "stage. Every one of its objects is missing from the export."
        )

    empty = sorted(name for name, found in resolved.items() if not found)
    if empty:
        raise VariantAuthoringError(
            f"Variants {', '.join(repr(name) for name in empty)} of set "
            f"{variant_set.name!r} have no members on the exported stage, "
            "while others do. A variant that activates nothing cannot be "
            "authored."
        )

    if missing:
        _warn(diagnostics,
              f"Variant set {variant_set.name!r}: "
              f"{', '.join(repr(name) for name in missing)} produced no prim "
              "and is left out of its variant.")
    return resolved


def _require_no_nested_members(variant_set: VariantSetPlan, resolved: dict) -> None:
    """Refuse a member whose prim encloses another variant's member.

    Blender parents objects, and a parented object's prim is a child of its
    parent's. Deactivating the parent takes the child's subtree with it
    whatever the child's own ``active`` says, so selecting the child's variant
    would compose nothing. Objects alone cannot show this; only prim paths can.
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
                            f"(variant {other_name!r}). Deactivating the "
                            "parent would remove the child as well, so the "
                            "child's variant could never compose. Unparent one "
                            "of them or put both in the same variant."
                        )


# ---------------------------------------------------------------------------
# The clip library interaction
# ---------------------------------------------------------------------------

def _animated_members(stage, paths: dict, settings) -> frozenset:
    """Members whose prim subtree holds a SkelAnimation.

    Gathered before authoring: once a variant deactivates a member, its
    subtree is not composed and cannot be inspected at all. Only gathered when
    a clip library is being written, since that is all it is used for.
    """
    if not bool(getattr(settings, "export_animation", False)):
        return frozenset()
    if not bool(getattr(settings, "author_animation_library", False)):
        return frozenset()
    return frozenset(
        name for name, path in paths.items() if _has_skel_animation(stage, path)
    )


def _has_skel_animation(stage, path: str) -> bool:
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


def _warn_about_animated_members(plan, animated: frozenset, authored, diagnostics) -> None:
    """Flag clips whose animation sits inside a member the default hides.

    The clip library is authored before this pass, over a stage where every
    member is still active, so it can name a source animation that the default
    selection then deactivates.
    """
    if not animated or not diagnostics:
        return
    hidden = {
        name
        for variant_set in plan.sets
        if variant_set.name in authored
        for variant in variant_set.variants
        if variant.name != variant_set.default_variant
        for name in variant.object_names
        if name in animated
    }
    if hidden:
        diagnostics.add_warning(
            "The animation library names animations inside variant members the "
            f"default selection deactivates ({', '.join(sorted(hidden))}). "
            "Those clips will not resolve until that variant is selected."
        )


def _warn(diagnostics, message: str) -> None:
    if diagnostics:
        diagnostics.add_warning(message)
