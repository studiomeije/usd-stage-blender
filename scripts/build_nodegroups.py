"""
Build the RealityKit NodeGroup library (`Plugin/assets/nodegroups.blend`) for USD Stage for Blender.

Run in Blender:

  blender --background --factory-startup --python-exit-code 1 \
    --python scripts/build_nodegroups.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bpy


REQUIRED_BLENDER_SERIES = (5, 2)


def _parse_args() -> argparse.Namespace:
    script_argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(
        description="Build the USD Stage for Blender node-group library."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("Plugin/assets/nodegroups.blend"),
        help="Output .blend path, relative to the repository root by default.",
    )
    return parser.parse_args(script_argv)


def main() -> int:
    if tuple(bpy.app.version[:2]) != REQUIRED_BLENDER_SERIES:
        raise SystemExit(
            "Node groups must be built with Blender 5.2.x; "
            f"found {bpy.app.version_string}"
        )

    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    from Plugin.nodes import metadata
    from scripts import nodegroups as rk_nodegroups

    # Never allow groups from a startup file or an earlier generator version to
    # bypass `_build_group` merely because their stored rk_version still
    # matches.  The generated library must be a clean projection of the current
    # catalog and builder code.
    for group in list(bpy.data.node_groups):
        if group.get("rk_id") or metadata.is_catalog_group_name(group.name):
            bpy.data.node_groups.remove(group, do_unlink=True)

    asset_path = args.output
    if not asset_path.is_absolute():
        asset_path = repo_root / asset_path
    rk_nodegroups.save_nodegroup_library(asset_path)
    group_count = len(metadata.get_node_catalog())
    print(
        "USDSTAGE_NODEGROUP_BUILD="
        + json.dumps(
            {
                "blender_version": bpy.app.version_string,
                "group_count": group_count,
                "output": str(asset_path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
