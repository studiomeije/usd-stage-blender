# Changelog

Notable changes to USD Stage for Blender, newest first. Dates are release dates.

Entries describe what changed for you — what now works, what stops the export,
and what you have to do differently. Internal refactors are not listed.

## 1.0.0 — 2026-09-14

The first release of USD Stage for Blender. It exports Blender scenes to
`.usda`, `.usdc` or `.usdz` for RealityKit and Reality Composer Pro. Requires
Blender 5.2 LTS.

- Every export follows one spatial contract: Y-up, -Z forward, meters at
  `metersPerUnit=1`, relative dependencies.
- Materials become MaterialX 1.39 graphs on RealityKit's PBR Surface 2, with
  the supported Blender nodes transcribed to match Cycles.
- Validation is strict: a node or value the export cannot translate stops it
  with an error naming the node and the fix, in the sidebar, in `usdstage
  validate` and in the Shader Editor's RealityKit Compatibility panel, which
  follows the chosen Profile.
- One Export button with a Profile choice translates materials or bakes them
  (RealityKit PBR ▸ Bake Materials, or RealityKit Unlit ▸ Material Color Only
  or Lighting & Shadows) in a second Blender process. A bake covers exactly
  the objects Blender's USD exporter writes, and a material whose UV map tiles
  past the 0-1 square or overlaps bakes into a generated `usdstage_bake` UV map.
- Animation takes, skinned meshes, shape keys, collection instances,
  subdivision surfaces, vertex displacement and drivers over `frame` export.
- Textures are staged beside the export as PNG, JPEG or OpenEXR; new bakes
  are PNG. USDZ files are packaged by a built-in packager, or by `usdzip`
  when its path is set in the add-on preferences.
- Failed exports always write a diagnostics report, and **Create Support
  Bundle** collects it with logs and environment details, with absolute paths
  redacted.
- The Shader Editor's RealityKit Authoring panel and **Add ▸ RealityKit
  Nodes** menu insert RealityKit node groups.
- A `usdstage` command line and two agent skills run the same exports without
  the Blender UI. Options and `key=value` overrides can come in any order, the
  `-o` extension chooses the format, and errors carry stable codes.

What exports, and what is refused, is listed in
[docs/FEATURE_SUPPORT.md](docs/FEATURE_SUPPORT.md).

### Coming from BlenderToRCP

USD Stage for Blender replaces BlenderToRCP. It is a new extension, so nothing
from BlenderToRCP carries over:

- Uninstall BlenderToRCP, then install `usd-stage-for-blender-<version>.zip`.
  With both installed, Blender shows two sidebar tabs.
- Export settings saved in `.blend` files and the add-on preferences start
  from their defaults.
- Rename the material custom property `blender_to_rcp_alpha_cutout_threshold`
  to `usd_stage_alpha_cutout_threshold`; the old one is ignored.
- The command line is `usdstage`, Blender's path comes from
  `USDSTAGE_BLENDER`, operators are `usdstage.*`, and the agent skills are
  `usd-stage-cli` and `usd-stage-setup`.
- Exported materials carry `USDStage:surfaceProfile` in their customData.
- Working folders are named `.usdstage_*`; leftover `.blendertorcp_*` folders
  next to earlier exports can be deleted.
- Textures are staged under `textures/<export file>/<generation>/`. Texture
  folders an earlier BlenderToRCP export left beside the same file are not
  removed; delete them by hand.
- Support bundles are named `USDStage-support-<scene>-<time>.zip`.
