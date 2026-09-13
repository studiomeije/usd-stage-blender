# Feature support

This page tells you which Blender features survive an export, and what happens
to the ones that don't. Read it before you build a scene you intend to ship.

*Applies to: Blender 5.2 LTS; Reality Composer Pro 3; macOS 27, iOS 27,
iPadOS 27, tvOS 27, visionOS 27.*

USD Stage for Blender writes USD — `.usda`, `.usdc`, or `.usdz`. Blender's USD exporter
runs first; this add-on then rewrites materials for RealityKit, normalizes the
stage, and validates it before anything reaches your output path.

## How to read the tables

| Word | Meaning |
|---|---|
| **Yes** | The feature reaches the output intact. |
| **Partial** | The feature is carried, but constrained or lossy. The Notes say how. |
| **Bake** | The direct export stops and names the node; exporting with a bake profile captures it. |
| **Refused** | The export stops and tells you what it could not handle. Nothing is written. |
| **Dropped** | The data does not reach the output. The Notes say whether you are warned. |

**Refused and Dropped are not the same problem.** A refusal is a decision: you
get a message naming the material, mesh, or node, and you fix it. A drop means
the export succeeds and the data is gone. Warnings appear in the export result,
in Blender's status report, and in the diagnostics sidecar — but a few drops
emit nothing at all. Those are listed at the end of this page.

## Geometry

| Feature | Supported | Notes |
|---|---|---|
| Polygon meshes | Yes | Quads and n-gons keep their face structure unless **Triangulate Meshes** is on. |
| Mesh normals | Yes | |
| UV maps | Yes | Every UV map is exported. The render UV map is written as `st`; the others keep their names. |
| Vertex colors | Yes | |
| Subdivision surfaces | Yes | **Best Match** writes the base mesh with its Catmull-Clark scheme, and RealityKit subdivides it when rendering; **Tessellate** writes the subdivided mesh. |
| Multi-material meshes | Yes | Each material becomes a `GeomSubset` binding. |
| Shape keys (blend shapes) | Yes | |
| Armature skinning | Yes | Exported through USD's skeletal schema. |
| Collection instances | Yes | With **Instancing** on (the default), each instance is an instanceable reference to one shared prototype, so the mesh is written once. Reality Composer Pro imports them as instances of one prototype entity, keeping each instance's transform and the prototype's materials. |
| Object hierarchy | Yes | Object transforms and parenting are preserved. |
| Curves, hair, point clouds, volumes | Dropped | These object types are never exported and nothing warns. |
| Cameras and lights | Dropped | Author them in Reality Composer Pro; nothing warns that they were skipped. |

## Materials

Every translated material ships as a MaterialX graph declaring MaterialX 1.39,
with no UsdPreviewSurface network beside it (see
[MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network)).
This table covers the surface a material ends in and the Principled BSDF
inputs; every node that feeds them is in [Shader nodes](#shader-nodes).

| Feature | Supported | Notes |
|---|---|---|
| Principled BSDF | Yes | Terminates in RealityKit PBR Surface 2. |
| Diffuse, Metallic, Sheen, Subsurface Scattering BSDFs | Yes | Exported as the Principled BSDF Cycles evaluates each as, so the Principled limits below apply, reported under the node's own socket names: a Diffuse Roughness above 0 warns. A Metallic BSDF needs F82 Tint Fresnel and a white Edge Tint. Subsurface Scattering warns unless its method is Random Walk (Skin). |
| Glossy BSDF | Partial | A metallic surface of its Color. Glossy has no Fresnel, so its rim brightens toward white, and the export says so. |
| Two of the shaders above in a Mix Shader | Partial | Blended into one surface. Exact when the only input that differs is Base Color or Metallic; otherwise every differing input is named in a warning. Differing linked normals are refused. |
| Principled Hair BSDF | Partial | Direct coloring only; terminates in RealityKit's hair surface. See the Shader nodes table. |
| Emission on the Material Output, or a material without nodes | Yes | Terminates in RealityKit's unlit surface. |
| Unlit bake modes | Yes | **RealityKit Unlit ▸ Material Color Only** and **Lighting & Shadows** author the unlit surface. See [BAKING.md](BAKING.md#2-the-bake-modes). |
| RealityKit node groups | Yes | A group from the add-on's own catalog on the output exports as that RealityKit material. |
| Base Color, Metallic, Roughness | Yes | Constant or linked. |
| Normal, Coat Normal | Yes | Constant or linked; any normal expression is converted to the tangent space the surface takes. |
| Specular IOR Level, IOR | Partial | Constant or linked, folded into the dielectric reflectance Cycles computes from them. RealityKit caps that reflectance at 0.08, an IOR of about 1.79 at the default level; on a surface that is not fully metallic a warning says so wherever the cap is exceeded, or could be because either input is linked. |
| Diffuse Roughness | Dropped | The surface shades as Lambert, with a warning, because RealityKit's rough diffuse is further from Cycles than Lambert is. |
| Emission Color and Strength | Yes | Constant or linked. |
| Alpha (opacity) | Yes | Constant or linked. A Mix Shader with a Transparent BSDF also becomes opacity. |
| Alpha cutout | Partial | Opt-in: set the material custom property `usd_stage_alpha_cutout_threshold` to a number from 0 to 1 on a transparent material. Blender's render method never implies a cutout. |
| Subsurface | Partial | Weight, Radius, Scale and Anisotropy are carried. A Subsurface IOR other than 1.4 is refused. |
| Sheen | Partial | Weight and Tint are carried while Weight is on. RealityKit's sheen replaces the specular lobe, so a warning says the highlight and metallic reflection disappear, unless the base is black, non-metallic and at Specular IOR Level 0, as in the Sheen BSDF. A linked Sheen Roughness, or one other than 0.5, is refused. |
| Coat | Partial | Weight, Roughness and IOR constants are carried. Linked coat controls and a non-white Coat Tint are refused. |
| Specular Tint | Partial | White only. A coloured or linked tint is refused; an overbright grey is refused unless **Clamp Overbright Specular Tint** clamps it. |
| Transmission, Thin Wall, Thin Film, Anisotropic | Refused | Export stops and names the Principled input to bake or clear. |

## Shader nodes

Every node in Blender's shader editor, by its menu name, and what the export
does with it. "Yes" exports as an editable MaterialX graph; "Partial" exports
with a named limitation; "Bake" stops the direct export and points at a bake
profile; "Refused" stops the export with the node named,
and a bake captures it only where the bake mode says so. The export message
always names the node and, where one exists, the remedy.

| Node | Supported | Notes |
|---|---|---|
| Material Output | Yes | The output Cycles renders: the active one targeting All or Cycles. A material without one, or with nothing linked to Surface, is refused. Displacement exports as a geometry modifier (see below); Volume is not exported. |
| Principled BSDF | Yes | See the Materials table for which inputs reach the surface. |
| Emission | Yes | An Emission node on the output makes an unlit material. |
| Image Texture | Yes | Any UV map, or coordinates computed by other nodes; Extend, Clip and Mirror; Closest, Cubic and Smart filters; Box projection with Projection Blend, weighted as Cycles weights it. Color and Alpha read as Cycles hands them (see [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#image-color-and-alpha)). A premultiplied image whose Alpha output is used, read at computed coordinates, is refused. |
| Environment Texture | Yes | Projected as Cycles projects it, Equirectangular or Mirror Ball, from its Vector or, unlinked, the world position. Not a reflection environment. A premultiplied image whose Alpha output is used is refused. |
| Noise Texture | Partial | 3D fBm with Blender's range, octave count, fractional Detail and Color output, but MaterialX's hash, so a warning says the pattern differs. Other dimensions and types, Distortion, and a linked or driven Detail or Roughness are refused. |
| Voronoi Texture | Partial | 3D Euclidean F1 Distance with Detail 0, Normalize honoured, and the same hash warning. The Color and Position outputs, other features and metrics, and a Detail above 0 are refused. |
| Gradient Texture | Yes | All seven types, exactly. An unlinked Vector samples object position in place of Generated coordinates, with a warning. |
| Normal Map | Yes | Tangent space, OpenGL or DirectX, constant or linked Strength, from any colour source, as Cycles computes it. Object or world space, a UV Map field naming anything but the render UV map, and Base Original with a Strength other than 1 on a material that displaces vertices are refused. |
| Color Attribute | Yes | Reads the mesh's colour attribute. |
| Texture Coordinate | Partial | UV, Object and Generated export; Generated lacks Blender's bounding-box scaling and warns. Object with the node's Object field set, Window, Camera, Reflection and Normal are refused. |
| Geometry | Partial | Position, Normal, Incoming and Backfacing export. Tangent (Blender's is a radial tangent around the object's Z axis, not the UV tangent), True Normal, Parametric, Pointiness and Random Per Island are refused. |
| Fresnel | Yes | Blender's own formula. A linked Normal is refused. |
| Layer Weight | Yes | Blender's own formula with a constant Blend. A linked Normal or Blend is refused. |
| UV Map | Yes | The named UV map as a coordinate for any node. A name that a mesh using the material lacks, that is the render UV map on only some of those meshes, or that is not a valid primvar name is refused, as is From Instancer. |
| Mapping | Yes | A Point mapping of UVs feeding an Image Texture, with no X or Y rotation and no zero X or Y scale, is the material's one texture transform, and a second distinct one is refused. Everywhere else, including the Texture type and linked sockets, it is computed as vector math in all four types and does not count toward that limit. |
| RGB, Value, Boolean, Integer, Vector | Yes | Constants. A Value output driven by a `frame` expression exports as the time reader. |
| Mix, Mix Color | Partial | A plain mix, or a multiply, add or subtract of two linked inputs, or a constant Factor of 0 or 1 with a passthrough. A linked Factor may blend two constant colours. Clamp Factor, Clamp Result and Non-Uniform are honoured, and Float and Vector mixes ignore the blend type, as in Blender. Other colour blend modes and Rotation mixes are refused. |
| Math | Partial | Twenty-four operations, with Cycles' guards for division, roots, powers, logarithms and inverse trigonometry; the rest are refused with the operation named. Use Clamp is honoured. |
| Vector Math | Partial | Twenty-seven operations; Modulo, Wrap and Snap are refused with the reason named. |
| Clamp | Yes | Min Max and Range. |
| Map Range | Partial | Float and Vector, Linear interpolation only, with Blender's results for inverted and equal bounds. Stepped Linear, Smooth Step and Smoother Step are refused. |
| Color Ramp | Partial | RGB colour mode with Linear, Constant or Ease interpolation, any number of stops. |
| Hue/Saturation/Value, Brightness/Contrast, Invert Color, RGB to BW | Yes | Cycles' formulas. Hue/Saturation/Value carries RealityKit's half-precision hue error of at most 2.4e-4 (see [APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#colour-nodes-compute-in-half-precision)). |
| Separate Color, Combine Color | Yes | RGB mode, and HSV for Separate Color. HSL, and Combine Color in any mode but RGB, are refused. |
| Separate XYZ, Combine XYZ | Yes | |
| Vector Rotate | Yes | Every rotation type, with Center and Invert. |
| Normal | Partial | The Normal output exports its constant direction; the Dot output is refused. |
| Vector Transform | Bake | Object, World and Camera space need the render's matrices, which no exported node reads. |
| Bump | Yes | The height's gradient along the mesh's UVs, as Cycles computes it; the mesh needs a UV map. A height that reads a mesh attribute, or a Normal fed by anything but another Bump, is refused. |
| Displacement, Vector Displacement | Partial | Object space only, exported as a RealityKit geometry modifier that moves vertices; normals are not recomputed. World space, tangent-space vector displacement, a linked Normal, or a Bump Only displacement method is refused with the remedy named. |
| Checker Texture | Yes | Cycles' checker. Unlinked, it samples object position in place of Generated coordinates, with a warning. |
| Brick, Wave, Magic, White Noise, Gabor, Sky, IES textures | Bake | The bake modes capture them. |
| RGB Curves, Float Curve, Vector Curves | Bake | The bake modes capture them. |
| Gamma | Yes | Cycles' gamma. |
| Camera Data | Yes | View Vector, View Z Depth and View Distance, from RealityKit's model-to-view matrix. |
| Attribute | Partial | Geometry attributes: Float and Vector attributes on Point or Face Corner, the mesh's first colour attribute, and UV maps. Object, Instancer and View Layer attributes, other attribute types and the Face domain are refused. |
| Object Info | Partial | Random only, the value Cycles gives each object, written per mesh from the object's name. Instanced copies of one mesh share a value. Other outputs are refused. |
| Blackbody, Wavelength, Light Falloff, Radial Tiling | Bake | The bake modes capture them. |
| Shader to RGB | Refused | EEVEE only; Cycles, which runs every bake, cannot evaluate it. |
| Mix Shader | Partial | Two of Principled, Diffuse, Glossy, Metallic, Sheen and Subsurface Scattering blend into one surface (see the Materials table). One of them mixed with a white Transparent BSDF becomes opacity, factor constant or linked. A mix with an Emission, or with any other shader, is refused; a bake profile captures it where [BAKING.md](BAKING.md#2-the-bake-modes) says. |
| Add Shader | Partial | One of the shaders above plus an Emission shader adds the emission. Refused over a Principled BSDF whose Alpha is below 1 or linked, because RealityKit dims emission by opacity; anything else is refused with the operands named. |
| Transparent BSDF | Partial | Only as the Mix Shader operand above, with a white Color. |
| Specular, Toon, Translucent, Glass, Refraction, Ray Portal BSDFs | Refused | No RealityKit surface; the Materials table lists the shaders that export. |
| Principled Hair BSDF | Partial | Direct coloring only, exported as RealityKit's hair surface: colour and roughness, and under the Huang model the two reflection weights. The transmission lobe, cuticle Offset, radial roughness and the melanin controls have no input on it, and the export says so. Melanin or Absorption coloring is refused. The strand direction is +U of the mesh's UV map, so strands must run along +U; use it on mesh hair cards. |
| Hair BSDF | Refused | A single-lobe Cycles shader with no RealityKit form; use Principled Hair BSDF. |
| Volume Absorption, Volume Scatter, Principled Volume, Volume Coefficients | Refused | RealityKit has no volume shading. |
| Light Path, Ambient Occlusion, Tangent, Bevel, Wireframe, Curves Info, Particle Info, Point Info, Volume Info, Holdout | Refused | No reader with settled semantics on the platform. |
| World Output, Light Output, AOV Output, Background | Refused | Not part of a material. |
| Frame, Reroute | Yes | Ignored and passed through. |
| Node groups | Partial | RealityKit node groups from the add-on's own catalog export; other groups are refused. |

## Textures

| Feature | Supported | Notes |
|---|---|---|
| Several textures per material | Yes | No per-role limit. |
| Source texture formats | Partial | PNG, JPEG and EXR pass through; AVIF and other readable formats are converted to PNG. |
| Baked texture format | Yes | PNG by default. AVIF output is opt-in through **Image Format**, and Reality Composer Pro 3 cannot import it. |
| USDZ image formats | Partial | A `.usdz` may contain only PNG, JPEG, EXR and AVIF; anything else fails validation. |
| Color-space tagging | Yes | See [MATERIAL_TRANSLATION.md](MATERIAL_TRANSLATION.md#color-space). |
| Working color space other than Linear Rec.709 | Refused | Every colour is written as Linear Rec.709 without conversion, so a Linear Rec.2020 or ACEScg file stops the export. |
| Baked textures | Yes | All three bake modes; see [BAKING.md](BAKING.md). |

A USDZ carries only the images the stage references. The platform's format
rules are in
[APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#texture-formats).

## Animation

| Feature | Supported | Notes |
|---|---|---|
| Object translation, rotation and scale | Yes | Sampled per frame. |
| Armature and bone animation | Yes | Including bone scale. |
| Shape key animation | Yes | Weight curves are exported. |
| Several Actions as one timeline | Yes | Actions are concatenated into takes, each with its own frame span. |
| RCP clip library | Partial | Opt-in through **RCP Clip Library**. Reality Composer Pro flattens the named clips on import; see [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#the-experimental-rcp-clip-library). |
| Stashed Actions | Dropped | Warned by name — push a stashed take to an NLA strip to include it. |
| Fractional Action ranges | Partial | Ranges are quantized to whole frames and time-scaled, with a warning naming the Action. |
| Time-sampled mesh points | Refused | RealityKit cannot play deforming points; validation stops the export. |
| Material, light and camera animation | Dropped | Only object, armature and shape-key channels are collected, and nothing warns. |

## Scene and packaging

| Feature | Supported | Notes |
|---|---|---|
| Y-up axis and meter scale | Yes | Always written. See [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#1-the-apple-spatial-contract). |
| Root prim name | Yes | Set it in **Root Prim**. |
| Selection-only export | Yes | An empty selection is refused rather than exported as an empty file. |
| Custom properties | Yes | Reach the stage as `userProperties`. |
| Empties as transforms | Yes | Exported as Xforms. |
| Double-sided meshes | Dropped | Always written single-sided; each changed mesh is noted in the diagnostics sidecar. |
| World material and environment lighting | Dropped | Never exported; use the **Lighting & Shadows** bake mode to keep the look. |
| USDZ packaging | Yes | Includes only referenced textures. |
| Diagnostics sidecar | Yes | Always written on failure; turn on **Keep Success Diagnostics** to keep it otherwise. |

Exports are not byte-reproducible; see
[EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#exports-are-not-byte-reproducible).

## Dropped without a warning

Everything here leaves your scene without any message. The export reports
success and the data is not in the file. This is the list to check when an asset
looks wrong and nothing told you why.

- Cameras and lights.
- Curve, hair, point-cloud and volume objects.
- The world material and its environment lighting.
- Animation on materials, lights and cameras.
