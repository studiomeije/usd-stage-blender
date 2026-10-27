# Changelog

Notable changes to USD Stage for Blender, newest first. Dates are release dates.

Entries describe what changed for you — what now works, what stops the export,
and what you have to do differently. Internal refactors are not listed.

## Unreleased

- You can now export USD variant sets. Group each variant in its own
  collection, then declare the set in the new **Variants** panel — **Shape
  Set** builds one from a parent collection whose children are already the
  variants, starting from the collection selected in the Outliner. The export
  authors a `variantSet` on the asset's root prim, and the file ships
  selecting the variant you marked as the default. Objects in no variant are
  present whatever the selection.
- The Variants panel keeps a declaration consistent as you edit it. Renaming
  a variant or a set carries the new name to the default selection, to the
  finish sets nested inside it and to their Offered On toggles. A variant you
  add takes its name from the collection or material you then give it, until
  you name it yourself. And the panel lists, for the selected set, every
  problem an export would refuse or warn about, in the export's own words.
- A variant you have hidden is brought into the export, and your view layer is
  put back exactly as it was afterwards. Blender's exporter drops a collection
  excluded from the view layer and anything disabled for renders, so until now
  a variant-organised scene exported only whatever happened to be visible —
  or, if you hid variants with the outliner's eye, exported all of them stacked
  on top of each other.
- An export stops rather than shipping a variant set that cannot switch. Two
  variants claiming the same object, a variant whose objects are parented under
  another variant's, a name that is not a USD identifier, or an opinion in the
  file that would override the variants are all refused with the names
  involved. Variant names are never renamed for you: a variant name is what
  Reality Composer Pro lists and what your code passes to
  `setVariantSelection(for:to:)`. Creating a set from a collection is the one
  place a name is adjusted, where you can see and edit it in the panel.
- A finish set can now nest inside a shape set, so a finish can be offered
  on some shapes and not others - walnut on the square top but not the
  round one. Set Nest Inside on the finish set, then use each finish's
  Offered On toggles - one per shape - to pick the shapes that offer it. A
  finish you leave offered everywhere is offered on a shape you add later
  too, and you cannot turn the last shape off: a finish offered on nothing
  is refused where you make the mistake, not at export.
- A variant set now varies either **shape** or **finish**. A shape set
  switches whole collections of objects, as before. A finish set keeps the
  objects and rebinds their material, so the same tabletop can be offered in
  oak or walnut without a second copy of the mesh. Because the two vary
  different things, one object can belong to a shape set and a finish set at
  once: three tops and four finishes is twelve looks from three meshes.
  **Finish Set** builds one in a step: pick the collection it applies to and
  the start the finishes' material names share, such as `Fabric_`, and every
  matching material becomes a variant named after the rest of its name, with
  the one your objects already wear as the default. A finish recolours
  objects a shape set hides by default too, and leaves an object parented
  under a target alone unless it is a target itself. Offering a finish
  nothing currently wears works too, whatever its name - `Bordeaux.001`
  included.
- An export can now be split into an asset interface and a payload, with
  Advanced USD ▸ Payload Geometry. The interface holds the root prim, its
  kind, asset info, bounds and variant sets; the geometry moves to
  `assets/<name>_payload`. Another scene can then reference your asset,
  place it and pick a variant without loading any geometry, and a USDZ
  still carries both halves. Composed, it is identical to a flat export:
  the payload repeats the units and frame rate, so animation plays at the
  same speed.
- Objects with Blender Rigid Body enabled now export with a RealityKit
  collider, sized from the object's own bounds, so an asset can be tapped,
  dragged or collided with in an app without adding a collision component
  by hand in Reality Composer Pro. Collision shapes other than Box are
  approximated as a box and the diagnostics report says which. The
  component is a child called `Collider`, or `Collider_1` when the object
  already has a child of that name, such as a collision proxy. Turn it off
  with Advanced USD ▸ Colliders.
- Exports now identify themselves as an asset. The root prim carries a USD
  `kind` (Component by default), `assetInfo` naming the file a consumer
  references, and an `extentsHint` giving its bounds. Another stage can
  therefore reference, instance and lay out the export as one model without
  loading its geometry. Set Asset Kind to None to author none of it.
- `usdstage info` lists the variant sets a `.blend` declares - what each
  varies, and the collection or material behind every variant - so you can
  see what a file will export without exporting it, and the diagnostics
  report records what an export authored.
- **Reality Composer Pro's `.reality` compile keeps only the selection you
  shipped.** `realitytool` resolves the default and discards the other
  variants, so a `.reality` built from an `.rkassets` bundle carries none
  unless you also define Reality Composer Pro's own variants in the editor.
  The variant sets survive intact in `.usda`, `.usdc` and `.usdz`.

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
