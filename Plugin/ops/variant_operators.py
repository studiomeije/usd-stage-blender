"""Operators behind the Variants panel.

Declaring a variant set is the one thing the user has to do that the scene
cannot imply: collections group a scene for many reasons, and only the user
knows which grouping is a variant set. These operators keep that declaration
cheap - one click for the common case, where a parent collection's children
are already the variants.
"""

from __future__ import annotations

import bpy
from bpy.props import IntProperty, StringProperty
from bpy.types import Operator

from ..export.variants import suggest_identifier


def _variant_sets(context):
    return context.scene.usd_stage_variant_sets


def _active_set(context):
    sets = _variant_sets(context)
    index = context.scene.usd_stage_variant_sets_index
    if 0 <= index < len(sets):
        return sets[index]
    return None


def _unique_name(existing, base: str) -> str:
    taken = {item.name for item in existing}
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def _find_layer_collection(layer_collection, collection):
    """The first layer collection in this tree standing for ``collection``."""
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _is_visible(context, collection) -> bool:
    layer_collection = _find_layer_collection(
        context.view_layer.layer_collection, collection
    )
    if layer_collection is None:
        return False
    return not layer_collection.exclude and not collection.hide_render


class USDSTAGE_OT_variant_set_add_from_collection(Operator):
    """Create a variant set from a collection whose children are the variants"""

    bl_idname = "usdstage.variant_set_add_from_collection"
    bl_label = "From Collection"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name: StringProperty(
        name="Parent Collection",
        description="Its child collections become the variants",
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop_search(
            self, "collection_name", bpy.data, "collections", text="Collection"
        )

    def execute(self, context):
        parent = bpy.data.collections.get(self.collection_name)
        if parent is None:
            self.report({'ERROR'}, "Pick a collection")
            return {'CANCELLED'}
        children = list(parent.children)
        if not children:
            self.report(
                {'ERROR'},
                f"'{parent.name}' has no child collections to use as variants",
            )
            return {'CANCELLED'}

        sets = _variant_sets(context)
        variant_set = sets.add()
        # Sanitized here, where the user can see and change it. Export-time
        # naming is strict precisely because by then it is too late to ask.
        variant_set.name = _unique_name(
            sets[:-1], suggest_identifier(parent.name, "Set")
        )

        visible = None
        for child in children:
            variant = variant_set.variants.add()
            variant.name = _unique_name(
                variant_set.variants[:-1], suggest_identifier(child.name)
            )
            variant.collection = child
            if visible is None and _is_visible(context, child):
                visible = variant.name

        variant_set.default_variant = visible or variant_set.variants[0].name
        context.scene.usd_stage_variant_sets_index = len(sets) - 1

        renamed = variant_set.name != parent.name
        self.report(
            {'INFO'},
            f"Added variant set '{variant_set.name}' with {len(children)} variants"
            + (" (name adjusted for USD)" if renamed else ""),
        )
        return {'FINISHED'}


class USDSTAGE_OT_variant_set_add_empty(Operator):
    """Add an empty variant set to fill in by hand"""

    bl_idname = "usdstage.variant_set_add_empty"
    bl_label = "Empty Set"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sets = _variant_sets(context)
        variant_set = sets.add()
        variant_set.name = _unique_name(sets[:-1], "VariantSet")
        context.scene.usd_stage_variant_sets_index = len(sets) - 1
        return {'FINISHED'}


class USDSTAGE_OT_variant_set_remove(Operator):
    """Remove the selected variant set"""

    bl_idname = "usdstage.variant_set_remove"
    bl_label = "Remove Variant Set"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_set(context) is not None

    def execute(self, context):
        sets = _variant_sets(context)
        index = context.scene.usd_stage_variant_sets_index
        removed = sets[index].name
        sets.remove(index)
        # A set that nested inside this one would otherwise keep pointing at a
        # name that no longer exists, and only find out at export.
        orphaned = 0
        for variant_set in sets:
            if variant_set.parent_set == removed:
                variant_set.parent_set = ""
                orphaned += 1
        context.scene.usd_stage_variant_sets_index = max(0, min(index, len(sets) - 1))
        if orphaned:
            self.report(
                {'INFO'},
                f"{orphaned} set(s) no longer nest inside '{removed}'",
            )
        return {'FINISHED'}


class USDSTAGE_OT_variant_add(Operator):
    """Add a variant to the selected set"""

    bl_idname = "usdstage.variant_add"
    bl_label = "Add Variant"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _active_set(context) is not None

    def execute(self, context):
        variant_set = _active_set(context)
        variant = variant_set.variants.add()
        variant.name = _unique_name(variant_set.variants[:-1], "Variant")
        if not variant_set.default_variant:
            variant_set.default_variant = variant.name
        variant_set.variants_index = len(variant_set.variants) - 1
        return {'FINISHED'}


class USDSTAGE_OT_variant_remove(Operator):
    """Remove the selected variant"""

    bl_idname = "usdstage.variant_remove"
    bl_label = "Remove Variant"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        variant_set = _active_set(context)
        return variant_set is not None and len(variant_set.variants)

    def execute(self, context):
        variant_set = _active_set(context)
        index = variant_set.variants_index
        if not 0 <= index < len(variant_set.variants):
            return {'CANCELLED'}
        removed = variant_set.variants[index].name
        variant_set.variants.remove(index)
        variant_set.variants_index = max(0, min(index, len(variant_set.variants) - 1))
        # The default selection has to name a variant that still exists.
        if variant_set.default_variant == removed:
            variant_set.default_variant = (
                variant_set.variants[0].name if len(variant_set.variants) else ""
            )
        return {'FINISHED'}


class USDSTAGE_OT_variant_set_default(Operator):
    """Ship this variant as the selection the asset opens with"""

    bl_idname = "usdstage.variant_set_default"
    bl_label = "Set Default Variant"
    bl_options = {'REGISTER', 'UNDO'}

    variant: StringProperty()
    set_index: IntProperty(default=-1)

    def execute(self, context):
        sets = _variant_sets(context)
        index = self.set_index if self.set_index >= 0 else (
            context.scene.usd_stage_variant_sets_index
        )
        if not 0 <= index < len(sets):
            return {'CANCELLED'}
        sets[index].default_variant = self.variant
        return {'FINISHED'}


class USDSTAGE_OT_variant_toggle_shape(Operator):
    """Offer this finish on a shape, or stop offering it there"""

    bl_idname = "usdstage.variant_toggle_shape"
    bl_label = "Toggle Shape"
    bl_options = {'REGISTER', 'UNDO'}

    shape: StringProperty()

    def execute(self, context):
        variant_set = _active_set(context)
        if variant_set is None:
            return {'CANCELLED'}
        index = variant_set.variants_index
        if not 0 <= index < len(variant_set.variants):
            return {'CANCELLED'}
        variant = variant_set.variants[index]

        parent = next(
            (other for other in _variant_sets(context)
             if other.name == variant_set.parent_set),
            None,
        )
        if parent is None:
            self.report({'ERROR'}, "This set does not nest inside a shape set")
            return {'CANCELLED'}

        shapes = [child.name for child in parent.variants]
        # An empty list means every shape, so start from that when narrowing.
        offered = [
            name.strip() for name in variant.applies_to.split(",") if name.strip()
        ] or list(shapes)

        if self.shape in offered:
            if len(offered) == 1:
                self.report(
                    {'WARNING'},
                    f"'{variant.name}' has to be offered on at least one shape",
                )
                return {'CANCELLED'}
            offered.remove(self.shape)
        else:
            offered.append(self.shape)

        ordered = [name for name in shapes if name in offered]
        # Offered everywhere is stored as empty, so adding a shape later keeps
        # offering it rather than quietly excluding the new one.
        variant.applies_to = (
            "" if len(ordered) == len(shapes) else ",".join(ordered)
        )
        return {'FINISHED'}


class USDSTAGE_OT_variant_preview(Operator):
    """Show this variant in the viewport, hiding the others in its set"""

    bl_idname = "usdstage.variant_preview"
    bl_label = "Preview in Viewport"
    bl_options = {'REGISTER', 'UNDO'}

    variant: StringProperty()

    @classmethod
    def poll(cls, context):
        return _active_set(context) is not None

    def execute(self, context):
        variant_set = _active_set(context)
        root = context.view_layer.layer_collection
        shown = False
        for variant in variant_set.variants:
            if variant.collection is None:
                continue
            layer_collection = _find_layer_collection(root, variant.collection)
            if layer_collection is None:
                continue
            wanted = variant.name == self.variant
            layer_collection.exclude = not wanted
            shown = shown or wanted
        if not shown:
            self.report({'WARNING'}, f"'{self.variant}' has no collection to show")
            return {'CANCELLED'}
        context.view_layer.update()
        return {'FINISHED'}


_CLASSES = (
    USDSTAGE_OT_variant_set_add_from_collection,
    USDSTAGE_OT_variant_set_add_empty,
    USDSTAGE_OT_variant_set_remove,
    USDSTAGE_OT_variant_add,
    USDSTAGE_OT_variant_remove,
    USDSTAGE_OT_variant_set_default,
    USDSTAGE_OT_variant_toggle_shape,
    USDSTAGE_OT_variant_preview,
)


def register():
    """Register variant operators."""
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    """Unregister variant operators."""
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
