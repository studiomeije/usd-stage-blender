"""Focused Blender 5.2 Action-slot and selected-scope regressions."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from Plugin.export import animation_export  # noqa: E402


class _Object:
    def __init__(self, name, object_type="MESH", **values):
        self.name = name
        self.type = object_type
        self.id_type = "OBJECT"
        self.parent = values.pop("parent", None)
        self.parent_type = values.pop("parent_type", "OBJECT")
        self.modifiers = values.pop("modifiers", [])
        self.instance_collection = values.pop("instance_collection", None)
        self.animation_data = values.pop("animation_data", None)
        self.data = values.pop("data", SimpleNamespace(shape_keys=None))
        self._selected = values.pop("selected", False)
        for key, value in values.items():
            setattr(self, key, value)

    def select_set(self, selected):
        self._selected = bool(selected)

    def select_get(self):
        return self._selected

    def path_resolve(self, data_path):
        if data_path not in {"location", "rotation_euler", "scale"}:
            raise ValueError(data_path)
        return object()


def _scope_context(scene_objects, selected):
    # ``SimpleNamespace`` cannot provide collection iteration through an
    # instance-level ``__iter__`` special method, so use a tiny list subclass.
    class _ViewObjects(list):
        active = None

    return SimpleNamespace(
        scene=SimpleNamespace(
            objects=list(scene_objects),
            frame_start=1,
            frame_end=250,
            frame_current=1,
        ),
        selected_objects=list(selected),
        view_layer=SimpleNamespace(objects=_ViewObjects(scene_objects)),
    )


def _settings(**values):
    defaults = dict(
        selected_objects_only=True,
        export_armatures=True,
        evaluation_mode="RENDER",
        export_meshes=True,
        export_shapekeys=True,
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_full_scene_scope_leaves_out_objects_outside_the_view_layer():
    """An object in a collection excluded from the view layer is never exported."""
    room = _Object("Room")
    hidden_asset = _Object("HiddenAsset")
    prototype = _Object("Prototype")
    instancer = _Object("Instancer", "EMPTY", instance_collection=SimpleNamespace(all_objects=[prototype]))
    context = _scope_context([room, hidden_asset, prototype, instancer], [])
    context.view_layer.objects[:] = [room, instancer]

    export_objects = animation_export.collect_export_objects(
        context, _settings(selected_objects_only=False)
    )

    assert export_objects == [room, instancer]
    assert animation_export.collect_processing_objects(context, export_objects) == [
        room,
        prototype,
        instancer,
    ]


def _collection(name, objects, *, children=(), exclude=False, **flags):
    collection = SimpleNamespace(name=name, objects=list(objects), hide_render=False, hide_viewport=False)
    for key, value in flags.items():
        setattr(collection, key, value)
    return SimpleNamespace(collection=collection, children=list(children), exclude=exclude)


@pytest.mark.parametrize(
    ("evaluation_mode", "expected"),
    [
        ("RENDER", ["Visible", "HideViewport", "EyeHidden", "InHideViewportColl"]),
        ("VIEWPORT", ["Visible", "HideRender", "EyeHidden", "InHideRenderColl"]),
    ],
)
def test_full_scene_scope_matches_blender_usd_export_visibility(evaluation_mode, expected):
    """Measured against wm.usd_export in Blender 5.2 for both evaluation modes."""
    visible = _Object("Visible", hide_render=False, hide_viewport=False)
    hide_render = _Object("HideRender", hide_render=True, hide_viewport=False)
    hide_viewport = _Object("HideViewport", hide_render=False, hide_viewport=True)
    eye_hidden = _Object("EyeHidden", hide_render=False, hide_viewport=False)
    in_excluded = _Object("InExcluded", hide_render=False, hide_viewport=False)
    in_hide_render = _Object("InHideRenderColl", hide_render=False, hide_viewport=False)
    in_hide_viewport = _Object("InHideViewportColl", hide_render=False, hide_viewport=False)
    objects = [visible, hide_render, hide_viewport, eye_hidden, in_excluded, in_hide_render, in_hide_viewport]
    root = _collection(
        "Scene Collection",
        [visible, hide_render, hide_viewport, eye_hidden],
        children=[
            _collection("Excluded", [in_excluded], exclude=True),
            _collection("CollHideRender", [in_hide_render], hide_render=True),
            _collection("CollHideViewport", [in_hide_viewport], hide_viewport=True),
        ],
    )
    context = _scope_context(objects, [])
    context.view_layer.layer_collection = root

    result = animation_export.collect_export_objects(
        context, _settings(selected_objects_only=False, evaluation_mode=evaluation_mode)
    )

    assert [obj.name for obj in result] == expected


def test_empty_selected_only_scope_stays_empty():
    scene = [_Object("Cube")]
    assert animation_export.collect_export_objects(
        _scope_context(scene, []), _settings()
    ) == []


def test_selected_skinned_mesh_adds_only_effective_armature():
    armature = _Object("Rig", "ARMATURE")
    sibling = _Object("Sibling")
    modifier = SimpleNamespace(
        type="ARMATURE",
        object=armature,
        show_render=True,
        show_viewport=True,
    )
    mesh = _Object("Body", modifiers=[modifier], selected=True)
    objects = [armature, mesh, sibling]

    result = animation_export.collect_export_objects(
        _scope_context(objects, [mesh]), _settings()
    )

    assert result == [armature, mesh]
    assert sibling not in result


def test_selected_skinned_mesh_adds_every_effective_armature():
    primary = _Object("PrimaryRig", "ARMATURE")
    secondary = _Object("SecondaryRig", "ARMATURE")
    modifiers = [
        SimpleNamespace(
            type="ARMATURE",
            object=primary,
            show_render=True,
            show_viewport=True,
        ),
        SimpleNamespace(
            type="ARMATURE",
            object=secondary,
            show_render=True,
            show_viewport=True,
        ),
        # A repeated target remains deterministic and de-duplicated.
        SimpleNamespace(
            type="ARMATURE",
            object=primary,
            show_render=True,
            show_viewport=True,
        ),
    ]
    mesh = _Object("LayeredSkin", modifiers=modifiers, selected=True)
    context = _scope_context([primary, secondary, mesh], [mesh])

    assert animation_export.collect_export_objects(context, _settings()) == [
        primary,
        secondary,
        mesh,
    ]


def test_armature_modifier_enablement_uses_evaluation_mode():
    armature = _Object("Rig", "ARMATURE")
    modifier = SimpleNamespace(
        type="ARMATURE",
        object=armature,
        show_render=False,
        show_viewport=True,
    )
    mesh = _Object("Body", modifiers=[modifier], selected=True)
    context = _scope_context([armature, mesh], [mesh])

    assert animation_export.collect_export_objects(
        context, _settings(evaluation_mode="RENDER")
    ) == [mesh]
    assert animation_export.collect_export_objects(
        context, _settings(evaluation_mode="VIEWPORT")
    ) == [armature, mesh]


def test_selected_disabled_export_classes_do_not_make_scope_nonempty():
    for object_type in ("CAMERA", "CURVE", "CURVES", "POINTCLOUD", "VOLUME", "LIGHT"):
        obj = _Object(object_type.title(), object_type, selected=True)
        context = _scope_context([obj], [obj])
        assert animation_export.collect_export_objects(context, _settings()) == []


def test_full_scene_scope_filters_disabled_classes_but_keeps_xforms():
    camera = _Object("Camera", "CAMERA")
    unsupported = _Object("Speaker", "SPEAKER")
    empty = _Object("Group", "EMPTY")
    mesh = _Object("Mesh")

    result = animation_export.collect_export_objects(
        _scope_context([camera, unsupported, empty, mesh], []),
        _settings(selected_objects_only=False),
    )

    assert result == [empty, mesh]


def test_full_scene_with_only_disabled_classes_fails_before_native_export():
    camera = _Object("Camera", "CAMERA")
    context = _scope_context([camera], [])

    with pytest.raises(RuntimeError, match="object class is enabled"):
        animation_export.prepare_animation_export(
            context,
            _settings(selected_objects_only=False, export_animation=False),
        )


def test_removed_raw_content_settings_cannot_reenable_selected_objects():
    camera = _Object("Camera", "CAMERA", selected=True)
    hair_curves = _Object("Groom", "CURVES", selected=True)
    point_cloud = _Object("Scan", "POINTCLOUD", selected=True)
    volume = _Object("Fog", "VOLUME", selected=True)
    light = _Object("Key", "LIGHT", selected=True)
    hair_settings = SimpleNamespace(type="HAIR")
    particle_system = SimpleNamespace(settings=hair_settings)
    particle_mesh = _Object(
        "ParticleHair",
        "MESH",
        particle_systems=[particle_system],
        selected=True,
    )

    curves_context = _scope_context(
        [camera, hair_curves, point_cloud, volume, light],
        [camera, hair_curves, point_cloud, volume, light],
    )
    assert animation_export.collect_export_objects(
        curves_context,
        _settings(
            export_cameras=True,
            export_curves=True,
            export_points=True,
            export_hair=True,
            export_volumes=True,
            export_lights=True,
        ),
    ) == []

    # A mesh carrying hair particles still exports as a mesh: meshes have no
    # off switch, and the removed hair setting cannot add its strands.
    particle_context = _scope_context([particle_mesh], [particle_mesh])
    assert animation_export.collect_export_objects(
        particle_context,
        _settings(export_hair=True),
    ) == [particle_mesh]


def test_full_scene_scope_also_ignores_removed_raw_content_settings():
    raw_objects = [
        _Object("Camera", "CAMERA"),
        _Object("Key", "LIGHT"),
        _Object("Curve", "CURVE"),
        _Object("Groom", "CURVES"),
        _Object("Scan", "POINTCLOUD"),
        _Object("Fog", "VOLUME"),
    ]
    mesh = _Object("Mesh", "MESH")

    result = animation_export.collect_export_objects(
        _scope_context([*raw_objects, mesh], []),
        _settings(
            selected_objects_only=False,
            export_meshes=True,
            export_cameras=True,
            export_lights=True,
            export_curves=True,
            export_points=True,
            export_hair=True,
            export_volumes=True,
        ),
    )

    assert result == [mesh]


def test_skinned_mesh_is_exportable_through_armature_switch_without_meshes():
    armature = _Object("Rig", "ARMATURE")
    modifier = SimpleNamespace(
        type="ARMATURE",
        object=armature,
        show_render=True,
        show_viewport=True,
    )
    mesh = _Object("Body", modifiers=[modifier], selected=True)
    context = _scope_context([armature, mesh], [mesh])

    assert animation_export.collect_export_objects(
        context,
        _settings(export_meshes=False, export_armatures=True),
    ) == [armature, mesh]


def test_native_selection_does_not_expand_parents_or_instance_prototypes():
    parent = _Object("Parent")
    prototype = _Object("Prototype")
    collection = SimpleNamespace(all_objects=[prototype])
    child = _Object("Child", parent=parent, instance_collection=collection, selected=True)
    context = _scope_context([parent, child], [child])

    native = animation_export.collect_export_objects(context, _settings())
    processing = animation_export.collect_processing_objects(context, native)

    assert native == [child]
    assert processing == [parent, child, prototype]


class _FCurve:
    def __init__(self, data_path, frame_range):
        self.data_path = data_path
        self._range = frame_range

    def range(self):
        return self._range


class _KeyframeStrip:
    type = "KEYFRAME"

    def __init__(self, slot, fcurves):
        self._slot = slot
        self._bag = SimpleNamespace(fcurves=fcurves)

    def channelbag(self, slot):
        return self._bag if slot is self._slot else None


class _Slot:
    def __init__(self, name, id_type="OBJECT", users=None):
        self.identifier = f"OB{name}"
        self.name_display = name
        self.target_id_type = id_type
        self._users = list(users or [])

    def users(self):
        return list(self._users)


def _action(name, slot, frame_range):
    fcurve = _FCurve("location", frame_range)
    strip = _KeyframeStrip(slot, [fcurve])
    return SimpleNamespace(
        name=name,
        slots=[slot],
        layers=[SimpleNamespace(strips=[strip])],
        use_frame_range=False,
        frame_range=frame_range,
    )


def _animated_object(name, action, slot):
    obj = _Object(name)
    obj.animation_data = SimpleNamespace(
        action=action,
        action_slot=slot,
        nla_tracks=[],
        use_tweak_mode=False,
    )
    return obj


def test_target_schedules_do_not_broadcast_actions():
    alpha_slot = _Slot("Alpha")
    beta_slot = _Slot("Beta")
    alpha_action = _action("AlphaMove", alpha_slot, (1.0, 11.0))
    beta_action = _action("BetaMove", beta_slot, (5.0, 25.0))
    alpha = _animated_object("Alpha", alpha_action, alpha_slot)
    beta = _animated_object("Beta", beta_action, beta_slot)

    targets = animation_export._collect_targets([alpha, beta], _settings())
    actions = animation_export._collect_actions_for_targets(targets)
    schedule, _total = animation_export._build_schedule(actions, targets=targets)

    alpha_schedule = animation_export._schedule_for_target(schedule, targets[0])
    beta_schedule = animation_export._schedule_for_target(schedule, targets[1])
    assert [
        (
            segment["start_frame"],
            segment["end_frame"],
            segment["end_frame_exclusive"],
        )
        for segment in schedule
    ] == [(1, 11, 12), (12, 32, 33)]
    assert [segment["name"] for segment in alpha_schedule] == ["AlphaMove"]
    assert [segment["name"] for segment in beta_schedule] == ["BetaMove"]
    assert alpha_schedule[0]["slot"] is alpha_slot
    assert beta_schedule[0]["slot"] is beta_slot


def test_only_active_and_nla_owned_actions_are_scheduled():
    active_slot = _Slot("Cube")
    nla_slot = _Slot("Cube")
    orphan_slot = _Slot("Cube")
    active = _action("Active", active_slot, (0.0, 10.0))
    nla = _action("NLA", nla_slot, (0.0, 20.0))
    _orphan = _action("Orphan", orphan_slot, (0.0, 30.0))
    owner = _animated_object("Cube", active, active_slot)
    owner.animation_data.nla_tracks = [
        SimpleNamespace(strips=[SimpleNamespace(action=nla, action_slot=nla_slot)])
    ]

    bindings = animation_export._action_bindings_for_owner(owner)

    assert [(action.name, slot) for action, slot in bindings] == [
        ("Active", active_slot),
        ("NLA", nla_slot),
    ]


def test_action_slot_users_preserve_owned_library_takes(monkeypatch):
    active_slot = _Slot("Cube")
    active = _action("Active", active_slot, (0.0, 10.0))
    owner = _animated_object("Cube", active, active_slot)
    library_slot = _Slot("Cube", users=[owner])
    library_take = _action("LibraryTake", library_slot, (0.0, 20.0))
    monkeypatch.setattr(
        animation_export.bpy,
        "data",
        SimpleNamespace(actions=[active, library_take]),
        raising=False,
    )

    bindings = animation_export._action_bindings_for_owner(owner)

    assert [(action.name, slot) for action, slot in bindings] == [
        ("Active", active_slot),
        ("LibraryTake", library_slot),
    ]


def test_slotless_action_assignment_fails_closed():
    slot = _Slot("Cube")
    action = _action("Broken", slot, (0.0, 10.0))
    owner = _animated_object("Cube", action, None)

    try:
        animation_export._action_bindings_for_owner(owner)
    except RuntimeError as exc:
        assert "has no Action slot" in str(exc)
    else:
        raise AssertionError("slotless Action unexpectedly accepted")


class _Diagnostics:
    def __init__(self):
        self.warnings = []

    def add_warning(self, message):
        self.warnings.append(message)


def test_fractional_action_ranges_are_quantized_without_source_truncation():
    slots = [_Slot("Rig") for _ in range(4)]
    actions = [
        _action("Agree", slots[0], (0.0, 312.0)),
        _action("Running", slots[1], (0.0, 15.2)),
        _action("Walking", slots[2], (0.0, 24.8)),
        _action("walking_2", slots[3], (0.0, 28.8)),
    ]
    target = {"bindings": list(zip(actions, slots))}

    diagnostics = _Diagnostics()
    schedule, final_end = animation_export._build_schedule(
        actions,
        diagnostics,
        targets=[target],
    )

    assert [segment["start_frame"] for segment in schedule] == [
        1,
        314,
        331,
        357,
    ]
    assert [segment["end_frame"] for segment in schedule] == [
        313,
        330,
        356,
        386,
    ]
    assert [segment["end_frame_exclusive"] for segment in schedule] == [
        314,
        331,
        357,
        387,
    ]
    assert final_end == 386
    assert [segment["action_end"] for segment in schedule] == [
        312.0,
        15.2,
        24.8,
        28.8,
    ]
    assert len(diagnostics.warnings) == 3
    assert "source range [0, 15.2]" in diagnostics.warnings[0]
    assert "exported sample range [314, 330]" in diagnostics.warnings[0]
    assert "time-scaled" in diagnostics.warnings[0]


class _FailingStrips:
    def __init__(self):
        self.calls = 0

    def new(self, _name, _start, _action):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("forced second-strip failure")
        return SimpleNamespace(action_slot=None)


class _Track:
    def __init__(self):
        self.name = "NlaTrack"
        self.strips = _FailingStrips()
        self.mute = False
        self.is_solo = False


class _Tracks(list):
    def new(self):
        track = _Track()
        self.append(track)
        return track

    def get(self, name):
        return next((track for track in self if track.name == name), None)

    def remove(self, track):
        super().remove(track)


def test_mid_schedule_failure_removes_partial_track_and_restores_assignment():
    original_action = SimpleNamespace(name="Original")
    original_slot = SimpleNamespace(identifier="OBCube")
    tracks = _Tracks()
    anim_data = SimpleNamespace(
        action=original_action,
        action_slot=original_slot,
        last_slot_identifier="OBCube",
        use_nla=False,
        action_blend_type="REPLACE",
        action_extrapolation="HOLD",
        action_influence=0.75,
        nla_tracks=tracks,
    )
    action = SimpleNamespace(name="Take")
    slot = SimpleNamespace(identifier="OBCube")
    schedule = [
        {
            "name": "First",
            "action": action,
            "slot": slot,
            "action_start": 0.25,
            "action_end": 2.5,
            "start_frame": 1,
            "end_frame": 3,
            "end_frame_exclusive": 4,
            "length": 2.25,
        },
        {
            "name": "Second",
            "action": action,
            "slot": slot,
            "action_start": 0.0,
            "action_end": 2.0,
            "start_frame": 4,
            "end_frame": 6,
            "end_frame_exclusive": 7,
            "length": 2.0,
        },
    ]

    with pytest.raises(RuntimeError, match="forced second-strip failure"):
        animation_export._apply_schedule(
            anim_data,
            schedule,
            track_name="__TxnTrack__",
        )

    assert tracks == []
    assert anim_data.action is original_action
    assert anim_data.action_slot is original_slot
    assert anim_data.last_slot_identifier == "OBCube"
    assert anim_data.use_nla is False
    assert anim_data.action_blend_type == "REPLACE"
    assert anim_data.action_extrapolation == "HOLD"
    assert anim_data.action_influence == 0.75


def test_shape_key_data_path_escapes_quotes_in_the_name(monkeypatch):
    """A shape key named e.g. Eye "Blink" must still animate.

    fcurves.new() does not validate data_path, so an unescaped name produced
    key_blocks["Eye "Blink""].value - a well-formed F-Curve object pointing at
    nothing. The bake loop keyframed it, the action was assigned, and the key
    exported as a static value with no warning anywhere.
    """
    recorded = []

    def fake_ensure(action, owner, data_path, index=0, group_name=None):
        recorded.append(data_path)
        return SimpleNamespace(keyframe_points=SimpleNamespace(insert=lambda *a, **k: None))

    monkeypatch.setattr(animation_export, "_ensure_action_fcurve", fake_ensure)
    monkeypatch.setattr(
        animation_export.bpy,
        "utils",
        SimpleNamespace(escape_identifier=lambda name: name.replace('"', '\\"')),
        raising=False,
    )

    key_blocks = [
        SimpleNamespace(name="Basis", value=0.0),
        SimpleNamespace(name='Eye "Blink"', value=0.0),
        SimpleNamespace(name="Plain", value=0.0),
    ]
    key = SimpleNamespace(key_blocks=key_blocks)

    monkeypatch.setattr(animation_export, "_new_action", lambda name, owner: SimpleNamespace())
    monkeypatch.setattr(animation_export, "_assign_action_and_slot", lambda *a, **k: None)
    monkeypatch.setattr(animation_export, "_slot_for_owner", lambda *a, **k: None)

    scene = SimpleNamespace(frame_set=lambda *a, **k: None)
    obj = SimpleNamespace(name="Head")
    animation_export._bake_shapekeys(scene, obj, key, SimpleNamespace(), total_frames=1)

    assert 'key_blocks["Eye \\"Blink\\""].value' in recorded
    assert 'key_blocks["Plain"].value' in recorded


# ---------------------------------------------------------------------------
# Stashed Actions are dropped, and the code comment claimed otherwise.
#
# _action_bindings_for_owner documents its ActionSlot scan as covering "logical
# takes that are not the active Action and are not currently staged as NLA
# strips". In Blender 5.2 ActionSlot.users() returns only *live* users, so it is
# empty for exactly that case and the branch never fires for the scenario its
# comment describes. Measured: three Actions on one object (active / NLA strip /
# stashed with fake user) exported two takes and dropped the stashed one with no
# warning.
# ---------------------------------------------------------------------------


class _StashSlot:
    def __init__(self, live_users=()):
        self._users = list(live_users)

    def users(self):
        return list(self._users)


class _StashAction:
    def __init__(self, name, *, use_fake_user=False, live_users=()):
        self.name = name
        self.use_fake_user = use_fake_user
        self.slots = [_StashSlot(live_users)]


def _stash_diag():
    class _D:
        def __init__(self):
            self.warnings = []

        def add_warning(self, message):
            self.warnings.append(message)
    return _D()


def _with_actions(monkeypatch, actions):
    monkeypatch.setattr(
        animation_export.bpy, "data", SimpleNamespace(actions=actions), raising=False
    )


def test_stashed_action_is_reported(monkeypatch):
    stashed = _StashAction("StashedTake", use_fake_user=True)
    exported = _StashAction("ActiveTake", live_users=[object()])
    _with_actions(monkeypatch, [exported, stashed])
    diagnostics = _stash_diag()

    animation_export._warn_about_stashed_actions([exported], diagnostics)

    assert diagnostics.warnings
    assert "StashedTake" in diagnostics.warnings[0]
    assert "ActiveTake" not in diagnostics.warnings[0]


def test_exported_actions_are_not_reported(monkeypatch):
    exported = _StashAction("ActiveTake", use_fake_user=True, live_users=[object()])
    _with_actions(monkeypatch, [exported])
    diagnostics = _stash_diag()

    animation_export._warn_about_stashed_actions([exported], diagnostics)

    assert diagnostics.warnings == []


def test_an_action_without_a_fake_user_is_not_reported(monkeypatch):
    """No fake user means Blender will discard it; it is not a stashed take."""
    orphan = _StashAction("Orphan", use_fake_user=False)
    _with_actions(monkeypatch, [orphan])
    diagnostics = _stash_diag()

    animation_export._warn_about_stashed_actions([], diagnostics)

    assert diagnostics.warnings == []


def test_no_diagnostics_sink_is_not_an_error(monkeypatch):
    _with_actions(monkeypatch, [_StashAction("StashedTake", use_fake_user=True)])

    animation_export._warn_about_stashed_actions([], None)


def test_export_scope_by_object_type():
    """Meshes always export - the exporter's mesh flag is hard-coded on - so
    the scope check must not consult a mesh setting that does not exist.
    It used to read a phantom ``export_meshes`` attribute, which made the
    armature-modifier branch beside it unreachable."""
    from types import SimpleNamespace
    from Plugin.export.animation_export import _is_exportable_object

    def obj(kind):
        return SimpleNamespace(type=kind, modifiers=[])

    with_armatures = SimpleNamespace(export_armatures=True)
    without_armatures = SimpleNamespace(export_armatures=False)
    assert _is_exportable_object(obj("MESH"), without_armatures) is True
    assert _is_exportable_object(obj("ARMATURE"), with_armatures) is True
    assert _is_exportable_object(obj("ARMATURE"), without_armatures) is False
    assert _is_exportable_object(obj("EMPTY"), with_armatures) is True
    for kind in ("LIGHT", "CAMERA", "CURVE", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL", "SPEAKER"):
        assert _is_exportable_object(obj(kind), with_armatures) is False, kind
