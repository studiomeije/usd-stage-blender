#!/usr/bin/env python3
"""Report what Apple's toolchain actually does with the USD features we author.

A clean exit code from `realitytool` or `usdchecker` says a file was accepted,
not that its contents survived. Three times this project learned the
difference the hard way: `realitytool` discards variant sets outright, macOS
Quick Look ignores an authored variant selection, and `usdchecker` - alone
among them - validates every variant selection it finds.

So this measures instead of asking. Each probe is a minimal asset exercising
one feature. Every probe is packaged with `usdzip` and compiled with
`realitytool`, and the results are read back out of the artifacts themselves:
the USDZ through OpenUSD, the `.reality` by unzipping it and reading
`assetMap.json`, which is stored uncompressed and names every compiled asset
by the prim path it came from.

    python3 scripts/check_apple_conformance.py            # compare to expectations
    python3 scripts/check_apple_conformance.py --update   # record what it finds
    python3 scripts/check_apple_conformance.py --verbose  # show every detail

Exits non-zero only when a result differs from the recorded expectation, so a
feature Apple does not support is reported rather than treated as a failure.
It is what changes that matters.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTATIONS = REPO_ROOT / "scripts" / "apple_conformance.json"

REALITYTOOL = Path(
    "/Applications/Xcode.app/Contents/Developer/usr/bin/realitytool"
)
PLATFORM = "xros"
DEPLOYMENT_TARGET = "27.0"

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover - reported, not raised
    Usd = None


# ---------------------------------------------------------------------------
# Probe assets
# ---------------------------------------------------------------------------

def _stage(path: Path, root_name: str = "Asset"):
    stage = Usd.Stage.CreateNew(str(path))
    root = UsdGeom.Xform.Define(stage, f"/{root_name}")
    stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    return stage, root


def _cube(stage, path, size=1.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.GetSizeAttr().Set(size)
    half = size / 2.0
    cube.GetExtentAttr().Set([(-half,) * 3, (half,) * 3])
    return cube


def _material(stage, path, colour):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(*colour)
    )
    material.CreateSurfaceOutput().ConnectToSource(
        shader.ConnectableAPI(), "surface"
    )
    return material


def probe_variant_activation(path: Path):
    """A variant set that switches whole prims on and off."""
    stage, root = _stage(path)
    _cube(stage, "/Asset/Low", 1.0)
    _cube(stage, "/Asset/High", 2.0)
    variants = root.GetPrim().GetVariantSets().AddVariantSet("geo")
    for name, active in (("low", "Low"), ("high", "High")):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            for other in ("Low", "High"):
                stage.GetPrimAtPath(f"/Asset/{other}").SetActive(other == active)
    variants.SetVariantSelection("low")
    stage.GetRootLayer().Save()


def probe_variant_binding(path: Path):
    """A variant set that rebinds one mesh's material, changing no geometry."""
    stage, root = _stage(path)
    plinth = _cube(stage, "/Asset/Plinth", 1.0)
    _material(stage, "/Asset/Materials/Red", (0.8, 0.1, 0.1))
    _material(stage, "/Asset/Materials/Blue", (0.1, 0.2, 0.8))
    UsdShade.MaterialBindingAPI.Apply(plinth.GetPrim())
    variants = root.GetPrim().GetVariantSets().AddVariantSet("look")
    for name, material in (("red", "Red"), ("blue", "Blue")):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            UsdShade.MaterialBindingAPI(plinth.GetPrim()).Bind(
                UsdShade.Material.Get(stage, f"/Asset/Materials/{material}")
            )
    variants.SetVariantSelection("red")
    stage.GetRootLayer().Save()


def probe_variant_two_sets(path: Path):
    """Two variant sets on one prim, varying different properties."""
    stage, root = _stage(path)
    box = _cube(stage, "/Asset/Box", 1.0)
    sphere = UsdGeom.Sphere.Define(stage, "/Asset/Ball")
    sphere.GetRadiusAttr().Set(0.5)
    sphere.GetExtentAttr().Set([(-0.5,) * 3, (0.5,) * 3])
    _material(stage, "/Asset/Materials/Red", (0.8, 0.1, 0.1))
    _material(stage, "/Asset/Materials/Blue", (0.1, 0.2, 0.8))

    geo = root.GetPrim().GetVariantSets().AddVariantSet("geo")
    for name, active in (("boxy", "Box"), ("round", "Ball")):
        geo.AddVariant(name)
        geo.SetVariantSelection(name)
        with geo.GetVariantEditContext():
            for other in ("Box", "Ball"):
                stage.GetPrimAtPath(f"/Asset/{other}").SetActive(other == active)
    geo.SetVariantSelection("boxy")

    look = root.GetPrim().GetVariantSets().AddVariantSet("look")
    for name, material in (("red", "Red"), ("blue", "Blue")):
        look.AddVariant(name)
        look.SetVariantSelection(name)
        with look.GetVariantEditContext():
            for target in (box.GetPrim(), sphere.GetPrim()):
                UsdShade.MaterialBindingAPI.Apply(target)
                UsdShade.MaterialBindingAPI(target).Bind(
                    UsdShade.Material.Get(stage, f"/Asset/Materials/{material}")
                )
    look.SetVariantSelection("red")
    stage.GetRootLayer().Save()


def probe_variant_nested(path: Path):
    """A variant set authored inside another variant set's variant."""
    stage, root = _stage(path)
    _cube(stage, "/Asset/Top", 1.0)
    _material(stage, "/Asset/Materials/Oak", (0.7, 0.5, 0.3))
    _material(stage, "/Asset/Materials/Walnut", (0.2, 0.1, 0.05))
    top = stage.GetPrimAtPath("/Asset/Top")
    UsdShade.MaterialBindingAPI.Apply(top)

    shape = root.GetPrim().GetVariantSets().AddVariantSet("shape")
    for shape_name, finishes in (("round", ("Oak",)), ("square", ("Oak", "Walnut"))):
        shape.AddVariant(shape_name)
        shape.SetVariantSelection(shape_name)
        with shape.GetVariantEditContext():
            finish = root.GetPrim().GetVariantSets().AddVariantSet("finish")
            for material in finishes:
                finish.AddVariant(material.lower())
                finish.SetVariantSelection(material.lower())
                with finish.GetVariantEditContext():
                    UsdShade.MaterialBindingAPI(top).Bind(
                        UsdShade.Material.Get(stage, f"/Asset/Materials/{material}")
                    )
            finish.SetVariantSelection(finishes[0].lower())
    shape.SetVariantSelection("round")
    stage.GetRootLayer().Save()


def probe_variant_default_is_second(path: Path):
    """The shipped selection is the SECOND variant, not the first authored."""
    stage, root = _stage(path)
    _cube(stage, "/Asset/First", 1.0)
    _cube(stage, "/Asset/Second", 2.0)
    variants = root.GetPrim().GetVariantSets().AddVariantSet("pick")
    for name, active in (("first", "First"), ("second", "Second")):
        variants.AddVariant(name)
        variants.SetVariantSelection(name)
        with variants.GetVariantEditContext():
            for other in ("First", "Second"):
                stage.GetPrimAtPath(f"/Asset/{other}").SetActive(other == active)
    variants.SetVariantSelection("second")
    stage.GetRootLayer().Save()


def probe_asset_metadata(path: Path):
    """kind, assetInfo and extentsHint on the root prim."""
    stage, root = _stage(path)
    _cube(stage, "/Asset/Box", 1.0)
    prim = root.GetPrim()
    Usd.ModelAPI(prim).SetKind("component")
    Usd.ModelAPI(prim).SetAssetName("probe_asset")
    Usd.ModelAPI(prim).SetAssetIdentifier("probe_asset.usdz")
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.EarliestTime(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )
    UsdGeom.ModelAPI(prim).SetExtentsHint(
        UsdGeom.ModelAPI(prim).ComputeExtentsHint(cache)
    )
    stage.GetRootLayer().Save()


def probe_payload(path: Path):
    """An interface layer that payloads its geometry from a second file."""
    geometry_path = path.with_name(f"{path.stem}_geo.usda")
    geometry, geometry_root = _stage(geometry_path, "Geo")
    _cube(geometry, "/Geo/Box", 1.0)
    geometry.GetRootLayer().Save()

    stage, root = _stage(path)
    root.GetPrim().GetPayloads().AddPayload(f"./{geometry_path.name}")
    Usd.ModelAPI(root.GetPrim()).SetKind("component")
    stage.GetRootLayer().Save()


def probe_reference(path: Path):
    """An interface layer that references its geometry from a second file."""
    geometry_path = path.with_name(f"{path.stem}_geo.usda")
    geometry, _ = _stage(geometry_path, "Geo")
    _cube(geometry, "/Geo/Box", 1.0)
    geometry.GetRootLayer().Save()

    stage, root = _stage(path)
    root.GetPrim().GetReferences().AddReference(f"./{geometry_path.name}")
    stage.GetRootLayer().Save()


def probe_physics(path: Path):
    """UsdPhysics rigid body and collider on a mesh."""
    stage, root = _stage(path)
    cube = _cube(stage, "/Asset/Box", 1.0)
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
    mass = UsdPhysics.MassAPI.Apply(cube.GetPrim())
    mass.CreateMassAttr(2.5)
    stage.GetRootLayer().Save()


def probe_realitykit_component(path: Path):
    """A RealityKitComponent prim, the shape Reality Composer Pro writes."""
    stage, root = _stage(path)
    _cube(stage, "/Asset/Box", 1.0)
    component = stage.DefinePrim("/Asset/Component", "RealityKitComponent")
    attribute = component.CreateAttribute(
        "info:id", Sdf.ValueTypeNames.Token, custom=False
    )
    attribute.Set("RealityKit.Collider")
    stage.GetRootLayer().Save()


def probe_point_instancer(path: Path):
    """A UsdGeomPointInstancer scattering one prototype three times."""
    stage, root = _stage(path)
    instancer = UsdGeom.PointInstancer.Define(stage, "/Asset/Scatter")
    prototype = _cube(stage, "/Asset/Scatter/Prototypes/Box", 0.4)
    instancer.CreatePrototypesRel().SetTargets([prototype.GetPath()])
    instancer.CreateProtoIndicesAttr([0, 0, 0])
    instancer.CreatePositionsAttr(
        [Gf.Vec3f(-1, 0, 0), Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0)]
    )
    stage.GetRootLayer().Save()


PROBES = {
    "variant_activation": probe_variant_activation,
    "variant_binding": probe_variant_binding,
    "variant_two_sets": probe_variant_two_sets,
    "variant_nested": probe_variant_nested,
    "variant_default_is_second": probe_variant_default_is_second,
    "asset_metadata": probe_asset_metadata,
    "payload": probe_payload,
    "reference": probe_reference,
    "physics": probe_physics,
    "realitykit_component": probe_realitykit_component,
    "point_instancer": probe_point_instancer,
}


# ---------------------------------------------------------------------------
# Measuring what survived
# ---------------------------------------------------------------------------

def _read_usd(path: Path) -> dict:
    """What a composed stage holds, read back out of the artifact."""
    stage = Usd.Stage.Open(str(path))
    if not stage:
        return {"opened": False}
    root = stage.GetDefaultPrim()
    result: dict = {
        "opened": True,
        "prims": sorted(
            prim.GetName() for prim in stage.TraverseAll() if prim.GetName()
        ),
    }
    if not root:
        return result

    names = list(root.GetVariantSets().GetNames())
    result["variant_sets"] = sorted(names)
    result["variant_selections"] = {
        name: root.GetVariantSet(name).GetVariantSelection() for name in sorted(names)
    }
    result["variants"] = {
        name: sorted(root.GetVariantSet(name).GetVariantNames())
        for name in sorted(names)
    }
    kind = Usd.ModelAPI(root).GetKind()
    if kind:
        result["kind"] = kind
    info = root.GetAssetInfo()
    if info:
        result["asset_info"] = sorted(info.keys())
    hint = UsdGeom.ModelAPI(root).GetExtentsHint()
    result["extents_hint"] = bool(hint)
    result["physics_apis"] = sorted(
        {
            schema
            for prim in stage.TraverseAll()
            for schema in prim.GetAppliedSchemas()
            if "Physics" in schema
        }
    )
    result["type_names"] = sorted(
        {
            str(prim.GetTypeName())
            for prim in stage.TraverseAll()
            if prim.GetTypeName()
        }
    )
    return result


def _read_reality(path: Path) -> dict:
    """What a compiled .reality holds, read from its own asset map."""
    with zipfile.ZipFile(path) as archive:
        payload = json.loads(archive.read("assetMap.json"))
    assets = payload.get("assets", [])
    return {
        "asset_types": sorted({asset.get("typeName") for asset in assets if asset}),
        "prim_paths": sorted(
            {
                asset.get("metadata", {}).get("cdm:primpath")
                for asset in assets
                if asset.get("metadata", {}).get("cdm:primpath")
            }
        ),
        "mesh_count": sum(1 for a in assets if a.get("typeName") == "Mesh"),
        "material_count": sum(1 for a in assets if a.get("typeName") == "Material"),
        "configuration_sets": payload.get("configurationSetNames", []),
        "default_configurations": payload.get("defaultConfigurationNames", []),
    }


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=300
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        return 1, str(exc)
    return completed.returncode, (completed.stdout + completed.stderr)


def _package(source: Path, destination: Path) -> tuple[bool, str]:
    usdzip = shutil.which("usdzip")
    if usdzip is None:
        return False, "usdzip not installed"
    code, output = _run([usdzip, "--asset", str(source), str(destination)])
    return (code == 0 and destination.exists()), output


def _compile(source: Path, workdir: Path, name: str) -> tuple[bool, Path | None, str]:
    if not REALITYTOOL.exists():
        return False, None, "realitytool not installed"
    bundle = workdir / f"{name}.rkassets"
    bundle.mkdir(parents=True, exist_ok=True)
    for sibling in source.parent.glob(f"{source.stem}*.usda"):
        shutil.copy(sibling, bundle / sibling.name)
    output = workdir / f"{name}.reality"
    code, text = _run([
        str(REALITYTOOL), "compile",
        "--platform", PLATFORM,
        "--deployment-target", DEPLOYMENT_TARGET,
        "-o", str(output), str(bundle),
    ])
    if code != 0 or not output.exists():
        return False, None, text
    return True, output, text


def measure(name: str, build, workdir: Path) -> dict:
    """Author one probe, push it through both tools, and read the results back."""
    source = workdir / f"{name}.usda"
    build(source)

    result: dict = {"authored": _read_usd(source)}

    package = workdir / f"{name}.usdz"
    packaged, output = _package(source, package)
    result["usdz"] = _read_usd(package) if packaged else {"failed": output.strip()[:200]}

    compiled, reality, text = _compile(source, workdir, name)
    if compiled:
        result["reality"] = _read_reality(reality)
    else:
        result["reality"] = {"failed": text.strip()[:200]}
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(name: str, measured: dict) -> str:
    """One line saying what became of this feature."""
    authored, usdz, reality = (
        measured["authored"], measured["usdz"], measured["reality"]
    )

    if name.startswith("variant"):
        kept = usdz.get("variant_sets", [])
        survived_usdz = "kept" if kept == authored.get("variant_sets", []) else "LOST"
        sets = reality.get("configuration_sets")
        survived_reality = (
            "no configurations" if sets == [] else f"configurations={sets}"
        )
        if "failed" in reality:
            survived_reality = "compile failed"
        return f"usdz: variant sets {survived_usdz} | reality: {survived_reality}"

    if name == "asset_metadata":
        return (
            f"usdz: kind={usdz.get('kind')} assetInfo={usdz.get('asset_info')} "
            f"extentsHint={usdz.get('extents_hint')}"
        )

    if name in {"payload", "reference"}:
        prims = usdz.get("prims", [])
        return (
            f"usdz: composes {'Box' in prims and 'yes' or 'NO'} "
            f"({len(prims)} prims) | reality: {reality.get('mesh_count', 'n/a')} mesh(es)"
        )

    if name == "physics":
        return (
            f"usdz: {usdz.get('physics_apis')} | "
            f"reality: {reality.get('mesh_count', 'n/a')} mesh(es)"
        )

    if name == "realitykit_component":
        kept = "RealityKitComponent" in usdz.get("type_names", [])
        return (
            f"usdz: component prim {'kept' if kept else 'LOST'} | "
            f"reality: types={reality.get('asset_types')}"
        )

    if name == "point_instancer":
        kept = "PointInstancer" in usdz.get("type_names", [])
        return (
            f"usdz: instancer {'kept' if kept else 'LOST'} | "
            f"reality: {reality.get('mesh_count', 'n/a')} mesh(es)"
        )

    return "measured"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true",
                        help="record what this run finds as the expectation")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full measurement for every probe")
    parser.add_argument("--only", help="run one probe by name")
    args = parser.parse_args()

    if Usd is None:
        print("OpenUSD Python bindings are not available; install usd-core.")
        return 1

    probes = PROBES
    if args.only:
        if args.only not in PROBES:
            print(f"Unknown probe {args.only!r}. Known: {', '.join(sorted(PROBES))}")
            return 1
        probes = {args.only: PROBES[args.only]}

    measured: dict = {}
    with tempfile.TemporaryDirectory(prefix="apple-conformance-") as temporary:
        workdir = Path(temporary)
        for name, build in probes.items():
            measured[name] = measure(name, build, workdir)

    width = max(len(name) for name in probes)
    print(f"Apple toolchain conformance  ({PLATFORM} {DEPLOYMENT_TARGET})")
    print("=" * (width + 62))
    for name in probes:
        print(f"{name:{width}}  {summarise(name, measured[name])}")
    if args.verbose:
        print()
        print(json.dumps(measured, indent=2, sort_keys=True))

    if args.update:
        EXPECTATIONS.write_text(json.dumps(measured, indent=2, sort_keys=True) + "\n")
        print(f"\nRecorded {len(measured)} probe(s) in {EXPECTATIONS.name}.")
        return 0

    if not EXPECTATIONS.exists():
        print(f"\nNo expectations recorded yet. Run with --update to create "
              f"{EXPECTATIONS.name}.")
        return 0

    expected = json.loads(EXPECTATIONS.read_text())
    drifted = [
        name for name in probes
        if name in expected and expected[name] != measured[name]
    ]
    missing = [name for name in probes if name not in expected]

    if missing:
        print(f"\nNot yet recorded: {', '.join(missing)}. Run with --update.")
    if drifted:
        print(f"\nCHANGED since the recorded run: {', '.join(drifted)}")
        for name in drifted:
            print(f"\n--- {name} ---")
            print(f"  expected: {json.dumps(expected[name], sort_keys=True)}")
            print(f"  measured: {json.dumps(measured[name], sort_keys=True)}")
        print("\nApple's behaviour changed, or ours did. Work out which before "
              "recording the new result.")
        return 1

    print("\nEvery probe matches the recorded behaviour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
