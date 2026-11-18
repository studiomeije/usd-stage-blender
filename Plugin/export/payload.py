"""Splitting a finished export into an asset interface and a payload.

A single-layer export has to be loaded whole to be looked at. Split it and a
consumer can reference the asset, place it, read what it is and how big it is,
and choose a variant, all with the geometry still on disk - loading it only
when something actually has to be drawn.

The split follows the shape the OpenUSD asset guidelines describe:

    table.usdc                    the interface: root prim, kind, assetInfo,
                                  extentsHint, variant sets, and a payload arc
    assets/table.usdc/<generation>/table_payload.usdc
                                  everything else: geometry, materials, skeletons

Which half each piece belongs in is not arbitrary. Variant sets stay in the
interface because their whole purpose is to be selectable before the payload
loads; that is also why the guidelines put them on the root prim, which this
exporter already does. Geometry goes in the payload because it is the weight.

The payload layer lives in the export's own sidecar generation under
``assets/``, next to the dependencies ``usd_assets`` localized there. Publication
treats every file below ``assets/`` as immutable: it installs the new files
first, swaps the root last, and never rewrites a file in place, so a crash
leaves either the old root with the old payload or the new root with the new
one. A fixed ``assets/<name>_payload`` path would have to be rewritten on every
re-export; a generation-scoped one is simply a new file, and the previous
generation is pruned as a stale sidecar once the new root is in place.
"""

from __future__ import annotations

from pathlib import Path
import posixpath

from .asset_paths import asset_scheme
from .staging_namespace import output_sidecar_namespace
from .usd_utils import Sdf, require_pxr

#: The owned sidecar root the payload layer is written below, relative to the
#: exported stage. Publication already tracks it, so it needs no new rules.
PAYLOAD_DIRECTORY = "assets"

#: Layer metadata the payload repeats from the interface. The units keep it
#: legible opened on its own. The timing is not optional: USD scales time
#: across a composition arc whose two layers disagree on time codes per second,
#: and a payload that omits it reads as 24, so a 30 fps animation would play
#: back stretched by 30/24.
_SHARED_LAYER_METADATA = (
    "upAxis",
    "metersPerUnit",
    "kilogramsPerUnit",
    "timeCodesPerSecond",
    "framesPerSecond",
    "startTimeCode",
    "endTimeCode",
)


def split_asset_payload(stage, usd_path, settings=None, diagnostics=None) -> dict:
    """Move the geometry into a payload layer, leaving an interface behind.

    Returns what was written, or an empty mapping when the export is left
    whole. The stage is edited in place; the caller saves it as usual.
    """
    if not bool(getattr(settings, "payload_geometry", False)):
        return {}
    require_pxr()

    root_prim = stage.GetDefaultPrim()
    if not root_prim:
        return {}

    root_layer = stage.GetRootLayer()
    root_path = root_prim.GetPath()
    interface_spec = root_layer.GetPrimAtPath(root_path)
    if interface_spec is None:
        return {}
    if not interface_spec.nameChildren:
        if diagnostics:
            diagnostics.add_warning(
                "Nothing to move into a payload: the asset's root prim has no "
                "children. The export is left whole."
            )
        return {}

    stage_path = Path(str(usd_path))
    # The same <output>/<generation> namespace textures and localized
    # dependencies use, so every export attempt writes a payload no earlier
    # publication owns. Reusing one fixed name made the second export to a
    # path collide with its own immutable payload from the first.
    namespace = Path(PAYLOAD_DIRECTORY) / output_sidecar_namespace(stage_path)
    payload_dir = stage_path.parent / namespace
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = _unused_payload_path(payload_dir, stage_path)

    payload_layer = Sdf.Layer.CreateNew(str(payload_path))

    # Only the children move. Copying the root prim wholesale would bring its
    # variant sets with it, and two layers authoring the same selection means
    # the payload's composes over whichever one the consumer chose - besides
    # which Sdf will not let them be removed again afterwards.
    payload_root = Sdf.CreatePrimInLayer(payload_layer, root_path)
    payload_root.specifier = interface_spec.specifier
    if interface_spec.typeName:
        payload_root.typeName = interface_spec.typeName
    for name in interface_spec.nameChildren.keys():
        child = root_path.AppendChild(name)
        Sdf.CopySpec(root_layer, child, payload_layer, child)

    # Copied opinions now resolve relative to the payload's directory, not the
    # interface's. Rebase every asset opinion, including those in variants,
    # time samples, clip metadata and external composition arcs.
    _rebase_payload_assets(payload_layer, namespace.as_posix())

    for key in _SHARED_LAYER_METADATA:
        if root_layer.pseudoRoot.HasInfo(key):
            payload_layer.pseudoRoot.SetInfo(key, root_layer.pseudoRoot.GetInfo(key))
    payload_layer.defaultPrim = root_path.name
    payload_layer.Save()

    # The interface keeps the root prim and everything said *about* it, and
    # hands off everything said *inside* it.
    for name in list(interface_spec.nameChildren.keys()):
        del interface_spec.nameChildren[name]
    relative = f"./{namespace.as_posix()}/{payload_path.name}"
    interface_spec.payloadList.explicitItems = [Sdf.Payload(relative)]

    written = {
        "payload_layer": payload_path.name,
        "payload_path": relative,
        "interface": stage_path.name,
    }
    if diagnostics:
        diagnostics.set_payload_split(written)
    return written


def _unused_payload_path(payload_dir: Path, stage_path: Path) -> Path:
    """Name the payload layer without clobbering a localized dependency.

    The generation directory is shared with the layers ``usd_assets`` copied
    in, and one of those may already be called ``<stem>_payload``.
    """
    stem, suffix = f"{stage_path.stem}_payload", stage_path.suffix
    candidate = payload_dir / f"{stem}{suffix}"
    index = 2
    while candidate.exists() or Sdf.Layer.Find(str(candidate)):
        candidate = payload_dir / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _payload_asset_path(path: str, payload_dir: str) -> str:
    """Re-anchor a relative path from the interface's directory to the payload's.

    ``payload_dir`` is the payload layer's directory relative to the
    interface's, in POSIX form and without ``..``. A path into the payload's
    own generation comes out as ``./<name>``; anything else climbs out of it.
    """
    # USD asset paths are POSIX-style; pathlib would call "/abs" relative
    # on Windows, where it has no drive.
    if not path or asset_scheme(path) or path.startswith(("/", "\\")):
        return path
    # A Windows absolute path must also stay absolute on a POSIX test host.
    if len(path) >= 2 and path[1] == ":":
        return path
    # A package-relative path names a member inside the archive after "[";
    # only the archive's own path is relative to the layer.
    outer, bracket, member = path.partition("[")
    target = [part for part in posixpath.normpath(outer).split("/") if part != "."]
    base = [part for part in payload_dir.split("/") if part not in ("", ".")]
    shared = 0
    while (
        shared < len(base)
        and shared < len(target) - 1
        and target[shared] == base[shared]
    ):
        shared += 1
    parts = [".."] * (len(base) - shared) + target[shared:]
    rebased = "/".join(parts)
    if not rebased.startswith("../"):
        rebased = f"./{rebased}"
    return f"{rebased}{bracket}{member}"


def _rebase_payload_assets(layer, payload_dir: str) -> None:
    """Re-anchor asset opinions without dropping empty array entries."""
    def rewrite(value):
        if isinstance(value, Sdf.AssetPath):
            return Sdf.AssetPath(_payload_asset_path(value.path, payload_dir))
        if isinstance(value, Sdf.AssetPathArray):
            return Sdf.AssetPathArray([rewrite(item) for item in value])
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, (Sdf.ReferenceListOp, Sdf.PayloadListOp)):
            result = type(value)()
            fields = ("explicitItems",) if value.isExplicit else (
                "addedItems", "prependedItems", "appendedItems",
                "deletedItems", "orderedItems",
            )
            for field in fields:
                setattr(result, field, [rewrite(item) for item in getattr(value, field)])
            return result
        if isinstance(value, Sdf.Reference):
            return Sdf.Reference(
                _payload_asset_path(value.assetPath, payload_dir), value.primPath,
                value.layerOffset, rewrite(value.customData),
            )
        if isinstance(value, Sdf.Payload):
            return Sdf.Payload(
                _payload_asset_path(value.assetPath, payload_dir), value.primPath,
                value.layerOffset,
            )
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, tuple):
            return tuple(rewrite(item) for item in value)
        return value

    paths = []
    layer.Traverse(Sdf.Path.absoluteRootPath, paths.append)
    for path in paths:
        spec = layer.GetObjectAtPath(path)
        if spec is None:
            continue
        for key in spec.ListInfoKeys():
            if key == "timeSamples":
                for time in layer.ListTimeSamplesForPath(path):
                    layer.SetTimeSample(path, time, rewrite(layer.QueryTimeSample(path, time)))
            else:
                value = spec.GetInfo(key)
                mapped = rewrite(value)
                if mapped != value:
                    spec.SetInfo(key, mapped)
