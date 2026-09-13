"""Tests for the deterministic extension release archive."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Optional


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RELEASE_SCRIPT = REPOSITORY_ROOT / "scripts" / "release_archive.py"


def valid_manifest(**overrides: str) -> str:
    fields = {
        "schema_version": "1.0.0",
        "id": "usd_stage",
        "name": "USD Stage for Blender",
        "tagline": "USD export for RealityKit and Reality Composer Pro",
        "version": "1.0.0",
        "maintainer": "Studio Meije <maintainers@usdstage.dev>",
        "type": "add-on",
        "blender_version_min": "5.2.0",
        "website": "https://github.com/studiomeije/usd-stage-blender",
    }
    fields.update(overrides)
    lines = [f'{key} = "{value}"' for key, value in fields.items()]
    lines.extend(
        (
            "license = [\"SPDX:GPL-3.0-or-later\"]",
            "[permissions]",
            'files = "Export USD and stage referenced assets"',
        )
    )
    return "\n".join(lines) + "\n"


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="usdstage-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.plugin = self.repo / "Plugin"
        self.plugin.mkdir(parents=True)
        (self.repo / "LICENSE").write_bytes((REPOSITORY_ROOT / "LICENSE").read_bytes())
        (self.repo / "THIRD_PARTY_LICENSES").mkdir()
        (self.repo / "THIRD_PARTY_LICENSES" / "Apache-2.0.txt").write_bytes(
            (
                REPOSITORY_ROOT
                / "THIRD_PARTY_LICENSES"
                / "Apache-2.0.txt"
            ).read_bytes()
        )
        (self.repo / "THIRD_PARTY_NOTICES.txt").write_bytes(
            (REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.txt").read_bytes()
        )
        (self.plugin / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.plugin / "__main__.py").write_text(
            "from core.package_bootstrap import bootstrap\n", encoding="utf-8"
        )
        (self.plugin / "blender_manifest.toml").write_text(
            valid_manifest(), encoding="utf-8"
        )
        assets = self.plugin / "assets"
        assets.mkdir()
        (assets / "nodegroups.blend").write_bytes(b"fixture blend data")
        (assets / "icons").mkdir()
        (assets / "icons" / "usd_stage.png").write_bytes(b"fixture png data")
        core = self.plugin / "core"
        core.mkdir()
        (core / "package_bootstrap.py").write_text(
            "def bootstrap():\n    return None\n", encoding="utf-8"
        )
        manifest_assets = self.plugin / "manifest"
        manifest_assets.mkdir()
        (manifest_assets / "rk_nodes_manifest.json").write_text("{}\n", encoding="utf-8")
        nested = self.plugin / "nested"
        nested.mkdir()
        (nested / "module.py").write_text("print('fixture')\n", encoding="utf-8")
        cache = nested / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-313.pyc").write_bytes(b"excluded")
        (self.plugin / ".DS_Store").write_bytes(b"excluded")

    def run_release(
        self,
        *arguments: str,
        output_dir: Optional[Path] = None,
        environment_updates: Optional[dict] = None,
    ) -> subprocess.CompletedProcess:
        command = [
            sys.executable,
            str(RELEASE_SCRIPT),
            "--repo-root",
            str(self.repo),
            "--output-dir",
            str(output_dir or (self.repo / "dist")),
            *arguments,
        ]
        environment = os.environ.copy()
        environment.pop("USDSTAGE_RELEASE_TAG", None)
        environment.pop("GITHUB_REF_NAME", None)
        environment.pop("GITHUB_REF_TYPE", None)
        environment.pop("SOURCE_DATE_EPOCH", None)
        if environment_updates:
            environment.update(environment_updates)
        return subprocess.run(command, text=True, capture_output=True, env=environment)

    def test_check_builds_versioned_deterministic_verified_artifacts(self) -> None:
        first_output = self.repo / "first"
        second_output = self.repo / "second"
        first = self.run_release("--check", output_dir=first_output)
        second = self.run_release("--check", output_dir=second_output)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

        first_archive = first_output / "usd-stage-for-blender-1.0.0.zip"
        second_archive = second_output / "usd-stage-for-blender-1.0.0.zip"
        first_checksum = first_archive.with_suffix(".zip.sha256")
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        self.assertEqual(first_archive.stat().st_mode & 0o777, 0o644)
        self.assertEqual(first_checksum.stat().st_mode & 0o777, 0o644)

        digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
        self.assertEqual(
            first_checksum.read_text(encoding="ascii"),
            f"{digest}  usd-stage-for-blender-1.0.0.zip\n",
        )

        with zipfile.ZipFile(first_archive) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            self.assertEqual(names, sorted(names))
            self.assertIn("usd_stage/__init__.py", names)
            self.assertIn("usd_stage/__main__.py", names)
            self.assertIn("usd_stage/blender_manifest.toml", names)
            self.assertIn("usd_stage/core/package_bootstrap.py", names)
            self.assertIn("usd_stage/LICENSE", names)
            self.assertIn(
                "usd_stage/THIRD_PARTY_LICENSES/Apache-2.0.txt", names
            )
            self.assertIn("usd_stage/THIRD_PARTY_NOTICES.txt", names)
            self.assertNotIn("usd_stage/.DS_Store", names)
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertEqual(
                archive.read("usd_stage/blender_manifest.toml"),
                (self.plugin / "blender_manifest.toml").read_bytes(),
            )
            self.assertEqual(
                archive.read("usd_stage/LICENSE"),
                (self.repo / "LICENSE").read_bytes(),
            )
            self.assertEqual(
                archive.read(
                    "usd_stage/THIRD_PARTY_LICENSES/Apache-2.0.txt"
                ),
                (
                    self.repo
                    / "THIRD_PARTY_LICENSES"
                    / "Apache-2.0.txt"
                ).read_bytes(),
            )
            self.assertEqual(
                archive.read("usd_stage/THIRD_PARTY_NOTICES.txt"),
                (self.repo / "THIRD_PARTY_NOTICES.txt").read_bytes(),
            )
            self.assertTrue(all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in members))
            self.assertTrue(all(member.compress_type == zipfile.ZIP_DEFLATED for member in members))
            for member in members:
                mode = (member.external_attr >> 16) & 0xFFFF
                expected = 0o40755 if member.is_dir() else 0o100644
                self.assertEqual(mode, expected, member.filename)

    def test_expected_tag_requires_exact_bare_manifest_version(self) -> None:
        accepted = self.run_release("--expected-tag", "1.0.0")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        for tag in ("v1.0.0", "1.0.1"):
            with self.subTest(tag=tag):
                rejected = self.run_release("--expected-tag", tag)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("release tag", rejected.stderr)

        (self.plugin / "blender_manifest.toml").write_text(
            valid_manifest(version="1.0.0-rc.1"), encoding="utf-8"
        )
        prerelease = self.run_release("--expected-tag", "1.0.0-rc.1")
        self.assertNotEqual(prerelease.returncode, 0)
        self.assertIn("stable bare SemVer", prerelease.stderr)

    def test_github_tag_ref_is_enforced_automatically(self) -> None:
        accepted = self.run_release(
            environment_updates={"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "1.0.0"}
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        rejected = self.run_release(
            environment_updates={"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "1.0.1"}
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("does not exactly match", rejected.stderr)

        missing_name = self.run_release(
            environment_updates={"GITHUB_REF_TYPE": "tag"}
        )
        self.assertNotEqual(missing_name.returncode, 0)
        self.assertIn("GITHUB_REF_NAME is missing", missing_name.stderr)

    def test_manifest_release_invariants_fail_closed(self) -> None:
        invalid_fields = {
            "schema_version": "2.0.0",
            "version": "2.0",
            "blender_version_min": "5.1.0",
            "maintainer": "Your Name <your.email@example.com>",
            "website": "http://example.com",
            "tagline": "x" * 65,
        }
        for field, value in invalid_fields.items():
            with self.subTest(field=field):
                (self.plugin / "blender_manifest.toml").write_text(
                    valid_manifest(**{field: value}), encoding="utf-8"
                )
                result = self.run_release()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr.startswith("ERROR:"), result.stderr)

        manifest_without_permission = valid_manifest().split("[permissions]", maxsplit=1)[0]
        (self.plugin / "blender_manifest.toml").write_text(
            manifest_without_permission, encoding="utf-8"
        )
        missing_permission = self.run_release()
        self.assertNotEqual(missing_permission.returncode, 0)
        self.assertIn("permissions.files", missing_permission.stderr)

    def test_installed_cli_bootstrap_files_are_required(self) -> None:
        for relative in ("__main__.py", "core/package_bootstrap.py"):
            with self.subTest(relative=relative):
                target = self.plugin / relative
                original = target.read_bytes()
                target.unlink()
                try:
                    result = self.run_release()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(relative, result.stderr)
                finally:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original)

    def test_release_legal_files_are_required_and_validated(self) -> None:
        for filename in (
            "LICENSE",
            "THIRD_PARTY_LICENSES/Apache-2.0.txt",
            "THIRD_PARTY_NOTICES.txt",
        ):
            with self.subTest(filename=filename):
                target = self.repo / filename
                original = target.read_bytes()
                target.unlink()
                try:
                    missing = self.run_release()
                    self.assertNotEqual(missing.returncode, 0)
                    self.assertIn(filename, missing.stderr)
                finally:
                    target.write_bytes(original)

        license_path = self.repo / "LICENSE"
        license_path.write_text("GNU GENERAL PUBLIC LICENSE\n", encoding="utf-8")
        incomplete_gpl = self.run_release()
        self.assertNotEqual(incomplete_gpl.returncode, 0)
        self.assertIn("complete GNU GPL version 3 text", incomplete_gpl.stderr)

        license_path.write_bytes((REPOSITORY_ROOT / "LICENSE").read_bytes())
        apache_path = self.repo / "THIRD_PARTY_LICENSES" / "Apache-2.0.txt"
        apache_path.write_text("Apache License\n", encoding="utf-8")
        incomplete_apache = self.run_release()
        self.assertNotEqual(incomplete_apache.returncode, 0)
        self.assertIn("complete Apache 2.0 license text", incomplete_apache.stderr)

        apache_path.write_bytes(
            (
                REPOSITORY_ROOT
                / "THIRD_PARTY_LICENSES"
                / "Apache-2.0.txt"
            ).read_bytes()
        )
        notices_path = self.repo / "THIRD_PARTY_NOTICES.txt"
        notices_path.write_text("Copyright © 2024 Apple Inc.\n", encoding="utf-8")
        incomplete_notice = self.run_release()
        self.assertNotEqual(incomplete_notice.returncode, 0)
        self.assertIn("required Apple MaterialX notice", incomplete_notice.stderr)

        notices_path.write_bytes((REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.txt").read_bytes())
        notices_path.write_text(
            notices_path.read_text(encoding="utf-8").replace(
                "Copyright Contributors to the MaterialX Project.", ""
            ),
            encoding="utf-8",
        )
        incomplete_materialx_notice = self.run_release()
        self.assertNotEqual(incomplete_materialx_notice.returncode, 0)
        self.assertIn(
            "required MaterialX notice", incomplete_materialx_notice.stderr
        )

    def test_symlinked_plugin_content_is_rejected(self) -> None:
        target = self.repo / "outside.txt"
        target.write_text("must not be archived\n", encoding="utf-8")
        link = self.plugin / "linked.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this platform")
        result = self.run_release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlinks are not allowed", result.stderr)


if __name__ == "__main__":
    unittest.main()
