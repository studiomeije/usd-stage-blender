"""
Texture baking utilities for baked exports.

Bakes base color and optional opacity textures per object/material.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional
import re
from contextlib import contextmanager

import bpy

from .materials.extract.core import _active_output_node, material_has_transparency

_BAKE_IMAGE_FORMATS = {
    "AVIF": {
        "file_format": "AVIF",
        "extension": ".avif",
    },
    "PNG": {
        "file_format": "PNG",
        "extension": ".png",
    },
    "ORIGINAL": {
        "file_format": "ORIGINAL",
        "extension": "",
    },
}

# Every lobe/direction a COMBINED bake can contribute. Passed explicitly so the
# bake never inherits the scene's use_pass_* toggles (see _bake_object_pass).
COMBINED_PASS_FILTER = frozenset(
    {'DIRECT', 'INDIRECT', 'DIFFUSE', 'GLOSSY', 'TRANSMISSION', 'EMIT'}
)

_DEFAULT_BAKE_RESOLUTION = 2048
# Two mixed Principled BSDFs whose constant inputs differ by no more than this
# are treated as sharing one value for passthrough purposes.
_MIX_EPSILON = 1e-4
# Floor for source-keyed bake sizes: a tiled source texture flattens its
# repetition into the bake, so tiny tiles must not force a tiny bake.
_MIN_SOURCE_BAKE_RESOLUTION = 512
_DEFAULT_BAKE_IMAGE_FORMAT = "PNG"
_DEFAULT_BAKE_MARGIN = 8


class BakeResult:
    """Holds bake session data for restoration/cleanup."""

    def __init__(self):
        self.original_materials: Dict[object, List[Optional[object]]] = {}
        self.baked_materials: List[object] = []
        self.baked_images: List[object] = []
        self.temporary_images: List[object] = []
        # Slot link modes captured before baking. A slot switched to 'OBJECT'
        # so a shared mesh could hold per-instance bakes must be switched back.
        self.original_slot_links: Dict[object, List[str]] = {}


def _forget_image(result: BakeResult, image) -> None:
    """Drop ``image`` from the result's tracking lists by identity.

    Must be called before ``bpy.data.images.remove``: a freed datablock raises
    ReferenceError on *any* attribute access, so leaving it in ``baked_images``
    turns a later read into a hard failure at the end of a successful bake.
    """
    for images in (result.baked_images, result.temporary_images):
        images[:] = [tracked for tracked in images if tracked is not image]


def bake_materials_for_objects(
    context,
    settings,
    objects,
    output_dir: Path,
    diagnostics=None,
    progress_callback=None,
) -> BakeResult:
    """Bake atomically, restoring every material slot when baking fails.

    Callers normally restore a successful result in their own ``finally`` block.
    Before this wrapper existed, an exception inside the bake meant no result was
    returned, so those callers had no handle with which to restore slots or
    remove partially-built material datablocks. Keep the result alive across the
    implementation call and roll it back here before re-raising.
    """
    object_list = list(objects)
    result = BakeResult()
    result.original_materials = {
        obj: [slot.material for slot in obj.material_slots]
        for obj in object_list
        if getattr(obj, "type", None) == 'MESH'
    }
    try:
        return _bake_materials_for_objects_impl(
            context,
            settings,
            object_list,
            output_dir,
            diagnostics,
            progress_callback,
            result=result,
        )
    except BaseException:
        try:
            restore_baked_materials(result)
        except Exception:
            # Cleanup must never replace the original bake failure. Individual
            # datablock operations are already best-effort below; this final
            # guard also covers an object being deleted by a failing operator.
            pass
        raise


def _bake_materials_for_objects_impl(
    context,
    settings,
    objects,
    output_dir: Path,
    diagnostics=None,
    progress_callback=None,
    *,
    result: Optional[BakeResult] = None,
) -> BakeResult:
    """Bake textures for mesh objects and replace their materials with baked versions."""
    result = result or BakeResult()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    bake_mode = resolve_bake_mode(settings)
    resolution = resolve_effective_bake_resolution(settings)
    image_format = _resolve_bake_image_format(settings, diagnostics, safe_for_blender_save=True)
    margin = _resolve_bake_margin(settings)
    bake_base = bool(getattr(settings, "bake_base_color", True))
    bake_opacity = bool(getattr(settings, "bake_opacity", True))
    bake_roughness_map = bake_mode == "LIT_ALBEDO"
    isolate_meshes_lit = bool(getattr(settings, "bake_isolate_meshes_lit", False))
    roughness_single = (str(getattr(settings, "bake_roughness_mode", "TEXTURE")) == "AVERAGE")

    mesh_objects = [obj for obj in objects if getattr(obj, "type", None) == 'MESH']

    # Snapshot every object's source materials BEFORE any baking begins. Material
    # slots are DATA-linked by default, so assigning a baked material to one
    # object writes it onto the *shared mesh datablock* - which instantly changes
    # the "source" material seen by every sibling instance still to be processed.
    # Reading the source per-object inside the loop would therefore key later
    # pawns off an already-baked material, miss the reuse cache, and re-bake a
    # private texture each (also defeating the exporter's instancing). Keying off
    # this pre-bake snapshot keeps every instance resolving to the same original
    # material -> same cache key -> one shared bake. It also keeps
    # ``result.original_materials`` pointing at the true originals for restore.
    original_slot_materials: Dict[object, List[Optional[object]]] = {
        obj: [slot.material for slot in obj.material_slots] for obj in mesh_objects
    }

    # Fail-closed pre-pass: refuse (with a precise, actionable message) any
    # Mix Shader material this bake mode cannot handle honestly, BEFORE any
    # baking mutates materials. See check_mix_shader_bakeable for the decision
    # table. Raising here matches the missing-UV refusal pattern below.
    checked_materials = set()
    for obj in mesh_objects:
        for source_mat in original_slot_materials[obj]:
            if not source_mat or source_mat in checked_materials:
                continue
            checked_materials.add(source_mat)
            refusal = check_mix_shader_bakeable(
                source_mat, bake_mode, bake_opacity=bake_opacity
            )
            if refusal:
                if diagnostics:
                    diagnostics.add_error(refusal)
                raise RuntimeError(refusal)

    # One analysis per unique source material, shared by the step pre-count, the
    # cache-key pre-pass and the bake loop. A single source of truth is what
    # keeps the progress total aligned with the passes that actually run (they
    # can't drift apart when both derive from the same dict), and it avoids
    # re-walking the same node tree once per object that shares the material.
    material_analysis: Dict[object, Dict[str, object]] = {}

    def _analyze_material(mat) -> Dict[str, object]:
        info = material_analysis.get(mat)
        if info is None:
            passthrough = _validate_bake_material_contract(
                mat,
                bake_mode=bake_mode,
                diagnostics=diagnostics,
            )
            info = {
                # Flat short-circuiting only applies in the material-color-only
                # modes; LIT_IBL bakes lighting onto every surface, flat or not.
                "flat": (
                    _flat_material_constants(mat, lit=(bake_mode == "LIT_ALBEDO"))
                    if bake_mode != "LIT_IBL"
                    else None
                ),
                "needs_opacity": _material_needs_opacity(mat),
                "resolution": (
                    resolution if resolution > 0 else _material_source_resolution(mat)
                ),
                "normal": passthrough.get("normal"),
                "metallic": passthrough.get("metallic"),
                "roughness_passthrough": passthrough.get("roughness_passthrough"),
                "principled_constants": passthrough.get("principled_constants"),
            }
            material_analysis[mat] = info
        return info

    def _object_step_flags(obj) -> tuple:
        """(base, roughness, opacity) progress steps this object will run.

        Derived from the same ``_analyze_material`` results the bake loop uses,
        so the pre-counted total always matches the steps that actually execute
        - including the flat-material skips.
        """
        infos = [_analyze_material(m) for m in original_slot_materials[obj] if m]
        if not infos:
            return (False, False, False)
        has_nonflat = any(info["flat"] is None for info in infos)
        has_baked_roughness = any(
            info["flat"] is None and not info["roughness_passthrough"]
            for info in infos
        )
        return (
            bake_base and has_nonflat,
            bake_roughness_map and has_baked_roughness,
            bake_opacity
            and any(info["needs_opacity"] and info["flat"] is None for info in infos),
        )

    total_steps = 0
    for obj in mesh_objects:
        total_steps += sum(_object_step_flags(obj))
    if total_steps <= 0:
        total_steps = 1
    completed_steps = 0

    def _report_progress(message: str) -> None:
        if progress_callback:
            try:
                progress_callback(completed_steps / float(total_steps), message)
            except Exception:
                pass

    def _start_step(message: str) -> None:
        label = f"Step {completed_steps + 1}/{total_steps} - {message}"
        _report_progress(label)

    def _finish_step(message: str) -> None:
        nonlocal completed_steps
        completed_steps += 1
        _report_progress(f"Done {completed_steps}/{total_steps} - {message}")

    color_bake_type = 'DIFFUSE'
    color_pass_filter = {'COLOR'}
    if bake_mode == "LIT_IBL":
        color_bake_type = 'COMBINED'
        # Explicit, not None: an omitted pass_filter inherits the scene's
        # use_pass_* toggles and a .blend with them disabled bakes pure black.
        color_pass_filter = set(COMBINED_PASS_FILTER)

    # Maps a per-slot reuse key -> the baked material already produced for it.
    # Lets objects that share a source material + mesh under identical bake
    # parameters share one baked material instead of each getting a private
    # copy+bake, so the USD exporter can emit instanceable references.
    bake_cache: Dict[tuple, object] = {}
    # Reuse is only sound when the bake is position-independent. The LIT_IBL
    # COMBINED pass bakes path-traced lighting/shadows/AO in world space, so two
    # instances at different transforms genuinely differ - sharing one bake would
    # paint the first piece's lighting onto all of them. So in LIT_IBL every
    # instance bakes its own textures; the albedo/Lit-PBR modes bake pure
    # material color and can safely share.
    cache_enabled = bake_mode != "LIT_IBL"

    with _temporary_ibl_world(context, settings, diagnostics, enabled=(bake_mode == "LIT_IBL")):
        mesh_count = len(mesh_objects)
        for mesh_index, obj in enumerate(mesh_objects, start=1):
            uv_layer_name = _get_active_uv(obj)
            if not uv_layer_name:
                msg = f"Bake failed: '{obj.name}' has no UV map."
                if diagnostics:
                    diagnostics.add_error(msg)
                raise RuntimeError(msg)

            _report_progress(f"Preparing bake targets [{mesh_index}/{mesh_count}] - {obj.name}")

            original_mats = original_slot_materials[obj]
            result.original_materials[obj] = original_mats
            result.original_slot_links[obj] = [
                getattr(slot, "link", 'DATA') for slot in obj.material_slots
            ]

            # Identity (not name) of the mesh datablock: a baked texture is tied
            # to a specific UV layout, and distinct datablocks can share a name
            # (e.g. across linked libraries). Never id(): ``obj.data`` returns a
            # transient Python wrapper freed right after the expression, so its
            # address can be reused by the next object's wrapper (two different
            # meshes colliding to one id) and differs across accesses to the
            # same mesh (defeating reuse). session_uid / as_pointer() identify
            # the underlying datablock itself.
            mesh_data = obj.data
            mesh_id = getattr(mesh_data, "session_uid", None)
            if mesh_id is None:
                mesh_id = mesh_data.as_pointer()

            # Pre-compute a reuse key for every slot. Objects that share a source
            # material AND mesh datablock under identical bake parameters produce
            # byte-identical baked materials/textures, so they can share one baked
            # material instead of each getting its own copy+bake. That shared
            # binding is what lets the USD exporter emit instanceable references
            # (use_instancing) for e.g. all 8 pawns of a chess set.
            slot_keys = []
            for src in original_mats:
                if not src:
                    slot_keys.append(None)
                    continue
                info = _analyze_material(src)
                slot_keys.append(
                    _make_cache_key(
                        # name_full includes the library suffix, so a local and a
                        # library-linked material sharing a short name can't collide.
                        source_material_name=src.name_full,
                        mesh_id=mesh_id,
                        resolution=info["resolution"],
                        uv_layer=uv_layer_name,
                        bake_mode=bake_mode,
                        bake_base=bake_base,
                        use_opacity=info["needs_opacity"],
                        bake_roughness_map=bake_roughness_map,
                        roughness_single=roughness_single,
                        is_flat=info["flat"] is not None,
                    )
                )

            present_keys = [k for k in slot_keys if k is not None]
            if cache_enabled and present_keys and all(k in bake_cache for k in present_keys):
                # Every slot was already baked for an earlier object under the same
                # key: reuse those baked materials verbatim (no copy, no re-bake)
                # so the objects stay instanceable.
                for slot_idx, key in enumerate(slot_keys):
                    if key is None:
                        continue
                    obj.material_slots[slot_idx].material = bake_cache[key]
                # The pre-count included this object's steps (it can't know in
                # advance which objects will hit the cache), so mark them done
                # here - otherwise progress would stall short of 100% on every
                # scene with instanced duplicates.
                completed_steps += sum(_object_step_flags(obj))
                _report_progress(
                    f"Reusing baked materials [{mesh_index}/{mesh_count}] - {obj.name}"
                )
                continue

            baked_entries = []
            for slot_idx, slot in enumerate(obj.material_slots):
                # Use the pre-bake snapshot, not slot.material: an earlier sibling
                # sharing this DATA-linked mesh may have already overwritten the
                # slot with its baked material.
                source_mat = original_mats[slot_idx]
                if not source_mat:
                    baked_entries.append(None)
                    continue

                baked_mat = source_mat.copy()
                # Strip any existing _Baked chain before re-appending, so a baked
                # material that gets fed back in as a source (e.g. a prior bake
                # was saved into the slot, or "keep baked materials" was on) does
                # not compound into names like "Marble_Baked_Baked_Baked".
                baked_mat.name = _unique_name(
                    f"{_strip_baked_suffix(source_mat.name)}_Baked", bpy.data.materials
                )
                # Material slots are DATA-linked by default, so assigning here
                # writes onto the *shared mesh datablock*. With a shared mesh
                # that means the last instance to bake overwrites every earlier
                # instance's material - and in LIT_IBL, where the cache is
                # deliberately disabled so each instance captures its own
                # lighting, every instance ends up bound to the last one's bake.
                # A cube in full sun exported black because its linked duplicate
                # sat under an occluder. Switch the slot to OBJECT so the baked
                # material lands on this object; restore_baked_materials puts
                # the link back, which also restores the untouched mesh-level
                # material. Only for shared meshes: for a single-user mesh the
                # two are equivalent and DATA keeps the scene as the user left it.
                if getattr(getattr(obj, "data", None), "users", 1) > 1:
                    try:
                        slot.link = 'OBJECT'
                    except Exception:
                        pass
                slot.material = baked_mat
                result.baked_materials.append(baked_mat)

                # Only short-circuit flat materials in the material-color-only
                # modes (Unlit / Lit PBR), where the baked texture would just be
                # the constant color. In LIT_IBL the bake captures
                # lighting/shadows/AO onto every surface - including flat-colored
                # ones - so they must still be baked normally. (_analyze_material
                # already encodes that mode split.)
                info = _analyze_material(source_mat)
                flat_constants = info["flat"]
                mat_resolution = info["resolution"]

                entry = {
                    "source_material": source_mat,
                    "material": baked_mat,
                    "base_image": None,
                    "opacity_image": None,
                    "merged_opacity_image": None,
                    "roughness_image": None,
                    "roughness_value": None,
                    "use_opacity": info["needs_opacity"],
                    "surface_render_method": _surface_render_method(
                        source_mat,
                        transparent=bool(info["needs_opacity"]),
                    ),
                    "uv_layer": uv_layer_name,
                    "flat": flat_constants,
                    "throwaway_image": None,
                    "resolution": mat_resolution,
                    "cache_key": slot_keys[slot_idx],
                    # Pass the source normal map through untouched - we never
                    # bake a normal pass, and only LIT_ALBEDO authors a lit
                    # material that uses it (Unlit output ignores normals).
                    "normal": info.get("normal"),
                    "metallic": info.get("metallic"),
                    "roughness_passthrough": info.get("roughness_passthrough"),
                    "principled_constants": info.get("principled_constants"),
                }

                if flat_constants is not None:
                    # Flat-color material: there is nothing texture-varying to bake,
                    # so skip baking entirely. Baking it would render a constant into
                    # a full-resolution texture (wasted file size) and, when the flat
                    # material's faces have no real UV unwrap, produce an all-black
                    # texture. The constant is authored directly in _build_baked_material.
                    #
                    # A whole-object bake still touches these faces when the object also
                    # has textured materials, and bpy.ops.object.bake errors on any slot
                    # without an active image node. Attach a tiny throwaway target to
                    # satisfy that requirement; it is never saved and is removed below.
                    throwaway = _create_bake_image(
                        name=f"{obj.name}_{baked_mat.name}_skipflat",
                        filepath=Path(""),
                        width=4,
                        height=4,
                        colorspace="sRGB",
                        file_format=image_format["file_format"],
                    )
                    entry["throwaway_image"] = throwaway
                    result.temporary_images.append(throwaway)
                    _set_active_image_node(baked_mat, throwaway, uv_layer_name)
                elif bake_base:
                    base_image_path = _make_image_path(
                        output_dir,
                        obj.name,
                        baked_mat.name,
                        "baseColor",
                        image_format["extension"],
                    )
                    base_image = _create_bake_image(
                        name=f"{obj.name}_{baked_mat.name}_baseColor",
                        filepath=base_image_path,
                        width=mat_resolution,
                        height=mat_resolution,
                        colorspace="sRGB",
                        file_format=image_format["file_format"],
                    )
                    entry["base_image"] = base_image
                    result.baked_images.append(base_image)
                    _set_active_image_node(baked_mat, base_image, uv_layer_name)

                baked_entries.append(entry)

            has_base_targets = any(entry and entry.get("base_image") for entry in baked_entries)
            # Roughness (like base color) only has non-flat slots to bake into,
            # and a slot whose roughness passes through takes nothing from the
            # pass; an object with neither would just render into throwaway
            # targets. This gate must stay aligned with _object_step_flags'
            # pre-count.
            has_roughness_targets = any(
                entry
                and not entry.get("flat")
                and not entry.get("roughness_passthrough")
                for entry in baked_entries
            )
            isolate_meshes = bake_mode == "LIT_IBL" and isolate_meshes_lit

            with _temporary_mesh_isolation(context, obj, enabled=isolate_meshes):
                if bake_base and has_base_targets:
                    label = "Baking lighting and shadows" if bake_mode == "LIT_IBL" else "Baking material color"
                    step_message = f"{label} [{mesh_index}/{mesh_count}] - {obj.name}"
                    _start_step(step_message)
                    _select_object(context, obj)
                    # COMBINED (LIT_IBL) bakes real lighting and needs the user's
                    # samples; the albedo-only DIFFUSE/COLOR pass does not.
                    base_samples = None if bake_mode == "LIT_IBL" else 1
                    with _temporary_cycles_samples(context, base_samples):
                        _bake_object_pass(
                            context,
                            obj,
                            bake_type=color_bake_type,
                            pass_filter=color_pass_filter,
                            margin=margin,
                        )
                    for entry in baked_entries:
                        if not entry or not entry.get("base_image"):
                            continue
                        if entry.get("use_opacity"):
                            # Cycles weights the surface by its Alpha, so the
                            # color bake of a transparent slot is premultiplied.
                            _unpremultiply_color_bake(entry["base_image"])
                        entry["base_image"].save()
                        if diagnostics:
                            diagnostics.add_generated_file(
                                "baked_base_color",
                                getattr(entry["base_image"], "filepath_raw", ""),
                                object=obj.name,
                                material=entry["source_material"].name,
                            )
                    _finish_step(step_message)

                if bake_roughness_map and has_roughness_targets:
                    step_message = f"Baking roughness [{mesh_index}/{mesh_count}] - {obj.name}"
                    _start_step(step_message)
                    # The ROUGHNESS pass bakes the whole object. A slot whose
                    # roughness passes through never wires a baked roughness in,
                    # so it gets a tiny unsaved target instead of a roughness
                    # image - and it needs one, or the pass writes into its
                    # active base color image.
                    rough_throwaways = []
                    for entry in baked_entries:
                        if not entry or entry.get("flat") or not entry.get("roughness_passthrough"):
                            continue
                        baked_mat = entry["material"]
                        throwaway = _create_bake_image(
                            name=f"{obj.name}_{baked_mat.name}_skiproughness",
                            filepath=Path(""),
                            width=4,
                            height=4,
                            colorspace="Non-Color",
                            file_format=image_format["file_format"],
                        )
                        rough_throwaways.append(throwaway)
                        result.temporary_images.append(throwaway)
                        _set_active_image_node(baked_mat, throwaway, entry["uv_layer"])
                    if roughness_single:
                        # Averaged roughness only needs a tiny bake; cap at 64px.
                        # (resolution may be 0 here, the "use source resolution" sentinel.)
                        small_res = min(resolution, 64) if resolution > 0 else 64
                        for entry in baked_entries:
                            if not entry or entry.get("flat") or entry.get("roughness_passthrough"):
                                continue
                            baked_mat = entry["material"]
                            rough_image = _create_bake_image(
                                name=f"{obj.name}_{baked_mat.name}_roughness",
                                filepath=Path(""),
                                width=small_res,
                                height=small_res,
                                colorspace="Non-Color",
                                file_format=image_format["file_format"],
                            )
                            entry["roughness_image"] = rough_image
                            result.temporary_images.append(rough_image)
                            # Mark every texel as uncovered before baking. The
                            # bake writes alpha 1 wherever it touches, so what
                            # is left at alpha 0 is texture space no UV island
                            # reaches - see _average_image_value.
                            _mark_pixels_uncovered(rough_image)
                            _set_active_image_node(baked_mat, rough_image, entry["uv_layer"])

                        _select_object(context, obj)
                        with _temporary_cycles_samples(context, 1):
                            _bake_object_pass(
                                context,
                                obj,
                                bake_type='ROUGHNESS',
                                pass_filter=None,
                                margin=margin,
                                # Keep the sentinel: use_clear would reset the
                                # whole buffer and destroy the coverage mask.
                                use_clear=False,
                            )
                        for entry in baked_entries:
                            if not entry or not entry.get("roughness_image"):
                                continue
                            rough_image = entry["roughness_image"]
                            entry["roughness_value"] = _average_image_value(rough_image)
                            entry["roughness_image"] = None
                            ## Force-remove the throwaway roughness bake image. A texture
                            ## node still references it here, so the old users==0 guard never
                            ## fired and leaked images with an empty ("." ) filepath into
                            ## bpy.data, which breaks downstream texture staging.
                            try:
                                bpy.data.images.remove(rough_image, do_unlink=True)
                            except Exception:
                                pass
                    else:
                        for entry in baked_entries:
                            if not entry or entry.get("flat") or entry.get("roughness_passthrough"):
                                continue
                            baked_mat = entry["material"]
                            rough_image_path = _make_image_path(
                                output_dir,
                                obj.name,
                                baked_mat.name,
                                "roughness",
                                image_format["extension"],
                            )
                            rough_image = _create_bake_image(
                                name=f"{obj.name}_{baked_mat.name}_roughness",
                                filepath=rough_image_path,
                                width=entry["resolution"],
                                height=entry["resolution"],
                                colorspace="Non-Color",
                                file_format=image_format["file_format"],
                            )
                            entry["roughness_image"] = rough_image
                            result.baked_images.append(rough_image)
                            _set_active_image_node(baked_mat, rough_image, entry["uv_layer"])

                        _select_object(context, obj)
                        with _temporary_cycles_samples(context, 1):
                            _bake_object_pass(
                                context,
                                obj,
                                bake_type='ROUGHNESS',
                                pass_filter=None,
                                margin=margin,
                            )
                        for entry in baked_entries:
                            if not entry or not entry.get("roughness_image"):
                                continue
                            entry["roughness_image"].save()
                            if diagnostics:
                                diagnostics.add_generated_file(
                                    "baked_roughness",
                                    getattr(entry["roughness_image"], "filepath_raw", ""),
                                    object=obj.name,
                                    material=entry["source_material"].name,
                                )
                    for throwaway in rough_throwaways:
                        _forget_image(result, throwaway)
                        try:
                            bpy.data.images.remove(throwaway, do_unlink=True)
                        except Exception:
                            pass
                    _finish_step(step_message)

                # Only materials that are actually transparent get an opacity
                # bake. An opaque material's alpha is a constant 1.0, so a baked
                # opacity map would be a flat-white texture that is never wired
                # into the material (see _build_baked_material) - pure wasted
                # bake time plus an orphan file left in the export's textures
                # dir. So gate the whole pass on real transparency.
                opacity_targets = [
                    entry
                    for entry in baked_entries
                    if entry and not entry.get("flat") and entry.get("use_opacity")
                ]
                if bake_opacity and opacity_targets:
                    step_message = f"Baking opacity [{mesh_index}/{mesh_count}] - {obj.name}"
                    _start_step(step_message)
                    # The EMIT pass bakes the whole object, so every non-flat slot
                    # needs an active bake target - otherwise the pass overwrites
                    # the just-baked base color of opaque slots. Slots that aren't
                    # getting an opacity map get a tiny throwaway target instead,
                    # removed right after the bake. (Flat slots are already pointed
                    # at their own throwaway from the base-color pass.)
                    emit_throwaways = []
                    for entry in baked_entries:
                        if not entry or entry.get("flat"):
                            continue
                        baked_mat = entry["material"]
                        if not entry.get("use_opacity"):
                            throwaway = _create_bake_image(
                                name=f"{obj.name}_{baked_mat.name}_skipopacity",
                                filepath=Path(""),
                                width=4,
                                height=4,
                                colorspace="Non-Color",
                                file_format=image_format["file_format"],
                            )
                            emit_throwaways.append(throwaway)
                            result.temporary_images.append(throwaway)
                            _set_active_image_node(baked_mat, throwaway, entry["uv_layer"])
                            continue
                        opacity_image_path = _make_image_path(
                            output_dir,
                            obj.name,
                            baked_mat.name,
                            "opacity",
                            image_format["extension"],
                        )
                        opacity_image = _create_bake_image(
                            name=f"{obj.name}_{baked_mat.name}_opacity",
                            filepath=opacity_image_path,
                            # Must match the base image so opacity can be merged into its alpha.
                            width=entry["resolution"],
                            height=entry["resolution"],
                            colorspace="Non-Color",
                            file_format=image_format["file_format"],
                        )
                        entry["opacity_image"] = opacity_image
                        result.baked_images.append(opacity_image)
                        _set_active_image_node(baked_mat, opacity_image, entry["uv_layer"])
                        _configure_emission_for_alpha(baked_mat)

                    _select_object(context, obj)
                    with _temporary_cycles_samples(context, 1):
                        _bake_object_pass(
                            context,
                            obj,
                            bake_type='EMIT',
                            pass_filter=None,
                            margin=margin,
                        )
                    for entry in baked_entries:
                        if not entry or not entry.get("opacity_image"):
                            continue
                        entry["opacity_image"].save()
                        if diagnostics:
                            diagnostics.add_generated_file(
                                "baked_opacity",
                                getattr(entry["opacity_image"], "filepath_raw", ""),
                                object=obj.name,
                                material=entry["source_material"].name,
                            )
                    for throwaway in emit_throwaways:
                        try:
                            bpy.data.images.remove(throwaway, do_unlink=True)
                        except Exception:
                            pass
                    _finish_step(step_message)

                    for entry in opacity_targets:
                        merged = _merge_opacity_into_base_image(
                            entry.get("base_image"),
                            entry.get("opacity_image"),
                        )
                        if merged:
                            entry["merged_opacity_image"] = entry.get("opacity_image")
                            entry["opacity_image"] = None

            for entry in baked_entries:
                if not entry:
                    continue
                # Remove the flat-slot throwaway target before authoring the final
                # material. It is only needed to satisfy the whole-object bake (now
                # finished) and is not tracked in result.baked_images, so doing this
                # first guarantees it can't leak if _build_baked_material raises.
                throwaway_image = entry.get("throwaway_image")
                if throwaway_image is not None:
                    try:
                        bpy.data.images.remove(throwaway_image, do_unlink=True)
                    except Exception:
                        pass
                    entry["throwaway_image"] = None
                _build_baked_material(
                    entry["material"],
                    entry.get("base_image"),
                    entry.get("opacity_image") if entry.get("use_opacity") else None,
                    entry.get("use_opacity", False),
                    uv_layer=entry.get("uv_layer"),
                    roughness_image=entry.get("roughness_image"),
                    roughness_value=entry.get("roughness_value"),
                    roughness_passthrough=entry.get("roughness_passthrough"),
                    flat=entry.get("flat"),
                    normal=entry.get("normal"),
                    metallic=entry.get("metallic"),
                    principled_constants=entry.get("principled_constants"),
                    surface_render_method=entry.get("surface_render_method", "DITHERED"),
                )
                # First object to bake this key owns the shared baked material;
                # later objects with the same key reuse it (see the pre-pass
                # above) so they export as instances. Disabled in LIT_IBL, where
                # each instance must keep its own position-dependent lighting.
                cache_key = entry.get("cache_key")
                if cache_enabled and cache_key is not None:
                    bake_cache.setdefault(cache_key, entry["material"])
                merged_opacity_image = entry.get("merged_opacity_image")
                if merged_opacity_image is not None and getattr(merged_opacity_image, "users", 0) == 0:
                    # Drop every reference before freeing the datablock.
                    # Consumers iterate result.baked_images and read
                    # image.filepath_raw; touching a removed datablock raises
                    # ReferenceError, which getattr's default does not absorb.
                    _forget_image(result, merged_opacity_image)
                    entry["merged_opacity_image"] = None
                    try:
                        bpy.data.images.remove(merged_opacity_image)
                    except Exception:
                        pass

    return result


def _warn_if_scene_has_no_illumination(context, diagnostics) -> None:
    """Warn when a Lighting & Shadows bake has no light to capture.

    LIT_IBL renders incoming light onto every surface. With no world and no
    light object the only possible contribution is material emission, so an
    otherwise ordinary scene bakes to pure black and still reports success.
    Measured: world None, no lights, one 2048 texture, mean red 0.0000, no
    warning anywhere in the response or diagnostics.

    A warning rather than a refusal: emissive materials are a legitimate sole
    light source, and this check cannot cheaply prove their absence. The
    message names exactly what was detected so a false positive is obvious.
    """
    if diagnostics is None:
        return
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    if getattr(scene, "world", None) is not None:
        return
    try:
        has_light = any(
            getattr(obj, "type", None) == 'LIGHT' for obj in scene.objects
        )
    except Exception:
        return
    if has_light:
        return

    diagnostics.add_warning(
        "Lighting & Shadows bake: the scene has no World and no light objects, "
        "so nothing illuminates it. Baked textures will be black unless a "
        "material is emissive. Add a World or a light, or choose a "
        "Material Color Only bake mode."
    )


@contextmanager
def _temporary_ibl_world(context, settings, diagnostics=None, enabled: bool = True):
    """Temporarily override the scene World with a known IBL (HDRI) setup."""
    if not enabled:
        yield
        return

    source = str(getattr(settings, "bake_ibl_source", "SCENE_WORLD") or "SCENE_WORLD")
    if source != "HDRI_FILE":
        # No HDRI override: the scene lights itself, so check that it can.
        _warn_if_scene_has_no_illumination(context, diagnostics)
        yield
        return

    try:
        hdri_file = _resolve_hdri_filepath(settings)
    except RuntimeError as exc:
        msg = str(exc)
        if diagnostics:
            diagnostics.add_error(msg)
        raise

    strength = float(getattr(settings, "bake_ibl_strength", 1.0))
    rotation = float(getattr(settings, "bake_ibl_rotation", 0.0))  # stored in radians (ANGLE subtype)

    scene = context.scene
    original_world = scene.world
    temp_world = None
    try:
        temp_world = _create_hdri_world(str(hdri_file), strength, rotation)
        scene.world = temp_world
        yield
    finally:
        try:
            scene.world = original_world
        except Exception:
            pass
        if temp_world is not None:
            try:
                bpy.data.worlds.remove(temp_world)
            except Exception:
                pass


def _resolve_hdri_filepath(settings, *, blend_file: str | Path | None = None) -> Path:
    """Resolve the explicit bake HDRI without losing Blender ``//`` semantics.

    ``pathlib`` treats ``//studio.hdr`` as a filesystem-root path, while Blender
    means "next to the current .blend".  Resolve that form explicitly while the
    source scene is still active.  Background jobs serialize the returned
    absolute path before saving their temporary scene copy, so loading the copy
    from a private job directory cannot retarget the lighting source.
    """
    raw_path = str(getattr(settings, "bake_ibl_filepath", "") or "").strip()
    if not raw_path:
        raise RuntimeError(
            "Bake mode is 'Lighting & Shadows' but no HDRI file is set."
        )

    expanded = Path(raw_path).expanduser()
    if raw_path.startswith("//"):
        source_blend = str(
            blend_file
            if blend_file is not None
            else getattr(getattr(bpy, "data", None), "filepath", "")
            or ""
        ).strip()
        if not source_blend:
            raise RuntimeError(
                f"HDRI path '{raw_path}' is relative to a .blend file, but the "
                "scene has never been saved. Save the .blend or choose an "
                "absolute HDRI path before baking."
            )
        expanded = Path(source_blend).expanduser().parent / raw_path[2:]
    elif not expanded.is_absolute():
        # Ordinary CLI-relative paths retain normal shell/CWD semantics. Blender
        # ``//`` paths are the only form that is relative to the source .blend.
        expanded = Path.cwd() / expanded

    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = expanded.absolute()

    if not resolved.is_file():
        raise RuntimeError(
            f"HDRI file not found: {raw_path} (resolved to {resolved}). "
            "Relink it, or choose an existing absolute path."
        )
    return resolved


@contextmanager
def _temporary_cycles_samples(context, samples: Optional[int]):
    """Temporarily override Cycles bake sample count (and denoising).

    Property bakes - albedo (DIFFUSE/COLOR), roughness and opacity (EMIT) - are
    deterministic: their result is identical at 1 sample as at 64, so the scene's
    sample count is pure wasted render time on those passes. Only the LIT_IBL
    COMBINED pass bakes path-traced lighting and genuinely needs samples, so it
    passes ``samples=None`` to leave the user's setting untouched.
    """
    if samples is None:
        yield
        return

    cycles = getattr(context.scene, "cycles", None)
    if cycles is None:
        yield
        return

    original_samples = getattr(cycles, "samples", None)
    original_denoising = getattr(cycles, "use_denoising", None)
    try:
        try:
            cycles.samples = int(samples)
        except Exception:
            pass
        try:
            cycles.use_denoising = False
        except Exception:
            pass
        yield
    finally:
        if original_samples is not None:
            try:
                cycles.samples = original_samples
            except Exception:
                pass
        if original_denoising is not None:
            try:
                cycles.use_denoising = original_denoising
            except Exception:
                pass


@contextmanager
def _temporary_mesh_isolation(context, target_obj, enabled: bool = False):
    """Temporarily hide non-target meshes while baking a target object."""
    if not enabled:
        yield
        return

    hidden_states = []
    for obj in list(context.view_layer.objects):
        if obj == target_obj or getattr(obj, "type", None) != "MESH":
            continue
        try:
            hidden_states.append((obj, bool(obj.hide_viewport), bool(obj.hide_render)))
            obj.hide_viewport = True
            obj.hide_render = True
        except Exception:
            continue

    try:
        yield
    finally:
        for obj, old_hide_viewport, old_hide_render in hidden_states:
            try:
                obj.hide_viewport = old_hide_viewport
                obj.hide_render = old_hide_render
            except Exception:
                continue


def _create_hdri_world(hdri_path: str, strength: float, rotation_z: float):
    """Create a World datablock with an Environment Texture IBL."""
    world = bpy.data.worlds.new(name=_unique_name("USDStage_IBL", bpy.data.worlds))
    nt = world.node_tree
    nodes = nt.nodes
    links = nt.links
    nodes.clear()

    out = nodes.new("ShaderNodeOutputWorld")
    out.location = (600, 0)

    bg = nodes.new("ShaderNodeBackground")
    bg.location = (360, 0)
    bg.inputs["Strength"].default_value = strength

    env = nodes.new("ShaderNodeTexEnvironment")
    env.location = (0, 0)
    try:
        env.image = bpy.data.images.load(hdri_path, check_existing=True)
    except Exception as exc:
        raise RuntimeError(f"Failed to load HDRI image: {hdri_path} ({exc})") from exc

    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-240, 0)
    mapping.inputs["Rotation"].default_value[2] = rotation_z

    texcoord = nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-480, 0)

    links.new(texcoord.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], bg.inputs["Color"])
    links.new(bg.outputs["Background"], out.inputs["Surface"])

    return world


def restore_baked_materials(result: BakeResult) -> None:
    """Restore original materials and clean up baked data blocks."""
    for obj, materials in result.original_materials.items():
        links = result.original_slot_links.get(obj) or []
        for idx, mat in enumerate(materials):
            try:
                if idx >= len(obj.material_slots):
                    continue
                slot = obj.material_slots[idx]
                # Link first: a slot switched to OBJECT for a shared mesh must
                # go back to DATA before the material is written, otherwise the
                # original is restored onto the object while the baked material
                # stays on the mesh.
                if idx < len(links):
                    try:
                        slot.link = links[idx]
                    except Exception:
                        pass
                slot.material = mat
            except (ReferenceError, Exception):
                pass

    for mat in list(result.baked_materials):
        try:
            if mat.users == 0:
                bpy.data.materials.remove(mat)
        except (ReferenceError, Exception):
            pass

    for image in list(result.baked_images):
        try:
            if image.users == 0:
                bpy.data.images.remove(image)
        except (ReferenceError, Exception):
            pass

    # Throwaway flat/opaque targets and averaged-roughness targets are never
    # export artifacts. They may still be linked from a partially-built copied
    # material when a bake operator raises, so unlink them unconditionally as
    # part of the atomic rollback.
    for image in list(result.temporary_images):
        try:
            bpy.data.images.remove(image, do_unlink=True)
        except (ReferenceError, Exception):
            pass


def _resolve_bake_resolution(settings) -> int:
    # Returns a fixed bake resolution in pixels, or 0 meaning "use each
    # material's own source-texture resolution" (resolved per material at
    # bake time by _material_source_resolution).
    #
    # When texture overrides are off (the default), we bake at the source
    # resolution rather than forcing 2048 - a 1K material should stay 1K
    # instead of being upscaled to 2K (slower bakes, 4x larger files).
    if not _export_texture_settings_enabled(settings):
        return 0

    value = getattr(settings, "bake_resolution", "2048")
    if str(value).upper() == "ORIGINAL":
        return 0
    if value == 'CUSTOM':
        return int(getattr(settings, "bake_resolution_custom", _DEFAULT_BAKE_RESOLUTION))
    try:
        return int(value)
    except Exception:
        return _DEFAULT_BAKE_RESOLUTION


def resolve_bake_mode(settings) -> str:
    """The bake mode the bake runs; an unknown value bakes as ``LIT_IBL``."""
    bake_mode = str(getattr(settings, "bake_mode", "LIT_IBL") or "LIT_IBL")
    if bake_mode not in {"UNLIT_ALBEDO", "LIT_ALBEDO", "LIT_IBL"}:
        return "LIT_IBL"
    return bake_mode


def resolve_effective_bake_resolution(settings) -> int:
    """The bake size in pixels, or 0 for per-material source-keyed sizing.

    Source-keyed resolution only makes sense for material-color bakes. The
    LIT_IBL COMBINED pass bakes spatial lighting/shadow detail whose required
    resolution has nothing to do with the source texture sizes (a 256px
    tileable albedo says nothing about shadow gradients), so Lighting &
    Shadows always bakes a 0 at the fixed default. Anything that reports the
    bake size reads it here, so it reports the size the bake used.
    """
    resolution = _resolve_bake_resolution(settings)
    if resolution <= 0 and resolve_bake_mode(settings) == "LIT_IBL":
        return _DEFAULT_BAKE_RESOLUTION
    return resolution


def record_bake_diagnostics(
    diagnostics,
    settings,
    *,
    object_count: int,
    native_export_object_count: int,
    texture_dir,
) -> Dict[str, object]:
    """Record the bake's resolved decisions as the diagnostics ``bake`` block.

    Shared by ``bake-export`` and the background job so both lanes record the
    same block. The image format is resolved silently: the bake itself reports
    its format warnings, once.
    """
    block = {
        "mode": getattr(settings, "bake_mode", None),
        "resolution": resolve_effective_bake_resolution(settings),
        "image_format": _resolve_bake_image_format(
            settings, safe_for_blender_save=True, report=False
        )["file_format"],
        "margin": _resolve_bake_margin(settings),
        "base_color": bool(getattr(settings, "bake_base_color", False)),
        "opacity": bool(getattr(settings, "bake_opacity", False)),
        "isolate_meshes_lit": bool(getattr(settings, "bake_isolate_meshes_lit", False)),
        "texture_settings_enabled": _export_texture_settings_enabled(settings),
        "object_count": int(object_count),
        "native_export_object_count": int(native_export_object_count),
        "texture_dir": str(texture_dir),
    }
    if diagnostics is not None:
        diagnostics.data["bake"] = block
    return block


def remove_superseded_bake_outputs(usd_path, staging_dir, result: BakeResult, diagnostics=None):
    """Delete the bake's own output files the final USD no longer references.

    Texture staging copies every baked image to a content-addressed name, which
    leaves the file the bake saved - and any image nothing wired in - beside the
    USD in the staging directory, where publication and packaging would ship
    it. Shared by ``bake-export`` and the background job.
    """
    from . import usd_textures

    return usd_textures.remove_unreferenced_bake_outputs(
        usd_path,
        staging_dir,
        (
            bpy.path.abspath(str(getattr(image, "filepath_raw", "") or ""))
            for image in result.baked_images
        ),
        diagnostics,
    )


_BAKED_CHANNEL_INPUTS = ("Base Color", "Roughness", "Alpha")


def _active_material_output(material):
    """The Material Output Cycles renders, which is the one the bake renders."""
    if getattr(material, "node_tree", None) is None:
        return None
    return _active_output_node(material)


def _surface_principled_node(material):
    """Resolve the Principled shader feeding the active material surface.

    Blender files commonly retain disconnected test or legacy shader nodes. The
    bake must follow the active Material Output instead of picking the first
    Principled node in collection order. Only a directly connected Principled
    surface is returned: choosing one branch from a Mix/Add/Group graph is only
    sound where ``check_mix_shader_bakeable`` has proved the branches agree,
    which is ``_passthrough_principled``'s job. Unsupported active surface
    graphs are rejected by ``_validate_bake_material_contract`` before material
    mutation.
    """
    node_tree = getattr(material, "node_tree", None)
    if node_tree is None:
        return None

    output = _active_material_output(material)
    if output is None:
        return None
    surface = getattr(output, "inputs", {}).get('Surface')
    links = list(getattr(surface, "links", ()) or ()) if surface is not None else []
    if len(links) != 1:
        return None
    node = getattr(links[0], "from_node", None)
    return node if getattr(node, "type", None) == 'BSDF_PRINCIPLED' else None


def _make_cache_key(
    *,
    source_material_name: str,
    mesh_id: object,
    resolution: int,
    uv_layer: Optional[str],
    bake_mode: str,
    bake_base: bool,
    use_opacity: bool,
    bake_roughness_map: bool,
    roughness_single: bool,
    is_flat: bool,
) -> tuple:
    """Identity of a baked-material result for reuse across objects.

    Two slots that hash to the same key bake to byte-identical materials and
    textures, so the second (and later) slots can reuse the first's baked
    material instead of copying + re-baking. Reusing a shared binding is what
    lets the USD exporter emit instanceable references.

    The key deliberately includes the mesh datablock identity: a baked texture is
    tied to a specific UV layout, so two objects sharing a source material but
    different meshes (hence possibly different UVs) must NOT share a bake. It also
    includes every parameter that changes the baked output - resolution, UV
    layer, bake mode, and the base/opacity/roughness/flat flags - so a change in
    any of them forces a fresh bake rather than an incorrect cache hit.
    """
    return (
        source_material_name,
        mesh_id,
        int(resolution),
        uv_layer or "",
        bake_mode,
        bool(bake_base),
        bool(use_opacity),
        bool(bake_roughness_map),
        bool(roughness_single),
        bool(is_flat),
    )


def _material_source_resolution(material, fallback: int = _DEFAULT_BAKE_RESOLUTION) -> int:
    """Largest source-texture dimension feeding the baked channels, or *fallback*.

    Used when baking at "original" resolution so each material's baked maps
    match the size of the textures it was authored with, rather than a global
    default. Only images that actually drive the channels we bake (base color,
    roughness, alpha) are considered - traced upstream from those Principled
    inputs. Maps we do not bake (e.g. a high-res normal map) and disconnected
    texture nodes are ignored, so a 1K albedo is not upscaled just because some
    other input uses a 4K texture. Falls back when no such image is found or
    its size is unknown (e.g. a procedural-only or unloaded texture).

    The result is floored at ``_MIN_SOURCE_BAKE_RESOLUTION``: a small tileable
    texture repeated many times across the UV layout flattens into the bake, so
    keying the bake to the tile size would squeeze all that repetition into a
    handful of texels.
    """
    if getattr(material, "node_tree", None) is None:
        return fallback

    principled = _surface_principled_node(material)
    if principled is None:
        return fallback

    stack = [
        principled.inputs[name]
        for name in _BAKED_CHANNEL_INPUTS
        if principled.inputs.get(name) is not None and principled.inputs[name].is_linked
    ]
    visited = set()
    largest = 0
    while stack:
        socket = stack.pop()
        for link in socket.links:
            node = link.from_node
            if node in visited:
                continue
            visited.add(node)
            if node.type == 'TEX_IMAGE':
                image = getattr(node, "image", None)
                size = tuple(getattr(image, "size", ()) or ())
                for dimension in size[:2]:
                    if dimension and dimension > largest:
                        largest = int(dimension)
            else:
                stack.extend(inp for inp in node.inputs if inp.is_linked)
    if largest <= 0:
        return fallback
    return max(largest, _MIN_SOURCE_BAKE_RESOLUTION)


def _resolve_texture_override_resolution(settings) -> int:
    if not _export_texture_settings_enabled(settings):
        return _DEFAULT_BAKE_RESOLUTION

    value = getattr(settings, "bake_resolution", "2048")
    if str(value).upper() == "ORIGINAL":
        return 0
    if value == 'CUSTOM':
        return int(getattr(settings, "bake_resolution_custom", _DEFAULT_BAKE_RESOLUTION))
    try:
        return int(value)
    except Exception:
        return _DEFAULT_BAKE_RESOLUTION


def _resolve_bake_image_format(
    settings,
    diagnostics=None,
    *,
    safe_for_blender_save: bool = False,
    report: bool = True,
) -> Dict[str, str]:
    """Resolve the bake image format; ``report=False`` neither prints nor warns."""
    if _export_texture_settings_enabled(settings):
        requested = str(getattr(settings, "bake_image_format", _DEFAULT_BAKE_IMAGE_FORMAT) or _DEFAULT_BAKE_IMAGE_FORMAT).upper()
    else:
        requested = _DEFAULT_BAKE_IMAGE_FORMAT
    if requested not in _BAKE_IMAGE_FORMATS:
        requested = _DEFAULT_BAKE_IMAGE_FORMAT

    if requested == "ORIGINAL":
        if safe_for_blender_save:
            message = (
                "Original texture format is only available for existing texture staging; "
                "baked textures are saved as PNG."
            )
            if report:
                print(f"Warning: {message}")
                if diagnostics:
                    diagnostics.add_warning(message)
            requested = "PNG"
        else:
            return dict(_BAKE_IMAGE_FORMATS["ORIGINAL"])

    if requested == "AVIF":
        # Reality Composer Pro 3 refuses an AVIF texture on import
        # ("Unhandled image format"), loose beside a .usdc and inside a .usdz
        # alike, although the file itself is valid.
        message = (
            "AVIF textures are refused by Reality Composer Pro 3 on import; "
            "select PNG unless this package is loaded only by RealityKit at runtime."
        )
        if report and diagnostics and message not in diagnostics.data.get("warnings", []):
            diagnostics.add_warning(message)

    available = _available_image_file_formats()
    if available and requested not in available:
        fallback = "PNG"
        if report and diagnostics:
            diagnostics.add_warning(
                f"Bake image format '{requested}' is not supported by this Blender build; falling back to '{fallback}'."
            )
        requested = fallback

    return dict(_BAKE_IMAGE_FORMATS[requested])


def _resolve_bake_margin(settings) -> int:
    if not _export_texture_settings_enabled(settings):
        return _DEFAULT_BAKE_MARGIN
    try:
        return int(getattr(settings, "bake_margin", _DEFAULT_BAKE_MARGIN))
    except Exception:
        return _DEFAULT_BAKE_MARGIN


def _export_texture_settings_enabled(settings) -> bool:
    return bool(getattr(settings, "export_texture_settings_enabled", False))


def _available_image_file_formats() -> Optional[set[str]]:
    try:
        prop = bpy.types.Image.bl_rna.properties["file_format"]
        return {item.identifier for item in prop.enum_items}
    except Exception:
        return None


def _get_active_uv(obj) -> Optional[str]:
    uv_layers = getattr(obj.data, "uv_layers", None)
    if not uv_layers:
        return None
    active = uv_layers.active
    if active:
        return active.name
    if uv_layers:
        return uv_layers[0].name
    return None


def _get_surface_shader_node(material):
    """Shader node feeding the active Material Output's Surface, skipping reroutes."""
    if getattr(material, "node_tree", None) is None:
        return None
    active_output = _active_material_output(material)
    if active_output is None:
        return None
    socket = active_output.inputs.get('Surface')
    return _follow_link_source(socket)


def _surface_subtree_contains(surface, node_type: str) -> bool:
    """Whether *node_type* appears anywhere upstream of *surface* (inclusive)."""
    seen: set = set()
    stack = [surface]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if getattr(node, "type", "") == node_type:
            return True
        sockets = getattr(node, "inputs", None)
        # bpy collections and mapping-style test doubles both expose values();
        # plain iteration over a mapping would yield socket NAMES instead.
        if hasattr(sockets, "values"):
            sockets = sockets.values()
        for socket in sockets or []:
            if not getattr(socket, "is_linked", False):
                continue
            for link in getattr(socket, "links", ()) or ():
                stack.append(getattr(link, "from_node", None))
    return False


def _follow_link_source(socket):
    """The node feeding *socket*, with REROUTE nodes skipped. None when unlinked."""
    if socket is None or not socket.is_linked:
        return None
    node = socket.links[0].from_node
    seen = set()
    while node is not None and getattr(node, "type", "") == 'REROUTE' and node not in seen:
        seen.add(node)
        inp = node.inputs[0] if len(node.inputs) else None
        if inp is None or not inp.is_linked:
            return None
        node = inp.links[0].from_node
    return node


def _analyze_mix_shader_surface(material) -> Optional[Dict[str, object]]:
    """Describe a Mix Shader surface, or ``None`` when the surface is not one.

    Keys:
      - ``supported``: True when the mix blends exactly two directly-connected
        Principled BSDFs — the only shape whose per-channel semantics the
        Material Color bake modes can reason about honestly.
      - ``reason``: human-readable shape problem when unsupported.
      - ``fac``: the constant mix factor, or ``None`` when the factor is linked.
      - ``principled``: the two Principled nodes (Fac=0 side first) when supported.
      - ``selected``: the single effective Principled when ``fac`` is a constant
        0 or 1 (the mix degenerates to one BSDF), else ``None``.
    """
    surface = _get_surface_shader_node(material)
    if surface is None or getattr(surface, "type", "") != 'MIX_SHADER':
        return None

    info: Dict[str, object] = {
        "supported": False,
        "reason": None,
        "fac": None,
        "principled": None,
        "selected": None,
    }

    fac_socket = surface.inputs.get('Fac')
    if fac_socket is not None and not fac_socket.is_linked:
        try:
            info["fac"] = float(fac_socket.default_value)
        except Exception:
            pass

    shader_sockets = [s for s in surface.inputs if getattr(s, "type", "") == 'SHADER']
    sources = [_follow_link_source(s) for s in shader_sockets]
    if len(sources) != 2 or any(src is None for src in sources):
        info["reason"] = "Mix Shader does not have two connected shader inputs"
        return info

    bad_types = sorted({src.type for src in sources if src.type != 'BSDF_PRINCIPLED'})
    if bad_types:
        if 'MIX_SHADER' in bad_types:
            info["reason"] = "Mix Shader inputs are nested Mix Shaders"
        else:
            info["reason"] = (
                "Mix Shader mixes non-Principled shader(s): " + ", ".join(bad_types)
            )
        return info

    info["supported"] = True
    info["principled"] = (sources[0], sources[1])
    fac = info["fac"]
    if fac is not None:
        if abs(fac) <= _MIX_EPSILON:
            info["selected"] = sources[0]
        elif abs(fac - 1.0) <= _MIX_EPSILON:
            info["selected"] = sources[1]
    return info


def _mix_input_divergence(p1, p2, input_name: str, *, compare_values: bool = True) -> Optional[str]:
    """Describe how *input_name* differs between two Principled nodes, or ``None``.

    Two sockets agree when both are linked to the exact same source socket, or
    both are unlinked with (when *compare_values*) constants within
    ``_MIX_EPSILON``. Anything else is a divergence — never silently resolved.
    """
    s1 = p1.inputs.get(input_name)
    s2 = p2.inputs.get(input_name)
    if s1 is None or s2 is None:
        return None

    if s1.is_linked != s2.is_linked:
        return f"'{input_name}' inputs diverge (one is linked, the other is a constant)"
    if s1.is_linked:
        if s1.links[0].from_socket == s2.links[0].from_socket:
            return None
        return f"'{input_name}' inputs are linked to different sources"
    if not compare_values:
        return None
    try:
        v1 = float(s1.default_value)
        v2 = float(s2.default_value)
    except (TypeError, ValueError):
        return None
    if abs(v1 - v2) > _MIX_EPSILON:
        return f"'{input_name}' values diverge ({v1:g} vs {v2:g})"
    return None


def check_mix_shader_bakeable(material, bake_mode: str, *, bake_opacity: bool = True) -> Optional[str]:
    """Refusal message when a Mix Shader material cannot be baked honestly.

    Decision table (``None`` = bakeable):

    - Surface is not a Mix Shader: ``None`` (out of scope here).
    - LIT_IBL: always ``None``. The COMBINED pass renders the full shading
      (Cycles evaluates any mix) and the exported material is Unlit, so no
      per-channel passthrough exists to get wrong.
    - Unsupported mix shape (nested mixes, non-Principled inputs, unconnected
      inputs) in the Material Color modes: refused — per-channel semantics
      (albedo/roughness passes, opacity) are only defined for two Principled
      BSDFs. The message points at LIT_IBL, which does handle it.
    - Two-Principled mix with constant Factor 0/1: ``None`` — the mix
      degenerates to a single BSDF; every channel has one well-defined source.
    - Two-Principled genuine blend:
        * Base color: baked (DIFFUSE/COLOR or COMBINED pass) — always fine.
        * Roughness (LIT_ALBEDO): baked via the ROUGHNESS pass, which renders
          the factor-weighted roughness of the mixed closure — may diverge.
        * Metallic / Normal / IOR / Diffuse Roughness (LIT_ALBEDO): passed through from a single
          Principled node, never baked — must agree between the two BSDFs
          (same constant within epsilon, or the same source socket), else
          refused naming the diverging property.
        * Alpha (any mode, when the material is transparent and opacity is
          baked): the opacity EMIT bake reads one Principled's Alpha — must
          agree likewise, else refused.
    """
    mix = _analyze_mix_shader_surface(material)
    if mix is None:
        return None

    mat_name = getattr(material, "name", "Unknown")

    # Transparency has no bake representation at all: every bake mode renders
    # into opaque textures, so a Transparent BSDF anywhere under the mix would
    # be silently flattened to opaque. Refused in ALL modes, including
    # LIT_IBL, which otherwise accepts any mix it can render.
    if _surface_subtree_contains(
        _get_surface_shader_node(material), 'BSDF_TRANSPARENT'
    ):
        return (
            f"Material '{mat_name}': the Mix Shader blends a Transparent BSDF, and "
            "baking would flatten that transparency into opaque textures. Express "
            "transparency through the Principled BSDF Alpha input instead, or export "
            "directly with RealityKit PBR > Translate Materials, which turns a Principled "
            "BSDF mixed with a white Transparent BSDF into opacity."
        )

    if not mix["supported"]:
        if bake_mode == "LIT_IBL":
            return None
        return (
            f"Material '{mat_name}': {mix['reason']}. The Material Color bake modes "
            "(UNLIT_ALBEDO / LIT_ALBEDO) only support a Mix Shader over exactly two "
            "Principled BSDFs. Use bake mode LIT_IBL (Lighting & Shadows), which "
            "renders the full mixed shading into the baked textures."
        )
    if mix["selected"] is not None:
        return None

    p1, p2 = mix["principled"]
    problems = []
    if bake_mode == "LIT_ALBEDO":
        # Roughness is genuinely baked (ROUGHNESS pass), so it may diverge.
        # Metallic and Normal are single-source passthroughs, so they may not.
        divergence = _mix_input_divergence(p1, p2, 'Metallic')
        if divergence:
            problems.append(divergence)
        divergence = _mix_input_divergence(p1, p2, 'Normal', compare_values=False)
        if divergence:
            problems.append(divergence)
        # IOR and Diffuse Roughness are copied from one side too.
        for name in _LIT_ALBEDO_CARRIED_CONSTANTS:
            divergence = _mix_input_divergence(p1, p2, name)
            if divergence:
                problems.append(divergence)
    # The opacity EMIT bake reads a single Principled's Alpha in EVERY bake
    # mode, LIT_IBL included, so a divergent alpha has no honest source
    # anywhere.
    if bake_opacity and _material_needs_opacity(material):
        divergence = _mix_input_divergence(p1, p2, 'Alpha')
        if divergence:
            problems.append(divergence)
    if bake_mode == "LIT_IBL" and not problems:
        return None

    if problems:
        return (
            f"Material '{mat_name}': Mix Shader blends two Principled BSDFs but "
            + "; ".join(problems)
            + ". These channels are passed through from a single Principled node "
            "(they are not baked), so a genuine blend has no honest single source. "
            "Make them match, set the mix Factor to a constant 0 or 1, or use bake "
            "mode LIT_IBL (Lighting & Shadows), which bakes the full mixed shading."
        )
    return None


def _passthrough_principled(material):
    """The single Principled node whose non-baked channels may be passed through.

    For a plain material this is the first Principled node (historic behavior).
    For a supported Mix Shader surface it is the Factor-selected side when the
    factor is a constant 0/1, else either side — valid only because
    ``check_mix_shader_bakeable`` already refused any material whose
    passed-through channels diverge between the two sides. Unsupported mix
    shapes return ``None`` (nothing is passed through; LIT_ALBEDO refuses them
    before this is ever consulted).
    """
    if getattr(material, "node_tree", None) is None:
        return None
    mix = _analyze_mix_shader_surface(material)
    if mix is not None:
        if not mix["supported"]:
            return None
        if mix["selected"] is not None:
            return mix["selected"]
        return mix["principled"][0]
    return _surface_principled_node(material)


def _flat_material_constants(material, *, lit: bool = True) -> Optional[Dict[str, object]]:
    """Return constant PBR values when *material* is a flat-color material.

    A material is "flat" when nothing texture-varying feeds its surface: it has
    no image-texture nodes and its Principled Base Color is an unlinked constant.
    Such materials must not be baked - baking would burn a single color into a
    full-resolution texture (wasted file size) and, when the faces have no real
    UV unwrap, yields an all-black texture. Instead the returned constants are
    authored directly so apps like Reality Composer Pro show the exact color.

    A *linked* Alpha input always disqualifies: procedural (non-image)
    transparency must go through the real opacity bake, not be silently
    flattened to a constant 1.0. When ``lit`` is True (the Lit-PBR mode, where
    roughness/metallic are actually authored), linked Roughness or Metallic
    chains likewise force a real bake so their variation isn't collapsed to the
    Principled defaults.

    Returns ``None`` for any material that genuinely needs a texture bake.
    """
    if getattr(material, "node_tree", None) is None:
        return None

    node_tree = material.node_tree
    # Any image texture means there is something worth baking.
    if any(node.type == 'TEX_IMAGE' for node in node_tree.nodes):
        return None

    mix = _analyze_mix_shader_surface(material)
    if mix is not None:
        # A Mix Shader surface is only "flat" when a constant Factor of 0/1
        # selects a single Principled BSDF. A genuine blend must never be
        # collapsed to one side's constants — it needs a real bake (the
        # DIFFUSE/COLOR pass renders the factor-weighted albedo).
        principled = mix["selected"] if mix["supported"] else None
        if principled is None:
            return None
    else:
        principled = _surface_principled_node(material)
    if principled is None:
        return None

    base_color = principled.inputs.get('Base Color')
    if base_color is None or base_color.is_linked:
        return None

    base = tuple(base_color.default_value)
    base_rgba = (base[0], base[1], base[2], base[3] if len(base) > 3 else 1.0)

    roughness = principled.inputs.get('Roughness')
    metallic = principled.inputs.get('Metallic')
    alpha = principled.inputs.get('Alpha')

    # Procedural transparency (a linked Alpha with no image textures) can't be
    # represented by a constant - it needs the real opacity bake.
    if alpha is not None and alpha.is_linked:
        return None
    # In Lit-PBR mode roughness/metallic are authored on the exported material,
    # so procedurally-driven values need the real bake passes too.
    if lit and roughness is not None and roughness.is_linked:
        return None
    if lit and metallic is not None and metallic.is_linked:
        return None

    return {
        "base_color": base_rgba,
        "roughness": float(roughness.default_value)
        if roughness is not None and not roughness.is_linked
        else None,
        "metallic": float(metallic.default_value)
        if metallic is not None and not metallic.is_linked
        else None,
        "alpha": float(alpha.default_value) if alpha is not None else 1.0,
    }


def _validate_bake_material_contract(
    material,
    *,
    bake_mode: str,
    diagnostics=None,
) -> Dict[str, Optional[Dict[str, object]]]:
    """Validate semantics the rebuilt bake material must preserve exactly.

    Base color and roughness have real bake passes, but opacity, normal, and
    metallic are reconstructed. A shader mix or an unsupported normal/metallic
    chain must therefore stop before material slots are changed; guessing would
    produce a visually plausible but semantically different RealityKit asset.
    """
    result = {"normal": None, "metallic": None}
    if getattr(material, "node_tree", None) is None:
        return result

    output = _active_material_output(material)
    surface = getattr(output, "inputs", {}).get("Surface") if output is not None else None
    links = list(getattr(surface, "links", ()) or ()) if surface is not None else []
    surface_node = getattr(links[0], "from_node", None) if len(links) == 1 else None
    if getattr(surface_node, "type", None) == "MIX_SHADER":
        # A Mix Shader surface is legal exactly where check_mix_shader_bakeable
        # allows it (the pre-pass already refused everything else with the
        # precise message). The full-shading modes render the blend itself, so
        # when no single Principled side is well-defined there is simply
        # nothing to pass through.
        refusal = check_mix_shader_bakeable(material, bake_mode)
        if refusal:
            if diagnostics:
                diagnostics.add_error(refusal)
            raise RuntimeError(refusal)
        surface_node = _passthrough_principled(material)
        if surface_node is None:
            return result
    elif surface_node is None or getattr(surface_node, "type", None) != "BSDF_PRINCIPLED":
        node_type = getattr(surface_node, "type", "unconnected surface")
        message = (
            f"Bake Textures cannot preserve material '{getattr(material, 'name', '<unnamed>')}' "
            f"because its active surface is {node_type}, not one directly connected Principled "
            "BSDF. Surfaces other than a Principled BSDF or a supported Mix Shader "
            "require an explicit opacity bake that this pipeline does not provide. "
            "Export directly with RealityKit PBR > Translate Materials, or simplify the active surface before baking."
        )
        if diagnostics:
            diagnostics.add_error(message)
        raise RuntimeError(message)

    if bake_mode == "LIT_ALBEDO":
        try:
            _validate_lit_albedo_principled_inputs(material, surface_node)
            result["normal"] = _source_normal_passthrough(material, principled=surface_node)
            result["metallic"] = _source_metallic_passthrough(material, principled=surface_node)
            result["principled_constants"] = _source_principled_constants(surface_node)
            # The ROUGHNESS pass is unusable on an alpha-blended surface: it
            # returns 0, so the export would claim a mirror finish. Carry the
            # source through instead, exactly as normal and metallic are.
            if _material_needs_opacity(material):
                result["roughness_passthrough"] = _source_roughness_passthrough(
                    material, principled=surface_node
                )
        except RuntimeError as exc:
            if diagnostics:
                diagnostics.add_error(str(exc))
            raise
    return result


#: Principled inputs Material Color Only copies onto its rebuilt Principled
#: BSDF. The export reads IOR into the specular reflectance and judges Diffuse
#: Roughness in its Lambert notice, and the fresh node's 1.5 and 0 would
#: otherwise replace them without a word.
_LIT_ALBEDO_CARRIED_CONSTANTS = ("IOR", "Diffuse Roughness")


def _source_principled_constants(principled) -> Dict[str, float]:
    """The unlinked ``_LIT_ALBEDO_CARRIED_CONSTANTS`` values on the source."""
    constants: Dict[str, float] = {}
    for name in _LIT_ALBEDO_CARRIED_CONSTANTS:
        socket = principled.inputs.get(name)
        if socket is None or bool(getattr(socket, "is_linked", False)):
            continue
        try:
            constants[name] = float(socket.default_value)
        except (TypeError, ValueError):
            continue
    return constants


def _validate_lit_albedo_principled_inputs(material, principled) -> None:
    """Reject active controls the Material Color Only rebuild would discard."""
    # Reuse the graph capability policy: the rebuilt material exports through
    # PBR Surface 2 like everything else, so the bake refuses exactly what the
    # export would and nothing the export would carry.
    from ..nodes.validate import _unsupported_principled_inputs, _values_differ

    issues = list(_unsupported_principled_inputs(principled))

    def _active(name: str, neutral) -> bool:
        socket = principled.inputs.get(name)
        if socket is None:
            return False
        if bool(getattr(socket, "is_linked", False)):
            return True
        return _values_differ(getattr(socket, "default_value", None), neutral)

    # Material Color Only bakes Base Color/Roughness/Alpha and handles a narrow
    # normal/metallic passthrough below. It does not bake or copy these otherwise
    # exportable controls, so even constants must not be reset to fresh-node values.
    omitted = (
        ("Weight", 1.0, None),
        ("Specular IOR Level", 0.5, None),
        ("Coat Weight", 0.0, None),
        ("Coat Roughness", 0.03, "Coat Weight"),
        ("Coat Normal", (0.0, 0.0, 0.0), "Coat Weight"),
        ("Sheen Weight", 0.0, None),
        ("Subsurface Weight", 0.0, None),
    )
    for name, neutral, controller in omitted:
        if controller is not None and not _active(controller, 0.0):
            continue
        if _active(name, neutral):
            issues.append(f"Principled '{name}' is not preserved by Material Color Only bake.")

    # Carried onto the rebuild as constants (_source_principled_constants);
    # neither changes the colour the bake reads, so only a link is lost.
    for name in _LIT_ALBEDO_CARRIED_CONSTANTS:
        socket = principled.inputs.get(name)
        if socket is not None and bool(getattr(socket, "is_linked", False)):
            issues.append(
                f"Principled '{name}' is linked, and Material Color Only bake carries only a "
                "constant value."
            )

    # Blender 5.2 defaults Emission Color to white and uses Strength=0 as the
    # lobe controller. Color is dormant while Strength is zero.
    if _active("Emission Strength", 0.0):
        issues.append("Principled 'Emission Strength' is not preserved by Material Color Only bake.")

    if issues:
        preview = "; ".join(issues)
        raise _passthrough_error(
            material,
            "Material Color Only shading",
            f"{preview} Use Lighting & Shadows bake to flatten the full lit appearance instead",
        )


def _passthrough_error(material, channel: str, detail: str) -> RuntimeError:
    return RuntimeError(
        f"Bake Textures cannot preserve {channel} for material "
        f"'{getattr(material, 'name', '<unnamed>')}': {detail}. "
        "Export directly with RealityKit PBR > Translate Materials, bake that channel to a supported direct image first, or simplify the graph."
    )


def _validate_direct_passthrough_image(material, image_node, channel: str) -> None:
    if getattr(image_node, "image", None) is None:
        raise _passthrough_error(material, channel, "the Image Texture has no image")

    vector_socket = getattr(image_node, "inputs", {}).get("Vector")
    if vector_socket is not None and bool(getattr(vector_socket, "is_linked", False)):
        raise _passthrough_error(
            material,
            channel,
            "Mapping, UV Map, or other linked Vector chains are not reconstructed",
        )

    projection = str(getattr(image_node, "projection", "FLAT") or "FLAT").upper()
    extension = str(getattr(image_node, "extension", "REPEAT") or "REPEAT").upper()
    interpolation = str(getattr(image_node, "interpolation", "LINEAR") or "LINEAR").upper()
    if projection != "FLAT" or extension != "REPEAT" or interpolation != "LINEAR":
        raise _passthrough_error(
            material,
            channel,
            "non-default Image Texture projection, extension, or interpolation would be lost",
        )


def _source_normal_passthrough(material, *, principled=None) -> Optional[Dict[str, object]]:
    """Capture the source material's normal map so the bake can pass it through.

    The bake only renders base color / roughness / opacity - it never bakes a
    normal pass. A normal map is already a clean, RCP-compatible image, so the
    right thing is to carry it onto the baked material untouched rather than
    drop it (which left baked surfaces looking flat and over-glossy). Returns
    the source image, its UV layer and Normal Map strength, or ``None`` when the
    Normal input is unlinked or not driven by an image.
    """
    if getattr(material, "node_tree", None) is None:
        return None
    principled = principled or _passthrough_principled(material)
    if principled is None:
        return None
    normal_socket = principled.inputs.get('Normal')
    if normal_socket is None or not normal_socket.is_linked:
        return None

    links = list(getattr(normal_socket, "links", ()) or ())
    if len(links) != 1:
        raise _passthrough_error(material, "normal", "the Principled Normal input has multiple links")
    from_node = links[0].from_node
    strength = 1.0
    uv_layer = None
    tex_node = None
    if from_node.type == 'NORMAL_MAP':
        convention = str(getattr(from_node, "convention", "OPENGL") or "OPENGL").upper()
        if convention != "OPENGL":
            raise _passthrough_error(
                material,
                "normal",
                "RealityKit requires OpenGL normal maps but the source uses DirectX convention",
            )
        space = str(getattr(from_node, "space", "TANGENT") or "TANGENT").upper()
        if space != "TANGENT":
            raise _passthrough_error(
                material,
                "normal",
                f"Normal Map space '{space}' is not the supported tangent space",
            )
        strength_socket = from_node.inputs.get('Strength')
        if strength_socket is not None and bool(getattr(strength_socket, "is_linked", False)):
            raise _passthrough_error(material, "normal", "linked Normal Map Strength is not reconstructed")
        if strength_socket is not None:
            try:
                strength = float(strength_socket.default_value)
            except Exception:
                strength = 1.0
        uv_layer = getattr(from_node, "uv_map", None) or None
        color_socket = from_node.inputs.get('Color')
        color_links = list(getattr(color_socket, "links", ()) or ()) if color_socket is not None else []
        if len(color_links) == 1:
            candidate = color_links[0].from_node
            if candidate.type == 'TEX_IMAGE':
                output_name = str(
                    getattr(getattr(color_links[0], "from_socket", None), "name", "Color") or "Color"
                )
                if output_name != "Color":
                    raise _passthrough_error(
                        material,
                        "normal",
                        f"the Normal Map is driven by Image Texture output '{output_name}', not Color",
                    )
                tex_node = candidate
        if tex_node is None:
            raise _passthrough_error(
                material,
                "normal",
                "only a direct Image Texture Color -> Normal Map -> Principled chain is supported",
            )
    else:
        raise _passthrough_error(
            material,
            "normal",
            "only a direct Image Texture Color -> Normal Map -> Principled chain is supported",
        )

    _validate_direct_passthrough_image(material, tex_node, "normal")
    if not uv_layer:
        uv_layer = getattr(tex_node, "uv_map", None) or None
    return {"image": tex_node.image, "uv_layer": uv_layer, "strength": strength}


def _source_metallic_passthrough(material, *, principled=None) -> Optional[Dict[str, object]]:
    """Capture the source material's metallic input for passthrough.

    Like the normal map, metallic is never baked, so a textured or non-default
    constant metallic would otherwise be dropped (reset to 0) by the rebuilt
    material. Returns ``{"image", "uv_layer"}`` for a directly-wired metallic
    texture, ``{"value"}`` for a non-zero constant, or ``None`` when metallic is
    the default 0 or driven by a chain we don't pass through.
    """
    if getattr(material, "node_tree", None) is None:
        return None
    principled = principled or _passthrough_principled(material)
    if principled is None:
        return None
    metallic_socket = principled.inputs.get('Metallic')
    if metallic_socket is None:
        return None
    if not metallic_socket.is_linked:
        try:
            value = float(metallic_socket.default_value)
        except Exception:
            return None
        return {"value": value} if value else None

    links = list(getattr(metallic_socket, "links", ()) or ())
    if len(links) != 1:
        raise _passthrough_error(material, "metallic", "the Principled Metallic input has multiple links")
    link = links[0]
    from_node = link.from_node
    if from_node.type == 'TEX_IMAGE' and getattr(from_node, "image", None) is not None:
        output_name = str(
            getattr(getattr(link, "from_socket", None), "name", "Color") or "Color"
        )
        if output_name != "Color":
            raise _passthrough_error(
                material,
                "metallic",
                f"the source uses Image Texture output '{output_name}', not Color",
            )
        _validate_direct_passthrough_image(material, from_node, "metallic")
        return {
            "image": from_node.image,
            "uv_layer": getattr(from_node, "uv_map", None) or None,
        }
    raise _passthrough_error(
        material,
        "metallic",
        "packed channel, Math/Separate Color, or procedural chains are not reconstructed and would become zero",
    )


def _source_roughness_passthrough(material, *, principled=None) -> Optional[Dict[str, object]]:
    """Capture the source material's roughness for transparent materials.

    Cycles' ROUGHNESS pass returns 0 on an alpha-blended surface, so baking one
    exports a mirror finish. Measured on Blender 5.2 with two materials
    identical but for Alpha, both at roughness 0.5: opaque baked
    R[0.0000, 0.5020], Alpha=0.4 baked R[0.0000, 0.0000]. Carrying the source
    value through is the same treatment normal and metallic already get, for
    the same reason - the bake cannot represent them.

    Returns ``{"image", "uv_layer"}`` for a directly-wired roughness texture,
    ``{"value"}`` for a constant, or ``None`` when the input is driven by a
    chain we do not reconstruct (the caller then falls back to baking).
    """
    if getattr(material, "node_tree", None) is None:
        return None
    principled = principled or _surface_principled_node(material)
    if principled is None:
        return None
    roughness_socket = principled.inputs.get('Roughness')
    if roughness_socket is None:
        return None

    if not roughness_socket.is_linked:
        try:
            return {"value": float(roughness_socket.default_value)}
        except Exception:
            return None

    links = list(getattr(roughness_socket, "links", ()) or ())
    if len(links) != 1:
        return None
    link = links[0]
    from_node = link.from_node
    if from_node.type == 'TEX_IMAGE' and getattr(from_node, "image", None) is not None:
        output_name = str(
            getattr(getattr(link, "from_socket", None), "name", "Color") or "Color"
        )
        if output_name != "Color":
            return None
        return {
            "image": from_node.image,
            "uv_layer": getattr(from_node, "uv_map", None) or None,
        }
    return None


def _material_needs_opacity(material) -> bool:
    # Detect transparency from the active surface's real Alpha input. Render
    # method selects how transparency is displayed; it does not establish that
    # the material actually contains alpha below one.
    return material_has_transparency(material)


def _surface_render_method(material, *, transparent: bool) -> str:
    """Resolve Blender 5.2's two-value surface transparency method.

    Fully opaque baked materials use Blender's ray-tracing-compatible DITHERED
    mode with alpha fixed to one. Transparent materials preserve the source's
    DITHERED versus BLENDED choice. There is intentionally no legacy render-mode
    fallback: this release targets Blender 5.2 and later only.
    """
    if not transparent:
        return "DITHERED"
    value = str(material.surface_render_method)
    if value not in {"DITHERED", "BLENDED"}:
        raise ValueError(f"Unsupported Blender 5.2 surface render method: {value}")
    return value


def _bind_uv_layer(node_tree, node, uv_layer: Optional[str]) -> None:
    """Point ``node`` at a specific UV map.

    ``ShaderNodeTexImage`` has no ``uv_map`` property - only ShaderNodeNormalMap
    and ShaderNodeUVMap do - so the ``hasattr(node, "uv_map")`` guards this
    replaces never fired and every image-node binding was a silent no-op. An
    image node with an unconnected Vector input samples whatever UV map the
    renderer picks by default, which is not necessarily the one the bake wrote
    into (the bake follows ``mesh.uv_layers.active``, verified against Blender
    5.2). On a mesh with more than one UV map that is a texture sampled through
    the wrong layout. Bind it explicitly so the result does not depend on which
    map happens to be the default.
    """
    if not uv_layer:
        return
    # Real for ShaderNodeNormalMap and ShaderNodeUVMap, never for an image node.
    if hasattr(node, "uv_map"):
        node.uv_map = uv_layer
        return

    vector_input = node.inputs.get("Vector")
    if vector_input is None:
        return
    if vector_input.is_linked:
        return

    # One UV source per layer per material; reuse it across the baked textures.
    uv_node = next(
        (
            candidate
            for candidate in node_tree.nodes
            if candidate.type == 'UVMAP' and candidate.uv_map == uv_layer
        ),
        None,
    )
    if uv_node is None:
        uv_node = node_tree.nodes.new("ShaderNodeUVMap")
        uv_node.uv_map = uv_layer
    node_tree.links.new(uv_node.outputs["UV"], vector_input)


def _set_active_image_node(material, image, uv_layer: Optional[str]) -> None:
    # No UV binding here on purpose: this node is only the bake *target*, and
    # the layout the bake writes comes from the mesh, not from this node's
    # Vector input. Binding is done on the final baked material, which is what
    # actually gets sampled and exported.
    nodes = material.node_tree.nodes
    node = nodes.new("ShaderNodeTexImage")
    node.image = image
    nodes.active = node
    node.select = True


def _configure_emission_for_alpha(material) -> None:
    """Route alpha into an Emission output for opacity baking.

    Alpha comes from ``_passthrough_principled``: the Principled BSDF connected
    to the active Material Output, or, for a Mix Shader over two Principled
    BSDFs, the Factor-selected side or either side of a genuine blend.
    ``check_mix_shader_bakeable`` refuses a transparent blend whose two Alpha
    inputs differ before any material is copied, so both sides give the same
    opacity.
    """
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    output_node = _active_material_output(material)
    principled = _passthrough_principled(material)
    if output_node is None or principled is None:
        raise RuntimeError(
            f"Material '{getattr(material, 'name', '<unnamed>')}' has no Principled "
            "shader, or Mix Shader over two Principled shaders, connected to its "
            "active Material Output; opacity cannot be baked."
        )

    for link in list(output_node.inputs['Surface'].links):
        links.remove(link)

    emission_node = nodes.new("ShaderNodeEmission")
    alpha_socket = principled.inputs.get('Alpha')

    if alpha_socket and alpha_socket.is_linked:
        from_socket = alpha_socket.links[0].from_socket
        combine, inputs, output = _new_combine_color_node(nodes)
        for input_name in inputs:
            links.new(from_socket, combine.inputs[input_name])
        links.new(combine.outputs[output], emission_node.inputs['Color'])
    else:
        alpha_value = 1.0
        if alpha_socket:
            try:
                alpha_value = float(alpha_socket.default_value)
            except Exception:
                alpha_value = 1.0
        emission_node.inputs['Color'].default_value = (alpha_value, alpha_value, alpha_value, 1.0)

    links.new(emission_node.outputs['Emission'], output_node.inputs['Surface'])


def _mark_pixels_uncovered(image) -> None:
    """Zero an averaging target so unbaked texels stay distinguishable.

    Pairs with ``use_clear=False`` on the bake: the operator writes alpha 1
    wherever it touches, so any texel still at alpha 0 afterwards is texture
    space no UV island reaches.
    """
    try:
        import numpy as np

        image.pixels.foreach_set(np.zeros(len(image.pixels), dtype=np.float32))
    except Exception:
        pass


def _average_image_value(image) -> float:
    """Mean of the baked texels only, ignoring space no UV island covers.

    Averaging the whole buffer made the result a function of bake margin
    rather than of the material: uncovered texels read 0, and a larger margin
    dilates real values over more of them. Measured on Blender 5.2 with one
    material at Roughness 0.5 and a Non-Color target, margins 0/8/32 gave
    0.1178 / 0.2120 / 0.4376 unmasked, against 0.5020 / 0.4919 / 0.5011 masked.
    The default margin is 8, so the exported constant was far too low.
    """
    try:
        import numpy as np

        buf = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(buf)
    except Exception:
        return 0.5

    reds = buf[0::4]
    if not reds.size:
        return 0.5

    covered = reds[buf[3::4] > 0.5]
    if covered.size:
        return float(covered.mean())
    # No coverage mask available (an unprefilled target, or a bake that wrote
    # nothing). The unmasked mean is wrong, but it is what the caller had
    # before, and it is better than reporting a material as fully rough.
    return float(reds.mean())


def _new_combine_color_node(nodes):
    """Create a shader color-combine node."""
    return nodes.new("ShaderNodeCombineColor"), ("Red", "Green", "Blue"), "Color"


def _build_baked_material(
    material,
    base_image,
    opacity_image,
    use_opacity: bool,
    *,
    uv_layer: Optional[str] = None,
    roughness_image=None,
    roughness_value=None,
    roughness_passthrough: Optional[Dict[str, object]] = None,
    flat: Optional[Dict[str, object]] = None,
    normal: Optional[Dict[str, object]] = None,
    metallic: Optional[Dict[str, object]] = None,
    principled_constants: Optional[Dict[str, float]] = None,
    surface_render_method: str = "DITHERED",
) -> None:
    if surface_render_method not in {"DITHERED", "BLENDED"}:
        raise ValueError(
            f"Unsupported Blender 5.2 surface render method: {surface_render_method}"
        )
    node_tree = material.node_tree
    nodes = node_tree.nodes
    links = node_tree.links
    nodes.clear()

    output_node = nodes.new("ShaderNodeOutputMaterial")
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    links.new(principled.outputs['BSDF'], output_node.inputs['Surface'])
    for name, value in (principled_constants or {}).items():
        principled.inputs[name].default_value = value

    if flat is not None:
        # Flat-color material: author the captured constants directly (no textures),
        # so the exported USD carries the exact color/roughness/metallic.
        base = flat.get("base_color", (0.8, 0.8, 0.8, 1.0))
        try:
            principled.inputs['Base Color'].default_value = (base[0], base[1], base[2], 1.0)
        except Exception:
            pass
        if flat.get("roughness") is not None:
            try:
                principled.inputs['Roughness'].default_value = float(flat["roughness"])
            except Exception:
                pass
        if flat.get("metallic") is not None:
            try:
                principled.inputs['Metallic'].default_value = float(flat["metallic"])
            except Exception:
                pass
        alpha_value = float(flat.get("alpha", 1.0))
        try:
            principled.inputs['Alpha'].default_value = alpha_value
        except Exception:
            pass
        material.surface_render_method = (
            surface_render_method if (use_opacity and alpha_value < 1.0) else 'DITHERED'
        )
        return

    # Carry the source normal map through untouched (the bake never renders a
    # normal pass). Done before the base/opacity wiring so the early returns in
    # that block can't skip it.
    if normal is not None and normal.get("image") is not None:
        normal_map_node = nodes.new("ShaderNodeNormalMap")
        try:
            normal_map_node.inputs['Strength'].default_value = float(
                normal.get("strength", 1.0)
            )
        except Exception:
            pass
        normal_uv = normal.get("uv_layer") or uv_layer
        _bind_uv_layer(node_tree, normal_map_node, normal_uv)
        normal_tex = nodes.new("ShaderNodeTexImage")
        # Reference the source image as-is; it already carries its authored
        # colorspace. Forcing it here would mutate the shared datablock for
        # every other user of the image (and isn't restored).
        normal_tex.image = normal["image"]
        _bind_uv_layer(node_tree, normal_tex, normal_uv)
        links.new(normal_tex.outputs['Color'], normal_map_node.inputs['Color'])
        links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])

    # Carry the source metallic through (texture or non-default constant); the
    # bake never renders a metallic pass.
    if metallic is not None:
        if metallic.get("image") is not None:
            metallic_tex = nodes.new("ShaderNodeTexImage")
            # As-is: don't mutate the shared image's colorspace (see normal map).
            metallic_tex.image = metallic["image"]
            metallic_uv = metallic.get("uv_layer") or uv_layer
            _bind_uv_layer(node_tree, metallic_tex, metallic_uv)
            links.new(metallic_tex.outputs['Color'], principled.inputs['Metallic'])
        elif metallic.get("value") is not None:
            try:
                principled.inputs['Metallic'].default_value = float(metallic["value"])
            except Exception:
                pass

    if roughness_passthrough is not None:
        # Transparent material: the bake could not measure roughness, so the
        # source is carried through untouched (see _source_roughness_passthrough).
        if roughness_passthrough.get("image") is not None:
            rough_node = nodes.new("ShaderNodeTexImage")
            rough_node.image = roughness_passthrough["image"]
            _bind_uv_layer(
                node_tree,
                rough_node,
                roughness_passthrough.get("uv_layer") or uv_layer,
            )
            links.new(rough_node.outputs['Color'], principled.inputs['Roughness'])
        elif roughness_passthrough.get("value") is not None:
            try:
                principled.inputs['Roughness'].default_value = float(
                    roughness_passthrough["value"]
                )
            except Exception:
                pass
    elif roughness_image is not None:
        rough_node = nodes.new("ShaderNodeTexImage")
        rough_node.image = roughness_image
        _bind_uv_layer(node_tree, rough_node, uv_layer)
        links.new(rough_node.outputs['Color'], principled.inputs['Roughness'])
    elif roughness_value is not None:
        try:
            principled.inputs['Roughness'].default_value = float(roughness_value)
        except Exception:
            pass

    if base_image:
        base_node = nodes.new("ShaderNodeTexImage")
        base_node.image = base_image
        _bind_uv_layer(node_tree, base_node, uv_layer)
        links.new(base_node.outputs['Color'], principled.inputs['Base Color'])
        if use_opacity and opacity_image is None:
            alpha_output = base_node.outputs.get('Alpha')
            if alpha_output is not None:
                links.new(alpha_output, principled.inputs['Alpha'])
                material.surface_render_method = surface_render_method
                return

    if use_opacity and opacity_image:
        opacity_node = nodes.new("ShaderNodeTexImage")
        opacity_node.image = opacity_image
        _bind_uv_layer(node_tree, opacity_node, uv_layer)
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.mode = 'RGB'
        links.new(opacity_node.outputs['Color'], separate.inputs['Color'])
        links.new(separate.outputs['Red'], principled.inputs['Alpha'])
        material.surface_render_method = surface_render_method
    else:
        material.surface_render_method = 'DITHERED'


def _srgb_to_linear(values):
    import numpy as np

    return np.where(values <= 0.04045, values / 12.92, ((values + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(values):
    import numpy as np

    return np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(np.maximum(values, 0.0), 1.0 / 2.4) - 0.055,
    )


def _unpremultiply_color_bake(image) -> bool:
    """Divide a transparent slot's color bake by the alpha Cycles wrote with it.

    Cycles weights a Principled BSDF by its Alpha, and both color bakes
    (DIFFUSE/COLOR and COMBINED) return that weighted color with the same
    Alpha in the target's alpha channel. Measured on Blender 5.2, byte target,
    linear base color (0.8, 0.1, 0.1): DIFFUSE/COLOR gave (0.7682, 0.0953,
    0.0953) at Alpha 1 and (0.3095, 0.0382, 0.0382) with alpha 0.4 at Alpha
    0.4; COMBINED gave (0.8148, 0.1329, 0.1329) and (0.3231, 0.0529, 0.0529).
    The exported material reads straight color and blends it by opacity once,
    so the premultiplied color must be divided back out. The base image is an
    sRGB byte buffer: the division happens on linear values, only where alpha
    is strictly between 0 and 1, so opaque and uncovered texels are untouched.
    """
    try:
        import numpy as np

        pixel_count = len(image.pixels)
        pixels = np.empty(pixel_count, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    except Exception:
        return False

    rgba = pixels.reshape(-1, 4).astype(np.float64)
    alpha = rgba[:, 3]
    partial = (alpha > (0.5 / 255.0)) & (alpha < (254.5 / 255.0))
    if not partial.any():
        return False
    linear = _srgb_to_linear(rgba[partial, :3]) / alpha[partial, None]
    rgba[partial, :3] = _linear_to_srgb(np.clip(linear, 0.0, 1.0))
    try:
        image.pixels.foreach_set(rgba.astype(np.float32).reshape(-1))
    except Exception:
        return False
    return True


def _merge_opacity_into_base_image(base_image, opacity_image) -> bool:
    """Merge opacity as straight alpha while leaving base-color RGB unchanged.

    The exported bake convention is explicitly straight (unassociated) alpha:
    RGB retains the material-color bake and the opacity pass supplies only A.
    This avoids baking premultiplication into the pixels and then asking
    RealityKit/MaterialX to apply alpha a second time.
    """
    if base_image is None or opacity_image is None:
        return False

    try:
        base_size = tuple(getattr(base_image, "size", ())[:2])
        opacity_size = tuple(getattr(opacity_image, "size", ())[:2])
    except Exception:
        return False

    if len(base_size) != 2 or len(opacity_size) != 2 or base_size != opacity_size:
        return False

    try:
        import numpy as np

        pixel_count = len(base_image.pixels)
        if pixel_count != len(opacity_image.pixels):
            return False
        base_pixels = np.empty(pixel_count, dtype=np.float32)
        opacity_pixels = np.empty(pixel_count, dtype=np.float32)
        base_image.pixels.foreach_get(base_pixels)
        opacity_image.pixels.foreach_get(opacity_pixels)
    except Exception:
        return False

    # Opacity bakes are grayscale: red carries the value. RGB is deliberately
    # not multiplied, making the resulting image straight/unassociated RGBA.
    base_pixels[3::4] = opacity_pixels[0::4]

    try:
        base_image.pixels.foreach_set(base_pixels)
        base_image.alpha_mode = 'STRAIGHT'
        base_image.save()
    except Exception:
        return False

    opacity_path = getattr(opacity_image, "filepath_raw", "") or ""
    if not opacity_path:
        return True

    try:
        opacity_file = Path(opacity_path)
        if opacity_file.exists():
            opacity_file.unlink()
    except Exception:
        pass
    return True


def _bake_object_pass(
    context,
    obj,
    bake_type: str,
    pass_filter: Optional[set],
    margin: int,
    **extra_kwargs,
) -> None:
    if context.view_layer.objects.active != obj:
        context.view_layer.objects.active = obj
    if obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    kwargs = {
        "type": bake_type,
        "margin": int(margin),
        "use_clear": True,
        "use_selected_to_active": False,
        # Pin every operator property this pipeline depends on. Blender fills
        # unset bake properties from scene.render.bake, so anything left out
        # here silently inherits whatever the .blend was last saved with.
        # A scene saved with target='VERTEX_COLORS' bakes into a color
        # attribute and leaves the target image untouched - the operator still
        # returns {'FINISHED'} and an all-black texture is saved and packaged.
        "target": 'IMAGE_TEXTURES',
        # EXTERNAL redirects the bake to files on disk; this pipeline reads the
        # pixels back off the image datablock and saves them itself.
        "save_mode": 'INTERNAL',
        "use_split_materials": False,
    }
    if pass_filter is None and bake_type == 'COMBINED':
        # Same inheritance trap: an omitted pass_filter takes the scene's
        # use_pass_direct/use_pass_indirect/... toggles, so a .blend with those
        # disabled bakes pure black through the LIT_IBL path.
        pass_filter = set(COMBINED_PASS_FILTER)
    if pass_filter is not None:
        kwargs["pass_filter"] = pass_filter
    kwargs.update(extra_kwargs)
    bpy.ops.object.bake(**kwargs)


def _select_object(context, obj) -> None:
    for selected in list(context.selected_objects):
        try:
            selected.select_set(False)
        except Exception:
            pass
    obj.select_set(True)
    context.view_layer.objects.active = obj


def _create_bake_image(
    name: str,
    filepath: Path,
    width: int,
    height: int,
    colorspace: str,
    file_format: str,
) -> object:
    image = bpy.data.images.new(name=name, width=width, height=height, alpha=True)
    image.filepath_raw = str(filepath)
    try:
        image.file_format = file_format
    except Exception:
        image.filepath_raw = str(filepath.with_suffix(".png"))
        image.file_format = "PNG"
    try:
        image.colorspace_settings.name = colorspace
    except Exception:
        pass
    return image


def _make_image_path(
    output_dir: Path,
    object_name: str,
    material_name: str,
    suffix: str,
    ext: str,
) -> Path:
    base = _safe_filename(f"{object_name}__{material_name}_{suffix}")
    filename = base + ext
    path = output_dir / filename
    counter = 1
    while path.exists():
        filename = f"{base}_{counter}{ext}"
        path = output_dir / filename
        counter += 1
    return path


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_\\-]+", "_", name.strip())
    return name.strip("_") or "baked"


def _unique_name(name: str, collection) -> str:
    if name not in collection:
        return name
    idx = 1
    while f"{name}_{idx}" in collection:
        idx += 1
    return f"{name}_{idx}"


# A trailing run of "_Baked" suffixes, each optionally carrying a _unique_name
# collision counter (e.g. "_Baked", "_Baked_3", "_Baked_3_Baked_Baked").
_BAKED_SUFFIX_RE = re.compile(r"(?:_Baked(?:_\d+)?)+$")


def _strip_baked_suffix(name: str) -> str:
    """Remove a trailing _Baked(_N) chain from a material name.

    Keeps the bake idempotent on its own output: re-baking a material that is
    already a baked product yields "<base>_Baked" instead of compounding into
    "<base>_Baked_Baked_Baked...". A genuine name component that ends in digits
    (e.g. "Marble_001") is untouched because it is not part of a _Baked run.
    """
    stripped = _BAKED_SUFFIX_RE.sub("", name)
    # Never return empty (e.g. a material literally named "_Baked").
    return stripped or name
