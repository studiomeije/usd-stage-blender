# USD Stage for Blender Documentation

This page is the index of the USD Stage for Blender documentation. Use it to find the right page, or to trace an export that surprised you.

| Document | Read it when you want to know |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Which module owns what, and how an export moves through the code |
| [CLI.md](CLI.md) | Every command, flag, exit code, and the JSON envelope |
| [SETTINGS.md](SETTINGS.md) | What every toggle changes, and which ones override each other |
| [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md) | Which Blender features survive an export, and which are refused or dropped |
| [APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md) | Which USD and MaterialX features Reality Composer Pro and RealityKit accept |
| [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md) | How a Blender shader graph becomes a RealityKit MaterialX graph: the surface, color space, textures |
| [BAKING.md](BAKING.md) | When a bake happens, what each bake mode captures, and what scene state it overrides |
| [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md) | Geometry, units, animation, staging and publication, and USDZ packaging |

## If you are chasing a specific surprise

- **"My texture looks wrong."** [BAKING.md](BAKING.md) for baked output; [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#color-space) for color space and [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#the-texture-pipeline) for texture staging.
- **"My object is the wrong size."** [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#1-the-apple-spatial-contract).
- **"The export doesn't match my viewport."** [BAKING.md](BAKING.md#2-the-bake-modes); the view transform is not applied to baked textures.
- **"I set a setting and nothing happened."** [SETTINGS.md](SETTINGS.md), which records the settings that other settings override.
- **"Something is missing from the export and nothing told me."** [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md), which ends with the features that leave without a warning.

## Known issues

Open defects are tracked in the issues of the private `studiomeije/usd-stage-blender` repository. Search there before filing a bug; it may already be recorded. Without access to the repository, send the report and a support bundle to tom@studiomeije.com.
