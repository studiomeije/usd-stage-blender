"""
USD post-processing pipeline for RealityKit compatibility.

Runs scene normalization, material rewriting, and texture preparation.
"""

from .materials.rewrite import rewrite_materials
from .materials.extract import (
    begin_image_staging_session,
    cleanup_image_staging_session,
)
from .usd_animation_library import author_animation_library
from .realitykit_preflight import (
    _record_diagnostics,
    validate_stage,
)
from .usd_scene import normalize_scene, _normalize_owned_double_sided_mesh_specs
from .usd_assets import prepare_assets
from .usd_utils import Usd, require_pxr


def process_usd_stage(usd_path: str, settings, context, diagnostics=None) -> None:
    """Post-process a USD stage for RealityKit compatibility."""
    require_pxr()

    stage = Usd.Stage.Open(usd_path, Usd.Stage.LoadAll)
    if not stage:
        raise RuntimeError(f"Failed to open USD stage: {usd_path}")

    # Material extraction may snapshot dirty/generated image pixels to disk.
    # Keep those files alive through the final texture localization, then clear
    # every export-local cache entry and temp file even if a later phase fails.
    begin_image_staging_session(diagnostics)
    try:
        # Resolve asset opinions while external source layers still retain their
        # original resolver anchors. Composition layers are then copied into the
        # export-owned namespace before any namespace or schema mutation occurs.
        writable_layer_paths = _run_step(
            diagnostics,
            "localize_source_dependencies",
            _prepare_assets,
            stage,
            usd_path,
            settings,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "normalize_scene",
            _normalize_localized_scene,
            stage,
            settings,
            writable_layer_paths,
            diagnostics,
        )

        # Persist the localized, normalized layer stack before later passes can
        # add or rewrite composition arcs. Even a semantically identical Sdf
        # arc edit may trigger recomposition; no such reload may resurrect the
        # pre-normalized bytes copied from an external source layer.
        _run_step(
            diagnostics,
            "save_localized_layers",
            _save_stage,
            stage,
        )

        _run_step(diagnostics, "rewrite_materials", rewrite_materials, stage, settings, context, diagnostics)

        _run_step(diagnostics, "author_animation_library", author_animation_library, stage, settings, diagnostics)

        # Catch any asset opinions authored by material/animation post-processing.
        # All composition arcs already point to output-owned layers at this point.
        final_writable_layer_paths = _run_step(
            diagnostics,
            "finalize_assets",
            _prepare_assets,
            stage,
            usd_path,
            settings,
            diagnostics,
        )
        # Material/animation authoring normally adds only root-layer schemas,
        # but the final localization pass is the authoritative ownership set.
        # Re-run the raw Sdf normalization over that exact set so a newly
        # discovered inactive USD-valued asset cannot bypass the portable
        # double-sided contract. Already-normalized owners do not warn twice.
        _run_step(
            diagnostics,
            "normalize_finalized_meshes",
            _normalize_finalized_meshes,
            stage,
            final_writable_layer_paths,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "retag_unmapped_color_spaces",
            _retag_unmapped_color_space_names,
            stage,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "author_object_random",
            _author_object_random,
            stage,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "author_uv_derivatives",
            _author_uv_derivatives,
            stage,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "publish_vertex_colors_as_display_color",
            _publish_vertex_colors_as_display_color,
            stage,
            diagnostics,
            context,
        )
        _run_step(
            diagnostics,
            "complete_blend_shape_weights",
            _complete_blend_shape_weights,
            stage,
            diagnostics,
        )
        _run_step(
            diagnostics,
            "realitykit_preflight",
            _require_realitykit_preflight,
            stage,
            usd_path,
            settings,
            diagnostics,
        )

        if diagnostics:
            diagnostics.begin_phase("stage_save", {"usd_path": usd_path})
        stage.Save()
        if diagnostics:
            diagnostics.end_phase("stage_save")

        if diagnostics:
            # A progress note, not a warning: it fires on every successful
            # export and consumed one of the few user-visible warning slots.
            diagnostics.add_info("USD stage post-processed for RealityKit compatibility")
    finally:
        cleanup_image_staging_session(diagnostics)


def _complete_blend_shape_weights(stage, diagnostics=None) -> None:
    """Stop a SkelAnimation claiming blend-shape animation it does not have.

    Blender routes shape keys through USD's skeletal schema. When the keys carry
    no animation it still names them in the ``SkelAnimation``'s ``blendShapes``
    and leaves ``blendShapeWeights`` with no value - no default, no time
    samples. Reality Composer Pro then tries to import a blend-shape animation
    that has no weights and refuses the file:

        Failed to import blend shape animation: prim_path='.../Skel/Anim'

    Apple's own shape-keyed assets do not do this: their animation leaves
    ``blendShapes`` unauthored entirely, and the shapes still reach the mesh
    through its ``skel:blendShapes`` and ``skel:blendShapeTargets``. Clearing
    the empty declaration matches that, and loses nothing - there is no
    animation to lose.

    An animation with authored weights is left exactly as it is.
    """

    from pxr import UsdSkel

    for prim in stage.TraverseAll():
        if prim.GetTypeName() != "SkelAnimation":
            continue
        animation = UsdSkel.Animation(prim)
        shapes_attr = animation.GetBlendShapesAttr()
        if not shapes_attr or not shapes_attr.HasAuthoredValue():
            continue
        shapes = shapes_attr.Get() or ()
        weights = animation.GetBlendShapeWeightsAttr()
        if weights and weights.GetTimeSamples():
            continue
        values = weights.Get() if weights else None
        if values is not None and len(values) == len(shapes) and any(values):
            # A static non-zero pose is a pose, not an absence. Leave it.
            continue

        shapes_attr.Clear()
        if weights:
            weights.Clear()
        if diagnostics:
            diagnostics.add_warning(
                f"{prim.GetPath()}: the shape keys "
                f"({', '.join(str(name) for name in shapes)}) have no animated "
                "weights, so the empty blend-shape animation was removed. "
                "Reality Composer Pro refuses a file that declares one with no "
                "weights. The shapes themselves are unaffected."
            )


def _run_step(diagnostics, name: str, func, *args):
    if diagnostics:
        diagnostics.begin_phase(name)
    try:
        result = func(*args)
    except Exception as exc:
        if diagnostics:
            diagnostics.record_phase_error(name, exc)
        raise
    if diagnostics:
        diagnostics.end_phase(name)
    return result


def _normalize_localized_scene(stage, settings, writable_layer_paths, diagnostics=None):
    """Normalize only the root and dependency layers owned by this export."""
    return normalize_scene(
        stage,
        settings,
        writable_layer_paths=writable_layer_paths,
        diagnostics=diagnostics,
    )


def _normalize_finalized_meshes(stage, writable_layer_paths, diagnostics=None):
    """Normalize Mesh specs discovered by the authoritative final asset pass."""
    return _normalize_owned_double_sided_mesh_specs(
        writable_layer_paths,
        stage=stage,
        diagnostics=diagnostics,
    )


def _prepare_assets(stage, usd_path: str, settings, diagnostics=None):
    """Localize every direct asset opinion without composing it into root.

    Layer traversal preserves variant and instance-prototype authorship.  A
    composed ``Usd.Attribute.Set`` pass would instead create a stronger edit in
    the root layer and can silently collapse those authored choices.
    """
    return prepare_assets(
        stage,
        usd_path,
        diagnostics,
        settings=settings,
    )


def _save_stage(stage):
    """Persist only the root and currently composed output-owned layers."""
    stage.Save()


def _require_realitykit_preflight(stage, usd_path: str, settings, diagnostics=None):
    """Fail the shared UI/CLI/bake pipeline on strict OS 27 findings."""
    report = validate_stage(stage, usd_path, settings)
    if diagnostics is not None:
        _record_diagnostics(diagnostics, report)
    if report.errors:
        preview = "; ".join(issue.format() for issue in report.errors[:5])
        remaining = len(report.errors) - 5
        if remaining > 0:
            preview = f"{preview}; {remaining} more"
        raise RuntimeError(
            f"RealityKit OS 27 preflight failed with {len(report.errors)} "
            f"error(s): {preview}"
        )
    return report


#: ColorSpaceAPI tokens Blender 5.2 authors that Reality Composer Pro 3 has no
#: alias for, mapped to the engine-known token with the same encoding. Both
#: names describe the same sRGB transfer on Rec.709 primaries, so the rewrite
#: is a renaming, not a conversion.
#:
#: This must be a name ``UsdColorSpaceAPI`` accepts, which is a stricter test
#: than being in CoreRE's alias table. ``colorSpace:name`` is resolved by
#: ``ComputeColorSpaceName`` -> ``IsValidColorSpaceName`` -> ``GfColorSpace``,
#: whose registry is nanocolor's, and ``srgb_texture`` is not in it. Measured
#: against a real export with usd-core 26.08: the prim logs
#: "Unknown color space srgb_texture encountered." and resolves to the *empty*
#: token - it does not fall back to the ancestor's opinion - so the texture
#: reaches RealityKit with no colour space at all. ``srgb_rec709_scene`` is
#: present in both that registry and RealityKit's own.
#:
#: Attribute-level ``colorSpace`` metadata is deliberately left spelled
#: ``srgb_texture``: that is MaterialX's vocabulary, it is what RCP's own
#: MaterialX writer emits, and it is resolved through a different table that
#: does carry the name.
_COLOR_SPACE_NAME_REWRITES = {
    "srgb_rec709_display": "srgb_rec709_scene",
}


def _retag_unmapped_color_space_names(stage, diagnostics=None) -> None:
    """Rewrite authored ``colorSpace:name`` tokens RCP cannot interpret."""
    retagged = {}
    for prim in stage.Traverse():
        attribute = prim.GetAttribute("colorSpace:name")
        if not attribute or not attribute.HasAuthoredValue():
            continue
        authored = str(attribute.Get() or "")
        replacement = _COLOR_SPACE_NAME_REWRITES.get(authored)
        if replacement is None:
            continue
        attribute.Set(replacement)
        retagged[str(prim.GetPath())] = (authored, replacement)

    if retagged and diagnostics:
        pairs = sorted({change for change in retagged.values()})
        diagnostics.add_info(
            "Renamed Blender colour-space tokens RealityKit has no alias for: "
            + ", ".join(f"{old} -> {new}" for old, new in pairs)
            + f" ({len(retagged)} prims)"
        )


def _corner_uvs(prim, corner_count):
    """The mesh's UV set resolved to one value per face corner, or None."""
    from pxr import UsdGeom

    primvars = UsdGeom.PrimvarsAPI(prim)
    uv_primvar = None
    for primvar in primvars.GetPrimvars():
        if primvar.GetTypeName().role == "TextureCoordinate":
            # ``st`` is the set the MaterialX texcoord reader samples; any
            # other set only stands in when a mesh has no ``st``.
            if primvar.GetPrimvarName() == "st":
                uv_primvar = primvar
                break
            if uv_primvar is None:
                uv_primvar = primvar
    if uv_primvar is None:
        return None
    values = uv_primvar.Get()
    if not values:
        return None
    indices = uv_primvar.GetIndices()
    interpolation = uv_primvar.GetInterpolation()
    if indices:
        if len(indices) != corner_count:
            return None
        return [values[index] for index in indices]
    if interpolation == UsdGeom.Tokens.faceVarying and len(values) == corner_count:
        return list(values)
    return None


def _meshes_reading_primvar(stage, primvar_name):
    """Mesh prims whose bound material, or a face subset's, reads ``primvar_name``."""
    from pxr import UsdGeom, UsdShade

    reading = set()
    for prim in stage.Traverse():
        shader = UsdShade.Shader(prim)
        if not shader:
            continue
        geomprop = shader.GetInput("geomprop")
        if geomprop and str(geomprop.Get() or "") == primvar_name:
            parent = prim.GetParent()
            while parent and not parent.IsA(UsdShade.Material):
                parent = parent.GetParent()
            if parent:
                reading.add(parent.GetPath())
    if not reading:
        return []

    def bound(prim):
        try:
            material, _relationship = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        except Exception:
            return False
        return bool(material) and material.GetPrim().GetPath() in reading

    meshes = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        subsets = [child for child in prim.GetChildren() if child.IsA(UsdGeom.Subset)]
        if bound(prim) or any(bound(subset) for subset in subsets):
            meshes.append(prim)
    return meshes


def _uv_derivatives(points, counts, indices, uvs):
    """Object-space dP/du and dP/dv per face corner, unnormalised.

    Each corner uses the triangle it forms with its two neighbours in the
    face. A corner whose UVs are degenerate keeps zero vectors.
    """
    from pxr import Gf

    zero = Gf.Vec3f(0.0, 0.0, 0.0)
    dpdu, dpdv = [zero] * len(indices), [zero] * len(indices)
    corner = 0
    for count in counts:
        if count < 3:
            corner += count
            continue
        for k in range(count):
            i0 = corner + k
            i1 = corner + (k + 1) % count
            i2 = corner + (k - 1) % count
            p0, p1, p2 = (Gf.Vec3f(points[indices[i]]) for i in (i0, i1, i2))
            uv0, uv1, uv2 = (uvs[i] for i in (i0, i1, i2))
            du1, dv1 = uv1[0] - uv0[0], uv1[1] - uv0[1]
            du2, dv2 = uv2[0] - uv0[0], uv2[1] - uv0[1]
            determinant = du1 * dv2 - du2 * dv1
            if abs(determinant) < 1e-12:
                continue
            e1, e2 = p1 - p0, p2 - p0
            dpdu[i0] = Gf.Vec3f((e1 * dv2 - e2 * dv1) / determinant)
            dpdv[i0] = Gf.Vec3f((e2 * du1 - e1 * du2) / determinant)
        corner += count
    return dpdu, dpdv


def _author_uv_derivatives(stage, diagnostics=None) -> None:
    """Write dP/du and dP/dv on each mesh whose material has a Bump.

    The Bump translation differences its height along the UV parametrization
    and needs the surface's metric there to turn that into a gradient. These
    are the exported points and UVs, in object space.
    """
    from pxr import Sdf, UsdGeom, Vt

    from .materials.extract.core import UV_DERIVATIVE_PRIMVARS

    written, missing = [], []
    for prim in _meshes_reading_primvar(stage, UV_DERIVATIVE_PRIMVARS[0]):
        mesh = UsdGeom.Mesh(prim)
        points = mesh.GetPointsAttr().Get()
        counts = mesh.GetFaceVertexCountsAttr().Get()
        indices = mesh.GetFaceVertexIndicesAttr().Get()
        if not points or not counts or not indices:
            continue
        uvs = _corner_uvs(prim, len(indices))
        if uvs is None:
            missing.append(str(prim.GetPath()))
            continue
        for name, values in zip(UV_DERIVATIVE_PRIMVARS, _uv_derivatives(points, counts, indices, uvs)):
            primvar = UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
                name, Sdf.ValueTypeNames.Vector3fArray, UsdGeom.Tokens.faceVarying
            )
            primvar.Set(Vt.Vec3fArray(values))
        written.append(str(prim.GetPath()))
    if written and diagnostics:
        diagnostics.add_info("Wrote UV derivatives for Bump on: " + ", ".join(sorted(written)))
    if missing and diagnostics:
        diagnostics.add_warning(
            "These meshes have a Bump but no UV set, so their bump reads flat; unwrap them: "
            + ", ".join(sorted(missing))
        )


def _blender_object_name(prim):
    """The Blender object a prim was exported from, from the exporter's custom property."""
    while prim and prim.GetPath().pathString != "/":
        for attribute in prim.GetAttributes():
            if attribute.GetName().endswith(":blender:object_name"):
                value = attribute.Get()
                if value:
                    return str(value)
        prim = prim.GetParent()
    return None


def _author_object_random(stage, diagnostics=None) -> None:
    """Write Cycles' Object Info Random on each mesh whose material reads it.

    The value is ``cycles_object_random`` of the Blender object's name, which
    the USD exporter records as a custom property on the object's prim, and it
    is written as one vertex primvar value per point so a geometric-property
    read returns it everywhere on the mesh.
    """
    from pxr import Sdf, UsdGeom, Vt

    from .materials.extract.core import OBJECT_RANDOM_PRIMVAR, cycles_object_random

    written, unnamed = [], []
    for prim in _meshes_reading_primvar(stage, OBJECT_RANDOM_PRIMVAR):
        name = _blender_object_name(prim)
        if name is None:
            unnamed.append(str(prim.GetPath()))
            continue
        points = UsdGeom.Mesh(prim).GetPointsAttr().Get() or []
        primvar = UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
            OBJECT_RANDOM_PRIMVAR, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex
        )
        primvar.Set(Vt.FloatArray([cycles_object_random(name)] * len(points)))
        written.append(str(prim.GetPath()))
    if written and diagnostics:
        diagnostics.add_info("Wrote Object Info Random for: " + ", ".join(sorted(written)))
    if unnamed and diagnostics:
        diagnostics.add_warning(
            "These meshes read Object Info Random but carry no Blender object name, because "
            "custom properties were not exported, so they read 0; export custom properties: "
            + ", ".join(sorted(unnamed))
        )


#: Colour primvars Blender writes for a mesh Color Attribute, in the order it
#: writes them. displayColor/displayOpacity are USD's conventional vertex
#: colour channels and are what a MaterialX geomcolor read resolves as colour
#: set 0; Blender authors them empty and puts the data under the attribute's
#: own name instead, so a geomcolor read finds nothing and renders black.
_DISPLAY_COLOR = "primvars:displayColor"
_DISPLAY_OPACITY = "primvars:displayOpacity"
_COLOR_PRIMVAR_TYPES = ("color3f[]", "color4f[]")


def _blender_first_color_attribute(prim, context) -> "str | None":
    """The name of the mesh's *first* Blender colour attribute, or None.

    USD sorts primvars, Blender does not, so "pick the first colour primvar on
    the prim" and "pick the mesh's first colour attribute" are different
    orderings that only coincide by luck. A mesh with Paint (Blender-first, the
    one the material reads) and Mask publishes Mask, because M sorts before P.

    The exporter records the mesh datablock's name on the prim, so the real
    order can be read back rather than guessed. Returns None whenever Blender
    is not reachable - callers keep their positional fallback.
    """
    if context is None:
        return None
    attribute = prim.GetAttribute("userProperties:blender:data_name")
    data_name = str(attribute.Get() or "") if attribute else ""
    if not data_name:
        return None
    try:
        import bpy

        mesh = bpy.data.meshes.get(data_name)
        if mesh is None:
            return None
        attributes = mesh.color_attributes
        if len(attributes) == 0:
            return None
        return str(attributes[0].name)
    except Exception:
        # Test doubles, a renamed datablock, or no bpy at all. The positional
        # fallback still publishes something reasonable.
        return None


def _publish_vertex_colors_as_display_color(stage, diagnostics=None, context=None) -> None:
    """Copy a mesh's first colour attribute into displayColor/displayOpacity.

    Only meshes whose bound material actually reads vertex colours are
    touched, and an already-populated displayColor is never overwritten.

    "Bound" includes materials reached through a GeomSubset. Blender writes a
    multi-material mesh as one Mesh whose direct binding is slot 0 plus a
    GeomSubset per additional slot, so looking only at the direct binding meant
    a vertex-colour material in any slot but the first published nothing at all
    and the object rendered unlit.
    """
    from pxr import Sdf, UsdGeom, UsdShade, Vt

    def reads_vertex_color(material_prim) -> bool:
        if not material_prim or not material_prim.IsValid():
            return False
        for child in material_prim.GetChildren():
            shader = UsdShade.Shader(child)
            if shader and "geomcolor" in str(shader.GetIdAttr().Get() or ""):
                return True
        return False

    def any_bound_material_reads_vertex_color(prim) -> bool:
        bindings = [UsdShade.MaterialBindingAPI(prim)]
        for child in prim.GetChildren():
            if UsdGeom.Subset(child):
                bindings.append(UsdShade.MaterialBindingAPI(child))
        for binding in bindings:
            material = binding.ComputeBoundMaterial()[0]
            if reads_vertex_color(material.GetPrim() if material else None):
                return True
        return False

    published = []
    for prim in stage.Traverse():
        mesh = UsdGeom.Mesh(prim)
        if not mesh:
            continue
        if not any_bound_material_reads_vertex_color(prim):
            continue

        api = UsdGeom.PrimvarsAPI(prim)
        existing = api.GetPrimvar("displayColor")
        if existing and existing.Get():
            continue

        candidates = [
            primvar
            for primvar in api.GetPrimvars()
            if primvar.GetName() not in (_DISPLAY_COLOR, _DISPLAY_OPACITY)
            and str(primvar.GetTypeName()) in _COLOR_PRIMVAR_TYPES
            and primvar.Get()
        ]
        if not candidates:
            continue

        preferred = _blender_first_color_attribute(prim, context)
        source = None
        if preferred:
            source = next(
                (
                    primvar
                    for primvar in candidates
                    if primvar.GetBaseName() == preferred
                ),
                None,
            )
        if source is None:
            source = candidates[0]
            if len(candidates) > 1 and diagnostics:
                diagnostics.add_warning(
                    f"{prim.GetPath()} carries {len(candidates)} colour attributes "
                    f"({', '.join(primvar.GetBaseName() for primvar in candidates)}) "
                    f"and Blender's own order could not be read, so displayColor "
                    f"was published from '{source.GetBaseName()}'. Rename the one "
                    f"your material reads so it sorts first, or delete the others."
                )

        values = source.Get()
        interpolation = source.GetInterpolation()
        colors = Vt.Vec3fArray([(v[0], v[1], v[2]) for v in values])
        api.CreatePrimvar(
            "displayColor", Sdf.ValueTypeNames.Color3fArray, interpolation
        ).Set(colors)
        if len(values) and len(values[0]) > 3:
            alphas = Vt.FloatArray([float(v[3]) for v in values])
            api.CreatePrimvar(
                "displayOpacity", Sdf.ValueTypeNames.FloatArray, interpolation
            ).Set(alphas)
        published.append(f"{prim.GetPath()} <- {source.GetName()}")

    if published and diagnostics:
        diagnostics.add_info(
            "Published vertex colours as displayColor so RealityKit's "
            "vertex-colour reader resolves them: " + ", ".join(sorted(published))
        )
