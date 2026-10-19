"""The metadata that makes an export an asset rather than a layer.

Blender's exporter writes correct contents and says nothing about what the
file *is*. A consumer cannot tell whether the prims are one model or a loose
scene, cannot look up what it is without opening it, and cannot get its size
without loading its geometry. Three pieces of root-prim metadata answer those,
and the OpenUSD asset guidelines ask for all three:

``kind``
    Marks the root as a model, so another stage can reference, instance and
    reason about it as one unit instead of as whatever prims it happens to
    contain.

``assetInfo``
    What this asset is called and where it came from, readable without
    composing the stage.

``extentsHint``
    Its bounds, so a consumer can lay it out without loading geometry. Only
    meaningful on a model, so it follows ``kind``.
"""

from __future__ import annotations

from pathlib import Path

from .usd_utils import Usd, UsdGeom, require_pxr

#: ``kind`` values this export will author. ``component`` is the default
#: because the plugin's whole subject is one asset for Reality Composer Pro;
#: a leaf model, with no models beneath it, which is exactly what we write.
KINDS = ("COMPONENT", "GROUP", "ASSEMBLY", "NONE")
_KIND_TOKENS = {"COMPONENT": "component", "GROUP": "group", "ASSEMBLY": "assembly"}


def author_asset_metadata(stage, usd_path, settings=None, diagnostics=None) -> dict:
    """Author ``kind``, ``assetInfo`` and ``extentsHint`` on the root prim.

    Returns what was authored, for the diagnostics sidecar. Authoring nothing
    is a valid outcome: ``root_prim_kind`` of ``NONE`` leaves the stage exactly
    as it was, for a pipeline that assigns model hierarchy itself.
    """
    require_pxr()

    kind = str(getattr(settings, "root_prim_kind", "COMPONENT") or "COMPONENT").upper()
    if kind not in KINDS:
        kind = "COMPONENT"
    if kind == "NONE":
        return {}

    root = stage.GetDefaultPrim()
    if not root:
        # normalize_scene guarantees one; nothing to attach metadata to.
        return {}

    authored: dict = {}

    token = _KIND_TOKENS[kind]
    Usd.ModelAPI(root).SetKind(token)
    authored["kind"] = token

    # The identifier is what a consumer references, which for a USDZ export is
    # the package, not the staged layer sitting inside it. The requested output
    # path knows that; the path being post-processed does not.
    requested = str(getattr(settings, "filepath", "") or "")
    published = Path(requested).name if requested else Path(str(usd_path)).name
    name = Path(published).stem
    identifier = published
    Usd.ModelAPI(root).SetAssetName(name)
    Usd.ModelAPI(root).SetAssetIdentifier(identifier)
    authored["asset_name"] = name
    authored["asset_identifier"] = identifier

    extent = _extents_hint(stage, root)
    if extent is not None:
        UsdGeom.ModelAPI(root).SetExtentsHint(extent)
        authored["extents_hint"] = [list(point) for point in extent]
    elif diagnostics:
        diagnostics.add_warning(
            "Asset bounds could not be computed, so the export carries no "
            "extentsHint; a consumer has to load its geometry to size it."
        )

    if diagnostics:
        diagnostics.set_asset_metadata(authored)
    return authored


def _extents_hint(stage, root):
    """Bounds of the default variant selection, in the root's own space.

    A variant-bearing asset has as many sizes as it has selections. The hint
    describes the one it ships with, which is what a consumer composes before
    it chooses anything.
    """
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.EarliestTime(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
        useExtentsHint=False,
    )
    try:
        box = cache.ComputeUntransformedBound(root)
    except Exception:
        return None
    if box.GetRange().IsEmpty():
        return None
    return UsdGeom.ModelAPI(root).ComputeExtentsHint(cache)
