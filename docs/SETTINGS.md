# USD Stage for Blender settings reference

This page lists every USD Stage for Blender export setting, the panel control that shows it, and the other settings it depends on or overrides. Read it when a setting seems to have no effect.

[`CLI.md`](CLI.md#settings-on-the-command-line) covers how to pass a setting. `usdstage settings list` prints each key's type, allowed values, default, and group. This page covers what the exporter does with it.

## Two independent stores

USD Stage for Blender keeps two kinds of configuration.

| | Per-scene export settings | Add-on preferences |
|---|---|---|
| Defined in | `USDStageExportSettings` in `Plugin/ui/panel.py` | `USDStagePreferences` in `Plugin/prefs.py` |
| Stored on | `Scene.usd_stage_export_settings`, saved with the `.blend` | Blender user preferences |
| UI | 3D Viewport ▸ sidebar (N) ▸ **USD Stage** tab | Edit ▸ Preferences ▸ Add-ons ▸ USD Stage for Blender |
| CLI | `settings list/get/set`, and `key=value` overrides on `export` and `bake-export` | `preferences get/set` |

### How per-scene settings persist

Every change you make in the panel does two things:

1. It rewrites the extension of `filepath` to match `export_format`.
2. It saves every public setting except `filepath` into the hidden preference `last_export_settings_json`.

What that means:

- **A scene keeps the settings saved with it.** The panel and the command line export a `.blend` with the same values.
- **A scene with no saved settings starts from your last-used values.** The first time the panel or an export operator touches such a scene in a Blender session, it copies them in from `last_export_settings_json`. A payload from another schema version, or one missing a key, is ignored.
- **`filepath` is remembered per `.blend`**, in the hidden preference `last_export_paths_json`, and fills an empty Output Path. An unsaved `.blend` has no remembered path.
- **`export` and `bake-export` do not write preferences.** They suspend persistence for the whole command, so a `key=value` override does not leak into your panel values.

## The settings

"Applies to" names the pipeline that reads a key:

- **direct** is `usdstage export`, or the panel with Profile set to RealityKit PBR and Processing set to Translate Materials.
- **bake** is `usdstage bake-export`, or the panel with any other Profile choice.
- **both** means both pipelines read it.

UI paths below start from the **USD Stage** tab. Where a row says "see below", the interaction is described after the table.

| Key | UI control | Applies to | Precondition or interaction |
|---|---|---|---|
| `filepath` | Export ▸ Output Path | both | The extension always follows `export_format`. A `//` or other relative path is refused until the `.blend` is saved. On the CLI, pass `-o/--output`. |
| `export_format` | Export ▸ Format | both | Only `USDZ` packages the result and consults `usdzip_path`. |
| `root_prim_name` | Advanced USD ▸ General ▸ Root Prim | both | A missing leading `/` is added. Empty means `Scene`. |
| `export_animation` | Export ▸ Include ▸ Animation | both | Master switch for animation, including `author_animation_library`. |
| `author_animation_library` | Export ▸ RCP Clip Library, shown only with Animation on | both | Needs `export_animation`. See below. |
| `selected_objects_only` | Export ▸ Include ▸ Selection Only | both | Narrows validation and fails on an empty selection. See below. CLI flag: `--selected-only`. |
| `export_custom_properties` | Advanced USD ▸ General ▸ Custom Properties | both | Master switch for the next two keys. See below. |
| `custom_properties_namespace` | Advanced USD ▸ General ▸ Namespace, shown only with Custom Properties on | both | Ignored when `export_custom_properties` is off. |
| `author_blender_name` | Advanced USD ▸ General ▸ Blender Names, grayed out with Custom Properties off | both | Ignored when `export_custom_properties` is off. |
| `allow_unicode` | Advanced USD ▸ General ▸ Allow Unicode | both | Also controls how USD Stage for Blender renames invalid prims. See below. |
| `evaluation_mode` | Advanced USD ▸ General ▸ Use Settings for | both | Also decides which modifiers USD Stage for Blender treats as enabled. See below. |
| `use_instancing` | Advanced USD ▸ General ▸ Instancing | both | In a bake, instances that share a material and mesh share one baked material, except with `bake_mode=LIT_IBL`. |
| `triangulate_meshes` | Advanced USD ▸ Geometry ▸ Triangulate Meshes | both | Shows `quad_method` and `ngon_method` in the panel. |
| `quad_method` | Advanced USD ▸ Geometry ▸ Quad Method | both | No effect unless `triangulate_meshes` is on. |
| `ngon_method` | Advanced USD ▸ Geometry ▸ N-gon Method | both | No effect unless `triangulate_meshes` is on. |
| `export_subdivision` | Advanced USD ▸ Geometry ▸ Subdivision | both | Passed to Blender's USD exporter unchanged. |
| `export_armatures` | Advanced USD ▸ Rigging ▸ Armatures | both | Off drops armature objects, so a selected skinned mesh no longer brings its armature along. |
| `only_deform_bones` | Advanced USD ▸ Rigging ▸ Only Deform Bones | both | Meaningful only with `export_armatures` on. The panel does not gray it out. |
| `export_shapekeys` | Advanced USD ▸ Rigging ▸ Shape Keys | both | Also gates shape-key animation. |
| `export_texture_settings_enabled` | Not drawn: on the direct route, choosing anything but Keep Original or Original in Textures ▸ Maximum Resolution or Image Format turns it on | both | Gate for the four keys below. See [Texture settings](#texture-settings). |
| `bake_resolution` | Textures ▸ Maximum Resolution (direct, through `ui_source_texture_resolution`) or Bake Resolution (bake) | both | Ignored unless the gate is on. |
| `bake_resolution_custom` | Textures ▸ Custom Resolution, shown only for Custom | both | Ignored unless the gate is on and `bake_resolution=CUSTOM`. |
| `bake_image_format` | Textures ▸ Image Format (direct through `ui_source_texture_format`, and bake) | both | Ignored unless the gate is on. |
| `bake_margin` | Textures ▸ Bake Margin, bake route only | bake | Ignored unless the gate is on; the bake then uses 8. |
| `clamp_specular_tint` | Material Settings ▸ Clamp Overbright Specular Tint, PBR ▸ Translate only | direct | See below. |
| `bake_mode` | None; derived from Profile | bake | The panel overwrites it on every bake export. See [BAKING.md](BAKING.md#1-when-baking-happens-at-all). |
| `bake_ibl_source` | Material Settings ▸ Lighting Source, Unlit ▸ Lighting & Shadows | bake | Read only when `bake_mode=LIT_IBL`. `SCENE_WORLD` uses the scene's World as authored. |
| `bake_ibl_filepath` | Material Settings ▸ HDRI File | bake | Needs `bake_mode=LIT_IBL` and `bake_ibl_source=HDRI_FILE`; then an empty or missing file fails the bake. See below. |
| `bake_ibl_strength` | Material Settings ▸ Lighting Strength | bake | Needs `LIT_IBL` and `HDRI_FILE`. |
| `bake_ibl_rotation` | Material Settings ▸ Lighting Rotation | bake | Needs `LIT_IBL` and `HDRI_FILE`. Stored in radians; the panel shows degrees. |
| `bake_isolate_meshes_lit` | Material Settings ▸ Isolate Meshes for Shadows | bake | Read only when `bake_mode=LIT_IBL`. |
| `bake_base_color` | Material Settings ▸ Advanced ▸ Bake Base Color | bake | CLI flag: `--no-base-color`. |
| `bake_opacity` | Material Settings ▸ Advanced ▸ Bake Opacity | bake | CLI flag: `--no-opacity`. |
| `bake_step_timeout_seconds` | Material Settings ▸ Advanced ▸ Step Timeout (sec) | bake | Per-step budget for the bake worker; `0` means no limit. CLI flag: `--step-timeout`. |
| `bake_roughness_mode` | Material Settings ▸ Roughness, PBR ▸ Bake only | bake | Read only when `bake_mode=LIT_ALBEDO`, the only mode that bakes roughness. |
| `diagnostics_enabled` | Diagnostics ▸ Keep Success Diagnostics | both | Controls only successful runs; failures always write a sidecar. See [CLI.md](CLI.md#diagnostics-files). |

### Settings that interact

**`export_custom_properties` is a master switch.** When it is off, the exporter sends an empty `custom_properties_namespace` and turns `author_blender_name` off, whatever their stored values. The panel hides Namespace and grays out Blender Names to match.

**`selected_objects_only` changes more than Blender's own flag.**

- In a direct export, material validation checks only materials on the selected objects and their dependencies, not every material in the scene.
- An export or bake with nothing selected fails with `NO_EXPORTABLE_OBJECTS` instead of exporting the whole scene.
- A selected skinned mesh brings its deforming armature along, when `export_armatures` is on.
- A bake re-applies your selection after baking, so the bake's own selection changes cannot widen the export.

**`allow_unicode` also applies after Blender's export.** USD Stage for Blender renames any prim whose name is not a valid identifier. With `allow_unicode` on, non-ASCII letters and digits survive and other characters become `_`. With it off, every non-ASCII character becomes `_`. Reality Composer Pro imports non-ASCII names intact, Chinese included, although its hierarchy displays Chinese characters as `¿`.

**`author_animation_library` runs only after an animated export.** It needs `export_animation` and at least one recorded animation segment. When it is off, USD Stage for Blender removes any existing AnimationLibrary from the stage. See [the experimental RCP clip library](EXPORT_PIPELINE.md#the-experimental-rcp-clip-library).

**`evaluation_mode` reaches past Blender's exporter.** `RENDER` uses each modifier's render visibility and `VIEWPORT` its viewport visibility. USD Stage for Blender applies the same choice when it decides:

- which Armature modifiers count when a selected skinned mesh brings its armature along
- which modifier inputs the bake's asset preflight counts as dependencies

**`clamp_specular_tint` is an export-only repair.** When it is on, one case changes: an unlinked, achromatic Principled Specular Tint above 1 exports as `[1, 1, 1]`. The Blender node and the `.blend` are not changed. Colored, linked, negative, and non-finite values still fail. The same setting relaxes `validate` and the Shader Editor's **RealityKit Compatibility** panel, so they agree with the export. Bake exports do not validate source node graphs, and the panel shows the setting only for PBR ▸ Translate. A support bundle records any value other than `false` as a deviation from the `REALITYKIT_OS27` profile.

**`bake_ibl_filepath` resolves relative paths two ways.** A path that starts with `//` is relative to the saved `.blend`, and fails if the `.blend` was never saved. Any other relative path is relative to the current working directory. The panel's background job resolves the path to an absolute one before it copies the scene.

## Texture settings

This section is the reference for how `bake_resolution`, `bake_resolution_custom`, `bake_image_format`, and `bake_margin` resolve. [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#the-texture-pipeline) covers where staged textures go and how they are named.

### The gate

All four keys are ignored unless `export_texture_settings_enabled` is on. How it gets turned on depends on the front end:

| Front end | Gate |
|---|---|
| Panel, PBR ▸ Translate | Follows the two fields: **Maximum Resolution** and **Image Format** show Keep Original and Original while it is off, and choosing anything else turns it on. |
| Panel, any bake profile | Forced on for the job. The toggle is not drawn, and the fields are always live. |
| `bake-export` | Turned on for that run by `--resolution`, `--image-format`, or `--margin`. Otherwise the scene value applies. |
| `export` | Scene value only. Pass `export_texture_settings_enabled=true` to turn it on for one run. |

### Direct export

A direct export copies the image files your materials use. With the gate off, or with both `bake_resolution` and `bake_image_format` set to `ORIGINAL`, textures keep their size and encoding where RealityKit accepts it:

- **PNG, JPEG, and OpenEXR** are copied byte for byte.
- **AVIF, TIFF, TGA, BMP, WebP, and other formats Blender can read** are converted to PNG at their original size. If Blender cannot convert one, the export fails.
- **Radiance `.hdr`** fails the export. Converting it to PNG would destroy its float range. Convert it to OpenEXR first.

With the gate on:

- **`bake_resolution`** is a maximum. A texture whose longest side is larger is scaled down, keeping its aspect ratio. Smaller textures are left alone. `ORIGINAL` never resizes. `CUSTOM` uses `bake_resolution_custom`.
- **`bake_image_format` `PNG`** writes every texture as PNG.
- **`bake_image_format` `ORIGINAL`** keeps PNG and JPEG in their own encoding, resized if needed. Every other format becomes PNG.
- **`bake_image_format` `AVIF`** writes AVIF. If the AVIF encode fails, USD Stage for Blender writes PNG instead and adds a warning.
- **OpenEXR is never resized or re-encoded.** The override is skipped for it with a warning.
- **A premultiplied-alpha base color texture refuses AVIF.** Blender cannot encode it without breaking the alpha, so the export fails and asks for PNG.

### Bake export

Newly baked textures resolve differently:

| Key | Gate off | Gate on |
|---|---|---|
| `bake_resolution` | Each material bakes at the size of the largest image feeding its Base Color, Roughness, or Alpha, with a 512 px minimum. A material with no such image bakes at 2048. | `ORIGINAL` behaves like gate off. A number or `CUSTOM` bakes at that size. |
| `bake_image_format` | `PNG` | `PNG` or `AVIF`. `ORIGINAL` has no source encoding to keep, so it writes PNG with a warning. |
| `bake_margin` | 8 px | The configured value. |

`bake_mode=LIT_IBL` never sizes from source images. Lighting and shadow detail has nothing to do with the albedo's size, so a `LIT_IBL` bake that would size from source bakes at 2048 instead.

Textures that a bake copies rather than bakes follow the direct-export rules above.

### AVIF is opt-in

`bake_image_format` defaults to `PNG`. Reality Composer Pro 3 refuses AVIF textures on import, both beside a `.usdc` and inside a `.usdz`. Choosing AVIF adds a warning to the export. Use it only for packages that RealityKit loads at runtime. If the running Blender build cannot write the chosen format, the export falls back to PNG with a warning. See [texture formats](APPLE_PLATFORM_CONTRACT.md#texture-formats).

## Panel map

```text
3D Viewport ▸ Sidebar ▸ "USD Stage"
└─ USD Stage Export                                    (USDSTAGE_PT_export_panel)
   ├─ [Background Job card, only while a job exists]
   ├─ Export box: filepath · export_format · selected_objects_only · export_animation
   │              [· author_animation_library] · Profile (ui_material_type) · [Export]
   ├─ Material Settings
   │  ├─ PBR   ▸ Processing (ui_pbr_processing)
   │  │  ├─ Translate ▸ clamp_specular_tint
   │  │  └─ Bake      ▸ bake_roughness_mode · Advanced ▸ bake_base_color · bake_opacity
   │  │                                     · bake_step_timeout_seconds
   │  └─ Unlit ▸ Appearance (ui_unlit_appearance)
   │     ├─ Material Color Only ▸ Advanced ▸ (as above)
   │     └─ Lighting & Shadows  ▸ bake_ibl_source [· bake_ibl_filepath · bake_ibl_strength
   │                               · bake_ibl_rotation] · bake_isolate_meshes_lit · Advanced ▸ (as above)
   ├─ Textures                               (collapsed)
   │  ├─ direct route ▸ ui_source_texture_resolution · ui_source_texture_format [· bake_resolution_custom]
   │  └─ bake route   ▸ bake_resolution · bake_image_format [· bake_resolution_custom] · bake_margin
   ├─ Advanced USD                           (collapsed)
   │  ├─ General  ▸ root_prim_name · export_custom_properties [· custom_properties_namespace] · author_blender_name
   │  │             · allow_unicode · evaluation_mode · use_instancing
   │  ├─ Geometry ▸ triangulate_meshes [· quad_method · ngon_method] · export_subdivision
   │  └─ Rigging  ▸ export_shapekeys · export_armatures · only_deform_bones
   └─ Diagnostics                            (collapsed)
      └─ diagnostics_enabled · [Show Diagnostics] [Create Support Bundle]

Shader Editor ▸ Sidebar ▸ "USD Stage"
├─ RealityKit Compatibility                  (USDSTAGE_PT_shader_validation)
│  reads clamp_specular_tint and the Profile
└─ RealityKit Authoring                      (USDSTAGE_PT_shader_authoring)
   Insert RK PBR Group · Insert RK Unlit Group
```

Brackets mark controls drawn only when their parent setting enables them. Settings are locked while a background bake job runs.

The Profile, Processing, and Appearance controls choose the pipeline and set `bake_mode`. See [BAKING.md](BAKING.md#2-the-bake-modes) for how each choice maps to a mode.

## Internal keys

The properties in `INTERNAL_KEYS` (`Plugin/api/commands/_settings_common.py`), namely the `ui_*` profile and texture controls, `force_unlit_materials`, and bookkeeping values, cannot be read or set from the CLI, so choose the pipeline with `export` or `bake-export` and the mode with `--bake-mode` instead.

## Add-on preferences

`usdzip_path` is the only preference you set. The two hidden preferences hold the persistence described in [Two independent stores](#two-independent-stores). See [CLI.md](CLI.md) for `preferences get` and `preferences set`.

**`usdzip_path`** points at an external `usdzip`. It is read only for `USDZ` output.

- When the path is an executable file, USD Stage for Blender packages with `usdzip --asset <layer> --checkCompliance`. A `usdchecker` in the same directory is then used for compliance validation.
- When it is empty or not executable, USD Stage for Blender uses its built-in packager. It then looks for `usdchecker` through `xcrun` on macOS, or on `PATH` elsewhere.

## Fixed export policy

Not settings, fixed in `_build_export_kwargs` (`Plugin/export/blender_usd_export.py`): Y-up orientation and meter units, meshes always on, and cameras, lights, curves, point clouds, volumes, and hair always off; see [the Apple spatial contract](EXPORT_PIPELINE.md#1-the-apple-spatial-contract).
