"""Live Blender 5.2 regression for premultiplied-alpha texture policy."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_MARKER = "---PREMULTIPLIED_ALPHA_POLICY_JSON---"


DRIVER_SOURCE = r'''
import json
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import bpy

repo_root = Path(sys.argv[sys.argv.index("--") + 1])
scratch = Path(sys.argv[sys.argv.index("--") + 2])
marker = sys.argv[sys.argv.index("--") + 3]
sys.path.insert(0, str(repo_root))

from Plugin.export import usd_textures  # noqa: E402


def png_chunk(kind, payload):
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def write_rgba_png(path, width, height, pixels):
    rows = []
    stride = width * 4
    raw = bytes(pixels)
    for row in range(height):
        rows.append(b"\x00" + raw[row * stride:(row + 1) * stride])
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


def read_non_color_pixels(path):
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        image.colorspace_settings.name = "Non-Color"
        image.alpha_mode = "STRAIGHT"
        return tuple(int(value) for value in image.size), [float(value) for value in image.pixels[:]]
    finally:
        bpy.data.images.remove(image)


def associated_alpha_is_preserved(pixels, tolerance=2.0 / 255.0):
    samples = [pixels[index:index + 4] for index in range(0, len(pixels), 4)]
    return bool(samples) and all(
        abs(red - alpha) <= tolerance
        and abs(green) <= tolerance
        and abs(blue) <= tolerance
        for red, green, blue, alpha in samples
    )


def settings(image_format, resolution):
    return SimpleNamespace(
        export_texture_settings_enabled=True,
        bake_image_format=image_format,
        bake_resolution=resolution,
    )


def rejected(source, export_settings):
    try:
        usd_textures.require_safe_texture_alpha_staging_policy(
            source,
            alpha_mode="premul",
            has_premultiplied_alpha=True,
            settings=export_settings,
        )
    except RuntimeError as exc:
        return str(exc)
    return ""


bpy.ops.wm.read_factory_settings(use_empty=True)
scratch.mkdir(parents=True, exist_ok=True)

# Stored RGB is already associated with alpha: a red edge fades from opaque
# (255, alpha 255) to transparent black (0, alpha 0).
source = scratch / "associated-edge.png"
write_rgba_png(
    source,
    4,
    1,
    [
        255, 0, 0, 255,
        191, 0, 0, 191,
        64, 0, 0, 64,
        0, 0, 0, 0,
    ],
)
source_size, source_pixels = read_non_color_pixels(source)

# Original PNG staging is byte-for-byte and cannot alter alpha semantics.
scene = scratch / "scene.usda"
scene.write_text("#usda 1.0\n")
copy_state = usd_textures.create_texture_staging_state(
    scene,
    SimpleNamespace(export_texture_settings_enabled=False),
)
copied = usd_textures._stage_texture_source(
    source,
    authored_path=str(source),
    usd_attribute="/Material/BaseColor.inputs:file",
    state=copy_state,
)
copy_is_exact = copied.read_bytes() == source.read_bytes()

# PNG resizing uses the real Blender 5.2 ImBuf path. Both RGB and alpha must be
# filtered together, preserving the associated edge without a colored halo.
resized_png = scratch / "associated-edge-resized.png"
png_resize_ok = usd_textures._convert_texture_atomically(
    source,
    resized_png,
    {"file_format": "PNG", "extension": ".png", "resolution": 2},
)
resized_size, resized_pixels = read_non_color_pixels(resized_png)

# Produce a real AVIF fixture. Explicit AVIF output is refused for premultiplied
# alpha. An AVIF *source* is converted to PNG (Reality Composer Pro 3 refuses
# AVIF on import), so resizing one is the safe PNG path, not the AVIF encoder.
avif_fixture = scratch / "associated-edge.avif"
avif_fixture_created = usd_textures._convert_texture_atomically(
    source,
    avif_fixture,
    {"file_format": "AVIF", "extension": ".avif", "resolution": 0},
)
explicit_avif_error = rejected(source, settings("AVIF", "ORIGINAL"))
original_avif_resize_error = rejected(avif_fixture, settings("ORIGINAL", "2"))

# These are the intentionally safe policy branches.
png_resize_policy_error = rejected(source, settings("PNG", "2"))
original_avif_copy_error = rejected(avif_fixture, settings("ORIGINAL", "ORIGINAL"))

results = {
    "checks": {
        "blender_5_2": tuple(bpy.app.version[:2]) == (5, 2),
        "source_is_associated_edge": source_size == (4, 1)
        and associated_alpha_is_preserved(source_pixels),
        "png_copy_is_byte_exact": copy_is_exact,
        "png_resize_completed": png_resize_ok and resized_size == (2, 1),
        "png_resize_preserves_associated_edge": associated_alpha_is_preserved(resized_pixels),
        "real_avif_fixture_created": avif_fixture_created and avif_fixture.is_file(),
        "explicit_avif_is_rejected": "Select PNG" in explicit_avif_error,
        "original_avif_resize_converts_to_png": not original_avif_resize_error,
        "png_resize_policy_is_allowed": not png_resize_policy_error,
        "original_avif_copy_policy_is_allowed": not original_avif_copy_error,
    },
    "source_pixels": source_pixels,
    "resized_pixels": resized_pixels,
    "explicit_avif_error": explicit_avif_error,
    "original_avif_resize_error": original_avif_resize_error,
}

print(marker)
print(json.dumps(results, sort_keys=True))
print(marker)
'''


def test_blender_52_premultiplied_edge_png_safe_avif_encoding_rejected(tmp_path):
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    if shutil.which(blender) is None and not Path(blender).exists():
        pytest.skip("Blender not available (set USDSTAGE_BLENDER)")

    driver = tmp_path / "premultiplied_alpha_policy_driver.py"
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
