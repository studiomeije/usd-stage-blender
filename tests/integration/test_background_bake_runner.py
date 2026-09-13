"""Integration tests - the sidebar's background bake job against real Blender.

The sidebar's Export button runs ``Plugin/bake_export_runner.py`` in a second
Blender process and reports through ``status.json``. These tests launch that
runner exactly as ``USDSTAGE_OT_bake_export_background`` does - a job
directory holding ``scene_snapshot.blend`` and ``settings.json`` - and read
what the job monitor and the diagnostics sidecar receive. The same scene also
goes through ``bake-export`` so the two lanes can be compared.

The scene is a Lighting & Shadows bake with no World and no lights, which is
guaranteed to produce the no-illumination warning, at the default source-keyed
resolution, which Lighting & Shadows replaces with 2048.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from blender_path import blender_executable


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "Plugin" / "bake_export_runner.py"

_BUILD = r'''
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 1
scene.world = None                       # nothing illuminates the scene

bpy.ops.mesh.primitive_cube_add(size=2)
obj = bpy.context.active_object
obj.name = "DarkCube"

material = bpy.data.materials.new("M")
material.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
    1.0, 1.0, 1.0, 1.0,
)
obj.data.materials.append(material)

bpy.ops.wm.save_as_mainfile(filepath=out)
'''

_NO_LIGHT_WARNING = "no World and no light objects"


@pytest.fixture(scope="module")
def background_job(tmp_path_factory):
    """Run one background job; every test inspects the same run."""
    workdir = tmp_path_factory.mktemp("background_bake")
    job_dir = workdir / "export" / ".usdstage_jobs" / "bake_export_test"
    job_dir.mkdir(parents=True)
    snapshot = job_dir / "scene_snapshot.blend"
    script = workdir / "build.py"
    script.write_text(_BUILD)
    built = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", "--python", str(script),
         "--", str(snapshot)],
        capture_output=True, text=True, timeout=300,
    )
    assert snapshot.exists(), built.stdout + built.stderr
    # The runner deletes the snapshot once loaded; keep a copy for bake-export.
    shutil.copyfile(snapshot, job_dir / "build_source.blend")

    export_path = workdir / "export" / "dark.usda"
    diagnostics_path = export_path.with_suffix(".diagnostics.json")
    payload = {
        "job_dir": str(job_dir),
        "blend_file": str(snapshot),
        "source_blend_file": None,
        "export_path": str(export_path),
        # What _serialize_settings sends from the sidebar for Unlit ->
        # Lighting & Shadows: texture settings forced on, resolution left at
        # its ORIGINAL default.
        "export_settings": {
            "export_format": "USDA",
            "bake_mode": "LIT_IBL",
            "bake_ibl_source": "SCENE_WORLD",
            "export_texture_settings_enabled": True,
            "bake_resolution": "ORIGINAL",
            "bake_image_format": "PNG",
        },
        "selected_only": False,
        "selection": [],
        "diagnostics_path": str(diagnostics_path),
        "success_diagnostics_enabled": True,
    }
    settings_path = job_dir / "settings.json"
    settings_path.write_text(json.dumps(payload, indent=2))

    proc = subprocess.run(
        [blender_executable(), "--background", "--factory-startup", str(snapshot),
         "--python", str(RUNNER), "--", str(settings_path)],
        capture_output=True, text=True, timeout=900,
    )
    status = json.loads((job_dir / "status.json").read_text())
    assert status.get("state") == "done", (
        f"background job did not finish: {status}\n{proc.stdout}\n{proc.stderr}"
    )
    diagnostics = json.loads(diagnostics_path.read_text())
    return status, diagnostics, export_path


@pytest.fixture(scope="module")
def cli_bake(background_job, tmp_path_factory):
    """The same scene and settings through ``bake-export``."""
    _status, _diagnostics, export_path = background_job
    source = next((export_path.parent / ".usdstage_jobs").rglob("build_source.blend"))
    out = tmp_path_factory.mktemp("cli_bake") / "dark.usda"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "Plugin"), "--json",
         "bake-export", str(source), "-o", str(out),
         "--format", "USDA", "--bake-mode", "LIT_IBL", "--ibl-source", "SCENE_WORLD",
         "--resolution", "ORIGINAL", "--image-format", "PNG", "--diagnostics"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    return payload, json.loads(Path(payload["diagnostics_path"]).read_text())


def test_terminal_status_carries_warning_text(background_job):
    """The job monitor draws ``status['warnings']``; the count alone in the
    message tells the artist nothing about why the textures are black."""
    status, diagnostics, _export_path = background_job

    assert _NO_LIGHT_WARNING in " ".join(diagnostics["warnings"])
    warnings = status.get("warnings")
    assert isinstance(warnings, list) and warnings, (
        f"terminal status carries no warning text: {status}"
    )
    assert any(_NO_LIGHT_WARNING in str(text) for text in warnings), warnings
    assert warnings == diagnostics["warnings"][:5]
    assert status["message"] == (
        f"Baked export complete - {len(diagnostics['warnings'])} warning(s)"
    )


def test_diagnostics_record_the_bake_block(background_job):
    """The background job records the same resolved decisions as bake-export."""
    _status, diagnostics, _export_path = background_job

    bake = diagnostics.get("bake")
    assert isinstance(bake, dict), "background job wrote no bake diagnostics block"
    assert bake["mode"] == "LIT_IBL"
    assert bake["image_format"] == "PNG"
    assert bake["margin"] == 8
    assert bake["object_count"] == 1
    assert bake["native_export_object_count"] == 1


def test_lit_ibl_records_the_resolution_it_baked_at(background_job):
    """ORIGINAL means source-keyed, which Lighting & Shadows replaces with 2048.
    The recorded value must be the size of the texture that was written."""
    _status, diagnostics, export_path = background_job
    Image = pytest.importorskip("PIL.Image", reason="pillow required for size checks")

    assert diagnostics["bake"]["resolution"] == 2048
    textures = sorted((export_path.parent / "textures").rglob("*_baseColor*.png"))
    assert textures, "no baked base color published"
    with Image.open(textures[0]) as image:
        assert image.size == (2048, 2048)


def test_superseded_bake_sources_are_not_published(background_job):
    """Texture staging copies each bake output to a content-addressed name; the
    unhashed bake source is superseded and must not ship beside the export."""
    _status, diagnostics, export_path = background_job

    removed = [
        entry for entry in diagnostics["generated_files"]
        if entry.get("role") == "removed_superseded_bake_texture"
    ]
    assert removed, "background job never removed superseded bake outputs"

    published = sorted(
        path.relative_to(export_path.parent).as_posix()
        for path in (export_path.parent / "textures").rglob("*")
        if path.is_file()
    )
    usda = export_path.read_text()
    unreferenced = [path for path in published if path not in usda]
    assert unreferenced == [], (
        f"published textures the USD does not reference: {unreferenced}"
    )


def test_bake_export_records_the_resolution_it_baked_at(cli_bake):
    payload, diagnostics = cli_bake

    assert diagnostics["bake"]["resolution"] == 2048
    assert payload["bake_stats"]["resolution"] == 2048


def test_both_lanes_record_the_same_bake_block(background_job, cli_bake):
    _status, background_diagnostics, _export_path = background_job
    _payload, cli_diagnostics = cli_bake

    def comparable(block):
        return {key: value for key, value in block.items() if key != "texture_dir"}

    assert comparable(background_diagnostics["bake"]) == comparable(cli_diagnostics["bake"])
