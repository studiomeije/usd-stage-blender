"""Blender-independent ownership manifest validation for exported sidecars.

The USD exporter records the exact ``textures/`` and ``assets/`` files owned
by each unpacked output.  Consumers such as support-bundle creation must use
that manifest instead of recursively walking the shared output directories.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


SIDECAR_MANIFEST_DIRECTORY = ".usdstage_sidecars"
SIDECAR_MANIFEST_SCHEMA_VERSION = 1
OWNED_SIDECAR_DIRECTORIES = frozenset({"textures", "assets"})


class SidecarManifestError(RuntimeError):
    """Raised when an output's sidecar ownership contract is unsafe."""


@dataclass(frozen=True)
class SidecarManifest:
    """A validated ownership manifest tied to one exact output filename."""

    path: Path
    output: str
    sidecars: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class OwnedSidecar:
    """A validated sidecar file and its canonical manifest-relative path."""

    path: Path
    relative_path: PurePosixPath


def output_sidecar_manifest_path(output_path: str | Path) -> Path:
    """Return the ownership manifest uniquely tied to ``output_path``."""
    output = Path(output_path)
    return (
        output.parent
        / SIDECAR_MANIFEST_DIRECTORY
        / f"{canonical_output_identity(output)}.json"
    )


def canonical_output_identity(output_path: str | Path) -> str:
    """Return one macOS-safe identity for an output's complete filename."""
    name = Path(output_path).name
    return unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", name).casefold(),
    )


def validate_unambiguous_output_identity(output_path: str | Path) -> None:
    """Reject distinct sibling names that collapse to the same Apple identity."""
    output = Path(output_path)
    parent = output.parent
    if not parent.is_dir():
        return
    identity = canonical_output_identity(output)
    for sibling in parent.iterdir():
        if sibling.name == output.name:
            continue
        if canonical_output_identity(sibling) != identity:
            continue
        try:
            if output.exists() and sibling.samefile(output):
                # A case-insensitive filesystem may return the stored spelling
                # even when the caller used another alias for the same entry.
                continue
        except OSError:
            pass
        raise SidecarManifestError(
            "Ambiguous output filenames share one macOS-normalized identity: "
            f"'{output.name}' and '{sibling.name}'"
        )


def validate_manifest_destination(output_path: str | Path) -> Path:
    """Validate and return the manifest location without following symlinks."""
    manifest_path = output_sidecar_manifest_path(output_path)
    manifest_directory = manifest_path.parent

    if manifest_directory.is_symlink():
        raise SidecarManifestError(
            f"Refusing symlinked sidecar manifest directory: {manifest_directory}"
        )
    if manifest_directory.exists() and not manifest_directory.is_dir():
        raise SidecarManifestError(
            f"Sidecar manifest path is not a directory: {manifest_directory}"
        )
    if manifest_path.is_symlink():
        raise SidecarManifestError(
            f"Refusing symlinked sidecar manifest: {manifest_path}"
        )
    if manifest_path.exists() and not manifest_path.is_file():
        raise SidecarManifestError(
            f"Sidecar manifest path is not a file: {manifest_path}"
        )
    return manifest_path


def validate_sidecar_relative_path(value: object) -> PurePosixPath:
    """Parse one canonical, managed POSIX sidecar path or fail closed."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise SidecarManifestError(f"Invalid sidecar manifest entry: {value!r}")

    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or relative.parts[0] not in OWNED_SIDECAR_DIRECTORIES
        or relative.as_posix() != value
        or len(relative.parts) < 2
    ):
        raise SidecarManifestError(f"Unsafe sidecar manifest entry: {value!r}")
    return relative


def read_output_sidecar_manifest(
    output_path: str | Path,
) -> SidecarManifest | None:
    """Read a manifest strictly; absence means that the output owns no sidecars."""
    output = Path(output_path)
    validate_unambiguous_output_identity(output)
    manifest_path = validate_manifest_destination(output)
    if not manifest_path.exists():
        return None

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SidecarManifestError(
            f"Could not read sidecar ownership manifest '{manifest_path}': {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise SidecarManifestError(
            f"Invalid sidecar ownership manifest object: {manifest_path}"
        )
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != SIDECAR_MANIFEST_SCHEMA_VERSION
    ):
        raise SidecarManifestError(
            f"Unsupported sidecar ownership manifest schema: {manifest_path}"
        )
    output_identity = canonical_output_identity(output)
    if payload.get("output") != output_identity:
        raise SidecarManifestError(
            f"Sidecar ownership manifest targets another output: {manifest_path}"
        )
    raw_sidecars = payload.get("sidecars")
    if not isinstance(raw_sidecars, list):
        raise SidecarManifestError(
            f"Invalid sidecar ownership list: {manifest_path}"
        )

    sidecars = tuple(validate_sidecar_relative_path(entry) for entry in raw_sidecars)
    if len(set(sidecars)) != len(sidecars):
        raise SidecarManifestError(
            f"Duplicate sidecar ownership entry: {manifest_path}"
        )
    return SidecarManifest(
        path=manifest_path,
        output=output_identity,
        sidecars=sidecars,
    )


def validate_owned_sidecar_files(
    output_path: str | Path,
    manifest: SidecarManifest | None = None,
) -> tuple[OwnedSidecar, ...]:
    """Resolve every owned sidecar and reject missing, linked, or escaped paths."""
    output = Path(output_path)
    validate_unambiguous_output_identity(output)
    manifest = manifest if manifest is not None else read_output_sidecar_manifest(output)
    if manifest is None:
        return ()
    if (
        manifest.output != canonical_output_identity(output)
        or manifest.path != output_sidecar_manifest_path(output)
    ):
        raise SidecarManifestError("Sidecar manifest does not belong to this output")

    resolved_output_parent = output.parent.resolve()
    validated: list[OwnedSidecar] = []
    for relative in manifest.sidecars:
        root = output.parent / relative.parts[0]
        if root.is_symlink():
            raise SidecarManifestError(
                f"Refusing symlinked sidecar directory: {root}"
            )
        if not root.exists() or not root.is_dir():
            raise SidecarManifestError(
                f"Owned sidecar directory is missing or invalid: {root}"
            )

        current = root
        for part in relative.parts[1:-1]:
            current = current / part
            if current.is_symlink():
                raise SidecarManifestError(
                    f"Refusing symlinked sidecar directory: {current}"
                )
            if not current.exists() or not current.is_dir():
                raise SidecarManifestError(
                    f"Owned sidecar directory is missing or invalid: {current}"
                )

        sidecar_path = output.parent.joinpath(*relative.parts)
        if sidecar_path.is_symlink():
            raise SidecarManifestError(
                f"Refusing symlinked sidecar file: {sidecar_path}"
            )
        if not sidecar_path.exists() or not sidecar_path.is_file():
            raise SidecarManifestError(
                f"Owned sidecar file is missing or invalid: {sidecar_path}"
            )
        if os.lstat(sidecar_path).st_nlink != 1:
            raise SidecarManifestError(
                f"Refusing hard-linked sidecar file: {sidecar_path}"
            )

        resolved_root = root.resolve(strict=True)
        resolved_sidecar = sidecar_path.resolve(strict=True)
        if (
            not resolved_root.is_relative_to(resolved_output_parent)
            or not resolved_sidecar.is_relative_to(resolved_root)
        ):
            raise SidecarManifestError(
                f"Owned sidecar escapes its managed output directory: {sidecar_path}"
            )
        validated.append(
            OwnedSidecar(path=sidecar_path, relative_path=relative)
        )
    return tuple(validated)
