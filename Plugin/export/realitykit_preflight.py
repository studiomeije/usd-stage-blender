"""Strict USD asset preflight for RealityKit on Apple OS 27.

The checks in this module intentionally validate the composed USD stage rather
than Blender source state.  Run :func:`validate_stage` after material rewriting
and asset staging, but before saving or packaging the stage.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Iterable

from .usd_utils import Sdf, Usd, UsdGeom, UsdShade
from .materials.mapping import (
    authored_texture_mapping_contract,
    mapping_contract_details,
)

try:
    from pxr import UsdSkel
except ImportError:  # pragma: no cover - guarded by the plugin's pxr requirement
    UsdSkel = None


TARGET_PROFILE = "RealityKit-AppleOS27"

# Variant validation is intentionally exhaustive, but an unbounded Cartesian
# product lets a hostile or accidental asset turn export preflight into an
# exponential-time operation. 256 composed states is large enough for normal
# look/LOD/product variants while keeping the validation cost deterministic.
# Assets above this fixed safety bound fail closed and should be split or have
# their variants flattened before export.
MAX_VARIANT_COMBINATIONS = 256

# Apple documents these as the texture formats permitted inside USDZ. Other
# USD file types can reference a wider set, so unsupported extensions are only
# fatal when the requested final export is USDZ.
USDZ_TEXTURE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".exr", ".avif"})
_KNOWN_IMAGE_EXTENSIONS = USDZ_TEXTURE_EXTENSIONS | frozenset(
    {".bmp", ".gif", ".hdr", ".psd", ".tga", ".tif", ".tiff", ".webp"}
)

# These schemas are documented as unsupported by the RealityKit USD importer.
# ParticleField is standardized in OS 27, but it does not yet have a shipping
# USD-to-RealityKit Gaussian-splat import path, so the strict profile must not
# imply that schema availability is runtime rendering support.
UNSUPPORTED_REALITYKIT_PRIM_TYPES = {
    "BasisCurves": "convert curves to polygon meshes before export",
    "NurbsCurves": "convert NURBS curves to polygon meshes before export",
    "NurbsPatch": "convert NURBS surfaces to polygon meshes before export",
    "Points": "convert points to polygon meshes before export",
    "PointInstancer": "realize instances or export ordinary mesh instances",
    "ParticleField": (
        "convert particle fields to polygon meshes or create Gaussian splats "
        "with RealityKit runtime APIs instead of USD import"
    ),
    "TetMesh": "convert tetrahedral volume data to a polygon surface mesh",
    "Volume": "convert volume data to polygon meshes before export",
    "OpenVDBAsset": "convert OpenVDB fields to polygon meshes before export",
    "DistantLight": "author lighting in Reality Composer Pro or RealityKit",
    "SphereLight": "author lighting in Reality Composer Pro or RealityKit",
    "DiskLight": "author lighting in Reality Composer Pro or RealityKit",
    "RectLight": "author lighting in Reality Composer Pro or RealityKit",
    "CylinderLight": "author lighting in Reality Composer Pro or RealityKit",
    "DomeLight": "author environment lighting in Reality Composer Pro or RealityKit",
    "PortalLight": "author lighting in Reality Composer Pro or RealityKit",
    "GeometryLight": "author lighting in Reality Composer Pro or RealityKit",
    "Camera": "author cameras in Reality Composer Pro or RealityKit",
}

_COLOR_INPUT_TERMS = (
    "basecolor",
    "base_color",
    "diffusecolor",
    "diffuse_color",
    "emissioncolor",
    "emission_color",
    "emissivecolor",
    "emissive_color",
    "subsurfacecolor",
    "subsurface_color",
    "transmissioncolor",
    "transmission_color",
    "sheencolor",
    "sheen_color",
    "coatcolor",
    "coat_color",
    "unlitcolor",
    "unlit_color",
)
_DATA_INPUT_TERMS = (
    "roughness",
    "metallic",
    "metalness",
    "normal",
    "occlusion",
    "ambientocclusion",
    "ambient_occlusion",
    "displacement",
    "height",
    "thickness",
    "anisotropy",
)
# MaterialX distinguishes color values from scalar/vector data. A perceptual
# color texture may be authored either in an sRGB encoding or as already-linear
# Rec.709 scene color; neither contract is appropriate for roughness, normal,
# metallic, or other data inputs. This set is the intersection of what the
# exporter may legitimately author and what Reality Composer Pro 3 can decode:
# its CoreRE engine aliases ``srgb_texture``, ``srgb_rec709_scene`` and the
# ``lin_rec709*`` family, but has no mapping at all for Blender's OCIO name
# ``srgb_rec709_display`` — the postprocess retags that token to
# ``srgb_rec709_scene`` before this gate runs, so accepting it here would hide
# a failure of that rewrite. The prim-level ColorSpaceAPI token has to
# be one ``UsdColorSpaceAPI`` resolves, which ``srgb_texture`` is not; it stays
# correct as MaterialX metadata on an image node's file input.
_COLOR_TEXTURE_COLOR_SPACES = frozenset(
    {
        "srgb",
        "srgb_texture",
        "srgbtexture",
        "srgb_rec709_scene",
        "srgbrec709scene",
        "lin_rec709",
        "linrec709",
        "lin_rec709_scene",
        "linrec709scene",
    }
)
_DATA_TEXTURE_COLOR_SPACES = frozenset({"raw", "data", "none"})

#: MaterialX nodedefs the manifest carries and that no policy flag rejects,
#: but that Reality Composer Pro 3 cannot instantiate: a
#: material containing one is replaced wholesale by a striped placeholder.
#: Every four-channel read is authored as color4 instead.
RCP_UNSUPPORTED_NODEDEFS = frozenset(
    {
        "ND_image_vector4",
        "ND_swizzle_vector4_float",
        "ND_extract_vector4",
        "ND_separate4_vector4",
    }
)

#: Color-space tokens the exporter must never author on a MaterialX image
#: reader. RealityKit has no mapping for the lowercase ``raw`` token, and no
#: shipping RealityKit package uses it. A data texture authors no color space
#: at all, which is MaterialX's no-transform contract.
_UNSUPPORTED_FILE_COLOR_SPACES = frozenset({"raw"})


@dataclass(frozen=True)
class PreflightIssue:
    """One actionable RealityKit asset finding."""

    severity: str
    code: str
    message: str
    prim_path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.prim_path:
            result["prim_path"] = self.prim_path
        if self.details:
            result["details"] = self.details
        return result

    def format(self) -> str:
        location = f" {self.prim_path}" if self.prim_path else ""
        formatted = f"RealityKit preflight [{self.code}]{location}: {self.message}"
        contexts = self.details.get("variant_contexts")
        if not contexts:
            return formatted
        first_context = contexts[0]
        selections = ", ".join(
            (
                f"{entry['prim_path']}"
                f"{{{entry['variant_set']}={entry.get('selection') or '<unselected>'}}}"
            )
            for entry in first_context
        )
        suffix = f" Variant context: {selections or '<default composition>'}."
        if len(contexts) > 1:
            suffix += f" (+{len(contexts) - 1} more)"
        return formatted + suffix


@dataclass
class RealityKitPreflightReport:
    """Structured result returned by :func:`validate_stage`."""

    asset_path: str | None = None
    profile: str = TARGET_PROFILE
    issues: list[PreflightIssue] = field(default_factory=list)

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        prim_path: object | None = None,
        **details: Any,
    ) -> None:
        self.issues.append(
            PreflightIssue(
                severity=severity,
                code=code,
                message=message,
                prim_path=str(prim_path) if prim_path else None,
                details={key: _plain_value(value) for key, value in details.items()},
            )
        )

    @property
    def errors(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def infos(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "info"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "asset_path": self.asset_path,
            "ok": self.ok,
            "counts": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "info": len(self.infos),
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }


def validate_stage(
    stage,
    usd_path: str | Path | None = None,
    settings=None,
    diagnostics=None,
) -> RealityKitPreflightReport:
    """Validate a composed stage against the strict Apple OS 27 profile.

    The function does not raise for asset findings. It returns a structured
    report and, when ``diagnostics`` is supplied, records errors and warnings
    using the existing export diagnostics contract. The caller can therefore
    fail the export using the same ``diagnostics.data['errors']`` gate as the
    material pipeline.
    """
    asset_path = str(usd_path) if usd_path is not None else None
    report = RealityKitPreflightReport(asset_path=asset_path)

    _check_stage_metadata(stage, report)
    prims = _preflight_prims(stage)
    variant_sites = _variant_sites(prims)
    if variant_sites:
        _check_variant_combinations(
            stage,
            report,
            asset_path,
            settings,
        )
    else:
        _check_composed_stage(stage, prims, report, asset_path, settings)

    if diagnostics is not None:
        _record_diagnostics(diagnostics, report)
    return report


def _check_composed_stage(
    stage,
    prims: Iterable[Any],
    report: RealityKitPreflightReport,
    asset_path: str | None,
    settings,
) -> None:
    """Run all checks whose result can change with a variant selection."""

    prims = list(prims)
    _check_uninspectable_variant_sets(prims, report)
    _check_prim_types(prims, report)
    _check_meshes(prims, report, settings)
    _check_material_bindings(prims, report)
    _check_material_texture_transforms(prims, report)
    _check_materialx_nodedefs(prims, report)
    _check_materialx_node_inputs(prims, report)
    _check_geompropvalue_primvar_types(prims, report)
    _check_skeletons(stage, prims, report)
    _check_textures(prims, report, asset_path, settings)
    _check_accessibility(stage, prims, report, settings)


@dataclass(frozen=True)
class _VariantSite:
    prim_path: str
    variant_set: str
    variant_names: tuple[str, ...]
    selection: str


def _variant_sites(prims: Iterable[Any]) -> dict[tuple[str, str], _VariantSite]:
    """Return authored variant sets visible in one composed stage state."""

    sites: dict[tuple[str, str], _VariantSite] = {}
    for prim in prims:
        # Variant selections cannot be authored on generated prototype prims.
        # Their current composition is still checked, and the explicit
        # fail-closed finding from _check_uninspectable_variant_sets prevents
        # inactive branches from being mistaken for exhaustive coverage.
        if prim.IsInPrototype():
            continue
        variant_sets = prim.GetVariantSets()
        for set_name in sorted(str(name) for name in variant_sets.GetNames()):
            variant_set = variant_sets.GetVariantSet(set_name)
            variant_names = tuple(
                sorted(str(name) for name in variant_set.GetVariantNames())
            )
            if not variant_names:
                continue
            prim_path = str(prim.GetPath())
            sites[(prim_path, set_name)] = _VariantSite(
                prim_path=prim_path,
                variant_set=set_name,
                variant_names=variant_names,
                selection=str(variant_set.GetVariantSelection() or ""),
            )
    return sites


def _check_variant_combinations(
    source_stage,
    report: RealityKitPreflightReport,
    asset_path: str | None,
    settings,
) -> None:
    """Validate every reachable authored variant combination transactionally.

    Each composition is opened with the caller's session layer mounted
    read-only beneath a private anonymous override. Variant selections are
    authored only into that override, so the source stage remains byte-for-byte
    unchanged on success, validation failure, limit failure, or exception.

    A breadth-first state walk is used instead of a one-time product so variant
    sets introduced by another variant (nested product/configuration variants)
    are discovered and validated too.
    """

    pending: deque[dict[tuple[str, str], str]] = deque([{}])
    queued_requests: set[tuple[tuple[str, str, str], ...]] = {()}
    seen_compositions: set[tuple[tuple[str, str, str], ...]] = set()
    issue_indices: dict[tuple[Any, ...], int] = {}

    while pending:
        requested_context = pending.popleft()
        variant_stage = _open_variant_stage(source_stage, requested_context)
        prims = _preflight_prims(variant_stage)
        sites = _variant_sites(prims)
        effective_context = {
            key: site.selection for key, site in sites.items()
        }
        composition_key = _variant_context_key(effective_context)
        if composition_key in seen_compositions:
            continue

        if len(seen_compositions) >= MAX_VARIANT_COMBINATIONS:
            _report_variant_limit(report, len(seen_compositions) + 1)
            return

        seen_compositions.add(composition_key)
        combination_report = RealityKitPreflightReport(asset_path=asset_path)
        _check_composed_stage(
            variant_stage,
            prims,
            combination_report,
            asset_path,
            settings,
        )
        _merge_variant_issues(
            report,
            combination_report,
            effective_context,
            issue_indices,
            variant_stage,
        )

        for key, site in sites.items():
            for selection in site.variant_names:
                if selection == site.selection:
                    continue
                neighbor = _neighbor_variant_context(
                    effective_context,
                    key,
                    selection,
                )
                request_key = _variant_context_key(neighbor)
                if request_key in queued_requests:
                    continue
                queued_requests.add(request_key)
                pending.append(neighbor)


def _neighbor_variant_context(
    context: dict[tuple[str, str], str],
    changed_key: tuple[str, str],
    selection: str,
) -> dict[tuple[str, str], str]:
    """Change one site and discard descendant choices it can recompose away."""

    changed_path = Sdf.Path(changed_key[0])
    neighbor: dict[tuple[str, str], str] = {}
    for context_key, context_selection in context.items():
        if not context_selection or context_key == changed_key:
            continue
        context_path = Sdf.Path(context_key[0])
        if context_path != changed_path and context_path.HasPrefix(changed_path):
            continue
        neighbor[context_key] = context_selection
    neighbor[changed_key] = selection
    return neighbor


def _open_variant_stage(
    source_stage,
    selections: dict[tuple[str, str], str],
):
    """Open an isolated composition and apply variant selections to it."""

    # Keep the original session layer at its original identifier/anchor so its
    # relative sublayers and asset paths resolve exactly as they do for the
    # caller. A separate strongest override is the only layer we ever edit.
    session_layer = Sdf.Layer.CreateAnonymous("realitykit-variant-session.usda")
    override_layer = Sdf.Layer.CreateAnonymous("realitykit-variant-overrides.usda")
    session_layer.subLayerPaths = [
        override_layer.identifier,
        source_stage.GetSessionLayer().identifier,
    ]
    variant_stage = Usd.Stage.OpenMasked(
        source_stage.GetRootLayer(),
        session_layer,
        source_stage.GetPathResolverContext(),
        source_stage.GetPopulationMask(),
        Usd.Stage.LoadAll,
    )
    if not variant_stage:
        raise RuntimeError("Could not open an isolated stage for variant preflight")

    for layer_identifier in source_stage.GetMutedLayers():
        variant_stage.MuteLayer(layer_identifier)
    variant_stage.SetLoadRules(source_stage.GetLoadRules())
    variant_stage.SetInterpolationType(source_stage.GetInterpolationType())
    variant_stage.SetEditTarget(override_layer)
    _apply_variant_selections(variant_stage, selections)
    return variant_stage


def _apply_variant_selections(
    stage,
    selections: dict[tuple[str, str], str],
) -> None:
    """Apply reachable selections, parents first, to the private session layer."""

    pending = sorted(
        selections.items(),
        key=lambda item: (item[0][0].count("/"), item[0][0], item[0][1]),
    )
    while pending:
        deferred: list[tuple[tuple[str, str], str]] = []
        made_progress = False
        for (prim_path, set_name), selection in pending:
            prim = stage.GetPrimAtPath(prim_path)
            if not prim:
                deferred.append(((prim_path, set_name), selection))
                continue
            variant_set = prim.GetVariantSets().GetVariantSet(set_name)
            if selection not in {
                str(name) for name in variant_set.GetVariantNames()
            }:
                deferred.append(((prim_path, set_name), selection))
                continue
            if not variant_set.SetVariantSelection(selection):
                deferred.append(((prim_path, set_name), selection))
                continue
            made_progress = True
        if not deferred:
            return
        if not made_progress:
            # A changed ancestor or same-prim variant can legitimately remove
            # another site captured in the previous frontier state. The
            # resulting effective composition is rediscovered and de-duplicated
            # by the caller, so stale selections are safely normalized away.
            return
        pending = deferred


def _check_uninspectable_variant_sets(
    prims: Iterable[Any],
    report: RealityKitPreflightReport,
) -> None:
    """Fail closed for variant sets nested inside generated instance prototypes."""

    for prim in prims:
        if not prim.IsInPrototype():
            continue
        variant_sets = prim.GetVariantSets()
        for set_name in sorted(str(name) for name in variant_sets.GetNames()):
            variant_set = variant_sets.GetVariantSet(set_name)
            variant_names = sorted(
                str(name) for name in variant_set.GetVariantNames()
            )
            if not variant_names:
                continue
            report.add(
                "error",
                "VARIANT_SET_UNINSPECTABLE",
                (
                    "A variant set nested inside an instance prototype cannot be "
                    "selected transactionally; realize the instance or move the "
                    "variant set to its public instance root before export."
                ),
                _canonical_prototype_path(prim.GetStage(), str(prim.GetPath())),
                variant_set=set_name,
                variant_names=variant_names,
                current_selection=(
                    str(variant_set.GetVariantSelection() or "") or None
                ),
            )


def _variant_context_key(
    context: dict[tuple[str, str], str],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (path, set_name, selection)
            for (path, set_name), selection in context.items()
        )
    )


def _variant_context_details(
    context: dict[tuple[str, str], str],
) -> list[dict[str, str | None]]:
    return [
        {
            "prim_path": path,
            "variant_set": set_name,
            "selection": selection or None,
        }
        for path, set_name, selection in _variant_context_key(context)
    ]


def _merge_variant_issues(
    target: RealityKitPreflightReport,
    source: RealityKitPreflightReport,
    context: dict[tuple[str, str], str],
    issue_indices: dict[tuple[Any, ...], int],
    stage,
) -> None:
    """Merge identical findings and retain every composition that exposed them."""

    context_details = _variant_context_details(context)
    for issue in source.issues:
        prim_path = _canonical_prototype_path(stage, issue.prim_path)
        canonical_details = _canonicalize_prototype_paths(stage, issue.details)
        issue_key = (
            issue.severity,
            issue.code,
            issue.message,
            prim_path,
            _freeze_detail(canonical_details),
        )
        existing_index = issue_indices.get(issue_key)
        if existing_index is not None:
            contexts = target.issues[existing_index].details["variant_contexts"]
            if context_details not in contexts:
                contexts.append(context_details)
            continue

        details = dict(canonical_details)
        details["variant_contexts"] = [context_details]
        issue_indices[issue_key] = len(target.issues)
        target.issues.append(
            PreflightIssue(
                severity=issue.severity,
                code=issue.code,
                message=issue.message,
                prim_path=prim_path,
                details=details,
            )
        )


def _canonical_prototype_path(stage, value: str | None) -> str | None:
    """Map generated prototype paths to a stable public instance namespace."""

    if not value or not str(value).startswith("/__Prototype_"):
        return value
    try:
        path = Sdf.Path(str(value))
        prim_path = path.GetPrimPath()
        prim = stage.GetPrimAtPath(prim_path)
    except Exception:
        return value
    if not prim or not prim.IsInPrototype():
        return value

    prototype = prim
    while prototype and not prototype.IsPrototype():
        prototype = prototype.GetParent()
    if not prototype:
        return value
    instances = sorted(prototype.GetInstances(), key=lambda item: str(item.GetPath()))
    if not instances:
        return value
    relative = prim_path.MakeRelativePath(prototype.GetPath())
    public_path = instances[0].GetPath()
    if not relative.isEmpty:
        public_path = public_path.AppendPath(relative)
    if path.IsPropertyPath():
        public_path = public_path.AppendProperty(path.name)
    return str(public_path)


def _canonicalize_prototype_paths(stage, value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonicalize_prototype_paths(stage, item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_prototype_paths(stage, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_canonicalize_prototype_paths(stage, item) for item in value)
    if isinstance(value, str):
        return _canonical_prototype_path(stage, value)
    return value


def _freeze_detail(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(
            sorted(
                (str(key), _freeze_detail(item))
                for key, item in value.items()
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_detail(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_detail(item) for item in value))
    return value


def _report_variant_limit(
    report: RealityKitPreflightReport,
    discovered_combinations: int,
) -> None:
    report.add(
        "error",
        "VARIANT_VALIDATION_LIMIT",
        (
            "Authored variant combinations exceed the exhaustive RealityKit "
            f"preflight limit of {MAX_VARIANT_COMBINATIONS}; split the asset or "
            "flatten variant dimensions before export."
        ),
        limit=MAX_VARIANT_COMBINATIONS,
        discovered_combinations=discovered_combinations,
    )


def _preflight_prims(stage) -> list[Any]:
    """Return each composed prim that can affect a RealityKit asset once.

    ``UsdStage.Traverse()`` deliberately stops at instance roots.  Blender's
    default instanceable-reference export therefore hides the mesh, material,
    and texture prims below every instance from an ordinary traversal.  Visit
    each shared prototype exactly once in addition to the ordinary namespace:
    validating every instance proxy would report the same asset defect once per
    instance, while omitting prototypes would let those defects ship.

    Blender 5.2 also authors collection definitions below an abstract
    ``class \"prototypes\"`` prim. ``TraverseAll()`` is required to see that
    namespace at all. Include only active, loaded, defined class subtrees that
    an ordinary prim actually references; an unused class must not fail an
    export. When an internal class reference also creates an OpenUSD prototype,
    validate the authored class subtree and skip that equivalent prototype so
    the same defect is not reported twice.

    ``GetPrototypes()`` also returns nested and external-reference prototypes.
    De-duplicating composed prim paths keeps the helper robust if OpenUSD
    exposes a nested prototype from more than one range in a future release.
    """
    prims: list[Any] = []
    seen_paths: set[str] = set()

    def append_range(prim_range) -> None:
        for prim in prim_range:
            path = str(prim.GetPath())
            if path in seen_paths:
                continue
            seen_paths.add(path)
            prims.append(prim)

    ordinary_prims = list(stage.Traverse())
    append_range(ordinary_prims)

    all_namespace_prims = [
        prim
        for prim in stage.TraverseAll()
        if prim.IsActive() and prim.IsLoaded() and prim.IsDefined()
    ]
    abstract_roots, class_backed_prototypes = _referenced_abstract_roots(
        stage,
        all_namespace_prims,
    )
    for abstract_root in abstract_roots:
        append_range(
            prim
            for prim in all_namespace_prims
            if prim.IsAbstract() and prim.GetPath().HasPrefix(abstract_root.GetPath())
        )

    for prototype in stage.GetPrototypes():
        if str(prototype.GetPath()) in class_backed_prototypes:
            continue
        append_range(Usd.PrimRange(prototype))
    return prims


def _referenced_abstract_roots(stage, prims) -> tuple[list[Any], set[str]]:
    """Return used internal class roots and prototypes already backed by them."""
    roots: dict[str, Any] = {}
    class_backed_prototypes: set[str] = set()

    for prim in prims:
        if prim.IsAbstract():
            continue
        try:
            references = prim.GetMetadata("references")
        except Exception:
            references = None
        if not references:
            continue
        try:
            items = list(references.GetAddedOrExplicitItems())
        except Exception:
            items = []

        for reference in items:
            if str(getattr(reference, "assetPath", "") or ""):
                continue
            target_path = getattr(reference, "primPath", None)
            if not target_path or bool(getattr(target_path, "isEmpty", False)):
                continue
            target = stage.GetPrimAtPath(target_path)
            if not target or not target.IsAbstract():
                continue
            if not (target.IsActive() and target.IsLoaded() and target.IsDefined()):
                continue
            roots[str(target.GetPath())] = target
            if prim.IsInstance():
                prototype = prim.GetPrototype()
                if prototype:
                    class_backed_prototypes.add(str(prototype.GetPath()))

    return list(roots.values()), class_backed_prototypes


def _check_stage_metadata(stage, report: RealityKitPreflightReport) -> None:
    default_prim = stage.GetDefaultPrim()
    if not default_prim:
        report.add(
            "error",
            "DEFAULT_PRIM_MISSING",
            "Author a valid root-level defaultPrim for RealityKit loading.",
        )
    elif default_prim.GetPath().pathElementCount != 1:
        report.add(
            "error",
            "DEFAULT_PRIM_NOT_ROOT",
            "defaultPrim must reference a root prim.",
            default_prim.GetPath(),
        )

    authored_up_axis = bool(stage.HasAuthoredMetadata("upAxis"))
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    if not authored_up_axis:
        report.add(
            "error",
            "UP_AXIS_UNAUTHORED",
            "Explicitly author upAxis=Y; do not rely on a schema fallback.",
        )
    elif up_axis.upper() != "Y":
        report.add(
            "error",
            "UP_AXIS_NOT_Y",
            "RealityKit uses a Y-up coordinate system.",
            actual=up_axis,
        )

    authored_meters = bool(UsdGeom.StageHasAuthoredMetersPerUnit(stage))
    meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    if not authored_meters:
        report.add(
            "error",
            "METERS_PER_UNIT_UNAUTHORED",
            "Explicitly author metersPerUnit=1 for RealityKit.",
        )
    elif not math.isfinite(meters_per_unit) or meters_per_unit <= 0:
        report.add(
            "error",
            "METERS_PER_UNIT_INVALID",
            "metersPerUnit must be a finite positive value.",
            actual=meters_per_unit,
        )
    elif not math.isclose(meters_per_unit, 1.0, rel_tol=0.0, abs_tol=1e-9):
        report.add(
            "error",
            "METERS_PER_UNIT_NOT_ONE",
            "Bake source units to meters and author metersPerUnit=1.",
            actual=meters_per_unit,
        )


def _check_prim_types(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    for prim in prims:
        type_name = str(prim.GetTypeName())
        if type_name in UNSUPPORTED_REALITYKIT_PRIM_TYPES:
            report.add(
                "error",
                "UNSUPPORTED_REALITYKIT_PRIM_TYPE",
                (
                    f"{type_name} isn't supported by the RealityKit renderer; "
                    f"{UNSUPPORTED_REALITYKIT_PRIM_TYPES[type_name]}."
                ),
                prim.GetPath(),
                prim_type=type_name,
            )
        elif type_name.startswith("Preliminary_"):
            report.add(
                "warning",
                "PRELIMINARY_SCHEMA",
                "Preliminary schemas can change and need renderer-specific testing.",
                prim.GetPath(),
                prim_type=type_name,
            )


def _check_meshes(
    prims: Iterable[Any], report: RealityKitPreflightReport, settings
) -> None:
    require_lightmap_uv = bool(getattr(settings, "require_lightmap_uv", False))
    for prim in prims:
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        path = prim.GetPath()

        for attribute_name in ("points", "faceVertexCounts", "faceVertexIndices"):
            attribute = prim.GetAttribute(attribute_name)
            if not attribute or not attribute.HasAuthoredValueOpinion():
                report.add(
                    "error",
                    "MESH_TOPOLOGY_MISSING",
                    f"Mesh is missing authored {attribute_name} data.",
                    path,
                    attribute=attribute_name,
                )

        points = mesh.GetPointsAttr()
        if points and points.GetNumTimeSamples() > 0:
            report.add(
                "error",
                "VERTEX_ANIMATION_UNSUPPORTED",
                "RealityKit doesn't support time-sampled mesh points.",
                path,
                time_samples=points.GetTimeSamples(),
            )

        subdivision = mesh.GetSubdivisionSchemeAttr()
        if not subdivision.HasAuthoredValueOpinion():
            report.add(
                "error",
                "SUBDIVISION_SCHEME_UNAUTHORED",
                "Author subdivisionScheme explicitly; USD otherwise defaults to catmullClark.",
                path,
            )
        else:
            scheme = str(subdivision.Get())
            if scheme not in {"none", "catmullClark", "loop", "bilinear"}:
                report.add(
                    "error",
                    "SUBDIVISION_SCHEME_INVALID",
                    "Use a standard USD subdivision scheme.",
                    path,
                    actual=scheme,
                )
            elif scheme != "none":
                report.add(
                    "warning",
                    "SUBDIVISION_RUNTIME_COST",
                    "Runtime subdivision multiplies polygon count; confirm it is intentional.",
                    path,
                    scheme=scheme,
                )

        if bool(mesh.GetDoubleSidedAttr().Get()):
            report.add(
                "error",
                "DOUBLE_SIDED_GEOMETRY",
                "Double-sided meshes are unsupported by the portable RealityKit renderer profile.",
                path,
            )

        uv_primvars = _texture_coordinate_primvars(prim)
        uv_names = [str(primvar.GetPrimvarName()) for primvar in uv_primvars]
        if len(uv_primvars) > 2:
            report.add(
                "error",
                "TOO_MANY_UV_SETS",
                "RealityKit supports at most two texture-coordinate sets.",
                path,
                uv_sets=uv_names,
            )

        if len(uv_primvars) >= 2:
            lightmap_uv = uv_primvars[1]
            try:
                lightmap_values = lightmap_uv.ComputeFlattened()
            except Exception:
                lightmap_values = None
            if lightmap_values is None or len(lightmap_values) == 0:
                severity = "error" if require_lightmap_uv else "warning"
                report.add(
                    severity,
                    "LIGHTMAP_UV_EMPTY",
                    "The secondary UV set has no resolved coordinates.",
                    path,
                    uv_set=str(lightmap_uv.GetPrimvarName()),
                )
            else:
                report.add(
                    "info",
                    "LIGHTMAP_UV_PRESENT",
                    (
                        "A secondary UV set is present; verify non-overlap and padding "
                        "with the Reality Composer Pro lightmap baker."
                    ),
                    path,
                    uv_set=str(lightmap_uv.GetPrimvarName()),
                )
        else:
            severity = "error" if require_lightmap_uv else "info"
            report.add(
                severity,
                "LIGHTMAP_UV_MISSING",
                "Add a non-overlapping secondary UV set before baking RCP3 lightmaps.",
                path,
            )


def _texture_coordinate_primvars(prim) -> list[Any]:
    primvars = UsdGeom.PrimvarsAPI(prim).FindPrimvarsWithInheritance()
    result = []
    for primvar in primvars:
        type_name = primvar.GetTypeName()
        role = str(getattr(type_name, "role", "")).lower()
        name = str(primvar.GetPrimvarName()).lower()
        if role in {"texturecoordinate", "texcoord"}:
            result.append(primvar)
        elif type_name in {Sdf.ValueTypeNames.Float2Array, Sdf.ValueTypeNames.Half2Array}:
            if name == "st" or name.startswith("st") or name.startswith("uv"):
                result.append(primvar)
    return sorted(result, key=lambda item: str(item.GetPrimvarName()))


def _check_material_bindings(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    missing_api_paths: set[str] = set()

    def report_missing_api(owner, message: str) -> None:
        owner_path = str(owner.GetPath())
        if owner_path in missing_api_paths:
            return
        missing_api_paths.add(owner_path)
        report.add(
            "error",
            "MATERIAL_BINDING_API_MISSING",
            message,
            owner.GetPath(),
        )

    for prim in prims:
        if not (prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Subset)):
            continue
        binding_api = UsdShade.MaterialBindingAPI(prim)
        try:
            material, binding_rel = binding_api.ComputeBoundMaterial()
        except Exception:
            material, binding_rel = None, None
        direct_rel = prim.GetRelationship("material:binding")
        has_direct_binding = bool(direct_rel and direct_rel.GetTargets())

        if has_direct_binding and not _has_applied_api(prim, "MaterialBindingAPI"):
            report_missing_api(
                prim,
                "Apply MaterialBindingAPI to geometry that authors a material binding.",
            )

        if has_direct_binding and not material:
            report.add(
                "error",
                "MATERIAL_BINDING_INVALID",
                "Material binding doesn't resolve to a UsdShade Material.",
                prim.GetPath(),
                targets=[str(path) for path in direct_rel.GetTargets()],
            )
        elif binding_rel and material:
            owner = binding_rel.GetPrim()
            if owner and not _has_applied_api(owner, "MaterialBindingAPI"):
                report_missing_api(
                    owner,
                    "Apply MaterialBindingAPI where the inherited binding is authored.",
                )


def _check_material_texture_transforms(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    """Enforce RealityKit's one effective 2D transform per bound material.

    Source validation protects Blender-authored graphs, but USD layers,
    variants, and instance prototypes can introduce their own UsdTransform2d
    or MaterialX place2d nodes. Walk upstream from each *bound* material so an
    unused library material does not fail the asset, and de-duplicate identical
    transforms by their semantic UV source and canonical transform values.
    """

    materials: dict[str, Any] = {}
    for prim in prims:
        if not (prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Subset)):
            continue
        try:
            material, _binding = (
                UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            )
        except Exception:
            material = None
        if material:
            material_prim = material.GetPrim()
            materials.setdefault(str(material_prim.GetPath()), material_prim)

    for material_path, material_prim in sorted(materials.items()):
        # Collected separately because a material can carry *two* networks -
        # the MaterialX ShaderGraph RealityKit consumes, and a UsdPreviewSurface
        # network. This exporter deletes its own, but a referenced or
        # hand-authored layer can still carry one.
        # One Blender Mapping node then appears twice: once as a
        # MaterialX place2d (texcoord UV0, reciprocal SRT scale) and once as a
        # UsdTransform2d (texcoord st, direct scale). The two describe the same
        # transform in different conventions, so they never compare equal.
        #
        # Counted together, any non-identity Mapping node would produce two
        # distinct transforms and fail the export with a conflict the artist
        # did not create and cannot resolve.
        materialx_contracts: dict[Any, list[str]] = defaultdict(list)
        preview_contracts: dict[Any, list[str]] = defaultdict(list)
        uninspectable: list[tuple[str, str]] = []
        for prim in _upstream_material_prims(material_prim):
            shader = UsdShade.Shader(prim)
            shader_id = str(shader.GetIdAttr().Get() or "")
            lowered_id = shader_id.lower()
            is_place2d = "place2d" in lowered_id
            if not is_place2d and lowered_id != "usdtransform2d":
                continue
            try:
                contract = _usd_texture_transform_contract(shader, shader_id)
            except (TypeError, ValueError) as exc:
                uninspectable.append((str(prim.GetPath()), str(exc)))
                continue
            if contract is not None:
                bucket = materialx_contracts if is_place2d else preview_contracts
                bucket[contract].append(str(prim.GetPath()))

        # Judge the network RealityKit actually consumes. Only when a material
        # has no MaterialX transform at all does the preview network stand in -
        # that is the hand-authored-USD case this check was written for, where
        # UsdTransform2d nodes really are the effective transforms.
        contracts = materialx_contracts or preview_contracts

        if uninspectable:
            report.add(
                "error",
                "TEXTURE_TRANSFORM_UNINSPECTABLE",
                (
                    "A bound material has a dynamic or malformed 2D texture "
                    "transform, so RealityKit's one-transform limit cannot be "
                    "verified. Author constant transform values or bake the "
                    "texture mapping."
                ),
                material_prim.GetPath(),
                transforms=[
                    {"shader_path": path, "reason": reason}
                    for path, reason in sorted(uninspectable)
                ],
            )

        if len(contracts) <= 1:
            continue
        report.add(
            "error",
            "MATERIAL_TEXTURE_TRANSFORM_CONFLICT",
            (
                "RealityKit honors only the first 2D texture transform per "
                "material. Use one identical transform and UV set for every "
                "mapped texture, or bake the transforms into the images."
            ),
            material_prim.GetPath(),
            material=material_path,
            distinct_transform_count=len(contracts),
            mappings=[
                {
                    **mapping_contract_details(contract),
                    "shader_paths": sorted(paths),
                }
                for contract, paths in sorted(
                    contracts.items(), key=lambda item: repr(item[0])
                )
            ],
        )


#: MaterialX version assumed when a Material carries no ``config:mtlx:version``.
#: RealityKit's own loader says so: libtm-material carries the string
#: "unrecognized MaterialX version \"%s\"; treating as 1.38". This is the
#: loader's rule, not the exporter's default: every exported material declares
#: 1.39 explicitly, and one that carries no attribute is judged as RealityKit
#: would judge it.
_DEFAULT_MTLX_VERSION = "1.38"

_NODEDEF_INPUT_GAPS: dict[str, dict[str, frozenset[str]]] | None = None


def _nodedef_input_gaps() -> dict[str, dict[str, frozenset[str]]]:
    """Inputs our manifest declares that RealityKit's nodedef store does not.

    RealityKit keeps one nodedef store per MaterialX version, and the two
    disagree about which inputs exist: ``blend`` and ``upaxis`` are on 1.39's
    triplanar but not 1.38's, ``operationorder`` is on 1.39's place2d but not
    1.38's. Our manifest is generated from definition files and does not carry
    that split, so it can promise an input the bound nodedef has never heard of.

    Keyed by declared version, then nodedef name. Generated from the resolver
    RCP itself uses - see ``Plugin/manifest/rcp_nodedef_input_gaps.json``.
    """
    global _NODEDEF_INPUT_GAPS
    if _NODEDEF_INPUT_GAPS is None:
        try:
            import json
            from pathlib import Path

            path = (
                Path(__file__).resolve().parent.parent
                / "manifest"
                / "rcp_nodedef_input_gaps.json"
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            _NODEDEF_INPUT_GAPS = {
                version: {
                    "inputs": {
                        nodedef: frozenset(inputs)
                        for nodedef, inputs in (entries.get("inputs") or {}).items()
                    },
                    "unresolved": frozenset(entries.get("unresolved") or ()),
                }
                for version, entries in (raw.get("by_version") or {}).items()
            }
        except Exception:
            _NODEDEF_INPUT_GAPS = {}
    return _NODEDEF_INPUT_GAPS


def _declared_mtlx_version(prim: Any) -> str:
    """The MaterialX version the enclosing Material declares."""
    node = prim
    while node and node.GetPath() != node.GetPath().GetParentPath():
        material = UsdShade.Material(node)
        if material:
            attr = node.GetAttribute("config:mtlx:version")
            value = attr.Get() if attr else None
            if value:
                return str(value)
            break
        node = node.GetParent()
    return _DEFAULT_MTLX_VERSION


def _check_materialx_node_inputs(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    """No authored input may be absent from the nodedef RealityKit will bind.

    Measured with ``realitytool compile``: a single input the bound nodedef does
    not declare makes the compiler drop that material's *entire* shader graph -
    every texture binding included - and substitute a default-parameter PBR
    material. It exits 0 and prints nothing, ``usdchecker --arkit --strict``
    passes, and the loss is only visible as an untextured object in RCP.

    Scene 15 shipped two of these at once (``blend`` on a triplanar, and
    ``operationorder`` on a place2d) and lost two of its four materials.
    """
    gaps = _nodedef_input_gaps()
    if not gaps:
        report.add(
            "warning",
            "MATERIALX_NODEDEF_INPUT_TABLE_UNAVAILABLE",
            "The RealityKit nodedef input table could not be loaded, so "
            "authored shader inputs were not verified.",
            None,
        )
        return

    for prim in prims:
        shader = UsdShade.Shader(prim)
        if not shader:
            continue
        shader_id = str(shader.GetIdAttr().Get() or "")
        if not shader_id.startswith("ND_"):
            continue
        version = _declared_mtlx_version(prim)
        entries = gaps.get(version) or {}

        if shader_id in (entries.get("unresolved") or ()):
            report.add(
                "error",
                "MATERIALX_NODEDEF_ABSENT_AT_DECLARED_VERSION",
                (
                    "Shader authors a nodedef that does not exist at the "
                    "MaterialX version this material claims. RealityKit keeps a "
                    "separate nodedef store per version; a node missing from the "
                    "bound one costs the whole material its shader graph, "
                    "silently, and the object renders untextured."
                ),
                prim.GetPath(),
                nodedef=shader_id,
                mtlx_version=version,
            )
            continue

        absent = (entries.get("inputs") or {}).get(shader_id)
        if not absent:
            continue
        authored = sorted(
            name for name in (
                shader_input.GetBaseName() for shader_input in shader.GetInputs()
            ) if name in absent
        )
        if not authored:
            continue
        report.add(
            "error",
            "MATERIALX_INPUT_ABSENT_FROM_NODEDEF",
            (
                "Shader authors an input RealityKit's nodedef does not declare "
                "at the MaterialX version this material claims. RealityKit "
                "discards the whole material's shader graph when that happens, "
                "without reporting it, and the object renders untextured."
            ),
            prim.GetPath(),
            nodedef=shader_id,
            mtlx_version=version,
            unsupported_inputs=authored,
        )


_KNOWN_NODEDEF_NAMES: frozenset[str] | None = None


def _known_nodedef_names() -> frozenset[str] | None:
    """The manifest's bindable nodedef names, loaded once; None when unavailable.

    Excludes ``editor_unresolvable`` entries: those ids exist in Apple's USD
    parsing libraries (the manifest carries them for completeness) but RCP's
    ShaderGraph editor has no definition and the runtime cannot render them,
    so authoring one is exactly as broken as authoring a fabricated id.
    """
    global _KNOWN_NODEDEF_NAMES
    if _KNOWN_NODEDEF_NAMES is None:
        try:
            from ..manifest.materialx_nodes import load_manifest

            _KNOWN_NODEDEF_NAMES = frozenset(
                name
                for name, node in load_manifest().get("nodes", {}).items()
                if not (node.get("policy") or {}).get("editor_unresolvable")
            )
        except Exception:
            return None
    return _KNOWN_NODEDEF_NAMES or None


def _check_materialx_nodedefs(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    """Every authored MaterialX info:id must exist in the shipped manifest.

    This is the closing gate on nodedef validity. Selection refuses
    impossible asks upstream; this check catches any id that still reaches
    the stage. RealityKit cannot bind a shader whose nodedef does not exist,
    so an unknown id is a broken material regardless of how it was produced.

    Only ``ND_``-prefixed ids are judged: a preview network's
    UsdPreviewSurface/UsdUVTexture/UsdTransform2d ids are USD schemas, not
    MaterialX nodedefs.
    """
    known = _known_nodedef_names()
    if known is None:
        report.add(
            "warning",
            "MATERIALX_MANIFEST_UNAVAILABLE",
            "The MaterialX manifest could not be loaded, so authored "
            "info:id values were not verified.",
            None,
        )
        return

    for prim in prims:
        shader = UsdShade.Shader(prim)
        if not shader:
            continue
        shader_id = str(shader.GetIdAttr().Get() or "")
        if not shader_id.startswith("ND_"):
            continue
        if shader_id in RCP_UNSUPPORTED_NODEDEFS:
            report.add(
                "error",
                "MATERIALX_NODEDEF_UNSUPPORTED_BY_RCP",
                (
                    "Shader authors a four-channel vector reader or extractor "
                    "Reality Composer Pro replaces with a placeholder "
                    "material; read the file as color3/color4 instead."
                ),
                prim.GetPath(),
                nodedef=shader_id,
            )
            continue
        if shader_id in known:
            continue
        report.add(
            "error",
            "UNKNOWN_MATERIALX_NODEDEF",
            (
                "Shader authors an info:id that RealityKit's ShaderGraph "
                "cannot resolve; the material would fail to bind."
            ),
            prim.GetPath(),
            nodedef=shader_id,
        )


#: Channel count of each MaterialX type a ``geompropvalue`` nodedef reads.
_GEOMPROPVALUE_CHANNEL_COUNTS = {
    "float": 1,
    "integer": 1,
    "boolean": 1,
    "vector2": 2,
    "color3": 3,
    "vector3": 3,
    "color4": 4,
    "vector4": 4,
}

#: Channel count of each USD primvar array type a geometric-property read can
#: land on. Roles are deliberately collapsed: ``texCoord2f`` and ``float2``
#: hold the same two floats, so a vector2 read of a texCoord2f primvar is not
#: a mismatch. Channel *count* is what RealityKit cannot reconcile.
_PRIMVAR_CHANNEL_COUNTS = {
    "bool[]": 1, "int[]": 1, "half[]": 1, "float[]": 1, "double[]": 1,
    "half2[]": 2, "float2[]": 2, "double2[]": 2,
    "texCoord2h[]": 2, "texCoord2f[]": 2, "texCoord2d[]": 2,
    "half3[]": 3, "float3[]": 3, "double3[]": 3,
    "color3h[]": 3, "color3f[]": 3, "color3d[]": 3,
    "normal3h[]": 3, "normal3f[]": 3, "normal3d[]": 3,
    "point3h[]": 3, "point3f[]": 3, "point3d[]": 3,
    "vector3h[]": 3, "vector3f[]": 3, "vector3d[]": 3,
    "texCoord3h[]": 3, "texCoord3f[]": 3, "texCoord3d[]": 3,
    "half4[]": 4, "float4[]": 4, "double4[]": 4,
    "color4h[]": 4, "color4f[]": 4, "color4d[]": 4,
}


def _check_geompropvalue_primvar_types(
    prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    """A geometric-property read must declare the primvar's own channel count.

    Measured in Reality Composer Pro 3: a cube whose base
    colour came from a Blender Color Attribute rendered as the striped
    placeholder - the material could not be instantiated at all. The mesh
    carried ``primvars:Paint  color4f[]``, and the shader read it with
    ND_geompropvalue_color3. Same class as the ND_image_vector4-over-a-
    three-channel-PNG failure: the declared type does not describe the data
    that exists, and RCP substitutes a placeholder rather than erroring.

    Only bound materials are judged, and only when the named primvar is
    actually found on the bound geometry - a read of a primvar that reaches
    no geometry is a different (already source-refused) defect, and a
    ``geomprop`` naming something this stage does not author is not something
    this check can measure.
    """
    for prim in prims:
        if not (prim.IsA(UsdGeom.Mesh) or prim.IsA(UsdGeom.Subset)):
            continue
        try:
            material, _binding = (
                UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            )
        except Exception:
            continue
        if not material:
            continue
        primvars_api = UsdGeom.PrimvarsAPI(prim)
        for shader_prim in _upstream_material_prims(material.GetPrim()):
            shader = UsdShade.Shader(shader_prim)
            if not shader:
                continue
            shader_id = str(shader.GetIdAttr().Get() or "")
            if not shader_id.startswith("ND_geompropvalue_"):
                continue
            declared = shader_id[len("ND_geompropvalue_"):]
            declared_channels = _GEOMPROPVALUE_CHANNEL_COUNTS.get(declared)
            if declared_channels is None:
                continue
            geomprop_input = shader.GetInput("geomprop")
            geomprop = geomprop_input.Get() if geomprop_input else None
            if not geomprop:
                continue
            try:
                primvar = primvars_api.FindPrimvarWithInheritance(str(geomprop))
            except Exception:
                continue
            if not primvar:
                continue
            primvar_type = str(primvar.GetTypeName())
            primvar_channels = _PRIMVAR_CHANNEL_COUNTS.get(primvar_type)
            if primvar_channels is None or primvar_channels == declared_channels:
                continue
            report.add(
                "error",
                "GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH",
                (
                    f"Shader reads primvars:{geomprop} ({primvar_type}) with "
                    f"{shader_id}, whose declared type carries "
                    f"{declared_channels} channel(s) instead of "
                    f"{primvar_channels}. Reality Composer Pro replaces such "
                    "a material with a placeholder; read the primvar at its "
                    "own type and adapt with a swizzle."
                ),
                shader_prim.GetPath(),
                nodedef=shader_id,
                primvar=str(geomprop),
                primvar_type=primvar_type,
                geometry=str(prim.GetPath()),
            )


def _upstream_material_prims(material_prim) -> list[Any]:
    """Return connectable prims reachable upstream from a material output."""

    queue = deque([material_prim])
    result = []
    seen: set[str] = set()
    while queue:
        prim = queue.popleft()
        path = str(prim.GetPath())
        if path in seen:
            continue
        seen.add(path)
        result.append(prim)
        connectable = UsdShade.ConnectableAPI(prim)
        if not connectable:
            continue
        properties = list(connectable.GetInputs()) + list(connectable.GetOutputs())
        for shader_property in properties:
            for source_prim in _connected_source_prims(shader_property):
                queue.append(source_prim)
    return result


def _connected_source_prims(shader_property) -> list[Any]:
    try:
        result = shader_property.GetConnectedSources()
    except Exception:
        return []
    infos = result[0] if isinstance(result, tuple) else result
    prims = []
    for info in infos or []:
        source = getattr(info, "source", None)
        if source is None and isinstance(info, tuple) and info:
            source = info[0]
        try:
            prim = source.GetPrim()
        except Exception:
            continue
        if prim:
            prims.append(prim)
    return prims


def _constant_shader_input(shader, name: str, default):
    shader_input = shader.GetInput(name)
    if not shader_input:
        return default
    if _shader_property_connection_paths(shader_input):
        raise ValueError(f"input '{name}' is connected instead of constant")
    attribute = shader_input.GetAttr()
    try:
        if attribute.GetNumTimeSamples() > 0:
            raise ValueError(f"input '{name}' is time sampled")
    except AttributeError:
        pass
    value = shader_input.Get()
    return default if value is None else value


def _texture_coordinate_semantic(shader, input_name: str) -> str:
    shader_input = shader.GetInput(input_name)
    if not shader_input:
        return "<default>"
    connection_paths = _shader_property_connection_paths(shader_input)
    sources = _connected_source_prims(shader_input)
    if not sources:
        if connection_paths:
            raise ValueError(
                f"input '{input_name}' has an unresolved texture-coordinate source"
            )
        return "<default>"
    if len(sources) != 1:
        raise ValueError(
            f"input '{input_name}' has {len(sources)} texture-coordinate sources"
        )

    source = UsdShade.Shader(sources[0])
    source_id = str(source.GetIdAttr().Get() or "")
    if "texcoord" in source_id.lower():
        return "UV0"
    for name in ("geomprop", "varname"):
        source_input = source.GetInput(name)
        if not source_input:
            continue
        if _shader_property_connection_paths(source_input):
            raise ValueError(
                f"texture-coordinate source input '{name}' is connected"
            )
        value = source_input.Get()
        if value is not None and str(value):
            return str(value)
    # Preserve a stable distinction when an unknown source node cannot be
    # reduced to a semantic primvar contract.
    return f"{source_id or '<unknown>'}@{sources[0].GetPath()}"


def _shader_property_connection_paths(shader_property) -> list[Any]:
    try:
        return list(shader_property.GetAttr().GetConnections())
    except Exception:
        return []


def _usd_texture_transform_contract(shader, shader_id: str):
    lowered_id = shader_id.lower()
    if "place2d" in lowered_id:
        mapping = {
            "offset": _constant_shader_input(shader, "offset", (0.0, 0.0)),
            "scale": _constant_shader_input(shader, "scale", (1.0, 1.0)),
            # MaterialX authors angle inputs in degrees. The shared contract
            # stores Blender/radian values, so normalize before comparison.
            "rotate": math.radians(
                float(_constant_shader_input(shader, "rotate", 0.0))
            ),
            "pivot": _constant_shader_input(shader, "pivot", (0.0, 0.0)),
            "operationorder": _constant_shader_input(
                shader, "operationorder", 0
            ),
        }
        texcoord = _texture_coordinate_semantic(shader, "texcoord")
    elif lowered_id == "usdtransform2d":
        mapping = {
            "offset": _constant_shader_input(
                shader, "translation", (0.0, 0.0)
            ),
            "scale": _constant_shader_input(shader, "scale", (1.0, 1.0)),
            "rotate": math.radians(
                float(_constant_shader_input(shader, "rotation", 0.0))
            ),
            "pivot": (0.0, 0.0),
            "operationorder": 0,
        }
        texcoord = _texture_coordinate_semantic(shader, "in")
    else:  # pragma: no cover - caller filters ids
        return None
    return authored_texture_mapping_contract(mapping, texcoord)


def _check_skeletons(
    stage, prims: Iterable[Any], report: RealityKitPreflightReport
) -> None:
    skeletons = [prim for prim in prims if str(prim.GetTypeName()) == "Skeleton"]
    if len(skeletons) > 1:
        report.add(
            "error",
            "MULTIPLE_SKELETONS",
            "Merge rigs into one skeleton hierarchy for RealityKit.",
            skeletons=[str(prim.GetPath()) for prim in skeletons],
        )

    for skeleton in skeletons:
        if not _has_ancestor_type(skeleton, "SkelRoot"):
            report.add(
                "error",
                "SKELETON_OUTSIDE_SKEL_ROOT",
                "Place Skeleton and bound meshes beneath a SkelRoot.",
                skeleton.GetPath(),
            )
        # ``joints`` is the typed UsdSkel Skeleton attribute. Some interchange
        # tools also author the binding namespace's ``skel:joints`` primvar, so
        # accept it only as a fallback.
        joints = skeleton.GetAttribute("joints")
        if not joints or not joints.Get():
            joints = skeleton.GetAttribute("skel:joints")
        if not joints or not joints.Get():
            report.add(
                "error",
                "SKELETON_JOINTS_MISSING",
                "Skeleton must author a non-empty skel:joints array.",
                skeleton.GetPath(),
            )

    for prim in prims:
        if not prim.IsA(UsdGeom.Mesh):
            continue
        indices = prim.GetAttribute("primvars:skel:jointIndices")
        weights = prim.GetAttribute("primvars:skel:jointWeights")
        has_indices = bool(indices and indices.HasAuthoredValueOpinion())
        has_weights = bool(weights and weights.HasAuthoredValueOpinion())
        if not (has_indices or has_weights):
            continue
        if has_indices != has_weights:
            report.add(
                "error",
                "SKINNING_PRIMVARS_INCOMPLETE",
                "Skinned meshes require both jointIndices and jointWeights.",
                prim.GetPath(),
            )
        if not _has_applied_api(prim, "SkelBindingAPI"):
            report.add(
                "error",
                "SKEL_BINDING_API_MISSING",
                "Apply SkelBindingAPI to every skinned mesh.",
                prim.GetPath(),
            )

        skeleton_targets = _inherited_relationship_targets(prim, "skel:skeleton")
        if len(skeleton_targets) != 1:
            report.add(
                "error",
                "SKELETON_BINDING_INVALID",
                "A skinned mesh must resolve exactly one skel:skeleton target.",
                prim.GetPath(),
                targets=[str(path) for path in skeleton_targets],
            )
            continue
        target = stage.GetPrimAtPath(skeleton_targets[0])
        if not target or str(target.GetTypeName()) != "Skeleton":
            report.add(
                "error",
                "SKELETON_TARGET_INVALID",
                "skel:skeleton must target a valid Skeleton prim.",
                prim.GetPath(),
                target=str(skeleton_targets[0]),
            )

    if UsdSkel is None and skeletons:
        report.add(
            "warning",
            "USDSKEL_SCHEMA_UNAVAILABLE",
            "UsdSkel bindings could only be checked structurally in this environment.",
        )


def _check_textures(
    prims: Iterable[Any],
    report: RealityKitPreflightReport,
    asset_path: str | None,
    settings,
) -> None:
    prims = list(prims)
    consumers = _connection_consumers(prims)
    export_format = str(getattr(settings, "export_format", "") or "").upper()
    target_is_usdz = export_format == "USDZ" or bool(
        asset_path and Path(asset_path).suffix.lower() == ".usdz"
    )
    checked_properties: set[str] = set()

    for prim in prims:
        connectable = UsdShade.ConnectableAPI(prim)
        if not connectable:
            continue
        for shader_input in connectable.GetInputs():
            value = shader_input.Get()
            if not isinstance(value, Sdf.AssetPath) or not value.path:
                continue
            checked_properties.add(str(shader_input.GetAttr().GetPath()))
            authored_path = str(value.path)
            prim_path = prim.GetPath()
            extension = _asset_extension(authored_path)

            if target_is_usdz and extension not in USDZ_TEXTURE_EXTENSIONS:
                report.add(
                    "error",
                    "USDZ_TEXTURE_FORMAT_UNSUPPORTED",
                    "USDZ textures must be JPEG, PNG, EXR, or AVIF.",
                    prim_path,
                    texture=authored_path,
                    extension=extension or None,
                )
            if target_is_usdz and (Path(authored_path).is_absolute() or "://" in authored_path):
                report.add(
                    "error",
                    "USDZ_TEXTURE_PATH_EXTERNAL",
                    "USDZ texture paths must be localized and relative.",
                    prim_path,
                    texture=authored_path,
                )

            if asset_path and not _asset_exists(value, authored_path, asset_path):
                report.add(
                    "error",
                    "TEXTURE_ASSET_MISSING",
                    "Texture dependency doesn't resolve after asset staging.",
                    prim_path,
                    texture=authored_path,
                )

            roles = _downstream_texture_roles(str(prim_path), consumers)
            color_space = _texture_color_space(shader_input, connectable)
            normalized_space = _normalize_color_space(color_space)
            shader_id = str(UsdShade.Shader(prim).GetIdAttr().Get() or "")
            is_materialx_reader = shader_id.startswith("ND_image")
            if shader_id == "ND_image_color4":
                # A four-channel read is only correct over a file that
                # carries a fourth channel. Staging may transcode between
                # authoring and here, so the count is measured on the file
                # this stage actually points at.
                channels = _source_channel_count(value, authored_path, asset_path)
                if channels in (1, 3):
                    report.add(
                        "warning",
                        "TEXTURE_ALPHA_SOURCE_MISSING",
                        (
                            "A four-channel reader points at a texture with "
                            "no alpha channel; the alpha it samples is "
                            "undefined."
                        ),
                        prim_path,
                        texture=authored_path,
                        actual=channels,
                        expected=4,
                    )
            own_space = _normalize_color_space(
                _attribute_color_space(shader_input)
            )
            if is_materialx_reader and own_space in _UNSUPPORTED_FILE_COLOR_SPACES:
                report.add(
                    "error",
                    "TEXTURE_COLOR_SPACE_UNSUPPORTED_TOKEN",
                    (
                        "RealityKit has no mapping for this color-space token "
                        "on a MaterialX image reader. A data texture authors "
                        "no color space at all."
                    ),
                    prim_path,
                    texture=authored_path,
                    actual=color_space or None,
                    expected="no authored color space",
                )
            if is_materialx_reader:
                # The exporter authors each MaterialX reader with the colour
                # space Blender decodes its image with, and Cycles decodes an
                # sRGB image the same way whichever inputs it feeds. So the
                # consumers say nothing about the right token; only a token
                # RealityKit cannot map is wrong.
                if normalized_space and normalized_space not in (
                    _COLOR_TEXTURE_COLOR_SPACES | _DATA_TEXTURE_COLOR_SPACES
                ):
                    report.add(
                        "error",
                        "TEXTURE_COLOR_SPACE_MISMATCH",
                        "RealityKit maps no such color space for an image reader.",
                        prim_path,
                        texture=authored_path,
                        actual=color_space or None,
                        expected="sRGB, linear Rec.709 or no color space",
                    )
            elif roles == {"srgb", "linear"}:
                report.add(
                    "error",
                    "TEXTURE_COLOR_ROLES_CONFLICT",
                    "One texture node feeds both perceptual-color and linear-data inputs.",
                    prim_path,
                    texture=authored_path,
                )
            elif roles == {"srgb"}:
                if normalized_space not in _COLOR_TEXTURE_COLOR_SPACES:
                    report.add(
                        "error",
                        "TEXTURE_COLOR_SPACE_MISMATCH",
                        (
                            "Base, emissive, and other perceptual color textures "
                            "must use an authored sRGB or linear Rec.709 color space."
                        ),
                        prim_path,
                        texture=authored_path,
                        actual=color_space or None,
                        expected="sRGB or linear Rec.709",
                    )
            elif roles == {"linear"}:
                # A MaterialX image reader that authors no color space of its
                # own already states the data contract: an absent MaterialX
                # color space is the no-transform case. Judging the inherited
                # material-level ColorSpaceAPI opinion instead would reject
                # every correctly authored data texture.
                data_contract_satisfied = (
                    normalized_space in _DATA_TEXTURE_COLOR_SPACES
                    or (is_materialx_reader and not own_space)
                )
                if not data_contract_satisfied:
                    report.add(
                        "error",
                        "TEXTURE_COLOR_SPACE_MISMATCH",
                        "Normal, roughness, metallic, and occlusion textures must be linear data.",
                        prim_path,
                        texture=authored_path,
                        actual=color_space or None,
                        expected="raw/data",
                    )
            elif not normalized_space:
                report.add(
                    "info",
                    "TEXTURE_COLOR_ROLE_UNRESOLVED",
                    "Color role couldn't be inferred; verify the texture import color space.",
                    prim_path,
                    texture=authored_path,
                )

    # Dome lights and other image-bearing schemas aren't UsdShade
    # connectables. Validate their asset-valued image attributes as well, while
    # leaving non-image references, payloads, and audio files alone.
    for prim in prims:
        for attribute in prim.GetAttributes():
            if str(attribute.GetPath()) in checked_properties:
                continue
            value = attribute.Get()
            if not isinstance(value, Sdf.AssetPath) or not value.path:
                continue
            authored_path = str(value.path)
            extension = _asset_extension(authored_path)
            if extension not in _KNOWN_IMAGE_EXTENSIONS:
                continue
            if target_is_usdz and extension not in USDZ_TEXTURE_EXTENSIONS:
                report.add(
                    "error",
                    "USDZ_TEXTURE_FORMAT_UNSUPPORTED",
                    "USDZ textures must be JPEG, PNG, EXR, or AVIF.",
                    prim.GetPath(),
                    texture=authored_path,
                    extension=extension,
                )
            if target_is_usdz and (
                Path(authored_path).is_absolute() or "://" in authored_path
            ):
                report.add(
                    "error",
                    "USDZ_TEXTURE_PATH_EXTERNAL",
                    "USDZ texture paths must be localized and relative.",
                    prim.GetPath(),
                    texture=authored_path,
                )
            if asset_path and not _asset_exists(value, authored_path, asset_path):
                report.add(
                    "error",
                    "TEXTURE_ASSET_MISSING",
                    "Texture dependency doesn't resolve after asset staging.",
                    prim.GetPath(),
                    texture=authored_path,
                )


def _connection_consumers(prims: Iterable[Any]) -> dict[str, list[tuple[str, str]]]:
    consumers: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for prim in prims:
        connectable = UsdShade.ConnectableAPI(prim)
        if not connectable:
            continue
        destination = str(prim.GetPath())
        destinations = list(connectable.GetInputs()) + list(connectable.GetOutputs())
        for shader_property in destinations:
            for source_path in _connected_source_paths(shader_property):
                consumers[source_path].append(
                    (destination, str(shader_property.GetBaseName()))
                )
    return consumers


def _connected_source_paths(shader_input) -> list[str]:
    try:
        result = shader_input.GetConnectedSources()
    except Exception:
        return []
    infos = result[0] if isinstance(result, tuple) else result
    paths: list[str] = []
    for info in infos or []:
        source = getattr(info, "source", None)
        if source is None and isinstance(info, tuple) and info:
            source = info[0]
        try:
            paths.append(str(source.GetPrim().GetPath()))
        except Exception:
            continue
    return paths


def _downstream_texture_roles(
    source_path: str, consumers: dict[str, list[tuple[str, str]]]
) -> set[str]:
    roles: set[str] = set()
    queue = deque([source_path])
    visited: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        for destination, input_name in consumers.get(current, []):
            normalized = input_name.replace(" ", "").lower()
            if any(term in normalized for term in _COLOR_INPUT_TERMS):
                roles.add("srgb")
            if any(term in normalized for term in _DATA_INPUT_TERMS):
                roles.add("linear")
            queue.append(destination)
    return roles


def _source_channel_count(value, authored_path: str, usd_path: str | None) -> int | None:
    """Read a texture's channel count from its file header, best effort.

    Returns None whenever the file cannot be located or its format carries no
    cheap channel-count header, so an unknown count never raises a finding.
    """
    if any(token in authored_path for token in ("<UDIM>", "#", "$F", "[")):
        return None
    resolved = str(getattr(value, "resolvedPath", "") or "")
    if resolved:
        candidate = Path(resolved)
    else:
        if "://" in authored_path:
            return None
        candidate = Path(authored_path)
        if not candidate.is_absolute():
            if not usd_path:
                return None
            candidate = Path(usd_path).parent / candidate
    try:
        with candidate.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return None
    if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 26:
        # IHDR colour type: 0 gray, 2 RGB, 4 gray+alpha, 6 RGBA. Type 3
        # (palette) can carry alpha in a tRNS chunk, so it stays unknown.
        return {0: 1, 2: 3, 4: 2, 6: 4}.get(header[25])
    return None


def _attribute_color_space(shader_input) -> str:
    """Return only the color space authored on the attribute itself."""
    try:
        return str(shader_input.GetAttr().GetColorSpace() or "")
    except Exception:
        return ""


def _texture_color_space(shader_input, connectable) -> str:
    attribute = shader_input.GetAttr()
    try:
        color_space = str(attribute.GetColorSpace() or "")
    except Exception:
        color_space = ""
    if color_space:
        return color_space

    # ColorSpaceAPI is the standard USD contract Blender 5.2 uses on shader,
    # material, and root prims. Read its exact authored token first, even when
    # a particular OpenUSD build doesn't register a matching GfColorSpace and
    # ComputeColorSpaceName would discard the token — judging the literal
    # authored name is the whole point of this gate.
    color_space = _authored_color_space_api_name(connectable.GetPrim())
    if color_space:
        return color_space

    # Let OpenUSD resolve definition-backed or otherwise computed color spaces
    # when there is no directly authored API opinion in the prim ancestry.
    try:
        color_space_api = getattr(Usd, "ColorSpaceAPI", None)
        if color_space_api is not None:
            color_space = str(
                color_space_api.ComputeColorSpaceName(attribute, None) or ""
            )
    except Exception:
        color_space = ""
    if color_space:
        return color_space

    for name in ("sourceColorSpace", "colorspace", "colorSpace"):
        candidate = connectable.GetInput(name)
        if candidate:
            value = candidate.Get()
            if value is not None:
                return str(value)
    return ""


def _authored_color_space_api_name(prim) -> str:
    """Return the nearest authored ColorSpaceAPI token in ``prim`` ancestry."""
    current = prim
    while current and not current.IsPseudoRoot():
        if _has_applied_api(current, "ColorSpaceAPI"):
            attribute = current.GetAttribute("colorSpace:name")
            if attribute and attribute.HasAuthoredValueOpinion():
                value = attribute.Get()
                if value is not None:
                    return str(value).strip()
        current = current.GetParent()
    return ""


def _normalize_color_space(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "")


def _asset_extension(path: str) -> str:
    cleaned = path.split("?", 1)[0].replace("<UDIM>", "1001")
    return Path(cleaned).suffix.lower()


def _asset_exists(value, authored_path: str, usd_path: str) -> bool:
    if "[" in authored_path and "]" in authored_path:
        return True
    if any(token in authored_path for token in ("<UDIM>", "#", "$F")):
        # Expanded dependency validation belongs to the packaging validator.
        return True
    resolved = str(getattr(value, "resolvedPath", "") or "")
    if resolved:
        try:
            return Path(resolved).exists()
        except OSError:
            return False
    if "://" in authored_path:
        return False
    candidate = Path(authored_path)
    if not candidate.is_absolute():
        candidate = Path(usd_path).parent / candidate
    try:
        return candidate.exists()
    except OSError:
        return False


def _check_accessibility(
    stage,
    prims: Iterable[Any],
    report: RealityKitPreflightReport,
    settings,
) -> None:
    require_accessibility = bool(
        getattr(settings, "require_accessibility_metadata", False)
    )
    accessible = [prim for prim in prims if _has_applied_api(prim, "AccessibilityAPI")]
    default_prim = stage.GetDefaultPrim()
    if not accessible:
        report.add(
            "error" if require_accessibility else "info",
            "ACCESSIBILITY_METADATA_MISSING",
            "Add AccessibilityAPI label and description metadata to semantic assets.",
            default_prim.GetPath() if default_prim else None,
        )
        return

    for prim in accessible:
        label = _accessibility_attribute(prim, "label")
        description = _accessibility_attribute(prim, "description")
        if not label:
            report.add(
                "error" if require_accessibility else "warning",
                "ACCESSIBILITY_LABEL_MISSING",
                "AccessibilityAPI requires a concise non-empty label.",
                prim.GetPath(),
            )
        if not description:
            report.add(
                "warning",
                "ACCESSIBILITY_DESCRIPTION_MISSING",
                "Add a useful accessibility description for spatial context.",
                prim.GetPath(),
            )


def _accessibility_attribute(prim, suffix: str) -> str:
    for attribute in prim.GetAttributes():
        name = str(attribute.GetName()).lower()
        if "accessibility" in name and name.endswith(suffix.lower()):
            value = attribute.Get()
            if value is not None:
                return str(value).strip()
    return ""


def _has_applied_api(prim, schema_name: str) -> bool:
    return any(
        str(applied).split(":", 1)[0].endswith(schema_name)
        for applied in prim.GetAppliedSchemas()
    )


def _has_ancestor_type(prim, type_name: str) -> bool:
    current = prim.GetParent()
    while current and not current.IsPseudoRoot():
        if str(current.GetTypeName()) == type_name:
            return True
        current = current.GetParent()
    return False


def _inherited_relationship_targets(prim, relationship_name: str) -> list[Any]:
    current = prim
    while current and not current.IsPseudoRoot():
        relationship = current.GetRelationship(relationship_name)
        if relationship:
            targets = list(relationship.GetForwardedTargets())
            if targets:
                return targets
        current = current.GetParent()
    return []


def _record_diagnostics(diagnostics, report: RealityKitPreflightReport) -> None:
    payload = report.to_dict()
    diagnostics.data["realitykit_preflight"] = payload
    diagnostics.data.setdefault("validation", {})["realitykit"] = payload
    for issue in report.errors:
        message = issue.format()
        if message not in diagnostics.data.setdefault("errors", []):
            diagnostics.add_error(message)
    for issue in report.warnings:
        message = issue.format()
        if message not in diagnostics.data.setdefault("warnings", []):
            diagnostics.add_warning(message)


def _plain_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
