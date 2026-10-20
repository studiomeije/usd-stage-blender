# What RealityKit reads, and what we write

[FEATURE_SUPPORT.md](FEATURE_SUPPORT.md) answers "will my Blender scene
survive an export". This page answers the other direction: of the USD that
RealityKit can consume, how much does this exporter author, and what is left
on the table.

*Applies to: Blender 5.2 LTS; Reality Composer Pro 3; macOS 27, iOS 27,
iPadOS 27, tvOS 27, visionOS 27; Apple USD Tools 0.25.11.*

## How to read the tables

Two columns, two different questions.

**RealityKit** - whether the platform consumes the feature, and how it is
known:

| Word | Meaning |
|---|---|
| **measured** | A probe authored it, pushed it through `usdzip` and `realitytool`, and read the artifact back. |
| **observed** | Seen in a file Reality Composer Pro wrote, or in a resource it ships. |
| **assumed** | Taken from Apple's documentation or common practice, not verified here. |

**We author** - what this exporter writes:

| Word | Meaning |
|---|---|
| **Yes** | Authored on every export where it applies. |
| **Opt-in** | Authored when a setting asks for it. |
| **No** | Never authored. |
| **Refused** | The export stops if the feature appears in the stage. |

A gap is not automatically work worth doing. The last section ranks them.

## How the measurements were made

Two mechanisms, because they answer different questions.

`scripts/check_apple_conformance.py` authors a minimal asset per feature,
packages it with `usdzip`, compiles it with `realitytool`, and reads
`assetMap.json` out of the `.reality`, which is stored uncompressed and names
every compiled asset by the prim path it came from. That settles features
that become assets: meshes, materials, skeletons, collision shapes.

A component does not become an asset. A light or an anchoring component lives
inside the `Scene` asset, so a light that imported and a light that was
dropped produce the same asset list. The remaining members of a `.reality` are
compressed with a method neither Python's `zipfile` nor `ditto` can read, but
the zip directory still records each member's uncompressed size. Comparing the
`Scene` asset's size against a control asset - the same scene without the
feature prim - says whether the feature reached the compiled scene at all.

That comparison is decisive for a `RealityKitComponent` child prim and only
suggestive for a typed prim. A component prim is consumed as a component or
ignored: a recognised one grows the scene, and an unrecognised `info:id` grows
it by nothing. A typed prim such as a `UsdLux` light is also an `Xformable`,
so it can arrive as a bare entity carrying a transform and no light at all,
which looks the same from the outside as arriving with its light.

## Geometry and the scene graph

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| `UsdGeomMesh` points, counts, indices | measured | Yes | The one geometry schema everything else hangs off. |
| `UsdGeomXform`, `Scope`, hierarchy | measured | Yes | Written as `TRS` ops so the post-process can read object transforms. |
| `primvars:st` and further UV sets | assumed | Yes | The render UV map becomes `st`; the others keep their names. |
| Normals | assumed | Yes | |
| Vertex colours | assumed | Yes | Written as `displayColor` for the materials that read one. |
| `GeomSubset` with `familyName = "materialBind"` | measured | Yes | A multi-material mesh. Compiles to one mesh asset carrying both materials. |
| `subdivisionScheme = catmullClark` | measured | Yes | `realitytool` tessellates at compile: a single quad's mesh asset grew from 2 KB to 10 KB. **Best Match** writes the base cage, **Tessellate** subdivides in Blender. |
| `extent` on meshes | assumed | Yes | |
| `visibility = invisible` | measured | No | An invisible prim still compiles into the scene; nothing prunes it. Blender's hidden objects are excluded before export instead. |
| `purpose = guide`/`proxy` | measured | No | A `guide` prim compiles in like any other, so purpose is not a culling mechanism on this platform. |
| `doubleSided` | observed | Refused | Always written single-sided; Reality Composer Pro's viewport honours the flag and a RealityKit app does not, so relying on it looks right in the editor and wrong on device. |
| `instanceable` references over a shared prototype | measured | Yes | Composes, and the prototype's mesh is written once. See the caveat below. |
| `UsdGeomPointInstancer` | measured | Refused | Survives into the USDZ; what the renderer draws is unsettled. |
| `BasisCurves`, `NurbsCurves`, `NurbsPatch`, `Points`, `TetMesh` | assumed | Refused | No RealityKit geometry. Convert to meshes. |
| `Volume`, `OpenVDBAsset`, `ParticleField` | assumed | Refused | No volume rendering. |

**The instancing caveat.** `realitytool` deduplicates identical meshes, so
three cubes written as separate meshes, as plain references, and as
`instanceable` references all compile to exactly one mesh asset. Asset counting
therefore cannot prove instancing survives as instancing - only that it does
not cost more. What *is* proved, in `tests/integration/test_variants_with_instancing.py`,
is that the USD side is correct: activation and material bindings land on
instance roots, above the proxies where USD forbids authoring.

## Composition

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| `variantSet` on the root prim | measured | Opt-in | Kept in `.usda`, `.usdc` and `.usdz`, and switchable through the USDKit stage APIs. **Discarded by `realitytool`**, so a compiled `.reality` ships one selection. |
| Nested variant sets | measured | Opt-in | Same: kept in the file, discarded on compile. |
| `payload` to a second layer | measured | Opt-in | Composes, and the geometry reaches the compiled `.reality`. **Payload Geometry**. |
| `references` to a second layer | measured | Yes | How localized dependencies arrive. |
| `kind`, `assetInfo`, `extentsHint` | measured | Opt-in | **Asset Kind**; `component` by default. |
| `userProperties` | measured | Yes | Reach the USD stage. **They do not reach a compiled `.reality`** - the scene asset was byte-identical to the control. A USDZ-loading app still reads them. |
| `inherits`, `specializes`, class prims | assumed | No | Nothing in a Blender scene maps to them. |
| Sublayers | assumed | No | The export is one layer, or two with a payload. |

## Materials and shading

Covered in full by
[APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported)
and [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md); only the schema-level
summary belongs here.

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| MaterialX graph declaring 1.39 | observed | Yes | Every translated material. The nodes are checked against the OS library by `scripts/check_shader_implementations.py`. |
| `ND_realitykit_pbr_surfaceshader_2_0` | observed | Yes | The surface a Principled BSDF ends in. |
| `ND_realitykit_unlit_surfaceshader` | observed | Yes | Emission-only materials and the unlit bake modes. |
| `ND_realitykit_hair_surfaceshader` | observed | Yes | Principled Hair BSDF, direct coloring. |
| `ND_realitykit_geometrymodifier_2_0_vertexshader` | observed | Yes | Displacement, object space. |
| `UsdPreviewSurface` | measured | No | Deleted from translated materials; the MaterialX graph is the only surface. Blender generates it first because it carries the bindings. |
| `material:binding` on meshes and subsets | measured | Yes | Including inside variants. |
| MaterialX `pbrlib` closures (`ND_dielectric_bsdf`, `ND_mix_bsdf`) | observed | Refused | They parse and nothing renders them. |

## Textures

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| PNG, JPEG, OpenEXR in a USDZ | observed | Yes | |
| AVIF in a USDZ | observed | Opt-in | RealityKit's validator accepts it; Reality Composer Pro 3 refuses it on import. |
| `sourceColorSpace` tagging | observed | Yes | |
| Wrap and filter modes | observed | Yes | Extend, Clip, Mirror; Closest, Cubic, Smart. |
| UV transforms | observed | Yes | One texture transform per material. |
| UDIM tile sets | assumed | No | Not probed, not authored. |
| Lightmaps (`RealityFoundation.LightmapComponent`) | observed | No | New in the v27 component set. The preflight already reports whether a mesh has a second UV set for Reality Composer Pro's lightmap baker, but nothing is authored, and the `require_lightmap_uv` flag that would promote those reports to errors is read from settings that never define it. |

## Animation and deformation

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| Time samples on `xformOp`s | measured | Opt-in | Compiles to `AnimationLibraryAsset`, `AnimationScene` and `Timeline` assets. |
| `UsdSkel` skeleton and skinning | measured | Opt-in | Compiles to a `Skeleton` asset beside the mesh. |
| `UsdSkelAnimation` joint animation | measured | Opt-in | Part of the same probe. |
| `UsdSkelBlendShape` | measured | Opt-in | Compiles to a `BlendFactor` asset. |
| `RealityKitClipDefinition` animation library | observed | Opt-in | **RCP Clip Library**. Reality Composer Pro flattens the clips on import. |
| Time-sampled mesh points | assumed | Refused | No deforming-points playback. |
| Material, light and camera animation | assumed | No | Only object, armature and shape-key channels are collected. |
| `RealityFoundation.AnimationGraphComponent` | observed | No | v27 component; state machines belong in app code or Reality Composer Pro. |

## Lighting, cameras and environment

This is where the exporter is most conservative, and where the evidence is
thinnest.

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| `UsdLux` `DistantLight`, `SphereLight`, `DomeLight` | measured, inconclusive | Refused | Each one grew the compiled scene (541 → 678, 759, 694 bytes), so the prim is not discarded. Whether it arrives as a light or as an empty entity cannot be told from the artifact. |
| `UsdGeomCamera` | measured, inconclusive | Refused | Same: the scene grew to 719 bytes, which proves arrival and not a camera. |
| `RealityKit.DirectionalLight` component | measured | No | Grew the scene by 192 bytes. A recognised component. |
| `RealityKit.SpotLight` component | measured | No | Grew the scene by 218 bytes. |
| `RealityKit.ImageBasedLight` and `...Receiver` | measured | No | Grew the scene by 168 bytes. |
| `RealityKit.VirtualEnvironmentProbe` | observed | No | In the OS component set. |
| World material / environment lighting | assumed | No | Use the **Lighting & Shadows** bake mode to keep the look. |

**Why a component prim's growth is decisive.** A `RealityKitComponent` child
prim is either consumed as a component or ignored: it never becomes an entity
of its own. The control is `info:id = "RealityKit.MeshSorting"`, a plausible
id that is not a real one - it grew the compiled scene by **exactly zero
bytes**. The four ids above each grew it. So the ids are real, the components
are read, and an `info:id` the platform does not know is dropped in silence.

That last part is the warning for us: a mistyped component id produces a
clean export, a clean compile, and no component.

## RealityKit components

Reality Composer Pro ships `Contents/Resources/component-availability.json`,
which lists **97 components** with the `.reality` file version each appears in
(v25, v26, v27). We author **one**.

| Component | We author | Why it would matter from Blender |
|---|---|---|
| `RealityKit.Collider` (`CollisionComponent`) | Yes | From Blender Rigid Body. Compiles to a real `CollisionShape` asset. Box only. |
| `InputTargetComponent` | No | A collider without an input target is not tappable. These two travel together in nearly every interactive asset. |
| `HoverEffectComponent` | No | The standard visionOS affordance for "this is interactive". |
| `BillboardComponent` | No | Labels and cards that face the viewer. Nothing in Blender implies it. |
| `GroundingShadowComponent` | No | Cheap contact shadow; the usual fix for an object that looks like it floats. |
| `OpacityComponent` | No | Entity-level fade, separate from material opacity. |
| `PhysicsBodyComponent` | No | We author the collider from a Blender rigid body but not the body itself, so the collider is static. Blender already holds mass, friction and restitution. |
| `ImageBasedLightComponent` | No | Per-asset environment lighting. |
| `TextComponent` | No | Blender text objects are not exported at all. |
| `SpatialAudioComponent`, `AmbientAudioComponent`, `AudioLibraryComponent` | No | Blender speakers are not exported. |
| `ParticleEmitterComponent` | No | No Blender particle mapping. |
| `MeshInstancesComponent` (v26) | No | The platform's own instancing path, distinct from USD instanceable references. |
| `LevelOfDetailComponent` (v27) | No | Blender has no LOD concept to read. |
| `AccessibilityComponent` | No | Blender's USD exporter takes an accessibility label and description; the exporter passes empty strings. |
| `RealityKitCustomComponent` | No | Project-specific Swift components; not an exporter's job. |

## Physics

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| `RealityKitComponent` collider | measured | Opt-in | **Colliders**, on by default. Box shape, sized from the object's bounds. |
| `UsdPhysics` `CollisionAPI`, `RigidBodyAPI`, `MassAPI` | measured | No | Validates, packages, compiles - and arrives as nothing. Apple's USD build ships no `usdPhysics` plugin at all, so the schema is not even registered. |
| Capsule, sphere, convex, mesh collider shapes | observed | No | Reality Composer Pro's inspector offers box, capsule and sphere. Only Box's USD spelling is verified here. |
| Joints, character controller, force effects | observed | No | In the component set; nothing in Blender maps cleanly. |

## Audio, video and text

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| `UsdMediaSpatialAudio` | assumed | No | `usdMedia` is in Apple's USD plugin set. Blender speakers are not exported. |
| `VideoPlayerComponent` | observed | No | |
| `TextComponent` | observed | No | |

## Metadata and packaging

| Feature | RealityKit | We author | Notes |
|---|---|---|---|
| Y-up, metres, `-Z` forward | observed | Yes | Always. A RealityKit app converts neither. |
| `defaultPrim` | measured | Yes | |
| USDZ packaging, referenced textures only | measured | Yes | |
| Crate 0.8 / `#usda 1.0` | observed | Yes | Well below the ceilings Apple's build reads. |
| `.reality` compilation | measured | No | `realitytool` is Xcode's, run by the app project, not by this exporter. |

## The gaps, ranked

Ordered by what closing them would buy, not by how hard they are.

1. **`InputTargetComponent` beside the collider.** We already decide which
   objects get a collider; an asset whose colliders cannot be tapped is half a
   feature, and the component is a two-line prim. Measure the `info:id` first.
2. **`PhysicsBodyComponent` from the Blender rigid body.** We read the rigid
   body and author only its shape, while Blender holds mass, friction,
   restitution and whether the body is passive. The data is already in hand.
3. **Lights.** The honest statement today is that we refuse `UsdLux` on
   evidence that does not support refusing it, and that the component path
   (`RealityKit.DirectionalLight`, `RealityKit.SpotLight`) is real and
   unused. Settling it needs a probe that counts what a RealityKit build
   actually draws, not another artifact read.
4. **More collider shapes.** Blender's collision shape already says sphere,
   capsule, cylinder, convex hull or mesh, and we approximate all of them with
   a box. Reality Composer Pro offers three of them.
5. **`GroundingShadowComponent`.** One toggle, and the usual answer to "my
   object looks like it is floating".
6. **Capsule and sphere `shapeType` spellings.** Needed by 4, and cheap to
   measure with the existing harness.
7. **`AccessibilityComponent`.** Blender's exporter already has the two fields
   and we pass empty strings.
8. **Cameras.** Same evidential hole as lights, and less often wanted in an
   asset than a light rig is.

Deliberately not pursued: `PointInstancer`, volumes, curves and particle
fields, all of which the preflight refuses because there is no RealityKit
geometry behind them; `UsdPhysics`, which is not a registered schema in
Apple's USD build; and `RealityKitCustomComponent`, which belongs to the app
project rather than to an exporter.

## What this page cannot tell you

Every measurement here reads a file. None of it says what a RealityKit app
draws, which needs a device or a build, and none of it covers macOS Quick
Look, which has its own behaviour - a USDZ shipping a variant selection has
been observed displaying a different variant. The inconclusive rows above are
inconclusive for exactly this reason, and no further artifact reading will
close them.
