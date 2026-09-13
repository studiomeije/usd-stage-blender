"""Focused tests for standards-compliant USDZ packaging."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from Plugin.export import pack_usdz


def _root_layer(path: Path) -> None:
    path.write_text(
        """#usda 1.0
(
    defaultPrim = "Root"
    metersPerUnit = 1
    upAxis = "Y"
)
def Xform "Root" {}
"""
    )


def test_allowed_member_extension_contract_is_immutable():
    assert isinstance(pack_usdz.USDZ_ALLOWED_MEMBER_EXTENSIONS, frozenset)
    assert pack_usdz.USDZ_ALLOWED_MEMBER_EXTENSIONS == frozenset(
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


def test_python_packager_stores_every_payload_at_64_byte_boundary(tmp_path):
    root = tmp_path / "scene.usda"
    _root_layer(root)
    (tmp_path / "textures").mkdir()
    (tmp_path / "textures" / "albedo.png").write_bytes(b"texture bytes")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "nested.usdz").write_bytes(b"nested package bytes")
    marker_dir = tmp_path / pack_usdz.GENERATION_MARKER_DIRECTORY
    marker_dir.mkdir()
    (marker_dir / "scene.txt").write_text("not a USDZ dependency\n")
    internal_dir = tmp_path / ".usdstage_internal"
    internal_dir.mkdir()
    (internal_dir / "state.json").write_text("{}\n")
    output = tmp_path.parent / f"{tmp_path.name}.usdz"

    pack_usdz.create_usdz_python(str(root), str(output))

    assert pack_usdz.validate_usdz_details(str(output))[0]
    with zipfile.ZipFile(output) as archive, output.open("rb") as raw_archive:
        assert archive.namelist() == [
            "scene.usda",
            "assets/nested.usdz",
            "textures/albedo.png",
        ]
        assert not any(
            part.startswith(".usdstage_")
            for name in archive.namelist()
            for part in Path(name).parts
        )
        for member in archive.infolist():
            assert member.compress_type == zipfile.ZIP_STORED
            assert pack_usdz._member_data_offset(raw_archive, member) % 64 == 0


def test_python_packager_rejects_unknown_staging_member(tmp_path):
    root = tmp_path / "scene.usda"
    output = tmp_path.parent / f"{tmp_path.name}.usdz"
    _root_layer(root)
    (tmp_path / "notes.txt").write_text("not a USDZ dependency\n")

    with pytest.raises(RuntimeError, match="Unsupported USDZ staging member type"):
        pack_usdz.create_usdz_python(str(root), str(output))

    assert not output.exists()


def test_validator_rejects_an_ordinary_misaligned_zip(tmp_path):
    output = tmp_path / "invalid.usdz"
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("scene.usda", "#usda 1.0\n")

    valid, errors = pack_usdz.validate_usdz_details(str(output))

    assert valid is False
    assert any("Misaligned package member" in error for error in errors)


def test_validator_accepts_aligned_avif_and_nested_usdz_without_checker(tmp_path):
    root = tmp_path / "scene.usda"
    avif = tmp_path / "albedo.avif"
    nested = tmp_path / "nested.usdz"
    output = tmp_path / "apple-members.usdz"
    _root_layer(root)
    avif.write_bytes(b"avif payload")
    nested.write_bytes(b"nested package payload")

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        pack_usdz._write_aligned_member(archive, root, root.name)
        pack_usdz._write_aligned_member(archive, avif, "textures/albedo.avif")
        pack_usdz._write_aligned_member(archive, nested, "assets/nested.usdz")

    valid, errors = pack_usdz.validate_usdz_details(str(output))

    assert valid is True
    assert errors == []


def test_validator_rejects_aligned_unknown_member_without_checker(tmp_path):
    root = tmp_path / "scene.usda"
    note = tmp_path / "note.txt"
    output = tmp_path / "unknown-member.usdz"
    _root_layer(root)
    note.write_text("not an Apple USDZ payload\n")

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        pack_usdz._write_aligned_member(archive, root, root.name)
        pack_usdz._write_aligned_member(archive, note, note.name)

    valid, errors = pack_usdz.validate_usdz_details(str(output))

    assert valid is False
    assert errors == ["Unsupported USDZ package member type: note.txt"]


@pytest.mark.parametrize(
    "internal_name",
    [
        f"{pack_usdz.GENERATION_MARKER_DIRECTORY}/scene.txt",
        ".usdstage_internal/state.json",
    ],
)
def test_validator_rejects_aligned_internal_metadata(tmp_path, internal_name):
    root = tmp_path / "scene.usda"
    metadata = tmp_path / "metadata.txt"
    output = tmp_path / "internal-metadata.usdz"
    _root_layer(root)
    metadata.write_text("exporter bookkeeping\n")

    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        pack_usdz._write_aligned_member(archive, root, root.name)
        pack_usdz._write_aligned_member(archive, metadata, internal_name)

    valid, errors = pack_usdz.validate_usdz_details(str(output))

    assert valid is False
    assert any(
        error == f"Internal USD Stage for Blender metadata in package: {internal_name}"
        for error in errors
    )


def test_external_packager_uses_dependency_isolating_asset_mode(tmp_path, monkeypatch):
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output.write_bytes(b"created")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)

    pack_usdz.create_usdz_with_tool(str(root), str(output), "/tools/usdzip")

    assert calls[0][0] == [
        "/tools/usdzip",
        "--asset",
        str(root),
        "--checkCompliance",
        str(output),
    ]
    assert calls[0][1]["timeout"] == pack_usdz._PACKAGER_TIMEOUT_SECONDS


def test_apple_checker_failure_is_not_reported_as_valid(tmp_path, monkeypatch):
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    pack_usdz.create_usdz_python(str(root), str(output))

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="  --arkit  Check RealityKit compatibility\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="Failed!", stderr="bad asset")

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)
    valid, errors = pack_usdz.validate_usdz_details(
        str(output),
        usdchecker_path="/tools/usdchecker",
    )

    assert valid is False
    assert calls == [
        ["/tools/usdchecker", "--help"],
        ["/tools/usdchecker", "--arkit", "--strict", str(output)],
    ]
    assert any("usdchecker --arkit --strict failed" in error for error in errors)


def test_checker_without_arkit_uses_explicit_strict_fallback(tmp_path, monkeypatch):
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    pack_usdz.create_usdz_python(str(root), str(output))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="Options:\n  --strict  Treat warnings as errors\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)

    valid, errors = pack_usdz.validate_usdz_details(
        str(output),
        usdchecker_path="/tools/usdchecker",
    )

    assert valid is True
    assert errors == []
    assert calls == [
        ["/tools/usdchecker", "--help"],
        ["/tools/usdchecker", "--strict", str(output)],
    ]


def test_checker_capability_probe_failure_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    pack_usdz.create_usdz_python(str(root), str(output))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=2, stdout="", stderr="broken toolchain")

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)

    valid, errors = pack_usdz.validate_usdz_details(
        str(output),
        usdchecker_path="/tools/usdchecker",
    )

    assert valid is False
    assert calls == [["/tools/usdchecker", "--help"]]
    assert any("capability probe failed with exit code 2" in error for error in errors)


def test_checker_capability_probe_timeout_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    pack_usdz.create_usdz_python(str(root), str(output))

    def fake_run(command, **kwargs):
        raise pack_usdz.ExternalToolTimeout(command, kwargs["timeout"])

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)

    valid, errors = pack_usdz.validate_usdz_details(
        str(output),
        usdchecker_path="/tools/usdchecker",
    )

    assert valid is False
    assert any("capability probe failed" in error for error in errors)
    assert any("timed out after 15s" in error for error in errors)


def test_create_usdz_mandates_arkit_when_checker_advertises_it(
    tmp_path,
    monkeypatch,
):
    import Plugin

    root = tmp_path / "scene.usda"
    output = tmp_path.parent / f"{tmp_path.name}.usdz"
    _root_layer(root)
    preferences = SimpleNamespace(usdzip_path="")
    prefs_module = SimpleNamespace(get_preferences=lambda _context: preferences)
    monkeypatch.setitem(sys.modules, "Plugin.prefs", prefs_module)
    monkeypatch.setattr(Plugin, "prefs", prefs_module, raising=False)
    monkeypatch.setattr(
        pack_usdz,
        "_find_usdchecker",
        lambda _usdzip_path: "/tools/usdchecker",
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="  --arkit  Check RealityKit compatibility\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pack_usdz, "_run_external_tool", fake_run)

    pack_usdz.create_usdz(str(root), str(output), None, None)

    assert output.is_file()
    assert len(calls) == 2
    assert calls[0] == ["/tools/usdchecker", "--help"]
    assert calls[1][:3] == ["/tools/usdchecker", "--arkit", "--strict"]
    checker_input = Path(calls[1][3])
    assert checker_input.parent == output.parent
    assert checker_input.name.startswith(f".{output.stem}.")
    assert checker_input.name.endswith(".tmp.usdz")


def test_create_usdz_refuses_output_symlink_without_touching_target(tmp_path):
    root = tmp_path / "scene.usda"
    sentinel = tmp_path / "unrelated.dat"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    sentinel.write_bytes(b"must survive")
    output.symlink_to(sentinel)

    with pytest.raises(RuntimeError, match="Refusing USDZ output symlink"):
        pack_usdz.create_usdz(str(root), str(output), None, None)

    assert output.is_symlink()
    assert sentinel.read_bytes() == b"must survive"


def test_builtin_archive_passes_available_apple_usdchecker(tmp_path):
    checker = pack_usdz._find_usdchecker(None)
    if not checker:
        return
    root = tmp_path / "scene.usda"
    output = tmp_path / "scene.usdz"
    _root_layer(root)
    pack_usdz.create_usdz_python(str(root), str(output))

    valid, errors = pack_usdz.validate_usdz_details(
        str(output),
        usdchecker_path=checker,
    )

    assert valid is True, "\n".join(errors)
