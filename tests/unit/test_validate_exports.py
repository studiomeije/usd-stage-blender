"""Focused tests for the release export validator."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import validate_exports


def test_collect_inputs_includes_usdz_and_treats_rkassets_as_one_unit(tmp_path):
    package = tmp_path / "asset.usdz"
    package.write_bytes(b"package")
    bundle = tmp_path / "Project.rkassets"
    bundle.mkdir()
    (bundle / "scene.usdc").write_bytes(b"binary")

    assert validate_exports._collect_inputs(bundle) == [bundle]
    assert validate_exports._collect_inputs(package) == [package]
    assert validate_exports._collect_inputs(tmp_path) == [package, bundle]


def test_collect_inputs_ignores_directories_with_usd_file_suffixes(tmp_path):
    scene = tmp_path / "Robot.usdc"
    scene.write_bytes(b"binary")
    texture_cache = tmp_path / "textures" / "Robot.usdc"
    texture_cache.mkdir(parents=True)
    (texture_cache / "albedo.png").write_bytes(b"texture")

    assert validate_exports._collect_inputs(tmp_path) == [scene]


def test_compile_output_stems_disambiguate_formats_with_same_stem(tmp_path):
    usdc = tmp_path / "RedCube.usdc"
    usdz = tmp_path / "RedCube.usdz"
    usdc.write_bytes(b"binary")
    usdz.write_bytes(b"package")

    stems = validate_exports._compile_output_stems([usdc, usdz])

    assert stems == {
        usdc: "RedCube-usdc",
        usdz: "RedCube-usdz",
    }


def test_compile_output_stems_preserve_unique_historical_name(tmp_path):
    scene = tmp_path / "RedCube.usdc"
    scene.write_bytes(b"binary")

    assert validate_exports._compile_output_stems([scene]) == {scene: None}


def test_nodedef_lint_detects_unknown_ids():
    lint = validate_exports._lint_usd_text(
        'token info:id = "ND_not_in_manifest"\noutputs:mtlx:surface.connect',
        {"ND_known"},
    )

    assert lint["errors"] == ["Unknown nodedef: ND_not_in_manifest"]


def test_usdchecker_always_uses_strict_mode(tmp_path, monkeypatch):
    scene = tmp_path / "scene.usda"
    scene.write_text("#usda 1.0\n")
    calls = []

    monkeypatch.setattr(
        validate_exports,
        "_resolve_usd_tool",
        lambda name: f"/tools/{name}",
    )
    monkeypatch.setattr(validate_exports, "_tool_version", lambda tool: "test-version")

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(validate_exports, "_run_external_tool", fake_run)

    result = validate_exports._run_usdchecker(scene, arkit=True)

    assert result["ok"] is True
    assert calls == [["/tools/usdchecker", "--arkit", "--strict", str(scene)]]


def test_usdz_is_alignment_checked_and_apple_strict_by_default(tmp_path, monkeypatch):
    package = tmp_path / "misaligned.usdz"
    # 30-byte local header + len("payload.usda") == payload offset 42, which
    # violates USDZ's mandatory 64-byte alignment.
    with zipfile.ZipFile(package, mode="w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("payload.usda", "#usda 1.0\n")

    checker_calls = []

    def fake_checker(path, *, arkit, timeout):
        checker_calls.append((path, arkit, timeout))
        return {"ok": True, "exit_code": 0, "timed_out": False}

    monkeypatch.setattr(validate_exports, "_run_usdchecker", fake_checker)
    args = SimpleNamespace(
        no_usdchecker=False,
        no_lint=True,
        no_compile=True,
        arkit=False,
        tool_timeout=12,
    )

    result = validate_exports._validate_usd(package, set(), args)

    assert result["status"] == "error"
    assert result["usdz_structure"]["ok"] is False
    assert result["usdz_structure"]["profile"] == "apple-usdz-64-byte-alignment"
    assert any("payload offset 42" in error for error in result["usdz_structure"]["errors"])
    assert checker_calls == [(package, True, 12)]


def test_compile_staging_preserves_binary_extension_and_dependencies(tmp_path, monkeypatch):
    scene = tmp_path / "scene.usdc"
    scene.write_bytes(b"binary-usdc")
    assets = tmp_path / "assets"
    assets.mkdir()
    sublayer = assets / "sub.usda"
    sublayer.write_text('#usda 1.0\ndef Xform "Sub" {}\n')
    (assets / "metadata.bin").write_bytes(b"metadata")
    textures = tmp_path / "textures"
    textures.mkdir()
    (textures / "albedo.png").write_bytes(b"texture")
    inspected = {}

    def fake_load(path):
        if path == scene:
            return "@assets/sub.usda@ @textures/albedo.png@"
        return sublayer.read_text()

    def fake_compile(bundle, args, **kwargs):
        inspected["scene"] = (bundle / "scene.usdc").read_bytes()
        inspected["sublayer"] = (bundle / "assets" / "sub.usda").read_text()
        inspected["metadata"] = (bundle / "assets" / "metadata.bin").read_bytes()
        inspected["texture"] = (bundle / "textures" / "albedo.png").read_bytes()
        assert not (bundle / "scene.usda").exists()
        return {"ok": True, "exit_code": 0}

    monkeypatch.setattr(validate_exports, "_load_usd_text", fake_load)
    monkeypatch.setattr(validate_exports, "_compile_rkassets", fake_compile)

    result = validate_exports._compile_from_usd(scene, SimpleNamespace())

    assert result["ok"] is True
    assert inspected == {
        "scene": b"binary-usdc",
        "sublayer": sublayer.read_text(),
        "metadata": b"metadata",
        "texture": b"texture",
    }


def test_compile_staging_expands_usdz_instead_of_nesting_package(
    tmp_path,
    monkeypatch,
):
    package = tmp_path / "Robot.usdz"
    with zipfile.ZipFile(
        package,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        archive.writestr("Robot.usdc", b"binary-usdc")
        archive.writestr("textures/albedo.png", b"texture")
    inspected = {}

    def fake_compile(bundle, args, **kwargs):
        inspected["scene"] = (bundle / "Robot.usdc").read_bytes()
        inspected["texture"] = (
            bundle / "textures" / "albedo.png"
        ).read_bytes()
        inspected["nested_package"] = (bundle / "scene.usdz").exists()
        return {"ok": True, "exit_code": 0}

    monkeypatch.setattr(validate_exports, "_compile_rkassets", fake_compile)

    result = validate_exports._compile_from_usd(package, SimpleNamespace())

    assert result["ok"] is True
    assert inspected == {
        "scene": b"binary-usdc",
        "texture": b"texture",
        "nested_package": False,
    }


def test_compile_staging_rejects_unsafe_usdz_member(tmp_path, monkeypatch):
    package = tmp_path / "Unsafe.usdz"
    with zipfile.ZipFile(
        package,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        archive.writestr("Robot.usdc", b"binary-usdc")
        archive.writestr("../escape.png", b"escape")
    monkeypatch.setattr(
        validate_exports,
        "_compile_rkassets",
        lambda *_args, **_kwargs: pytest.fail("unsafe package was compiled"),
    )

    result = validate_exports._compile_from_usd(package, SimpleNamespace())

    assert result["ok"] is False
    assert "Unsafe USDZ compile member path" in result["stderr"]
    assert not (tmp_path / "escape.png").exists()


def test_realitytool_success_without_output_is_failure(tmp_path, monkeypatch):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    sibling = bundle.with_suffix(".reality")
    sibling.write_bytes(b"stale project artifact")
    args = SimpleNamespace(platform="xros", deployment_target="27.0")
    monkeypatch.setattr(
        validate_exports,
        "_run_external_tool",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(validate_exports.shutil, "which", lambda name: "/usr/bin/xcrun")
    monkeypatch.setattr(
        validate_exports,
        "_find_xcrun_tool",
        lambda name: f"/tools/{name}",
    )
    monkeypatch.setattr(validate_exports, "_xcode_version", lambda: "Xcode 27")

    result = validate_exports._compile_rkassets(bundle, args)

    assert result["ok"] is False
    assert "created no fresh regular .reality output" in result["stderr"]
    assert result["output_reality"] is None
    assert sibling.read_bytes() == b"stale project artifact"


def test_default_deployment_target_is_os_27(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["validate_exports.py", "--input", "scene.usdc"],
    )

    args = validate_exports._parse_args()

    assert args.deployment_target == "27.0"
    assert args.compiled_output_dir is None


def test_use_metal_is_forwarded_to_realitytool(tmp_path, monkeypatch):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    args = SimpleNamespace(
        platform="xros",
        deployment_target="27.0",
        use_metal=True,
        tool_timeout=42,
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("--output-reality") + 1])
        output.write_bytes(b"compiled")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(validate_exports, "_run_external_tool", fake_run)
    monkeypatch.setattr(validate_exports.shutil, "which", lambda name: "/usr/bin/xcrun")
    monkeypatch.setattr(
        validate_exports,
        "_find_xcrun_tool",
        lambda name: f"/tools/{name}",
    )
    monkeypatch.setattr(validate_exports, "_xcode_version", lambda: "Xcode 27")

    result = validate_exports._compile_rkassets(bundle, args)

    assert result["ok"] is True
    assert "--use-metal" in calls[0][0]
    assert calls[0][0][calls[0][0].index("--use-metal") + 1] == "true"
    assert calls[0][1]["timeout"] == 42
    assert result["output_reality"] is None
    assert result["output_persisted"] is False
    assert result["output_size"] == len(b"compiled")
    assert result["output_sha256"]
    assert not bundle.with_suffix(".reality").exists()


def test_realitytool_empty_output_is_failure(tmp_path, monkeypatch):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    args = SimpleNamespace(platform="macosx", deployment_target="27.0")

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output-reality") + 1])
        output.touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(validate_exports, "_run_external_tool", fake_run)
    monkeypatch.setattr(validate_exports.shutil, "which", lambda name: "/usr/bin/xcrun")
    monkeypatch.setattr(validate_exports, "_find_xcrun_tool", lambda name: f"/tools/{name}")
    monkeypatch.setattr(validate_exports, "_xcode_version", lambda: "Xcode 27")

    result = validate_exports._compile_rkassets(bundle, args)

    assert result["ok"] is False
    assert result["output_size"] == 0
    assert "created an empty .reality output" in result["stderr"]
    assert not bundle.with_suffix(".reality").exists()


def test_checker_timeout_has_structured_exit_124(tmp_path, monkeypatch):
    scene = tmp_path / "scene.usda"
    scene.write_text("#usda 1.0\n")
    monkeypatch.setattr(
        validate_exports,
        "_resolve_usd_tool",
        lambda name: "/tools/usdchecker",
    )
    monkeypatch.setattr(validate_exports, "_tool_version", lambda tool: "test-version")

    def timeout(command, **kwargs):
        raise validate_exports.ExternalToolTimeout(command, kwargs["timeout"])

    monkeypatch.setattr(validate_exports, "_run_external_tool", timeout)

    result = validate_exports._run_usdchecker(scene, timeout=0.01)

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["exit_code"] == 124


def test_json_only_rcp3_bundle_relies_on_real_compile_gate(tmp_path, monkeypatch):
    bundle = tmp_path / "Project.rkassets"
    bundle.mkdir()
    (bundle / "project.json").write_text("{}")
    args = SimpleNamespace(
        no_usdchecker=False,
        no_lint=False,
        no_compile=False,
    )
    monkeypatch.setattr(
        validate_exports,
        "_compile_rkassets",
        lambda path, args: {"ok": True, "exit_code": 0},
    )

    result = validate_exports._validate_rkassets(bundle, set(), args)

    assert result["status"] == "ok"
    assert result["usdchecker"]["skipped"] is True
    assert result["compile"]["ok"] is True


def test_compiled_output_dir_preserves_deterministic_platform_artifact(tmp_path, monkeypatch):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    compiled = tmp_path / "compiled"
    args = SimpleNamespace(
        platform="macosx",
        deployment_target="27.0",
        use_metal=True,
        tool_timeout=42,
        compiled_output_dir=str(compiled),
    )

    def fake_run(command, **kwargs):
        output = Path(command[command.index("--output-reality") + 1])
        output.write_bytes(b"compiled reality")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(validate_exports, "_run_external_tool", fake_run)
    monkeypatch.setattr(validate_exports.shutil, "which", lambda name: "/usr/bin/xcrun")
    monkeypatch.setattr(
        validate_exports,
        "_find_xcrun_tool",
        lambda name: f"/tools/{name}",
    )
    monkeypatch.setattr(validate_exports, "_xcode_version", lambda: "Xcode 27")

    result = validate_exports._compile_rkassets(
        bundle,
        args,
        output_stem="My Scene",
    )

    output = Path(result["output_reality"])
    assert result["ok"] is True
    assert output.parent == compiled.resolve()
    assert output.name == "My-Scene-58a4e6a8-macosx-27.0.reality"
    assert output.read_bytes() == b"compiled reality"
    assert result["output_persisted"] is True
    assert result["output_size"] == len(b"compiled reality")
    assert result["output_sha256"]
    assert not list(compiled.glob("*.lock"))


def test_compiled_output_dir_refuses_existing_or_duplicate_output(tmp_path):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    existing = compiled / "Scene-xros-27.0.reality"
    existing.write_bytes(b"do not overwrite")
    args = SimpleNamespace(
        platform="xros",
        deployment_target="27.0",
        compiled_output_dir=str(compiled),
    )

    result = validate_exports._compile_rkassets(bundle, args)

    assert result["ok"] is False
    assert "already exists" in result["stderr"]
    assert existing.read_bytes() == b"do not overwrite"


def test_compiled_output_dir_refuses_platform_traversal(tmp_path):
    bundle = tmp_path / "Scene.rkassets"
    bundle.mkdir()
    args = SimpleNamespace(
        platform="../../escape",
        deployment_target="27.0",
        compiled_output_dir=str(tmp_path / "compiled"),
    )

    result = validate_exports._compile_rkassets(bundle, args)

    assert result["ok"] is False
    assert "Unsafe or unsupported platform" in result["stderr"]
    assert not (tmp_path / "escape.reality").exists()
