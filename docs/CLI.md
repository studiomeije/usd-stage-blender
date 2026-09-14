# USD Stage for Blender CLI reference

The command line runs the same exports, bakes and validation as the Blender
sidebar, from a terminal, a script or an AI agent. This page lists every
command, flag, exit code and error code.

How it runs:

- Every command starts its own `blender --background` process. Bake and
  export commands use factory startup, so other add-ons cannot affect them.
- A successful command prints JSON on stdout. Progress and failure summaries
  go to stderr.
- Nothing carries over between commands unless it was written to disk; see
  [`settings set --save`](#settings-set).

## Setup

Tell the CLI where Blender is, in your shell profile or with `--blender` on
each command:

```bash
export USDSTAGE_BLENDER=/Applications/Blender.app/Contents/MacOS/Blender
```

The CLI lives in the installed extension folder, which Blender names after the
manifest id, `usd_stage`:

| System | Extension folder |
|--------|------------------|
| macOS | `~/Library/Application Support/Blender/<version>/extensions/<repository>/usd_stage/` |
| Linux | `~/.config/blender/<version>/extensions/<repository>/usd_stage/` |
| Windows | `%APPDATA%\Blender Foundation\Blender\<version>\extensions\<repository>\usd_stage\` |

`<repository>` is usually `user_default`. In a repository checkout, the same
folder is `Plugin/`. Run the CLI by pointing Python at that folder, or add an
alias:

```bash
alias usdstage="python3 /path/to/usd_stage"
usdstage version
```

The `usd-stage-setup` skill walks through finding these paths.

## Arguments

### Global options

| Flag | Default | Description |
|------|---------|-------------|
| `--blender <path>` | `$USDSTAGE_BLENDER`, else `blender` | The Blender executable |
| `--json` | off | Also print failures as a JSON envelope on stdout; implies `--quiet` |
| `--verbose` | off | Forward Blender's stderr to stderr, unredacted |
| `--quiet` | off | Hide progress messages; failure messages still print |
| `--timeout <sec>` | no limit for `export`, `bake-export` and `support-bundle`; 600 for the rest | Limit on the whole Blender process; `0` removes it |

### Order

Global options can go before or after the subcommand, and a command's flags,
`.blend` path and `key=value` overrides can come in any order. These are the
same command:

```bash
usdstage --json export scene.blend -o out.usda export-animation=true
usdstage export -o out.usda export-animation=true scene.blend --json
```

### Setting overrides

`export` and `bake-export` take any key from [`settings list`](#settings-list)
as a `key=value` token. An override applies to that run only and never changes
the `.blend`.

| Setting type | Accepted values |
|--------------|-----------------|
| `BOOLEAN` | `true`, `1`, `yes` or `false`, `0`, `no`, in any case. `on` and `off` are refused |
| `ENUM` | One of the `values` from `settings list`, exactly as spelled |
| `INT`, `FLOAT` | A number |
| `STRING` | The text as given; quote it if it contains spaces |

Hyphens and underscores in keys are interchangeable (`bake-resolution` is
`bake_resolution`); values are never changed. A token without `=` fails with
`INVALID_OVERRIDE` before Blender starts. An unknown key fails with
`INVALID_SETTING_OVERRIDE` and a refused value with `INVALID_SETTING_VALUE`;
with `--json`, `error.details` names the key and the allowed values:

```bash
usdstage --json export scene.blend -o out.usda bake-resolution=true
```

```
error.code: "INVALID_SETTING_VALUE"
error.details[0].reason: "Invalid value 'true' for 'bake_resolution'.
  Allowed: ['1024', '2048', '4096', '512', 'CUSTOM', 'ORIGINAL']"
```

### Output format and path

For `export` and `bake-export`:

- A `.usda`, `.usdc` or `.usdz` extension on `-o` chooses the format.
- Otherwise `--format` chooses it, else the scene's `export_format` (USDZ by
  default), and that format's extension replaces the last one in the name:
  `-o /output/my.scene.v2` writes `/output/my.scene.usdz`.
- A `--format` that disagrees with the `-o` extension fails with
  `INVALID_ARGUMENTS` before Blender starts.

The result's `export_path` is the file that was written.

## Commands

### `version`

```bash
usdstage version
```

```json
{"plugin": "1.0.0", "blender": "5.2.0", "python": "3.13.13"}
```

`version` does not load the add-on, so it is the quickest check that
`--blender` points at a working Blender.

### `info`

```bash
usdstage info scene.blend
```

```json
{
  "file": "/path/to/scene.blend",
  "scene": "Scene",
  "frame_range": [1, 250],
  "fps": 24,
  "unit_system": "METRIC",
  "unit_scale": 1.0,
  "object_count": 4,
  "material_count": 1
}
```

`material_count` counts materials assigned to scene objects.

### `list-objects`

```bash
usdstage list-objects scene.blend [--type MESH] [--type LIGHT] [--selected]
```

| Flag | Description |
|------|-------------|
| `--type <TYPE>` | Only objects of this Blender `Object.type`, in any case. Repeatable |
| `--selected` | Only selected objects |

Blender 5.2 types are `MESH`, `CURVE`, `SURFACE`, `META`, `FONT`, `CURVES`,
`POINTCLOUD`, `VOLUME`, `GREASEPENCIL`, `ARMATURE`, `LATTICE`, `EMPTY`,
`LIGHT`, `LIGHT_PROBE`, `CAMERA` and `SPEAKER`. An unknown type matches
nothing and returns `[]`.

```json
[
  {"name": "Cube", "type": "MESH", "visible": true, "selected": false, "materials": ["Paint"], "vertices": 8},
  {"name": "Sun", "type": "LIGHT", "visible": true, "selected": false, "materials": [], "light_type": "SUN"}
]
```

`vertices` appears only on meshes and `light_type` only on lights.

### `list-materials`

```bash
usdstage list-materials scene.blend [--unused]
```

`--unused` adds materials no scene object uses. Blender drops materials with
no users when it saves, so those appear only if they have a fake user.

```json
[{"name": "Paint", "users": 1, "node_count": 2}]
```

### `validate`

Checks materials against the same rules as a direct export.

```bash
usdstage validate scene.blend [--material NAME] [--only-errors] [--clamp-specular-tint]
```

| Flag | Description |
|------|-------------|
| `--material <name>` | Validate one material; otherwise the materials of every object the export writes, and of the collections they instance |
| `--only-errors` | Leave out the `warnings` lists and `warning_count` |
| `--clamp-specular-tint` | Validate as if **Clamp Overbright Specular Tint** were on |

```json
{
  "ok": false,
  "error_count": 1,
  "clamp_specular_tint": false,
  "materials": [
    {
      "name": "Mixed",
      "ok": false,
      "errors": [
        {"node_name": "Toon BSDF", "node_type": "BSDF_TOON", "message": "Node is not supported by RealityKit export."}
      ],
      "warnings": []
    }
  ],
  "warning_count": 0
}
```

A material with errors is still a normal result: the command exits 1 and
prints this report with `ok: false`. A run that could not execute exits 1 with
an error instead, for example `MATERIAL_NOT_FOUND` for `--material` naming a
material that is not in the file. Test for the `error` key to tell them apart:

```bash
usdstage --json validate scene.blend > report.json
if jq -e 'has("error")' report.json > /dev/null; then
  echo "validate failed: $(jq -r .error.code report.json)"
elif jq -e '.ok' report.json > /dev/null; then
  echo "all materials translate"
fi
```

Which surface each material exports to, and what is refused, is in
[MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md). Baking profiles do not
need materials to validate.

### `settings get`

```bash
usdstage settings get scene.blend [--group NAME | --keys KEY [KEY ...]]
```

| Flag | Description |
|------|-------------|
| `--group <name>` | One of the [groups](#setting-groups), or `all` (the default) |
| `--keys <key> ...` | Only these keys; overrides `--group` |

Values come back as JSON booleans, numbers and strings. An unknown key fails
with `UNKNOWN_SETTING_KEY`, and an unknown group with `UNKNOWN_SETTING_GROUP`.

```bash
usdstage settings get scene.blend --keys export_format bake_resolution
```

```json
{"export_format": "USDZ", "bake_resolution": "ORIGINAL"}
```

### `settings set`

```bash
usdstage settings set scene.blend key=value [key=value ...] (--save | --dry-run)
```

| Flag | Description |
|------|-------------|
| `--save` | Save the `.blend` after applying the values |
| `--dry-run` | Check the keys and values without applying them; wins over `--save` |

**Without `--save`, nothing reaches the file.** The values are applied to the
background Blender process and lost when it exits. The command still exits 0
and lists the keys under `updated`, so check `saved`:

```json
{
  "updated": ["export_format"],
  "saved": false,
  "warnings": ["Settings were applied to a temporary Blender session and NOT written to the .blend. Re-run with --save to persist them."]
}
```

With `--save`, the result is `{"updated": [...], "saved": true}`. With
`--dry-run`, it is `{"valid": true, "would_update": [...]}`. An unknown key or
refused value fails with exit 1 in every mode, so a dry run that exits 0 has
passed. Values follow the [override rules](#setting-overrides).

Error codes: `INVALID_SETTING_FORMAT` (a token without `=`),
`INVALID_SETTING_OVERRIDE`, `INVALID_SETTING_VALUE`, and
`SETTINGS_SAVE_FAILED` (the save was refused, or the `.blend` has no path).

### `settings list`

```bash
usdstage settings list
```

Prints every export setting as read from the installed add-on, so it always
matches the version you run. It needs no `.blend`.

```json
[
  {
    "key": "export_format",
    "type": "ENUM",
    "description": "Export format and file extension",
    "group": "general",
    "values": ["USDA", "USDC", "USDZ"],
    "default": "USDZ"
  }
]
```

`type` is `BOOLEAN`, `INT`, `FLOAT`, `STRING` or `ENUM`, and `values` appears
only for `ENUM`. What each setting does is in [SETTINGS.md](SETTINGS.md).

### `export`

Translates materials directly and exports.

```bash
usdstage export scene.blend -o OUTPUT [--format FORMAT] [--selected-only] [--diagnostics | --no-diagnostics] [key=value ...]
```

| Flag | Description |
|------|-------------|
| `-o, --output <path>` | Output file; see [Output format and path](#output-format-and-path) |
| `--format <FORMAT>` | `USDA`, `USDC` or `USDZ` |
| `--selected-only` | Export selected objects only |
| `--diagnostics` | Keep `<output>.diagnostics.json` after a successful export |
| `--no-diagnostics` | Don't keep it, even when the scene's setting is on; wins over `--diagnostics` |

Every export is Y-up, -Z forward and in meters, with relative dependency
paths.

```bash
usdstage export scene.blend -o /output/scene.usdz export-animation=true
```

```json
{
  "ok": true,
  "export_path": "/output/scene.usdz",
  "format": "USDZ",
  "duration_seconds": 0.3,
  "warnings": [],
  "diagnostics_path": null,
  "support_bundle_hint": "usdstage support-bundle scene.blend --export-path /output/scene.usdz"
}
```

`warnings` lists every warning the export raised; they also print on stderr. `diagnostics_path` is `null` unless a diagnostics file
was kept.

### `bake-export`

Bakes materials to textures, then exports. The sidebar's Export button runs
the same pipeline when the Profile bakes.

```bash
usdstage bake-export scene.blend -o OUTPUT [--bake-mode MODE] [options] [key=value ...]
```

| `--bake-mode` | Sidebar Profile |
|---------------|-----------------|
| `LIT_ALBEDO` | RealityKit PBR ▸ Bake Materials |
| `UNLIT_ALBEDO` | RealityKit Unlit ▸ Material Color Only |
| `LIT_IBL` | RealityKit Unlit ▸ Lighting & Shadows, the default for a new scene |

What each mode captures is in [BAKING.md](BAKING.md#2-the-bake-modes). Baking
renders the node graph, so `bake-export` does not validate material graphs and
never fails with `UNSUPPORTED_MATERIAL_NODES`.

Options, in addition to `-o`, `--format`, `--selected-only`, `--diagnostics`
and `--no-diagnostics` from `export`:

| Flag | Default | Description |
|------|---------|-------------|
| `--bake-mode <MODE>` | the scene's `bake_mode` | See above |
| `--resolution <RES>` | `ORIGINAL` | `ORIGINAL`, `512`, `1024`, `2048`, `4096` or another whole number. `ORIGINAL` sizes each material from its own textures, at least 512 |
| `--image-format <FMT>` | `PNG` | `ORIGINAL`, `PNG` or `AVIF`. Reality Composer Pro 3 does not import AVIF |
| `--margin <px>` | `8` | Bake padding |
| `--roughness-mode <MODE>` | `TEXTURE` | `LIT_ALBEDO` only: `TEXTURE` bakes a roughness map, `AVERAGE` one constant |
| `--no-base-color` | off | Skip the base colour bake |
| `--no-opacity` | off | Skip the opacity bake |
| `--ibl-source <SRC>` | `SCENE_WORLD` | `LIT_IBL` only: `SCENE_WORLD` or `HDRI_FILE` |
| `--ibl-filepath <path>` | none | The HDRI, required with `HDRI_FILE` |
| `--ibl-strength <float>` | `1.0` | HDRI strength |
| `--ibl-rotation <radians>` | `0.0` | HDRI rotation around Z |
| `--isolate-meshes` | off | `LIT_IBL` only: bake each mesh without the others' shadows |
| `--step-timeout <sec>` | `0` (none) | Limit on each bake, export and packaging step |

`--resolution`, `--image-format` and `--margin` turn the scene's texture
overrides on for the run.

Before baking, the command checks every file the export depends on. Missing
unpacked images fail with `MISSING_EXTERNAL_TEXTURES`, and other missing files
(linked libraries, caches, the HDRI) with `MISSING_EXTERNAL_ASSETS`.

`--step-timeout` and `--timeout` fail differently. A step timeout stops the
Blender worker, writes diagnostics and fails with `BAKE_STEP_TIMEOUT`, naming
the step in `error.stage`; `context.returncode` is the worker's 124. A
`--timeout` stops Blender outright and fails with `BLENDER_TIMEOUT`, with no
diagnostics. Both exit 1.

```bash
usdstage --timeout 900 bake-export scene.blend -o /output/scene.usdz \
  --bake-mode LIT_IBL --ibl-source HDRI_FILE --ibl-filepath /hdris/studio.exr \
  --resolution 2048 --step-timeout 300
```

The result has the same fields as `export`, plus `bake_stats`:

```json
{
  "ok": true,
  "export_path": "/output/scene.usdz",
  "format": "USDZ",
  "duration_seconds": 45.3,
  "bake_stats": {"objects_baked": 8, "resolution": 2048, "image_format": "PNG"},
  "warnings": [],
  "diagnostics_path": null,
  "support_bundle_hint": "usdstage support-bundle scene.blend --export-path /output/scene.usdz"
}
```

### `support-bundle`

Creates a ZIP with what is needed to diagnose a failed export or bake.

```bash
usdstage support-bundle [scene.blend] [options]
```

| Flag | Description |
|------|-------------|
| `--export-path <path>` | The export that failed |
| `--diagnostics-path <path>` | A diagnostics file; defaults to the one beside `--export-path` |
| `--job-dir <path>` | A sidebar background job folder, `<export folder>/.usdstage_jobs/<job>/` |
| `-o, --bundle-output <path>` | Where to write the ZIP |
| `--include-output` | Add the exported files |
| `--include-blend` | Add the `.blend` |
| `--full-log` | Add whole logs instead of their last 2000 lines |
| `--no-redact` | Keep absolute paths |

Every input is optional. The bundle holds the diagnostics, logs, environment
details and `diagnostics/assets.json`; direct exports also get
`diagnostics/validate.json`. Absolute paths are redacted unless you pass
`--no-redact`. Without `-o`, the ZIP is named
`USDStage-support-<blend name>-<YYYYMMDD-HHMMSS>.zip` and written beside the
export, else beside the `.blend`, else in the current folder.

```bash
usdstage support-bundle scene.blend \
  --export-path /output/scene.usdz \
  --job-dir /output/.usdstage_jobs/bake_export_20260504_143012_abcd
```

```json
{
  "support_bundle_path": "/output/USDStage-support-scene-20260504-143512.zip",
  "file_count": 7,
  "redacted": true,
  "included_output": false,
  "included_blend": false
}
```

### `preferences get`

```bash
usdstage preferences get
```

```json
{"usdzip_path": ""}
```

### `preferences set`

```bash
usdstage preferences set usdzip_path=/opt/usd/bin/usdzip
usdstage preferences set usdzip_path=
```

| Key | Description |
|-----|-------------|
| `usdzip_path` | A `usdzip` executable to package USDZ files; empty uses the built-in packager |

**`preferences set` saves Blender's preferences immediately**, for every file,
with no `--save` or `--dry-run`. An unknown key fails with
`UNKNOWN_PREFERENCE_KEY` before anything is saved, and a refused value with
`INVALID_PREFERENCE_VALUE`.

## Settings on the command line

### Setting groups

| Group | Settings |
|-------|----------|
| `general` | `filepath`, `export_format`, `root_prim_name`, `export_animation`, `author_animation_library`, `selected_objects_only`, `export_custom_properties`, `custom_properties_namespace`, `author_blender_name`, `allow_unicode`, `evaluation_mode`, `use_instancing` |
| `geometry` | `triangulate_meshes`, `quad_method`, `ngon_method`, `export_subdivision` |
| `rigging` | `export_armatures`, `only_deform_bones`, `export_shapekeys` |
| `texture` | `export_texture_settings_enabled`, `bake_resolution`, `bake_resolution_custom`, `bake_image_format`, `bake_margin` |
| `materials` | `clamp_specular_tint` |
| `bake` | `bake_mode`, `bake_ibl_source`, `bake_ibl_filepath`, `bake_ibl_strength`, `bake_ibl_rotation`, `bake_isolate_meshes_lit`, `bake_step_timeout_seconds`, `bake_base_color`, `bake_opacity`, `bake_roughness_mode` |
| `diagnostics` | `diagnostics_enabled` |

### Flags and the settings they set

| Flag | Setting |
|------|---------|
| `--format` | `export_format` |
| `--selected-only` | `selected_objects_only` |
| `--diagnostics`, `--no-diagnostics` | `diagnostics_enabled` |
| `--bake-mode` | `bake_mode` |
| `--resolution` | `bake_resolution`, `bake_resolution_custom` |
| `--image-format` | `bake_image_format` |
| `--margin` | `bake_margin` |
| `--ibl-source`, `--ibl-filepath`, `--ibl-strength`, `--ibl-rotation` | `bake_ibl_*` |
| `--isolate-meshes` | `bake_isolate_meshes_lit` |
| `--no-base-color`, `--no-opacity` | `bake_base_color`, `bake_opacity` |
| `--roughness-mode` | `bake_roughness_mode` |
| `--step-timeout` | `bake_step_timeout_seconds` |
| `--clamp-specular-tint` (`validate`) | `clamp_specular_tint` |

### Diagnostics files

A failed `export` or `bake-export` always writes `<output>.diagnostics.json`,
including a failure before anything was exported, such as a bad override.
`diagnostics_enabled`, `--diagnostics` and `--no-diagnostics` only decide
whether a successful run keeps one.

## Output

stdout carries only JSON: the result on success and, with `--json`, the error
envelope on failure. stderr carries everything else.

| Flags | stderr on success | stdout on failure | stderr on failure |
|-------|-------------------|-------------------|-------------------|
| none | progress, warnings | nothing | progress, error summary |
| `--quiet` | warnings | nothing | error summary |
| `--json` | warnings | error envelope | nothing |

`--verbose` adds Blender's stderr to any of these, even with `--json`. That
text is not redacted, and a clean run usually adds nothing, because Blender's
startup messages go to its stdout.

The error summary without `--json`:

```
Error: Unsupported nodes in material 'Paint'.
Diagnostics: /output/scene.diagnostics.json
Support bundle: usdstage support-bundle scene.blend --export-path /output/scene.usda --diagnostics-path /output/scene.diagnostics.json
```

The error envelope with `--json`:

```json
{
  "ok": false,
  "schema_version": "1.0",
  "command": "export",
  "error": {
    "code": "POSTPROCESS_FAILED",
    "type": "CommandError",
    "message": "Postprocess failed",
    "stage": "postprocess_usd"
  },
  "context": {
    "blend_file": "scene.blend",
    "blender_path": "/Applications/Blender.app/Contents/MacOS/Blender",
    "returncode": 1
  },
  "artifacts": {
    "diagnostics_path": "/output/scene.diagnostics.json",
    "support_bundle_hint": "usdstage support-bundle scene.blend --export-path /output/scene.usdz --diagnostics-path /output/scene.diagnostics.json"
  },
  "process_output": {"stdout_tail": "...", "stderr_tail": "..."}
}
```

| Field | Notes |
|-------|-------|
| `command` | The subcommand; `null` when the command line named none |
| `error.code` | Stable; branch on it rather than on `error.message` |
| `error.stage` | The pipeline step that failed, when known |
| `error.details` | Which key, value or file caused the error, when known |
| `error.traceback` | Only for an unexpected internal error, with the home folder redacted |
| `context`, `artifacts` | `{}` when the CLI failed before Blender started |
| `process_output` | The last 500 characters of Blender's output, with the home folder redacted |

A successful result is printed as it is, with no envelope.

### Reporting a problem

Create a support bundle and attach it: `usdstage support-bundle` or
**USD Stage ▸ Diagnostics ▸ Create Support Bundle** in the sidebar. It
redacts absolute paths. Don't attach `--verbose` output to a public issue,
because it is not redacted.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success, including `--help` |
| 1 | The command failed, `validate` found errors, an argument or override was refused, or a timeout expired |
| 2 | Blender was not found or did not start (`BLENDER_NOT_FOUND`, `BLENDER_START_FAILED`) |
| 3 | The add-on did not load in Blender (`ADDON_LOAD_FAILED`) |
| 130 | Interrupted with Ctrl-C (`INTERRUPTED`) |

Bad arguments exit 1, not argparse's usual 2. A step timeout exits 1 as well;
the worker's 124 appears only in `context.returncode`.

## Error codes

Raised by the CLI before Blender starts:

| Code | Meaning |
|------|---------|
| `INVALID_ARGUMENTS` | The command line was refused |
| `INVALID_OVERRIDE` | An `export` or `bake-export` override has no `=` |
| `INVALID_SETTING_FORMAT` | A `settings set` value has no `=` |
| `INVALID_PREFERENCE_FORMAT` | A `preferences set` value has no `=` |
| `BLENDER_NOT_FOUND` | The Blender executable does not exist |
| `BLENDER_START_FAILED` | The executable could not be run |
| `BLENDER_TIMEOUT` | `--timeout` expired |
| `BLENDER_PROCESS_FAILED` | Blender returned no readable result, or exited with an error after reporting success |
| `BLENDER_BRIDGE_FAILED` | Another failure talking to Blender |
| `CLI_RUNTIME_ERROR` | An unexpected error in the CLI |
| `INTERRUPTED` | Ctrl-C |

Raised inside Blender:

| Code | Commands |
|------|----------|
| `ADDON_LOAD_FAILED` | all except `version` |
| `MATERIAL_NOT_FOUND` | `validate --material` |
| `UNKNOWN_SETTING_KEY`, `UNKNOWN_SETTING_GROUP` | `settings get` |
| `INVALID_SETTING_OVERRIDE`, `INVALID_SETTING_VALUE` | `settings set`, `export`, `bake-export` |
| `SETTINGS_SAVE_FAILED` | `settings set --save` |
| `UNKNOWN_PREFERENCE_KEY`, `INVALID_PREFERENCE_VALUE` | `preferences set` |
| `INVALID_EXPORT_SELECTION`, `UNSUPPORTED_MATERIAL_NODES`, `EXPORT_FAILED` | `export` |
| `NO_EXPORTABLE_OBJECTS`, `BLENDER_USD_EXPORT_FAILED`, `POSTPROCESS_FAILED` | `export`, `bake-export` |
| `MISSING_EXTERNAL_TEXTURES`, `MISSING_EXTERNAL_ASSETS`, `BAKE_STEP_TIMEOUT`, `BAKE_EXPORT_FAILED` | `bake-export` |

An unexpected internal error reports the Python exception name in capitals,
such as `VALUEERROR`, with a `traceback`. Treat it as a bug to report.

The sidebar's background jobs have their own codes, which appear in a job's
`status.json` and in support bundles but never from the CLI:
`INVALID_EXPORT_SETTINGS`, `ASSET_PREFLIGHT_FAILED`, `SCENE_SNAPSHOT_FAILED`,
`JOB_SETTINGS_WRITE_FAILED`, `BACKGROUND_RUNNER_MISSING` and
`BACKGROUND_LAUNCH_FAILED`.
