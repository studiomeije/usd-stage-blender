# What Apple's toolchain does with USD features

This page records what Apple's tools actually do with the USD features this
exporter authors, measured rather than assumed. Read it before relying on a
feature reaching a RealityKit app.

`scripts/check_apple_conformance.py` produces every result here. It authors a
minimal asset per feature, packages it with `usdzip`, compiles it with
`realitytool`, and reads the artifacts back: the USDZ through OpenUSD, the
`.reality` by unzipping it and reading `assetMap.json`, which is stored
uncompressed and names every compiled asset by the prim path it came from.

A component is not an asset, so the asset map cannot see one. The rest of a
`.reality`'s members are compressed with a method neither Python nor `ditto`
can read, but the zip directory still records each member's uncompressed size,
so the compiled scene's size against a control asset's says whether a feature
reached the scene at all. [Reading a component](#reading-a-component) explains
what that comparison can and cannot settle.

```bash
python3 scripts/check_apple_conformance.py
```

It compares against `scripts/apple_conformance.json` and exits non-zero only
when a result changes. A feature Apple does not support is reported, not
treated as a failure — it is the change that matters.

## Why this exists

A clean exit code says a file was accepted, not that its contents survived.
`realitytool` discards every variant set and still exits 0. `usdchecker`
validates variant selections thoroughly, which makes it tempting to trust
elsewhere. The two tools disagree about the same file, so neither can stand in
for looking at the output.

This is the same approach `scripts/check_shader_implementations.py` takes for
MaterialX nodes, and for the same reason.

## Results

Measured on Apple USD Tools 0.25.11 and `realitytool` for `xros 27.0`.

| Feature | USDZ | Compiled `.reality` |
|---|---|---|
| Variant set switching activation | Kept | **Discarded** |
| Variant set switching material bindings | Kept | **Discarded** |
| Two variant sets on one prim | Kept | **Discarded** |
| Nested variant sets | Kept | **Discarded** |
| A default selection that is not the first variant | Kept | **Discarded** |
| `kind`, `assetInfo`, `extentsHint` | Kept | n/a |
| Payloaded geometry in a second layer | Composes | Geometry arrives |
| Referenced geometry in a second layer | Composes | Geometry arrives |
| `UsdPhysics` collider, rigid body, mass | Kept | **Dropped** |
| `RealityKitComponent` prim | Kept | **Becomes a `CollisionShape`** |
| `UsdGeomPointInstancer` | Kept | One mesh asset; see below |
| `GeomSubset` material bindings | Kept | Both materials arrive on one mesh |
| `subdivisionScheme = catmullClark` | Kept | **Tessellated at compile**: one quad's mesh grew from 2,188 to 9,969 bytes |
| `UsdSkel` skeleton, skinning, joint animation | Kept | A `Skeleton` asset, with `AnimationLibraryAsset` and `Timeline` |
| `UsdSkelBlendShape` | Kept | A `BlendFactor` asset |
| Time samples on an `xformOp` | Kept | `AnimationLibraryAsset` and `Timeline` |
| `visibility = invisible` | Kept | Compiled in; nothing prunes it |
| `purpose = guide` | Kept | **Dropped**: no asset is compiled for the prim |
| `userProperties` | Kept | **Dropped**: the compiled scene is byte-identical to the control |
| `UsdLux` distant, sphere and dome lights | Kept | Reach the scene; what they arrive as is unsettled |
| `UsdGeomCamera` | Kept | Reaches the scene; same limit |
| `RealityKit.DirectionalLight`, `.SpotLight`, `.PointLight` components | Kept | Read |
| The same ids under the components' Swift names (`.PointLightComponent`) | Kept | **Dropped in silence** |
| A collider `shapeType` other than `Box`, or an invented one | Kept | A `CollisionShape` either way; the shape is unverifiable here |
| `RealityKit.ImageBasedLight` and `.ImageBasedLightReceiver` | Kept | Read |
| `RealityKit.Anchoring` component | Kept | Read |
| An `info:id` the platform does not know | Kept | **Dropped in silence** |
| `instanceable` references over a Scope prototype | Composes | One shared mesh asset |
| `instanceable` references over a **class** prototype | Composes | **No mesh asset at all** |

## What the results mean

**Variants do not reach a compiled `.reality`.** Every shape of variant set is
discarded — activation, material binding, two sets side by side, nested sets,
and a default selection other than the first. `realitytool` resolves the
shipped selection, compiles only that, and exits cleanly. The compiled
package's `configurationSetNames` and `defaultConfigurationNames` come back
empty every time.

The format itself is not the limit: `.reality` carries those configuration
fields, and Reality Composer Pro models variants internally with its own
`tm_usd_variant` and `tm_usd_variant_selection` types, where a variant is a
named set of (prim, variant set, selection) choices stored per imported asset.
So configurations are authored in the Reality Composer Pro project, not read
from the USD. Variant sets in the file are the raw material its picker offers.

Variants do survive `.usda`, `.usdc` and `.usdz`, and the USDKit stage APIs on
visionOS and macOS 27 switch them at runtime, so an app that loads a USDZ
rather than a compiled `.reality` keeps them.

**RealityKit wants its own components, not `UsdPhysics`.** A mesh carrying
`PhysicsCollisionAPI`, `PhysicsRigidBodyAPI` and `PhysicsMassAPI` compiles to
a `.reality` holding only `Material`, `MaterialDefinition`, `Mesh` and
`Scene` — the physics is gone. A `RealityKitComponent` prim with
`info:id = "RealityKit.Collider"` compiles to a real `CollisionShape` asset
beside the mesh.

Anything this exporter does for physics should therefore author RealityKit
component prims. `UsdPhysics` would validate, package and compile without
complaint, and arrive as nothing.

**Payloads and references survive.** Both compose through `usdzip --asset`,
which copies the second layer into the package, and the geometry reaches the
compiled `.reality`. A payloaded asset's `kind` is readable while its payload
is unloaded, which is the point of putting it on the root prim.

### Reading a component

A light, an anchoring component and a collider are not alike. A collider
compiles to a `CollisionShape`, which the asset map names; a light does not
compile to an asset at all, so a light that imported and a light that was
dropped leave the same asset list.

What tells them apart is the compiled scene's size against a control asset
that differs by the feature alone. There are two controls, because an empty
Xform is an entity and an entity costs bytes: a component hung on an Xform of
its own is read against `control_xform`, and one applied to a prim that
already exists against `control_cube`. Reading the first kind against
`control_cube` would credit the component with the Xform's bytes.

The control for the reading itself is an `info:id` that is not a real one:

| Probe | Compiled scene | Against its control |
|---|---|---|
| `control_cube` | 541 bytes | - |
| `control_xform` | 680 bytes | - |
| `unknown_component_id` | 541 bytes | **+0** vs `control_cube` |
| `rk_point_light_suffixed_id` | 680 bytes | **+0** vs `control_xform` |
| `RealityKit.DirectionalLight` | 733 bytes | +53 vs `control_xform` |
| `RealityKit.PointLight` | 747 bytes | +67 vs `control_xform` |
| `RealityKit.SpotLight` | 759 bytes | +79 vs `control_xform` |
| `RealityKit.ImageBasedLight` + receiver | 709 bytes | +168 vs `control_cube` |
| `RealityKit.Anchoring` | 809 bytes | +268 vs `control_cube` |

So the ids are real, the components are read, and **an `info:id` the platform
does not know is dropped without a word** - which is the shape a typo in our
own authoring would take: a clean export, a clean compile, and no component.

**The ids do not follow the components' Swift names.** The OS component set
lists `PointLightComponent`, and the id it answers to is
`RealityKit.PointLight`: the suffixed spelling measures +0, exactly like an
invented one. `RealityKit.AreaLight` and `RealityKit.Light` measure +0 too.
An id has to be measured, not derived.

**A collider's `shapeType` is not validated at compile.** `Box`, `Sphere`,
`Capsule`, `Cylinder`, `Convex`, `Mesh` and the invented token `Nonsense` all
produce the same `CollisionShape` asset and the same 586-byte scene. So the
compile says a collider arrived and nothing about its shape, and `Box` is the
one spelling backed by what Reality Composer Pro shows rather than by a clean
exit code.

For a typed prim the same comparison is only suggestive. A `UsdLux` light and
a `UsdGeomCamera` each grow the scene - 678, 759 and 694 bytes for the
distant, sphere and dome lights, 719 for the camera - but every one of them is
also an `Xformable`, so it can arrive as a bare entity carrying a transform
and no light. Growth proves arrival, not what arrived. Settling it needs a
RealityKit build that reports the components on an imported entity, which is
outside what reading a file can do.

**A prototype in a class prim loses its geometry.** Three `instanceable`
references over a prototype under an ordinary `Scope` compile to one shared
mesh asset. The same three over a prototype held in a *class* prim compile to
**no mesh asset at all**, while still reporting success. The exporter writes
Scope prototypes, so this is a warning about hand-written USD rather than
about our output.

**Mesh assets cannot measure instancing.** `realitytool` deduplicates
identical meshes: three separate cubes, three plain references and three
`instanceable` references all compile to exactly one mesh asset. Counting
assets therefore shows only that instancing costs no more, never that it
survived as instancing. What the USD side does is covered by
`tests/integration/test_variants_with_instancing.py`.

**One field is not stable.** Two full runs of `xform_time_samples` disagreed
about whether the animated Xform's own prim path appears in the asset map
beside the mesh's, with every other field identical. The suite records that
probe without that field rather than failing at random over it; the constant
`UNSTABLE_FIELDS` names it.

**Point instancing needs a closer look.** The instancer prim survives into the
USDZ, and the compiled `.reality` holds one mesh asset. That is consistent
both with the instancer working — one mesh, drawn three times — and with it
collapsing to its prototype. `assetMap.json` counts assets, not scene
instances, so it cannot tell the two apart. Settling it needs a RealityKit
build that counts what is drawn.

## What is not covered

These probes measure `usdzip` and `realitytool`. They say nothing about what a
RealityKit app draws, which needs a device or a build, and nothing about macOS
Quick Look, which has its own behaviour: a USDZ shipping a variant selection
has been observed displaying a different variant. Treat the viewer as a
separate question from the file.
