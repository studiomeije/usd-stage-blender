#!/usr/bin/env python3
"""Build and verify reproducible USD Stage for Blender extension archives.

The implementation uses only the Python standard library (3.11 or newer, for
``tomllib``) so the same command works on macOS and GitHub's Ubuntu runners.
Blender remains the authority for full extension-manifest validation.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


MANIFEST_ID = "usd_stage"
MANIFEST_NAME = "USD Stage for Blender"
ARCHIVE_BASENAME = "usd-stage-for-blender"
ARCHIVE_ROOT = "usd_stage"
DEFAULT_SOURCE_DATE_EPOCH = 315532800  # 1980-01-01 00:00:00 UTC
REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "id",
    "name",
    "tagline",
    "version",
    "maintainer",
    "type",
    "blender_version_min",
    "website",
    "permissions.files",
)
REQUIRED_PLUGIN_FILES = (
    "__init__.py",
    "__main__.py",
    "blender_manifest.toml",
    "assets/nodegroups.blend",
    "assets/icons/usd_stage.png",
    "core/package_bootstrap.py",
    "manifest/rk_nodes_manifest.json",
)
REQUIRED_LEGAL_FILES = (
    "LICENSE",
    "THIRD_PARTY_LICENSES/Apache-2.0.txt",
    "THIRD_PARTY_NOTICES.txt",
)
REQUIRED_ARCHIVE_FILES = REQUIRED_PLUGIN_FILES + REQUIRED_LEGAL_FILES
GPL_REQUIRED_MARKERS = (
    "GNU GENERAL PUBLIC LICENSE",
    "Version 3, 29 June 2007",
    "Copyright (C) 2007 Free Software Foundation, Inc.",
    "END OF TERMS AND CONDITIONS",
)
APPLE_NOTICE_REQUIRED_MARKERS = (
    "Copyright © 2024 Apple Inc.",
    "Permission is hereby granted, free of charge, to any person obtaining a copy",
    "The above copyright notice and this permission notice shall be included",
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
)
APACHE_REQUIRED_MARKERS = (
    "Apache License",
    "Version 2.0, January 2004",
    "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
    "END OF TERMS AND CONDITIONS",
)
MATERIALX_NOTICE_REQUIRED_MARKERS = (
    "Copyright Contributors to the MaterialX Project.",
    "SPDX-License-Identifier: Apache-2.0",
    "THIRD_PARTY_LICENSES/Apache-2.0.txt",
)
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
STABLE_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    "__pycache__",
}
EXCLUDED_FILE_NAMES = {".DS_Store"}
EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo"}


class ReleaseCheckError(RuntimeError):
    """A release invariant was not satisfied."""


def parse_manifest(manifest_path: Path) -> Dict[str, str]:
    """The manifest's string fields, with tables flattened to ``table.key``."""
    if not manifest_path.is_file():
        raise ReleaseCheckError(f"missing manifest: {manifest_path}")
    try:
        document = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseCheckError(f"invalid manifest: {exc}") from exc

    fields: Dict[str, str] = {}
    for key, value in document.items():
        if isinstance(value, str):
            fields[key] = value
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, str):
                    fields[f"{key}.{nested_key}"] = nested_value

    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in fields]
    if missing:
        raise ReleaseCheckError(
            "manifest is missing required scalar fields: " + ", ".join(missing)
        )
    return fields


def validate_manifest(
    fields: Mapping[str, str], source_dir: Path, expected_tag: Optional[str]
) -> str:
    if fields["schema_version"] != "1.0.0":
        raise ReleaseCheckError("manifest schema_version must be exactly 1.0.0")

    version = fields["version"]
    if not SEMVER.fullmatch(version):
        raise ReleaseCheckError(f"manifest version is not valid SemVer: {version!r}")

    if fields["id"] != MANIFEST_ID:
        raise ReleaseCheckError(f"manifest id must be exactly {MANIFEST_ID}")
    if fields["name"] != MANIFEST_NAME:
        raise ReleaseCheckError(f"manifest name must be exactly {MANIFEST_NAME}")
    # Blender's extension validator refuses a tagline over 64 characters or
    # one ending in punctuation, which only surfaced when CI installed it.
    tagline = fields["tagline"]
    if len(tagline) > 64 or tagline[-1:] in ".,;:!?":
        raise ReleaseCheckError(
            "manifest tagline must be at most 64 characters with no trailing punctuation"
        )
    if fields["type"] != "add-on":
        raise ReleaseCheckError("manifest type must be exactly add-on")
    if not (source_dir / "__init__.py").is_file():
        raise ReleaseCheckError("the extension has no Plugin/__init__.py")
    if fields["blender_version_min"] != "5.2.0":
        raise ReleaseCheckError(
            "manifest blender_version_min must be exactly 5.2.0"
        )

    maintainer = fields["maintainer"].strip()
    lowered_maintainer = maintainer.lower()
    if (
        not maintainer
        or "your name" in lowered_maintainer
        or "example.com" in lowered_maintainer
    ):
        raise ReleaseCheckError("manifest maintainer must not contain placeholder metadata")

    website = fields["website"].strip()
    if not website.startswith("https://") or len(website) <= len("https://"):
        raise ReleaseCheckError("manifest website must be a non-empty HTTPS URL")

    file_permission = fields["permissions.files"].strip()
    lowered_permission = file_permission.lower()
    if (
        len(file_permission) < 12
        or "todo" in lowered_permission
        or "placeholder" in lowered_permission
    ):
        raise ReleaseCheckError(
            "manifest permissions.files must explain why the add-on needs file access"
        )

    if expected_tag is not None:
        if not STABLE_SEMVER.fullmatch(expected_tag):
            raise ReleaseCheckError(
                "release tag must be a stable bare SemVer value such as 1.0.0"
            )
        if expected_tag != version:
            raise ReleaseCheckError(
                f"release tag {expected_tag!r} does not exactly match manifest version {version!r}"
            )
    return version


def _source_date_epoch() -> int:
    raw_value = os.environ.get("SOURCE_DATE_EPOCH", str(DEFAULT_SOURCE_DATE_EPOCH))
    try:
        epoch = int(raw_value, 10)
    except ValueError as exc:
        raise ReleaseCheckError("SOURCE_DATE_EPOCH must be an integer") from exc

    # ZIP timestamps have a 1980-2107 range and two-second precision.
    if epoch < DEFAULT_SOURCE_DATE_EPOCH:
        raise ReleaseCheckError("SOURCE_DATE_EPOCH cannot be earlier than 1980-01-01")
    utc = time.gmtime(epoch)
    if utc.tm_year > 2107:
        raise ReleaseCheckError("SOURCE_DATE_EPOCH cannot be later than 2107-12-31")
    return epoch - (epoch % 2)


def _zip_timestamp(epoch: int) -> Tuple[int, int, int, int, int, int]:
    utc = time.gmtime(epoch)
    return (utc.tm_year, utc.tm_mon, utc.tm_mday, utc.tm_hour, utc.tm_min, utc.tm_sec)


def _is_excluded(relative_path: Path) -> bool:
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative_path.parts):
        return True
    if relative_path.name in EXCLUDED_FILE_NAMES:
        return True
    return relative_path.suffix.lower() in EXCLUDED_FILE_SUFFIXES


def collect_source_entries(source_dir: Path) -> List[Tuple[str, Optional[Path], int]]:
    """Return sorted ``(archive path, source path, mode)`` entries."""

    if not source_dir.is_dir():
        raise ReleaseCheckError(f"missing plugin source directory: {source_dir}")
    missing_files = [
        relative for relative in REQUIRED_PLUGIN_FILES if not (source_dir / relative).is_file()
    ]
    if missing_files:
        raise ReleaseCheckError(
            "plugin source is missing required release files: " + ", ".join(missing_files)
        )

    repo_root = source_dir.parent
    legal_paths = {name: repo_root / name for name in REQUIRED_LEGAL_FILES}
    missing_legal_files = [
        name for name, legal_path in legal_paths.items() if not legal_path.is_file()
    ]
    if missing_legal_files:
        raise ReleaseCheckError(
            "repository is missing required release legal files: "
            + ", ".join(missing_legal_files)
        )
    _validate_legal_texts(legal_paths)

    entries: List[Tuple[str, Optional[Path], int]] = [
        (f"{ARCHIVE_ROOT}/", None, stat.S_IFDIR | 0o755)
    ]
    for legal_name, legal_path in legal_paths.items():
        entries.append(
            (
                PurePosixPath(ARCHIVE_ROOT, legal_name).as_posix(),
                legal_path,
                stat.S_IFREG | 0o644,
            )
        )
    for path in sorted(source_dir.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(source_dir)
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            raise ReleaseCheckError(f"symlinks are not allowed in release archives: {relative}")
        archive_path = PurePosixPath(ARCHIVE_ROOT, *relative.parts).as_posix()
        if path.is_dir():
            entries.append((archive_path + "/", None, stat.S_IFDIR | 0o755))
        elif path.is_file():
            entries.append((archive_path, path, stat.S_IFREG | 0o644))
        else:
            raise ReleaseCheckError(f"unsupported source entry: {relative}")

    entries.sort(key=lambda entry: entry[0])
    archive_names = {name for name, _, _ in entries}
    missing_archive_files = [
        relative
        for relative in REQUIRED_ARCHIVE_FILES
        if f"{ARCHIVE_ROOT}/{relative}" not in archive_names
    ]
    if missing_archive_files:
        raise ReleaseCheckError(
            "release archive is missing required files: "
            + ", ".join(missing_archive_files)
        )
    manifests = [
        name
        for name, _, _ in entries
        if PurePosixPath(name).name == "blender_manifest.toml"
    ]
    if manifests != [f"{ARCHIVE_ROOT}/blender_manifest.toml"]:
        raise ReleaseCheckError("release archive must contain exactly one root manifest")
    return entries


def _validate_legal_texts(legal_paths: Mapping[str, Path]) -> None:
    try:
        license_text = legal_paths["LICENSE"].read_text(encoding="utf-8")
        apache_text = legal_paths["THIRD_PARTY_LICENSES/Apache-2.0.txt"].read_text(
            encoding="utf-8"
        )
        third_party_text = legal_paths["THIRD_PARTY_NOTICES.txt"].read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError) as exc:
        raise ReleaseCheckError(f"could not read release legal files: {exc}") from exc

    missing_gpl_markers = [
        marker for marker in GPL_REQUIRED_MARKERS if marker not in license_text
    ]
    if missing_gpl_markers:
        raise ReleaseCheckError(
            "LICENSE is not the complete GNU GPL version 3 text; missing: "
            + ", ".join(repr(marker) for marker in missing_gpl_markers)
        )

    missing_apple_markers = [
        marker
        for marker in APPLE_NOTICE_REQUIRED_MARKERS
        if marker not in third_party_text
    ]
    if missing_apple_markers:
        raise ReleaseCheckError(
            "THIRD_PARTY_NOTICES.txt is missing required Apple MaterialX notice text: "
            + ", ".join(repr(marker) for marker in missing_apple_markers)
        )

    missing_apache_markers = [
        marker for marker in APACHE_REQUIRED_MARKERS if marker not in apache_text
    ]
    if missing_apache_markers:
        raise ReleaseCheckError(
            "THIRD_PARTY_LICENSES/Apache-2.0.txt is not the complete Apache 2.0 license text; missing: "
            + ", ".join(repr(marker) for marker in missing_apache_markers)
        )

    missing_materialx_markers = [
        marker
        for marker in MATERIALX_NOTICE_REQUIRED_MARKERS
        if marker not in third_party_text
    ]
    if missing_materialx_markers:
        raise ReleaseCheckError(
            "THIRD_PARTY_NOTICES.txt is missing required MaterialX notice text: "
            + ", ".join(repr(marker) for marker in missing_materialx_markers)
        )


def _zip_info(
    name: str, mode: int, timestamp: Tuple[int, int, int, int, int, int]
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=timestamp)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = mode << 16
    if stat.S_ISDIR(mode):
        info.external_attr |= 0x10
    return info


def build_archive(source_dir: Path, archive_path: Path, epoch: int) -> None:
    entries = collect_source_entries(source_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = _zip_timestamp(epoch)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".tmp", dir=str(archive_path.parent)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True
        ) as archive:
            for archive_name, source_path, mode in entries:
                data = b"" if source_path is None else source_path.read_bytes()
                archive.writestr(_zip_info(archive_name, mode, timestamp), data)
        temporary_path.chmod(0o644)
        os.replace(str(temporary_path), str(archive_path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(archive_path: Path, checksum_path: Path) -> str:
    digest = sha256_file(archive_path)
    contents = f"{digest}  {archive_path.name}\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{checksum_path.name}.", suffix=".tmp", dir=str(checksum_path.parent)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_bytes(contents.encode("ascii"))
        temporary_path.chmod(0o644)
        os.replace(str(temporary_path), str(checksum_path))
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return digest


def verify_archive(
    source_dir: Path,
    archive_path: Path,
    checksum_path: Path,
    manifest_fields: Mapping[str, str],
    epoch: int,
) -> str:
    expected_entries = collect_source_entries(source_dir)
    expected_names = [entry[0] for entry in expected_entries]
    expected_by_name = {entry[0]: entry for entry in expected_entries}
    expected_timestamp = _zip_timestamp(epoch)

    if not archive_path.is_file():
        raise ReleaseCheckError(f"archive was not created: {archive_path}")
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ReleaseCheckError(f"archive CRC check failed for {bad_member}")
            members = archive.infolist()
            names = [member.filename for member in members]
            if names != expected_names:
                raise ReleaseCheckError("archive content or entry order differs from Plugin/")
            if len(names) != len(set(names)):
                raise ReleaseCheckError("archive contains duplicate entries")

            for member in members:
                pure_name = PurePosixPath(member.filename)
                if pure_name.is_absolute() or ".." in pure_name.parts or "\\" in member.filename:
                    raise ReleaseCheckError(f"unsafe archive path: {member.filename}")
                _, source_path, expected_mode = expected_by_name[member.filename]
                actual_mode = (member.external_attr >> 16) & 0xFFFF
                if actual_mode != expected_mode:
                    raise ReleaseCheckError(
                        f"archive mode mismatch for {member.filename}: {actual_mode:o}"
                    )
                if member.date_time != expected_timestamp:
                    raise ReleaseCheckError(
                        f"archive timestamp mismatch for {member.filename}: {member.date_time}"
                    )
                if member.compress_type != zipfile.ZIP_DEFLATED:
                    raise ReleaseCheckError(
                        f"archive entry is not deflated: {member.filename}"
                    )
                expected_data = b"" if source_path is None else source_path.read_bytes()
                if archive.read(member) != expected_data:
                    raise ReleaseCheckError(f"archive content mismatch for {member.filename}")

            archived_manifest_name = f"{ARCHIVE_ROOT}/blender_manifest.toml"
            archived_manifest = archive.read(archived_manifest_name)
            if archived_manifest != (source_dir / "blender_manifest.toml").read_bytes():
                raise ReleaseCheckError("archived manifest differs from source manifest")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ReleaseCheckError(f"invalid release archive: {exc}") from exc

    expected_name = f"{ARCHIVE_BASENAME}-{manifest_fields['version']}.zip"
    if archive_path.name != expected_name:
        raise ReleaseCheckError(
            f"archive must be versioned as {expected_name}, got {archive_path.name}"
        )

    digest = sha256_file(archive_path)
    expected_checksum = f"{digest}  {archive_path.name}\n"
    try:
        checksum_contents = checksum_path.read_text(encoding="ascii")
    except FileNotFoundError as exc:
        raise ReleaseCheckError(f"checksum was not created: {checksum_path}") from exc
    if checksum_contents != expected_checksum:
        raise ReleaseCheckError("SHA-256 checksum file does not match the archive")
    return digest


def _automatic_expected_tag() -> Optional[str]:
    release_tag = os.environ.get("USDSTAGE_RELEASE_TAG")
    if release_tag:
        return release_tag
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        github_ref_name = os.environ.get("GITHUB_REF_NAME")
        if not github_ref_name:
            raise ReleaseCheckError(
                "GITHUB_REF_TYPE is tag but GITHUB_REF_NAME is missing"
            )
        return github_ref_name
    return None


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and verify the deterministic USD Stage for Blender release archive."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Artifact directory (default: <repo>/dist).",
    )
    parser.add_argument(
        "--expected-tag",
        default=None,
        help="Require this bare release tag to exactly match the manifest version.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Build twice and fail unless the resulting archives are byte-for-byte identical.",
    )
    return parser.parse_args(argv)


def run(argv: Optional[Sequence[str]] = None) -> Tuple[Path, Path, str]:
    args = _parse_args(argv)

    repo_root = args.repo_root.resolve()
    source_dir = repo_root / "Plugin"
    manifest_path = source_dir / "blender_manifest.toml"
    output_dir = (args.output_dir or (repo_root / "dist")).resolve()
    expected_tag = args.expected_tag
    if expected_tag is None:
        expected_tag = _automatic_expected_tag()

    manifest_fields = parse_manifest(manifest_path)
    version = validate_manifest(manifest_fields, source_dir, expected_tag)
    epoch = _source_date_epoch()
    archive_path = output_dir / f"{ARCHIVE_BASENAME}-{version}.zip"
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")

    build_archive(source_dir, archive_path, epoch)
    write_checksum(archive_path, checksum_path)
    digest = verify_archive(
        source_dir, archive_path, checksum_path, manifest_fields, epoch
    )

    if args.check:
        with tempfile.TemporaryDirectory(prefix="usdstage-release-check-") as temporary:
            second_archive = Path(temporary) / archive_path.name
            second_checksum = second_archive.with_suffix(second_archive.suffix + ".sha256")
            build_archive(source_dir, second_archive, epoch)
            write_checksum(second_archive, second_checksum)
            second_digest = verify_archive(
                source_dir, second_archive, second_checksum, manifest_fields, epoch
            )
            if archive_path.read_bytes() != second_archive.read_bytes() or digest != second_digest:
                raise ReleaseCheckError(
                    "archive is not byte-for-byte deterministic across repeated builds"
                )

    return archive_path, checksum_path, digest


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        archive_path, checksum_path, digest = run(argv)
    except ReleaseCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Built and verified: {archive_path}")
    print(f"SHA-256: {digest}")
    print(f"Checksum: {checksum_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
