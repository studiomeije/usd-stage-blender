---
name: "usd-stage-cli"
description: "Use when a user asks to export a Blender scene to USDA/USDC/USDZ, bake textures for RealityKit, validate materials for RealityKit compatibility, or read and change USD Stage for Blender export, texture, and bake settings from the command line. Requires the `usdstage` CLI alias or the Plugin path (see the usd-stage-setup skill)."
---

# USD Stage for Blender CLI

`usdstage` runs `blender --background` with the USD Stage for Blender add-on.
Successful commands print JSON on stdout. The full reference is `docs/CLI.md`;
this skill lists the commands and the mistakes that are easy to make.

Setup (the alias and `USDSTAGE_BLENDER`) is in the `usd-stage-setup` skill.

## Commands

```bash
usdstage version                                  # quickest check that Blender starts
usdstage info scene.blend
usdstage list-objects scene.blend [--type MESH] [--selected]
usdstage list-materials scene.blend [--unused]
usdstage validate scene.blend [--material NAME] [--only-errors] [--clamp-specular-tint]
usdstage export scene.blend -o out.usdz [key=value ...]
usdstage bake-export scene.blend -o out.usdz [--bake-mode MODE] [--resolution N] [key=value ...]
usdstage settings list
usdstage settings get scene.blend [--group bake | --keys export_format bake_resolution]
usdstage settings set scene.blend key=value ... (--save | --dry-run)
usdstage preferences get
usdstage preferences set usdzip_path=/opt/usd/bin/usdzip
usdstage support-bundle scene.blend --export-path out.usdz [--diagnostics-path out.diagnostics.json]
```

`usdstage settings list` prints every setting key with its type, allowed
values and default, read from the installed add-on. Use it instead of guessing
key names.

## Traps

**The output format.** A `.usda`, `.usdc` or `.usdz` extension on `-o` picks
the format. Without one, `--format` picks it, else the scene's `export_format`
(USDZ by default), and that format's extension replaces the last one in the
name. A `--format` that disagrees with the `-o` extension fails with
`INVALID_ARGUMENTS`. Read `export_path` from the result for the file written.

**Overrides.** `key=value` tokens change settings for this run only and never
touch the `.blend`. Hyphens and underscores in keys are interchangeable;
values are exact and case-sensitive. Booleans accept only `true/1/yes` and
`false/0/no`; `on` fails with `INVALID_SETTING_VALUE`. Arguments can come in
any order, and global flags (`--json`, `--timeout`, `--blender`, `--verbose`,
`--quiet`) can go before or after the subcommand.

**`settings set` needs `--save`.** Without it the command exits 0 and lists
the keys under `updated`, but nothing reaches the file; check `saved`. Never
report a setting as changed from a run without `--save`. `--dry-run` wins over
`--save`, and an unknown key or bad value fails with exit 1 either way.

**`preferences set` saves immediately.** It writes the user's Blender
preferences for every file, with no `--save` or `--dry-run`. Confirm with the
user first.

**Validation failure is a result, not an error.** `validate` exits 1 with an
ordinary payload whose top-level `ok` is false when materials have errors. A
run that could not execute (for example `--material` naming a missing
material) exits 1 with an `error` object instead. Check for the `error` key.

**Baking does not validate node graphs.** `bake-export` renders materials to
textures, so it accepts graphs `validate` and `export` refuse. Bake modes:
`UNLIT_ALBEDO` (RealityKit Unlit ▸ Material Color Only), `LIT_ALBEDO`
(RealityKit PBR ▸ Bake Materials; `--roughness-mode TEXTURE|AVERAGE`),
`LIT_IBL` (RealityKit Unlit ▸ Lighting & Shadows, the default for a new
scene). Passing `--resolution`, `--image-format` or `--margin` turns texture
overrides on for the run; the resolution default is `ORIGINAL`.

**Missing files stop the export.** Unpacked images that are missing fail with
`MISSING_EXTERNAL_TEXTURES`; other missing dependencies fail with
`MISSING_EXTERNAL_ASSETS`. Relink or pack them. Radiance `.hdr` textures are
refused; convert them to OpenEXR. AVIF output is opt-in, because Reality
Composer Pro 3 does not import it.

**Two timeouts.** `--timeout` bounds the whole Blender process (no limit for
`export`, `bake-export` and `support-bundle`, 600 s otherwise) and fails with
`BLENDER_TIMEOUT`. `--step-timeout` on `bake-export` bounds each worker step
and fails with `BAKE_STEP_TIMEOUT` and a diagnostics file. Both exit 1.

**Clip library.** `author-animation-library=true` writes named clips that
Reality Composer Pro flattens on import. Leave it off unless the user asks.

## Output and errors

Pass `--json` for automation: failures then print an envelope on stdout with
`ok`, `schema_version`, `command`, `error` (`code`, `message`, `details`),
`context` and `artifacts`. Branch on `error.code`. Success payloads are
printed as they are, without an envelope. Without `--json`, a rejected value
prints only a summary; the key and allowed values are in `error.details`.

`--quiet` hides progress, not failures. `--verbose` forwards Blender's stderr
unredacted, even with `--json`; don't paste it into a public issue. For a
support report, create a support bundle, which redacts absolute paths.

| Exit | Meaning |
|------|---------|
| 0 | Success |
| 1 | Command failed, validation errors, bad arguments, or a timeout |
| 2 | Blender not found or did not start |
| 3 | The add-on did not load in Blender (`ADDON_LOAD_FAILED`) |
| 130 | Interrupted |

Bad arguments exit 1, not argparse's usual 2.

```bash
if usdstage validate scene.blend; then
  out=$(usdstage --json export scene.blend -o scene.usdz | jq -r '.export_path')
fi
```

## Export scope

Exports are Y-up, -Z forward and in meters, with relative texture paths.
Blender cameras, lights, World lighting, curves, point clouds, volumes and
hair curves are not exported; author cameras and lighting in the target app
and convert other geometry to meshes. A Principled Hair BSDF on mesh hair
cards exports as RealityKit's hair surface.
