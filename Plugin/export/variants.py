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


def unique_name(taken, base: str) -> str:
    """``base``, or ``base_2``, ``base_3``... - the first not in ``taken``."""
    taken = set(taken)
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def guess_finish_prefix(material_name: str) -> str:
    """The family prefix of a material name, for picking its siblings.

    ``Fabric_Encre`` suggests ``Fabric_``: finishes are usually named as one
    family with a varying tail. A name with no separator suggests nothing.
    """
    name = str(material_name or "")
    for separator in ("_", ".", "-", " "):
        index = name.rfind(separator)
        if index > 0:
            return name[: index + 1]
    return ""


def finish_name_from_material(material_name: str, prefix: str = "") -> str:
    """A variant name for a finish, from its material less the family prefix."""
    name = str(material_name or "")
    if prefix and name.startswith(prefix) and len(name) > len(prefix):
        name = name[len(prefix):]
    return suggest_identifier(name.strip("_.- ") or name, "Finish")


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


class _Issues(list):
    """The findings so far, and a short way to add one.

    Every refusal reads the same way at its call site, so the shape of the
    issue stays out of the way of what is being refused.
    """

    def error(self, code: str, message: str, **where) -> None:
        self._add(ERROR, code, message, **where)

    def warn(self, code: str, message: str, **where) -> None:
        self._add(WARNING, code, message, **where)

    def _add(self, severity: str, code: str, message: str,
             *, variant_set=None, variant=None, **details) -> None:
        self.append(
            VariantIssue(severity, code, message, variant_set, variant, details)
        )


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
    #: The objects this variant owns. Names and identities are derived from
    #: them rather than stored, so there is one thing to keep correct.
    objects: tuple = ()
    #: Material variants only: the Blender material this variant binds.
    material_name: str = ""
    has_material: bool = False
    #: Nested finishes only: the parent variants that offer this finish.
    #: Empty means every one of them.
    applies_to: tuple[str, ...] = ()

    @property
    def object_names(self) -> tuple[str, ...]:
        return tuple(str(getattr(obj, "name", "")) for obj in self.objects)

    @property
    def identities(self) -> frozenset[int]:
        return frozenset(rna_identity(obj) for obj in self.objects)

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
    target_objects: tuple = ()
    #: Material sets only: the shape set this one is authored inside, so a
    #: finish can be offered on some shapes and not others. Empty means the
    #: set stands on its own.
    parent_set: str = ""

    @property
    def target_object_names(self) -> tuple[str, ...]:
        return tuple(str(getattr(obj, "name", "")) for obj in self.target_objects)

    @property
    def target_identities(self) -> frozenset[int]:
        return frozenset(rna_identity(obj) for obj in self.target_objects)

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
            if not variant_set.parent_set:
                total *= self._states(variant_set)
        return total

    def _states(self, variant_set) -> int:
        """Stage states one top-level set produces, nested sets included."""
        nested = [s for s in self.sets if s.parent_set == variant_set.name]
        if not nested:
            return max(len(variant_set.variants), 1)
        states = 0
        for variant in variant_set.variants:
            offered = 1
            for child in nested:
                offered *= max(
                    sum(
                        1 for finish in child.variants
                        if finish.applies_to_parent(variant.name)
                    ),
                    1,
                )
            states += offered
        return states or 1

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
                target_objects=tuple(target_objects),
                # Only a finish nests. A shape set can still hold a value the
                # panel no longer shows, left from when it was a finish.
                parent_set=(
                    str(getattr(entry, "parent_set", "") or "").strip()
                    if kind == MATERIAL else ""
                ),
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
        )

    objects = _collection_objects(collection)
    exportable = [obj for obj in objects if is_exportable_object(obj, settings)]
    return VariantMember(
        name=name,
        collection_name=str(getattr(collection, "name", "") or ""),
        has_collection=True,
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
    issues = _Issues()
    if plan.is_empty:
        return issues

    seen_set_names: set[str] = set()
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
            _validate_membership(variant_set, issues)
        _validate_property_ownership(variant_set, claimed_properties, issues)

    if plan.combination_count > MAX_VARIANT_COMBINATIONS:
        issues.error(
            "VARIANT_COMBINATIONS_EXCEEDED",
            f"The declared variant sets compose "
            f"{plan.combination_count} stage states, over the preflight "
            f"limit of {MAX_VARIANT_COMBINATIONS}. Split the asset or "
            "declare fewer variants.",
            combinations=plan.combination_count,
            limit=MAX_VARIANT_COMBINATIONS,
        )
    return issues


def _validate_set_name(
    variant_set: VariantSetPlan,
    seen_set_names: set[str],
    issues: _Issues,
) -> None:
    name = variant_set.name
    if not _USD_IDENTIFIER.match(name or ""):
        issues.error(
            "VARIANT_SET_NAME_INVALID",
            f"Variant set name {name!r} is not a USD identifier. Use "
            "letters, digits and underscores, starting with a letter "
            "or underscore.",
            variant_set=name,
        )
    elif name in seen_set_names:
        issues.error(
            "VARIANT_SET_NAME_DUPLICATE",
            f"Two variant sets are both named {name!r}. Each set is "
            "authored on the same prim, so their names must differ.",
            variant_set=name,
        )
    else:
        seen_set_names.add(name)


def _validate_variants(variant_set: VariantSetPlan, issues: _Issues) -> None:
    if not variant_set.variants:
        issues.error(
            "VARIANT_SET_EMPTY",
            f"Variant set {variant_set.name!r} declares no variants.",
            variant_set=variant_set.name,
        )
        return

    if len(variant_set.variants) == 1:
        issues.warn(
            "VARIANT_SET_SINGLE",
            f"Variant set {variant_set.name!r} has one variant, so "
            "there is nothing to switch between.",
            variant_set=variant_set.name,
        )

    seen_names: set[str] = set()
    for variant in variant_set.variants:
        if not _USD_IDENTIFIER.match(variant.name or ""):
            issues.error(
                "VARIANT_NAME_INVALID",
                f"Variant name {variant.name!r} in set "
                f"{variant_set.name!r} is not a USD identifier. Use "
                "letters, digits and underscores, starting with a "
                "letter or underscore.",
                variant_set=variant_set.name,
                variant=variant.name,
            )
        elif variant.name in seen_names:
            issues.error(
                "VARIANT_NAME_DUPLICATE",
                f"Variant set {variant_set.name!r} declares "
                f"{variant.name!r} twice.",
                variant_set=variant_set.name,
                variant=variant.name,
            )
        else:
            seen_names.add(variant.name)

        if variant_set.kind == MATERIAL:
            # A material variant owns a material, not a collection; its own
            # rules are checked in _validate_material_set.
            continue

        if not variant.has_collection:
            issues.error(
                "VARIANT_COLLECTION_MISSING",
                f"Variant {variant.name!r} in set {variant_set.name!r} "
                "has no collection. Assign one, or remove the variant.",
                variant_set=variant_set.name,
                variant=variant.name,
            )
        elif not variant.identities:
            issues.error(
                "VARIANT_EMPTY",
                f"Variant {variant.name!r} in set {variant_set.name!r} "
                f"resolves to nothing exportable in collection "
                f"{variant.collection_name!r}.",
                variant_set=variant_set.name,
                variant=variant.name,
                collection=variant.collection_name,
            )


def _validate_default_selection(
    variant_set: VariantSetPlan,
    issues: _Issues,
) -> None:
    if not variant_set.variants:
        return
    names = {variant.name for variant in variant_set.variants}
    if variant_set.default_variant not in names:
        issues.error(
            "VARIANT_DEFAULT_UNKNOWN",
            f"Variant set {variant_set.name!r} selects "
            f"{variant_set.default_variant!r} by default, which is not "
            f"one of its variants ({', '.join(sorted(names))}).",
            variant_set=variant_set.name,
            default_variant=variant_set.default_variant,
        )


def _validate_membership(variant_set: VariantSetPlan, issues: _Issues) -> None:
    """Refuse an object two variants of this set both claim.

    Membership is compared per object, not per collection: an object exports
    to exactly one prim however many collections hold it, and one prim cannot
    be activated by two variants at once. That also covers nesting for free -
    a collection containing another's resolves to a superset of its objects,
    so the overlap shows up here rather than needing a hierarchy rule.

    Overlap between two *sets* is a different question, judged by property
    rather than by object; see :func:`_validate_property_ownership`.
    """
    claimed: dict[int, str] = {}
    for variant in variant_set.variants:
        for other in sorted({
            claimed[identity] for identity in variant.identities
            if identity in claimed
        }):
            issues.error(
                "VARIANT_MEMBER_SHARED_IN_SET",
                f"Variants {other!r} and {variant.name!r} of set "
                f"{variant_set.name!r} share objects. One object cannot "
                "belong to two variants of the same set.",
                variant_set=variant_set.name,
                variant=variant.name,
                conflicts_with=other,
            )
        for identity in variant.identities:
            claimed.setdefault(identity, variant.name)


def _validate_material_set(
    variant_set: VariantSetPlan,
    issues: _Issues,
) -> None:
    """A material set needs objects to rebind and a material per variant."""
    if not variant_set.has_target:
        issues.error(
            "VARIANT_TARGET_MISSING",
            f"Material variant set {variant_set.name!r} has no target "
            "collection, so there is nothing for it to rebind.",
            variant_set=variant_set.name,
        )
    elif not variant_set.target_identities:
        issues.error(
            "VARIANT_TARGET_EMPTY",
            f"Material variant set {variant_set.name!r} targets "
            f"{variant_set.target_collection_name!r}, which holds "
            "nothing exportable.",
            variant_set=variant_set.name,
            collection=variant_set.target_collection_name,
        )

    seen_materials: dict[str, str] = {}
    for variant in variant_set.variants:
        if not variant.has_material:
            issues.error(
                "VARIANT_MATERIAL_MISSING",
                f"Variant {variant.name!r} of material set "
                f"{variant_set.name!r} has no material assigned.",
                variant_set=variant_set.name,
                variant=variant.name,
            )
            continue
        previous = seen_materials.get(variant.material_name)
        if previous is not None:
            issues.warn(
                "VARIANT_MATERIAL_REPEATED",
                f"Variants {previous!r} and {variant.name!r} of set "
                f"{variant_set.name!r} both bind "
                f"{variant.material_name!r}, so they look identical.",
                variant_set=variant_set.name,
                variant=variant.name,
            )
        else:
            seen_materials[variant.material_name] = variant.name


def _validate_nesting(
    variant_set: VariantSetPlan,
    plan: VariantPlan,
    issues: _Issues,
) -> None:
    """A nested finish set has to name a shape set, and real variants of it."""
    if not variant_set.parent_set:
        return

    parent = plan.set_named(variant_set.parent_set)
    if parent is None:
        issues.error(
            "VARIANT_PARENT_UNKNOWN",
            f"Variant set {variant_set.name!r} nests inside "
            f"{variant_set.parent_set!r}, which is not a declared set.",
            variant_set=variant_set.name,
        )
        return

    if parent.name == variant_set.name:
        issues.error(
            "VARIANT_PARENT_SELF",
            f"Variant set {variant_set.name!r} nests inside itself.",
            variant_set=variant_set.name,
        )
        return

    if parent.kind == MATERIAL:
        issues.error(
            "VARIANT_PARENT_NOT_A_SHAPE",
            f"Variant set {variant_set.name!r} nests inside "
            f"{parent.name!r}, which is a finish set. A finish nests "
            "inside a shape, so that different shapes can offer "
            "different finishes.",
            variant_set=variant_set.name,
        )
        return

    parent_names = {variant.name for variant in parent.variants}
    for variant in variant_set.variants:
        unknown = sorted(set(variant.applies_to) - parent_names)
        if unknown:
            issues.error(
                "VARIANT_APPLIES_TO_UNKNOWN",
                f"Variant {variant.name!r} of set "
                f"{variant_set.name!r} applies to "
                f"{', '.join(repr(name) for name in unknown)}, which "
                f"{'is' if len(unknown) == 1 else 'are'} not "
                f"{parent.name!r} variant"
                f"{'' if len(unknown) == 1 else 's'}.",
                variant_set=variant_set.name,
                variant=variant.name,
            )

    for parent_variant in parent.variants:
        offered = [
            variant for variant in variant_set.variants
            if variant.applies_to_parent(parent_variant.name)
        ]
        if not offered:
            issues.error(
                "VARIANT_PARENT_HAS_NO_FINISH",
                f"No variant of {variant_set.name!r} applies to "
                f"{parent_variant.name!r}, so that shape would ship "
                "with no finish to choose.",
                variant_set=variant_set.name,
                parent_variant=parent_variant.name,
            )


def _validate_property_ownership(
    variant_set: VariantSetPlan,
    claimed_properties: dict[tuple[int, str], str],
    issues: _Issues,
) -> None:
    """Refuse two sets varying the SAME property of the same object.

    Two geometry sets would both author ``active`` and fight; two material
    sets would both author ``material:binding``. A geometry set and a material
    set vary different properties, so a shape set and a finish set may act on
    one object - which is the whole point of offering shapes and finishes
    independently.

    Nesting does not exempt a set. A nested finish never contests its own
    parent, because the parent varies ``active`` and it varies the binding,
    but two finish sets still contest each other whether either is nested:
    both would author the binding and whichever composed second would win.
    """
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
        issues.error(
            "VARIANT_PROPERTY_CONTESTED",
            f"Sets {previous!r} and {variant_set.name!r} both vary "
            f"{varied!r} on the same objects. Whichever composes "
            "second would win, so one of them could never take "
            "effect.",
            variant_set=variant_set.name,
            conflicts_with_set=previous,
            property=varied,
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
