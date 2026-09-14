"""
Animation export preparation for USD Stage for Blender.

Concatenates compatible actions into a single sequential timeline and bakes
each target independently to improve compatibility with Reality Composer Pro.

Blender 5.2 Actions are layered and can contain multiple slots.  A slot is the
ownership boundary: an Action that has an ``OBCharacter`` slot must never be
broadcast to every object merely because they all have ``id_type == OBJECT``.
This module therefore builds one global clip timeline, but applies only the
``(Action, ActionSlot)`` bindings that belong to each target.
"""

from __future__ import annotations

import math
from typing import Any

import bpy


def _rna_identity(value) -> int:
    """Stable Blender datablock identity, with a test-double fallback."""
    try:
        return int(value.as_pointer())
    except Exception:
        return id(value)


def collect_export_objects(context, settings) -> list:
    """Return the dependency-closed object set for the requested export.

    Blender's ``selected_objects_only`` USD option already weak-exports parent
    transform chains and expands collection instances without selecting their
    prototypes.  Selecting either would change semantics by exporting parent
    data or duplicating prototype objects.  Shape keys live on the selected
    mesh data and likewise need no extra object selection.

    The one dependency Blender 5.2 does require in the native selection is the
    deforming armature for a selected skinned mesh.  Add only that dependency
    (when armature export is enabled) so the exporter cannot emit an orphaned
    SkelBinding.

    An empty selected-only export is intentionally empty.  Callers treat that
    as an error instead of silently falling back to the entire scene.
    """
    scene_objects = list(context.scene.objects)
    if not bool(getattr(settings, "selected_objects_only", False)):
        return [
            obj
            for obj in view_layer_objects(context, settings)
            if _is_exportable_object(obj, settings)
        ]

    selected = [
        obj
        for obj in context.selected_objects
        if _is_exportable_object(obj, settings)
    ]
    if not selected:
        return []

    closure: list = []
    seen: set[int] = set()

    def add(obj) -> bool:
        if obj is None:
            return False
        key = _rna_identity(obj)
        if key in seen:
            return False
        seen.add(key)
        closure.append(obj)
        return True

    for obj in selected:
        add(obj)

    if bool(getattr(settings, "export_armatures", True)):
        evaluation_mode = str(getattr(settings, "evaluation_mode", "RENDER"))
        for obj in selected:
            if getattr(obj, "type", None) != "MESH":
                continue
            for modifier in getattr(obj, "modifiers", []) or []:
                if (
                    getattr(modifier, "type", None) == "ARMATURE"
                    and (
                        getattr(modifier, "show_render", True)
                        if evaluation_mode == "RENDER"
                        else getattr(modifier, "show_viewport", True)
                    )
                ):
                    armature = getattr(modifier, "object", None)
                    if armature is None:
                        raise RuntimeError(
                            f"Selected skinned mesh '{obj.name}' has an enabled "
                            "Armature modifier with no target object."
                        )
                    add(armature)

    # Preserve scene order for deterministic exports, then retain a linked
    # armature that is not directly linked into the active scene.  Selection
    # application will reject an unavailable dependency rather than falling
    # back to an incomplete rig export.
    closed_ids = {_rna_identity(obj) for obj in closure}
    ordered = [obj for obj in scene_objects if _rna_identity(obj) in closed_ids]
    ordered_ids = {_rna_identity(obj) for obj in ordered}
    ordered.extend(obj for obj in closure if _rna_identity(obj) not in ordered_ids)
    return ordered


def view_layer_objects(context, settings=None) -> list:
    """The scene objects Blender's USD exporter writes, in scene order.

    Blender leaves out objects in collections excluded from the view layer,
    and follows ``evaluation_mode``: ``RENDER`` skips objects and collections
    disabled in renders, ``VIEWPORT`` those disabled in viewports. Hiding with
    the outliner's eye does not leave anything out. The export scope does the
    same. Collection-instance prototypes hidden this way still
    reach the processing scope through their instancer.
    """
    scene_objects = list(context.scene.objects)
    if settings is None:
        settings = getattr(getattr(context, "scene", None), "usd_stage_export_settings", None)
    viewport = str(getattr(settings, "evaluation_mode", "RENDER")) == "VIEWPORT"
    hidden_flag = "hide_viewport" if viewport else "hide_render"
    view_layer = getattr(context, "view_layer", None)
    root = getattr(view_layer, "layer_collection", None)
    if root is None:
        layer_objects = getattr(view_layer, "objects", None)
        if layer_objects is None:
            return scene_objects
        in_layer = {_rna_identity(obj) for obj in layer_objects}
        return [obj for obj in scene_objects if _rna_identity(obj) in in_layer]

    visible: set[int] = set()
    stack = [root]
    while stack:
        layer_collection = stack.pop()
        collection = layer_collection.collection
        if bool(getattr(layer_collection, "exclude", False)):
            continue
        if bool(getattr(collection, hidden_flag, False)):
            continue
        for obj in collection.objects:
            if not bool(getattr(obj, hidden_flag, False)):
                visible.add(_rna_identity(obj))
        stack.extend(layer_collection.children)
    return [obj for obj in scene_objects if _rna_identity(obj) in visible]


def collect_processing_objects(context, export_objects: list) -> list:
    """Expand native selection into non-selected processing dependencies.

    Parents and collection prototypes are intentionally *not* selected for the
    USD operator, but their transforms/materials/animation can still contribute
    to the exported asset.  This scope is used for animation preparation and
    Y-up safety checks only.
    """
    scene_objects = list(context.scene.objects)
    closure: list = []
    seen: set[int] = set()

    def add(obj) -> bool:
        if obj is None or _rna_identity(obj) in seen:
            return False
        seen.add(_rna_identity(obj))
        closure.append(obj)
        return True

    for obj in export_objects:
        add(obj)

    changed = True
    while changed:
        changed = False
        for obj in list(closure):
            parent = getattr(obj, "parent", None)
            while parent is not None:
                changed = add(parent) or changed
                parent = getattr(parent, "parent", None)

            collection = getattr(obj, "instance_collection", None)
            if collection is not None:
                prototypes = getattr(collection, "all_objects", None)
                if prototypes is None:
                    prototypes = getattr(collection, "objects", [])
                for prototype in prototypes or []:
                    changed = add(prototype) or changed

    closed_ids = {_rna_identity(obj) for obj in closure}
    ordered = [obj for obj in scene_objects if _rna_identity(obj) in closed_ids]
    ordered_ids = {_rna_identity(obj) for obj in ordered}
    ordered.extend(obj for obj in closure if _rna_identity(obj) not in ordered_ids)
    return ordered


def prepare_animation_export(context, settings, diagnostics=None) -> dict:
    """Prepare animation data for export by concatenating and baking actions.

    Returns a state dictionary that must be passed to restore_animation_export().
    """
    state = _init_state(context)
    scene = state["scene"]

    export_objects = collect_export_objects(context, settings)
    state["export_objects"] = export_objects
    if not export_objects:
        if bool(getattr(settings, "selected_objects_only", False)):
            raise RuntimeError(
                "Selected-only export requires at least one selected object "
                "whose object class is enabled for export."
            )
        raise RuntimeError(
            "Export requires at least one scene object whose object class is "
            "enabled for export."
        )
    if bool(getattr(settings, "selected_objects_only", False)):
        _set_export_selection(context, export_objects)

    # Selection closure is required even for a static export.  Returning the
    # state lets the caller restore the user's exact selection transactionally
    # after Blender's USD operator finishes.
    if not bool(getattr(settings, "export_animation", False)):
        return state

    processing_objects = collect_processing_objects(context, export_objects)
    state["processing_objects"] = processing_objects
    try:
        _link_processing_objects_for_bake(context, processing_objects, state)
        targets = _collect_targets(processing_objects, settings)
        actions = _collect_actions_for_targets(targets)
        schedule, total_frames = _build_schedule(actions, diagnostics, targets)
    except Exception:
        _unlink_temporary_processing_objects(state)
        raise
    if not targets and diagnostics:
        diagnostics.add_warning("Export animation enabled but no animated targets were found.")

    _warn_about_stashed_actions(actions, diagnostics)

    if not actions:
        if diagnostics and targets:
            # Only when animated targets exist: with none at all, the warning
            # three lines up already said so, and a static scene with
            # export_animation stored on got the same fact twice.
            diagnostics.add_warning(
                "Export animation enabled but no target-owned Action slots were found."
            )
        _unlink_temporary_processing_objects(state)
        _finalize_export_selection(context, settings, state, export_objects)
        return state

    if not schedule:
        if diagnostics:
            diagnostics.add_warning("Export animation schedule is empty; skipping bake.")
        _unlink_temporary_processing_objects(state)
        _finalize_export_selection(context, settings, state, export_objects)
        return state

    total_frames_int = max(1, int(math.ceil(total_frames)))
    if diagnostics:
        diagnostics.set_animation_schedule(
            fps=scene.render.fps,
            total_frames=total_frames_int,
            segments=[
                {
                    "name": seg["name"],
                    "start_frame": seg["start_frame"],
                    "end_frame": seg["end_frame"],
                    "end_frame_exclusive": seg["end_frame_exclusive"],
                }
                for seg in schedule
            ],
            targets=[
                {
                    "name": t.get("name"),
                    "kind": t.get("kind"),
                    "object_type": t.get("object_type"),
                    "actions": [binding[0].name for binding in t.get("bindings", [])],
                }
                for t in targets
            ],
        )

    scene.frame_start = 1
    scene.frame_end = total_frames_int
    try:
        scene.frame_set(scene.frame_start)
    except Exception:
        pass

    try:
        for target in targets:
            target_schedule = _schedule_for_target(schedule, target)
            if target_schedule:
                if diagnostics and len(target_schedule) > 1:
                    diagnostics.add_warning(
                        f"Target '{target.get('name', '<unknown>')}' has "
                        f"{len(target_schedule)} aggregate takes. Their final "
                        "and next-first poses are retained on adjacent integer "
                        "samples, but one baked USD animation cannot represent "
                        "a discontinuous hard cut without interpolation. Export "
                        "separate per-take assets when a lossless hard cut is "
                        "required."
                    )
                _prepare_target(
                    context,
                    target,
                    target_schedule,
                    total_frames_int,
                    state,
                    diagnostics,
                )
    except Exception:
        restore_animation_export(state)
        raise

    _ensure_object_mode(context)
    _unlink_temporary_processing_objects(state)
    _finalize_export_selection(context, settings, state, export_objects)
    return state


def restore_animation_export(state: dict) -> None:
    """Restore Blender scene and animation state after export."""
    if not state:
        return

    scene = state.get("scene")
    if scene:
        try:
            scene.frame_start = int(state.get("frame_start", scene.frame_start))
            scene.frame_end = int(state.get("frame_end", scene.frame_end))
            scene.frame_set(int(state.get("frame_current", scene.frame_current)))
        except Exception:
            pass

    for item in state.get("targets", []):
        anim_data = item.get("anim_data")
        if anim_data is None:
            continue

        # Remove export track.
        export_name = item.get("export_track_name")
        if export_name:
            try:
                export_track = anim_data.nla_tracks.get(export_name)
                if export_track:
                    anim_data.nla_tracks.remove(export_track)
            except Exception:
                pass

        # Restore track mute/solo flags.
        for track, mute, solo in item.get("track_states", []):
            try:
                track_name = getattr(track, "name", None)
                if track_name and anim_data.nla_tracks.get(track_name) is not None:
                    if hasattr(track, "mute"):
                        track.mute = mute
                    if hasattr(track, "is_solo"):
                        track.is_solo = solo
            except Exception:
                continue

        # Restore action, its exact Blender 5.2 slot, and NLA evaluation mode.
        try:
            _restore_anim_assignment(anim_data, item)
        except Exception:
            pass

        # Remove baked action if it was created for export.
        baked_action = item.get("baked_action")
        if baked_action and bpy.data.actions.get(baked_action.name) is not None:
            try:
                bpy.data.actions.remove(baked_action)
            except Exception:
                pass

        # Clear animation data if we created it.
        if item.get("created_anim_data"):
            owner = item.get("owner")
            try:
                if hasattr(owner, "animation_data_clear"):
                    owner.animation_data_clear()
            except Exception:
                pass

    _unlink_temporary_processing_objects(state)
    _restore_selection_from_state(state)
    _restore_mode_from_state(state)


def _collect_actions_for_targets(targets: list[dict]) -> list:
    """Return only Actions with an explicit target-owned slot binding."""
    by_identity: dict[int, Any] = {}
    for target in targets:
        for action, _slot in target.get("bindings", []):
            by_identity.setdefault(_rna_identity(action), action)
    return sorted(by_identity.values(), key=lambda action: action.name.lower())


def _schedule_for_target(schedule: list[dict], target: dict) -> list[dict]:
    slots_by_action = {
        _rna_identity(action): slot for action, slot in target.get("bindings", [])
    }
    out: list[dict] = []
    for segment in schedule:
        action = segment["action"]
        action_id = _rna_identity(action)
        if action_id not in slots_by_action:
            continue
        target_segment = dict(segment)
        target_segment["slot"] = slots_by_action[action_id]
        out.append(target_segment)
    return out


def _build_schedule(actions: list, diagnostics=None, targets=None) -> tuple[list, int]:
    schedule = []
    current = 1
    for action in actions:
        start, end = _action_frame_range(action, targets or [])
        source_start = float(start)
        source_end = float(end)
        source_length = source_end - source_start
        if source_length <= 0.0:
            source_length = 1.0
            if diagnostics:
                diagnostics.add_warning(
                    f"Action '{action.name}' has zero-length range; clamped to 1 frame."
                )
        bake_frame_count = max(1, int(math.ceil(source_length)))

        # Blender's NLA bake operator samples integer frames only. Sharing one
        # aggregate timecode between the prior take's final pose and the next
        # take's first pose necessarily drops one of them when they differ.
        # Give every take an integer output span with a distinct inclusive final
        # sample; the next take starts on the following timecode. NLA scales the
        # exact source_start..source_end range across that quantized span.
        final_sample_frame = current + bake_frame_count
        end_frame_exclusive = final_sample_frame + 1
        if diagnostics and (
            not _is_integral_frame(source_start)
            or not _is_integral_frame(source_end)
            or not _is_integral_frame(source_length)
            or not math.isclose(
                source_length,
                float(bake_frame_count),
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            diagnostics.add_warning(
                f"Action '{action.name}' fractional source range "
                f"[{_format_frame(source_start)}, {_format_frame(source_end)}] "
                f"(duration {_format_frame(source_length)} frames) was mapped "
                f"to integer exported sample range [{current}, "
                f"{final_sample_frame}] ({bake_frame_count} frame intervals; "
                f"next take starts at {end_frame_exclusive}); the exported take "
                "is time-scaled to retain explicit first and final pose samples."
            )
        segment = {
            "name": action.name,
            "action": action,
            "action_start": source_start,
            "action_end": source_end,
            "start_frame": current,
            "end_frame": final_sample_frame,
            "end_frame_exclusive": end_frame_exclusive,
            "length": source_length,
            "length_frames": bake_frame_count,
        }
        schedule.append(segment)
        current = end_frame_exclusive

    total_frames = schedule[-1]["end_frame"] if schedule else 0
    return schedule, total_frames


def _is_integral_frame(value: float) -> bool:
    return math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-7)


def _format_frame(value: float) -> str:
    value = float(value)
    if _is_integral_frame(value):
        return str(int(round(value)))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _collect_targets(objects: list, settings) -> list[dict]:
    targets: list[dict] = []
    seen_owners: set[tuple[str, int]] = set()
    for obj in objects:
        if not _is_exportable_object(obj, settings):
            continue

        if obj.type == "ARMATURE":
            target = {
                "kind": "ARMATURE",
                "name": obj.name,
                "object": obj,
                "owner": obj,
                "object_type": obj.type,
            }
            target["bindings"] = _action_bindings_for_owner(obj)
            owner_key = ("ARMATURE", _rna_identity(obj))
            if target["bindings"] and owner_key not in seen_owners:
                seen_owners.add(owner_key)
                targets.append(target)
        else:
            bindings = _action_bindings_for_owner(obj)
            owner_key = ("OBJECT", _rna_identity(obj))
            if bindings and owner_key not in seen_owners:
                seen_owners.add(owner_key)
                targets.append({
                    "kind": "OBJECT",
                    "name": obj.name,
                    "object": obj,
                    "owner": obj,
                    "object_type": obj.type,
                    "bindings": bindings,
                })

        if _has_shapekeys(obj, settings):
            owner = _get_shape_key_block(obj)
            bindings = _action_bindings_for_owner(owner)
            owner_key = ("SHAPEKEYS", _rna_identity(owner))
            if bindings and owner_key not in seen_owners:
                seen_owners.add(owner_key)
                targets.append({
                    "kind": "SHAPEKEYS",
                    "name": obj.name,
                    "object": obj,
                    "owner": owner,
                    "object_type": obj.type,
                    "bindings": bindings,
                })

    return targets


def _warn_about_stashed_actions(exported_actions, diagnostics) -> None:
    """Warn that stashed Actions exist and are not being exported.

    _action_bindings_for_owner documents its ActionSlot scan as covering
    "logical takes that are not the active Action and are not currently staged
    as NLA strips". In Blender 5.2 ``ActionSlot.users()`` returns only *live*
    users, so it is empty for exactly that case and the branch never fires for
    the scenario its comment describes.

    A stashed take - fake user set, not assigned, not staged - therefore
    disappeared from the schedule and the clip list with no warning, and an
    animator got a silently short export.

    This cannot prove which object a stashed Action belongs to (that is the
    information ``users()`` withholds), so it reports the count and names them
    rather than guessing at ownership or trying to export them.
    """
    if diagnostics is None:
        return
    try:
        all_actions = list(getattr(getattr(bpy, "data", None), "actions", []) or [])
    except Exception:
        return

    exported = {_rna_identity(action) for action in (exported_actions or [])}
    stashed = []
    for action in all_actions:
        if _rna_identity(action) in exported:
            continue
        if not getattr(action, "use_fake_user", False):
            continue
        try:
            live = any(list(slot.users()) for slot in (getattr(action, "slots", []) or []))
        except Exception:
            live = False
        if not live:
            stashed.append(str(getattr(action, "name", "?")))

    if stashed:
        diagnostics.add_warning(
            f"{len(stashed)} stashed Action(s) were not exported: "
            + ", ".join(sorted(stashed))
            + ". Only the active Action and Actions staged as NLA strips are "
            "exported. Push a stashed take to an NLA strip to include it."
        )


def _action_bindings_for_owner(owner) -> list[tuple[Any, Any]]:
    """Find ``(Action, ActionSlot)`` pairs that explicitly belong to *owner*.

    Only actual active-Action and NLA-strip associations are export intent.
    Merely finding a same-type or similarly named slot in ``bpy.data.actions``
    is deliberately insufficient; doing so is the global-broadcast bug this
    function prevents.
    """
    if owner is None:
        return []

    anim_data = getattr(owner, "animation_data", None)
    if anim_data is None:
        return []
    if bool(getattr(anim_data, "use_tweak_mode", False)):
        raise RuntimeError(
            f"Cannot export animation for '{owner.name}' while NLA tweak mode is active."
        )

    associations: list[tuple[Any, Any]] = []
    active_action = getattr(anim_data, "action", None)
    if active_action is not None:
        associations.append((active_action, getattr(anim_data, "action_slot", None)))
    for track in getattr(anim_data, "nla_tracks", []) or []:
        for strip in getattr(track, "strips", []) or []:
            action = getattr(strip, "action", None)
            if action is not None:
                associations.append((action, getattr(strip, "action_slot", None)))

    # Slots remain the ownership authority for logical takes that are not the
    # active Action and are not currently staged as NLA strips. ActionSlot.users
    # maps them to the datablock without falling back to unsafe name/type guesses.
    for action in getattr(getattr(bpy, "data", None), "actions", []) or []:
        for slot in getattr(action, "slots", []) or []:
            try:
                users = list(slot.users())
            except Exception:
                continue
            if any(_rna_identity(user) == _rna_identity(owner) for user in users):
                associations.append((action, slot))

    bindings_by_action: dict[int, tuple[Any, Any]] = {}
    for action, slot in associations:
        _validate_action_binding(owner, action, slot)
        action_identity = _rna_identity(action)
        previous = bindings_by_action.get(action_identity)
        if (
            previous is not None
            and _rna_identity(previous[1]) != _rna_identity(slot)
        ):
            raise RuntimeError(
                f"Action '{action.name}' is associated with multiple slots on "
                f"'{owner.name}'; make the take ownership unambiguous before export."
            )
        bindings_by_action[action_identity] = (action, slot)

    return sorted(
        bindings_by_action.values(),
        key=lambda pair: pair[0].name.lower(),
    )


def _iter_slot_fcurves(action, slot):
    if action is None or slot is None:
        return
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            if str(getattr(strip, "type", "")) != "KEYFRAME":
                continue
            channelbag = strip.channelbag(slot)
            if channelbag is not None:
                yield from channelbag.fcurves


def _validate_action_binding(owner, action, slot) -> None:
    owner_name = str(getattr(owner, "name", "<unknown>"))
    action_name = str(getattr(action, "name", "<unknown>"))
    if slot is None:
        raise RuntimeError(
            f"Action '{action_name}' assigned to '{owner_name}' has no Action slot."
        )
    if not any(
        _rna_identity(candidate) == _rna_identity(slot)
        for candidate in getattr(action, "slots", []) or []
    ):
        raise RuntimeError(
            f"Action slot for '{action_name}' does not belong to that Action."
        )
    owner_id_type = str(getattr(owner, "id_type", ""))
    slot_id_type = str(getattr(slot, "target_id_type", ""))
    if slot_id_type not in {owner_id_type, "UNSPECIFIED"}:
        raise RuntimeError(
            f"Action '{action_name}' slot targets {slot_id_type}, not "
            f"{owner_id_type} for '{owner_name}'."
        )

    fcurves = list(_iter_slot_fcurves(action, slot))
    if not fcurves:
        raise RuntimeError(
            f"Action '{action_name}' slot for '{owner_name}' has no F-Curves."
        )
    for fcurve in fcurves:
        data_path = str(getattr(fcurve, "data_path", ""))
        try:
            owner.path_resolve(data_path)
        except Exception as exc:
            raise RuntimeError(
                f"Action '{action_name}' is incompatible with '{owner_name}': "
                f"cannot resolve '{data_path}'."
            ) from exc


def _action_frame_range(action, targets: list[dict]) -> tuple[float, float]:
    if bool(getattr(action, "use_frame_range", False)):
        start, end = action.frame_range
        return float(start), float(end)

    ranges: list[tuple[float, float]] = []
    seen_slots: set[int] = set()
    for target in targets:
        for candidate, slot in target.get("bindings", []):
            slot_identity = _rna_identity(slot)
            if (
                _rna_identity(candidate) != _rna_identity(action)
                or slot_identity in seen_slots
            ):
                continue
            seen_slots.add(slot_identity)
            for fcurve in _iter_slot_fcurves(action, slot):
                start, end = fcurve.range()
                ranges.append((float(start), float(end)))

    if not ranges:
        start, end = action.frame_range
        return float(start), float(end)
    return min(start for start, _end in ranges), max(end for _start, end in ranges)


def _slot_matches_owner(slot, owner) -> bool:
    owner_id_type = str(getattr(owner, "id_type", "") or "")
    slot_id_type = str(getattr(slot, "target_id_type", "") or "")
    if owner_id_type and slot_id_type and owner_id_type != slot_id_type:
        return False

    try:
        if any(
            _rna_identity(user) == _rna_identity(owner)
            for user in slot.users()
        ):
            return True
    except Exception:
        pass

    owner_name = str(getattr(owner, "name", "") or "")
    if not owner_name:
        return False
    display_name = str(getattr(slot, "name_display", "") or "")
    if display_name == owner_name:
        return True

    identifier = str(getattr(slot, "identifier", "") or "")
    # Blender prefixes Action slot identifiers by ID type (OB, KE, ...).
    return len(identifier) > 2 and identifier[2:] == owner_name


#: Object types the RealityKit delivery contract never exports. The native
#: exporter's flags for them are hard-disabled too, so selection filtering must
#: never admit them.
_NEVER_EXPORTED_TYPES = frozenset({"LIGHT", "CAMERA", "CURVE", "CURVES", "POINTCLOUD", "VOLUME"})


def _is_exportable_object(obj, settings) -> bool:
    """Whether an object belongs in the export scope.

    Meshes always export: the USD exporter's mesh flag is hard-coded on
    (``_build_export_kwargs``), so no setting can drop one. Armatures follow
    ``export_armatures``. Empties are real USD Xforms and collection-instance
    roots; any other Blender object class is out of scope, so selected-only
    validation cannot succeed on a selection that would publish only the
    synthetic export root.
    """
    object_type = str(getattr(obj, "type", ""))
    if object_type == "MESH":
        return True
    if object_type == "ARMATURE":
        return bool(getattr(settings, "export_armatures", True))
    if object_type in _NEVER_EXPORTED_TYPES:
        return False
    return object_type == "EMPTY"


def _has_shapekeys(obj, settings) -> bool:
    if not bool(getattr(settings, "export_shapekeys", True)):
        return False
    if obj.type != "MESH":
        return False
    data = getattr(obj, "data", None)
    if not data:
        return False
    return bool(getattr(data, "shape_keys", None))


def _prepare_target(context, target: dict, schedule: list, total_frames: int, state: dict, diagnostics=None) -> None:
    kind = target.get("kind")
    if kind == "ARMATURE":
        _prepare_armature(context, target, schedule, total_frames, state, diagnostics)
    elif kind == "OBJECT":
        _prepare_object(context, target, schedule, total_frames, state, diagnostics)
    elif kind == "SHAPEKEYS":
        _prepare_shapekeys(context, target, schedule, total_frames, state, diagnostics)


def _prepare_armature(context, target: dict, schedule: list, total_frames: int, state: dict, diagnostics=None) -> None:
    obj = target["object"]
    anim_data, created = _ensure_anim_data(obj)
    if anim_data is None:
        raise RuntimeError(f"Failed to create animation data for armature '{obj.name}'.")

    target_state = _snapshot_anim_data(anim_data, obj)
    export_track_name = _unique_nla_track_name(
        anim_data,
        "__USDStage_Export__",
    )
    target_state["export_track_name"] = export_track_name
    target_state["created_anim_data"] = created
    # Register before the first NLA mutation. The outer transaction can now
    # restore this target even if track/strip creation fails midway.
    state["targets"].append(target_state)
    _apply_schedule(anim_data, schedule, track_name=export_track_name)

    _solo_export_track(anim_data, export_track_name)

    try:
        baked_action = _bake_armature(context, obj, anim_data, total_frames)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_error(f"Failed to bake armature '{obj.name}': {exc}")
        raise
    target_state["baked_action"] = baked_action

    _mute_all_tracks(anim_data)
    anim_data.use_nla = False
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, obj),
    )


def _prepare_object(context, target: dict, schedule: list, total_frames: int, state: dict, diagnostics=None) -> None:
    obj = target["object"]
    anim_data, created = _ensure_anim_data(obj)
    if anim_data is None:
        raise RuntimeError(f"Failed to create animation data for object '{obj.name}'.")

    target_state = _snapshot_anim_data(anim_data, obj)
    export_track_name = _unique_nla_track_name(
        anim_data,
        "__USDStage_Export__",
    )
    target_state["export_track_name"] = export_track_name
    target_state["created_anim_data"] = created
    state["targets"].append(target_state)
    _apply_schedule(anim_data, schedule, track_name=export_track_name)

    _solo_export_track(anim_data, export_track_name)

    try:
        baked_action = _bake_object(context, obj, anim_data, total_frames)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_error(f"Failed to bake object '{obj.name}': {exc}")
        raise
    target_state["baked_action"] = baked_action

    _mute_all_tracks(anim_data)
    anim_data.use_nla = False
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, obj),
    )


def _prepare_shapekeys(context, target: dict, schedule: list, total_frames: int, state: dict, diagnostics=None) -> None:
    obj = target["object"]
    key = _get_shape_key_block(obj)
    if key is None:
        return

    anim_data, created = _ensure_anim_data(key)
    if anim_data is None:
        raise RuntimeError(f"Failed to create animation data for shape keys on '{obj.name}'.")

    target_state = _snapshot_anim_data(anim_data, key)
    export_track_name = _unique_nla_track_name(
        anim_data,
        "__USDStage_Export__",
    )
    target_state["export_track_name"] = export_track_name
    target_state["created_anim_data"] = created
    state["targets"].append(target_state)
    _apply_schedule(anim_data, schedule, track_name=export_track_name)

    _solo_export_track(anim_data, export_track_name)

    try:
        baked_action = _bake_shapekeys(context.scene, obj, key, anim_data, total_frames)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_error(f"Failed to bake shape keys for '{obj.name}': {exc}")
        raise
    target_state["baked_action"] = baked_action

    _mute_all_tracks(anim_data)
    anim_data.use_nla = False
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, key),
    )


def _ensure_anim_data(owner) -> tuple[Any, bool]:
    anim_data = getattr(owner, "animation_data", None)
    if anim_data is not None:
        return anim_data, False
    try:
        owner.animation_data_create()
    except Exception:
        return None, False
    return getattr(owner, "animation_data", None), True


def _snapshot_anim_data(anim_data, owner) -> dict:
    track_states = []
    for track in anim_data.nla_tracks:
        track_states.append((track, getattr(track, "mute", False), getattr(track, "is_solo", False)))
    return {
        "owner": owner,
        **_snapshot_anim_assignment(anim_data),
        "track_states": track_states,
        "export_track_name": None,
        "baked_action": None,
        "created_anim_data": False,
    }


def _snapshot_anim_assignment(anim_data) -> dict:
    return {
        "anim_data": anim_data,
        "original_action": getattr(anim_data, "action", None),
        "original_action_slot": getattr(anim_data, "action_slot", None),
        "original_last_slot_identifier": getattr(
            anim_data,
            "last_slot_identifier",
            "",
        ),
        "original_use_nla": getattr(anim_data, "use_nla", False),
        "action_blend_type": getattr(anim_data, "action_blend_type", None),
        "action_extrapolation": getattr(anim_data, "action_extrapolation", None),
        "action_influence": getattr(anim_data, "action_influence", None),
    }


def _restore_anim_assignment(anim_data, snapshot: dict) -> None:
    original_action = snapshot.get("original_action")
    original_slot = snapshot.get("original_action_slot")
    original_last = snapshot.get("original_last_slot_identifier", "")
    _assign_action_and_slot(anim_data, None, None)
    if hasattr(anim_data, "last_slot_identifier"):
        anim_data.last_slot_identifier = original_last
    if original_action is not None:
        _assign_action_and_slot(anim_data, original_action, original_slot)
    if hasattr(anim_data, "last_slot_identifier"):
        anim_data.last_slot_identifier = original_last
    anim_data.use_nla = bool(snapshot.get("original_use_nla"))
    for attr in (
        "action_blend_type",
        "action_extrapolation",
        "action_influence",
    ):
        if (
            attr in snapshot
            and snapshot[attr] is not None
            and hasattr(anim_data, attr)
        ):
            setattr(anim_data, attr, snapshot[attr])


def _apply_schedule(
    anim_data,
    schedule: list,
    *,
    track_name: str | None = None,
) -> str:
    assignment_snapshot = _snapshot_anim_assignment(anim_data)
    track_name = track_name or _unique_nla_track_name(
        anim_data,
        "__USDStage_Export__",
    )
    export_track = None
    try:
        export_track = anim_data.nla_tracks.new()
        export_track.name = track_name

        for seg in schedule:
            logical_start = float(seg["start_frame"])
            final_sample_frame = float(seg["end_frame"])
            strip = export_track.strips.new(
                seg["name"],
                int(logical_start),
                seg["action"],
            )
            slot = seg.get("slot")
            if slot is not None and hasattr(strip, "action_slot"):
                strip.action_slot = slot
            action_start = float(seg["action_start"])
            action_end = float(seg["action_end"])
            if action_end <= action_start:
                action_end = action_start + float(seg.get("length", 1.0))

            # Assignment order matters in Blender 5.2: changing the source
            # Action range can rewrite frame_end, so set it before the final
            # integer output range. The differing ranges intentionally scale a
            # fractional source duration onto integer bake samples.
            strip.action_frame_start = action_start
            strip.action_frame_end = action_end
            strip.frame_start = logical_start

            # The final pose and the next take's first pose occupy distinct
            # integer samples. This avoids the lossy shared-boundary choice NLA
            # would otherwise make for discontinuous takes.
            strip.frame_end = final_sample_frame

        anim_data.use_nla = True
        _assign_action_and_slot(anim_data, None, None)
        return track_name
    except Exception as schedule_error:
        cleanup_errors: list[str] = []
        if export_track is not None:
            try:
                anim_data.nla_tracks.remove(export_track)
            except Exception as exc:
                cleanup_errors.append(f"remove partial NLA track: {exc}")
        try:
            _restore_anim_assignment(anim_data, assignment_snapshot)
        except Exception as exc:
            cleanup_errors.append(f"restore Action assignment: {exc}")
        if cleanup_errors:
            raise RuntimeError(
                "Animation schedule failed and transactional cleanup was "
                f"incomplete ({'; '.join(cleanup_errors)})."
            ) from schedule_error
        raise


def _assign_action_and_slot(anim_data, action, slot) -> None:
    """Assign an Action and its exact slot without relying on name guessing."""
    try:
        anim_data.action = None
    except Exception:
        return
    if action is None:
        return
    try:
        anim_data.action = action
        if hasattr(anim_data, "action_slot"):
            # Explicitly assign None as well: Blender may otherwise auto-pick a
            # same-name/last-used slot and change a slotless original state.
            anim_data.action_slot = slot
    except Exception:
        # Do not silently continue with an Action whose slot could not be
        # assigned; that evaluates as an empty animation in Blender 5.2.
        anim_data.action = None
        raise


def _slot_for_owner(action, owner):
    for slot in getattr(action, "slots", []) or []:
        if _slot_matches_owner(slot, owner):
            return slot
    return None


def _solo_export_track(anim_data, export_track_name: str) -> None:
    for track in anim_data.nla_tracks:
        if hasattr(track, "mute"):
            track.mute = (track.name != export_track_name)
        if hasattr(track, "is_solo"):
            track.is_solo = (track.name == export_track_name)


def _mute_all_tracks(anim_data) -> None:
    for track in anim_data.nla_tracks:
        if hasattr(track, "mute"):
            track.mute = True
        if hasattr(track, "is_solo"):
            track.is_solo = False


def _bake_armature(context, obj, anim_data, total_frames: int):
    _select_only(context, obj)
    _set_active(context, obj)
    _ensure_mode(context, "POSE")
    try:
        bpy.ops.pose.select_all(action="SELECT")
    except Exception:
        pass

    baked_action = _new_action(f"__USDSTAGE_BAKED_ARMATURE_{obj.name}", obj)
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, obj),
    )

    try:
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=int(total_frames),
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            clear_parents=False,
            use_current_action=True,
            bake_types={"POSE", "OBJECT"},
        )
    except TypeError:
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=int(total_frames),
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            clear_parents=False,
            bake_types={"POSE", "OBJECT"},
        )

    _ensure_mode(context, "OBJECT")
    return anim_data.action or baked_action


def _bake_object(context, obj, anim_data, total_frames: int):
    _select_only(context, obj)
    _set_active(context, obj)
    _ensure_mode(context, "OBJECT")

    baked_action = _new_action(f"__USDSTAGE_BAKED_OBJECT_{obj.name}", obj)
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, obj),
    )

    try:
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=int(total_frames),
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            clear_parents=False,
            use_current_action=True,
            bake_types={"OBJECT"},
        )
    except TypeError:
        bpy.ops.nla.bake(
            frame_start=1,
            frame_end=int(total_frames),
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            clear_parents=False,
            bake_types={"OBJECT"},
        )

    return anim_data.action or baked_action


def _bake_shapekeys(scene, obj, key, anim_data, total_frames: int):
    baked_action = _new_action(f"__USDSTAGE_BAKED_SHAPEKEYS_{obj.name}", key)
    _assign_action_and_slot(
        anim_data,
        baked_action,
        _slot_for_owner(baked_action, key),
    )

    key_blocks = [kb for kb in key.key_blocks if kb.name != "Basis"]
    if not key_blocks:
        return baked_action

    fcurves = {}
    for kb in key_blocks:
        # Escape the name: a shape key called e.g. Eye "Blink" would otherwise
        # produce key_blocks["Eye "Blink""].value. fcurves.new() does not
        # validate data_path, so the curve is created, keyframed, and resolves
        # to nothing - the key exports as a static value with no warning.
        data_path = f'key_blocks["{bpy.utils.escape_identifier(kb.name)}"].value'
        fcurves[kb.name] = _ensure_action_fcurve(
            baked_action,
            key,
            data_path,
            index=0,
            group_name="Shape Keys",
        )

    for frame in range(1, int(total_frames) + 1):
        scene.frame_set(frame)
        for kb in key_blocks:
            fcurve = fcurves[kb.name]
            fcurve.keyframe_points.insert(frame, kb.value, options={"FAST"})

    return baked_action


def _ensure_action_fcurve(action, datablock, data_path: str, index: int = 0, group_name: str = ""):
    """Create an F-Curve across Blender's legacy and layered Action APIs."""
    if hasattr(action, "fcurves"):
        return action.fcurves.new(data_path=data_path, index=index)
    if hasattr(action, "fcurve_ensure_for_datablock"):
        return action.fcurve_ensure_for_datablock(
            datablock,
            data_path,
            index=index,
            group_name=group_name,
        )
    raise RuntimeError("Blender Action API does not expose an F-Curve creation method.")


def _get_shape_key_block(obj):
    data = getattr(obj, "data", None)
    if not data:
        return None
    return getattr(data, "shape_keys", None)


def _new_action(base_name: str, owner=None):
    name = _unique_action_name(base_name)
    action = bpy.data.actions.new(name)
    if owner is not None:
        slots = getattr(action, "slots", None)
        if slots is not None:
            try:
                slots.new(
                    id_type=str(getattr(owner, "id_type", "")),
                    name=str(getattr(owner, "name", base_name)),
                )
            except Exception:
                # fcurve_ensure_for_datablock / nla.bake can materialize a slot
                # after the Action has been assigned, so creation failure is
                # non-fatal here.
                pass
    return action


def _unique_action_name(base: str) -> str:
    name = base
    suffix = 1
    while bpy.data.actions.get(name) is not None:
        suffix += 1
        name = f"{base}_{suffix}"
    return name


def _unique_nla_track_name(anim_data, base: str) -> str:
    name = base
    suffix = 1
    while anim_data.nla_tracks.get(name) is not None:
        suffix += 1
        name = f"{base}_{suffix}"
    return name


def _init_state(context) -> dict:
    active = context.view_layer.objects.active
    mode = None
    try:
        mode = active.mode if active else None
    except Exception:
        mode = None
    return {
        "scene": context.scene,
        "frame_start": context.scene.frame_start,
        "frame_end": context.scene.frame_end,
        "frame_current": context.scene.frame_current,
        "selection": [obj.name for obj in context.selected_objects],
        "active": active.name if active else None,
        "mode": mode,
        "targets": [],
        "temporary_scene_links": [],
    }


def _link_processing_objects_for_bake(context, objects: list, state: dict) -> None:
    """Temporarily make animated collection prototypes operator-evaluable."""
    scene_ids = {_rna_identity(obj) for obj in context.scene.objects}
    linked: list = state.setdefault("temporary_scene_links", [])
    for obj in objects:
        if _rna_identity(obj) in scene_ids:
            continue
        try:
            context.scene.collection.objects.link(obj)
        except Exception as exc:
            raise RuntimeError(
                f"Animated instance dependency '{obj.name}' cannot be made "
                "evaluable in the export scene."
            ) from exc
        linked.append(obj)
        scene_ids.add(_rna_identity(obj))
    try:
        context.view_layer.update()
    except Exception:
        pass


def _unlink_temporary_processing_objects(state: dict) -> None:
    scene = state.get("scene")
    if scene is None:
        return
    linked = list(state.get("temporary_scene_links", []) or [])
    for obj in reversed(linked):
        try:
            scene.collection.objects.unlink(obj)
        except Exception:
            pass
    state["temporary_scene_links"] = []


def _restore_selection(context, state: dict) -> None:
    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        pass
    names = set(state.get("selection", []))
    for obj in context.scene.objects:
        if obj.name in names:
            try:
                obj.select_set(True)
            except Exception:
                pass
    active_name = state.get("active")
    if active_name:
        obj = context.scene.objects.get(active_name)
        if obj:
            try:
                context.view_layer.objects.active = obj
            except Exception:
                pass


def _set_export_selection(context, objects: list) -> None:
    """Select exactly *objects*, failing if a required dependency is hidden."""
    try:
        for obj in context.view_layer.objects:
            obj.select_set(False)
    except Exception:
        pass

    failures: list[str] = []
    for obj in objects:
        try:
            obj.select_set(True)
            select_get = getattr(obj, "select_get", None)
            if callable(select_get) and not select_get():
                failures.append(str(getattr(obj, "name", "<unknown>")))
        except Exception:
            failures.append(str(getattr(obj, "name", "<unknown>")))

    if failures:
        names = ", ".join(repr(name) for name in failures[:5])
        if len(failures) > 5:
            names += f", and {len(failures) - 5} more"
        raise RuntimeError(
            "Selected-only export dependencies are unavailable in the active "
            f"view layer: {names}."
        )

    if objects:
        try:
            context.view_layer.objects.active = objects[0]
        except Exception:
            pass


def _finalize_export_selection(context, settings, state: dict, objects: list) -> None:
    if bool(getattr(settings, "selected_objects_only", False)):
        _set_export_selection(context, objects)
    else:
        _restore_selection(context, state)


def _restore_selection_from_state(state: dict) -> None:
    scene = state.get("scene")
    if scene is None:
        return
    try:
        view_layer = bpy.context.view_layer
    except Exception:
        view_layer = None

    try:
        if view_layer is not None:
            for obj in view_layer.objects:
                obj.select_set(False)
        else:
            for obj in scene.objects:
                obj.select_set(False)
    except Exception:
        pass
    names = set(state.get("selection", []))
    for obj in scene.objects:
        if obj.name in names:
            try:
                obj.select_set(True)
            except Exception:
                pass
    active_name = state.get("active")
    if active_name and scene.objects.get(active_name):
        try:
            bpy.context.view_layer.objects.active = scene.objects.get(active_name)
        except Exception:
            pass


def _restore_mode_from_state(state: dict) -> None:
    mode = state.get("mode")
    if not mode:
        return
    try:
        bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass


def _ensure_mode(context, mode: str) -> None:
    try:
        bpy.ops.object.mode_set(mode=mode)
    except Exception:
        pass


def _ensure_object_mode(context) -> None:
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass


def _select_only(context, obj) -> None:
    try:
        for o in context.view_layer.objects:
            o.select_set(False)
    except Exception:
        pass
    try:
        obj.select_set(True)
    except Exception:
        pass


def _set_active(context, obj) -> None:
    context.view_layer.objects.active = obj
