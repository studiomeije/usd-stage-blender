"""USDZ packaging and compliance checks.

USDZ is not an arbitrary ZIP file.  Every member must be stored without
compression and its payload must begin on a 64-byte boundary.  The root USD
layer must also be the first member.  Keep those constraints here so exports
remain valid even when Apple's ``usdzip`` utility is unavailable.
"""

from __future__ import annotations

import os
import re
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Tuple

from .staging_namespace import GENERATION_MARKER_DIRECTORY

_USD_EXTENSIONS = frozenset({".usd", ".usda", ".usdc"})
USDZ_ALLOWED_MEMBER_EXTENSIONS = frozenset(
    {
        ".usd",
        ".usda",
        ".usdc",
        ".usdz",
        ".png",
        ".jpg",
        ".jpeg",
        ".exr",
        ".avif",
        ".m4a",
        ".mp3",
        ".wav",
    }
)
_USDZ_ALIGNMENT = 64
# Private/unknown ZIP extra fields are explicitly skippable.  This field is
# used only as padding in each local header.
_ALIGNMENT_EXTRA_FIELD_ID = 0x1986
_PACKAGER_TIMEOUT_SECONDS = 600
_CHECKER_TIMEOUT_SECONDS = 300
_USDCHECKER_ARKIT_OPTION = re.compile(r"(?m)^\s*--arkit(?:\s|,|$)")


class ExternalToolTimeout(RuntimeError):
    """Raised after an external packager/checker process group is stopped."""

    def __init__(self, command: List[str], timeout: float, output: str = ""):
        self.command = command
        self.timeout = timeout
        self.output = output
        message = f"command timed out after {timeout:g}s: {' '.join(command)}"
        if output:
            message += f"\n{output}"
        super().__init__(message)


def create_usdz(usd_path: str, output_path: str, settings, context, diagnostics=None):
    """Create and validate a dependency-closed USDZ package atomically."""
    root_layer = Path(usd_path).resolve()
    if not root_layer.is_file():
        raise RuntimeError(f"USDZ root layer does not exist: {root_layer}")
    if root_layer.suffix.lower() not in _USD_EXTENSIONS:
        raise RuntimeError(f"USDZ root layer must be USD, USDA, or USDC: {root_layer}")
    output_file = _validated_output_path(output_path)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = _temporary_usdz_path(output_file)
    packager = "python_aligned_zip"
    usdzip_path: Optional[str] = None

    try:
        # Import preferences only for the Blender-facing entry point.  The
        # structural packager/validator remain usable by release tooling and
        # unit tests without importing bpy.
        from .. import prefs as addon_prefs

        prefs = addon_prefs.get_preferences(context)
        configured_tool = (
            prefs.usdzip_path
            if prefs and hasattr(prefs, "usdzip_path")
            else None
        )
        if configured_tool and _is_executable_file(configured_tool):
            usdzip_path = str(Path(configured_tool).resolve())
            create_usdz_with_tool(str(root_layer), str(temporary_output), usdzip_path)
            packager = "usdzip_asset"
        else:
            create_usdz_python(
                str(root_layer),
                str(temporary_output),
                settings,
                diagnostics,
            )

        checker = _find_usdchecker(usdzip_path)
        checker_supports_arkit: Optional[bool] = None
        if checker:
            try:
                checker_supports_arkit = _usdchecker_supports_arkit(checker)
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(
                    "USDZ compliance validation failed:\n"
                    f"- Could not determine whether usdchecker supports the "
                    f"required Apple profile: {exc}"
                ) from exc
        valid, errors = validate_usdz_details(
            str(temporary_output),
            usdchecker_path=checker,
            usdchecker_arkit=checker_supports_arkit,
        )
        if not valid:
            details = "\n".join(f"- {error}" for error in errors)
            raise RuntimeError(f"USDZ compliance validation failed:\n{details}")

        os.replace(temporary_output, output_file)
        if diagnostics:
            diagnostics.add_generated_file(
                "usdz",
                str(output_file),
                packager=packager,
                compliance=(
                    "usdchecker_arkit_strict"
                    if checker_supports_arkit
                    else "usdchecker_strict_fallback"
                    if checker
                    else "structural_only"
                ),
                packager_path=usdzip_path or "builtin",
                packager_version=_tool_version(usdzip_path) if usdzip_path else "builtin",
                packager_command=(
                    [
                        usdzip_path,
                        "--asset",
                        str(root_layer),
                        "--checkCompliance",
                        str(temporary_output),
                    ]
                    if usdzip_path
                    else [
                        "builtin_aligned_usdz",
                        str(root_layer),
                        str(temporary_output),
                    ]
                ),
                checker_path=checker or "unavailable",
                checker_version=_tool_version(checker) if checker else "unavailable",
                checker_command=(
                    _usdchecker_command(
                        checker,
                        temporary_output,
                        arkit=bool(checker_supports_arkit),
                    )
                    if checker
                    else []
                ),
            )
            if checker and checker_supports_arkit is False:
                diagnostics.add_warning(
                    "usdchecker ran with --strict but did not advertise --arkit; "
                    "generic USD compliance passed, but Apple RealityKit-specific "
                    "rules could not be checked."
                )
            elif not checker:
                diagnostics.add_info(
                    "usdchecker was not available; USDZ ZIP structure was validated, "
                    "but semantic USD compliance was not checked."
                )
    finally:
        try:
            temporary_output.unlink(missing_ok=True)
        except OSError:
            pass

    # Preserve the staging tree when packaging or validation fails so the
    # support bundle has the source material needed to diagnose the failure.
    _cleanup_usdz_staging(str(root_layer), diagnostics)


def create_usdz_with_tool(usd_path: str, output_path: str, usdzip_path: str):
    """Create USDZ with usdzip's dependency-isolating asset mode.

    Passing the root file as a positional member only archives that one file.
    ``--asset`` asks current OpenUSD/usdzip builds to resolve and copy the full
    dependency closure while retaining the authored composition structure.
    """
    command = [usdzip_path, "--asset", usd_path, "--checkCompliance", output_path]
    try:
        result = _run_external_tool(command, timeout=_PACKAGER_TIMEOUT_SECONDS)
    except ExternalToolTimeout:
        raise
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"Failed to run usdzip: {exc}") from exc

    if result.returncode != 0:
        output = "\n".join(
            part for part in (result.stdout.strip(), result.stderr.strip()) if part
        )
        raise RuntimeError(
            f"usdzip failed with exit code {result.returncode}"
            + (f":\n{output}" if output else "")
        )
    if not Path(output_path).is_file():
        raise RuntimeError("usdzip reported success but did not create an output package")


def create_usdz_python(usd_path: str, output_path: str, settings=None, diagnostics=None):
    """Create a standards-compliant stored USDZ without external utilities."""
    root_layer = Path(usd_path).resolve()
    output_file = Path(output_path).resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    members = list(_iter_package_files(root_layer, output_file))
    with zipfile.ZipFile(
        output_file,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        for source, archive_name in members:
            _write_aligned_member(archive, source, archive_name)

    valid, errors = validate_usdz_details(str(output_file))
    if not valid:
        output_file.unlink(missing_ok=True)
        details = "\n".join(f"- {error}" for error in errors)
        raise RuntimeError(f"Python USDZ packaging failed structural validation:\n{details}")

    if diagnostics:
        diagnostics.add_info(
            "USDZ packaged with the built-in aligned, uncompressed packager."
        )


def validate_usdz_details(
    usdz_path: str,
    *,
    usdchecker_path: Optional[str] = None,
    usdchecker_arkit: Optional[bool] = None,
) -> Tuple[bool, List[str]]:
    """Validate USDZ constraints and optionally run strict Apple validation.

    When ``usdchecker`` advertises ``--arkit``, Apple-profile validation is
    mandatory.  A checker that runs successfully but does not advertise the
    option receives the generic ``--strict`` fallback.  Capability-probe
    failures are errors rather than an implicit downgrade.
    """
    path = Path(usdz_path)
    errors: List[str] = []
    if not path.is_file():
        return False, [f"Package does not exist: {path}"]

    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            if not members:
                errors.append("Package is empty")
            else:
                first = members[0]
                first_path = PurePosixPath(first.filename)
                if (
                    len(first_path.parts) != 1
                    or first_path.suffix.lower() not in _USD_EXTENSIONS
                ):
                    errors.append("The first package member is not a root USD layer")

            seen_names = set()
            with path.open("rb") as raw_archive:
                for member in members:
                    archive_name = member.filename
                    archive_name_key = unicodedata.normalize(
                        "NFC", archive_name
                    ).casefold()
                    if archive_name_key in seen_names:
                        errors.append(
                            f"Case/Unicode-colliding package member: {archive_name}"
                        )
                    seen_names.add(archive_name_key)

                    if _is_internal_package_member(PurePosixPath(archive_name).parts):
                        errors.append(
                            "Internal USD Stage for Blender metadata in package: "
                            f"{archive_name}"
                        )
                    if not _is_supported_package_member(archive_name):
                        errors.append(
                            f"Unsupported USDZ package member type: {archive_name}"
                        )
                    if not _is_safe_archive_name(archive_name):
                        errors.append(f"Unsafe package member path: {archive_name}")
                    if member.is_dir():
                        errors.append(f"USDZ must not contain directory entries: {archive_name}")
                    if member.compress_type != zipfile.ZIP_STORED:
                        errors.append(f"Compressed package member: {archive_name}")
                    if member.flag_bits & 0x1:
                        errors.append(f"Encrypted package member: {archive_name}")

                    try:
                        data_offset = _member_data_offset(raw_archive, member)
                    except (OSError, ValueError, struct.error) as exc:
                        errors.append(f"Could not inspect {archive_name}: {exc}")
                        continue
                    if data_offset % _USDZ_ALIGNMENT:
                        errors.append(
                            f"Misaligned package member {archive_name}: "
                            f"payload offset {data_offset}"
                        )

            bad_member = archive.testzip()
            if bad_member:
                errors.append(f"CRC check failed for package member: {bad_member}")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        errors.append(f"Invalid ZIP archive: {exc}")

    if not errors and usdchecker_path:
        errors.extend(
            _strict_usdchecker_errors(
                path,
                usdchecker_path,
                arkit=usdchecker_arkit,
            )
        )

    return not errors, errors


def _iter_package_files(
    root_layer: Path,
    output_file: Path,
) -> Iterable[Tuple[Path, str]]:
    """Yield the root first, followed by every staged sidecar file."""
    staging_root = root_layer.parent
    yield root_layer, root_layer.name

    output_resolved = output_file.resolve()
    root_resolved = root_layer.resolve()
    for candidate in sorted(staging_root.rglob("*")):
        if candidate.is_symlink():
            raise RuntimeError(f"Refusing symlink in USDZ staging tree: {candidate}")
        if not candidate.is_file():
            continue
        candidate_resolved = candidate.resolve()
        if candidate_resolved in {root_resolved, output_resolved}:
            continue
        relative = candidate.relative_to(staging_root)
        if _is_internal_package_member(relative.parts):
            continue
        archive_name = relative.as_posix()
        if not _is_safe_archive_name(archive_name):
            raise RuntimeError(f"Refusing unsafe USDZ member path: {archive_name}")
        if not _is_supported_package_member(archive_name):
            raise RuntimeError(
                f"Unsupported USDZ staging member type: {archive_name}"
            )
        yield candidate, archive_name


def _write_aligned_member(
    archive: zipfile.ZipFile,
    source: Path,
    archive_name: str,
) -> None:
    """Write one stored member whose file payload starts on a 64-byte boundary."""
    if archive.fp is None:
        raise RuntimeError("USDZ archive is not open")

    encoded_name = archive_name.encode("utf-8")
    local_header_offset = archive.fp.tell()
    # ZIP local header (30 bytes) + UTF-8 name + custom extra-field header.
    padding_length = -(
        local_header_offset + 30 + len(encoded_name) + 4
    ) % _USDZ_ALIGNMENT
    padding = struct.pack(
        "<HH",
        _ALIGNMENT_EXTRA_FIELD_ID,
        padding_length,
    ) + (b"\0" * padding_length)

    stat_result = source.stat()
    info = zipfile.ZipInfo.from_file(source, arcname=archive_name)
    info.compress_type = zipfile.ZIP_STORED
    info.flag_bits |= 0x800  # UTF-8 filenames; harmless for ASCII names.
    info.extra = padding
    info.external_attr = (stat_result.st_mode & 0xFFFF) << 16

    with source.open("rb") as source_file, archive.open(info, "w") as member_file:
        shutil.copyfileobj(source_file, member_file, length=1024 * 1024)


def _member_data_offset(raw_archive, member: zipfile.ZipInfo) -> int:
    raw_archive.seek(member.header_offset)
    header = raw_archive.read(30)
    if len(header) != 30:
        raise ValueError("truncated local file header")
    signature, = struct.unpack_from("<I", header, 0)
    if signature != 0x04034B50:
        raise ValueError("invalid local file header signature")
    filename_length, extra_length = struct.unpack_from("<HH", header, 26)
    return member.header_offset + 30 + filename_length + extra_length


def _strict_usdchecker_errors(
    path: Path,
    checker: str,
    *,
    arkit: Optional[bool] = None,
) -> List[str]:
    if arkit is None:
        try:
            arkit = _usdchecker_supports_arkit(checker)
        except (OSError, RuntimeError) as exc:
            return [
                "Could not determine whether usdchecker supports the required "
                f"Apple profile: {exc}"
            ]

    command = _usdchecker_command(checker, path, arkit=arkit)
    try:
        result = _run_external_tool(
            command,
            timeout=_CHECKER_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError) as exc:
        return [f"Failed to run usdchecker: {exc}"]
    if result.returncode == 0:
        return []
    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    profile = " --arkit --strict" if arkit else " --strict"
    return [
        f"usdchecker{profile} failed with exit code {result.returncode}"
        + (f": {output}" if output else "")
    ]


def _usdchecker_supports_arkit(checker: str) -> bool:
    """Return Apple-profile support, failing if capability cannot be proven.

    A successful ``--help`` response without an exact ``--arkit`` option is a
    definitive unsupported result and permits the generic strict fallback.
    Tool launch errors, timeouts, and nonzero help results are ambiguous, so
    callers must fail closed instead of silently weakening validation.
    """
    command = [checker, "--help"]
    try:
        result = _run_external_tool(command, timeout=15)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"usdchecker capability probe failed: {exc}") from exc

    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    if result.returncode != 0:
        raise RuntimeError(
            "usdchecker capability probe failed with exit code "
            f"{result.returncode}" + (f": {output}" if output else "")
        )
    return _USDCHECKER_ARKIT_OPTION.search(output) is not None


def _usdchecker_command(
    checker: str,
    path: Path,
    *,
    arkit: bool,
) -> List[str]:
    command = [checker]
    if arkit:
        command.append("--arkit")
    command.extend(["--strict", str(path)])
    return command


def _find_usdchecker(usdzip_path: Optional[str]) -> Optional[str]:
    if usdzip_path:
        sibling = Path(usdzip_path).with_name("usdchecker")
        if _is_executable_file(sibling):
            return str(sibling)
    if sys.platform == "darwin":
        checker = _find_xcrun_tool("usdchecker")
    else:
        checker = shutil.which("usdchecker")
    return str(Path(checker).resolve()) if checker else None


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
    resolved = result.stdout.strip()
    if result.returncode != 0 or not resolved or not _is_executable_file(resolved):
        return None
    return resolved


@lru_cache(maxsize=8)
def _tool_version(path: Optional[str]) -> str:
    if not path:
        return "unavailable"
    try:
        result = _run_external_tool([path, "--version"], timeout=15)
    except (OSError, RuntimeError) as exc:
        return f"unknown ({exc})"
    output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    first_line = output.splitlines()[0] if output else "unknown"
    return first_line[:256]


def _run_external_tool(command: List[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run a bounded tool in its own process group.

    Background exports can be force-stopped by the worker watchdog. Keeping
    Apple tools in a separate group lets this caller terminate their complete
    process tree instead of leaving usdzip/usdchecker descendants behind.
    """
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


def _temporary_usdz_path(output_file: Path) -> Path:
    descriptor, path = tempfile.mkstemp(
        prefix=f".{output_file.stem}.",
        suffix=".tmp.usdz",
        dir=output_file.parent,
    )
    os.close(descriptor)
    temporary = Path(path)
    temporary.unlink()
    return temporary


def _validated_output_path(output_path: str) -> Path:
    """Resolve a safe output path without following a final-component link.

    ``Path.resolve()`` follows the requested destination itself.  Doing that
    before checking its type would turn ``scene.usdz -> unrelated.dat`` into
    permission to replace the unrelated target.  Inspect the lexical final
    component first, resolve only its parent, then repeat the check at the
    resolved location to cover parent-directory aliases.
    """
    requested = Path(output_path).expanduser()
    if not requested.is_absolute():
        requested = Path.cwd() / requested
    requested = Path(os.path.abspath(requested))
    _reject_unsafe_output_destination(requested)

    requested.parent.mkdir(parents=True, exist_ok=True)
    resolved = requested.parent.resolve(strict=True) / requested.name
    _reject_unsafe_output_destination(resolved)
    return resolved


def _reject_unsafe_output_destination(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing USDZ output symlink: {path}")
    if path.exists() and not path.is_file():
        raise RuntimeError(f"USDZ output must be a regular file: {path}")


def _is_executable_file(path) -> bool:
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.X_OK)


def _is_safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or name.endswith("/"):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    return True


def _is_internal_package_member(parts: Iterable[str]) -> bool:
    """Return whether an archive path belongs only to exporter bookkeeping."""
    return any(
        part == GENERATION_MARKER_DIRECTORY
        or part.startswith(".usdstage_")
        for part in parts
    )


def _is_supported_package_member(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in USDZ_ALLOWED_MEMBER_EXTENSIONS


def _cleanup_usdz_staging(usd_path: str, diagnostics=None) -> None:
    """Remove the temporary USDZ staging directory after successful packaging."""
    staging_dir = Path(usd_path).resolve().parent
    if staging_dir.name != ".usdstage_temp":
        if staging_dir.parent.name != ".usdstage_temp":
            return

    target_dir = staging_dir
    temp_root = staging_dir if staging_dir.name == ".usdstage_temp" else staging_dir.parent

    try:
        shutil.rmtree(target_dir)
    except Exception as exc:
        if diagnostics:
            diagnostics.add_warning(
                f"Failed to remove USDZ staging directory '{target_dir}': {exc}"
            )
        return

    if temp_root.name == ".usdstage_temp":
        try:
            temp_root.rmdir()
        except OSError:
            pass
