"""The Variants panel: declaring USD variant sets on the scene.

Blender has no variants of its own - the 2024 design work proposed some, but
nothing shipped - and its USD exporter writes none. So the declaration lives
here, on the scene, and the export pass in ``export/variants.py`` reads it.

The sets are registered on ``Scene`` rather than on ``USDStageExportSettings``
on purpose. They are scene content, not a remembered preference: the settings
group is serialized into add-on preferences as last-used values and carried
into the next scene, which is right for a bake resolution and wrong for a list
of collections belonging to this file.

The declaration refers to variants and sets by name - the default selection,
a finish's parent set, the shapes a finish is offered on - because names are
what USD stores. Renaming one therefore carries the new name to everything
that pointed at the old one, here, rather than leaving a broken reference for
the export to refuse.
"""

import re

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Panel, PropertyGroup, UIList

from ..export.variants import (
    ERROR,
    resolve_variant_plan,
    suggest_identifier,
    unique_name,
    validate_variant_plan,
)
from .panel import _is_job_running

#: The names the Add Variant button hands out. A variant still called one of
#: these takes its name from the collection or material it is given.
_PLACEHOLDER = re.compile(r"^Variant(_\d+)?$")


def _set_named(scene, name):
    return next(
        (other for other in scene.usd_stage_variant_sets if other.name == name),
        None,
    )


def _owner_set(variant):
    """The scene and the set holding ``variant``."""
    scene = variant.id_data
    pointer = variant.as_pointer()
    for variant_set in getattr(scene, "usd_stage_variant_sets", ()):
        for item in variant_set.variants:
            if item.as_pointer() == pointer:
                return scene, variant_set
    return scene, None


def _offered_on(variant) -> list:
    return [name.strip() for name in variant.applies_to.split(",") if name.strip()]


# --------------------------------------------------------------------------
# Keeping names consistent
# --------------------------------------------------------------------------

def _on_variant_renamed(self, context):
    """Carry a variant's new name to the default and to nested finishes."""
    old, new = self.previous_name, self.name
    if old == new:
        return
    self.previous_name = new
    if not old:
        return
    scene, variant_set = _owner_set(self)
    if variant_set is None:
        return
    if variant_set.default_variant == old:
        variant_set.default_variant = new
    for other in scene.usd_stage_variant_sets:
        if other.parent_set != variant_set.name:
            continue
        for finish in other.variants:
            names = _offered_on(finish)
            if old in names:
                finish.applies_to = ",".join(new if name == old else name for name in names)


def _on_set_renamed(self, context):
    """Carry a set's new name to the finish sets nested inside it."""
    old, new = self.previous_name, self.name
    if old == new:
        return
    self.previous_name = new
    if not old:
        return
    for other in self.id_data.usd_stage_variant_sets:
        if other.parent_set == old:
            other.parent_set = new


def _on_kind_changed(self, context):
    # Only a finish nests; a shape set keeps no hidden parent to trip on.
    if self.kind != 'MATERIAL' and self.parent_set:
        self.parent_set = ""


def _name_from(variant, source_name: str) -> None:
    """Name a placeholder variant after what it was just given."""
    if not source_name or not _PLACEHOLDER.match(variant.name):
        return
    _scene, variant_set = _owner_set(variant)
    pointer = variant.as_pointer()
    taken = [
        item.name for item in (variant_set.variants if variant_set else ())
        if item.as_pointer() != pointer
    ]
    variant.name = unique_name(taken, suggest_identifier(source_name))


def _on_collection_picked(self, context):
    if self.collection is not None:
        _name_from(self, self.collection.name)


def _on_material_picked(self, context):
    if self.material is not None:
        _name_from(self, self.material.name)


# --------------------------------------------------------------------------
# Property groups
# --------------------------------------------------------------------------

class USDStageVariant(PropertyGroup):
    """One variant: a name, and the collection or material it stands for."""

    name: StringProperty(
        name="Variant",
        description="The variant name, as Reality Composer Pro and RealityKit will show it",
        default="Variant",
        update=_on_variant_renamed,
    )
    #: The name before the latest rename, which is what lets a rename find
    #: everything still pointing at the old one.
    previous_name: StringProperty(options={'HIDDEN'})
    collection: PointerProperty(
        type=bpy.types.Collection,
        name="Collection",
        description="The objects this variant activates",
        update=_on_collection_picked,
    )
    material: PointerProperty(
        type=bpy.types.Material,
        name="Material",
        description="The material this variant binds to the set's targets",
        update=_on_material_picked,
    )
    applies_to: StringProperty(
        name="Offered On",
        description=(
            "The shapes that offer this finish, set with the Offered On "
            "toggles. Empty means every shape"
        ),
    )


class USDStageVariantSet(PropertyGroup):
    """One variant set, authored on the asset's root prim."""

    name: StringProperty(
        name="Variant Set",
        description="The variant set name, as Reality Composer Pro and RealityKit will show it",
        default="VariantSet",
        update=_on_set_renamed,
    )
    previous_name: StringProperty(options={'HIDDEN'})
    kind: EnumProperty(
        name="Varies",
        description="What switching this set changes",
        items=[
            (
                "GEOMETRY",
                "Shape",
                "Each variant activates its own collection of objects",
                'OUTLINER_COLLECTION',
                0,
            ),
            (
                "MATERIAL",
                "Finish",
                "Every variant keeps the same objects and rebinds their material",
                'MATERIAL',
                1,
            ),
        ],
        default="GEOMETRY",
        update=_on_kind_changed,
    )
    target_collection: PointerProperty(
        type=bpy.types.Collection,
        name="Applies To",
        description="The objects a finish set rebinds",
    )
    parent_set: StringProperty(
        name="Nest Inside",
        description=(
            "A shape set to author this finish set inside, so each shape can "
            "offer different finishes. Leave empty to offer every finish on "
            "every shape"
        ),
    )
    variants: CollectionProperty(type=USDStageVariant)
    variants_index: IntProperty(default=0)
    default_variant: StringProperty(
        name="Default",
        description="The selection the exported asset opens with",
    )


# --------------------------------------------------------------------------
# What the panel reports
# --------------------------------------------------------------------------

def _nesting_problem(scene, variant_set) -> str:
    """Why this set's Nest Inside is unusable, or an empty string."""
    parent = _set_named(scene, variant_set.parent_set)
    if parent is None:
        return f"No set named '{variant_set.parent_set}'"
    if parent == variant_set:
        return "A set cannot nest inside itself"
    if parent.kind == 'MATERIAL':
        return "Finishes nest inside a shape set, not another finish"
    return ""


def _ships_as(scene, variant_set) -> list:
    """What each shape actually ships with, which nesting can change.

    A nested finish the chosen default does not offer falls back to the first
    one that shape does offer, so the single default can be misleading.
    """
    parent = _set_named(scene, variant_set.parent_set) if variant_set.parent_set else None
    if parent is None or variant_set.kind != 'MATERIAL':
        return [f"Ships as: {variant_set.default_variant}"]

    lines = []
    for shape in parent.variants:
        offered = [
            variant.name for variant in variant_set.variants
            if not _offered_on(variant) or shape.name in _offered_on(variant)
        ]
        if not offered:
            lines.append(f"{shape.name}: nothing offered")
        elif variant_set.default_variant in offered:
            lines.append(f"{shape.name}: {variant_set.default_variant}")
        else:
            lines.append(f"{shape.name}: {offered[0]}")
    return lines


def _set_issues(context, variant_set) -> list:
    """The export's own findings about this set, errors first.

    The same validation the export runs, so the panel can never disagree with
    it: what is listed here is exactly what an export would refuse or warn
    about.
    """
    settings = context.scene.usd_stage_export_settings
    try:
        issues = validate_variant_plan(resolve_variant_plan(context, settings))
    except Exception:
        return []
    mine = [
        issue for issue in issues
        if issue.variant_set in (None, variant_set.name)
    ]
    return sorted(mine, key=lambda issue: issue.severity != ERROR)


def _wrapped(text: str, width: int) -> list:
    """``text`` broken into lines of about ``width`` characters."""
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines


def _draw_issues(layout, context, variant_set) -> None:
    issues = _set_issues(context, variant_set)
    if not issues:
        return
    # Region width is in pixels; a character is about 9 of them at UI scale 1,
    # once the panel's margins and the icon column are allowed for.
    region = getattr(context, "region", None)
    scale = getattr(getattr(context.preferences, "system", None), "ui_scale", 1.0) or 1.0
    width = max(20, int((getattr(region, "width", 300) / scale - 70) / 9))
    box = layout.box()
    column = box.column(align=True)
    for issue in issues[:6]:
        icon = 'ERROR' if issue.severity == ERROR else 'INFO'
        for number, line in enumerate(_wrapped(issue.message, width)):
            column.label(text=line, icon=icon if number == 0 else 'BLANK1')
    if len(issues) > 6:
        column.label(text=f"... and {len(issues) - 6} more", icon='BLANK1')


def _draw_offered_on(layout, scene, variant_set, variant) -> None:
    """One toggle per shape, so the finish's shapes are picked, not typed.

    Stored as the same comma-separated list the export reads; empty still
    means every shape, so a shape added later is offered rather than quietly
    left out.
    """
    parent = _set_named(scene, variant_set.parent_set)
    if parent is None or not parent.variants:
        return

    chosen = _offered_on(variant)

    # Split by hand rather than through use_property_split, which only labels
    # a property row and these are operator buttons.
    row = layout.row()
    row.use_property_split = False
    split = row.split(factor=0.4)
    label = split.row()
    label.alignment = 'RIGHT'
    label.label(text="Offered On")
    toggles = split.row(align=True)
    for shape in parent.variants:
        toggle = toggles.operator(
            "usdstage.variant_toggle_shape",
            text=shape.name,
            depress=not chosen or shape.name in chosen,
        )
        toggle.shape = shape.name

    # A name no shape carries any more still has to be visible to be fixed.
    known = {shape.name for shape in parent.variants}
    stale = [name for name in chosen if name not in known]
    if stale:
        warning = layout.row()
        warning.use_property_split = False
        warning.label(text=f"No longer a shape: {', '.join(stale)}", icon='ERROR')


# --------------------------------------------------------------------------
# Lists and panel
# --------------------------------------------------------------------------

class USDSTAGE_UL_variant_sets(UIList):
    """The scene's declared variant sets, each with what it varies."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        row = layout.row(align=True)
        is_finish = item.kind == 'MATERIAL'
        row.prop(
            item, "name", text="", emboss=False,
            icon='MATERIAL' if is_finish else 'OUTLINER_COLLECTION',
        )
        count = len(item.variants)
        kind = "Finish" if is_finish else "Shape"
        row.label(text=f"{kind} · {count} variant{'s' if count != 1 else ''}")


class USDSTAGE_UL_variants(UIList):
    """The variants of one set, with the shipped default marked."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        row = layout.row(align=True)
        is_default = item.name == data.default_variant
        default = row.operator(
            "usdstage.variant_set_default",
            text="",
            icon='RADIOBUT_ON' if is_default else 'RADIOBUT_OFF',
            emboss=False,
        )
        default.variant = item.name
        row.prop(item, "name", text="", emboss=False)
        # Refused at export; saying so here costs nothing and saves a failed
        # export later.
        if data.kind == 'MATERIAL':
            if item.material is None:
                row.label(text="No material", icon='ERROR')
            else:
                row.label(text=item.material.name, icon='MATERIAL')
        elif item.collection is None:
            row.label(text="No collection", icon='ERROR')
        else:
            row.label(text=item.collection.name, icon='OUTLINER_COLLECTION')


def _draw_add_buttons(layout, compact: bool) -> None:
    for idname, text, icon in (
        ("usdstage.variant_set_add_from_collection", "Shape Set", 'OUTLINER_COLLECTION'),
        ("usdstage.variant_set_add_finish", "Finish Set", 'MATERIAL'),
        ("usdstage.variant_set_add_empty", "Empty", 'ADD'),
    ):
        layout.operator(idname, text="" if compact else text, icon=icon)


class USDSTAGE_PT_export_variants(Panel):
    """Declare USD variant sets built from collections and materials."""

    bl_label = "Variants"
    bl_idname = "USDSTAGE_PT_export_variants"
    bl_parent_id = "USDSTAGE_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "USD Stage"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw_header(self, context):
        self.layout.label(text="", icon='OUTLINER_COLLECTION')

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.usd_stage_export_settings
        layout.enabled = not _is_job_running(settings)
        variant_sets = scene.usd_stage_variant_sets

        if not variant_sets:
            column = layout.column(align=True)
            column.label(text="No variant sets.", icon='INFO')
            column.label(text="Shape: each variant is a child collection.")
            column.label(text="Finish: each variant is a material.")
            _draw_add_buttons(layout.row(align=True), compact=False)
            return

        layout.prop(settings, "export_variants")

        column = layout.column()
        column.enabled = bool(getattr(settings, "export_variants", True))

        row = column.row()
        row.template_list(
            "USDSTAGE_UL_variant_sets", "", scene, "usd_stage_variant_sets",
            scene, "usd_stage_variant_sets_index", rows=3,
        )
        buttons = row.column(align=True)
        _draw_add_buttons(buttons, compact=True)
        buttons.separator()
        buttons.operator("usdstage.variant_set_remove", text="", icon='REMOVE')

        index = scene.usd_stage_variant_sets_index
        if not 0 <= index < len(variant_sets):
            return
        variant_set = variant_sets[index]

        # What the set varies, and over what: settings of the set as a whole.
        set_box = column.column(align=True)
        set_box.use_property_split = True
        set_box.use_property_decorate = False
        set_box.row().prop(variant_set, "kind", expand=True)
        if variant_set.kind == 'MATERIAL':
            set_box.prop(variant_set, "target_collection")
            set_box.prop_search(
                variant_set, "parent_set", scene, "usd_stage_variant_sets",
                text="Nest Inside",
            )
            problem = (
                _nesting_problem(scene, variant_set) if variant_set.parent_set else ""
            )
            if problem:
                set_box.label(text=problem, icon='ERROR')

        row = column.row()
        row.template_list(
            "USDSTAGE_UL_variants", "", variant_set, "variants",
            variant_set, "variants_index", rows=3,
        )
        buttons = row.column(align=True)
        buttons.operator("usdstage.variant_add", text="", icon='ADD')
        buttons.operator("usdstage.variant_remove", text="", icon='REMOVE')

        variant_index = variant_set.variants_index
        if 0 <= variant_index < len(variant_set.variants):
            variant = variant_set.variants[variant_index]
            # The selected variant's own settings.
            variant_box = column.column(align=True)
            variant_box.use_property_split = True
            variant_box.use_property_decorate = False
            if variant_set.kind == 'MATERIAL':
                variant_box.prop(variant, "material")
                if variant_set.parent_set and not _nesting_problem(scene, variant_set):
                    _draw_offered_on(variant_box, scene, variant_set, variant)
            else:
                variant_box.prop(variant, "collection")
                preview = column.operator("usdstage.variant_preview", icon='HIDE_OFF')
                preview.variant = variant.name

        if not variant_set.default_variant:
            column.label(text="No default selection chosen", icon='ERROR')
        else:
            for line in _ships_as(scene, variant_set):
                column.label(text=line)

        _draw_issues(column, context, variant_set)


@bpy.app.handlers.persistent
def _sync_previous_names(*_args) -> None:
    """Record every current name as the one a first rename starts from."""
    for scene in bpy.data.scenes:
        for variant_set in getattr(scene, "usd_stage_variant_sets", ()):
            if variant_set.previous_name != variant_set.name:
                variant_set.previous_name = variant_set.name
            for variant in variant_set.variants:
                if variant.previous_name != variant.name:
                    variant.previous_name = variant.name


_CLASSES = (
    USDStageVariant,
    USDStageVariantSet,
    USDSTAGE_UL_variant_sets,
    USDSTAGE_UL_variants,
    USDSTAGE_PT_export_variants,
)


def register():
    """Register the variant property groups and panel."""
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.usd_stage_variant_sets = CollectionProperty(type=USDStageVariantSet)
    bpy.types.Scene.usd_stage_variant_sets_index = IntProperty(default=0)
    # A file declared before names were tracked starts with none recorded.
    if _sync_previous_names not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sync_previous_names)
    try:
        _sync_previous_names()
    except Exception:
        # Registration can run where bpy.data is restricted; load_post covers it.
        pass


def unregister():
    """Unregister the variant property groups and panel."""
    if _sync_previous_names in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sync_previous_names)
    del bpy.types.Scene.usd_stage_variant_sets_index
    del bpy.types.Scene.usd_stage_variant_sets
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
