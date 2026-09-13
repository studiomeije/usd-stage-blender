"""Live Blender 5.2 Action-slot scheduling and restoration coverage."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_MARKER = "---ACTION_SLOT_TEST_JSON---"


DRIVER_SOURCE = r'''
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy

repo_root = Path(sys.argv[sys.argv.index("--") + 1])
marker = sys.argv[sys.argv.index("--") + 2]
sys.path.insert(0, str(repo_root))

from Plugin.export import animation_export  # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def make_object(name):
    mesh = bpy.data.meshes.new(name + "Mesh")
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    return obj


def make_action(obj, name, first, last, end_frame=3.0):
    action = bpy.data.actions.new(name)
    slot = action.slots.new(id_type=obj.id_type, name=obj.name)
    anim = obj.animation_data_create()
    anim.action = action
    anim.action_slot = slot
    curve = action.fcurve_ensure_for_datablock(obj, "location", index=0)
    curve.keyframe_points.insert(1, first)
    curve.keyframe_points.insert(end_frame, last)
    return action, slot


alpha = make_object("Alpha")
beta = make_object("Beta")
gamma = make_object("Gamma")
delta = make_object("Delta")
alpha_action, alpha_slot = make_action(alpha, "AlphaMove", 10.0, 20.0)
beta_action, beta_slot = make_action(beta, "BetaMove", 100.0, 200.0)
gamma_a, gamma_a_slot = make_action(gamma, "GammaA", 210.0, 220.0)
gamma_b, gamma_b_slot = make_action(gamma, "GammaB", 300.0, 310.0)
delta_a, delta_a_slot = make_action(delta, "ZetaA", 410.0, 420.0, 3.2)
delta_b, delta_b_slot = make_action(delta, "ZetaB", 500.0, 510.0, 2.8)
gamma_track = gamma.animation_data.nla_tracks.new()
gamma_strip = gamma_track.strips.new("GammaA", 1, gamma_a)
gamma_strip.action_slot = gamma_a_slot
gamma_track.mute = True
delta_track = delta.animation_data.nla_tracks.new()
delta_strip = delta_track.strips.new("ZetaA", 1, delta_a)
delta_strip.action_slot = delta_a_slot
delta_track.mute = True

# Force a failure after one strip has been added. The helper must remove its
# partial track and restore the exact Action/slot/NLA state before re-raising.
transaction_schedule = [
    {
        "name": "TxnValid",
        "action": alpha_action,
        "slot": alpha_slot,
        "action_start": 1.0,
        "action_end": 3.0,
        "start_frame": 1,
        "end_frame": 3,
        "end_frame_exclusive": 4,
        "length": 2.0,
    },
    {
        "name": "TxnInvalid",
        "action": None,
        "slot": None,
        "action_start": 1.0,
        "action_end": 3.0,
        "start_frame": 4,
        "end_frame": 6,
        "end_frame_exclusive": 7,
        "length": 2.0,
    },
]
alpha_use_nla_before_transaction = alpha.animation_data.use_nla
try:
    animation_export._apply_schedule(
        alpha.animation_data,
        transaction_schedule,
        track_name="__TransactionProbe__",
    )
except Exception:
    transactional_cleanup = (
        alpha.animation_data.nla_tracks.get("__TransactionProbe__") is None
        and alpha.animation_data.action == alpha_action
        and alpha.animation_data.action_slot == alpha_slot
        and alpha.animation_data.use_nla == alpha_use_nla_before_transaction
    )
else:
    transactional_cleanup = False

alpha.select_set(True)
beta.select_set(False)
gamma.select_set(False)
delta.select_set(False)
bpy.context.view_layer.objects.active = alpha
scene.frame_start = 7
scene.frame_end = 42
scene.frame_set(9)

settings = SimpleNamespace(
    export_animation=True,
    selected_objects_only=False,
    export_meshes=True,
    export_armatures=True,
    export_shapekeys=False,
)


class Diagnostics:
    def __init__(self):
        self.schedule = None
        self.warnings = []
        self.errors = []

    def set_animation_schedule(self, **values):
        self.schedule = values

    def add_warning(self, message):
        self.warnings.append(message)

    def add_error(self, message):
        self.errors.append(message)


diagnostics = Diagnostics()
state = animation_export.prepare_animation_export(
    bpy.context,
    settings,
    diagnostics,
)
prepared = {}
for frame in (1, 3, 4, 6, 7, 9, 10, 13, 16, 17, 19):
    scene.frame_set(frame)
    prepared[str(frame)] = [
        alpha.location.x,
        beta.location.x,
        gamma.location.x,
        delta.location.x,
    ]

segments = {
    segment["name"]: (
        segment["start_frame"],
        segment["end_frame"],
        segment["end_frame_exclusive"],
    )
    for segment in diagnostics.schedule["segments"]
}

results = {
    "alpha_baked_has_slot": alpha.animation_data.action_slot is not None,
    "beta_baked_has_slot": beta.animation_data.action_slot is not None,
    "later_first_sample": abs(prepared["4"][1] - 100.0) < 1e-5,
    "alpha_first_sample": abs(prepared["1"][0] - 10.0) < 1e-5,
    "alpha_final_sample": abs(prepared["3"][0] - 20.0) < 1e-5,
    "beta_last_sample": abs(prepared["6"][1] - 200.0) < 1e-5,
    "same_target_prior_final_sample": abs(prepared["9"][2] - 220.0) < 1e-5,
    "same_target_later_first_sample": abs(prepared["10"][2] - 300.0) < 1e-5,
    "fractional_prior_final_sample": abs(prepared["16"][3] - 420.0) < 1e-5,
    "fractional_later_first_sample": abs(prepared["17"][3] - 500.0) < 1e-5,
    "fractional_final_sample": abs(prepared["19"][3] - 510.0) < 1e-5,
    "same_target_clip_ranges_are_distinct": (
        segments["GammaA"] == (7, 9, 10)
        and segments["GammaB"] == (10, 12, 13)
        and segments["ZetaA"] == (13, 16, 17)
        and segments["ZetaB"] == (17, 19, 20)
    ),
    "aggregate_hard_cut_limitation_reported": any(
        "cannot represent a discontinuous hard cut" in warning
        for warning in diagnostics.warnings
    ),
    "transactional_schedule_cleanup": transactional_cleanup,
}

animation_export.restore_animation_export(state)
results.update({
    "alpha_action_restored": alpha.animation_data.action == alpha_action,
    "alpha_slot_restored": alpha.animation_data.action_slot == alpha_slot,
    "beta_action_restored": beta.animation_data.action == beta_action,
    "beta_slot_restored": beta.animation_data.action_slot == beta_slot,
    "gamma_action_restored": gamma.animation_data.action == gamma_b,
    "gamma_slot_restored": gamma.animation_data.action_slot == gamma_b_slot,
    "delta_action_restored": delta.animation_data.action == delta_b,
    "delta_slot_restored": delta.animation_data.action_slot == delta_b_slot,
    "timeline_restored": (
        scene.frame_start == 7 and scene.frame_end == 42 and scene.frame_current == 9
    ),
    "selection_restored": (
        alpha.select_get()
        and not beta.select_get()
        and not gamma.select_get()
        and not delta.select_get()
        and bpy.context.view_layer.objects.active == alpha
    ),
})

print(marker)
print(json.dumps(results, sort_keys=True))
print(marker)
'''


def test_action_slots_are_target_owned_and_restored(tmp_path):
    driver = tmp_path / "animation_slot_driver.py"
    driver.write_text(DRIVER_SOURCE)
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    proc = subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(driver),
            "--",
            str(REPO_ROOT),
            OUTPUT_MARKER,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    chunks = proc.stdout.split(OUTPUT_MARKER)
    assert len(chunks) >= 3, proc.stdout + proc.stderr
    results = json.loads(chunks[-2].strip())
    assert results and all(results.values()), results
