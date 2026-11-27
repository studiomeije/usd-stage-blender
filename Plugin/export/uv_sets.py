"""The second UV set, written where RealityKit's second texcoord reads it.

RealityKit reads at most two texture-coordinate sets. ``ND_texcoord`` index 0
returns ``primvars:st``, which Blender's USD exporter writes from each mesh's
render UV map, and index 1 returns ``primvars:st1`` (measured on macOS 27.2.1:
``texcoord`` index 1 and ``geompropvalue "st1"`` read the same values). Blender
writes every other UV map under its own name, so a mesh's second map, the one
it does not render with, is renamed ``st1`` here, and a material reading it
authors ``texcoord`` index 1 (``uv_set_resolution`` resolves it to ``UV1``).

The rename edits only the layers the export owns, as raw specs, so inactive
variants and prototypes are covered and a referenced layer is never touched;
the preflight then refuses a mesh whose material reads index 1 without an
``st1`` (``SECOND_UV_SET_MISSING``). A mesh with more than two UV sets is
left alone for the preflight to refuse (``TOO_MANY_UV_SETS``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

SECOND_UV_SET = "st1"
_FIRST_UV_SET = "st"


def _owned_layers(stage, writable_layer_paths: Iterable[str]):
    from .usd_scene import _layer_filesystem_key
    from .usd_utils import Sdf

    allowed = {str(Path(path).resolve()) for path in writable_layer_paths or () if path}
    by_key = {}
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        key = _layer_filesystem_key(layer)
        if key in allowed:
            by_key.setdefault(key, layer)
    for key in sorted(allowed):
        layer = by_key.get(key) or Sdf.Layer.FindOrOpen(key)
        if layer is not None and getattr(layer, "permissionToEdit", True):
            yield layer


def _uv_names(prim_spec) -> list:
    from .usd_utils import Sdf

    names = []
    for prop in prim_spec.properties:
        name = str(prop.name)
        if not name.startswith("primvars:") or name.endswith(":indices"):
            continue
        if getattr(prop, "typeName", None) == Sdf.ValueTypeNames.TexCoord2fArray:
            names.append(name[len("primvars:"):])
    return names


def rename_second_uv_sets(stage, writable_layer_paths, diagnostics=None) -> list:
    """Rename each owned mesh spec's non-render UV set to ``st1``.

    Returns the ``(prim path, old name)`` pairs renamed.
    """
    from .usd_utils import Sdf

    renamed = []
    for layer in _owned_layers(stage, writable_layer_paths):
        specs = []
        layer.Traverse(Sdf.Path.absoluteRootPath, specs.append)
        edit = Sdf.BatchNamespaceEdit()
        for path in specs:
            prim_spec = layer.GetObjectAtPath(path)
            if not isinstance(prim_spec, Sdf.PrimSpec):
                continue
            others = [name for name in _uv_names(prim_spec) if name != _FIRST_UV_SET]
            if len(others) != 1 or others[0] == SECOND_UV_SET:
                continue
            old = others[0]
            if prim_spec.properties.get(f"primvars:{SECOND_UV_SET}") is not None:
                continue
            for suffix in ("", ":indices"):
                if prim_spec.properties.get(f"primvars:{old}{suffix}") is not None:
                    edit.Add(
                        path.AppendProperty(f"primvars:{old}{suffix}"),
                        path.AppendProperty(f"primvars:{SECOND_UV_SET}{suffix}"),
                    )
            renamed.append((str(path), old))
        if edit.edits:
            if not layer.CanApply(edit):
                raise RuntimeError(f"Could not rename the second UV sets in {layer.identifier}")
            layer.Apply(edit)
    if renamed and diagnostics:
        diagnostics.add_info(
            "Wrote each mesh's second UV map as primvars:st1, RealityKit's texcoord index 1: "
            + ", ".join(f"{path} ({old})" for path, old in sorted(renamed))
        )
    return renamed
