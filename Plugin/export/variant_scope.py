"""The variant sets included in the current export scope."""

from __future__ import annotations

from .object_scope import rna_identity
from .variants import VariantPlan, resolve_variant_plan


def effective_variant_plan(context, settings) -> VariantPlan:
    """Resolve only the sets the export writes, including selection dependencies.

    The panel and bake validation must agree: an unrelated material set must
    not block a selected-only geometry bake. The export's selection collector
    already closes over sibling variants, nested sets and rigs, so use its
    result instead of reconstructing that scope from the raw selection.
    """
    plan = resolve_variant_plan(context, settings)
    if plan.is_empty or not bool(getattr(settings, "selected_objects_only", False)):
        return plan

    # This import requires Blender; full-scene planning stays usable without it.
    from .animation_export import collect_export_objects

    objects = collect_export_objects(context, settings)
    return VariantPlan(sets=plan.sets_touching(rna_identity(obj) for obj in objects))
