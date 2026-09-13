<img src="docs/images/usd-stage-for-blender.png" alt="USD Stage" width="128">

# USD Stage for Blender

USD Stage for Blender is a Blender add-on that exports scenes to `.usda`,
`.usdc`, or `.usdz` and rewrites Blender materials into MaterialX graphs that
Reality Composer Pro and RealityKit open, edit, and render. A command line
runs the same exports from a terminal or an AI agent without opening
Blender's interface.

## Key features

- **Editable RealityKit materials**: Principled BSDF materials, and the shader
  and texture nodes listed in
  [docs/FEATURE_SUPPORT.md](docs/FEATURE_SUPPORT.md), become RealityKit PBR
  Surface 2 graphs that match Cycles.
- **Strict validation**: a node the export cannot translate stops it with an
  error naming the node and the fix, instead of degrading silently.
- **Baking**: one Export button with a Profile choice bakes what cannot be
  translated, in a second Blender process that keeps the interface responsive
  ([docs/BAKING.md](docs/BAKING.md)).
- **Animation**: transform animation, skinned meshes, shape keys, vertex
  displacement and drivers over `frame` export
  ([docs/EXPORT_PIPELINE.md](docs/EXPORT_PIPELINE.md)).
- **Portable output**: every export is Y-up, -Z forward and in meters, with
  textures staged beside it under relative paths.
- **Command line and agent skills**: export, bake, validate and manage
  settings from a terminal ([docs/CLI.md](docs/CLI.md)), or let an AI agent do
  it through the bundled skills.
- **Diagnostics**: failed exports always write a report, and a redacted
  support bundle collects everything needed to report a problem.

## Requirements

- Blender 5.2 LTS.
- `python3`, for the command line.
- Reality Composer Pro 3, or RealityKit on visionOS, iOS or macOS 27, to open
  the exports.

## Install

USD Stage for Blender is distributed from the private
`studiomeije/usd-stage-blender` repository. Ask Tom Krikorian
(tom@studiomeije.com) for access.

1. Download `usd-stage-for-blender-<version>.zip` and its matching
   `usd-stage-for-blender-<version>.zip.sha256` from the repository's
   Releases page.
2. Verify the download from the folder that holds both files:

   ```bash
   shasum -a 256 -c usd-stage-for-blender-<version>.zip.sha256
   ```

3. In Blender, open **Edit ▸ Preferences ▸ Extensions ▸ Add-ons ▸ Install from Disk…**.
4. Select `usd-stage-for-blender-<version>.zip`.
5. Enable **USD Stage for Blender** in the add-ons list.

## Where to find it in Blender

- **3D Viewport ▸ Sidebar ▸ USD Stage**: the export panel with the Profile,
  the Export button, material, texture and USD settings, the background job
  monitor, and diagnostics.
- **Shader Editor ▸ Sidebar ▸ USD Stage ▸ RealityKit Compatibility**:
  check the active material against the chosen Profile and select the nodes
  that block translation.
- **Shader Editor ▸ Sidebar ▸ USD Stage ▸ RealityKit Authoring**: insert
  RealityKit PBR or Unlit node groups.
- **Shader Editor ▸ Add ▸ RealityKit Nodes**: insert RealityKit node groups
  from the bundled catalog.

## Choosing a Profile

| Goal | Profile |
|------|---------|
| Translate compatible materials directly | **RealityKit PBR ▸ Translate Materials** |
| Bake complex materials but keep dynamic RealityKit lighting | **RealityKit PBR ▸ Bake Materials** |
| Export material colour that ignores scene lighting | **RealityKit Unlit ▸ Material Color Only** |
| Preserve Blender lighting and shadows | **RealityKit Unlit ▸ Lighting & Shadows** |

What each bake captures is in [docs/BAKING.md](docs/BAKING.md#2-the-bake-modes).

## Command line

Every command runs `blender --background` and prints JSON to stdout on
success.

```bash
# Tell the command line where Blender is (add to ~/.zshrc or ~/.bashrc)
export USDSTAGE_BLENDER="/Applications/Blender.app/Contents/MacOS/Blender"

# Point an alias at the installed extension folder...
alias usdstage="python3 /path/to/extensions/user_default/usd_stage"
# ...or at a repository checkout
# alias usdstage="python3 /path/to/usd-stage-blender/Plugin"

usdstage version
usdstage validate scene.blend
usdstage export scene.blend -o output.usdz --format USDZ
usdstage bake-export scene.blend -o output.usdz --format USDZ --resolution 2048
usdstage settings get scene.blend --group bake
```

Every command, flag and exit code is in [docs/CLI.md](docs/CLI.md).

## AI agent skills

The repository ships two [Agent Skills](https://skills.sh) that let an AI
agent drive the command line:

| Skill | What it does |
|-------|--------------|
| `usd-stage-cli` | Exports scenes, bakes textures, validates materials and manages settings. |
| `usd-stage-setup` | Locates Blender and the extension, checks the connection and sets up the alias. |

Install them from a checkout of the repository by copying or symlinking the
two folders under `skills/` into your agent's skills folder:

```bash
ln -s "$PWD/skills/usd-stage-cli" ~/.claude/skills/usd-stage-cli
ln -s "$PWD/skills/usd-stage-setup" ~/.claude/skills/usd-stage-setup
```

## Troubleshooting

When something fails, create a support bundle: **USD Stage ▸ Diagnostics ▸
Create Support Bundle** in the sidebar, or

```bash
usdstage support-bundle scene.blend --export-path output.usdz
```

It collects the diagnostics, logs and environment details, with your home
folder redacted. What else to attach to a report is in
[docs/CLI.md](docs/CLI.md#reporting-a-problem).

## Documentation

- [docs/README.md](docs/README.md) indexes every documentation page.
- [CHANGELOG.md](CHANGELOG.md) lists what changed in each release, including
  what to redo when coming from BlenderToRCP.

## License

Copyright © 2026 Studio Meije. Maintained by Tom Krikorian
(tom@studiomeije.com).

USD Stage for Blender is licensed under the
[GNU General Public License, version 3 or later](LICENSE), as declared by
`SPDX:GPL-3.0-or-later` in the extension manifest.

The files under `THIRD_PARTY_LICENSES/` and
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) cover the Apple and
MaterialX material the extension redistributes. They preserve the upstream
MIT and Apache-2.0 notices and do not make the project dual-licensed.
