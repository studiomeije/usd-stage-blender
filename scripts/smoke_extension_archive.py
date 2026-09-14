#!/usr/bin/env python3
"""Install an extension archive in an isolated Blender profile and smoke it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Optional, Tuple
import unicodedata
import zipfile


CANONICAL_MODULE = "bl_ext.user_default.usd_stage"
RESULT_MARKER = "USDSTAGE_ARCHIVE_SMOKE="
USD_STAGE_RESULT_MARKER = "USDSTAGE_USD_EXPORT_SMOKE="
USD_EXTENSIONS = {".usd", ".usda", ".usdc"}
USDZ_ALIGNMENT = 64


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    expected_codes: tuple[int, ...] = (0,),
    cwd: Optional[Path] = None,
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        rendered = " ".join(command)
        raise RuntimeError(
            f"Command timed out after {timeout:g}s: {rendered}\n"
            f"stdout:\n{exc.stdout or ''}\n"
            f"stderr:\n{exc.stderr or ''}"
        ) from exc
    if result.returncode not in expected_codes:
        rendered = " ".join(command)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: {rendered}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _parse_cli_json(result: subprocess.CompletedProcess[str], label: str):
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Installed CLI {label} did not return JSON.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        ) from exc


def _create_export_scene_code() -> str:
    """Return a source-independent Blender script for the tiny export scene."""
    return """
import bpy
import os

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0.0, 0.0, 0.0))
obj = bpy.context.active_object
obj.name = "USDStage_InstalledExportCube"

material = bpy.data.materials.new("USDStage_DefaultMaterial")
material.use_nodes = True
principled = material.node_tree.nodes.get("Principled BSDF")
if principled is None:
    raise RuntimeError("Factory material is missing Principled BSDF")
principled.inputs["Base Color"].default_value = (0.18, 0.42, 0.8, 1.0)
principled.inputs["Roughness"].default_value = 0.45
obj.data.materials.append(material)

destination = os.environ["USDSTAGE_SMOKE_EXPORT_SCENE"]
bpy.ops.wm.save_as_mainfile(filepath=destination)
if bpy.data.filepath != destination:
    raise RuntimeError(f"Tiny export scene was not saved to {destination}")
"""


def _usd_stage_probe_code() -> str:
    """Return a Blender-bundled USD probe with no extension/source imports."""
    return f"""
import json
import os
from pxr import Usd, UsdGeom, UsdShade

results = {{}}
for format_name, environment_key in (
    ("USDC", "USDSTAGE_SMOKE_USDC"),
    ("USDZ", "USDSTAGE_SMOKE_USDZ"),
):
    path = os.environ[environment_key]
    stage = Usd.Stage.Open(path, Usd.Stage.LoadAll)
    if stage is None:
        raise RuntimeError(f"Could not open {{format_name}} stage: {{path}}")

    prims = list(stage.Traverse())
    meshes = [prim for prim in prims if prim.IsA(UsdGeom.Mesh)]
    materials = [prim for prim in prims if prim.IsA(UsdShade.Material)]
    shaders = [UsdShade.Shader(prim) for prim in prims if prim.IsA(UsdShade.Shader)]
    shader_ids = sorted(
        str(shader.GetIdAttr().Get())
        for shader in shaders
        if shader.GetIdAttr().Get()
    )
    if not meshes:
        raise RuntimeError(f"{{format_name}} stage has no mesh prim")
    if not materials:
        raise RuntimeError(f"{{format_name}} stage has no material prim")
    if "ND_realitykit_pbr_surfaceshader_2_0" not in shader_ids:
        raise RuntimeError(
            f"{{format_name}} stage did not author the default realitykit_pbr2 "
            f"ShaderGraph contract (ND_realitykit_pbr_surfaceshader_2_0): {{shader_ids}}"
        )

    materialx_surface_outputs = 0
    for material_prim in materials:
        output = UsdShade.Material(material_prim).GetSurfaceOutput("mtlx")
        if output and output.HasConnectedSource():
            materialx_surface_outputs += 1
    if materialx_surface_outputs != len(materials):
        raise RuntimeError(
            f"{{format_name}} stage has incomplete MaterialX surface wiring: "
            f"{{materialx_surface_outputs}}/{{len(materials)}} materials"
        )

    point_count = 0
    bound_meshes = 0
    for mesh_prim in meshes:
        points = UsdGeom.Mesh(mesh_prim).GetPointsAttr().Get() or []
        point_count += len(points)
        bound_material, _relationship = (
            UsdShade.MaterialBindingAPI(mesh_prim).ComputeBoundMaterial()
        )
        if bound_material:
            bound_meshes += 1
    if point_count < 8:
        raise RuntimeError(
            f"{{format_name}} stage has incomplete cube geometry: {{point_count}} points"
        )
    if bound_meshes != len(meshes):
        raise RuntimeError(
            f"{{format_name}} stage lost material bindings: "
            f"{{bound_meshes}}/{{len(meshes)}} meshes bound"
        )

    results[format_name] = {{
        "mesh_count": len(meshes),
        "material_count": len(materials),
        "shader_ids": shader_ids,
        "materialx_surface_output_count": materialx_surface_outputs,
        "bound_mesh_count": bound_meshes,
        "point_count": point_count,
        "root_layer_identifier": stage.GetRootLayer().identifier,
    }}

print({USD_STAGE_RESULT_MARKER!r} + json.dumps(results, sort_keys=True))
"""


def _validate_usdc_structure(path: Path) -> dict:
    """Fail closed on the minimum binary crate contract without USD tooling."""
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"USDC export is not a regular file: {path}")
    file_size = path.stat().st_size
    if file_size <= 16:
        raise RuntimeError(f"USDC export is unexpectedly small ({file_size} bytes): {path}")
    header = path.read_bytes()[:16]
    if header[:8] != b"PXR-USDC":
        raise RuntimeError(
            f"USDC export has an invalid crate signature {header[:8]!r}: {path}"
        )
    return {
        "ok": True,
        "profile": "binary-usd-crate",
        "file_size": file_size,
        "header_hex": header.hex(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _safe_usdz_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return bool(path.parts) and all(part not in {"", ".", ".."} for part in path.parts)


def _member_data_offset(raw_archive, member: zipfile.ZipInfo) -> int:
    raw_archive.seek(member.header_offset)
    header = raw_archive.read(30)
    if len(header) != 30:
        raise RuntimeError(f"Truncated local ZIP header for {member.filename}")
    signature, = struct.unpack_from("<I", header, 0)
    if signature != 0x04034B50:
        raise RuntimeError(f"Invalid local ZIP header for {member.filename}")
    filename_length, extra_length = struct.unpack_from("<HH", header, 26)
    return member.header_offset + 30 + filename_length + extra_length


def _validate_usdz_structure(
    path: Path,
    allowed_member_extensions,
) -> dict:
    """Validate USDZ using the contract read from the installed extension."""
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"USDZ export is not a regular file: {path}")

    allowed_extensions = {
        str(extension).strip().lower() for extension in allowed_member_extensions
    }
    if not allowed_extensions or any(
        not extension.startswith(".") for extension in allowed_extensions
    ):
        raise RuntimeError(
            "Installed extension returned an invalid USDZ member-extension contract: "
            f"{sorted(allowed_extensions)}"
        )

    errors: list[str] = []
    members: list[zipfile.ZipInfo] = []
    offsets: dict[str, int] = {}
    try:
        with zipfile.ZipFile(path, "r") as archive, path.open("rb") as raw_archive:
            members = archive.infolist()
            if not members:
                errors.append("package is empty")
            else:
                root = PurePosixPath(members[0].filename)
                if len(root.parts) != 1 or root.suffix.lower() not in USD_EXTENSIONS:
                    errors.append("first package member is not a root USD layer")

            seen_names: set[str] = set()
            for member in members:
                normalized = unicodedata.normalize("NFC", member.filename).casefold()
                if normalized in seen_names:
                    errors.append(f"case/Unicode-colliding member: {member.filename}")
                seen_names.add(normalized)
                if not _safe_usdz_member_name(member.filename):
                    errors.append(f"unsafe member path: {member.filename}")
                extension = PurePosixPath(member.filename).suffix.lower()
                if extension not in allowed_extensions:
                    errors.append(
                        f"unsupported Apple USDZ member extension {extension!r}: "
                        f"{member.filename}"
                    )
                if member.is_dir():
                    errors.append(f"directory entry is forbidden: {member.filename}")
                if member.compress_type != zipfile.ZIP_STORED:
                    errors.append(f"compressed member: {member.filename}")
                if member.flag_bits & 0x1:
                    errors.append(f"encrypted member: {member.filename}")
                try:
                    offset = _member_data_offset(raw_archive, member)
                    offsets[member.filename] = offset
                    if offset % USDZ_ALIGNMENT:
                        errors.append(
                            f"misaligned member {member.filename}: payload offset {offset}"
                        )
                except (OSError, RuntimeError, struct.error) as exc:
                    errors.append(str(exc))

            bad_member = archive.testzip()
            if bad_member:
                errors.append(f"CRC check failed for member: {bad_member}")
            if members and PurePosixPath(members[0].filename).suffix.lower() == ".usdc":
                root_header = archive.read(members[0])[:8]
                if root_header != b"PXR-USDC":
                    errors.append(
                        f"root USDC layer has invalid crate signature {root_header!r}"
                    )
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        errors.append(f"invalid ZIP archive: {exc}")

    if errors:
        raise RuntimeError(f"USDZ structural validation failed for {path}: {errors}")
    return {
        "ok": True,
        "profile": "apple-usdz-64-byte-alignment",
        "file_size": path.stat().st_size,
        "member_count": len(members),
        "members": [member.filename for member in members],
        "payload_offsets": offsets,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _resolve_tool_command(
    name: str,
    env: dict[str, str],
) -> Tuple[Optional[list[str]], Optional[str]]:
    """Resolve an explicit tool, Xcode tool, or PATH tool without source imports."""
    override_key = f"{name.upper()}_BIN"
    override = env.get(override_key)
    if override:
        override_path = Path(override).expanduser().resolve()
        if not override_path.is_file() or not os.access(override_path, os.X_OK):
            raise RuntimeError(f"{override_key} is not an executable file: {override_path}")
        return [str(override_path)], str(override_path)

    xcrun = shutil.which("xcrun", path=env.get("PATH"))
    if xcrun:
        probe = subprocess.run(
            [xcrun, "--find", name],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        resolved = probe.stdout.strip()
        if probe.returncode == 0 and resolved and Path(resolved).is_file():
            return [xcrun, name], resolved

    direct = shutil.which(name, path=env.get("PATH"))
    if direct:
        return [direct], str(Path(direct).resolve())
    return None, None


def _run_strict_usdchecker(
    paths: list[Path],
    *,
    env: dict[str, str],
    cwd: Path,
) -> dict:
    command_prefix, tool_path = _resolve_tool_command("usdchecker", env)
    if command_prefix is None:
        return {"available": False, "tool_path": None, "validated": []}

    apple_checker = len(command_prefix) >= 2 and command_prefix[-1] == "usdchecker"
    profiles = [("generic-strict", [])]
    if apple_checker:
        profiles.append(("arkit-strict", ["--arkit"]))
    validated = []
    for path in paths:
        for profile, profile_arguments in profiles:
            command = [*command_prefix, *profile_arguments, "--strict", str(path)]
            result = _run(command, env=env, cwd=cwd)
            validated.append(
                {
                    "path": str(path),
                    "profile": profile,
                    "command": command,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                }
            )
    return {"available": True, "tool_path": tool_path, "validated": validated}


def _xros27_sdk_available(env: dict[str, str]) -> bool:
    xcrun = shutil.which("xcrun", path=env.get("PATH"))
    if not xcrun:
        return False
    probe = subprocess.run(
        [xcrun, "--sdk", "xros", "--show-sdk-version"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if probe.returncode != 0:
        return False
    try:
        return int(probe.stdout.strip().split(".", 1)[0]) >= 27
    except (TypeError, ValueError):
        return False


def _compile_with_realitytool(
    paths: list[Path],
    *,
    env: dict[str, str],
    cwd: Path,
) -> dict:
    command_prefix, tool_path = _resolve_tool_command("realitytool", env)
    if command_prefix is None:
        return {"available": False, "reason": "realitytool not found", "compiled": []}
    if not _xros27_sdk_available(env):
        return {
            "available": False,
            "tool_path": tool_path,
            "reason": "visionOS 27 SDK is not selected",
            "compiled": [],
        }

    compiled = []
    for path in paths:
        bundle = cwd / f"{path.stem}-{path.suffix[1:]}.rkassets"
        bundle.mkdir()
        staged = bundle / f"scene{path.suffix.lower()}"
        shutil.copy2(path, staged)
        output = cwd / f"{path.stem}-{path.suffix[1:]}.reality"
        command = [
            *command_prefix,
            "compile",
            "--output-reality",
            str(output),
            "--platform",
            "xros",
            "--deployment-target",
            "27.0",
            "--use-metal",
            "true",
            str(bundle),
        ]
        result = _run(command, env=env, cwd=cwd)
        if not output.is_file() or output.is_symlink() or output.stat().st_size <= 0:
            raise RuntimeError(
                f"realitytool reported success without a non-empty output: {output}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        compiled.append(
            {
                "source": str(path),
                "command": command,
                "output_size": output.stat().st_size,
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            }
        )
    return {"available": True, "tool_path": tool_path, "compiled": compiled}


def _install_probe_code() -> str:
    return f"""
import bpy
import importlib
import json
import os
import sys

archive = os.environ["USDSTAGE_SMOKE_ARCHIVE"]
result = bpy.ops.extensions.package_install_files(
    filepath=archive,
    repo="user_default",
    enable_on_install=True,
    overwrite=True,
)
if "FINISHED" not in result:
    raise RuntimeError(f"Archive install did not finish: {{result}}")

canonical = {CANONICAL_MODULE!r}
enabled = sorted(
    addon.module
    for addon in bpy.context.preferences.addons
    if "usd_stage" in addon.module.lower()
)
if canonical not in enabled:
    raise RuntimeError(f"Canonical extension is not enabled: {{enabled}}")
if canonical not in sys.modules:
    raise RuntimeError(f"Canonical extension module was not imported: {{canonical}}")
if not hasattr(bpy.types.Scene, "usd_stage_export_settings"):
    raise RuntimeError("USD Stage for Blender scene settings were not registered")

aliases = sorted(
    name for name in sys.modules
    if name == "Plugin" or name.startswith("Plugin.")
)
if aliases:
    raise RuntimeError(f"Non-canonical Plugin modules were loaded: {{aliases}}")

# Validate the actual binary asset installed from the ZIP before any runtime
# builder can repair or replace it.  Matching IDs alone is insufficient: the
# interface is part of the authoring contract used by Blender materials.
metadata = importlib.import_module(canonical + ".nodes.metadata")
paths = importlib.import_module(canonical + ".core.paths")
nodegroup_operators = importlib.import_module(canonical + ".ops.nodegroup_operators")
usdz_packager = importlib.import_module(canonical + ".export.pack_usdz")
allowed_usdz_extensions = getattr(
    usdz_packager,
    "USDZ_ALLOWED_MEMBER_EXTENSIONS",
    None,
)
if not isinstance(allowed_usdz_extensions, (tuple, frozenset)):
    raise RuntimeError(
        "Installed packager lacks an immutable USDZ_ALLOWED_MEMBER_EXTENSIONS contract"
    )
if not allowed_usdz_extensions or any(
    not isinstance(extension, str) or not extension.startswith(".")
    for extension in allowed_usdz_extensions
):
    raise RuntimeError(
        f"Installed packager has an invalid USDZ extension contract: "
        f"{{allowed_usdz_extensions!r}}"
    )
catalog = metadata.get_node_catalog()
expected_by_id = {{str(entry["id"]): entry for entry in catalog}}
asset_path = paths.nodegroups_asset_path()
if not asset_path.is_file():
    raise RuntimeError(f"Packaged node-group library is missing: {{asset_path}}")

with bpy.data.libraries.load(str(asset_path), link=False) as (data_from, data_to):
    packaged_group_names = list(data_from.node_groups)
    data_to.node_groups = packaged_group_names
loaded_groups = [group for group in data_to.node_groups if group is not None]
if len(loaded_groups) != len(packaged_group_names):
    raise RuntimeError(
        f"Loaded only {{len(loaded_groups)}} of {{len(packaged_group_names)}} packaged node groups"
    )

loaded_by_id = {{}}
for group in loaded_groups:
    node_id = group.get("rk_id")
    if not isinstance(node_id, str) or not node_id:
        raise RuntimeError(f"Packaged node group lacks rk_id: {{group.name}}")
    if node_id in loaded_by_id:
        raise RuntimeError(f"Duplicate packaged node-group ID: {{node_id}}")
    loaded_by_id[node_id] = group

if set(loaded_by_id) != set(expected_by_id):
    raise RuntimeError(
        "Packaged node-group IDs differ from the installed manifest: "
        f"missing={{sorted(set(expected_by_id) - set(loaded_by_id))}}, "
        f"unexpected={{sorted(set(loaded_by_id) - set(expected_by_id))}}"
    )

# The interface socket types the node-group builder authors; anything else
# is built as a float socket.
INTERFACE_SOCKET_TYPES = {{
    "NodeSocketFloat", "NodeSocketInt", "NodeSocketBool", "NodeSocketVector",
    "NodeSocketColor", "NodeSocketMenu", "NodeSocketShader", "NodeSocketBundle",
    "NodeSocketClosure",
}}


def interface_socket_type(socket_type):
    if socket_type in INTERFACE_SOCKET_TYPES and hasattr(bpy.types, socket_type):
        return socket_type
    return "NodeSocketFloat"


for node_id, entry in expected_by_id.items():
    group = loaded_by_id[node_id]
    if group.get("rk_version") != metadata.RK_NODE_VERSION:
        raise RuntimeError(
            f"{{node_id}} has stale rk_version {{group.get('rk_version')!r}}; "
            f"expected {{metadata.RK_NODE_VERSION!r}}"
        )
    if group.get("rk_node_id") != entry.get("export_id"):
        raise RuntimeError(f"{{node_id}} has stale rk_node_id metadata")
    if not group.nodes:
        raise RuntimeError(f"{{node_id}} has an empty packaged preview graph")

    # Blender 5.2 stores group-interface outputs before inputs.  Preserve the
    # manifest order within each direction while matching that stable Blender
    # ordering exactly; normalizing the whole interface as a set would hide
    # meaningful socket-order drift.
    expected_interface = [
        (
            str(socket["name"]),
            "OUTPUT",
            interface_socket_type(str(socket["socket_type"])),
        )
        for socket in entry.get("io", {{}}).get("outputs", [])
    ] + [
        (
            str(socket["name"]),
            "INPUT",
            interface_socket_type(str(socket["socket_type"])),
        )
        for socket in entry.get("io", {{}}).get("inputs", [])
    ]
    actual_interface = [
        (str(item.name), str(item.in_out), str(item.socket_type))
        for item in group.interface.items_tree
        if str(getattr(item, "item_type", "")) == "SOCKET"
    ]
    if actual_interface != expected_interface:
        raise RuntimeError(
            f"{{node_id}} packaged interface differs from the installed manifest: "
            f"expected={{expected_interface!r}}, actual={{actual_interface!r}}"
        )

# Remove the inspection datablocks so the authoring probe exercises the normal
# installed asset-loader path from a clean in-memory group set.  Leaving all
# catalog groups preloaded here would mask a broken package resource lookup.
for group in loaded_groups:
    bpy.data.node_groups.remove(group)
preexisting_rk_groups = [
    group.name for group in bpy.data.node_groups if group.get("rk_id")
]
if preexisting_rk_groups:
    raise RuntimeError(
        f"Could not clear inspected node groups before clean-load probe: {{preexisting_rk_groups}}"
    )

# Exercise the installed authoring path, which must load rk_pbr on demand from
# the binary library shipped inside the installed archive.
mesh = bpy.data.meshes.new("USDStage_SmokeMesh")
obj = bpy.data.objects.new("USDStage_SmokeObject", mesh)
bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
material = bpy.data.materials.new("USDStage_SmokeMaterial")
material.use_nodes = True
obj.data.materials.append(material)
for output in [node for node in material.node_tree.nodes if node.type == "OUTPUT_MATERIAL"]:
    surface = output.inputs.get("Surface")
    if surface:
        for link in list(surface.links):
            material.node_tree.links.remove(link)

inserted, insert_error = nodegroup_operators._insert_group_node(
    bpy.context,
    "rk_pbr",
    auto_connect=True,
)
if inserted is None or inserted.node_tree is None:
    raise RuntimeError(f"Installed PBR authoring did not use the packaged rk_pbr node group: {{insert_error}}")
if inserted.node_tree.get("rk_id") != "rk_pbr":
    raise RuntimeError(
        f"Installed PBR authoring loaded the wrong node group: {{inserted.node_tree.name}}"
    )
weak_reference = getattr(inserted.node_tree, "library_weak_reference", None)
if weak_reference is None or not getattr(weak_reference, "filepath", ""):
    raise RuntimeError("Clean-loaded rk_pbr lacks packaged-library provenance")
loaded_from = os.path.realpath(bpy.path.abspath(weak_reference.filepath))
expected_asset = os.path.realpath(str(asset_path))
if loaded_from != expected_asset:
    raise RuntimeError(
        f"Clean-loaded rk_pbr came from {{loaded_from}}, expected {{expected_asset}}"
    )
clean_load_ids = sorted(
    str(group.get("rk_id"))
    for group in bpy.data.node_groups
    if group.get("rk_id")
)
if clean_load_ids != ["rk_pbr"]:
    raise RuntimeError(
        f"Clean installed authoring loaded unexpected catalog groups: {{clean_load_ids}}"
    )
if not any(
    link.from_node == inserted and link.to_node.type == "OUTPUT_MATERIAL"
    for link in material.node_tree.links
):
    raise RuntimeError("Installed PBR node group did not connect to Material Output")

bpy.ops.wm.save_userpref()
print({RESULT_MARKER!r} + json.dumps({{
    "installed": True,
    "canonical_module": canonical,
    "enabled_modules": enabled,
    "plugin_aliases": aliases,
    "nodegroup_count": len(loaded_by_id),
    "nodegroup_ids_match_manifest": True,
    "nodegroup_interfaces_match_manifest": True,
    "nodegroup_pbr_inserted": True,
    "nodegroup_clean_load_ids": clean_load_ids,
    "nodegroup_clean_load_asset": loaded_from,
    "usdz_allowed_member_extensions": sorted(allowed_usdz_extensions),
}}, sort_keys=True))
"""


#: Validation stages that need Apple tooling. Each one degrades to
#: ``{"available": False}`` when its tool is absent; that must not count as a
#: pass, or a lane without the tools would report the same success as one that
#: checked every artifact.
APPLE_VALIDATION_STAGES = ("usdchecker", "realitytool")


def unavailable_validation_stages(strict_checker: dict, realitytool: dict) -> list[str]:
    """Return the Apple validation stages that did not run."""
    return sorted(
        stage
        for stage, result in (
            ("usdchecker", strict_checker),
            ("realitytool", realitytool),
        )
        if not result.get("available")
    )


def undeclared_validation_gaps(
    skipped: list[str],
    allow_missing: frozenset[str],
) -> list[str]:
    """Return skipped stages the caller did not declare it would skip.

    Callers must name the stages their lane cannot perform, so a lane that
    stops validating exported artifacts is a workflow diff rather than an
    invisible change in what CI actually proves.
    """
    return sorted(stage for stage in skipped if stage not in allow_missing)


def smoke_archive(archive: Path, blender: str) -> dict:
    archive = archive.resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"Extension archive does not exist: {archive}")

    with tempfile.TemporaryDirectory(prefix="usdstage-archive-smoke-") as profile:
        profile_path = Path(profile)
        env = dict(os.environ)
        env["BLENDER_USER_RESOURCES"] = str(profile_path)
        env["USDSTAGE_SMOKE_ARCHIVE"] = str(archive)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Keep the workspace off child import paths.  Every extension command
        # below must bootstrap from the archive-installed root.
        env.pop("PYTHONPATH", None)

        install = _run(
            [
                blender,
                "--factory-startup",
                "--background",
                "--python-exit-code",
                "1",
                "--python-expr",
                _install_probe_code(),
            ],
            env=env,
        )
        install_lines = [
            line for line in install.stdout.splitlines() if line.startswith(RESULT_MARKER)
        ]
        if not install_lines:
            raise RuntimeError(
                "Blender archive install did not emit its success marker.\n"
                f"stdout:\n{install.stdout}\n"
                f"stderr:\n{install.stderr}"
            )
        install_result = json.loads(install_lines[-1][len(RESULT_MARKER) :])
        installed_usdz_extensions = install_result.get(
            "usdz_allowed_member_extensions"
        )
        if not isinstance(installed_usdz_extensions, list):
            raise RuntimeError(
                "Installed extension did not report its USDZ member-extension contract"
            )

        installed_root = (
            profile_path / "extensions" / "user_default" / "usd_stage"
        )
        if not (installed_root / "__main__.py").is_file():
            raise RuntimeError(f"Installed extension root is missing: {installed_root}")
        legal_files = {
            "LICENSE": (
                "GNU GENERAL PUBLIC LICENSE",
                "END OF TERMS AND CONDITIONS",
            ),
            "THIRD_PARTY_LICENSES/Apache-2.0.txt": (
                "Apache License",
                "Version 2.0, January 2004",
                "END OF TERMS AND CONDITIONS",
            ),
            "THIRD_PARTY_NOTICES.txt": (
                "Copyright © 2024 Apple Inc.",
                "Copyright Contributors to the MaterialX Project.",
                "THIRD_PARTY_LICENSES/Apache-2.0.txt",
            ),
        }
        for filename, required_texts in legal_files.items():
            legal_path = installed_root / filename
            if not legal_path.is_file():
                raise RuntimeError(f"Installed extension is missing {filename}")
            legal_text = legal_path.read_text(encoding="utf-8")
            if any(required_text not in legal_text for required_text in required_texts):
                raise RuntimeError(
                    f"Installed extension has an invalid or incomplete {filename}"
                )

        version = _run(
            [
                sys.executable,
                str(installed_root),
                "--blender",
                blender,
                "--json",
                "version",
            ],
            env=env,
        )
        version_result = _parse_cli_json(version, "version")
        if not version_result.get("plugin") or not version_result.get("blender"):
            raise RuntimeError(f"Installed CLI version result is incomplete: {version_result}")

        settings = _run(
            [
                sys.executable,
                str(installed_root),
                "--blender",
                blender,
                "--json",
                "settings",
                "list",
            ],
            env=env,
        )
        settings_result = _parse_cli_json(settings, "settings list")
        if not isinstance(settings_result, list) or not settings_result:
            raise RuntimeError("Installed CLI settings list returned no settings")

        # Create a tiny, ordinary Blender scene without importing the add-on,
        # then export it twice through the installed CLI directory entrypoint.
        # This is intentionally a real successful export: import-only probes
        # do not exercise packaged MaterialX data, post-processing, or USDZ
        # packaging resources.
        export_dir = profile_path / "installed-export-smoke"
        export_dir.mkdir()
        export_scene = export_dir / "default-material.blend"
        env["USDSTAGE_SMOKE_EXPORT_SCENE"] = str(export_scene)
        _run(
            [
                blender,
                "--factory-startup",
                "--background",
                "--python-exit-code",
                "1",
                "--python-expr",
                _create_export_scene_code(),
            ],
            env=env,
            cwd=profile_path,
        )
        if not export_scene.is_file() or export_scene.stat().st_size <= 0:
            raise RuntimeError("Could not create the tiny installed-export smoke scene")

        export_paths = {
            "USDC": export_dir / "default-material.usdc",
            "USDZ": export_dir / "default-material.usdz",
        }
        export_results = {}
        for format_name, output_path in export_paths.items():
            cli_export = _run(
                [
                    sys.executable,
                    str(installed_root),
                    "--blender",
                    blender,
                    "--json",
                    "export",
                    str(export_scene),
                    "-o",
                    str(output_path),
                    "--format",
                    format_name,
                    "--no-diagnostics",
                ],
                env=env,
                cwd=profile_path,
            )
            cli_result = _parse_cli_json(cli_export, f"{format_name} export")
            if cli_result.get("ok") is not True or cli_result.get("format") != format_name:
                raise RuntimeError(
                    f"Installed CLI returned an invalid {format_name} result: {cli_result}"
                )
            reported_path = Path(str(cli_result.get("export_path", ""))).resolve()
            if reported_path != output_path.resolve():
                raise RuntimeError(
                    f"Installed CLI reported the wrong {format_name} path: {cli_result}"
                )
            if not output_path.is_file() or output_path.is_symlink():
                raise RuntimeError(
                    f"Installed CLI did not create a regular {format_name} output: {output_path}"
                )
            export_results[format_name] = cli_result

        structure_results = {
            "USDC": _validate_usdc_structure(export_paths["USDC"]),
            "USDZ": _validate_usdz_structure(
                export_paths["USDZ"],
                installed_usdz_extensions,
            ),
        }

        # Open both exports with Blender 5.2's bundled USD runtime and assert
        # that geometry, the default material, and bindings survived.  This
        # remains available in Linux CI even when Apple's command-line tools
        # are not installed.
        env["USDSTAGE_SMOKE_USDC"] = str(export_paths["USDC"])
        env["USDSTAGE_SMOKE_USDZ"] = str(export_paths["USDZ"])
        usd_stage_probe = _run(
            [
                blender,
                "--factory-startup",
                "--background",
                "--python-exit-code",
                "1",
                "--python-expr",
                _usd_stage_probe_code(),
            ],
            env=env,
            cwd=profile_path,
        )
        stage_lines = [
            line
            for line in usd_stage_probe.stdout.splitlines()
            if line.startswith(USD_STAGE_RESULT_MARKER)
        ]
        if not stage_lines:
            raise RuntimeError(
                "Blender USD stage probe did not emit its success marker.\n"
                f"stdout:\n{usd_stage_probe.stdout}\n"
                f"stderr:\n{usd_stage_probe.stderr}"
            )
        stage_results = json.loads(
            stage_lines[-1][len(USD_STAGE_RESULT_MARKER) :]
        )
        if set(stage_results) != set(export_paths):
            raise RuntimeError(f"Incomplete Blender USD stage results: {stage_results}")

        strict_checker = _run_strict_usdchecker(
            list(export_paths.values()),
            env=env,
            cwd=profile_path,
        )
        realitytool = _compile_with_realitytool(
            list(export_paths.values()),
            env=env,
            cwd=profile_path,
        )

        # The background bake worker is another file-path entry point.  An
        # empty scene is expected to stop at object preflight; reaching that
        # controlled status proves package bootstrap and add-on registration
        # completed without relying on a source-tree ``Plugin`` import.
        job_dir = profile_path / "bake-runner-smoke"
        job_dir.mkdir()
        scene_snapshot = job_dir / "scene_snapshot.blend"
        env["USDSTAGE_SMOKE_SNAPSHOT"] = str(scene_snapshot)
        _run(
            [
                blender,
                "--factory-startup",
                "--background",
                "--python-exit-code",
                "1",
                "--python-expr",
                "import bpy, os; "
                "[bpy.data.objects.remove(obj, do_unlink=True) "
                "for obj in list(bpy.data.objects)]; "
                "assert len(bpy.data.objects) == 0; "
                "bpy.ops.wm.save_as_mainfile("
                "filepath=os.environ['USDSTAGE_SMOKE_SNAPSHOT'])",
            ],
            env=env,
        )
        if not scene_snapshot.is_file():
            raise RuntimeError("Could not create the background worker scene snapshot")

        source_blend_file = str(profile_path / "original-source.blend")
        diagnostics_path = job_dir / "empty.diagnostics.json"
        settings_path = job_dir / "settings.json"
        settings_path.write_text(
            json.dumps(
                {
                    "job_dir": str(job_dir),
                    "blend_file": str(scene_snapshot),
                    "source_blend_file": source_blend_file,
                    "export_path": str(job_dir / "empty.usdz"),
                    "export_settings": {},
                    "selected_only": False,
                    "diagnostics_path": str(diagnostics_path),
                }
            )
        )
        bake_runner = _run(
            [
                blender,
                "--factory-startup",
                "--background",
                "--python-exit-code",
                "1",
                str(scene_snapshot),
                "--python",
                str(installed_root / "bake_export_runner.py"),
                "--",
                str(settings_path),
            ],
            env=env,
            expected_codes=(1,),
        )
        status_path = job_dir / "status.json"
        if not status_path.is_file():
            raise RuntimeError(
                "Background bake runner did not write status.json.\n"
                f"stdout:\n{bake_runner.stdout}\n"
                f"stderr:\n{bake_runner.stderr}"
            )
        bake_status = json.loads(status_path.read_text())
        if bake_status.get("state") != "error" or not str(
            bake_status.get("message", "")
        ).startswith("No exportable objects found"):
            raise RuntimeError(f"Unexpected background bake smoke status: {bake_status}")
        if scene_snapshot.exists():
            raise RuntimeError("Background worker leaked scene_snapshot.blend")
        if not diagnostics_path.is_file():
            raise RuntimeError("Background worker did not preserve failure diagnostics")
        diagnostics = json.loads(diagnostics_path.read_text())
        export_context = diagnostics.get("export_context", {})
        if export_context.get("source_blend_file") != source_blend_file:
            raise RuntimeError(
                "Background diagnostics lost the original source .blend path: "
                f"{export_context}"
            )
        if export_context.get("blend_file") != source_blend_file:
            raise RuntimeError(
                "Background diagnostics reported the temporary snapshot as the source: "
                f"{export_context}"
            )

        skipped_validations = unavailable_validation_stages(
            strict_checker, realitytool
        )

        return {
            **install_result,
            # Names the artifact validation this run did NOT perform. An empty
            # list is the only value that means "everything was checked".
            "skipped_validations": skipped_validations,
            "cli_version": version_result,
            "setting_count": len(settings_result),
            "installed_cli_exports": sorted(export_results),
            "installed_cli_export_results": export_results,
            "export_structure": structure_results,
            "usd_stage_probe": stage_results,
            "strict_usdchecker": strict_checker,
            "realitytool_compile": realitytool,
            "bake_runner_preflight": bake_status.get("message"),
            "scene_snapshot_removed": True,
            "diagnostic_source_blend_file": export_context.get("source_blend_file"),
            "legal_files": sorted(legal_files),
            "legal_notices_present": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument(
        "--blender",
        default=os.environ.get("USDSTAGE_BLENDER", "blender"),
    )
    parser.add_argument(
        "--allow-missing",
        default="",
        help=(
            "Comma-separated Apple validation stages this lane cannot perform "
            f"({', '.join(APPLE_VALIDATION_STAGES)}). Anything skipped without "
            "being named here fails the run, so a lane that stops validating "
            "exported artifacts has to say so in its workflow."
        ),
    )
    args = parser.parse_args()

    allow_missing = frozenset(
        stage.strip() for stage in args.allow_missing.split(",") if stage.strip()
    )
    unknown = sorted(allow_missing - set(APPLE_VALIDATION_STAGES))
    if unknown:
        parser.error(
            f"--allow-missing does not know these stages: {', '.join(unknown)}. "
            f"Valid stages: {', '.join(APPLE_VALIDATION_STAGES)}"
        )

    result = smoke_archive(args.archive, args.blender)
    # Print before failing: the blob says which artifacts were checked and is
    # the only diagnostic when a stage unexpectedly went missing.
    print(RESULT_MARKER + json.dumps(result, sort_keys=True))

    gaps = undeclared_validation_gaps(result["skipped_validations"], allow_missing)
    if gaps:
        print(
            "ERROR: Apple validation stages could not run: "
            + ", ".join(gaps)
            + ". Install the tooling for this lane, or pass --allow-missing "
            + ",".join(gaps)
            + " to declare that this lane does not validate exported artifacts.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
