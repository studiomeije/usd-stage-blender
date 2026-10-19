"""Variant set planning and validation.

Blender 5.2's USD exporter authors no variant sets, and it does not export
collections as prims: a scene organised as ``Seat/Fabric`` and ``Seat/Leather``
reaches USD as a flat list of object prims with the grouping gone.  Worse, the
two ways an artist hides the variant they are not looking at disagree - a
collection excluded from the view layer and an object with ``hide_render`` set
are both dropped, while an eye- or monitor-hidden object is exported *visible*.
Either way today's export of a variant-organised scene is wrong.

USD Stage for Blender therefore authors variant sets itself, from sets the user
declares on the scene.  This module owns the Blender half of that: it resolves
each declared set into concrete object sets and refuses the ones that cannot be
authored deterministically.

It runs before anything is written, so a refusal costs the user nothing, and it
imports no OpenUSD and touches no stage - resolution here, authoring later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .object_scope import is_exportable_object, rna_identity
from .realitykit_preflight import MAX_VARIANT_COMBINATIONS

# USD identifiers are ASCII ``[A-Za-z_][A-Za-z0-9_]*``.  ``allow_unicode`` is
# deliberately not consulted: it governs how USD Stage for Blender *renames*
# invalid prim identifiers, and a variant name is not ours to rename.  It is
# consumer-facing API - the string Reality Composer Pro lists in its picker and
# the one a RealityKit app passes to ``setVariantSelection(for:to:)`` - so an
# unusable name is reported back to the user instead of being silently changed.
_USD_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ERROR = "error"
WARNING = "warning"


def is_valid_identifier(name: str) -> bool:
    """Whether a variant or variant-set name can be written to USD as-is."""
    return bool(_USD_IDENTIFIER.match(name or ""))


def suggest_identifier(name: str, fallback: str = "Variant") -> str:
    """A usable USD identifier derived from a Blender name.

    Offered when a set or variant is *created* from a collection, where the
    user sees the result in the panel and can edit it. Export-time naming stays
    strict: by then a rename would change a name a consumer already depends on,
    so it is refused instead.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(name or ""))
    if not cleaned or not re.match(r"[A-Za-z_]", cleaned[0]):
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    return cleaned


@dataclass(frozen=True)
class VariantIssue:
    """One actionable finding about a declared variant set."""

    severity: str
    code: str
    message: str
    variant_set: str | None = None
    variant: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result: dict = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.variant_set:
            result["variant_set"] = self.variant_set
        if self.variant:
            result["variant"] = self.variant
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class VariantMember:
    """One variant of one set, resolved to the objects it owns."""

    name: str
    collection_name: str
    has_collection: bool
    object_names: tuple[str, ...]
    identities: frozenset[int]
    #: The objects themselves, for the passes that must act on them rather
    #: than reason about them: visibility staging and selection closure.
    objects: tuple = ()


@dataclass(frozen=True)
class VariantSetPlan:
    """One declared variant set, resolved but not yet validated."""

    name: str
    variants: tuple[VariantMember, ...]
    default_variant: str


@dataclass(frozen=True)
class VariantPlan:
    """Every variant set an export will author."""

    sets: tuple[VariantSetPlan, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.sets

    @property
    def combination_count(self) -> int:
        """Composed stage states these sets produce together.

        The preflight walks this same product, so a plan that would blow its
        fixed bound is refused here instead - before an export is staged.
        """
        total = 1
        for variant_set in self.sets:
            total *= max(len(variant_set.variants), 1)
        return total

    def member_identities(self) -> frozenset[int]:
        identities: set[int] = set()
        for variant_set in self.sets:
            for variant in variant_set.variants:
                identities |= variant.identities
        return frozenset(identities)

    def member_objects(self) -> list:
        """Every member object across every set, each once, in declared order."""
        seen: set[int] = set()
        members: list = []
        for variant_set in self.sets:
            for variant in variant_set.variants:
                for obj in variant.objects:
                    identity = rna_identity(obj)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    members.append(obj)
        return members

    def sets_touching(self, identities) -> tuple[VariantSetPlan, ...]:
        """The sets with at least one member among ``identities``.

        Selection closure asks this: a set the selection does not touch is not
        a dependency of that selection, and pulling its members in would export
        geometry the user did not ask for.
        """
        wanted = set(identities)
        return tuple(
            variant_set
            for variant_set in self.sets
            if any(variant.identities & wanted for variant in variant_set.variants)
        )


class VariantPlanError(RuntimeError):
    """Raised when declared variant sets cannot be authored."""

    def __init__(self, issues: list[VariantIssue]):
        self.issues = list(issues)
        super().__init__(_format_issues(self.issues))


def _format_issues(issues: list[VariantIssue]) -> str:
    if not issues:
        return "Variant sets cannot be authored."
    lines = ["Variant sets cannot be authored:"]
    lines.extend(f"  - {issue.message}" for issue in issues)
    return "\n".join(lines)


def resolve_variant_plan(context, settings) -> VariantPlan:
    """Resolve the scene's declared variant sets into concrete object sets.

    Nothing is refused here.  A set naming a deleted collection, or one whose
    collection holds nothing exportable, is still returned - carrying enough to
    say exactly what is wrong - so that validation reports every problem at once
    rather than the first one resolution happened to trip over.
    """
    if not bool(getattr(settings, "export_variants", True)):
        return VariantPlan()

    scene = getattr(context, "scene", None)
    declared = getattr(scene, "usd_stage_variant_sets", None) or ()

    sets: list[VariantSetPlan] = []
    for entry in declared:
        variants: list[VariantMember] = []
        for declared_variant in getattr(entry, "variants", None) or ():
            variants.append(_resolve_variant(declared_variant, settings))
        sets.append(
            VariantSetPlan(
                name=str(getattr(entry, "name", "") or "").strip(),
                variants=tuple(variants),
                default_variant=str(getattr(entry, "default_variant", "") or "").strip(),
            )
        )
    return VariantPlan(sets=tuple(sets))


def _resolve_variant(declared_variant, settings) -> VariantMember:
    name = str(getattr(declared_variant, "name", "") or "").strip()
    collection = getattr(declared_variant, "collection", None)
    if collection is None:
        return VariantMember(
            name=name,
            collection_name="",
            has_collection=False,
            object_names=(),
            identities=frozenset(),
        )

    objects = _collection_objects(collection)
    exportable = [obj for obj in objects if is_exportable_object(obj, settings)]
    return VariantMember(
        name=name,
        collection_name=str(getattr(collection, "name", "") or ""),
        has_collection=True,
        object_names=tuple(str(getattr(obj, "name", "")) for obj in exportable),
        identities=frozenset(rna_identity(obj) for obj in exportable),
        objects=tuple(exportable),
    )


def _collection_objects(collection) -> list:
    """Every object in a collection and its children, each once.

    Blender forbids collection cycles, but the walk is guarded anyway so a
    malformed scene or a test double cannot hang an export.
    """
    seen_collections: set[int] = set()
    seen_objects: set[int] = set()
    objects: list = []

    def walk(current) -> None:
        identity = rna_identity(current)
        if identity in seen_collections:
            return
        seen_collections.add(identity)
        for obj in getattr(current, "objects", None) or ():
            object_identity = rna_identity(obj)
            if object_identity in seen_objects:
                continue
            seen_objects.add(object_identity)
            objects.append(obj)
        for child in getattr(current, "children", None) or ():
            walk(child)

    walk(collection)
    return objects


def validate_variant_plan(plan: VariantPlan) -> list[VariantIssue]:
    """Return every finding about a resolved plan, errors and warnings alike."""
    issues: list[VariantIssue] = []
    if plan.is_empty:
        return issues

    seen_set_names: set[str] = set()
    # {object identity: (set name, variant name)} of the first claim seen, so a
    # second claim can name both sides of the conflict.
    claimed: dict[int, tuple[str, str]] = {}

    for variant_set in plan.sets:
        _validate_set_name(variant_set, seen_set_names, issues)
        _validate_variants(variant_set, issues)
        _validate_default_selection(variant_set, issues)
        _validate_membership(variant_set, claimed, issues)

    if plan.combination_count > MAX_VARIANT_COMBINATIONS:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_COMBINATIONS_EXCEEDED",
                message=(
                    f"The declared variant sets compose "
                    f"{plan.combination_count} stage states, over the preflight "
                    f"limit of {MAX_VARIANT_COMBINATIONS}. Split the asset or "
                    "declare fewer variants."
                ),
                details={
                    "combinations": plan.combination_count,
                    "limit": MAX_VARIANT_COMBINATIONS,
                },
            )
        )
    return issues


def _validate_set_name(
    variant_set: VariantSetPlan,
    seen_set_names: set[str],
    issues: list[VariantIssue],
) -> None:
    name = variant_set.name
    if not _USD_IDENTIFIER.match(name or ""):
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_SET_NAME_INVALID",
                message=(
                    f"Variant set name {name!r} is not a USD identifier. Use "
                    "letters, digits and underscores, starting with a letter "
                    "or underscore."
                ),
                variant_set=name,
            )
        )
    elif name in seen_set_names:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_SET_NAME_DUPLICATE",
                message=(
                    f"Two variant sets are both named {name!r}. Each set is "
                    "authored on the same prim, so their names must differ."
                ),
                variant_set=name,
            )
        )
    else:
        seen_set_names.add(name)


def _validate_variants(variant_set: VariantSetPlan, issues: list[VariantIssue]) -> None:
    if not variant_set.variants:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_SET_EMPTY",
                message=f"Variant set {variant_set.name!r} declares no variants.",
                variant_set=variant_set.name,
            )
        )
        return

    if len(variant_set.variants) == 1:
        issues.append(
            VariantIssue(
                severity=WARNING,
                code="VARIANT_SET_SINGLE",
                message=(
                    f"Variant set {variant_set.name!r} has one variant, so "
                    "there is nothing to switch between."
                ),
                variant_set=variant_set.name,
            )
        )

    seen_names: set[str] = set()
    for variant in variant_set.variants:
        if not _USD_IDENTIFIER.match(variant.name or ""):
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_NAME_INVALID",
                    message=(
                        f"Variant name {variant.name!r} in set "
                        f"{variant_set.name!r} is not a USD identifier. Use "
                        "letters, digits and underscores, starting with a "
                        "letter or underscore."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )
        elif variant.name in seen_names:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_NAME_DUPLICATE",
                    message=(
                        f"Variant set {variant_set.name!r} declares "
                        f"{variant.name!r} twice."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )
        else:
            seen_names.add(variant.name)

        if not variant.has_collection:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_COLLECTION_MISSING",
                    message=(
                        f"Variant {variant.name!r} in set {variant_set.name!r} "
                        "has no collection. Assign one, or remove the variant."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )
        elif not variant.identities:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_EMPTY",
                    message=(
                        f"Variant {variant.name!r} in set {variant_set.name!r} "
                        f"resolves to nothing exportable in collection "
                        f"{variant.collection_name!r}."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                    details={"collection": variant.collection_name},
                )
            )


def _validate_default_selection(
    variant_set: VariantSetPlan,
    issues: list[VariantIssue],
) -> None:
    if not variant_set.variants:
        return
    names = {variant.name for variant in variant_set.variants}
    if variant_set.default_variant not in names:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_DEFAULT_UNKNOWN",
                message=(
                    f"Variant set {variant_set.name!r} selects "
                    f"{variant_set.default_variant!r} by default, which is not "
                    f"one of its variants ({', '.join(sorted(names))})."
                ),
                variant_set=variant_set.name,
                details={"default_variant": variant_set.default_variant},
            )
        )


def _validate_membership(
    variant_set: VariantSetPlan,
    claimed: dict[int, tuple[str, str]],
    issues: list[VariantIssue],
) -> None:
    """Refuse an object claimed twice, within one set or across two.

    Membership is compared per object, not per collection, because an object
    exports to exactly one prim however many collections hold it - and one prim
    cannot be activated by two variants at once.  Comparing objects also covers
    nesting for free: a collection that contains another's resolves to a
    superset of its objects, so the overlap is caught here rather than needing a
    separate hierarchy rule.
    """
    for variant in variant_set.variants:
        reported_against: set[tuple[str, str]] = set()
        for identity in sorted(variant.identities):
            previous = claimed.get(identity)
            if previous is None:
                claimed[identity] = (variant_set.name, variant.name)
                continue
            if previous in reported_against:
                continue
            reported_against.add(previous)

            previous_set, previous_variant = previous
            if previous_set == variant_set.name:
                issues.append(
                    VariantIssue(
                        severity=ERROR,
                        code="VARIANT_MEMBER_SHARED_IN_SET",
                        message=(
                            f"Variants {previous_variant!r} and {variant.name!r} "
                            f"of set {variant_set.name!r} share objects. One "
                            "object cannot belong to two variants of the same "
                            "set."
                        ),
                        variant_set=variant_set.name,
                        variant=variant.name,
                        details={"conflicts_with": previous_variant},
                    )
                )
            else:
                issues.append(
                    VariantIssue(
                        severity=ERROR,
                        code="VARIANT_MEMBER_SHARED_ACROSS_SETS",
                        message=(
                            f"Variant {variant.name!r} of set "
                            f"{variant_set.name!r} shares objects with "
                            f"{previous_variant!r} of set {previous_set!r}. "
                            "Both sets would author activation for the same "
                            "prim."
                        ),
                        variant_set=variant_set.name,
                        variant=variant.name,
                        details={
                            "conflicts_with_set": previous_set,
                            "conflicts_with_variant": previous_variant,
                        },
                    )
                )


# --------------------------------------------------------------------------
# Visibility staging
# --------------------------------------------------------------------------

#: Flags on a Collection or an Object that keep it out of the export. Both
#: spellings are cleared whatever ``evaluation_mode`` says, so staging does not
#: depend on which one the current mode happens to consult.
_HIDE_FLAGS = ("hide_render", "hide_viewport")


def prepare_variant_visibility(context, plan: VariantPlan) -> dict:
    """Make every variant member exportable, recording what to put back.

    An artist hides the variant they are not working on, and Blender's USD
    exporter then leaves it out: a collection excluded from the view layer is
    skipped, and so is anything whose ``hide_render`` or ``hide_viewport`` flag
    the active ``evaluation_mode`` consults.  Only the outliner's eye is
    ignored.  Every variant has to be in the export for the authoring pass to
    switch between them, so staging opens each member's path into the view
    layer for the length of the export.

    The returned state must be passed to :func:`restore_variant_visibility`,
    which puts every flag back exactly as it was.
    """
    state: dict = {"restores": [], "context": context}
    if plan.is_empty:
        return state

    restores: list = state["restores"]

    def assign(owner, attribute: str, value) -> None:
        if owner is None:
            return
        try:
            previous = getattr(owner, attribute)
        except Exception:
            return
        if bool(previous) == bool(value):
            return
        restores.append((owner, attribute, previous))
        try:
            setattr(owner, attribute, value)
        except Exception:
            # Nothing was changed, so nothing is owed on restore.
            restores.pop()

    members = {rna_identity(obj) for obj in plan.member_objects()}

    for chain in _member_layer_collection_chains(context, members):
        # Root first: clearing a parent's exclude is what makes clearing a
        # child's meaningful, and Blender resets descendants when a parent is
        # re-included.
        for layer_collection in chain:
            assign(layer_collection, "exclude", False)
            collection = getattr(layer_collection, "collection", None)
            for flag in _HIDE_FLAGS:
                assign(collection, flag, False)

    for obj in plan.member_objects():
        for flag in _HIDE_FLAGS:
            assign(obj, flag, False)

    _update_view_layer(context)
    return state


def restore_variant_visibility(state: dict) -> None:
    """Put every flag staging changed back, newest change first."""
    if not state:
        return
    for owner, attribute, previous in reversed(state.get("restores", [])):
        try:
            setattr(owner, attribute, previous)
        except Exception:
            continue
    state["restores"] = []
    _update_view_layer(state.get("context"))


def _member_layer_collection_chains(context, members: set[int]) -> list[tuple]:
    """Root-to-leaf chains of every layer collection holding a member object.

    A collection can be linked in more than one place, so every chain that
    reaches a member is opened rather than only the first one found.
    """
    view_layer = getattr(context, "view_layer", None)
    root = getattr(view_layer, "layer_collection", None)
    if root is None or not members:
        return []

    chains: list[tuple] = []

    def walk(layer_collection, ancestors: tuple) -> None:
        chain = ancestors + (layer_collection,)
        collection = getattr(layer_collection, "collection", None)
        objects = getattr(collection, "objects", None) or ()
        if any(rna_identity(obj) in members for obj in objects):
            chains.append(chain)
        for child in getattr(layer_collection, "children", None) or ():
            walk(child, chain)

    walk(root, ())
    return chains


def _update_view_layer(context) -> None:
    view_layer = getattr(context, "view_layer", None)
    update = getattr(view_layer, "update", None)
    if update is None:
        return
    try:
        update()
    except Exception:
        pass


def require_valid_variant_plan(plan: VariantPlan, diagnostics=None) -> VariantPlan:
    """Return the plan, or raise with every error at once.

    Warnings reach the diagnostics sidecar and the plan is still authored; only
    errors stop the export.
    """
    issues = validate_variant_plan(plan)
    errors = [issue for issue in issues if issue.severity == ERROR]
    warnings = [issue for issue in issues if issue.severity == WARNING]

    if diagnostics:
        for warning in warnings:
            diagnostics.add_warning(warning.message)

    if errors:
        if diagnostics:
            for error in errors:
                diagnostics.add_error(error.message)
        raise VariantPlanError(errors)
    return plan
