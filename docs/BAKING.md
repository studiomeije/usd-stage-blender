# Texture baking

Read this page if a baked export does not look like your viewport, or to learn
when the Export button bakes and what the bake decides on your behalf.

*Baking* renders a material's appearance into texture images. *IBL*
(image-based lighting) lights a scene with an environment image. A
*passthrough* copies a source texture or value into the export unbaked.

The code lives in `Plugin/export/bake_textures.py` and `bake_finalize.py`, the
`bake-export` command, and the sidebar's background job
(`Plugin/ops/bake_export_operator.py`, `Plugin/bake_export_runner.py`).

---

## 1. When baking happens at all

The sidebar has one **Export** button and a **Profile** choice. The button does
not say whether it will bake. `resolve_ui_export_route` in
`Plugin/export_profile.py` reads three enum settings and returns a route; the
panel then puts `usdstage.export` or `usdstage.bake_export_background`
behind the button.

| Profile (`ui_material_type`) | Material Settings option | Route | `bake_mode` |
|---|---|---|---|
| RealityKit PBR | Processing: Translate Materials (`ui_pbr_processing = TRANSLATE`) | **no bake**, direct export | none |
| RealityKit PBR | Processing: Bake Materials (`ui_pbr_processing = BAKE`) | bake | `LIT_ALBEDO` |
| RealityKit Unlit | Appearance: Material Color Only (`ui_unlit_appearance = MATERIAL_COLOR`) | bake | `UNLIT_ALBEDO` |
| RealityKit Unlit | Appearance: Lighting & Shadows (`ui_unlit_appearance = LIGHTING_SHADOWS`) | bake | `LIT_IBL` |

Only *RealityKit PBR → Translate Materials* skips the bake. **RealityKit Unlit
always bakes**, because an Unlit material still needs a color texture.

`bake_mode` (default `LIT_IBL`) is not drawn in the panel. The Export button
sets `apply_ui_profile` on the bake operator, which **overwrites**
`settings.bake_mode` with the routed value, so a `bake_mode` stored from the CLI
is discarded on the next sidebar export. `usdstage bake-export --bake-mode`
does the opposite: it sets the mode directly and never reads the `ui_*`
settings (names in [`CLI.md`](CLI.md#bake-export)). Any other value is
treated as `LIT_IBL`.

---

## 2. The bake modes

| | `UNLIT_ALBEDO` | `LIT_ALBEDO` | `LIT_IBL` |
|---|---|---|---|
| Sidebar profile | RealityKit Unlit → Material Color Only | RealityKit PBR → Bake Materials | RealityKit Unlit → Lighting & Shadows |
| Cycles bake type | `DIFFUSE` | `DIFFUSE` | `COMBINED` |
| `pass_filter` | `{COLOR}` | `{COLOR}` | `{DIRECT, INDIRECT, DIFFUSE, GLOSSY, TRANSMISSION, EMIT}` |
| Lighting included | none | none | scene lights, World or HDRI, shadows |
| Cycles samples | 1 | 1 | your scene's `cycles.samples` |
| Roughness | not authored | baked, averaged, or passed through (§3.6) | not authored |
| Normal and metallic | dropped | passed through | dropped |
| Flat materials skip the bake | yes | yes | no |
| Instances share one bake | yes | yes | no |
| Exported material | Unlit | Lit PBR | Unlit |

`UNLIT_ALBEDO` and `LIT_ALBEDO` run the same base color pass and differ in what
is authored around it: `bake_finalize.resolve_force_unlit` sets
`force_unlit_materials` for every mode except `LIT_ALBEDO`. Baked materials then
go through the same MaterialX rewrite as a direct export (see
[`MATERIAL_TRANSLATION.md`](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)).
Because `LIT_IBL` keeps your sample count, a scene saved with high
`cycles.samples` bakes much more slowly in that mode.

### Instances and the reuse cache

In the two albedo modes, slots that share a key reuse one baked material, with
no copy and no re-bake. That shared binding is what lets the USD exporter emit
instanceable references. `_make_cache_key` builds:

`(source_material.name_full, mesh_datablock_id, resolution, uv_layer, bake_mode, bake_base, use_opacity, bake_roughness_map, roughness_single, is_flat)`

The mesh identity is in the key because a baked texture is tied to a UV layout.
A hit needs every slot on the object baked already, and the cache lasts one run.

`LIT_IBL` disables the cache: a `COMBINED` bake records lighting in world space,
so two copies of a mesh at different transforms genuinely differ. Slots are
`DATA`-linked by default, which would put every linked duplicate's bake on the
one shared mesh. So when a mesh has more than one user, the bake switches that
object's slot to `OBJECT` link first, and `restore_baked_materials` puts the
link back before the original material. Linked duplicates (Alt+D) keep their own
lighting.

---

## 3. Decisions made for you

### 3.1 Scene state that is overridden and restored

| State | Overridden to | Restored |
|---|---|---|
| `scene.render.engine` | `CYCLES` | yes |
| `cycles.samples`, `use_denoising` | `1`, `False` for every pass except the `LIT_IBL` color pass | after each pass |
| `scene.world` | a temporary `USDStage_IBL` world, only for `LIT_IBL` with `bake_ibl_source = HDRI_FILE` | yes; the world is deleted |
| Object mode, selection, active object | `OBJECT`; each mesh selected alone before every pass | yes |
| `hide_viewport`, `hide_render` of other meshes | `True`, only for `LIT_IBL` with `bake_isolate_meshes_lit` | after each object |
| Material slots and slot links | `<name>_Baked` copies; `OBJECT` link on shared meshes | yes |
| `force_unlit_materials` | set from `bake_mode` | yes |
| Collection prototypes | linked into the scene so bake operators reach them | unlinked before USD export; fails closed |

Restoration runs on failure too: `bake_materials_for_objects` restores every slot
and removes partial datablocks before re-raising, and each caller guards its
restore steps separately.

**Color management is not touched**: if the export differs from the viewport,
the view transform, look or exposure is the reason.

`bpy.ops.object.bake` fills omitted properties from the `.blend`'s saved
`scene.render.bake`, so `_bake_object_pass` pins `target = IMAGE_TEXTURES`,
`save_mode = INTERNAL`, `use_split_materials`, `use_selected_to_active`,
`use_clear`, `margin`, and the full `COMBINED` `pass_filter`. A saved
`VERTEX_COLORS` target or switched-off passes would otherwise bake black.

### 3.2 Resolution, format, and margin

`_resolve_bake_resolution` returns a fixed size, or `0` ("size each material
from its own source textures") when `export_texture_settings_enabled` is off or
`bake_resolution` is `ORIGINAL`, the default. The sidebar's bake route forces the
toggle on; the CLI sets it with `--resolution`, `--image-format` or `--margin`.
With the defaults, both bake source-keyed.

Source-keyed sizing (`_material_source_resolution`) walks upstream from the
Principled **Base Color**, **Roughness** and **Alpha** inputs, takes the largest
image dimension, and returns at least **512**, because a small tiling texture
needs more texels once flattened into a bake. Images on channels that are not
baked, such as a normal map, do not count. With no image, or a Mix Shader
surface, it falls back to **2048**. **`LIT_IBL` never bakes source-keyed:** a
`0` becomes 2048, since a tiling albedo says nothing about shadow detail.

Image format follows [`SETTINGS.md`](SETTINGS.md#texture-settings); `ORIGINAL`
bakes PNG with a warning. Margin (dilation around UV islands, default 8 px) is
resolved as described in [`SETTINGS.md`](SETTINGS.md#texture-settings).

### 3.3 Which surfaces bake

Before any slot changes, `_validate_bake_material_contract` checks each node
material's active surface, with `check_mix_shader_bakeable` for Mix Shaders.

| Active surface | `UNLIT_ALBEDO`, `LIT_ALBEDO` | `LIT_IBL` |
|---|---|---|
| Principled BSDF connected directly to the active Material Output | accepted | accepted |
| Mix Shader over exactly two directly connected Principled BSDFs | accepted, limits below | accepted, Alpha limit |
| Other Mix Shader shapes: nested, non-Principled or unconnected inputs | refused | accepted |
| Mix Shader with a Transparent BSDF anywhere upstream | refused | refused |
| Anything else: another shader, a node group, a reroute, nothing | refused | refused |

A constant mix Factor of 0 or 1 counts as that one Principled. For a genuine
blend, `LIT_ALBEDO` refuses different **Metallic**, **Normal**, **IOR** or
**Diffuse Roughness** on the two sides, because each is copied from one side. Every mode refuses different
**Alpha** when the material is transparent and opacity baking is on.

`LIT_ALBEDO` then runs `_validate_lit_albedo_principled_inputs`. It applies the
direct export's unsupported-input check (`_unsupported_principled_inputs` in
`Plugin/nodes/validate.py`), and also refuses an active **Weight**, **Specular
IOR Level**, **Coat Weight** (with **Coat Roughness** and **Coat Normal**),
**Sheen Weight**, **Subsurface Weight**, or non-zero **Emission Strength**,
because the rebuilt material keeps none of them. **IOR** and **Diffuse
Roughness** are copied onto the rebuilt material as constants
(`_source_principled_constants`), so a linked one is refused.

### 3.4 Which channels are baked, and which are copied

| Channel | `UNLIT_ALBEDO` | `LIT_ALBEDO` | `LIT_IBL` |
|---|---|---|---|
| Base color | baked | baked | baked, with lighting |
| Alpha | baked (§3.5) | baked | baked |
| Normal | dropped | source image passed through | dropped |
| Metallic | dropped | source image or constant passed through | dropped |
| IOR, Diffuse Roughness | dropped | constant copied | dropped |
| Emission | **dropped, without a warning** | **refused** | folded into the lit color |

If emission matters, use `LIT_IBL` or remove it before an `UNLIT_ALBEDO` bake.

Passthrough fails closed. A normal map must be exactly `Image Texture ▸ Color →
Normal Map → Principled` (OpenGL, tangent space, unlinked Strength); metallic a
non-zero constant or an Image Texture's `Color` output. Either image node needs
an unlinked `Vector` and default projection, extension and interpolation.
Anything else, such as a Separate Color chain, raises an error.

Baked base color is `sRGB`; baked roughness and opacity are `Non-Color`.
Passed-through images keep their authored color space, because the datablock is
shared with every other user of that texture (see
[`MATERIAL_TRANSLATION.md`](MATERIAL_TRANSLATION.md#color-space)).

### 3.5 Opacity: straight alpha, merged into base color

Only materials with real transparency get an opacity pass:
`material_has_transparency` reads the active surface's `Alpha` (linked, or a
constant below 0.999), not `surface_render_method`. The pass is an `EMIT` bake of
an Emission node fed from `Alpha`, with the object's other slots pointed at
throwaway targets so it cannot overwrite their base color.

`_configure_emission_for_alpha` takes `Alpha` from `_passthrough_principled`:
the Principled BSDF on the active Material Output, or, for a Mix Shader over two
Principled BSDFs, the side a constant Factor selects or either side of a genuine
blend. §3.3 refuses a transparent blend whose two `Alpha` inputs differ, so both
sides give the same opacity.

The exported material reads `baseColor` from the base image's RGB and `opacity`
from its alpha, and authors no `hasPremultipliedAlpha`, so RealityKit takes the
RGB as straight color and applies opacity once. Cycles' color bakes do not
produce straight color on a transparent surface: `DIFFUSE`/`COLOR` and `COMBINED`
both weight the Principled BSDF by its `Alpha` and write that `Alpha` into the
target's alpha channel. So for every transparent slot, `_unpremultiply_color_bake` divides the color
bake's linear RGB by that alpha before the image is saved, wherever the alpha is
between 0 and 1. Opaque and uncovered texels are left unchanged.

`_merge_opacity_into_base_image` then writes the opacity pass into the base
image's alpha channel as **straight (unassociated) alpha**. RGB stays as the
unpremultiplied color bake left it, `alpha_mode` becomes `STRAIGHT`, and the
separate opacity file is deleted. If the merge cannot run (size mismatch, no
NumPy, read failure), the opacity texture is kept and wired through
`Separate Color ▸ Red`.

With `bake_opacity` off (`--no-opacity`), a transparent material takes opacity
from the base image's own alpha channel, which holds the `Alpha` the color bake
wrote.

### 3.6 Roughness

Only `LIT_ALBEDO` authors roughness. `bake_roughness_mode = TEXTURE` (default)
bakes a `ROUGHNESS` texture at the material's resolution. `AVERAGE` bakes a
throwaway target capped at **64×64** and exports one constant. The target is
zeroed and not cleared by the bake, so `_average_image_value` averages only
texels a UV island covers; margin and UV coverage do not skew it.

**Transparent materials pass their source roughness through.** Cycles'
`ROUGHNESS` pass returns 0 on an alpha-blended surface, a mirror finish in
RealityKit. `_source_roughness_passthrough` copies an unlinked constant or an
Image Texture wired from its `Color` output. Any other chain falls back to the
bake and exports 0.

A slot whose roughness passes through is left out of the roughness bake in both
`TEXTURE` and `AVERAGE`, so no roughness image is written for it. The pass runs
only for objects with at least one slot that bakes roughness, and it bakes the
whole object, so a passthrough slot on such an object is pointed at a 4×4
throwaway target that is never saved.

### 3.7 UV maps, flat materials, and the rebuilt material

The bake follows `mesh.uv_layers.active`, falling back to the first UV map.
`_bind_uv_layer` wires an explicit UV Map node into every image node on the
baked material; an unconnected `Vector` samples whatever map the renderer picks.
Passed-through textures use the UV map named on their own node when set.

`_flat_material_constants` treats a material as *flat* when its tree has no
Image Texture node and the Principled `Base Color` is unlinked (a Mix Shader
only when a constant Factor selects a flat side; a material without nodes uses
`diffuse_color`). Flat materials skip the bake and get base color, roughness,
metallic and alpha authored as constants. A linked `Alpha` always forces a bake;
under `LIT_ALBEDO` a linked `Roughness` or `Metallic` does too. `LIT_IBL` never
skips flat materials, because it must record lighting on them.

Each source material is copied as `<source>_Baked`; a trailing `_Baked(_N)` run
is stripped first, so re-baking never yields `Marble_Baked_Baked`.
`_build_baked_material` clears the copy and rebuilds it as one Principled BSDF →
Material Output, so only what §3.4 lists survives. An opaque baked material is
`DITHERED`; a transparent one keeps the source's `DITHERED` or `BLENDED`.

Baked files are written as `<object>__<material>_<suffix><ext>` and staged like
any texture ([`EXPORT_PIPELINE.md`](EXPORT_PIPELINE.md#where-textures-are-staged)).
After post-processing, `remove_superseded_bake_outputs` deletes the ones the final
USD does not reference, in `bake-export` and in the background job alike, so they
are neither published nor packaged.

---

## 4. Failure modes and what you see

| Situation | Behavior |
|---|---|
| A mesh has no UV map | `Bake failed: '<object>' has no UV map.`; baked slots rolled back |
| A surface or `LIT_ALBEDO` input §3.3 refuses, or a passthrough chain §3.4 cannot copy | hard failure before any material changes |
| No exportable objects; missing external images | `NO_EXPORTABLE_OBJECTS`; `MISSING_EXTERNAL_TEXTURES` or `MISSING_EXTERNAL_ASSETS`, before baking |
| An object with no material slots | exports without materials |
| `LIT_IBL` with `HDRI_FILE`: no path, a missing file, or `//` in a never-saved `.blend` | hard failure, e.g. `Bake mode is 'Lighting & Shadows' but no HDRI file is set.` |
| `LIT_IBL` with `SCENE_WORLD`, no World and no lights | **succeeds with a warning**; textures are black unless a material emits |
| A step exceeds `bake_step_timeout_seconds` | `BAKE_STEP_TIMEOUT`; the worker is terminated |
| The background worker dies, or Blender exits mid-job | `Background job exited unexpectedly.` or `Blender exited before the bake/export job finished.` |

Codes are listed in [`CLI.md`](CLI.md#error-codes); timeout exit behavior is in
[`CLI.md`](CLI.md#exit-codes). A refused surface names its node type (`Bake
Textures cannot preserve material 'M' because its active surface is GROUP, not
one directly connected Principled BSDF. …`); a refused Mix Shader shape names
the problem and points to bake mode `LIT_IBL`.

The no-light warning comes from `_warn_if_scene_has_no_illumination`.
`bake-export` returns it under `warnings`. The sidebar's completion message
counts it (`Baked export complete - 1 warning(s)`) and the job monitor shows its
text below the message.

A failure during the bake or export writes `<output>.diagnostics.json` whatever
`diagnostics_enabled` says (see [`CLI.md`](CLI.md#diagnostics-files)).
`record_bake_diagnostics` records the resolved decisions there in a `bake` block
(`mode`, `resolution`, `image_format`, `margin`, the channel flags, object
counts, `texture_dir`), identically for `bake-export` and the background job.
`resolution` is the size the bake uses (`resolve_effective_bake_resolution`):
`0` means source-keyed in the albedo modes, and `LIT_IBL` records 2048 in place
of a `0`. `bake-export`'s `bake_stats` reports the same value. Baked files
appear under `generated_files` as `baked_base_color`, `baked_roughness` or
`baked_opacity`, with their object and **source** material. Source-material
graph validation is skipped for bake exports, as `validation` records; §3.3's
checks apply instead.

---

## 5. Foreground vs background execution

`usdstage bake-export` bakes in the Blender process the CLI started, against
the `.blend` you named, and prints a JSON envelope. The sidebar's Export button
bakes a disposable copy of your in-memory scene in a **second** Blender process,
so the sidebar stays usable, and reports through `status.json`.

### Launching the background job

The operator refuses to launch with unsaved (dirty) image buffers, `//`-relative
external files in a never-saved scene, or missing external images. It saves the
live scene with `wm.save_as_mainfile(copy=True, relative_remap=True)`, checks
that the active file and dirty state are unchanged, and runs:

```text
blender --background --factory-startup <job dir>/scene_snapshot.blend \
        --python bake_export_runner.py -- <job dir>/settings.json
```

`_serialize_settings` copies every setting except the three `ui_*` settings,
`filepath` and job bookkeeping, forces `export_texture_settings_enabled` on, and
resolves a `//`-relative HDRI while your `.blend` is still active.

`bake_step_timeout_seconds` (**Material Settings ▸ Advanced ▸ Step Timeout**) is `0` (off) by default.

### Status protocol

Each job gets `<export dir>/.usdstage_jobs/bake_export_<YYYYmmdd_HHMMSS>_<random>/`
with `settings.json`, `status.json`, `log.txt` and, until loaded, the snapshot.
The five newest finished job directories are kept.

`status.json` is replaced atomically on every write. It carries `state`
(`queued`, `running`, `done`, `error`, `canceled`) and `time`, and where known
`pid`, `progress` (0.0 to 1.0), `message`, `log_path`, `export_path`,
`diagnostics_path` and `step_elapsed_seconds`. A timeout adds `error_code`,
`stage` and `timeout_seconds`. A successful job omits `diagnostics_path` unless
success diagnostics are on, and adds `warnings`, the text of its first five
warnings, when it has any.

The operator writes `queued`; the worker writes `running` about once a second,
appending `(<N>s)` and dots to the step message, and ends with `progress` 1.0
and the terminal state.

The panel's job monitor shows the state, a progress bar labeled with the
message, and **Open Log** and **Open Diagnostics**; while a job is `queued` or
`running`, the sidebar's settings are locked. Under a job that finished `done`
it shows up to three of the `warnings`, each cut to 120 characters.

The watcher polls every 0.5 s. If the worker's process disappears without a
terminal state, it writes `error`. If a step timeout is set and the worker is
still alive 15 s past it, the watcher re-reads `status.json` and, unless the job
has finished, terminates the worker. The cancel button
(`usdstage.cancel_bake_export`) sends `SIGTERM`, then `SIGKILL` after 2 s,
and writes `canceled`; a job whose recorded process no longer runs is cleared as
stale. The trash button (`usdstage.clear_bake_job`) clears a finished job.
The bake runs on a copy, so canceling cannot leave your scene half-baked.
