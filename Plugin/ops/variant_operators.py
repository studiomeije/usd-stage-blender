"""Operators behind the Variants panel.

Declaring a variant set is the one thing the user has to do that the scene
cannot imply: collections group a scene for many reasons, and only the user
knows which grouping is a variant set. These operators keep that declaration
cheap - one click for the common case, where a parent collection's children
are already the variants.
"""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator, PropertyGroup, UIList

from ..export.variants import (
    finish_name_from_material,
    guess_finish_prefix,
    suggest_identifier,
    unique_name,
)


def _variant_sets(context):
    return context.scene.usd_stage_variant_sets


def _active_set(context):
    sets = _variant_sets(context)
    index = context.scene.usd_stage_variant_sets_index
    if 0 <= index < len(sets):
        return sets[index]
    return None


def _unique_name(existing, base: str) -> str:
    return unique_name((item.name for item in existing), base)


def _active_collection(context):
    """The collection selected in the Outliner, unless it is the scene's own."""
    layer_collection = getattr(context.view_layer, "active_layer_collection", None)
    collection = getattr(layer_collection, "collection", None)
    if collection is None or collection == context.scene.collection:
        return None
    return collection


def _matching_materials(prefix: str) -> list:
    """Surface materials whose names start with ``prefix``, in name order."""
    return sorted(
        (
            material for material in bpy.data.materials
            if material.name.startswith(prefix)
            and not getattr(material, "is_grease_pencil", False)
        ),
        key=lambda material: material.name,
    )


def _worn_material(collection):
    """The first material a mesh in ``collection`` (or below) wears."""
    stack = [collection] if collection is not None else []
    while stack:
        current = stack.pop(0)
        for obj in current.objects:
            for slot in getattr(obj, "material_slots", ()):
                if slot.material is not None:
                    return slot.material
        stack.extend(current.children)
    return None


def _find_layer_collection(layer_collection, collection):
    """The first layer collection in this tree standing for ``collection``."""
    if layer_collection.collection == collection:
        return layer_collection
    for child in layer_collection.children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _layer_collection_chain(root, collection):
    if root.collection == collection:
        return [root]
    for child in root.children:
        found = _layer_collection_chain(child, collection)
        if found:
            return [root, *found]
    return []


class USDStageMaterialChoice(PropertyGroup):
    material_name: StringProperty()
    selected: BoolProperty(name="Include Material", default=False)


class USDSTAGE_UL_material_choices(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        layout.prop(item, "selected", text=item.material_name, icon='MATERIAL')


def _is_visible(context, collection) -> bool:
    layer_collection = _find_layer_collection(
        context.view_layer.layer_collection, collection
    )
    if layer_collection is None:
        return False
    return not layer_collection.exclude and not collection.hide_render


class USDSTAGE_OT_variant_set_add_from_collection(Operator):
    """Create a shape set from a collection whose child collections are the variants"""

    bl_idname = "usdstage.variant_set_add_from_collection"
    bl_label = "Geometry Set from Collection"
    bl_options = {'REGISTER', 'UNDO'}

    collection_name: StringProperty(
        name="Parent Collection",
        description="Its child collections become the variants",
    )

    def invoke(self, context, event):
        # The collection selected in the Outliner is usually the one meant.
        active = _active_collection(context)
        if not self.collection_name and active is not None and active.children:
            self.collection_name = active.name
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


class USDSTAGE_OT_variant_set_add_finish(Operator):
    """Create a finish set: one variant per material of a family, rebinding a collection's objects"""

    bl_idname = "usdstage.variant_set_add_finish"
    bl_label = "Material Set"
    bl_options = {'REGISTER', 'UNDO'}

    target_name: StringProperty(
        name="Applies To",
        description="The collection whose objects each material variant rebinds",
    )
    prefix: StringProperty(
        name="Materials Starting With",
        description=(
            "Every material whose name starts with this becomes a variant, "
            "named after the rest of its name. Leave empty to add variants by hand"
        ),
    )
    material_choices: CollectionProperty(type=USDStageMaterialChoice, options={'SKIP_SAVE'})
    material_choice_index: IntProperty(options={'SKIP_SAVE'})
    picker_initialized: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    selection_mode: EnumProperty(
        name="Choose Materials",
        items=[('PICK', "Pick Materials", "Choose materials without renaming them"),
               ('PREFIX', "Match Prefix", "Include materials whose names share a prefix")],
        default='PICK', options={'SKIP_SAVE'},
    )

    def invoke(self, context, event):
        target = _active_collection(context)
        if not self.target_name and target is not None:
            self.target_name = target.name
        if not self.prefix:
            worn = _worn_material(bpy.data.collections.get(self.target_name))
            if worn is not None:
                self.prefix = guess_finish_prefix(worn.name)
        self.material_choices.clear()
        worn = _worn_material(bpy.data.collections.get(self.target_name))
        for material in _matching_materials(""):
            choice = self.material_choices.add()
            choice.material_name = material.name
            choice.selected = material == worn
        self.picker_initialized = True
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.prop_search(self, "target_name", bpy.data, "collections", text="Applies To")
        layout.row().prop(self, "selection_mode", expand=True)
        if self.selection_mode == 'PICK':
            layout.template_list("USDSTAGE_UL_material_choices", "", self, "material_choices", self, "material_choice_index", rows=6)
            count = sum(choice.selected and bpy.data.materials.get(choice.material_name) is not None for choice in self.material_choices)
            layout.label(text=f"{count} materials selected")
        else:
            layout.prop(self, "prefix")
        if self.selection_mode == 'PREFIX' and self.prefix:
            found = [material.name for material in _matching_materials(self.prefix)]
            if found:
                shown = ", ".join(found[:4]) + (", ..." if len(found) > 4 else "")
                layout.label(text=f"{len(found)} materials: {shown}", icon='MATERIAL')
            else:
                layout.label(text="No material starts with that", icon='ERROR')

    def execute(self, context):
        target = bpy.data.collections.get(self.target_name)
        if target is None:
            self.report({'ERROR'}, "Pick the collection the materials apply to")
            return {'CANCELLED'}
        picked = self.picker_initialized and self.selection_mode == 'PICK'
        materials = ([bpy.data.materials[choice.material_name] for choice in self.material_choices if choice.selected and choice.material_name in bpy.data.materials]
                     if picked else _matching_materials(self.prefix) if self.prefix else [])
        if picked and not materials:
            self.report({'ERROR'}, "Choose at least one material")
            return {'CANCELLED'}
        if not picked and self.prefix and not materials:
            self.report({'ERROR'}, f"No material starts with '{self.prefix}'")
            return {'CANCELLED'}

        sets = _variant_sets(context)
        variant_set = sets.add()
        variant_set.kind = 'MATERIAL'
        base = (self.prefix.strip("_.- ") if not picked else "") or f"{target.name}_Materials"
        variant_set.name = _unique_name(sets[:-1], suggest_identifier(base, "Materials"))
        variant_set.target_collection = target
        for material in materials:
            variant = variant_set.variants.add()
            variant.name = _unique_name(
                variant_set.variants[:-1], finish_name_from_material(material.name, "" if picked else self.prefix)
            )
            variant.material = material

        # Ship what the objects already wear, when it is one of the finishes.
        worn = _worn_material(target)
        default = next(
            (variant.name for variant in variant_set.variants if variant.material == worn),
            variant_set.variants[0].name if len(variant_set.variants) else "",
        )
        variant_set.default_variant = default
        context.scene.usd_stage_variant_sets_index = len(sets) - 1
        self.report(
            {'INFO'},
            f"Added material set '{variant_set.name}' with {len(materials)} materials",
        )
        return {'FINISHED'}


class USDSTAGE_OT_variant_set_add_empty(Operator):
    """Add an empty variant set to fill in by hand"""

    bl_idname = "usdstage.variant_set_add_empty"
    bl_label = "Empty Variant Set"
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
        dependents = [entry for entry in _variant_sets(context) if entry.parent_set == variant_set.name]
        exclusive = [f"{entry.name}/{variant.name}" for entry in dependents for variant in entry.variants
                     if {name.strip() for name in variant.applies_to.split(',') if name.strip()} == {removed}]
        if exclusive:
            self.report({'WARNING'}, "Cannot remove this geometry: assign or remove materials offered only on it first (" + ", ".join(exclusive) + ")")
            return {'CANCELLED'}
        variant_set.variants.remove(index)
        for entry in dependents:
            for variant in entry.variants:
                if variant.applies_to:
                    variant.applies_to = ','.join(name.strip() for name in variant.applies_to.split(',')
                                                if name.strip() and name.strip() != removed)
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
        if not any(variant.name == self.variant for variant in sets[index].variants):
            self.report({'WARNING'}, "Choose an existing variant as the export default")
            return {'CANCELLED'}
        sets[index].default_variant = self.variant
        return {'FINISHED'}


class USDSTAGE_OT_variant_set_parent(Operator):
    """Choose the geometry set that determines which materials are offered"""

    bl_idname = "usdstage.variant_set_parent"
    bl_label = "Set Geometry Parent"
    bl_options = {'REGISTER', 'UNDO'}
    parent: StringProperty()

    def execute(self, context):
        entry = _active_set(context)
        if entry is None or entry.kind != 'MATERIAL':
            return {'CANCELLED'}
        parent = next((candidate for candidate in _variant_sets(context)
                       if candidate.name == self.parent and candidate.kind == 'GEOMETRY'), None)
        if self.parent and parent is None:
            self.report({'WARNING'}, "Choose an existing geometry set")
            return {'CANCELLED'}
        if entry.parent_set == self.parent:
            return {'FINISHED'}
        entry.parent_set = self.parent
        # Restrictions belong to the old parent; retaining names can silently
        # target different geometry in a new parent.
        for variant in entry.variants:
            variant.applies_to = ""
        return {'FINISHED'}


class USDSTAGE_MT_geometry_parents(Menu):
    bl_label = "Geometry Set"

    def draw(self, context):
        self.layout.operator("usdstage.variant_set_parent", text="All Geometry (Independent)").parent = ""
        for candidate in _variant_sets(context):
            if candidate.kind == 'GEOMETRY':
                self.layout.operator("usdstage.variant_set_parent", text=candidate.name, icon='OUTLINER_COLLECTION').parent = candidate.name


class USDSTAGE_OT_variant_toggle_shape(Operator):
    """Offer this finish on a shape, or stop offering it there"""

    bl_idname = "usdstage.variant_toggle_shape"
    bl_label = "Toggle Geometry Variant"
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
            self.report({'ERROR'}, "This set does not nest inside a geometry set")
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
                    f"'{variant.name}' has to be offered on at least one geometry variant",
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


class USDSTAGE_OT_variant_select(Operator):
    """Select a variant to edit without changing the exported default"""

    bl_idname = "usdstage.variant_select"
    bl_label = "Select Variant"
    index: IntProperty(default=0)

    def execute(self, context):
        variant_set = _active_set(context)
        if variant_set is None or not 0 <= self.index < len(variant_set.variants):
            return {'CANCELLED'}
        variant_set.variants_index = self.index
        return {'FINISHED'}


class USDSTAGE_OT_variant_generate_previews(Operator):
    """Generate native Blender previews from the current shapes or finishes"""

    bl_idname = "usdstage.variant_generate_previews"
    bl_label = "Refresh Variant Previews"

    @classmethod
    def poll(cls, context):
        return _active_set(context) is not None

    def execute(self, context):
        variant_set = _active_set(context)
        generated = 0
        preserved = 0
        seen = set()
        for variant in variant_set.variants:
            source = variant.material if variant_set.kind == 'MATERIAL' else variant.collection
            if source is None:
                continue
            if source.as_pointer() in seen:
                continue
            seen.add(source.as_pointer())
            preview = source.preview
            if preview and (preview.is_image_custom or preview.is_icon_custom):
                preserved += 1
                continue
            try:
                source.asset_generate_preview()
                generated += 1
            except (RuntimeError, TypeError) as exc:
                self.report({'WARNING'}, f"{variant.name}: {exc}")
        if not generated:
            if preserved:
                self.report({'INFO'}, f"Preserved {preserved} custom preview(s)")
                return {'FINISHED'}
            self.report({'WARNING'}, "Assign collections or materials before generating previews")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Updating {generated} preview(s); preserved {preserved} custom preview(s)")
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
        selected = next((variant for variant in variant_set.variants if variant.name == self.variant), None)
        chain = _layer_collection_chain(root, selected.collection) if selected and selected.collection else []
        if not chain:
            self.report({'WARNING'}, f"'{self.variant}' has no collection in this view layer to show")
            return {'CANCELLED'}
        changes = []

        def assign(owner, name, value):
            previous = getattr(owner, name)
            if previous != value:
                changes.append((owner, name, previous))
                setattr(owner, name, value)

        # Validate first, then reveal ancestors and the chosen collection.
        # A failure restores every changed flag, including child excludes
        # Blender resets when its parent is re-enabled.
        all_layers = []
        stack = [root]
        while stack:
            layer = stack.pop()
            all_layers.append((layer, layer.exclude))
            stack.extend(layer.children)
        touched_layers = set()
        try:
            for layer in chain:
                touched_layers.add(layer.as_pointer())
                assign(layer, 'exclude', False)
                assign(layer.collection, 'hide_viewport', False)
            stack = list(chain[-1].children)
            while stack:
                layer = stack.pop()
                touched_layers.add(layer.as_pointer())
                assign(layer, 'exclude', False)
                assign(layer.collection, 'hide_viewport', False)
                stack.extend(layer.children)
            for variant in variant_set.variants:
                if variant.collection is None:
                    continue
                layer_collection = _find_layer_collection(root, variant.collection)
                if layer_collection is not None:
                    touched_layers.add(layer_collection.as_pointer())
                    assign(layer_collection, 'exclude', variant != selected)
            for layer, excluded in all_layers:
                if layer.as_pointer() not in touched_layers:
                    assign(layer, 'exclude', excluded)
            for obj in selected.collection.all_objects:
                assign(obj, 'hide_viewport', False)
                hidden = obj.hide_get()
                if hidden:
                    changes.append((obj, 'hide_set', hidden))
                    obj.hide_set(False)
            context.view_layer.update()
        except Exception as exc:
            for owner, name, previous in reversed(changes):
                if name == 'hide_set':
                    owner.hide_set(previous)
                else:
                    setattr(owner, name, previous)
            for layer, excluded in all_layers:
                layer.exclude = excluded
            context.view_layer.update()
            self.report({'WARNING'}, f"Could not preview '{self.variant}': {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


_CLASSES = (
    USDStageMaterialChoice,
    USDSTAGE_UL_material_choices,
    USDSTAGE_OT_variant_set_add_from_collection,
    USDSTAGE_OT_variant_set_add_finish,
    USDSTAGE_OT_variant_set_add_empty,
    USDSTAGE_OT_variant_set_remove,
    USDSTAGE_OT_variant_add,
    USDSTAGE_OT_variant_remove,
    USDSTAGE_OT_variant_set_default,
    USDSTAGE_OT_variant_set_parent,
    USDSTAGE_MT_geometry_parents,
    USDSTAGE_OT_variant_select,
    USDSTAGE_OT_variant_generate_previews,
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
