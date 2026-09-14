"""Blender 5.2 integration coverage for packaged node-group insertion."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_blender_probe(code: str) -> subprocess.CompletedProcess[str]:
    blender = os.environ.get("USDSTAGE_BLENDER", "blender")
    executable = shutil.which(blender)
    if executable is None:
        pytest.skip(f"Blender executable is unavailable: {blender}")
    return subprocess.run(
        [
            executable,
            "--background",
            "--factory-startup",
            "--python-exit-code",
            "1",
            "--python-expr",
            code,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )


def _assert_probe_passed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"Blender probe failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _operator_probe(*, asset_path: Path | None, expect_success: bool) -> str:
    asset_override = ""
    if asset_path is not None:
        asset_override = (
            "nodegroup_operators.nodegroups_asset_path = "
            f"lambda: Path({str(asset_path)!r})"
        )

    return textwrap.dedent(
        f"""
        import sys
        from pathlib import Path

        import bpy

        sys.path.insert(0, {str(REPO_ROOT)!r})
        from Plugin.nodes import metadata
        from Plugin.ops import nodegroup_operators
        {asset_override}

        for group in list(bpy.data.node_groups):
            bpy.data.node_groups.remove(group, do_unlink=True)
        assert not bpy.data.node_groups

        mesh = bpy.data.meshes.new("NodeGroupAssetProbeMesh")
        obj = bpy.data.objects.new("NodeGroupAssetProbeObject", mesh)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)

        material = bpy.data.materials.new("NodeGroupAssetProbeMaterial")
        obj.data.materials.append(material)
        for output in [
            node for node in material.node_tree.nodes
            if node.type == "OUTPUT_MATERIAL"
        ]:
            surface = output.inputs.get("Surface")
            if surface:
                for link in list(surface.links):
                    material.node_tree.links.remove(link)

        nodegroup_operators.register()
        try:
            try:
                result = bpy.ops.usdstage.add_rk_node(
                    rk_node_id="rk_pbr",
                    auto_connect=True,
                )
            except RuntimeError as exc:
                if {expect_success!r}:
                    raise
                assert "could not be loaded from the add-on" in str(exc)
                result = {{"CANCELLED"}}
        finally:
            nodegroup_operators.unregister()

        expected = {{"FINISHED"}} if {expect_success!r} else {{"CANCELLED"}}
        assert result == expected, (result, expected)
        catalog_groups = [
            group for group in bpy.data.node_groups if group.get("rk_id")
        ]
        inserted_nodes = [
            node
            for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeGroup"
        ]

        if {expect_success!r}:
            assert len(catalog_groups) == 1, [group.name for group in catalog_groups]
            group = catalog_groups[0]
            assert group.name == "PBR Surface (RealityKit)"
            assert group.get("rk_id") == "rk_pbr"
            assert group.get("rk_node_id") == "realitykit_pbr_surfaceshader"
            assert group.get("rk_version") == metadata.RK_NODE_VERSION
            assert group.library is None
            assert group.library_weak_reference is not None
            assert Path(group.library_weak_reference.filepath).resolve() == (
                Path({str(REPO_ROOT)!r}) / "Plugin" / "assets" / "nodegroups.blend"
            ).resolve()
            assert group.nodes
            assert len(inserted_nodes) == 1
            assert inserted_nodes[0].node_tree == group
            assert any(
                link.from_node == inserted_nodes[0]
                and link.to_node.type == "OUTPUT_MATERIAL"
                and link.to_socket.name == "Surface"
                for link in material.node_tree.links
            )
        else:
            assert catalog_groups == []
            assert inserted_nodes == []

        print("USDSTAGE_NODEGROUP_OPERATOR_PROBE=ok")
        """
    )


def test_real_operator_loads_only_requested_packaged_group():
    result = _run_blender_probe(
        _operator_probe(asset_path=None, expect_success=True)
    )

    _assert_probe_passed(result)
    assert "USDSTAGE_NODEGROUP_OPERATOR_PROBE=ok" in result.stdout


@pytest.mark.parametrize("asset_case", ["missing", "corrupt"])
def test_real_operator_fails_closed_without_dynamic_generation(tmp_path, asset_case):
    asset_path = tmp_path / "nodegroups.blend"
    if asset_case == "corrupt":
        asset_path.write_bytes(b"not a Blender library")

    result = _run_blender_probe(
        _operator_probe(asset_path=asset_path, expect_success=False)
    )

    _assert_probe_passed(result)
    assert "USDSTAGE_NODEGROUP_OPERATOR_PROBE=ok" in result.stdout
