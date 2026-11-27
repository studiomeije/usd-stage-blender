# Changelog

Notable changes to USD Stage for Blender, newest first. Dates are release dates.

Entries describe what changed for you — what now works, what stops the export,
and what you have to do differently. Internal refactors are not listed.

## Unreleased

### New

- **Derivative and filtered-sampling nodes pass the export.** Graphs that use
  `ND_MTL_dfdx`, `ND_MTL_dfdy` or `ND_MTL_fwidth` (float, vector and half
  forms), `RealityKitTexture2DGradient` or `RealityKitTexture2DLOD` no longer
  stop with `UNKNOWN_MATERIALX_NODEDEF`; RealityKit on macOS 27.2 renders them.
- **The export warns when a node renders on the OS but not in Reality Composer
  Pro.** `ND_hextiledimage` renders on macOS 27.2, but Reality Composer Pro 3
  cannot build it; a material using it now exports with a warning that names
  the ShaderGraph builds that will show it without its shader graph.
- `scripts/check_shader_implementations.py --shadergraph os|rcp|all` checks an
  export against the OS's or every installed ShaderGraph build, not only
  Reality Composer Pro's.

- **Ambient occlusion is declared, not guessed.** Set the material custom
  property `usd_stage_ambient_occlusion` to a node, or `Node:Output` such as
  `ORM Split:Red`, or label one node `Ambient Occlusion`; the property wins.
  The source need not be connected to the shader.

- **Anisotropy exports.** Principled Anisotropic and Anisotropic Rotation,
  and the Glossy and Metallic BSDFs' Anisotropy and Rotation, export to
  RealityKit with Cycles' highlight shape: the exporter sets RealityKit's
  anisotropy level and angle and adjusts Roughness so the stretch matches,
  measured against both renderers. Link a Tangent node set to UV Map (on the
  render UV map) to the shader's Tangent input; without it, with a linked
  anisotropy, or where the adjusted roughness would exceed 1, the export stops
  and says why.

- **Textured clearcoat.** Coat Weight and Coat Roughness driven by a texture
  (an ORM-style channel, a Math node, any data chain) now export instead of
  stopping with `UNSUPPORTED_MATERIAL_NODES`, and read at their authored
  values. With Coat Normal unlinked, the coat now follows the Normal input as
  it does in Cycles.

- **Exact colour on unlit screens.** Set the material custom property
  `usd_stage_unlit_tonemap` to False and the unlit material is exported with
  RealityKit's tone mapping off, so a 0.18 grey shows as 0.18 rather than
  0.13. Materials without the property are unchanged.

- **Textures your app can swap.** Set the material custom property
  `usd_stage_texture_parameters` to True and each texture becomes a named
  material parameter (`baseColorTexture`, `metallicRoughnessTexture`,
  `normalTexture`, ...) that `ShaderGraphMaterial.setParameter` can rebind at
  runtime. Materials without the property export exactly as before.

### Before you update

- **A Coat IOR other than 1.5 stops the export.** RealityKit ignores the
  coat IOR and always reflects like IOR 1.5, so the exported coat was brighter
  or dimmer than Cycles'. Set Coat IOR to 1.5, or bake the material.

- **An image node whose name contains "ao" no longer becomes ambient
  occlusion.** The exporter used to route any such image linked to the
  Principled BSDF into `ambientOcclusion` as well, so a "Chaos AO" base colour
  also darkened the lighting. Declare the AO source as described above.

### Fixed

- **Roughness, metallic and AO maps now render at their authored values in
  RealityKit; before, they rendered darker.** RealityKit decodes every colour
  texture read as sRGB, even one tagged as data, so a Non-Color roughness
  texel of 0.5 rendered as 0.22. Data maps (roughness, metallic, AO, ORM
  channels, opacity masks and any other non-colour input) and colour maps
  tagged Linear Rec.709 or Non-Color are now read through a linear reader, for
  RGB and greyscale files alike. sRGB colour maps are unchanged. Re-export
  assets that use data maps.
- **A material with two Separate Color nodes, or any repeated node reading
  a texture, exports every read.** Two channel reads feeding the same input
  name of different nodes were written to one node, so the second texture
  replaced the first: a scan with tile detail rendered its roughness from the
  tile map alone. Each read now keeps its own node.

## 1.2.0 — 2026-11-18

### Before you update

- **Blender 5.3 or newer is now required.** Installation, command-line
  exports, release packaging and CI use this baseline. Older `.blend` files
  still open in Blender 5.3.

### New

- **Variant sets.** One file can now offer alternatives that Reality Composer
  Pro and RealityKit switch between. Declare them in the new **Variants**
  panel; the [user guide](docs/USER_GUIDE.md#offering-variants) walks through
  it.
    - A **geometry set** switches whole collections of objects: **Geometry Set**
      builds one from a parent collection whose children are the variants.
    - A **material set** keeps the objects and rebinds their material, so one
      tabletop can be offered in oak or walnut without a second copy of the mesh.
      **Material Set** builds one from the collection it applies to and the
      materials you pick, by checkbox or by a shared name prefix such as
      `Fabric_`. One object can belong to a geometry set and a material set at
      once: three tops and four finishes is twelve looks from three meshes.
    - A material set can be offered on some shapes only - walnut on the square
      top but not the round one - with **Geometry Restrictions** and each
      material's **Offered On** toggles.
    - The export authors a `variantSet` on the asset's root prim and ships the
      variant you chose with **Use as Export Default**. Objects in no variant
      are present whatever the selection.
    - Variants you have hidden or excluded are brought into the export, and your
      view layer is put back exactly as it was afterwards.
    - **Preview in Viewport** shows a geometry variant in the scene, and
      **Previews** renders a thumbnail of each variant.
    - The panel keeps a declaration consistent as you edit it - renaming carries
      through to the default and to nested sets - and lists every problem an
      export would refuse or warn about, in the export's own words.
    - An export stops rather than ship a set that cannot switch: two variants
      claiming one object, a variant parented under another's, a name that is
      not a USD identifier, or an opinion in the file that would override the
      variants. Variant names are never renamed for you, since they are what
      Reality Composer Pro lists and what your code passes to
      `setVariantSelection(for:to:)`.
    - A material nothing wears yet can still be offered: the export writes it,
      and reads the UV maps and colour attributes it uses from the objects the
      set targets.
    - Selected-only exports bring along the other variants, rigs and dependent
      members of any set the selection touches.
- **Asset identity.** The root prim carries a USD `kind` (Component by
  default), `assetInfo` naming the file a consumer references, and an
  `extentsHint` giving its bounds, so another stage can reference, instance
  and lay out the export without loading its geometry. Set **Advanced USD ▸
  General ▸ Asset Kind** to None to author none of it.
- **Payload Geometry** (Advanced USD ▸ General) splits an export into an asset
  interface - root prim, kind, asset info, bounds and variant sets - and a
  payload under `assets/` with the geometry and materials. Another scene can
  reference your asset, place it and pick a variant without loading any
  geometry. Composed, it is identical to a flat export, and a USDZ carries
  both halves.
- **Colliders.** Objects with Blender Rigid Body enabled export with a
  RealityKit collider sized from the object's own geometry, so an asset can be
  tapped, dragged or collided with in an app without adding a collision
  component in Reality Composer Pro. Colliders are boxes; other collision
  shapes are approximated and the diagnostics report says which. Turn it off
  with **Advanced USD ▸ General ▸ Colliders**.
- **A clearer export panel.** Save To, Format, the Profile and Selection Only
  sit together above one **Export** button, and an empty Save To opens a file
  picker. After an export a result card shows the outcome and warning count,
  with **Reveal File** and **View Details**, a summary of the export that
  opens the full report in Blender's Text Editor.
- **Blender 5.3's USD workflows.** Exports use Blender 5.3's native normals
  and keep matrix mesh attributes. Reloadable geometry from Blender's USD
  Collection Importer exports through the normal pipeline; see the
  [export guide](docs/EXPORT_PIPELINE.md#reloadable-usd-source-collections)
  for the workflow and its limits.
- `usdstage info` lists the variant sets a `.blend` declares, so you can see
  what a file will export without exporting it.
- **Documentation.** A new [user guide](docs/USER_GUIDE.md) covers the
  everyday tasks with screenshots, and the documentation is published at
  [studiomeije.github.io/usd-stage-blender](https://studiomeije.github.io/usd-stage-blender/).

### Fixed

- An asset referenced through a network `file://server/share/...` URL is now
  copied from that server. The server name used to be dropped, so the export
  looked for the path on your own disk instead. Upper-case `FILE://` URLs are
  read as URLs too, an asset path with a scheme such as `s3://` is no longer
  mistaken for a local file, and file URLs with escaped percent signs name the
  correct file.
- The diagnostics report is always valid JSON. A value such as an empty
  object's bounds could write `NaN` into it, which strict JSON readers refuse;
  such numbers are now written as the strings `"NaN"`, `"Infinity"` and
  `"-Infinity"`.
- An export started from the panel records how long each step took in its
  diagnostics report, as the command line always did.
- A material refused because a UV map or attribute it reads is missing names
  the scene objects that lack it, instead of the mesh data's name.

### Limits

- **Reality Composer Pro's `.reality` compile keeps only the selection you
  shipped.** `realitytool` resolves the default and discards the other
  variants, so a `.reality` built from an `.rkassets` bundle carries none
  unless you also define Reality Composer Pro's own variants in the editor.
  The variant sets survive intact in `.usda`, `.usdc` and `.usdz`.
- A material set over a collection instance is refused while Instancing is
  on, because the shared prototype's material would win. Turn Instancing off
  and every mesh of every instance takes the finish.
- Baking supports geometry sets, not material sets: use Translate Materials
  for material variants, or turn off **Include Variants in Export** to bake
  without them. Lighting & Shadows needs **Isolate Meshes for Shadows** when
  geometry variants are exported.

## 1.1.0 — 2026-10-17

- An export where two materials have names that reduce to the same USD prim
  name now stops with an error naming both materials and the prim they share.
  Blender's exporter writes a single material prim for them and binds every
  mesh to it, so one material was dropped and the meshes that used it exported
  with the other one — and which of the two survived depended on export order.
  Rename all but one so the names still differ once the characters USD cannot
  use in a prim name are replaced. A material slot no face uses is unaffected,
  and so is a mesh that binds several materials through GeomSubsets.
- A bake that cannot move a shared mesh's material slot onto its object now
  says so in the diagnostics report. It previously carried on in silence, and
  the symptom was one instance's bake appearing on its linked duplicates.

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
