#!/usr/bin/env python3
"""
Validate exported USD/USDZ/.rkassets for Reality Composer Pro compatibility.

Checks:
- usdchecker (structural)
- manifest lint (nodedef IDs)
- asset path lint (relative paths)
- optional realitytool compile for .rkassets bundles
- optional persisted, collision-safe .reality artifacts for runtime smoke tests
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional


NODEDEF_RE = re.compile(r'info:id\s*=\s*"(?P<nodedef>ND_[^"]+)"')
ASSET_RE = re.compile(r'@(?P<asset>[^@]+)@')
USD_EXTENSIONS = {".usda", ".usdc", ".usd", ".usdz"}
DEFAULT_TOOL_TIMEOUT_SECONDS = 600.0
INSPECTION_TOOL_TIMEOUT_SECONDS = 300.0


class ExternalToolTimeout(RuntimeError):
    """Raised after a complete external-tool process group is terminated."""

    def __init__(self, command: List[str], timeout: float, output: str = ""):
        self.command = command
        self.timeout = timeout
        self.output = output
        message = f"command timed out after {timeout:g}s: {' '.join(command)}"
        if output:
            message += f"\n{output}"
        super().__init__(message)


def main() -> int:
    args = _parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = repo_root / manifest_path

    manifest = _load_manifest(manifest_path)
    nodedefs = set(manifest.get("nodes", {}).keys())

    inputs = _collect_inputs(Path(args.input))
    if not inputs:
        print(f"No USD or rkassets found under {args.input}")
        return 2

    report: Dict[str, object] = {
        "input": str(Path(args.input).resolve()),
        "usdchecker": not args.no_usdchecker,
        "lint": not args.no_lint,
        "compile": not args.no_compile,
        "compiled_output_dir": (
            str(Path(args.compiled_output_dir).expanduser().resolve())
            if args.compiled_output_dir
            else None
        ),
        "results": [],
        "errors": [],
    }

    failures = 0
    compile_output_stems = _compile_output_stems(inputs)

    for entry in inputs:
        output_stem = compile_output_stems.get(entry)
        if entry.suffix.lower() == ".rkassets":
            result = _validate_rkassets(
                entry,
                nodedefs,
                args,
                compile_output_stem=output_stem,
            )
        else:
            result = _validate_usd(
                entry,
                nodedefs,
                args,
                compile_output_stem=output_stem,
            )

        report["results"].append(result)
        if result.get("status") != "ok":
            failures += 1

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))
        print(f"Report: {args.output}")

    return 0 if failures == 0 else 1


def _validate_usd(
    path: Path,
    nodedefs: set,
    args,
    *,
    compile_output_stem: Optional[str] = None,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "file": str(path),
        "status": "ok",
        "usdz_structure": None,
        "usdchecker": None,
        "lint": None,
        "compile": None,
    }

    if path.suffix.lower() == ".usdz":
        result["usdz_structure"] = _validate_usdz_structure(path)
        if result["usdz_structure"]["ok"] is False:
            result["status"] = "error"

    if not args.no_usdchecker:
        result["usdchecker"] = _run_usdchecker(
            path,
            # USDZ is an Apple delivery format. Its default validation must
            # include Apple's package restrictions even when a caller does not
            # remember to opt into the legacy-named --arkit flag.
            arkit=(path.suffix.lower() == ".usdz" or getattr(args, "arkit", False)),
            timeout=getattr(args, "tool_timeout", DEFAULT_TOOL_TIMEOUT_SECONDS),
        )
        if result["usdchecker"]["ok"] is False:
            result["status"] = "error"

    if not args.no_lint:
        lint = _lint_usd_path(path, nodedefs)
        result["lint"] = lint
        if lint["errors"]:
            result["status"] = "error"

    if not args.no_compile and _is_compilable_usd(path):
        compile_result = _compile_from_usd(
            path,
            args,
            output_stem=compile_output_stem,
        )
        result["compile"] = compile_result
        if compile_result["ok"] is False:
            result["status"] = "error"

    return result


def _validate_rkassets(
    path: Path,
    nodedefs: set,
    args,
    *,
    compile_output_stem: Optional[str] = None,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "rkassets": str(path),
        "status": "ok",
        "usdz_structure": None,
        "usdchecker": None,
        "lint": None,
        "compile": None,
    }

    scenes = _rkassets_scene_files(path)
    if not scenes:
        no_scene_message = (
            "No standalone USD/USDZ scene found; relying on direct realitytool "
            "compilation of the RCP3 bundle."
        )
        result["lint"] = {
            "errors": [],
            "warnings": [no_scene_message],
            "asset_count": 0,
        }
        if not args.no_usdchecker:
            result["usdchecker"] = {
                "ok": None,
                "skipped": True,
                "reason": no_scene_message,
            }
        if args.no_compile:
            result["status"] = "error"
            result["lint"]["errors"].append(
                "No validation gate remained after realitytool compilation was disabled."
            )

    package_results = [
        _validate_usdz_structure(scene)
        for scene in scenes
        if scene.suffix.lower() == ".usdz"
    ]
    if package_results:
        result["usdz_structure"] = (
            package_results[0] if len(package_results) == 1 else package_results
        )
        if any(item["ok"] is False for item in package_results):
            result["status"] = "error"

    if scenes and not args.no_usdchecker:
        checker_results = [
            _run_usdchecker(
                scene,
                arkit=(
                    scene.suffix.lower() == ".usdz"
                    or getattr(args, "arkit", False)
                ),
                timeout=getattr(args, "tool_timeout", DEFAULT_TOOL_TIMEOUT_SECONDS),
            )
            for scene in scenes
        ]
        result["usdchecker"] = checker_results[0] if len(checker_results) == 1 else checker_results
        if any(item["ok"] is False for item in checker_results):
            result["status"] = "error"

    if scenes and not args.no_lint:
        lint_results = [_lint_usd_path(scene, nodedefs) for scene in scenes]
        result["lint"] = lint_results[0] if len(lint_results) == 1 else lint_results
        if any(item["errors"] for item in lint_results):
            result["status"] = "error"

    if not args.no_compile:
        if compile_output_stem is None:
            compile_result = _compile_rkassets(path, args)
        else:
            compile_result = _compile_rkassets(
                path,
                args,
                output_stem=compile_output_stem,
            )
        result["compile"] = compile_result
        if compile_result["ok"] is False:
            result["status"] = "error"

    return result


def _collect_inputs(path: Path) -> List[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in USD_EXTENSIONS else []
    if path.suffix.lower() == ".rkassets" and path.is_dir():
        return [path]
    if not path.is_dir():
        return []
    results = []
    for ext in (".usda", ".usdc", ".usd", ".usdz", ".rkassets"):
        for entry in path.rglob(f"*{ext}"):
            if entry.is_file() or (
                entry.suffix.lower() == ".rkassets" and entry.is_dir()
            ):
                results.append(entry)
    # A .rkassets bundle is one validation unit; do not also report each of its
    # internal layers as an independent top-level export.
    bundle_roots = [
        entry
        for entry in results
        if entry.suffix.lower() == ".rkassets" and entry.is_dir()
    ]
    results = [
        entry
        for entry in results
        if entry in bundle_roots
        or not any(bundle in entry.parents for bundle in bundle_roots)
    ]
    # The same order on every platform: WindowsPath sorts case-insensitively
    # and PosixPath does not.
    return sorted(set(results), key=lambda item: item.as_posix().casefold())


def _compile_output_stems(inputs: List[Path]) -> Dict[Path, Optional[str]]:
    """Disambiguate persisted compiler outputs that share a source stem.

    A directory can legitimately contain ``Asset.usdc`` and ``Asset.usdz``,
    which would both reserve ``Asset-<platform>-<target>.reality``. A unique
    stem keeps the concise filename; a collision adds the source format.
    """
    groups: Dict[str, List[Path]] = {}
    for entry in inputs:
        safe_stem = _safe_output_stem(entry.stem)
        key = unicodedata.normalize("NFC", safe_stem).casefold()
        groups.setdefault(key, []).append(entry)

    output_stems: Dict[Path, Optional[str]] = {}
    reserved: set[str] = set()
    for entries in groups.values():
        collides = len(entries) > 1
        for entry in sorted(entries):
            source_kind = entry.suffix.lower().lstrip(".") or "asset"
            candidate: Optional[str] = (
                f"{entry.stem}-{source_kind}" if collides else None
            )
            effective_candidate = candidate or entry.stem
            safe_candidate = _safe_output_stem(effective_candidate)
            normalized = unicodedata.normalize("NFC", safe_candidate).casefold()
            if normalized in reserved:
                digest = hashlib.sha256(
                    entry.as_posix().encode("utf-8")
                ).hexdigest()[:8]
                candidate = f"{effective_candidate}-{digest}"
                safe_candidate = _safe_output_stem(candidate)
                normalized = unicodedata.normalize("NFC", safe_candidate).casefold()
            reserved.add(normalized)
            output_stems[entry] = candidate

    return output_stems


def _load_manifest(path: Path) -> Dict[str, object]:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise SystemExit(f"Failed to load manifest: {path}: {exc}") from exc


def _validate_usdz_structure(path: Path) -> Dict[str, object]:
    """Apply the package rules that make a ZIP a valid Apple USDZ.

    This check is deliberately independent of ``--no-usdchecker`` and
    ``--no-lint``. Those switches are useful for focused diagnostics, but they
    must not turn a misaligned package into a successful validation result.
    Reuse the exporter's structural validator so packaging and release tooling
    cannot silently diverge on alignment, compression, root-layer, path, CRC,
    or duplicate-name rules.
    """

    repo_root = Path(__file__).resolve().parents[1]
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)

    try:
        from Plugin.export.pack_usdz import validate_usdz_details

        valid, errors = validate_usdz_details(str(path))
    except (ImportError, OSError, RuntimeError) as exc:
        valid = False
        errors = [f"Could not run USDZ structural validation: {exc}"]

    return {
        "ok": bool(valid),
        "errors": list(errors),
        "profile": "apple-usdz-64-byte-alignment",
    }


def _run_usdchecker(
    path: Path,
    *,
    arkit: bool = False,
    timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> Dict[str, object]:
    checker = _resolve_usd_tool("usdchecker")
    if not checker:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "usdchecker not found",
            "exit_code": -1,
            "command": ["usdchecker", "--strict", str(path)],
            "timed_out": False,
        }

    command = [checker]
    if arkit:
        command.append("--arkit")
    command.extend(["--strict", str(path)])
    try:
        proc = _run_external_tool(command, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "exit_code": proc.returncode,
            "command": command,
            "tool_path": checker,
            "tool_version": _tool_version(checker),
            "timed_out": False,
        }
    except ExternalToolTimeout as exc:
        return {
            "ok": False,
            "stdout": exc.output,
            "stderr": str(exc),
            "exit_code": 124,
            "command": command,
            "tool_path": checker,
            "tool_version": _tool_version(checker),
            "timed_out": True,
        }
    except (OSError, RuntimeError) as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"usdchecker failed to start: {exc}",
            "exit_code": -1,
            "command": command,
            "tool_path": checker,
            "tool_version": _tool_version(checker),
            "timed_out": False,
        }


def _load_usd_text(path: Path) -> str:
    if path.suffix.lower() == ".usda":
        return path.read_text(errors="ignore")
    usdcat = _resolve_usd_tool("usdcat")
    if not usdcat:
        raise RuntimeError(f"usdcat is required to inspect binary/package USD: {path}")
    try:
        proc = _run_external_tool(
            [usdcat, str(path)],
            timeout=INSPECTION_TOOL_TIMEOUT_SECONDS,
        )
    except ExternalToolTimeout:
        raise
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"usdcat failed to start for {path}: {exc}") from exc
    if proc.returncode != 0:
        output = "\n".join(
            part for part in (proc.stdout.strip(), proc.stderr.strip()) if part
        )
        raise RuntimeError(
            f"usdcat failed for {path} with exit code {proc.returncode}"
            + (f": {output}" if output else "")
        )
    if not proc.stdout.strip():
        raise RuntimeError(f"usdcat produced no stage text for {path}")
    return proc.stdout


def _lint_usd_path(path: Path, nodedefs: set) -> Dict[str, object]:
    try:
        return _lint_usd_text(_load_usd_text(path), nodedefs)
    except ExternalToolTimeout as exc:
        return {
            "errors": [str(exc)],
            "warnings": [],
            "asset_count": 0,
            "timed_out": True,
            "exit_code": 124,
        }
    except (OSError, UnicodeError, RuntimeError) as exc:
        return {
            "errors": [str(exc)],
            "warnings": [],
            "asset_count": 0,
            "timed_out": False,
        }


def _lint_usd_text(text: str, nodedefs: set) -> Dict[str, object]:
    errors = []
    warnings = []

    for match in NODEDEF_RE.finditer(text):
        nodedef = match.group("nodedef")
        if nodedef not in nodedefs:
            errors.append(f"Unknown nodedef: {nodedef}")

    assets = [m.group("asset") for m in ASSET_RE.finditer(text)]
    for asset in assets:
        if _is_absolute_asset(asset):
            errors.append(f"Absolute asset path: {asset}")
        elif ".." in Path(asset.split("[", 1)[0]).parts:
            errors.append(f"Asset path escapes export root: {asset}")

    if "outputs:mtlx:surface.connect" not in text:
        warnings.append("No MaterialX surface output found.")

    return {"errors": errors, "warnings": warnings, "asset_count": len(assets)}


def _is_absolute_asset(asset: str) -> bool:
    lowered = asset.lower()
    if lowered.startswith(("http:", "https:", "data:", "blob:", "anon:", "mem:", "file:")):
        return True
    if os.path.isabs(asset):
        return True
    if re.match(r"^[a-zA-Z]:[\\/]", asset):
        return True
    return False


def _is_compilable_usd(path: Path) -> bool:
    return path.suffix.lower() in USD_EXTENSIONS


def _compile_from_usd(
    path: Path,
    args,
    *,
    output_stem: Optional[str] = None,
) -> Dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="usdstage_compile_") as temp_dir:
        bundle = Path(temp_dir) / f"{path.stem}.rkassets"
        bundle.mkdir(parents=True, exist_ok=True)
        try:
            if path.suffix.lower() == ".usdz":
                _extract_usdz_compile_input(path, bundle)
            else:
                shutil.copy2(path, bundle / f"scene{path.suffix.lower()}")
                _copy_compile_dependencies(path, bundle)
        except ExternalToolTimeout as exc:
            return {
                "ok": False,
                "stdout": exc.output,
                "stderr": str(exc),
                "exit_code": 124,
                "timed_out": True,
            }
        except (OSError, RuntimeError) as exc:
            return {
                "ok": False,
                "stdout": "",
                "stderr": f"Failed to stage Reality Composer Pro compile input: {exc}",
                "exit_code": -1,
                "timed_out": False,
            }

        return _compile_rkassets(
            bundle,
            args,
            output_stem=output_stem or path.stem,
        )


def _extract_usdz_compile_input(package: Path, bundle: Path) -> None:
    """Expand a validated USDZ into rkassets for a usable realitytool result.

    Realitytool 27 may exit successfully when a USDZ is nested as one file in
    an rkassets directory, yet emit a `.reality` that RealityKit cannot load.
    Compiling the package's root layer and dependencies directly avoids that
    false-positive path.
    """

    seen_names = set()
    root_scene = None
    try:
        with zipfile.ZipFile(package, "r") as archive:
            for index, member in enumerate(archive.infolist()):
                if member.is_dir():
                    continue
                name = member.filename
                pure = PurePosixPath(name)
                if (
                    not name
                    or "\\" in name
                    or pure.is_absolute()
                    or ".." in pure.parts
                ):
                    raise RuntimeError(
                        f"Unsafe USDZ compile member path: {name!r}"
                    )
                name_key = unicodedata.normalize("NFC", name).casefold()
                if name_key in seen_names:
                    raise RuntimeError(
                        f"Colliding USDZ compile member path: {name!r}"
                    )
                seen_names.add(name_key)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise RuntimeError(
                        f"Symlinked USDZ compile member is unsupported: {name!r}"
                    )
                if index == 0:
                    if (
                        len(pure.parts) != 1
                        or pure.suffix.lower() not in {".usd", ".usda", ".usdc"}
                    ):
                        raise RuntimeError(
                            "USDZ compile input must begin with a root USD layer"
                        )
                    root_scene = pure
                destination = bundle.joinpath(*pure.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, destination.open(
                    "xb"
                ) as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RuntimeError(f"Invalid USDZ compile input: {exc}") from exc
    if root_scene is None:
        raise RuntimeError("USDZ compile input contains no root USD layer")


def _copy_compile_dependencies(root: Path, bundle: Path) -> None:
    """Copy local asset/layer dependencies while preserving relative paths."""
    # These are the plugin's standard sidecar roots. Copying them in full also
    # covers tokenized texture paths such as UDIM patterns that cannot be
    # resolved to one filename by this lightweight scanner.
    for directory_name in ("textures", "assets"):
        source_directory = root.parent / directory_name
        if source_directory.is_dir():
            shutil.copytree(
                source_directory,
                bundle / directory_name,
                dirs_exist_ok=True,
            )

    queue = [(root.resolve(), Path("."))]
    inspected = set()
    while queue:
        source_layer, destination_relative = queue.pop(0)
        key = (source_layer, destination_relative.as_posix())
        if key in inspected:
            continue
        inspected.add(key)

        text = _load_usd_text(source_layer)
        for match in ASSET_RE.finditer(text):
            authored = match.group("asset").strip()
            if not authored or _is_absolute_asset(authored):
                continue
            filesystem_authored = authored.split("[", 1)[0]
            relative = Path(filesystem_authored)
            if not filesystem_authored or ".." in relative.parts:
                continue
            dependency = (source_layer.parent / relative).resolve()
            if not dependency.is_file():
                # usdchecker/realitytool will report unresolved or tokenized
                # paths; never invent a file or turn this into a false pass.
                continue

            destination = destination_relative.parent / relative
            destination_path = bundle / destination
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            if not destination_path.exists():
                shutil.copy2(dependency, destination_path)

            if (
                dependency.suffix.lower() in {".usd", ".usda", ".usdc"}
                and "[" not in authored
            ):
                queue.append((dependency, destination))


def _rkassets_scene_files(bundle: Path) -> List[Path]:
    results = []
    for extension in sorted(USD_EXTENSIONS):
        results.extend(bundle.rglob(f"*{extension}"))
    return sorted(path for path in set(results) if path.is_file())


def _find_xcrun_tool(name: str) -> Optional[str]:
    xcrun = shutil.which("xcrun")
    if not xcrun:
        return None
    try:
        result = _run_external_tool(
            [xcrun, "--find", name],
            timeout=15,
        )
    except (OSError, RuntimeError):
        return None
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        return None
    candidate = Path(path)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate.resolve())
    return None


def _resolve_usd_tool(name: str) -> Optional[str]:
    """Resolve Apple USD tools through xcrun on macOS.

    This honors DEVELOPER_DIR and avoids accidentally validating an Apple-27
    export with an unrelated pip/OpenUSD executable earlier on PATH.
    """
    if sys.platform == "darwin":
        return _find_xcrun_tool(name)
    return shutil.which(name)


@lru_cache(maxsize=16)
def _tool_version(tool: str) -> str:
    try:
        result = _run_external_tool([tool, "--version"], timeout=15)
    except (OSError, RuntimeError) as exc:
        return f"unknown ({exc})"
    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    return (output.splitlines()[0] if output else "unknown")[:256]


@lru_cache(maxsize=1)
def _xcode_version() -> str:
    xcrun = shutil.which("xcrun")
    if not xcrun:
        return "unavailable"
    try:
        result = _run_external_tool([xcrun, "xcodebuild", "-version"], timeout=30)
    except (OSError, RuntimeError) as exc:
        return f"unknown ({exc})"
    output = " ".join(
        part.strip()
        for part in (result.stdout, result.stderr)
        if part and part.strip()
    )
    return output[:512] or "unknown"


def _run_external_tool(command: List[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run a bounded command in a separately terminable process group."""
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired as cleanup_exc:
            stdout = _coerce_output_text(cleanup_exc.stdout)
            stderr = _coerce_output_text(cleanup_exc.stderr)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        captured = "\n".join(
            part for part in ((stdout or "").strip(), (stderr or "").strip()) if part
        )
        raise ExternalToolTimeout(command, timeout, captured) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _coerce_output_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _terminate_process_group(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    else:
        if process.poll() is not None:
            return
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        # The direct process may already have exited while a descendant still
        # owns the group's pipes, so always attempt the group-wide hard stop.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    elif process.poll() is None:
        process.kill()


def _compile_rkassets(
    bundle: Path,
    args,
    *,
    output_stem: Optional[str] = None,
) -> Dict[str, object]:
    persistent_output = bool(getattr(args, "compiled_output_dir", None))
    lock_path: Optional[Path] = None
    temporary_output: Optional[tempfile.TemporaryDirectory] = None
    try:
        output_reality, lock_path, temporary_output = _reserve_compile_output(
            bundle,
            args,
            output_stem=output_stem,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Refused compiled output path: {exc}",
            "exit_code": -1,
            "command": [],
            "output_reality": None,
            "output_persisted": persistent_output,
            "timed_out": False,
        }
    cmd: List[str] = []
    resolved_realitytool: Optional[str] = None
    try:
        xcrun = shutil.which("xcrun")
        resolved_realitytool = _find_xcrun_tool("realitytool") if xcrun else None
        if not xcrun or not resolved_realitytool:
            return {
                "ok": False,
                "stdout": "",
                "stderr": "realitytool not found via xcrun/DEVELOPER_DIR",
                "exit_code": -1,
                "command": ["xcrun", "realitytool", "compile"],
                "tool_path": resolved_realitytool or "unavailable",
                "tool_version": _xcode_version(),
                "output_reality": str(output_reality) if persistent_output else None,
                "output_persisted": persistent_output,
                "timed_out": False,
            }
        cmd = [
            xcrun, "realitytool", "compile",
            "--output-reality", str(output_reality),
            "--platform", args.platform,
            "--deployment-target", args.deployment_target,
        ]
        if getattr(args, "use_metal", False):
            cmd.extend(["--use-metal", "true"])
        cmd.append(str(bundle))

        compile_started_ns = time.time_ns()
        proc = _run_external_tool(
            cmd,
            timeout=getattr(args, "tool_timeout", DEFAULT_TOOL_TIMEOUT_SECONDS),
        )
        output_is_regular = output_reality.is_file() and not output_reality.is_symlink()
        output_size = 0
        output_mtime_ns = None
        output_sha256 = None
        output_is_fresh = False
        if output_is_regular:
            stat_result = output_reality.stat()
            output_size = stat_result.st_size
            output_mtime_ns = stat_result.st_mtime_ns
            # APFS and ext4 are nanosecond-resolution; tolerate coarse/rounded
            # timestamps so a genuinely new file on another supported runner
            # is not rejected merely because its mtime rounds to a prior tick.
            output_is_fresh = output_mtime_ns >= compile_started_ns - 2_000_000_000
            if output_size > 0:
                output_sha256 = hashlib.sha256(output_reality.read_bytes()).hexdigest()

        succeeded = (
            proc.returncode == 0
            and output_is_regular
            and output_size > 0
            and output_is_fresh
            and bool(output_sha256)
        )
        stderr = proc.stderr.strip()
        validation_error = None
        if proc.returncode == 0 and not output_is_regular:
            validation_error = "realitytool reported success but created no fresh regular .reality output"
        elif proc.returncode == 0 and output_size == 0:
            validation_error = "realitytool reported success but created an empty .reality output"
        elif proc.returncode == 0 and not output_is_fresh:
            validation_error = "realitytool reported success but the .reality output is not fresh"
        if validation_error:
            stderr = "\n".join(
                part for part in (stderr, validation_error) if part
            )
        if persistent_output and not succeeded:
            output_reality.unlink(missing_ok=True)
        return {
            "ok": succeeded,
            "stdout": proc.stdout.strip(),
            "stderr": stderr,
            "exit_code": proc.returncode,
            "command": cmd,
            "tool_path": resolved_realitytool,
            "tool_version": _xcode_version(),
            "output_reality": str(output_reality) if persistent_output and succeeded else None,
            "output_persisted": persistent_output and succeeded,
            "output_size": output_size,
            "output_mtime_ns": output_mtime_ns,
            "output_sha256": output_sha256,
            "timed_out": False,
        }
    except ExternalToolTimeout as exc:
        if persistent_output:
            output_reality.unlink(missing_ok=True)
        return {
            "ok": False,
            "stdout": exc.output,
            "stderr": str(exc),
            "exit_code": 124,
            "command": cmd,
            "tool_path": resolved_realitytool,
            "tool_version": _xcode_version(),
            "output_reality": None,
            "output_persisted": False,
            "timed_out": True,
        }
    except (OSError, RuntimeError) as exc:
        if persistent_output:
            output_reality.unlink(missing_ok=True)
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"realitytool failed to start via xcrun: {exc}",
            "exit_code": -1,
            "command": cmd,
            "tool_path": resolved_realitytool,
            "tool_version": _xcode_version(),
            "output_reality": None,
            "output_persisted": False,
            "timed_out": False,
        }
    finally:
        _release_compile_lock(lock_path)
        if temporary_output is not None:
            temporary_output.cleanup()


def _reserve_compile_output(
    bundle: Path,
    args,
    *,
    output_stem: Optional[str],
) -> tuple[Path, Optional[Path], Optional[tempfile.TemporaryDirectory]]:
    configured_directory = getattr(args, "compiled_output_dir", None)
    if not configured_directory:
        # Never place an implicit compiler output beside a user's .rkassets
        # bundle. A pre-existing sibling could be overwritten or mistaken for
        # fresh output when realitytool exits zero without writing anything.
        temporary = tempfile.TemporaryDirectory(prefix="usdstage-realitytool-")
        return Path(temporary.name) / "compiled.reality", None, temporary

    output_directory = Path(configured_directory).expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    if not output_directory.is_dir():
        raise RuntimeError(f"Not a directory: {output_directory}")

    platform = str(getattr(args, "platform", ""))
    allowed_platforms = {
        "appletvos",
        "appletvsimulator",
        "iphoneos",
        "iphonesimulator",
        "macosx",
        "xros",
        "xrsimulator",
    }
    if platform not in allowed_platforms:
        raise ValueError(f"Unsafe or unsupported platform component: {platform!r}")
    deployment_target = str(getattr(args, "deployment_target", ""))
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", deployment_target):
        raise ValueError(f"Unsafe deployment-target component: {deployment_target!r}")

    safe_stem = _safe_output_stem(output_stem or bundle.stem)
    filename = f"{safe_stem}-{platform}-{deployment_target}.reality"
    output_path = (output_directory / filename).resolve()
    if output_path.parent != output_directory:
        raise ValueError(f"Compiled output escapes destination: {output_path}")

    normalized_key = unicodedata.normalize("NFC", filename).casefold()
    reservations = getattr(args, "_compiled_output_reservations", None)
    if reservations is None:
        reservations = set()
        setattr(args, "_compiled_output_reservations", reservations)
    if normalized_key in reservations:
        raise RuntimeError(f"Duplicate compiled output name in this run: {filename}")
    if output_path.exists() or output_path.is_symlink():
        raise RuntimeError(f"Compiled output already exists: {output_path}")

    lock_path = output_directory / f".{filename}.lock"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"Compiled output is already reserved: {output_path}") from exc
    else:
        os.close(descriptor)
    if output_path.exists() or output_path.is_symlink():
        lock_path.unlink(missing_ok=True)
        raise RuntimeError(f"Compiled output appeared during reservation: {output_path}")
    reservations.add(normalized_key)
    return output_path, lock_path, None


def _safe_output_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-_")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "asset"
    if cleaned != normalized or len(cleaned) > 100:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{cleaned[:100]}-{digest}"
    return cleaned


def _release_compile_lock(lock_path: Optional[Path]) -> None:
    if not lock_path:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate exported USD, USDZ, or rkassets.")
    parser.add_argument("--input", required=True, help="USD file, rkassets bundle, or directory to scan.")
    parser.add_argument("--manifest", default="Plugin/manifest/rk_nodes_manifest.json", help="Manifest JSON path.")
    parser.add_argument("--output", default=None, help="Optional JSON report output.")
    parser.add_argument("--no-usdchecker", action="store_true", help="Skip usdchecker.")
    parser.add_argument(
        "--arkit",
        action="store_true",
        help=(
            "Also apply usdchecker's legacy --arkit restrictions to non-USDZ "
            "inputs. USDZ always receives Apple-strict package validation."
        ),
    )
    parser.add_argument("--no-lint", action="store_true", help="Skip manifest/asset lint.")
    parser.add_argument("--no-compile", action="store_true", help="Skip realitytool compile.")
    parser.add_argument(
        "--platform",
        default="xros",
        choices=(
            "appletvos",
            "appletvsimulator",
            "iphoneos",
            "iphonesimulator",
            "macosx",
            "xros",
            "xrsimulator",
        ),
        help="realitytool platform.",
    )
    parser.add_argument("--deployment-target", default="27.0", help="realitytool deployment target.")
    parser.add_argument(
        "--use-metal",
        action="store_true",
        help="Compile with realitytool --use-metal true (Apple OS 27 validation lane).",
    )
    parser.add_argument(
        "--tool-timeout",
        type=float,
        default=DEFAULT_TOOL_TIMEOUT_SECONDS,
        help="Maximum seconds for each usdchecker/usdcat/realitytool process.",
    )
    parser.add_argument(
        "--compiled-output-dir",
        default=None,
        help=(
            "Persist deterministic <asset>-<platform>-<target>.reality outputs "
            "for subsequent runtime-load validation. Existing/colliding outputs are refused."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
