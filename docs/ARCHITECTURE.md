# USD Stage for Blender Architecture

This page explains how USD Stage for Blender is organized: which module owns what, and how an export moves through them. Read it to find the code behind a behaviour the other pages describe.

## Overview

USD Stage for Blender is a Blender extension that exports scenes to USD or USDZ, then rewrites Blender-authored materials into RealityKit MaterialX graphs for Reality Composer Pro.

Core design constraints:

- **Blender 5.2 only.** Older Blender compatibility paths are not retained.
- **Strict first.** If a material graph cannot be translated deterministically, the export fails with actionable errors instead of silently falling back.
- **Portable outputs.** Textures and auxiliary assets are copied next to the exported USD and referenced by relative paths.
- **Reality Composer Pro compatibility.** Post-processing reshapes Blender-authored USD into the subset RealityKit and Reality Composer Pro accept. One validation covers every Apple device; see [APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#one-stack-everywhere).
- **Responsive baking.** Bake-backed profiles run in a second Blender process so the foreground UI stays interactive.
- **Built artifacts stay built.** At runtime the add-on loads the prebuilt MaterialX manifest and node-group library; it never regenerates them inside Blender.

## Module map

| Path | Owns |
|---|---|
| `Plugin/__init__.py`, `Plugin/__main__.py` | Add-on registration; `python3 Plugin <command>` runs the CLI from the extension folder. |
| `Plugin/cli/` | `__main__.py` parses CLI arguments and prints JSON; `bridge.py` finds Blender, launches it in background mode on `Plugin/api/runner.py`, and parses the result. |
| `Plugin/api/` | `runner.py`, the headless entry point inside Blender; `commands/`, one module per CLI command, registered in `commands/__init__.py`; `errors.py`, structured command errors. |
| `Plugin/bake_export_runner.py` | The background Blender job behind the panel's bake-backed export. |
| `Plugin/export_profile.py` | Routes the panel's material-type choices to the export or bake operator and a `bake_mode`. |
| `Plugin/apple_contract.py`, `Plugin/material_policies.py`, `Plugin/prefs.py` | The non-configurable spatial contract, material value policies, and add-on preferences. |
| `Plugin/core/` | `paths.py` (bundled resource paths), `version.py` (reads `blender_manifest.toml`), `package_bootstrap.py` (one package name for file-path entry points). |
| `Plugin/export/blender_usd_export.py` | Per-attempt staging directory, the call to Blender's USD exporter, and publication of unpacked exports. |
| `Plugin/export/usd_hook.py` | Captures the exporter's prim map so the material rewrite resolves material prims exactly. |
| `Plugin/export/animation_export.py` | Concatenates actions into one export timeline and restores the scene afterwards. |
| `Plugin/export/postprocess_usd.py` | `process_usd_stage`, the ordered post-processing pipeline. |
| `Plugin/export/usd_scene.py` | Stage metadata, prim naming, and mesh normalization. |
| `Plugin/export/usd_assets.py`, `Plugin/export/usd_textures.py` | Dependency localization and texture staging with relative paths. |
| `Plugin/export/usd_animation_library.py` | The opt-in Reality Composer Pro clip library. |
| `Plugin/export/realitykit_preflight.py` | Strict checks on the composed stage; fails the export on violation. |
| `Plugin/export/asset_preflight.py` | Missing or unpacked source assets, checked before baking and in support bundles. |
| `Plugin/export/bake_textures.py`, `Plugin/export/bake_finalize.py` | Texture baking and baked-material rebuild; whether baked materials are authored Unlit. |
| `Plugin/export/pack_usdz.py` | USDZ packaging, alignment, and archive validation. |
| `Plugin/export/staging_namespace.py`, `Plugin/export/sidecar_manifest.py` | Per-attempt texture generations and the ownership manifest that makes re-export safe. |
| `Plugin/export/diagnostics.py`, `Plugin/export/support_bundle.py` | The diagnostics sidecar and redacted support bundles. |
| `Plugin/export/materials/rewrite.py` | Material rewrite orchestration (see below). |
| `Plugin/export/materials/extract/core.py` | Walks Blender node graphs into normalized `material_data`. `drivers.py`, `hair.py`, `closures.py`, `displacement.py` and `readers.py` beside it hold those node families and reach shared helpers through `core`, which re-exports them. |
| `Plugin/export/materials/graph.py` | `MaterialXGraphBuilder`: `material_data` to a MaterialX graph payload. |
| `Plugin/export/materials/author.py` | Writes the graph payload as USD Shade prims. |
| `Plugin/export/materials/textures.py`, `conversions.py`, `helpers.py` | Texture, UV and normal-map nodes; type conversions; shared authoring helpers. |
| `Plugin/export/materials/mapping.py` | The one-texture-transform-per-material contract, shared by validation, authoring, and preflight. |
| `Plugin/manifest/` | `materialx_nodes.py` loads and queries `rk_nodes_manifest.json`, the prebuilt nodedef index; `rcp_nodedef_input_gaps.json` records inputs RealityKit's nodedef stores lack, read by the preflight. |
| `Plugin/nodes/validate.py` | Material validation used by the export operator, the CLI, and the Shader Editor panel. |
| `Plugin/nodes/metadata.py` | The RealityKit node catalog and the node-group version. |
| `Plugin/assets/nodegroups.blend` | The generated node-group library. |
| `Plugin/ui/` | `panel.py` (3D View sidebar: the main panel with Material Settings, Textures, Advanced USD, and Diagnostics child panels, plus the scene settings group); `shader_panel.py`, `shader_authoring_panel.py`, `shader_menu.py` for the Shader Editor. |
| `Plugin/ops/` | Operators: `export_operator.py`, `bake_export_operator.py`, `validation_operators.py`, `nodegroup_operators.py`. |

## Export pipeline

The panel's Export button resolves a route through `export_profile.resolve_ui_export_route`. Translated exports run the `usdstage.export` operator (`Plugin/ops/export_operator.py`); the CLI `export` command runs the same stages from `Plugin/api/commands/export.py`.

Bake-backed exports run `usdstage.bake_export_background` (`Plugin/ops/bake_export_operator.py`), which saves a copy of the in-memory scene with `_create_scene_snapshot`, so unsaved edits are included and the file need not be saved, then launches `Plugin/bake_export_runner.py` in a background Blender. The runner and the CLI `bake-export` command both call `run_bake_export` (`Plugin/api/commands/bake_export.py`), which bakes with `bake_textures.bake_materials_for_objects`, sets `force_unlit_materials` with `bake_finalize.apply_force_unlit`, continues from stage 2, and restores the scene; the runner reports its steps through `status.json`, the command through the step-timeout watchdog. When a bake runs at all is covered in [BAKING.md](BAKING.md#1-when-baking-happens-at-all).

| Stage | Code | Behaviour documented in |
|---|---|---|
| 1. Validate | `Plugin/nodes/validate.py` refuses unsupported nodes before anything is written. Bake-backed exports skip it. | [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md#shader-nodes) |
| 2. Base USD export | `blender_usd_export.export_blender_scene` exports into a per-attempt staging directory, with `animation_export.prepare_animation_export` and `usd_hook.capture_prim_map` around Blender's exporter. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#1-the-apple-spatial-contract) |
| 3. Post-process | `postprocess_usd.process_usd_stage`, in order: `localize_source_dependencies` (`usd_assets.prepare_assets`, which stages textures through `usd_textures`), `normalize_scene`, `save_localized_layers`, `rewrite_materials`, `author_animation_library`, `finalize_assets`, `normalize_finalized_meshes`, `retag_unmapped_color_spaces`, `author_hair_strand_tangents`, `publish_vertex_colors_as_display_color`, `complete_blend_shape_weights`, `realitykit_preflight`, then the stage is saved. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#where-textures-are-staged), [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#the-composed-stage-preflight), [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#the-experimental-rcp-clip-library) |
| 4. Package or publish | `pack_usdz.create_usdz` for USDZ; otherwise `blender_usd_export.publish_unpacked_export` copies the stage and its owned sidecars. | [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md) |
| 5. Diagnostics | `diagnostics.py` writes `<output>.diagnostics.json`; `support_bundle.py` builds redacted ZIPs. | [CLI.md](CLI.md#diagnostics-files) |

Export settings live on the scene, and last-used values persist in add-on preferences; see [SETTINGS.md](SETTINGS.md#two-independent-stores).

## Material rewrite flow

`rewrite_materials` in `Plugin/export/materials/rewrite.py` runs in two phases so that a failure leaves the stage untouched.

1. **Prepare.** For every bound material prim, including GeomSubset and variant bindings, it resolves the Blender material through the prim map captured by the USDHook, extracts it with `extract_blender_material_data`, and builds a graph with `MaterialXGraphBuilder`. Nothing is written yet.
2. **Author.** It backs up the edit layer, then for each material calls `create_materialx_material` at the existing material path, so the original bindings stay valid, checks that a MaterialX surface was authored, and deletes Blender's UsdPreviewSurface network with `_remove_preview_network` (see [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)). If any material fails, the backup is restored and all failures are raised together.

The extractor assigns each material a kind, and `_build_material_graph` picks a builder:

| Kind | Builder | Terminal |
|---|---|---|
| `principled` | `build_pbr_material` | RealityKit PBR Surface 2 |
| `hair` | `build_hair_material` | `ND_realitykit_hair_surfaceshader` |
| `emission`, `simple` | `build_unlit_material` | RealityKit Unlit |
| `rk_graph` | `build_rk_graph` | A graph of RealityKit node groups, passed through |
| `rk_group` | `build_rk_material` | A single RealityKit node group |

With `force_unlit_materials` set, `principled`, `emission`, `simple` and `hair` all build as Unlit. Every graph declares MaterialX 1.39: `graph.py` records it (`_MATERIALX_VERSION`, or the manifest's `materialx_version` for node-group kinds) and `author.py` writes it as `config:mtlx:version` on the material.

## MaterialX manifest and node catalog

`Plugin/manifest/materialx_nodes.py` loads `rk_nodes_manifest.json` from the path in `Plugin/core/paths.py`. It only reads the manifest; a missing or invalid manifest is reported as an error.

The manifest is built in two layers:

1. **Apple's published definitions.** The RealityKit MaterialX definition files Apple publishes, parsed into nodedef entries with lookup indexes and flags for half-precision and editor-unresolvable nodedefs.
2. **Runtime overlay.** Nodedefs and inputs that the MaterialX library inside `ShaderGraph.framework` declares and the published files lack, marked `policy.runtime_overlay`.

Where the manifest and the library RealityKit loads disagree, the library wins; see [APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported).

## RealityKit node-group authoring path

- `Plugin/nodes/metadata.py` builds the curated catalog from the manifest, with labels and sections, preferring non-half variants.
- `Plugin/assets/nodegroups.blend` holds one node group per catalog entry, each carrying `rk_node_id` and `RK_NODE_VERSION` and a Blender-side preview network. The extension loads it and never builds groups itself.
- `Plugin/ops/nodegroup_operators.py` inserts a RealityKit PBR group, a RealityKit Unlit group, or any catalog node from the Shader Editor's `Add > RealityKit Nodes` menu, loading it from the bundled library when the session lacks it.
- At export, the extractor recognizes these groups by their metadata and emits the `rk_group` or `rk_graph` kind.
