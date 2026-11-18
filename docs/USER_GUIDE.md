# User guide

This guide walks through the tasks you do with USD Stage for Blender, in the
order you usually meet them: a first export, choosing how materials leave
Blender, fixing a refused material, offering variants, and getting an asset
ready for an app. Each section says where the controls are and what to expect.
The reference pages linked along the way explain every setting in depth.

## Contents

- [Before you start](#before-you-start)
- [Your first export](#your-first-export)
- [Choosing how materials export](#choosing-how-materials-export)
- [When a material is refused](#when-a-material-is-refused)
- [Offering variants](#offering-variants)
- [Getting an asset ready for an app](#getting-an-asset-ready-for-an-app)
- [Reading the result of an export](#reading-the-result-of-an-export)
- [Exporting from the command line](#exporting-from-the-command-line)
- [Common refusals and what to do](#common-refusals-and-what-to-do)

## Before you start

You need Blender 5.3 or newer, with the extension installed as the
[README](../README.md#install) describes. To open what you export, use Reality
Composer Pro 3, or RealityKit on visionOS, iOS or macOS 27.

Everything happens in **3D Viewport ▸ Sidebar (N) ▸ USD Stage ▸ USD Stage
Export**. The panel has one export block at the top and collapsible sections
below it:

| Section | What it holds |
|---|---|
| **Material Settings** | Options for the chosen Profile: specular tint clamping, bake roughness, the lighting source for a lighting bake |
| **Textures** | Maximum texture size and image format, or the bake size and format |
| **Variants** | The asset's variant sets |
| **Advanced USD** | Root prim, asset kind, colliders, payload, custom properties, geometry and rigging options |
| **Diagnostics** | Whether successful exports also write a report, and the support bundle |

Two more panels live in **Shader Editor ▸ Sidebar ▸ USD Stage**:
**RealityKit Compatibility** checks the active material, and **RealityKit
Authoring** inserts RealityKit PBR or Unlit node groups.

## Your first export

1. Open **USD Stage Export**.
2. In **Save To**, choose where the file goes. You can leave it empty: pressing
   **Export** then opens a file picker.
3. Choose a **Format**. USDZ is a single packaged file, ready to drop into
   Reality Composer Pro or an app. USDA and USDC write the stage with its
   textures beside it, which is easier to inspect.
4. Choose a Profile: **RealityKit PBR** or **RealityKit Unlit**, then the method
   on the row beneath it. The line under the buttons says in a sentence what
   the export will produce. [Choosing how materials export](#choosing-how-materials-export)
   explains the four options.
5. Turn on **Selection Only** to export only the selected objects, and
   **Animation** to include animation.
6. Press **Export**.

<img src="images/user-guide/export.png" alt="The USD Stage Export panel after a successful export: the result card with Reveal File and View Details, then Save To, Format, the Profile and method buttons, Selection Only, Animation and Export, with the collapsed sections below" width="300">

A translate export runs in the foreground and takes seconds. A bake export
runs in a second Blender process, so you can keep working: a **Background
Job** card shows its progress and a cancel button, and the panel's settings
stay locked until it finishes.

Every export is Y-up, -Z forward and in meters, whatever your scene uses, so
it lands the right way up and the right size in Reality Composer Pro.

When it finishes, a result card at the top of the panel says whether it
succeeded; [Reading the result of an export](#reading-the-result-of-an-export)
explains it.

## Choosing how materials export

The Profile decides how Blender materials become RealityKit materials.

| You want | Choose |
|---|---|
| Editable materials that respond to RealityKit's lighting | **RealityKit PBR ▸ Translate Materials** |
| The same, for materials too complex to translate | **RealityKit PBR ▸ Bake Materials** |
| Material colour only, ignoring scene lighting | **RealityKit Unlit ▸ Material Color Only** |
| What you see in Blender, lighting and shadows included | **RealityKit Unlit ▸ Lighting & Shadows** |

Start with **Translate Materials**. It keeps materials editable in Reality
Composer Pro and does not bake anything. If a material uses nodes it cannot
translate, the export stops and names them; switch to a bake only if you
cannot or do not want to rework that material.

A bake renders what cannot be translated into textures, in a separate
Blender process. [BAKING.md](BAKING.md#2-the-bake-modes) says what each bake
captures and what it changes in the scene while it runs.

## When a material is refused

A material the export cannot translate stops the export with an error that
names the material, the node and the fix. To check a material before you
export:

1. Open the material in the **Shader Editor**.
2. Open **Sidebar ▸ USD Stage ▸ RealityKit Compatibility**. It says whether the
   active material is **Compatible**, **Compatible with warnings** or
   **Incompatible**, and lists every issue.
3. Press **Select Offending Nodes** to select the nodes to fix.

<img src="images/user-guide/compatibility.png" alt="The RealityKit Compatibility panel in the Shader Editor reporting an incompatible material: a Wireframe node is not supported, with Validate RealityKit Material and Select Offending Nodes buttons" width="300">

Warnings do not stop the export; they mark something that exports
differently from Cycles. [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md) lists every
node and how it exports, and [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md)
explains how a shader graph is translated.

## Offering variants

Variants let one file offer alternatives the person using the asset picks
from: a round or a square table top, an oak or a walnut finish. The export
writes them as USD variant sets, which Reality Composer Pro and RealityKit
let you switch between.

There are two kinds of set:

- A **geometry set** switches between collections of objects. Each variant is
  a collection; choosing one shows its objects and hides the others.
- A **material set** switches the material on a group of objects. Each variant
  is a material; choosing one binds it on every object in the set's target
  collection.

Open **USD Stage Export ▸ Variants** to declare them.

### Make a geometry set

1. Put each alternative in its own collection, and the alternatives side by
   side under one parent collection: for example `Top Shape` holding `Round`
   and `Square`.
2. In **Variants**, press **Geometry Set**, choose the parent collection and
   confirm. Each child collection becomes a variant.

You can keep working with the alternatives you are not editing hidden or
excluded from the view layer. The export brings every variant's objects in
for its own run and puts your view layer back exactly as it was afterwards.

<img src="images/user-guide/variants-geometry.png" alt="The Variants section with a geometry set TopShape selected: its Round and Square variants, Editing: Square, Use as Export Default, the Square collection, Preview in Viewport, and Previews with a thumbnail of each shape" width="300">

### Make a material set

1. Put the objects whose material changes in one collection, for example
   `Tops`.
2. In **Variants**, press **Material Set**.
3. In **Applies To**, choose that collection.
4. Pick the materials to offer. **Pick Materials** shows a checklist of the
   file's materials; **Match Prefix** takes every material whose name starts
   with a prefix, such as `Fabric_`. Each chosen material becomes a variant.

A finish nothing in the scene wears yet is fine: the export still writes it,
and reads the UV maps and colour attributes it uses from the objects the set
targets.

### Edit a set

Select a set in the list, then a variant in the list beneath it.

- **Editing: \<name\>** shows which variant you are changing. Its collection or
  material is below. For a material, **Selected Material** shows a live
  preview.
- **Use as Export Default** makes that variant the one the asset opens with.
  Selecting a variant to edit does not change the default; only this button
  does. The **Export default** line under the list says what the asset will
  open with.
- **Preview in Viewport** (geometry sets) shows that variant in the scene and
  excludes the set's other variants from the view layer. It stays that way
  until you preview another variant, which is how you switch back.
- **Previews** shows a thumbnail per variant. Press **Refresh** to render
  them; click one to edit that variant. These are Blender's own previews, not
  RealityKit renders.
- The **Geometry** / **Materials** switch at the top of a set is locked once
  the set has variants, so a populated set cannot change kind by accident.

Names of sets and variants become USD names, so use letters, digits and
underscores, starting with a letter: `TopShape`, not `Top Shape`.

### Offer a finish on some shapes only

A material set can be nested in a geometry set, so that a finish is offered
on some shapes and not others: walnut on the square top only, say.

1. Select the material set and expand **Geometry Restrictions**.
2. Choose the geometry set it belongs to. **All Geometry (Independent)** means
   it is not nested.
3. Select a finish in the variant list, and use the **Offered On** toggles to
   choose the shapes that offer it.

Changing the geometry set resets every finish to all of its shapes. Objects
that no shape owns, such as a frame every shape shares, take the finish under
every shape.

<img src="images/user-guide/variants-material.png" alt="The Variants section with a material set Finish selected: Applies To Tops, the Oak and Walnut variants, Editing: Walnut, and Geometry Restrictions set to TopShape with Walnut offered on Square only" width="300">

### Variants and baking

A bake export supports geometry sets, not material sets.

- With a material set declared, the export block says **Material variants
  need Translate Materials** and offers a **Use Translate Materials** button.
- With **Lighting & Shadows**, geometry alternatives need **Isolate Meshes for
  Shadows**, so one alternative does not cast shadows onto another. The export
  block offers the toggle when it is needed.

To export without variants for a while, turn off **Include Variants in
Export**. The sets stay in the file and remain editable.

What variants cannot do, and how they are written, is in
[EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#5-variant-sets).

## Getting an asset ready for an app

These settings are in **Advanced USD ▸ General**.

<img src="images/user-guide/advanced.png" alt="Advanced USD, General section: Root Prim, Asset Kind set to Component, Colliders, Payload Geometry, Custom Properties, Namespace, Blender Names, Allow Unicode, Use Settings for and Instancing" width="300">

- **Asset Kind** marks the root as a USD asset component, and writes its name
  and bounds. Another scene can then reference the asset and lay it out
  without loading it. Leave it as is unless your pipeline assigns kinds
  itself.
- **Colliders** writes a RealityKit collider for every object with Blender's
  **Rigid Body** enabled (**Properties ▸ Physics ▸ Rigid Body**), so the asset
  can be hit-tested and collide in an app without work in Reality Composer
  Pro. Colliders are always boxes sized from the object's own geometry; other
  collision shapes are approximated by that box, and the export report lists
  them. The rigid body's dynamics are not exported, and interaction still
  needs input targets and behaviour authored in Reality Composer Pro or app
  code.
- **Payload Geometry** splits the export in two: a small interface file with
  the root prim, its kind, bounds and variant sets, and a payload under
  `assets/` with the geometry and materials. A scene that references the
  asset can read what it is and choose a variant before loading any geometry.
  Composed, the two are identical to a single file, and a USDZ carries both.

[SETTINGS.md](SETTINGS.md) has the full detail for each.

## Reading the result of an export

After an export, a card at the top of **USD Stage Export** shows:

- **Export completed**, **Export failed** or **Export canceled**, with the
  number of warnings when there are some;
- the file name, and for a failure the reason;
- **Reveal File** to show the file in Finder;
- **View Details** for a summary of the export - its output, the materials and
  textures it converted, and its warnings and errors - with **Open Full Report
  in Text Editor** for the complete report;
- **Open Log** for a background bake's log.

A failed export never replaces a file that was already at the path: the
previous file stays as it was, and the card says so.

**Diagnostics** controls the reports. A failure always writes one; turn on
**Keep Success Diagnostics** to keep a report for successful exports too.
**Create Support Bundle** collects the scene's settings, the report and the
logs into one redacted archive to attach to a bug report, as
[CLI.md](CLI.md#reporting-a-problem) describes.

## Exporting from the command line

Everything the panel does is also available from a terminal, without opening
Blender's interface: export, bake, validate materials and read or change
settings.

```bash
usdstage export scene.blend -o table.usdz
```

[The README](../README.md#command-line) sets up the `usdstage` command, and
[CLI.md](CLI.md) lists every command and flag.

## Common refusals and what to do

| The export says | Do this |
|---|---|
| A node in a material cannot be translated | Rework the material with the nodes in [FEATURE_SUPPORT.md](FEATURE_SUPPORT.md), or switch to a bake Profile |
| Variant set name '…' is not a USD identifier | Rename the set or variant: letters, digits and underscores, starting with a letter |
| UV map '…' is not on object '…' | Add the UV map to the named objects, or read one every object using the material has |
| Color attribute '…' is not on object '…' | Add the colour attribute to the named objects, as the first colour attribute |
| Variant set '…' cannot rebind '…': it is exported as an instance | Turn off **Advanced USD ▸ General ▸ Instancing**, or make the instance real (**Object ▸ Apply ▸ Make Instances Real**) and target its meshes |
| Material variants need Translate Materials | Press **Use Translate Materials**, or turn off **Include Variants in Export** to bake without them |
| Export requires at least one scene object | Nothing visible is in scope: check **Selection Only**, and that the objects are not disabled for the mode in **Advanced USD ▸ General ▸ Use Settings for** |

For anything else, open **View Details** on the result card; the report names
what stopped the export and where.
