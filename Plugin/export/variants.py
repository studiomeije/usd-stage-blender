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

#: What a variant set varies. A geometry set switches whole objects on and
#: off; a material set leaves them all present and rebinds their material.
GEOMETRY = "GEOMETRY"
MATERIAL = "MATERIAL"


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
    """One variant of one set.

    A geometry variant owns a collection, and switching the set activates its
    objects. A material variant owns a material instead, and switching the set
    rebinds the objects its *set* targets. The two kinds vary different USD
    properties, which is what lets both act on one object.
    """

    name: str
    collection_name: str
    has_collection: bool
    object_names: tuple[str, ...]
    identities: frozenset[int]
    #: The objects themselves, for the passes that must act on them rather
    #: than reason about them: visibility staging and selection closure.
    objects: tuple = ()
    #: Material variants only: the Blender material this variant binds.
    material_name: str = ""
    has_material: bool = False
    #: Nested finishes only: the parent variants that offer this finish.
    #: Empty means every one of them.
    applies_to: tuple[str, ...] = ()

    def applies_to_parent(self, parent_variant: str) -> bool:
        return not self.applies_to or parent_variant in self.applies_to


@dataclass(frozen=True)
class VariantSetPlan:
    """One declared variant set, resolved but not yet validated."""

    name: str
    variants: tuple[VariantMember, ...]
    default_variant: str
    kind: str = GEOMETRY
    #: Material sets only: the objects whose binding this set varies.
    target_collection_name: str = ""
    has_target: bool = False
    target_object_names: tuple[str, ...] = ()
    target_identities: frozenset[int] = frozenset()
    target_objects: tuple = ()
    #: Material sets only: the shape set this one is authored inside, so a
    #: finish can be offered on some shapes and not others. Empty means the
    #: set stands on its own.
    parent_set: str = ""

    def default_for(self, parent_variant: str, offered) -> str:
        """The selection a nested set ships with under one parent variant.

        The declared default when that parent offers it, and the first finish
        it does offer otherwise - a selection naming a variant that does not
        exist there would not compose.
        """
        names = [variant.name for variant in offered]
        return self.default_variant if self.default_variant in names else names[0]

    @property
    def varied_property(self) -> str:
        """The USD property this set authors, and therefore owns.

        Two sets may act on the same object as long as they vary different
        properties; the conflict is over the property, not the object.
        """
        return "material:binding" if self.kind == MATERIAL else "active"

    @property
    def claimed_identities(self) -> frozenset[int]:
        """Objects whose varied property this set owns."""
        if self.kind == MATERIAL:
            return self.target_identities
        claimed: set[int] = set()
        for variant in self.variants:
            claimed |= variant.identities
        return frozenset(claimed)


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

    def set_named(self, name: str):
        """The declared set with this name, or None."""
        if not name:
            return None
        return next((s for s in self.sets if s.name == name), None)

    def member_objects(self) -> list:
        """Every object a set acts on, each once, in declared order.

        Includes a material set's targets: they have to be in the export for
        it to rebind them, even though it never deactivates them.
        """
        seen: set[int] = set()
        members: list = []

        def add(obj) -> None:
            identity = rna_identity(obj)
            if identity in seen:
                return
            seen.add(identity)
            members.append(obj)

        for variant_set in self.sets:
            for obj in variant_set.target_objects:
                add(obj)
            for variant in variant_set.variants:
                for obj in variant.objects:
                    add(obj)
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
            if (variant_set.target_identities & wanted)
            or any(variant.identities & wanted for variant in variant_set.variants)
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
        kind = str(getattr(entry, "kind", GEOMETRY) or GEOMETRY)
        variants: list[VariantMember] = []
        for declared_variant in getattr(entry, "variants", None) or ():
            variants.append(_resolve_variant(declared_variant, settings, kind))

        target = getattr(entry, "target_collection", None) if kind == MATERIAL else None
        target_objects: list = []
        if target is not None:
            target_objects = [
                obj for obj in _collection_objects(target)
                if is_exportable_object(obj, settings)
            ]

        sets.append(
            VariantSetPlan(
                name=str(getattr(entry, "name", "") or "").strip(),
                variants=tuple(variants),
                default_variant=str(getattr(entry, "default_variant", "") or "").strip(),
                kind=kind,
                target_collection_name=str(getattr(target, "name", "") or ""),
                has_target=target is not None,
                target_object_names=tuple(
                    str(getattr(obj, "name", "")) for obj in target_objects
                ),
                target_identities=frozenset(rna_identity(obj) for obj in target_objects),
                target_objects=tuple(target_objects),
                parent_set=str(getattr(entry, "parent_set", "") or "").strip(),
            )
        )
    return VariantPlan(sets=tuple(sets))


def _resolve_variant(declared_variant, settings, kind: str = GEOMETRY) -> VariantMember:
    name = str(getattr(declared_variant, "name", "") or "").strip()

    if kind == MATERIAL:
        material = getattr(declared_variant, "material", None)
        return VariantMember(
            name=name,
            collection_name="",
            has_collection=False,
            object_names=(),
            identities=frozenset(),
            material_name=str(getattr(material, "name", "") or ""),
            has_material=material is not None,
            applies_to=tuple(
                part.strip()
                for part in str(getattr(declared_variant, "applies_to", "") or "").split(",")
                if part.strip()
            ),
        )

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
    # Keyed by property as well as object: a geometry set and a material set
    # vary different properties, so both may act on one object.
    claimed_properties: dict[tuple[int, str], str] = {}

    for variant_set in plan.sets:
        _validate_set_name(variant_set, seen_set_names, issues)
        _validate_variants(variant_set, issues)
        _validate_default_selection(variant_set, issues)
        if variant_set.kind == MATERIAL:
            _validate_material_set(variant_set, issues)
            _validate_nesting(variant_set, plan, issues)
        else:
            _validate_membership(variant_set, claimed, issues)
        _validate_property_ownership(variant_set, claimed_properties, issues)

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

        if variant_set.kind == MATERIAL:
            # A material variant owns a material, not a collection; its own
            # rules are checked in _validate_material_set.
            continue

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
            if previous_set != variant_set.name:
                # Cross-set overlap is judged by property, not by object.
                continue
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


def _validate_material_set(
    variant_set: VariantSetPlan,
    issues: list[VariantIssue],
) -> None:
    """A material set needs objects to rebind and a material per variant."""
    if not variant_set.has_target:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_TARGET_MISSING",
                message=(
                    f"Material variant set {variant_set.name!r} has no target "
                    "collection, so there is nothing for it to rebind."
                ),
                variant_set=variant_set.name,
            )
        )
    elif not variant_set.target_identities:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_TARGET_EMPTY",
                message=(
                    f"Material variant set {variant_set.name!r} targets "
                    f"{variant_set.target_collection_name!r}, which holds "
                    "nothing exportable."
                ),
                variant_set=variant_set.name,
                details={"collection": variant_set.target_collection_name},
            )
        )

    seen_materials: dict[str, str] = {}
    for variant in variant_set.variants:
        if not variant.has_material:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_MATERIAL_MISSING",
                    message=(
                        f"Variant {variant.name!r} of material set "
                        f"{variant_set.name!r} has no material assigned."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )
            continue
        previous = seen_materials.get(variant.material_name)
        if previous is not None:
            issues.append(
                VariantIssue(
                    severity=WARNING,
                    code="VARIANT_MATERIAL_REPEATED",
                    message=(
                        f"Variants {previous!r} and {variant.name!r} of set "
                        f"{variant_set.name!r} both bind "
                        f"{variant.material_name!r}, so they look identical."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )
        else:
            seen_materials[variant.material_name] = variant.name


def _validate_nesting(
    variant_set: VariantSetPlan,
    plan: VariantPlan,
    issues: list[VariantIssue],
) -> None:
    """A nested finish set has to name a shape set, and real variants of it."""
    if not variant_set.parent_set:
        return

    parent = plan.set_named(variant_set.parent_set)
    if parent is None:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_PARENT_UNKNOWN",
                message=(
                    f"Variant set {variant_set.name!r} nests inside "
                    f"{variant_set.parent_set!r}, which is not a declared set."
                ),
                variant_set=variant_set.name,
            )
        )
        return

    if parent.name == variant_set.name:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_PARENT_SELF",
                message=f"Variant set {variant_set.name!r} nests inside itself.",
                variant_set=variant_set.name,
            )
        )
        return

    if parent.kind == MATERIAL:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_PARENT_NOT_A_SHAPE",
                message=(
                    f"Variant set {variant_set.name!r} nests inside "
                    f"{parent.name!r}, which is a finish set. A finish nests "
                    "inside a shape, so that different shapes can offer "
                    "different finishes."
                ),
                variant_set=variant_set.name,
            )
        )
        return

    if parent.parent_set:
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_NESTING_TOO_DEEP",
                message=(
                    f"Variant set {parent.name!r} is itself nested, so "
                    f"{variant_set.name!r} cannot nest inside it. One level "
                    "is the limit."
                ),
                variant_set=variant_set.name,
            )
        )
        return

    parent_names = {variant.name for variant in parent.variants}
    for variant in variant_set.variants:
        unknown = sorted(set(variant.applies_to) - parent_names)
        if unknown:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_APPLIES_TO_UNKNOWN",
                    message=(
                        f"Variant {variant.name!r} of set "
                        f"{variant_set.name!r} applies to "
                        f"{', '.join(repr(name) for name in unknown)}, which "
                        f"{'is' if len(unknown) == 1 else 'are'} not "
                        f"{parent.name!r} variant"
                        f"{'' if len(unknown) == 1 else 's'}."
                    ),
                    variant_set=variant_set.name,
                    variant=variant.name,
                )
            )

    for parent_variant in parent.variants:
        offered = [
            variant for variant in variant_set.variants
            if variant.applies_to_parent(parent_variant.name)
        ]
        if not offered:
            issues.append(
                VariantIssue(
                    severity=ERROR,
                    code="VARIANT_PARENT_HAS_NO_FINISH",
                    message=(
                        f"No variant of {variant_set.name!r} applies to "
                        f"{parent_variant.name!r}, so that shape would ship "
                        "with no finish to choose."
                    ),
                    variant_set=variant_set.name,
                    details={"parent_variant": parent_variant.name},
                )
            )


def _validate_property_ownership(
    variant_set: VariantSetPlan,
    claimed_properties: dict[tuple[int, str], str],
    issues: list[VariantIssue],
) -> None:
    """Refuse two sets varying the SAME property of the same object.

    Two geometry sets would both author ``active`` and fight; two material
    sets would both author ``material:binding``. A geometry set and a material
    set vary different properties, so a shape set and a finish set may act on
    one object - which is the whole point of offering shapes and finishes
    independently.
    """
    if variant_set.parent_set:
        # Scoped inside one shape variant at a time, so it cannot collide with
        # a set authored on the root prim.
        return
    varied = variant_set.varied_property
    reported: set[str] = set()
    for identity in sorted(variant_set.claimed_identities):
        key = (identity, varied)
        previous = claimed_properties.get(key)
        if previous is None:
            claimed_properties[key] = variant_set.name
            continue
        if previous == variant_set.name or previous in reported:
            continue
        reported.add(previous)
        issues.append(
            VariantIssue(
                severity=ERROR,
                code="VARIANT_PROPERTY_CONTESTED",
                message=(
                    f"Sets {previous!r} and {variant_set.name!r} both vary "
                    f"{varied!r} on the same objects. Whichever composes "
                    "second would win, so one of them could never take "
                    "effect."
                ),
                variant_set=variant_set.name,
                details={"conflicts_with_set": previous, "property": varied},
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
