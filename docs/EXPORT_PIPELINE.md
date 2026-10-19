# Export pipeline

This page explains what happens to geometry, animation, and packaging between
pressing Export and getting a file. Read it to understand what the exporter
decides on your behalf, and why re-exporting to the same path is safe.

*Applies to: Blender 5.2 LTS.*

Material translation and texture baking have their own pages
([`MATERIAL_TRANSLATION.md`](MATERIAL_TRANSLATION.md), [`BAKING.md`](BAKING.md)).
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the module map and
[`CLI.md`](CLI.md#settings-on-the-command-line) for setting keys and defaults. Function and
module names in backticks point at the code; you do not need it to use this
page.

## Contents

1. [The Apple spatial contract](#1-the-apple-spatial-contract)
2. [Object selection and scope](#2-object-selection-and-scope)
3. [Geometry decisions](#3-geometry-decisions)
4. [Animation](#4-animation)
5. [Variant sets](#5-variant-sets)
6. [Staging and publication](#6-staging-and-publication)
7. [Packaging](#7-packaging)
8. [Preflight and validation gates](#8-preflight-and-validation-gates)

## Pipeline order

Every export runs the same sequence, whether you start it from the sidebar
(`Plugin/ops/export_operator.py`), the CLI `export` command
(`Plugin/api/commands/export.py`), or the bake lane
(`Plugin/api/commands/bake_export.py`). *Staging* means assembling the export
in a private temporary directory. *Publication* means installing the finished
files into your output directory in an order that can never leave it
half-written.

| Step | Implementation | Notes |
|------|----------------|-------|
| Selection closure | `collect_export_objects` | Fails closed on an empty selected-only export |
| Material validation | `Plugin/nodes/validate.py` | Strict, before any file is written; the bake lane runs the asset preflight instead |
| Staging directory | `create_export_staging_dir` | `.usdstage_temp/<output>.<attempt>/` |
| Animation preparation | `prepare_animation_export` | Concatenates takes onto one NLA track and bakes |
| Native USD export | `export_blender_scene` | `bpy.ops.wm.usd_export` with a fixed contract |
| Post-processing | `process_usd_stage` | The steps below |
| Package or publish | `create_usdz` / `publish_unpacked_export` | USDZ archive, or transactional publication |
| Staging cleanup | `remove_export_staging_dir` | Runs from `finally`, success or failure |

Post-processing (`Plugin/export/postprocess_usd.py`) runs in this order:

1. Localize source dependencies into the export-owned namespace.
2. Normalize the scene: default prim, up axis, prim names, mesh repair,
   `doubleSided`.
3. Rewrite materials into RealityKit MaterialX graphs declaring MaterialX 1.39,
   deleting Blender's UsdPreviewSurface network (see
   [No UsdPreviewSurface network](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)).
4. Author the RCP animation library, when enabled.
5. Finalize assets: localize anything steps 3 and 4 authored.
6. Re-apply `doubleSided = false` over the final set of owned layers.
7. Retag `colorSpace:name` tokens RealityKit has no alias for (see
   [Color space](MATERIAL_TRANSLATION.md#color-space)).
8. Write the primvars materials read that Blender does not export: each
   object's Object Info Random value, and the per-corner UV derivatives Bump
   needs.
9. Copy vertex colors into `displayColor`/`displayOpacity` where a material
   reads them.
10. Clear empty blend-shape animation declarations.
11. Run the [composed-stage preflight](#the-composed-stage-preflight).

## 1. The Apple spatial contract

Every export is Y-up, faces `-Z` forward, and is scaled in meters. These are
**not** export options. They are constants in `Plugin/apple_contract.py` that
`_build_export_kwargs` passes to Blender's exporter, with
`convert_orientation=True`, regardless of your scene or settings. The
conversion is an authored transform on the export root prim; vertex data is not
rewritten. Post-processing re-asserts the up axis, and preflight fails the
export if the up axis or `metersPerUnit` deviates.

**Why it is fixed.** Reality Composer Pro converts scene units and up axis
when it imports a file, but a RealityKit app loading a USDZ directly does not,
so Y-up meters is the only choice that is correct in both. Likewise, Reality
Composer Pro's viewport honors the `doubleSided` flag while the RealityKit
runtime ignores it: a file relying on it would look right in the editor and
wrong on device. The exporter always writes single-sided geometry (see
[Mesh repair and the double-sided contract](#mesh-repair-and-the-double-sided-contract)).

**Other unit scales are refused.** Blender's exporter never rescales geometry
to match the scene's Unit Scale; it only declares `metersPerUnit`. With that
pinned to `1.0`, a model you read as "2 cm" at Unit Scale `0.01` would arrive
2 meters wide. So `_require_supported_scene_unit_scale` stops any export whose
Unit Scale is not `1.0`, and says how many times too large the asset would be.
Set Scene Properties > Units > Unit Scale to `1.0`, and apply object scale
where needed.

## 2. Object selection and scope

`selected_objects_only` (default `false`) exports either every scene object or
every selected object that passes the object-class filter. An empty
selected-only scope is never widened to the whole scene; it fails with
`NO_EXPORTABLE_OBJECTS` before anything is written.

- **Export closure** (`collect_export_objects`) is what gets selected for
  Blender's operator. It adds only the deforming armature of a selected skinned
  mesh, when `export_armatures` is on and the Armature modifier is enabled; a
  modifier with no target object is an error. Parents and collection-instance
  prototypes are not added: Blender already exports parent transforms and
  expands instances, and selecting them would duplicate geometry.
- **Processing closure** (`collect_processing_objects`) adds the full parent
  chain and every collection-instance prototype, for animation preparation and
  validation. Animated prototypes outside the active scene are linked
  temporarily for the bake.

`_is_exportable_object` always exports meshes and Empties, and armatures when
`export_armatures` is on (the default). Every other object type, including
lights, cameras, curves, point clouds, and volumes, is dropped silently, and
the matching native exporter flags are fixed off, because RealityKit does not
import those schemas.

Without `selected_objects_only`, the export scope (`view_layer_objects`) is the
set of objects Blender's USD exporter writes, so baking and validation never
process an object that does not reach the file:

| Object or collection state | `evaluation_mode` `RENDER` (default) | `VIEWPORT` |
|---|---|---|
| Collection excluded from the view layer (unchecked in the Outliner) | left out | left out |
| Disabled in Renders (`hide_render`), on the object or a collection | left out | exported |
| Disabled in Viewports (`hide_viewport`), on the object or a collection | exported | left out |
| Hidden with the Outliner's eye | exported | exported |

## 3. Geometry decisions

### Root prim and mesh options

- **Root prim.** `root_prim_name` (default `/root`) is passed as
  `root_prim_path`. If the stage has no `defaultPrim`, post-processing sets
  one at root level, creating an `Xform` if needed and turning a nested name
  such as `a/b` into `a_b`.
- **Triangulation.** `triangulate_meshes` defaults to `false`. When enabled,
  the UI's `EAR_CLIP` n-gon method is translated to `CLIP`, the spelling
  Blender's USD operator accepts (`_ngon_method_for_usd_export`).
- **Subdivision.** `export_subdivision` defaults to `BEST_MATCH`, which writes
  the base mesh with `subdivisionScheme = "catmullClark"`. RealityKit keeps the
  base mesh on the CPU and renders it subdivided: a cube with a level-3
  Subdivision Surface covers the same pixels with **Best Match** as with
  **Tessellate**. Preflight requires `subdivisionScheme` to be authored,
  because unauthored means `catmullClark` in USD, and warns about the runtime
  cost of any scheme other than `none`.
- **Instancing.** `use_instancing` defaults to `true`, the opposite of
  Blender's operator. A collection instance becomes an `instanceable` Xform
  that references a prototype under an abstract `class "prototypes"` subtree,
  so the mesh data appears once. An ordinary composed-stage traversal cannot
  reach that subtree, which is why mesh normalization edits raw `Sdf` specs.

### Prim naming and sanitization

`_rename_invalid_prims` repairs every prim name, inactive prims included, that
is not a valid USD identifier. Each character that is neither `_` nor
alphanumeric becomes `_`, a name not starting with a letter or underscore gets
a `prim_` prefix, and collisions get `_2`, `_3`, and so on. With
`allow_unicode` off (it defaults to on), only ASCII survives. So `My Object! 1`
exports as `My_Object__1`. Renames are applied one depth at a time as an
`Sdf.BatchNamespaceEdit`, which keeps descendants, variants, and time samples;
relationship targets, shader connections, and the `defaultPrim` are retargeted
afterwards. A prim that exists only through an external composition arc cannot
be renamed and fails the export.

### Mesh repair and the double-sided contract

`_repair_xform_mesh_prims` re-types a prim to `Mesh` when Blender wrote mesh
topology onto an `Xform`, which Reality Composer Pro would not treat as
geometry. `_normalize_owned_double_sided_mesh_specs` authors `doubleSided = false` on
every Mesh spec in an output-owned layer; Blender 5.2 authors `true`. It edits
raw specs because a composed edit would override externally referenced
geometry and cannot see inactive variants. Layers the export does not own are
never edited, so a surviving external `true` fails preflight with
`DOUBLE_SIDED_GEOMETRY`. Each change is recorded as an info note.

## 4. Animation

Animation export is off by default (`export_animation`).

### What counts as an animated take

`_collect_targets` produces one target per animated owner: an armature, another
exportable object, or a mesh's shape keys. A Blender 5.2 Action can hold several
slots, and a slot is the ownership boundary, so `_action_bindings_for_owner`
collects only explicit associations: the owner's active Action, Actions in NLA
strips on the owner, and Action slots whose `users()` include the owner. A
slotless Action, a slot for the wrong ID type or with no F-Curves, an F-Curve
path that does not resolve, one Action bound through two slots, and NLA tweak
mode are all errors.

**A stashed Action is not exported.** An Action that is neither active nor in
an NLA strip has no live slot users. `_warn_about_stashed_actions` names every
such Action in a warning. To export a take, assign it or push it to an NLA
strip.

### Concatenation and baking

`_build_schedule` sorts the Actions case-insensitively by name and lays them
end to end, quantized to integer frames. Each take spans `ceil(length)` frame
intervals, and the next take starts on the *following* frame: Blender's NLA
bake samples integer frames only, so sharing a frame between two takes would
drop a pose. A fractional range is time-scaled onto the integer span with a
warning. For example, `A_active` (frames 1–10) and `B_nla` (frames 1–5) occupy
frames 1–10 and 11–15.

Each target's takes become strips on a soloed NLA track named
`__USDStage_Export__`, baked with `bpy.ops.nla.bake` and visual keying.
Shape keys are sampled into F-Curves at every integer frame. A target with more
than one take gets a warning that one baked animation cannot hold a hard cut
between takes. `restore_animation_export` then removes the track and restores
mute and solo flags, the original Action and its slot, and the frame range.

### Skeletons and shape keys

Blender authors `SkelRoot`, `Skeleton`, `SkelAnimation`, and `BlendShape` prims
from `export_armatures`, `only_deform_bones`, and `export_shapekeys`. Preflight
checks them. Time-sampled mesh `points` fail with
`VERTEX_ANIMATION_UNSUPPORTED`, so convert vertex-cache animation to shape keys
or skinning.

Shape keys travel through the skeletal schema. A shape-keyed mesh with **no
armature** still gets a `SkelRoot` and a synthesized one-joint `Skeleton` that
deforms nothing. When nothing animates the shapes, Blender still names them in
the `SkelAnimation` without weights, which Reality Composer Pro refuses; the
exporter clears that empty declaration and warns, and the shapes stay on the
mesh.

### The experimental RCP clip library

`author_animation_library` defaults to **`false`**. With it and
`export_animation` on, `Plugin/export/usd_animation_library.py` writes a
Reality Composer Pro `AnimationLibrary` component under the default prim: one
`RealityKitClipDefinition` whose `sourceAnimationName` is always
`"default subtree animation"` (the default prim's animation plus every
descendant's, including UsdSkel), with `clipNames` from the schedule and
`startTimes` in seconds from the stage start. Two takes produce:

```usda
def RealityKitComponent "AnimationLibrary"
{
    uniform token info:id = "RealityKit.AnimationLibrary"

    def RealityKitClipDefinition "Clip_default_subtree_animation"
    {
        uniform string[] clipNames = ["A_active", "B_nla"]
        uniform string sourceAnimationName = "default subtree animation"
        uniform double[] startTimes = [0, 0.4166666666666667]
    }
}
```

`0.41666…` is `(11 - 1) / 24`. A repeated take name gets a counter. An existing
library is replaced, and exporting with the setting off removes it.

It is opt-in because Reality Composer Pro recognizes the schema but flattens
the named clips into the one aggregate animation when it imports the file, so
the clip names do not survive. Leave it off for RealityKit runtime exports and
split the imported animation in app code.

## 5. Variant sets

Blender authors no USD variant sets, and its exporter does not write
collections as prims, so a scene organised as `Seat/Fabric` and `Seat/Leather`
arrives in USD flat with the grouping gone. USD Stage for Blender authors the
sets itself, from the sets you declare in the **Variants** panel.

### What you declare

A variant set is a name and a list of variants; each variant points at the
collection whose objects it owns. **From Collection** builds one in a click
from a parent collection whose children are already the variants. The sets are
saved in the `.blend`, so the panel and the command line export the same
variants from the same file, and `usdstage info` lists them without exporting.

Objects in no variant are untouched and present whatever the selection.

### What is written

One `variantSet` on the asset's root prim — where the OpenUSD asset guidelines
put it, so a selection stays reachable even when the component is instanceable
or its payload is unloaded. Inside each variant, every member of every variant
in that set is activated or deactivated:

```usda
def Xform "root" (
    variants = { string Seat = "Fabric" }
    prepend variantSets = "Seat"
)
{
    variantSet "Seat" = {
        "Fabric" { over "SeatFabric" { bool active = 1 }
                   over "SeatLeather" { bool active = 0 } }
        "Leather" { over "SeatFabric" { bool active = 0 }
                    over "SeatLeather" { bool active = 1 } }
    }
}
```

`active` is authored **only** inside the variants. Variants sit low in USD's
composition order, so an opinion on the same field in the local layer stack
beats them and the set silently stops switching; the preflight refuses that
case rather than shipping it.

### Hidden variants are brought in for the export

The variant you are not working on is usually hidden, and Blender's exporter
drops an excluded collection and anything the active **Use Settings for** mode
sees as hidden. So the export opens every member's path into the view layer
first and puts your view layer back exactly as it was afterwards, even if the
export fails. Only the outliner's eye never mattered: Blender exports through
it regardless.

With **Author Variant Sets** off, neither authoring nor that staging runs, and
the export holds exactly what the viewport shows.

### Shape sets and finish sets

A variant set varies one of two things, chosen per set:

| | **Shape** | **Finish** |
|---|---|---|
| Each variant owns | a collection | a material |
| The set also needs | — | the collection it applies to |
| What it authors | `active` on the member prims | `material:binding` on the targets |

A finish set leaves every object present and rebinds it, so the same objects
can belong to a shape set *and* a finish set: the two vary different USD
properties and neither shadows the other. That is how one file offers three
tops and four finishes as twelve looks without twelve copies of the geometry.

What is refused is two sets varying the **same** property of one object — two
shape sets both authoring `active`, or two finish sets both rebinding. The
rule is about the property, not the object.

Two consequences worth knowing:

- Blender writes a `material:binding` on every mesh, and a local opinion beats
  every variant. Authoring a finish set therefore **removes** that binding, so
  the variants are the only thing saying what the object wears. Every
  selection supplies one, and the preflight refuses a stray binding left
  outside.
- A finish you do not currently use is still exportable. Blender writes no
  material prim for a material no face wears, so the finish set defines one
  and the MaterialX rewrite converts it like any other.

### Offering a finish on some shapes only

Independent sets offer every combination, including ones you do not sell. When
a finish suits only some shapes, nest the finish set inside the shape set:
set **Nest Inside** to the shape set, and give each finish an **Only For**
list of the shapes that offer it. A finish with no list is offered on all of
them.

The finish set is then authored inside each shape variant rather than beside
it, so selecting a shape changes which finishes exist:

```
Shape = Round   → Finish offers Oak
Shape = Square  → Finish offers Oak, Walnut
```

Inside a shape variant only that shape's prims compose, so the finishes bind
exactly what is present. Every shape must offer at least one finish, or the
export is refused: a shape that ships with nothing to choose is a mistake, not
a style. One level of nesting is the limit.

### What variants cannot do

- **They cannot be animated.** USD resolves a selection at composition time.
- **A member cannot be in two variants of one set**, and a member cannot be
  parented under another variant's member: deactivating the parent takes the
  child with it, so the child's variant could never compose. Both are refused
  before anything is written.
- **A finish set over a multi-material mesh** sets every GeomSubset to one
  material, and warns that it did.
- **A selected-only export** carries the other variants of any set the
  selection touches, and leaves out a set it does not touch.
- **Reality Composer Pro's `.reality` compile keeps only the selection you
  shipped.** `realitytool` resolves the default selection and discards the
  rest, so a `.reality` built from an `.rkassets` bundle carries no variants
  unless you define Reality Composer Pro's own variants in the editor. The
  variant sets survive in `.usd`, `.usdc` and `.usdz`, and the USDKit stage
  APIs switch them at runtime.

## 6. Staging and publication

### The attempt directory

Nothing is written directly to the destination. Every export first allocates
a private directory:

```
<output dir>/.usdstage_temp/<portable output filename>.<32 hex chars>/
```

The portable filename is the complete output filename with every character
other than letters, digits, `-`, `_`, and `.` replaced, plus a short digest when
anything changed. The hex part is a random attempt token. Together they make
the directory an ownership handle: `scene.usda` and `scene.usdc` never share
one, and concurrent exports never delete each other's work. Staging also stops
Blender's exporter from reusing stale files in an existing `textures/`.

`_validate_export_staging_dir` refuses symlinks and requires both levels to
resolve inside the output directory. All three entry points remove the attempt
directory from a `finally`, and remove `.usdstage_temp` only when empty.
The bake lane bakes into `<staging>/textures` before the native export, so it
allocates the directory itself and passes `reset_staging=False`.

### Where textures are staged

Staged sidecars (the texture and asset files that accompany a USD file) go into
a per-attempt namespace from `output_sidecar_namespace`
(`Plugin/export/staging_namespace.py`):

```
textures/<portable USD filename>/<32 hex generation token>/<file>
assets/<portable USD filename>/<32 hex generation token>/<file>
```

The generation token is random, recorded in an `O_EXCL` marker under
`.usdstage_generations/` beside the staged USD, so every sidecar of one
attempt shares one immutable namespace and the next attempt gets a new one. The
marker never reaches your output folder, and the `textures` name is fixed.
Asset paths are authored relative to the layer that references them
(`stage_layer_texture_asset` in `Plugin/export/usd_textures.py`). A USDZ keeps
the same layout inside the archive.

**Content-addressed file names.** `_finalize_content_addressed_texture` names
each staged texture after its final bytes:

```
<USD file stem>-<source stem>-<SHA-256, 64 hex><extension>
```

The digest covers the **destination file** after any conversion, not the
source path, and is never truncated, so a name is never seen with stale bytes
during publication. The readable stem is NFC-normalized, loses any earlier
digest suffix, and is truncated to 120 UTF-8 bytes; on Windows it is shortened
further, down to 8 bytes, to keep the path within the 260-character limit.

Identical textures are stored once: staging reuses a file already in the
current generation, a source it already staged by path or by bytes, and an
existing file with the same digest. A same-name file with different bytes, or
a symlink, fails with `Content-addressed texture collision`. Format conversion
and resizing are in
[The texture pipeline](MATERIAL_TRANSLATION.md#the-texture-pipeline).

### Publication, and why re-exporting is safe

Each output owns an explicit list of its sidecar files, at
`<output dir>/.usdstage_sidecars/<output identity>.json`
(`Plugin/export/sidecar_manifest.py`). The identity is the filename,
NFC-normalized and case-folded, and sibling outputs that collapse to the same
identity are rejected. Only `textures/` and `assets/` are ownable, and support
bundles read this list instead of walking shared directories.

`publish_unpacked_export` first takes an exclusive, non-blocking lock under
`<output dir>/.usdstage_publish/locks/`; a second process publishing the
same output fails with "Another export is already publishing" rather than
interleaving. The lock file stays afterwards by design. With the lock held:

1. **Recover** transaction directories this output abandoned in a hard exit.
2. **Plan.** Refuse to overwrite an unowned file, a non-file, or an owned file
   with different bytes. An identical existing file is shared.
3. **Prepare** every replacement in a transaction directory on the same
   filesystem, so committing is only `os.replace` calls.
4. **Commit, root last** (`_execute_root_last_publication`): write a transition
   manifest claiming old and new files, install every new sidecar, atomically
   replace the root USD, then write the final manifest.
5. **Roll back** on any exception, including cancellation.
6. **Clean up** the superseded generation's files and emptied directories.

New sidecars land in a new generation directory, so they cannot collide with
files the published root references. The root is swapped last, so a reader
sees the whole old asset or the whole new one. The transition manifest lets a
retry after a hard kill tell a half-installed generation from an unowned user
file, and stale files go only after the new root is in place. A hard exit may
leak an unused generation but cannot corrupt the published asset, and
re-exporting leaves exactly one generation directory.

### Exports are not byte-reproducible

Exporting the same unchanged `.blend` twice does not give you the same file.
Blender's USD exporter writes top-level prims in an order that varies between
runs: the prims and their values are identical, the order is not, so the bytes
and any checksum differ.

This matters if you put exports under version control or compare them by hash.
Diff the composed stage rather than the file, for example by opening both with
USD and walking the prims in sorted path order, or accept that every re-export
shows as changed.

## 7. Packaging

`export_format` offers `USDA`, `USDC`, and `USDZ` (the default), and the format
sets the output file's extension. USDZ stages its root as binary `.usdc`.

USDZ is not an arbitrary ZIP. `Plugin/export/pack_usdz.py` enforces its layout
so exports stay valid even without Apple's `usdzip`: every member is stored
uncompressed, every payload begins on a 64-byte boundary (padded inside a
skippable ZIP extra field), and the root USD layer is the first member. Members
also need safe relative paths, an allowed extension (`.usd`, `.usda`, `.usdc`,
`.usdz`, `.png`, `.jpg`, `.jpeg`, `.exr`, `.avif`, `.m4a`, `.mp3`, `.wav`), and
names that do not collide under Unicode normalization and case folding.
Exporter bookkeeping (`.usdstage_*`) never enters the archive.

`create_usdz` writes to a temporary sibling and replaces the destination only
after validation passes; a symlinked destination is refused. If the
`usdzip_path` preference points at an executable, it runs
`usdzip --asset <root> --checkCompliance <out>`; otherwise the built-in aligned
packager runs and self-validates. External tools run in their own process
group with timeouts (600 seconds to package, 300 to check).

## 8. Preflight and validation gates

### Before anything is written

| Gate | Failure |
|------|---------|
| CLI setting overrides | `INVALID_SETTING_OVERRIDE`, `INVALID_SETTING_VALUE` |
| Selection closure raises | `INVALID_EXPORT_SELECTION` |
| Selected-only with nothing exportable | `NO_EXPORTABLE_OBJECTS` |
| Strict material validation (not the bake lane) | `UNSUPPORTED_MATERIAL_NODES` |
| Missing external files (bake lane only) | `MISSING_EXTERNAL_TEXTURES` / `MISSING_EXTERNAL_ASSETS` |

Material validation covers the processing closure when `selected_objects_only`
is on, and otherwise the processing closure of the export scope (§2). See [Error codes](CLI.md#error-codes)
for the full list.

**The asset preflight is bake-only.** `Plugin/export/asset_preflight.py` walks
the datablocks the processing closure reaches, including prototypes, nested
node groups, caches, linked libraries, and the World when it contributes, and
expands UDIM tiles, sequences, and caches with `BlendData.file_path_foreach`.
It picks `MISSING_EXTERNAL_TEXTURES`, or `MISSING_EXTERNAL_ASSETS` when a
non-image file is missing, and `Plugin/api/commands/bake_export.py` raises it
before baking. The plain export does not call it: a missing texture fails later,
during texture staging, with `EXPORT_FAILED` and "Texture file not found".

**Operator contract.** `_validate_export_operator_contract` fails if Blender's
live USD operator lacks any argument the pipeline passes. `_invoke_usd_export`
turns any new `ERROR` report into a failure, forwards `WARNING` reports to
diagnostics, and requires a `FINISHED` result.

### The composed-stage preflight

`validate_stage` (`Plugin/export/realitykit_preflight.py`) runs against the
**composed stage** (the assembled USD scene after all layers are combined) as
the last post-processing step, and any error fails the export. With variant
sets, the checks run across every combination, up to 256
(`MAX_VARIANT_COMBINATIONS`).

| Area | Errors (the export fails) |
|------|-------|
| Stage metadata | `DEFAULT_PRIM_MISSING`, `DEFAULT_PRIM_NOT_ROOT`, `UP_AXIS_UNAUTHORED`, `UP_AXIS_NOT_Y`, `METERS_PER_UNIT_UNAUTHORED`, `METERS_PER_UNIT_INVALID`, `METERS_PER_UNIT_NOT_ONE` |
| Prims and meshes | `UNSUPPORTED_REALITYKIT_PRIM_TYPE`, `MESH_TOPOLOGY_MISSING`, `VERTEX_ANIMATION_UNSUPPORTED`, `SUBDIVISION_SCHEME_UNAUTHORED`, `SUBDIVISION_SCHEME_INVALID`, `DOUBLE_SIDED_GEOMETRY`, `TOO_MANY_UV_SETS` |
| Material bindings | `MATERIAL_BINDING_API_MISSING`, `MATERIAL_BINDING_INVALID`, `MATERIAL_TEXTURE_TRANSFORM_CONFLICT`, `TEXTURE_TRANSFORM_UNINSPECTABLE` |
| Skeletons | `MULTIPLE_SKELETONS`, `SKELETON_OUTSIDE_SKEL_ROOT`, `SKELETON_JOINTS_MISSING`, `SKELETON_BINDING_INVALID`, `SKELETON_TARGET_INVALID`, `SKEL_BINDING_API_MISSING`, `SKINNING_PRIMVARS_INCOMPLETE` |
| Textures | `TEXTURE_ASSET_MISSING`, `TEXTURE_COLOR_ROLES_CONFLICT`, `TEXTURE_COLOR_SPACE_MISMATCH`, `TEXTURE_COLOR_SPACE_UNSUPPORTED_TOKEN`, `USDZ_TEXTURE_FORMAT_UNSUPPORTED`, `USDZ_TEXTURE_PATH_EXTERNAL` |
| Variants | `VARIANT_SET_UNINSPECTABLE`, `VARIANT_VALIDATION_LIMIT` (more than 256 combinations) |

`UNSUPPORTED_REALITYKIT_PRIM_TYPE` covers curves, NURBS, `Points`,
`PointInstancer`, `ParticleField`, `TetMesh`, volumes, every light type, and
`Camera`, each with a specific remediation. The MaterialX errors each mean
RealityKit would drop or replace the material; the two version checks read
`Plugin/manifest/rcp_nodedef_input_gaps.json` (see
[Which MaterialX nodes are supported](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported)).

| MaterialX error | Meaning |
|------|---------|
| `UNKNOWN_MATERIALX_NODEDEF` | A shader's `info:id` is not a nodedef RealityKit can resolve |
| `MATERIALX_NODEDEF_UNSUPPORTED_BY_RCP` | A four-channel vector reader or extractor Reality Composer Pro replaces with a placeholder |
| `MATERIALX_NODEDEF_ABSENT_AT_DECLARED_VERSION` | The nodedef does not exist at the MaterialX version the material declares |
| `MATERIALX_INPUT_ABSENT_FROM_NODEDEF` | The shader authors an input the nodedef does not declare at that version |
| `GEOMPROPVALUE_PRIMVAR_TYPE_MISMATCH` | A primvar read declares a different channel count than the primvar has |

Warnings and info notes do not stop the export:

| Code | Severity | Meaning |
|------|----------|---------|
| `PRELIMINARY_SCHEMA` | warning | A `Preliminary_` schema that needs renderer-specific testing |
| `SUBDIVISION_RUNTIME_COST` | warning | A subdivision scheme other than `none` |
| `USDSKEL_SCHEMA_UNAVAILABLE` | warning | Skeleton bindings could only be checked structurally |
| `TEXTURE_ALPHA_SOURCE_MISSING` | warning | A four-channel reader points at a texture with no alpha |
| `LIGHTMAP_UV_EMPTY` | warning | The second UV set has no coordinates |
| `ACCESSIBILITY_LABEL_MISSING`, `ACCESSIBILITY_DESCRIPTION_MISSING` | warning | A prim with AccessibilityAPI lacks a label or description |
| `MATERIALX_MANIFEST_UNAVAILABLE` | warning | The node manifest failed to load, so `info:id` values went unchecked |
| `MATERIALX_NODEDEF_INPUT_TABLE_UNAVAILABLE` | warning | The input-gap table failed to load, so shader inputs went unchecked |
| `LIGHTMAP_UV_PRESENT` | info | A second UV set exists; check it in the Reality Composer Pro lightmap baker |
| `LIGHTMAP_UV_MISSING` | info | No second UV set for lightmaps |
| `TEXTURE_COLOR_ROLE_UNRESOLVED` | info | A texture's color role could not be inferred |
| `ACCESSIBILITY_METADATA_MISSING` | info | No prim carries AccessibilityAPI |

### After packaging

For USDZ, `validate_usdz_details` runs the structural checks above and then
`usdchecker`, found next to a configured `usdzip`, through `xcrun --find` on
macOS, or on `PATH`. A checker that advertises `--arkit` runs
`usdchecker --arkit --strict`, and a failed capability probe fails the export.
A checker without `--arkit` gets `--strict` with a warning, and no checker at
all is noted in the diagnostics without a warning. Diagnostics record the level reached as
`usdchecker_arkit_strict`, `usdchecker_strict_fallback`, or `structural_only`.

`usdchecker --arkit --strict` is a structural check only. Passing it does not
prove that Reality Composer Pro or RealityKit renders the asset; only importing
it and looking does.

Every failed export writes a `.diagnostics.json` sidecar; see
[Diagnostics sidecars](CLI.md#diagnostics-files).
