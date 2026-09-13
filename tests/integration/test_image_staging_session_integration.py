"""Live Blender 5.2 regression for export-scoped image staging."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_MARKER = "---IMAGE_STAGING_SESSION_JSON---"


DRIVER_SOURCE = r'''
import json
import sys
from pathlib import Path

import bpy

repo_root = Path(sys.argv[sys.argv.index("--") + 1])
scratch = Path(sys.argv[sys.argv.index("--") + 2])
marker = sys.argv[sys.argv.index("--") + 3]
sys.path.insert(0, str(repo_root))

from Plugin.export.materials.extract import core  # noqa: E402


def write_png(path, rgba, name):
    image = bpy.data.images.new(name, width=1, height=1, alpha=True)
    try:
        image.pixels[:] = rgba
        image.update()
        image.filepath_raw = str(path)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def read_pixel(path, name):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        return tuple(float(value) for value in image.pixels[:4])
    finally:
        bpy.data.images.remove(image)


bpy.ops.wm.read_factory_settings(use_empty=True)
scratch.mkdir(parents=True, exist_ok=True)
source = scratch / "current.png"
write_png(source, (1.0, 0.0, 0.0, 1.0), "DiskRed")

image = bpy.data.images.load(str(source), check_existing=False)
image.name = "LongLivedExportImage"
pointer = image.as_pointer()

# Export one snapshots unsaved blue pixels instead of stale red disk bytes.
image.pixels[:] = (0.0, 0.0, 1.0, 1.0)
image.update()
first_was_dirty = bool(image.is_dirty)
source_pixel_before = tuple(float(value) for value in image.pixels[:4])
first_session = core.begin_image_staging_session()
first_path = Path(core._resolve_image_path(image))
first_pixel = read_pixel(first_path, "FirstSnapshot")
first_cleanup_ok = core.cleanup_image_staging_session()
first_removed = not first_path.exists() and not first_session.exists()

# The same Blender datablock is reloaded from a newly written green file. A
# process-global pointer cache used to return export one's blue temp snapshot.
write_png(source, (0.0, 1.0, 0.0, 1.0), "DiskGreen")
image.reload()
same_pointer = image.as_pointer() == pointer
second_was_clean = not bool(image.is_dirty)
second_session = core.begin_image_staging_session()
second_path = Path(core._resolve_image_path(image))
second_pixel = read_pixel(second_path, "SecondSnapshot")
second_cleanup_ok = core.cleanup_image_staging_session()
second_removed = not second_path.exists() and not second_session.exists()

results = {
    "checks": {
        "blender_5_2": tuple(bpy.app.version[:2]) == (5, 2),
        "same_process_same_datablock": same_pointer,
        "first_image_was_dirty": first_was_dirty,
        "first_export_used_dirty_pixels": first_pixel[2] > first_pixel[0],
        "first_export_cleanup": first_cleanup_ok and first_removed,
        "second_export_reloaded_clean_file": second_was_clean,
        "second_export_used_current_pixels": second_pixel[1] > second_pixel[0] and second_pixel[1] > second_pixel[2],
        "second_export_did_not_reuse_path": second_path != first_path,
        "second_export_cleanup": second_cleanup_ok and second_removed,
        "cache_cleared": not core._STAGED_IMAGE_CACHE and core._STAGED_IMAGE_DIR is None,
    },
    "source_pixel_before": source_pixel_before,
    "first_pixel": first_pixel,
    "second_pixel": second_pixel,
}

bpy.data.images.remove(image)
print(marker)
print(json.dumps(results, sort_keys=True))
print(marker)
'''


def test_second_export_reads_current_pixels_and_cleans_session(tmp_path):
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    if shutil.which(blender) is None and not Path(blender).exists():
        pytest.skip("Blender not available (set USDSTAGE_BLENDER)")

    driver = tmp_path / "image_staging_driver.py"
    driver.write_text(DRIVER_SOURCE)
    proc = subprocess.run(
        [
            blender,
            "--background",
            "--factory-startup",
            "--python",
            str(driver),
            "--",
            str(REPO_ROOT),
            str(tmp_path / "scratch"),
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
    checks = results.get("checks", {})
    assert checks and all(checks.values()), results
