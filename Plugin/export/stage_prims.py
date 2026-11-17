"""Every prim of an exported stage that can reach RealityKit, each once.

``UsdStage.Traverse()`` stops at instance roots, so Blender's default
instanceable-reference export hides the meshes, materials and textures below
every instance from an ordinary traversal. And Blender writes collection
definitions below an abstract ``class "prototypes"`` prim that only
``TraverseAll()`` sees. Both the material rewrite and the preflight have to
visit all of that, and both ask here, so they always agree on what the asset
contains.
"""

from __future__ import annotations

from .usd_utils import Usd


def composed_prims(stage) -> list:
    """The ordinary namespace, the used class subtrees, then each prototype.

    Each shared prototype is visited once rather than once per instance, so a
    defect in it is found once. Only class subtrees an ordinary prim actually
    references are included: an unused class must not stop an export. When an
    internal class reference also creates a prototype, the class subtree is
    visited and that equivalent prototype is skipped.

    A stand-in stage without ``TraverseAll`` or ``GetPrototypes`` yields its
    ordinary namespace alone.
    """
    prims: list = []
    seen_paths: set[str] = set()

    def append_range(prim_range) -> None:
        for prim in prim_range:
            path = str(prim.GetPath())
            if path in seen_paths:
                continue
            seen_paths.add(path)
            prims.append(prim)

    append_range(list(stage.Traverse()))
    class_backed_prototypes: set[str] = set()

    if hasattr(stage, "TraverseAll"):
        all_namespace_prims = [
            prim
            for prim in stage.TraverseAll()
            if prim.IsActive() and prim.IsLoaded() and prim.IsDefined()
        ]
        abstract_roots, class_backed_prototypes = referenced_abstract_roots(
            stage, all_namespace_prims,
        )
        for abstract_root in abstract_roots:
            append_range(
                prim
                for prim in all_namespace_prims
                if prim.IsAbstract() and prim.GetPath().HasPrefix(abstract_root.GetPath())
            )

    if Usd is not None and hasattr(stage, "GetPrototypes"):
        for prototype in stage.GetPrototypes():
            if str(prototype.GetPath()) in class_backed_prototypes:
                continue
            append_range(Usd.PrimRange(prototype))
    return prims


def referenced_abstract_roots(stage, prims) -> tuple[list, set[str]]:
    """Class roots that ``prims`` reference internally, and the prototypes they back.

    Returns ``(roots, class_backed_prototype_paths)``: the active, loaded,
    defined abstract prims some non-abstract prim references by path within
    this stage, and the paths of the prototypes those references also created.
    """
    roots: dict = {}
    class_backed_prototypes: set[str] = set()
    for prim in prims:
        if prim.IsAbstract():
            continue
        try:
            references = prim.GetMetadata("references")
            items = list(references.GetAddedOrExplicitItems()) if references else []
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
