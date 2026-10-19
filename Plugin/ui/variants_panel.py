"""The Variants panel: declaring USD variant sets on the scene.

Blender has no variants of its own - the 2024 design work proposed some, but
nothing shipped - and its USD exporter writes none. So the declaration lives
here, on the scene, and the export pass in ``export/variants.py`` reads it.

The sets are registered on ``Scene`` rather than on ``USDStageExportSettings``
on purpose. They are scene content, not a remembered preference: the settings
group is serialized into add-on preferences as last-used values and carried
into the next scene, which is right for a bake resolution and wrong for a list
of collections belonging to this file.
"""

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Panel, PropertyGroup, UIList

from .panel import _is_job_running


class USDStageVariant(PropertyGroup):
    """One variant: a name, and the collection whose objects it owns."""

    name: StringProperty(
        name="Variant",
        description="The variant name, as Reality Composer Pro and RealityKit will show it",
        default="Variant",
    )
    collection: PointerProperty(
        type=bpy.types.Collection,
        name="Collection",
        description="The objects this variant activates",
    )
    material: PointerProperty(
        type=bpy.types.Material,
        name="Material",
        description="The material this variant binds to the set's targets",
    )
    applies_to: StringProperty(
        name="Only For",
        description=(
            "Comma-separated shape variants that offer this finish. Leave "
            "empty to offer it on every shape"
        ),
    )


class USDStageVariantSet(PropertyGroup):
    """One variant set, authored on the asset's root prim."""

    name: StringProperty(
        name="Variant Set",
        description="The variant set name, as Reality Composer Pro and RealityKit will show it",
        default="VariantSet",
    )
    kind: EnumProperty(
        name="Varies",
        description="What switching this set changes",
        items=[
            (
                "GEOMETRY",
                "Shape",
                "Each variant activates its own collection of objects",
            ),
            (
                "MATERIAL",
                "Finish",
                "Every variant keeps the same objects and rebinds their material",
            ),
        ],
        default="GEOMETRY",
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


class USDSTAGE_UL_variant_sets(UIList):
    """The scene's declared variant sets."""

    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='PROPERTIES')
        count = len(item.variants)
        row.label(text=f"{count} variant{'s' if count != 1 else ''}")


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


class USDSTAGE_PT_export_variants(Panel):
    """Declare USD variant sets built from collections."""

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
            column.label(text="Group each variant in its own collection.")
            row = layout.row(align=True)
            row.operator(
                "usdstage.variant_set_add_from_collection",
                icon='OUTLINER_COLLECTION',
            )
            row.operator("usdstage.variant_set_add_empty", icon='ADD')
            return

        layout.prop(settings, "export_variants")

        column = layout.column()
        column.enabled = bool(getattr(settings, "export_variants", True))

        row = column.row()
        row.template_list(
            "USDSTAGE_UL_variant_sets", "", scene, "usd_stage_variant_sets",
            scene, "usd_stage_variant_sets_index", rows=2,
        )
        buttons = row.column(align=True)
        buttons.operator(
            "usdstage.variant_set_add_from_collection", text="", icon='OUTLINER_COLLECTION'
        )
        buttons.operator("usdstage.variant_set_add_empty", text="", icon='ADD')
        buttons.operator("usdstage.variant_set_remove", text="", icon='REMOVE')

        index = scene.usd_stage_variant_sets_index
        if not 0 <= index < len(variant_sets):
            return
        variant_set = variant_sets[index]

        row = column.row()
        row.template_list(
            "USDSTAGE_UL_variants", "", variant_set, "variants",
            variant_set, "variants_index", rows=3,
        )
        buttons = row.column(align=True)
        buttons.operator("usdstage.variant_add", text="", icon='ADD')
        buttons.operator("usdstage.variant_remove", text="", icon='REMOVE')

        variant_index = variant_set.variants_index
        if not 0 <= variant_index < len(variant_set.variants):
            return
        variant = variant_set.variants[variant_index]

        box = column.column(align=True)
        box.use_property_split = True
        box.use_property_decorate = False
        box.prop(variant_set, "kind")
        if variant_set.kind == 'MATERIAL':
            box.prop(variant_set, "target_collection")
            box.prop_search(
                variant_set, "parent_set", scene, "usd_stage_variant_sets",
                text="Nest Inside",
            )
            box.prop(variant, "material")
            if variant_set.parent_set:
                box.prop(variant, "applies_to")
        else:
            box.prop(variant, "collection")

        if variant_set.kind != 'MATERIAL':
            preview = column.operator("usdstage.variant_preview", icon='HIDE_OFF')
            preview.variant = variant.name

        if variant_set.default_variant:
            column.label(text=f"Ships as: {variant_set.default_variant}")
        else:
            column.label(text="No default selection chosen", icon='ERROR')


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


def unregister():
    """Unregister the variant property groups and panel."""
    del bpy.types.Scene.usd_stage_variant_sets_index
    del bpy.types.Scene.usd_stage_variant_sets
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
