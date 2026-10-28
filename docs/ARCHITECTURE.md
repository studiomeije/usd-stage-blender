# USD Stage for Blender Architecture

This page explains how USD Stage for Blender is organized: which module owns
what, how an export moves through them, and where to make the common kinds of
change. Read it to find the code behind a behaviour the other pages describe,
or before you extend the add-on. Setting up a checkout and running the tests
is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Design rules

- **Blender 5.2 only.** Older Blender compatibility paths are not retained.
- **Strict first.** If a material graph cannot be translated deterministically,
  the export fails with an actionable error instead of silently falling back.
- **One implementation per decision.** The Export button, the command line
  and the bake export run the same export code; the validator asks the
  extractor which nodes it can translate; the asset and texture stagers share
  one answer to which file a path names. A second copy of a decision is how
  two parts of the add-on come to disagree.
- **Portable outputs.** Textures and auxiliary assets are copied next to the
  exported USD and referenced by relative paths.
- **Reality Composer Pro compatibility.** Post-processing reshapes
  Blender-authored USD into the subset RealityKit and Reality Composer Pro
  accept. One validation covers every Apple device; see
  [APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#one-stack-everywhere).
- **Responsive baking.** Bake-backed profiles run in a second Blender process
  so the foreground interface stays interactive.
- **Built artifacts stay built.** At runtime the add-on loads the prebuilt
  MaterialX manifest and node-group library; it never regenerates them inside
  Blender.

## Module map

| Path | Owns |
|---|---|
| `Plugin/__init__.py`, `Plugin/__main__.py` | Add-on registration; `python3 Plugin <command>` runs the command line from the extension folder. |
| `Plugin/cli/` | `__main__.py` parses arguments and prints JSON; `bridge.py` finds Blender, launches it in background mode on `Plugin/api/runner.py`, and parses the result. |
| `Plugin/api/` | `runner.py`, the headless entry point inside Blender; `commands/`, one module per command, registered in `commands/__init__.py`; `commands/_export_common.py`, what the export commands share; `errors.py`, structured command errors. |
| `Plugin/bake_export_runner.py` | The background Blender job behind the panel's bake-backed export. |
| `Plugin/export_profile.py` | Routes the panel's material-type choices to the export or bake operator and a `bake_mode`. |
| `Plugin/apple_contract.py`, `Plugin/material_policies.py`, `Plugin/prefs.py` | The non-configurable spatial contract, material value policies, and add-on preferences. |
| `Plugin/core/` | `paths.py` (bundled resource paths), `version.py` (reads `blender_manifest.toml`), `package_bootstrap.py` (one package name for file-path entry points). |
| `Plugin/export/pipeline.py` | One export, whichever front end starts it: the diagnostics header, the objects it covers, material validation, then writing, post-processing and publishing. Refusals are `ExportRefused` with a public code. |
| `Plugin/export/blender_usd_export.py` | Per-attempt staging directory, the call to Blender's USD exporter, and publication of unpacked exports. |
| `Plugin/export/usd_hook.py` | Captures the exporter's prim map, so materials and objects resolve to their prims exactly. |
| `Plugin/export/animation_export.py` | Which objects an export covers, the one export timeline built from every action, and restoring the scene afterwards. |
| `Plugin/export/scene_state.py` | Changing the scene for a bake - Object Mode, the render engine, the selection - and putting it back. |
| `Plugin/export/postprocess_usd.py` | `PASSES`, the ordered post-processing passes, and `process_usd_stage`, which runs them. |
| `Plugin/export/usd_scene.py` | Stage metadata, prim naming, and mesh normalization. |
| `Plugin/export/asset_paths.py` | Which file an authored asset path names, and what a copied file may be called. |
| `Plugin/export/usd_assets.py`, `Plugin/export/usd_textures.py` | Dependency localization and texture staging with relative paths. |
| `Plugin/export/stage_prims.py` | Every prim of a stage that can reach RealityKit, including instance prototypes and used class prims; walked by the material rewrite and the preflight alike. |
| `Plugin/export/usd_animation_library.py` | The opt-in Reality Composer Pro clip library. |
| `Plugin/export/variants.py` | Resolves the scene's declared variant sets into object sets, refuses the ones that cannot be authored, and stages every member's visibility for the export. Imports no OpenUSD. |
| `Plugin/export/variant_authoring.py` | Writes those sets: a shape set activating each variant's members, a finish set rebinding its targets' material, nested inside a shape set when a finish suits only some shapes. |
| `Plugin/export/colliders.py` | RealityKit collider components, authored from Blender Rigid Body objects and sized from the exported geometry. |
| `Plugin/export/asset_metadata.py` | The root-prim `kind`, `assetInfo` and `extentsHint` that make an export an asset another stage can reference, instance and size. |
| `Plugin/export/payload.py` | The optional split into an asset interface and a payload layer. |
| `Plugin/export/object_scope.py` | Which object types an export covers, and stable datablock identity. Free of `bpy`. |
| `Plugin/export/realitykit_preflight.py` | Strict checks on the composed stage, run for every variant selection; fails the export on violation. |
| `Plugin/export/asset_preflight.py` | Missing or unpacked source assets, checked before baking and in support bundles. |
| `Plugin/export/bake_textures.py`, `Plugin/export/bake_finalize.py` | Texture baking and baked-material rebuild; whether baked materials are authored Unlit. |
| `Plugin/export/pack_usdz.py` | USDZ packaging, alignment, and archive validation. |
| `Plugin/export/staging_namespace.py`, `Plugin/export/sidecar_manifest.py` | Per-attempt texture generations and the ownership manifest that makes re-export safe. |
| `Plugin/export/diagnostics.py`, `Plugin/export/support_bundle.py` | The diagnostics report and redacted support bundles. |
| `Plugin/export/materials/rewrite.py` | Material rewrite orchestration (see below). |
| `Plugin/export/materials/extract/core.py` | Walks Blender node graphs into normalized `material_data`. Each node type's output is read by one reader registered in `_SOCKET_READERS`. `drivers.py`, `hair.py`, `closures.py`, `displacement.py` and `readers.py` beside it hold those node families. |
| `Plugin/export/materials/extract/images.py` | Resolves an image texture to a file on disk, and stages packed, generated or edited images into a temporary directory for the length of an export. |
| `Plugin/export/materials/extract/expr.py` | The builders both layers share: constants, node expressions, manifest nodedef lookup, channel reads, socket defaults. It imports nothing from the package. |
| `Plugin/export/materials/graph.py` | `MaterialXGraphBuilder`: `material_data` to a MaterialX graph payload. |
| `Plugin/export/materials/author.py` | Writes the graph payload as USD Shade prims. |
| `Plugin/export/materials/textures.py`, `conversions.py`, `helpers.py` | Texture, UV and normal-map nodes; type conversions; shared authoring helpers. |
| `Plugin/export/materials/mapping.py` | The one-texture-transform-per-material contract, shared by validation, authoring, and preflight. |
| `Plugin/manifest/` | `materialx_nodes.py` loads and queries `rk_nodes_manifest.json`, the prebuilt nodedef index; `rcp_nodedef_input_gaps.json` records inputs RealityKit's nodedef stores lack, read by the preflight. |
| `Plugin/nodes/validate.py` | Material validation, used by every export, the `validate` command and the Shader Editor panel. Which nodes it accepts comes from the extractor. |
| `Plugin/nodes/metadata.py` | The RealityKit node catalog and the node-group version. |
| `Plugin/assets/nodegroups.blend` | The generated node-group library. |
| `Plugin/ui/` | `panel.py` (3D View sidebar: the main panel with Material Settings, Textures, Advanced USD and Diagnostics child panels, plus the scene settings group); `variants_panel.py` (the Variants panel and the variant property groups on `Scene`); `shader_panel.py`, `shader_authoring_panel.py`, `shader_menu.py` for the Shader Editor. |
| `Plugin/ops/` | Operators: `export_operator.py`, `bake_export_operator.py`, `validation_operators.py`, `nodegroup_operators.py`, `variant_operators.py`. |

## Export pipeline

An export runs in `Plugin/export/pipeline.py`, whichever front end starts it:

| Front end | Code |
|---|---|
| The panel's Export button, for a translated profile | `usdstage.export`, `Plugin/ops/export_operator.py` |
| `usdstage export` | `Plugin/api/commands/export.py` |
| The panel's Export button for a bake-backed profile, and `usdstage bake-export` | `run_bake_export` in `Plugin/api/commands/bake_export.py`, which bakes first |

The panel resolves which front end through
`export_profile.resolve_ui_export_route`. A bake-backed export from the panel
saves a copy of the in-memory scene, so unsaved edits are included, and runs
`Plugin/bake_export_runner.py` in a background Blender that calls
`run_bake_export`. When a bake runs at all is covered in
[BAKING.md](BAKING.md#1-when-baking-happens-at-all).

| Step | Code | Behaviour documented in |
|---|---|---|
| 1. Describe | `pipeline.describe_export` writes the diagnostics header: the command, output, format and environment. | [CLI.md](CLI.md#diagnostics-files) |
| 2. Scope | `pipeline.validation_scope`: a selected-only export covers the selection and what it depends on. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#2-object-selection-and-scope) |
| 3. Validate | `pipeline.validate_materials` refuses a node that cannot be translated, before anything is written. A bake export runs the asset preflight instead. | [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md#shader-nodes) |
| 4. Native export | `blender_usd_export.export_blender_scene` writes into a per-attempt staging directory, with `animation_export.prepare_animation_export` and `usd_hook.capture_prim_map` around Blender's exporter. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#1-the-apple-spatial-contract) |
| 5. Post-process | `postprocess_usd.process_usd_stage` runs the passes below. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#pipeline-order) |
| 6. Package or publish | `pack_usdz.create_usdz` for USDZ; otherwise `blender_usd_export.publish_unpacked_export` installs the stage and its owned sidecars. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#6-staging-and-publication) |

Steps 4 to 6 are `pipeline.write_export`. A refusal anywhere is an
`ExportRefused` with a public `code` and the `stage` it happened in; the
command line reports both, and the panel reports its message. Export settings
live on the scene, and last-used values persist in add-on preferences; see
[SETTINGS.md](SETTINGS.md#two-independent-stores).

### Post-processing passes

`PASSES` in `Plugin/export/postprocess_usd.py` lists them in order. The name
is the phase the diagnostics report records.

| Phase | What it does |
|---|---|
| `localize_source_dependencies` | Copies every layer and asset the stage depends on into the export, and records which layers the export owns. |
| `normalize_scene` | Up axis, units, default prim and prim names; returns the renames, so later passes can translate paths captured before them. |
| `save_localized_layers` | Saves the localized layers before anything recomposes them. |
| `reserve_variant_materials` | Defines a material for every variant finish, including one nothing in the scene wears. |
| `rewrite_materials` | Replaces Blender's materials with RealityKit MaterialX graphs. |
| `author_animation_library` | The Reality Composer Pro clip library, when enabled. |
| `finalize_assets` | Localizes anything the two passes before it authored. |
| `normalize_finalized_meshes` | Re-applies the single-sided mesh contract over the final owned layers. |
| `retag_unmapped_color_spaces` | Renames color-space tokens RealityKit has no alias for. |
| `author_object_random` | Writes each object's Object Info Random value for the materials that read it. |
| `author_uv_derivatives` | Writes the per-corner UV derivatives the Bump node needs. |
| `publish_vertex_colors_as_display_color` | Copies vertex colors into `displayColor` where a material reads them. |
| `complete_blend_shape_weights` | Clears empty blend-shape animation declarations. |
| `author_colliders` | RealityKit colliders for Rigid Body objects. |
| `author_variants` | The declared variant sets. |
| `author_asset_metadata` | `kind`, `assetInfo` and `extentsHint` on the root prim. |
| `realitykit_preflight` | The strict composed-stage checks, for every variant selection. |
| `split_asset_payload` | The interface and payload split, when enabled. |

The order is the design. Assets are localized while external layers still
resolve against their own anchors, so everything after edits only layers the
export owns. Materials are rewritten before anything reads them. Variants are
authored last, over finished work, so every earlier pass saw every variant's
content, and the preflight then judges each selection.

## Bake flow

A bake export bakes before it exports. `bake_materials_for_objects` in
`Plugin/export/bake_textures.py` runs the bake as a sequence of steps over one
`_BakeRun`, which holds the resolved settings, a snapshot of every object's
source materials, the progress counters and the reuse cache. If any step
fails, every material slot is put back before the error is raised.

| Step | Function | What it does |
|---|---|---|
| Plan | `_plan_bake` | Resolves the bake mode, resolution, image format and passes, and snapshots each object's source materials before anything changes them. |
| Refuse | `_refuse_unbakeable_mix_shaders` | Stops on a Mix Shader the bake mode cannot reproduce. |
| Count | `_BakeRun.count_steps` | Counts the passes that will run, so progress is a true fraction. |
| Per object | `_bake_object` | Picks the UV map the bake writes into, and works out a reuse key for each slot. |
| Reuse | `_reuse_cached_bakes` | Binds the baked materials of an earlier object with the same mesh, material and settings, so the two export as instances. Off for Lighting & Shadows, whose lighting differs per object. |
| Targets | `_create_bake_targets` | Copies each slot's material and creates the image the color pass bakes into. |
| Passes | `_bake_color_pass`, `_bake_roughness_pass`, `_bake_opacity_pass` | Bake color (or the lit surface), roughness and opacity, each only where some slot needs it. |
| Finish | `_finish_baked_materials` | Rebuilds each baked material around its images and offers it for reuse. |

What each bake mode captures, and the scene state it overrides, is in
[BAKING.md](BAKING.md).

## Material rewrite flow

`rewrite_materials` in `Plugin/export/materials/rewrite.py` runs in two phases
so that a failure leaves the stage untouched.

1. **Prepare.** For every bound material prim, including GeomSubset and
   variant bindings, it resolves the Blender material through the prim map
   captured by the USDHook, extracts it with `extract_blender_material_data`,
   and builds a graph with `MaterialXGraphBuilder`. Nothing is written yet.
2. **Author.** It backs up the edit layer, then for each material calls
   `create_materialx_material` at the existing material path, so the original
   bindings stay valid, checks that a MaterialX surface was authored, and
   deletes Blender's UsdPreviewSurface network (see
   [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)).
   If any material fails, the backup is restored and all failures are raised
   together.

Extraction reads each linked socket with `_resolve_socket_value`, which looks
up the reader for the upstream node's type in `_SOCKET_READERS`. A reader
returns an expression - a constant, a texture read or a MaterialX node - or
refuses the socket with a reason the export reports.

The extractor assigns each material a kind, and `_build_material_graph` picks
a builder:

| Kind | Builder | Terminal |
|---|---|---|
| `principled` | `build_pbr_material` | RealityKit PBR Surface 2 |
| `hair` | `build_hair_material` | `ND_realitykit_hair_surfaceshader` |
| `emission`, `simple` | `build_unlit_material` | RealityKit Unlit |
| `rk_graph` | `build_rk_graph` | A graph of RealityKit node groups, passed through |
| `rk_group` | `build_rk_material` | A single RealityKit node group |

With `force_unlit_materials` set, `principled`, `emission`, `simple` and
`hair` all build as Unlit. Every graph declares MaterialX 1.39: `graph.py`
records it and `author.py` writes it as `config:mtlx:version` on the material.

## MaterialX manifest and node catalog

`Plugin/manifest/materialx_nodes.py` loads `rk_nodes_manifest.json` from the
path in `Plugin/core/paths.py`. It only reads the manifest; a missing or
invalid manifest is reported as an error.

The manifest is built in two layers:

1. **Apple's published definitions.** The RealityKit MaterialX definition
   files Apple publishes, parsed into nodedef entries with lookup indexes and
   flags for half-precision and editor-unresolvable nodedefs.
2. **Runtime overlay.** Nodedefs and inputs that the MaterialX library inside
   `ShaderGraph.framework` declares and the published files lack, marked
   `policy.runtime_overlay`.

Where the manifest and the library RealityKit loads disagree, the library
wins; see
[APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported).

## RealityKit node-group authoring path

- `Plugin/nodes/metadata.py` builds the curated catalog from the manifest,
  with labels and sections, preferring non-half variants.
- `Plugin/assets/nodegroups.blend` holds one node group per catalog entry,
  each carrying `rk_node_id` and `RK_NODE_VERSION` and a Blender-side preview
  network. The extension loads it and never builds groups itself.
- `Plugin/ops/nodegroup_operators.py` inserts a RealityKit PBR group, a
  RealityKit Unlit group, or any catalog node from the Shader Editor's
  **Add ▸ RealityKit Nodes** menu, loading it from the bundled library when
  the session lacks it.
- At export, the extractor recognizes these groups by their metadata and
  emits the `rk_group` or `rk_graph` kind.

## Extending the add-on

The common changes each have one place to start. Every one of them needs a
test; [CONTRIBUTING.md](../CONTRIBUTING.md#running-the-tests) says how to run
them.

### Translate another shader node

1. Write a reader in `Plugin/export/materials/extract/core.py`, beside the
   others under *Socket readers*: `def _read_<node>(read: _SocketRead)`. It
   receives the upstream node, the socket and the walk state, and returns an
   expression dict, `None`, or an `unresolved` dict with a `reason`. Build
   MaterialX nodes with `_make_node_expr` and `_nodedef_for`; the second
   looks the node up in the manifest and raises when RealityKit has no
   matching nodedef, which fails the export rather than writing a node
   RealityKit cannot load.
2. Register it in `_SOCKET_READERS` under the node's Blender `type`.
3. Move the type into `SUPPORTED_TYPES` in `Plugin/nodes/validate.py`, out of
   `BAKE_TYPES` or `UNSUPPORTED_TYPES`, so validation stops refusing it. If
   some of the node's settings cannot be translated, write a check that
   reports them and register it in `_SUPPORTED_NODE_CHECKS`; prefer asking a
   refusal function beside your reader, so the validator and the export give
   the same answer.
4. Add the node to [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md#shader-nodes) and,
   if its translation is approximate, to
   [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md).

### Add a post-processing pass

1. Write the pass as a function of the stage and what it needs, in
   `Plugin/export/postprocess_usd.py` or its own module.
2. Add `(phase name, lambda run: ..., kept as)` to `PASSES` where its
   dependencies are met. `run` carries the stage, the settings, the Blender
   context, the diagnostics and what earlier passes kept; name a field in
   `kept as` to keep the pass's result for later ones.
3. Add the phase to the table above. A unit test fails until the table and
   `PASSES` agree.

### Add a command-line command

1. Write `Plugin/api/commands/<name>.py` with `handle(args: dict) -> dict`,
   which runs inside Blender and returns JSON-serializable data. Raise
   `CommandError` with a public `code` to refuse.
2. Register it in `REGISTRY` in `Plugin/api/commands/__init__.py`.
3. Add its parser and a `cmd_<name>` handler in `Plugin/cli/__main__.py`.
4. Document it, and any new error code, in [CLI.md](CLI.md).

### Add an export setting

1. Add the property to `USDStageExportSettings` in `Plugin/ui/panel.py`, with
   `update=_on_settings_changed` so it persists as a last-used value, and draw
   it in the panel.
2. Add its key to a group in `SETTING_GROUPS` in
   `Plugin/api/commands/_settings_common.py`, so `settings get` and
   `settings set` reach it.
3. Read it with `getattr(settings, "<key>", <default>)` where it takes effect.
4. Document it in [SETTINGS.md](SETTINGS.md) and in
   [CLI.md](CLI.md#settings-on-the-command-line).

### Refuse something new

Stop the export with an error that names what is wrong and how to fix it:
`ExportRefused` with a new `code` in the export pipeline, a `CommandError` in
a command, or an issue from `validate_material` for a material. List the code
in [CLI.md](CLI.md#error-codes).
