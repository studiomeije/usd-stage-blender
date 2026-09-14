"""USD scene normalization utilities.

Keeps stage metadata and prim names aligned with RealityKit expectations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from ..apple_contract import REALITYKIT_UP_AXIS
from .usd_utils import Sdf


@dataclass(frozen=True)
class _PathReference:
    """A composed relationship/connection that must follow a prim rename."""

    owner_path: object
    property_name: str
    targets: tuple[object, ...]
    kind: str


def normalize_scene(
    stage,
    settings,
    *,
    writable_layer_paths: Iterable[str] | None = None,
    diagnostics=None,
) -> None:
    """Normalize scene metadata and prim names for Reality Composer Pro.

    Prim namespace edits are performed on the contributing local layers with
    :class:`Sdf.BatchNamespaceEdit`.  Unlike recreating a prim, a namespace
    edit preserves descendants, attributes, metadata, variants, relationships,
    and time samples.  Composed relationship and shader-connection targets are
    subsequently retargeted because ``Sdf.Layer.Apply`` intentionally does not
    update paths stored in other specs.
    """
    # Ensure defaultPrim references a root prim.  ``root_prim_name`` accepts a
    # leading slash in the UI, but defaultPrim itself must name a root prim.
    if not stage.GetDefaultPrim():
        root_prim_name = _root_identifier(getattr(settings, "root_prim_name", ""))
        root_prim = stage.GetPrimAtPath(f"/{root_prim_name}")
        if not root_prim:
            root_prim = stage.DefinePrim(f"/{root_prim_name}", "Xform")
        stage.SetDefaultPrim(root_prim)

    # RealityKit and Reality Composer Pro assets always use the Apple Y-up
    # contract. This metadata is not an artist-configurable export option.
    stage.SetMetadata("upAxis", REALITYKIT_UP_AXIS)

    _rename_invalid_prims(
        stage,
        allow_unicode=bool(getattr(settings, "allow_unicode", True)),
        writable_layer_paths=writable_layer_paths,
    )

    # Blender can sometimes export mesh schema attributes onto an Xform prim
    # type. Reality Composer Pro won't treat this as geometry, so re-type such
    # prims to Mesh when they clearly contain mesh topology.
    _repair_xform_mesh_prims(stage)
    _normalize_owned_double_sided_mesh_specs(
        writable_layer_paths,
        stage=stage,
        diagnostics=diagnostics,
    )


def _normalize_owned_double_sided_mesh_specs(
    writable_layer_paths: Iterable[str] | None,
    *,
    stage=None,
    diagnostics=None,
) -> tuple[str, ...]:
    """Author ``doubleSided=false`` on every output-owned Mesh spec.

    Blender 5.2 authors ``doubleSided=true`` for ordinary meshes. RealityKit's
    portable Apple OS 27 renderer profile does not support that contract, but a
    composed ``UsdGeom.Mesh.GetDoubleSidedAttr().Set(False)`` pass would be
    unsafe: it can create a stronger root-layer override for geometry owned by
    an external reference and it cannot see inactive variants.

    Instead, localization supplies the exact filesystem allowlist of layers
    owned by this export. Traverse those layers as raw Sdf specs, which covers
    inactive variants and Blender's abstract ``class \"prototypes\"`` trees,
    and edit each Mesh opinion in place. Layers outside the allowlist are never
    opened for editing; a surviving external/package ``true`` opinion remains
    visible to strict composed-stage preflight and fails the export there.

    Returns the authored owner keys that changed from a true opinion. The
    return value is useful for focused tests; user-facing reporting is emitted
    through ``diagnostics`` exactly once per affected owner.
    """
    if not writable_layer_paths:
        return ()

    allowed_paths = sorted(
        {str(Path(path).resolve()) for path in writable_layer_paths if path}
    )
    owned_prim_specs = []

    # Prefer the exact layer objects already participating in the stage. On
    # macOS, temporary paths commonly have two lexical spellings
    # (``/var/...`` and ``/private/var/...``). Opening the canonical spelling
    # can create a second Sdf.Layer object while the stage keeps composing the
    # first one, so edits saved through the duplicate are invisible to the
    # immediate preflight. Match by canonical filesystem identity but mutate
    # the stage-owned object whenever it exists.
    stage_layers_by_key: dict[str, list[object]] = {}
    if stage is not None and hasattr(stage, "GetLayerStack"):
        for layer in stage.GetLayerStack(includeSessionLayers=False):
            layer_key = _layer_filesystem_key(layer)
            if layer_key:
                stage_layers_by_key.setdefault(layer_key, []).append(layer)

    for allowed_path in allowed_paths:
        layers = stage_layers_by_key.get(allowed_path)
        if not layers:
            layer = Sdf.Layer.FindOrOpen(allowed_path)
            if not layer:
                raise RuntimeError(
                    "Could not open output-owned USD layer for double-sided "
                    f"normalization: {allowed_path}"
                )
            layers = [layer]

        for layer in layers:
            layer_key = _layer_filesystem_key(layer)
            if layer_key != allowed_path:
                raise RuntimeError(
                    "Refusing to normalize a USD layer outside the localization "
                    f"allowlist: {getattr(layer, 'identifier', allowed_path)}"
                )

            # A layer whose permission was revoked after localization cannot
            # be safely edited. Leave its opinions intact so strict composed-
            # stage preflight reports surviving double-sided geometry.
            if not bool(getattr(layer, "permissionToEdit", True)):
                continue

            spec_paths = []
            layer.Traverse(
                Sdf.Path.absoluteRootPath,
                lambda path: spec_paths.append(path),
            )
            for spec_path in spec_paths:
                prim_spec = layer.GetObjectAtPath(spec_path)
                if not isinstance(prim_spec, Sdf.PrimSpec):
                    continue
                owned_prim_specs.append((layer, prim_spec))

    # Variant ``over`` specs commonly omit typeName while inheriting Mesh from
    # a definition at the same namespace path. Resolve that ownership only
    # from other output-owned specs. In particular, do not use the composed
    # stage type here: that would let a root over on an unowned package Mesh
    # become a stronger output-root override, defeating the ownership guard.
    owned_mesh_paths = {
        str(prim_spec.path.StripAllVariantSelections())
        for _layer, prim_spec in owned_prim_specs
        if str(getattr(prim_spec, "typeName", "")) == "Mesh"
    }

    actions = []
    for layer, prim_spec in owned_prim_specs:
        authored_type = str(getattr(prim_spec, "typeName", ""))
        namespace_path = str(prim_spec.path.StripAllVariantSelections())
        if authored_type == "Mesh":
            pass
        elif authored_type == "" and namespace_path in owned_mesh_paths:
            pass
        else:
            continue

        attribute = prim_spec.attributes.get("doubleSided")
        if attribute is not None and attribute.typeName != Sdf.ValueTypeNames.Bool:
            # Do not silently replace a malformed property definition. The
            # temporary export will fail before publication with a precise
            # ownership error instead of mutating an ambiguous schema.
            raise RuntimeError(
                "Cannot normalize non-boolean Mesh.doubleSided opinion at "
                f"{prim_spec.path} in {layer.identifier}"
            )

        sample_times = (
            list(layer.ListTimeSamplesForPath(attribute.path))
            if attribute is not None
            else []
        )
        affected = bool(
            attribute is not None
            and (
                _bool_opinion_is_true(attribute.default)
                or any(
                    _bool_opinion_is_true(
                        layer.QueryTimeSample(attribute.path, sample_time)
                    )
                    for sample_time in sample_times
                )
            )
        )
        actions.append(
            (layer, prim_spec.path, attribute, tuple(sample_times), affected)
        )

    changed_layers = set()
    affected_owners = []
    with Sdf.ChangeBlock():
        for layer, prim_path, attribute, sample_times, affected in actions:
            prim_spec = layer.GetPrimAtPath(prim_path)
            if prim_spec is None:
                raise RuntimeError(
                    f"Mesh spec disappeared during normalization: {prim_path}"
                )
            if attribute is None:
                attribute = Sdf.AttributeSpec(
                    prim_spec,
                    "doubleSided",
                    Sdf.ValueTypeNames.Bool,
                    Sdf.VariabilityUniform,
                )
            attribute.default = False
            for sample_time in sample_times:
                layer.SetTimeSample(attribute.path, sample_time, False)
            changed_layers.add(layer)

            if not affected:
                continue
            owner_key = f"{layer.identifier}:{prim_path}"
            affected_owners.append(owner_key)
            if diagnostics is not None and hasattr(diagnostics, "add_info"):
                # Info, not warning: this fires for every mesh on essentially
                # every export (Blender authors doubleSided=true by default),
                # names the mesh by USD prim path and a temp staging layer the
                # artist can never open, and there is nothing to act on for
                # closed geometry. As a warning it drowned the real ones.
                diagnostics.add_info(
                    "RealityKit portability normalization authored "
                    f"doubleSided=false for Mesh owner {prim_path} in layer "
                    f"'{layer.identifier}'. Backfaces are unsupported by the "
                    "strict Apple OS 27 profile; closed or thick geometry is "
                    "required."
                )

    # Some localized layers can be reachable only from an inactive variant and
    # therefore are not saved by UsdStage.Save(). Persist every edited owned
    # layer explicitly while preserving all external source files byte-for-byte.
    for layer in changed_layers:
        if not layer.Save():
            raise RuntimeError(
                "Failed to save double-sided normalization in output-owned "
                f"USD layer: {layer.identifier}"
            )

    return tuple(affected_owners)


def _bool_opinion_is_true(value: object) -> bool:
    """Return whether an authored bool/default or time sample is true."""
    try:
        return bool(value) if value is not None else False
    except Exception:
        return False


def _root_identifier(value: object) -> str:
    """Return a valid, root-level identifier for the requested default prim."""
    raw = str(value or "Scene").strip().strip("/")
    # A nested path cannot be a defaultPrim token. Keep the user's information
    # while turning separators into a deterministic root name.
    raw = raw.replace("/", "_")
    return _make_valid_identifier(raw, allow_unicode=True)


def _rename_invalid_prims(
    stage,
    *,
    allow_unicode: bool,
    writable_layer_paths: Iterable[str] | None = None,
) -> list[tuple[object, object]]:
    """Rename unsafe prim identifiers without rebuilding their prim specs.

    Renames are applied one hierarchy depth at a time. Moving a parent first
    ensures its whole subtree follows the edit; the next pass then sees the
    descendants at their new paths. Names are allocated per parent against both
    existing siblings and the destinations reserved by the current batch.

    Returns the ordered path edits, primarily for diagnostics and tests.
    """
    edits_applied: list[tuple[object, object]] = []
    references = _capture_path_references(stage)
    default_path = None
    default_prim = stage.GetDefaultPrim()
    if default_prim:
        default_path = default_prim.GetPath()

    while True:
        invalid = [
            prim
            for prim in stage.TraverseAll()
            if not _is_valid_identifier(prim.GetName(), allow_unicode=allow_unicode)
        ]
        if not invalid:
            break

        depth = min(prim.GetPath().pathElementCount for prim in invalid)
        current_depth = [
            prim for prim in invalid if prim.GetPath().pathElementCount == depth
        ]
        path_edits: list[tuple[object, object]] = []
        reserved_by_parent: dict[str, set[str]] = {}

        for prim in sorted(current_depth, key=lambda item: str(item.GetPath())):
            old_path = prim.GetPath()
            parent_path = old_path.GetParentPath()
            parent_key = str(parent_path)
            reserved = reserved_by_parent.setdefault(
                parent_key,
                {
                    child.GetName()
                    for child in stage.GetPrimAtPath(parent_path).GetChildren()
                },
            )
            base_name = _make_valid_identifier(
                prim.GetName(), allow_unicode=allow_unicode
            )
            new_name = _unique_identifier(base_name, reserved)
            reserved.add(new_name)
            path_edits.append((old_path, parent_path.AppendChild(new_name)))

        _apply_namespace_batch(
            stage,
            path_edits,
            writable_layer_paths=writable_layer_paths,
        )
        edits_applied.extend(path_edits)

    if edits_applied:
        _restore_path_references(stage, references, edits_applied)
        if default_path is not None:
            new_default_path = _remap_path(default_path, edits_applied)
            new_default = stage.GetPrimAtPath(new_default_path)
            if not new_default:
                raise RuntimeError(
                    f"Default prim was lost during namespace edit: {default_path}"
                )
            stage.SetDefaultPrim(new_default)

    return edits_applied


def _apply_namespace_batch(
    stage,
    path_edits: Iterable[tuple[object, object]],
    *,
    writable_layer_paths: Iterable[str] | None = None,
) -> None:
    """Apply a collision-checked namespace batch to output-owned layers only."""
    path_edits = list(path_edits)
    if not path_edits:
        return

    allowed = None
    if writable_layer_paths is not None:
        allowed = {str(Path(path).resolve()) for path in writable_layer_paths}

    layers = list(stage.GetLayerStack(includeSessionLayers=False))
    layer_batches: list[tuple[object, object]] = []
    covered_paths: set[str] = set()

    for layer in layers:
        batch = Sdf.BatchNamespaceEdit()
        count = 0
        for old_path, new_path in path_edits:
            if layer.GetPrimAtPath(old_path) is None:
                continue
            batch.Add(old_path, new_path)
            covered_paths.add(str(old_path))
            count += 1
        if count:
            layer_path = _layer_filesystem_key(layer)
            if allowed is not None and layer_path not in allowed:
                raise RuntimeError(
                    "Refusing to normalize a non-localized USD layer: "
                    f"{getattr(layer, 'identifier', '<unknown>')}"
                )
            can_apply = layer.CanApply(batch)
            if isinstance(can_apply, tuple):
                can_apply = can_apply[0]
            if not can_apply:
                raise RuntimeError(
                    f"USD layer rejected namespace edits in {layer.identifier}"
                )
            layer_batches.append((layer, batch))

    missing = [old for old, _new in path_edits if str(old) not in covered_paths]
    if missing:
        formatted = ", ".join(str(path) for path in missing)
        raise RuntimeError(
            "Cannot safely rename prims authored only by external composition "
            f"arcs: {formatted}"
        )

    with Sdf.ChangeBlock():
        for layer, batch in layer_batches:
            if not layer.Apply(batch):
                raise RuntimeError(
                    f"Failed to apply USD namespace edits in {layer.identifier}"
                )


def _layer_filesystem_key(layer) -> str | None:
    """Return a canonical local path for namespace-edit ownership checks."""
    for field in ("realPath", "resolvedPath", "identifier"):
        value = getattr(layer, field, None)
        if not value:
            continue
        text = str(value)
        if text.lower().startswith(("anon:", "mem:")) or "[" in text:
            continue
        try:
            return str(Path(text).resolve())
        except OSError:
            continue
    return None


def _capture_path_references(stage) -> list[_PathReference]:
    """Capture composed path properties affected by a namespace edit."""
    references: list[_PathReference] = []
    for prim in stage.TraverseAll():
        owner_path = prim.GetPath()
        for relationship in prim.GetRelationships():
            targets = tuple(relationship.GetTargets())
            if targets:
                references.append(
                    _PathReference(
                        owner_path,
                        relationship.GetName(),
                        targets,
                        "relationship",
                    )
                )
        for attribute in prim.GetAttributes():
            connections = tuple(attribute.GetConnections())
            if connections:
                references.append(
                    _PathReference(
                        owner_path,
                        attribute.GetName(),
                        connections,
                        "connection",
                    )
                )
    return references


def _restore_path_references(
    stage,
    references: Iterable[_PathReference],
    path_edits: Iterable[tuple[object, object]],
) -> None:
    """Retarget composed relationships and connections after prim moves."""
    path_edits = list(path_edits)
    for reference in references:
        owner_path = _remap_path(reference.owner_path, path_edits)
        prim = stage.GetPrimAtPath(owner_path)
        if not prim:
            raise RuntimeError(
                f"Path owner was lost during namespace edit: {reference.owner_path}"
            )
        targets = [_remap_path(target, path_edits) for target in reference.targets]
        if reference.kind == "relationship":
            prop = prim.GetRelationship(reference.property_name)
            if not prop or not prop.SetTargets(targets):
                raise RuntimeError(
                    f"Failed to retarget relationship {owner_path}.{reference.property_name}"
                )
        else:
            prop = prim.GetAttribute(reference.property_name)
            if not prop or not prop.SetConnections(targets):
                raise RuntimeError(
                    f"Failed to retarget connection {owner_path}.{reference.property_name}"
                )


def _remap_path(path, path_edits: Iterable[tuple[object, object]]):
    """Apply ordered namespace edits to an absolute prim/property path."""
    remapped = path
    for old_path, new_path in path_edits:
        if remapped.HasPrefix(old_path):
            remapped = remapped.ReplacePrefix(old_path, new_path)
    return remapped


def _unique_identifier(base_name: str, reserved: set[str]) -> str:
    if base_name not in reserved:
        return base_name
    suffix = 2
    while f"{base_name}_{suffix}" in reserved:
        suffix += 1
    return f"{base_name}_{suffix}"


def _make_valid_identifier(name: str, *, allow_unicode: bool) -> str:
    """Sanitize an identifier deterministically for USD and RealityKit."""
    value = str(name or "")
    if allow_unicode:
        cleaned = "".join(
            char if (char == "_" or char.isalnum()) else "_" for char in value
        )
    else:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not cleaned:
        cleaned = "prim"
    first = cleaned[0]
    if first != "_" and not first.isalpha():
        cleaned = f"prim_{cleaned}"
    if not allow_unicode:
        # ``str.isalpha`` accepts non-ASCII letters; the regex above doesn't,
        # but keep this explicit so future sanitizer changes stay ASCII-safe.
        cleaned = re.sub(r"[^A-Za-z0-9_]", "_", cleaned)
    return cleaned


def _is_valid_identifier(name: str, *, allow_unicode: bool = True) -> bool:
    """Return whether ``name`` is safe for the configured USD namespace."""
    if not name:
        return False
    if not allow_unicode:
        return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None
    try:
        return bool(Sdf.Path.IsValidIdentifier(name))
    except Exception:
        return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None


def _repair_xform_mesh_prims(stage) -> None:
    def has_attr(prim, name: str) -> bool:
        try:
            attr = prim.GetAttribute(name)
            return bool(attr and attr.IsValid())
        except Exception:
            return False

    for prim in stage.Traverse():
        try:
            if prim.GetTypeName() != "Xform":
                continue
        except Exception:
            continue

        # Minimal signature of a Mesh: topology + points.
        if (
            has_attr(prim, "faceVertexCounts")
            and has_attr(prim, "faceVertexIndices")
            and has_attr(prim, "points")
        ):
            try:
                prim.SetTypeName("Mesh")
            except Exception:
                continue
