# What Apple's toolchain does with USD features

This page records what Apple's tools actually do with the USD features this
exporter authors, measured rather than assumed. Read it before relying on a
feature reaching a RealityKit app.

`scripts/check_apple_conformance.py` produces every result here. It authors a
minimal asset per feature, packages it with `usdzip`, compiles it with
`realitytool`, and reads the artifacts back: the USDZ through OpenUSD, the
`.reality` by unzipping it and reading `assetMap.json`, which is stored
uncompressed and names every compiled asset by the prim path it came from.

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
