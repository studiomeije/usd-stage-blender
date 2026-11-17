"""Re-exporting a payload-split asset to the path it was published at.

Publication treats every file below ``assets/`` as an immutable sidecar: it
never rewrites one in place. A payload layer at one fixed path therefore made
the second export to an output fail on its own payload from the first. The
payload now lives in the attempt's ``assets/<output>/<generation>/`` namespace,
so a re-export is a new file and the old generation is pruned after commit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("bpy", SimpleNamespace())

pytest.importorskip("pxr")
from pxr import Sdf, Usd, UsdGeom  # noqa: E402

from Plugin.export import blender_usd_export  # noqa: E402
from Plugin.export.payload import _payload_asset_path, split_asset_payload  # noqa: E402
from Plugin.export.staging_namespace import output_sidecar_namespace  # noqa: E402


REPOSITORY = Path(__file__).resolve().parents[2]
_SETTINGS = SimpleNamespace(payload_geometry=True)


def _staged_payload_export(final: Path, size: float, *, texture: bytes = b"albedo"):
    """Stage one export the way the exporter does, then split it.

    The staged tree carries a texture and a localized layer in the attempt's
    own generation, both referenced from inside the root prim, so the split
    has to re-anchor them for the payload's directory.
    """
    staging = blender_usd_export.create_export_staging_dir(final)
    staged = staging / final.name
    namespace = output_sidecar_namespace(staged).as_posix()

    texture_path = staging / "textures" / namespace / "albedo.png"
    texture_path.parent.mkdir(parents=True)
    texture_path.write_bytes(texture)

    detail_path = staging / "assets" / namespace / "detail.usda"
    detail_path.parent.mkdir(parents=True)
    detail = Usd.Stage.CreateNew(str(detail_path))
    UsdGeom.Cube.Define(detail, "/Detail/Bolt")
    detail.SetDefaultPrim(detail.GetPrimAtPath("/Detail"))
    detail.GetRootLayer().Save()
    del detail

    stage = Usd.Stage.CreateNew(str(staged))
    root = UsdGeom.Xform.Define(stage, "/Table")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.Cube.Define(stage, "/Table/Top").GetSizeAttr().Set(size)
    stage.GetPrimAtPath("/Table/Top").CreateAttribute(
        "albedo", Sdf.ValueTypeNames.Asset
    ).Set(Sdf.AssetPath(f"./textures/{namespace}/albedo.png"))
    stage.DefinePrim("/Table/Detail").GetReferences().AddReference(
        f"./assets/{namespace}/detail.usda"
    )

    written = split_asset_payload(stage, staged, _SETTINGS)
    assert written, "the split must happen for this test to mean anything"
    stage.GetRootLayer().Save()
    del stage
    return staged, written


def _publish(final: Path, size: float, **kwargs):
    staged, written = _staged_payload_export(final, size, **kwargs)
    blender_usd_export.publish_unpacked_export(staged, final)
    return written


def _manifest_sidecars(final: Path) -> list[str]:
    manifest = blender_usd_export._output_sidecar_manifest_path(final)
    return json.loads(manifest.read_text(encoding="utf-8"))["sidecars"]


def _payload_files(output_dir: Path) -> list[str]:
    return sorted(
        path.relative_to(output_dir).as_posix()
        for path in (output_dir / "assets").rglob("*_payload*.usda")
    )


def _composed_size(final: Path) -> float:
    stage = Usd.Stage.Open(str(final))
    return stage.GetPrimAtPath("/Table/Top").GetAttribute("size").Get()


def test_reexport_to_the_same_path_publishes_the_new_payload(tmp_path):
    final = tmp_path / "table.usda"
    first = _publish(final, 1.0)
    second = _publish(final, 2.0)

    assert first["payload_path"] != second["payload_path"]
    assert _composed_size(final) == 2.0

    interface = Sdf.Layer.FindOrOpen(str(final))
    interface.Reload()
    arc = interface.GetPrimAtPath("/Table").payloadList.explicitItems
    assert [item.assetPath for item in arc] == [second["payload_path"]]


def test_reexport_prunes_the_previous_payload_generation(tmp_path):
    final = tmp_path / "table.usda"
    first = _publish(final, 1.0)
    first_generation = (tmp_path / first["payload_path"]).parent
    second = _publish(final, 2.0)

    new_payload = Path(second["payload_path"]).as_posix().removeprefix("./")
    assert _payload_files(tmp_path) == [new_payload]
    assert not first_generation.exists()
    assert new_payload in _manifest_sidecars(final)
    assert not any(
        entry.startswith(first_generation.relative_to(tmp_path).as_posix())
        for entry in _manifest_sidecars(final)
    )


def test_unchanged_reexport_also_succeeds(tmp_path):
    """Identical content still lands in a fresh generation, not a collision."""
    final = tmp_path / "table.usda"
    _publish(final, 1.0)
    second = _publish(final, 1.0)

    assert _composed_size(final) == 1.0
    assert _payload_files(tmp_path) == [second["payload_path"].removeprefix("./")]


def test_moved_paths_resolve_from_the_payload_generation(tmp_path):
    final = tmp_path / "table.usda"
    written = _publish(final, 1.0)
    _publish(final, 2.0, texture=b"second albedo")

    stage = Usd.Stage.Open(str(final))
    albedo = stage.GetPrimAtPath("/Table/Top").GetAttribute("albedo").Get()
    assert Path(albedo.resolvedPath).read_bytes() == b"second albedo"
    assert Path(albedo.resolvedPath).is_relative_to(tmp_path / "textures")
    assert stage.GetPrimAtPath("/Table/Detail/Bolt")

    # The reference into the payload's own generation is authored next door.
    payload_path = Path(
        Sdf.Layer.FindOrOpen(str(final))
        .GetPrimAtPath("/Table").payloadList.explicitItems[0].assetPath
    )
    payload = Sdf.Layer.FindOrOpen(str(tmp_path / payload_path))
    references = payload.GetPrimAtPath("/Table/Detail").referenceList.prependedItems
    assert [item.assetPath for item in references] == ["./detail.usda"]
    assert written["payload_layer"] == "table_payload.usda"


def test_republished_payload_survives_relocation(tmp_path):
    output_dir = tmp_path / "delivery"
    final = output_dir / "table.usda"
    _publish(final, 1.0)
    _publish(final, 2.0)

    relocated = tmp_path / "relocated"
    import shutil

    shutil.copytree(output_dir, relocated)
    shutil.rmtree(output_dir)
    stage = Usd.Stage.Open(str(relocated / "table.usda"))
    albedo = stage.GetPrimAtPath("/Table/Top").GetAttribute("albedo").Get()
    assert Path(albedo.resolvedPath).is_relative_to(relocated)
    assert stage.GetPrimAtPath("/Table/Top").GetAttribute("size").Get() == 2.0
    assert stage.GetPrimAtPath("/Table/Detail/Bolt")


def test_another_outputs_payload_is_left_alone(tmp_path):
    table = tmp_path / "table.usda"
    chair = tmp_path / "chair.usda"
    chair_payload = _publish(chair, 5.0)["payload_path"]
    chair_bytes = (tmp_path / chair_payload).read_bytes()

    _publish(table, 1.0)
    _publish(table, 2.0)

    assert (tmp_path / chair_payload).read_bytes() == chair_bytes
    assert chair_payload.removeprefix("./") in _manifest_sidecars(chair)
    assert _composed_size(chair) == 5.0


def test_a_failed_republish_keeps_the_previous_payload(tmp_path, monkeypatch):
    final = tmp_path / "table.usda"
    first = _publish(final, 1.0)
    staged, second = _staged_payload_export(final, 2.0)

    def fail(phase):
        if phase == "after_sidecars":
            raise RuntimeError("simulated failure before the root swap")

    monkeypatch.setattr(blender_usd_export, "_publication_phase_checkpoint", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        blender_usd_export.publish_unpacked_export(staged, final)

    assert _composed_size(final) == 1.0
    assert (tmp_path / first["payload_path"]).exists()
    assert not (tmp_path / second["payload_path"]).exists()

    monkeypatch.undo()
    blender_usd_export.publish_unpacked_export(staged, final)
    assert _composed_size(final) == 2.0
    assert _payload_files(tmp_path) == [second["payload_path"].removeprefix("./")]


def test_a_hard_exit_mid_republish_leaves_the_old_root_whole(tmp_path):
    """Killed after the new payload is installed but before the root swap.

    The case an in-place payload rewrite could not survive: the old interface
    must still compose its own payload, not the new one.
    """
    final = tmp_path / "table.usda"
    first = _publish(final, 1.0)
    staged, second = _staged_payload_export(final, 2.0)
    script = r'''
import os
import sys
import types

sys.modules.setdefault("bpy", types.ModuleType("bpy"))
sys.path.insert(0, sys.argv[1])
from Plugin.export import blender_usd_export

def checkpoint(phase):
    if phase == "after_sidecars":
        os._exit(77)

blender_usd_export._publication_phase_checkpoint = checkpoint
blender_usd_export.publish_unpacked_export(sys.argv[2], sys.argv[3])
raise SystemExit(3)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(REPOSITORY), str(staged), str(final)],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 77, result.stderr

    assert _composed_size(final) == 1.0
    assert (tmp_path / first["payload_path"]).exists()

    blender_usd_export.publish_unpacked_export(staged, final)
    assert _composed_size(final) == 2.0
    assert _payload_files(tmp_path) == [second["payload_path"].removeprefix("./")]


def test_a_localized_layer_named_like_the_payload_is_not_overwritten(tmp_path):
    path = tmp_path / "table.usda"
    namespace = output_sidecar_namespace(path).as_posix()
    clash = tmp_path / "assets" / namespace / "table_payload.usda"
    clash.parent.mkdir(parents=True)
    clash.write_text("#usda 1.0\n")

    stage = Usd.Stage.CreateNew(str(path))
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/Table").GetPrim())
    UsdGeom.Cube.Define(stage, "/Table/Top")
    written = split_asset_payload(stage, path, _SETTINGS)

    assert written["payload_layer"] == "table_payload_2.usda"
    assert clash.read_text() == "#usda 1.0\n"


@pytest.mark.parametrize(("authored", "rebased"), [
    ("./textures/t.usda/g/a.png", "../../../textures/t.usda/g/a.png"),
    ("textures/a.png", "../../../textures/a.png"),
    ("./assets/t.usda/g/detail.usda", "./detail.usda"),
    ("./assets/t.usda/g/sub/detail.usda", "./sub/detail.usda"),
    ("./assets/t.usda/other/detail.usda", "../other/detail.usda"),
    ("./assets/shared.usda", "../../shared.usda"),
    ("../outside/a.png", "../../../../outside/a.png"),
    ("./kit.usdz[textures/a.png]", "../../../kit.usdz[textures/a.png]"),
    ("", ""),
    ("/abs/a.png", "/abs/a.png"),
    ("C:/abs/a.png", "C:/abs/a.png"),
    ("https://example.com/a.png", "https://example.com/a.png"),
])
def test_relative_paths_are_reanchored_to_the_payload_directory(authored, rebased):
    assert _payload_asset_path(authored, "assets/t.usda/g") == rebased
