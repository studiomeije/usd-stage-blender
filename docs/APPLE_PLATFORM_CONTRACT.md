# USD and MaterialX support on Apple platforms

This page explains which USD and MaterialX features Reality Composer Pro and
RealityKit accept, and why the exporter's fixed choices follow from them.

*Applies to: macOS 27, iOS 27, iPadOS 27, tvOS 27, visionOS 27; Reality
Composer Pro 3; Xcode 27.*

## One stack, everywhere

On the OS 27 platforms, Reality Composer Pro and RealityKit share one USD
library and one MaterialX library:

- Apple's own OpenUSD build serves the RealityKit runtime, Reality Composer
  Pro, and the `usdchecker` and `usdzip` command-line tools.
- The ShaderGraph engine and its MaterialX node library ship with RealityKit
  in the OS, and Reality Composer Pro uses the identical library. A file format
  or node the editor accepts, the device accepts.
- MaterialX materials compile on the device when a USDZ loads; a `.reality`
  file only compiles them ahead of time.

The gap is in what surrounds that stack. Reality Composer Pro converts scene
units and up axis on import, and its viewport honors a mesh's `doubleSided`
flag. A RealityKit app loading a USDZ directly does neither, so a file relying
on the editor looks right there and wrong on device. The exporter therefore
always writes Y-up, meters, and single-sided meshes; see
[the Apple spatial contract](EXPORT_PIPELINE.md#1-the-apple-spatial-contract).
With those fixed, what renders in Reality Composer Pro renders on every OS 27
device.

Xcode's `realitytool` stands outside the stack: it links neither
`ShaderGraph.framework` nor the `libtm-*` libraries. It proves a file parses
and packages, not that a material renders, and `usdchecker --arkit --strict`
proves no more. On a rigged-character export, `realitytool compile --platform
xros` exited 0 with a successful shader graph, `usdchecker` reported
`Success!`, and every nodedef resolved to a compiled implementation, yet
Reality Composer Pro 3 refused the material with `Couldn't find compiled
shader graph buffer`. To judge a material, import the asset into Reality
Composer Pro and look at it.

## Which USD versions are supported

### File formats

| Format | Reads up to | Writes by default |
|---|---|---|
| Binary (`.usdc`, inside `.usdz`) | crate 0.14 | crate 0.8 |
| Text (`.usda`) | `#usda 1.2` | `#usda 1.0` |

"Crate" is USD's binary file format. Its version rises only when a file uses a
feature that needs it: path expressions (0.10), relocates (0.11), animation
splines (0.12 and 0.13), array edits (0.14). Blender 5.2 writes crate 0.8 and
`#usda 1.0`, well below both ceilings; only the file format written matters.
The exporter writes `.usdz` by default, and `.usda` or `.usdc` on request.

### The library behind them

Apple's USD build is OpenUSD 24.07 with features added from releases up to
roughly OpenUSD 25.11. Nothing introduced in OpenUSD 26.x is present.

### Texture formats

The USDZ specification admits PNG, JPEG, OpenEXR, and AVIF members, and
RealityKit's package validator accepts all four. Reality Composer Pro 3 refuses
a valid AVIF on import with `Unhandled image format, header corrupted or file
extension not accurate`, beside a `.usdc` or inside a `.usdz` alike. The
exporter therefore bakes to PNG by default and converts AVIF sources to
PNG. AVIF output is opt-in, for packages only RealityKit loads at runtime; see
[texture settings](SETTINGS.md#texture-settings).

## Which MaterialX nodes are supported

A MaterialX material is a graph of *nodedefs* (node definitions such as
`ND_image_color3`). RealityKit resolves them from an OS library combining
MaterialX 1.39.4, a 1.38 compatibility set, and Apple's RealityKit nodes. It
keeps one nodedef store per declared MaterialX version, and a node or input
present at one can be absent at the other. Every export declares MaterialX
1.39. `Plugin/manifest/rcp_nodedef_input_gaps.json` records where each store
differs from the exporter's node manifest.

A nodedef can resolve and still have no compiled shader behind it. RealityKit
then discards the material's whole shader graph, texture bindings included,
and substitutes default PBR. Everything the exporter authors resolves at 1.39
and has an implementation: the surfaces `ND_realitykit_pbr_surfaceshader_2_0`,
`ND_realitykit_hair_surfaceshader`, and `ND_realitykit_unlit_surfaceshader`;
image readers with wrap and filter modes; UV transforms; normal-map decoding;
and channel reads. A channel read converts the color to a vector
(`ND_convert_color4_vector4`, for example) and takes a dot product with a unit
mask (`ND_dotproduct_vector4`). Two nodes that resolve without an
implementation are worth knowing because MaterialX offers them for common
operations: `ND_fract_float`, which the exporter replaces with the value minus
its floor, and the `ND_curveadjust_*` family. For translated Blender nodes, see
[shader nodes](FEATURE_SUPPORT.md#shader-nodes).

World-space readers such as `ND_normal_vector3` and `ND_position_vector3` with
`space = world` return RealityKit's Y-up axes, including the export root's
rotation, not Blender's, and they return the space of the entity's anchor.
The surface stage also reads RealityKit's matrices, such as
`ND_realitykit_surface_world_to_view`, `ND_realitykit_surface_model_to_view`
and `ND_realitykit_surface_model_to_world`, whose outputs author as USD
`matrix4d`. The view matrices take the true world, not the anchor's space, so a
world-space position reader and `world_to_view` agree only while the anchor is
the identity; object-space position through `model_to_view` is exact anywhere.
Geometric primvars are read through `ND_geompropvalue_*`. The exporter reads
float and vector mesh attributes that way, and the primvars it writes itself:
Object Info Random and the UV derivatives Bump needs. Colour attributes go
through the vertex-colour reader instead, because Reality Composer Pro rejects
a material that reads one through `geompropvalue`.

The MaterialX *pbrlib* closure nodes (`ND_dielectric_bsdf`, `ND_mix_bsdf`,
displacement, and similar) parse without error, but no Apple platform renders
them. The node manifest marks them `editor_unresolvable`; the exporter never
selects them, and its preflight rejects any that appear.

Translated materials carry only this graph
([no UsdPreviewSurface network](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)),
and their textures follow [color space](MATERIAL_TRANSLATION.md#color-space).

### Colour nodes compute in half precision

RealityKit's MaterialX 1.39 library computes its colour-typed nodes
(`ND_mix_color3`, `ND_multiply_color3`, `ND_combine3_color3`, `ND_rgbtohsv_color3`
and the related `color3` nodes) in 16-bit float, where MaterialX specifies
float. Anything routed through a colour node inherits that precision:

| Data through a colour node | Result |
|---|---|
| A value above 65504 | overflows to infinity |
| A fine-grained value, such as a UV coordinate built with Combine Color | quantised to half-float steps |
| A constant stored as a half literal, such as the 1/360 in `ND_rgbtohsv_color3` | off by the literal's rounding (a hue error of at most 2.4e-4) |

Only the colour-typed nodes are affected, so data that is not a colour
belongs in Combine XYZ, Vector Math and Math, which author float and vector
nodes, rather than in Combine Color or a colour Mix.

### How the surfaces read their inputs

RealityKit reads these inputs in ways their names do not suggest, which
decides what the exporter authors
([what the surface carries](MATERIAL_TRANSLATION.md#what-the-surface-carries)).

| Surface input | How RealityKit reads it |
|---|---|
| PBR Surface 2 `sheenColor` | Any authored value, black included, switches the surface to a sheen shading that replaces the specular lobe: metals lose their reflection and dielectrics their highlight. |
| PBR Surface 2 `baseDiffuseRoughness` | Any authored value, 0 included, switches the diffuse to a rough model that lights it from the environment about a third as brightly as the default Lambert (a 0.5 grey renders like a 0.17 grey). |
| PBR Surface 2 `specular`, `specularIOR` | Without `baseDiffuseRoughness`, the dielectric reflectance at normal incidence is F0 = 0.08 × saturate(`specular`) and `specularIOR` is ignored. With it, F0 = saturate(`specular`) × F0(`specularIOR`). |
| PBR Surface 2 `specularWeight` | Scales the whole specular lobe, metals and grazing angles included. |
| PBR Surface 2 `opacity` | Dims the surface's emission along with everything else. |
| Hair surface `tangent` | A direction in the surface's UV tangent frame, the frame a normal map uses. The default (0, 1, 0) is +V; (1, 0, 0) is +U. (0, 0, 1), or an object-space direction, gives no strand highlight. |
| Hair surface `primarySpecular`, `secondarySpecular` | Each lobe grows linearly with its weight and saturates at 1; 0.5 is the 4% reflectance of a dielectric. |
| Hair surface `secondarySpecularColor` | Multiplies the secondary lobe after a saturate, and defaults to black, so the secondary lobe draws nothing without it. |
