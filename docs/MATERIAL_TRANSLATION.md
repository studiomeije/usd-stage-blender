# Material translation

This page explains how the exporter turns a Blender material into a RealityKit
MaterialX ShaderGraph, including every place where it substitutes, infers, or
drops something on your behalf. Read it to predict what a material exports as,
and to know which Blender controls survive the trip.

*Applies to: Blender 5.2; Reality Composer Pro 3.0.*

Function names in parentheses point at the code that makes each decision. Related:
[ARCHITECTURE.md](ARCHITECTURE.md), [CLI.md](CLI.md).

## Contents

1. [The export pipeline](#the-export-pipeline)
2. [The surface](#the-surface)
3. [Decisions the exporter makes for you](#decisions-the-exporter-makes-for-you)
4. [Which Blender nodes export](#which-blender-nodes-export)
5. [The texture pipeline](#the-texture-pipeline)
6. [Diagnostics and errors](#diagnostics-and-errors)

---

## The export pipeline

A material crosses five stages. Each can stop the export, and each fails in a
different way.

| # | Stage | Code | Runs on | Failure mode |
|---|-------|------|---------|--------------|
| 1 | Validate | `Plugin/nodes/validate.py` | Blender node tree | `UNSUPPORTED_MATERIAL_NODES`, before any USD is written |
| 2 | Extract | `Plugin/export/materials/extract/` | Blender node tree | recorded per material, batched into one `RuntimeError` |
| 3 | Build graph | `Plugin/export/materials/graph.py` | extracted payload | recorded per material, batched |
| 4 | Author USD | `Plugin/export/materials/author.py`, `textures.py` | USD stage | recorded per material, batched, stage rolled back |
| 5 | Preflight | `Plugin/export/realitykit_preflight.py` | composed USD stage | `RuntimeError` listing preflight issue codes |

Stages 2–4 run inside `rewrite_materials` (`Plugin/export/materials/rewrite.py`),
one step of `process_usd_stage` (`Plugin/export/postprocess_usd.py`). Preflight
is the last step before the stage is saved.

### Validate

Validation (`validate_material`) classifies each node that contributes to the
active Material Output, checks Principled inputs against
[what the surface carries](#what-the-surface-carries), and enforces one texture
transform per material. Every caller, from the CLI to the Export button and
the bake runner, passes `strict=True`, under which most warnings become errors,
so `usdstage validate` previews the export gate faithfully.

### Extract

Extraction reduces the node tree to a flat dictionary plus an `input_graphs`
map of expression trees for anything that is not a constant or a single
texture (`extract_blender_material_data`). It classifies the material into one
of five `type` values, which decide the graph built next
(`_build_material_graph`):

| `type` | Trigger | Graph built |
|--------|---------|-------------|
| `principled` | active output drives a Principled BSDF, or a supported [shader closure](#shader-closures) over one | PBR Surface 2 |
| `hair` | active output drives an exportable Principled Hair BSDF | hair surface (`build_hair_material`) |
| `emission` | active output drives an Emission node | unlit |
| `rk_graph` | active output drives an `RK_*` node group whose graph can be rebuilt | that graph, passed through |
| `rk_group` | active output drives any other `RK_*` node group | that RealityKit nodedef |

When the bake pipeline forces unlit output, `principled`, `hair` and
`emission` all build the unlit graph.

Only the shader wired to the Material Output **Cycles renders** contributes:
the active output whose target is All or Cycles (`_active_output_node`, as
`get_output_node('CYCLES')`), so an EEVEE-only output is skipped even when
active, and a disconnected Principled or Emission node changes nothing. The
bake renders the same output. A material with no such output, or with nothing
linked to its Surface, renders no surface in Cycles, and validation refuses it
by name (`surface_output_refusal`). An unresolved input
anywhere in an expression makes the whole expression unresolved
(`_make_node_expr`), and the material fails with
`Material graph contains unresolved input(s): ...` naming the chain.

### Build graph

The graph builder (`MaterialXGraphBuilder`) selects the surface *nodedef* (a
MaterialX node definition, the typed signature a shader node references), maps
Blender parameters onto its inputs, and filters out everything the surface
cannot carry. Every graph declares MaterialX 1.39 (`_MATERIALX_VERSION`). The
builder rejects a graph that needs more than one distinct 2D texture transform
(`require_realitykit_mapping_contract`).

### Author USD

Authoring turns the graph into `UsdShade.Shader` prims under the existing
`Material` prim (`create_materialx_material`). Where types or channels do not
line up, it inserts `convert`, `dotproduct` (a single-channel read),
`combine3`, `place2d` (MaterialX's 2D texture transform) and normal-decode
nodes.

Authoring is transactional: every material is built before the stage receives
its first MaterialX opinion, and a failure restores a backup of the edit layer.
The surface is authored at the existing prim path, so `material:binding` is
left alone and inactive variant selections survive.

Color-space failures surface here: the validator never inspects an image's
color space, so an image in a color space RealityKit has no token for
validates clean and then fails at authoring.

### Preflight

Preflight inspects the **composed USD stage**, not the Blender graph
(`validate_stage`), and its errors abort the export. Its texture, MaterialX and
binding codes, with the rest, are listed in
[EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#the-composed-stage-preflight).

---

## The surface

Every translated material terminates in one MaterialX surface; there is
nothing to select.

| Surface | Nodedef | `surfaceProfile` | Used for |
|---------|---------|------------------|----------|
| PBR Surface 2 | `ND_realitykit_pbr_surfaceshader_2_0` | `realitykit_pbr2` | every lit material |
| Hair surface | `ND_realitykit_hair_surfaceshader` | `realitykit_hair` | a Principled Hair BSDF; see [Hair](#hair) |
| Unlit surface | `realitykit_unlit_surfaceshader` | `realitykit_unlit` | see below |

### When a material exports as unlit

1. **Material shape.** A material whose active output is an Emission node is
   unlit without further notice.
2. **`force_unlit_materials`.** A hidden scene property only the bake pipeline
   sets (`resolve_force_unlit`): `LIT_ALBEDO` builds PBR, the other modes build
   unlit. See [BAKING.md](BAKING.md#2-the-bake-modes).

The unlit surface carries only `color`, `opacity`, `opacityThreshold` and
`hasPremultipliedAlpha`; other linked inputs are dropped with a warning naming
them (`_unlit_input_graphs`), and other constants are not mapped. A Principled
material built unlit takes its Base Color as `color`, textured or constant,
never scaled by Emission Strength, which scales only the emission.

### What the surface carries

`_map_realitykit_pbr2_inputs` maps Principled inputs onto the surface's 30
declared inputs. `bentNormal` and `bakedIndirectIrradiance` have no Blender
source, and `baseDiffuseRoughness`, `specularIOR` and `specularWeight` are
never authored, because of how RealityKit reads them
([how the surfaces read their inputs](APPLE_PLATFORM_CONTRACT.md#how-the-surfaces-read-their-inputs)).

| Blender Principled input | PBR Surface 2 input |
|---|---|
| Base Color, Metallic, Roughness | `baseColor`, `metallic`, `roughness` |
| Normal, Coat Normal | `normal`, `clearcoatNormal`, in tangent space ([normal maps](#normal-maps)) |
| Emission Color × Strength | `emissiveColor` |
| Alpha, cutout threshold, premultiplied alpha | `opacity`, `opacityThreshold`, `hasPremultipliedAlpha` |
| AO (baked) | `ambientOcclusion` |
| Specular IOR Level and IOR, folded into one reflectance | `specular` ([specular and IOR](#specular-and-ior)) |
| Specular Tint (achromatic, ≤ 1) | `specularColor` |
| Coat Weight / Roughness / IOR | `clearcoat`, `clearcoatRoughness`, `clearcoatIOR` |
| Subsurface Weight / Radius / Scale / Anisotropy | `subsurface*` |
| Sheen Weight × Tint, while Sheen Weight is on | `sheenColor` |
| Diffuse Roughness | not authored; the surface shades as Lambert |

The strict validator refuses these (`_unsupported_principled_inputs`):

| Blender Principled input | Why |
|---|---|
| Coat Tint | No RealityKit surface carries a coat tint |
| Sheen Roughness other than `0.5` | The surface has no such input |
| Specular Tint, coloured or linked | Colour semantics are not verified against the surface |
| Specular Tint, achromatic and brighter than 1 | Outside the verified range, unless [clamped](#specular-and-ior) |
| Anisotropic, Anisotropic Rotation, Tangent | Blender's level factor and tangent rotation are not reproduced |
| Transmission Weight | No surface input; the message says the material would be opaque |
| Thin Wall, while transmission or subsurface is active | RealityKit has no thin-wall shading |
| Thin Film Thickness / IOR, Subsurface IOR | No surface input |
| Linked Coat Weight / Roughness / Tint | Linked coat controls are not preserved; constants are |

Each refusal names the input; most tell you to bake. An input counts only
when linked or away from its neutral value, and subordinate controls only
while their weight is non-zero, so a red Coat Tint under `Coat Weight = 0` is
silent.

### No UsdPreviewSurface network

Blender writes a `UsdPreviewSurface` network beside every material. The
exporter removes it from every translated material (`_remove_preview_network`):
the universal `outputs:surface`, `outputs:displacement` and `outputs:volume`,
the prims they reach, and any leftover `UsdPreviewSurface`, `UsdUVTexture`,
`UsdTransform2d` or `UsdPrimvarReader_*` shader. Anything the MaterialX surface
or geometry modifier reaches is kept. RealityKit and Reality Composer Pro never
read the removed network. What you give up: tools that read only
`UsdPreviewSurface` (`usdview`, Blender re-importing the file, most non-Apple
USDZ viewers) show the materials untextured.

---

## Decisions the exporter makes for you

**Told?**: **Error** = export stops; **Warning** = diagnostics plus UI/CLI
warning; **Info** = diagnostics sidecar only; **Silent** = no record.

### Color space

| Trigger | What happens | Told? |
|---|---|---|
| Color input, image tagged **sRGB** or untagged | authored `srgb_texture` | — |
| Color input, image tagged **Linear Rec.709** | authored `lin_rec709` | — |
| Color input, image tagged **Non-Color** | authored `lin_rec709` | **Warning** |
| Data input, image tagged **Non-Color** or **Linear Rec.709** | no color space authored | — |
| Data input, image tagged **sRGB** | authored `srgb_texture`, so the input reads decoded values, as in Cycles | **Warning** |
| Data input reading the **alpha channel**, any tagging | no color space authored | **Silent** |
| Any other Blender color space | **export fails** | **Error** |
| Blend file working color space other than **Linear Rec.709** | **export fails**, naming the working space | **Error** |
| Baked AO texture | tagged Non-Color | **Silent** |
| `ColorSpaceAPI` token `srgb_rec709_display` | renamed `srgb_rec709_scene` | **Info** |

**A data texture carries no color space at all.** An absent MaterialX color
space is the no-transform contract, which is what a roughness, metallic or
normal image needs.

**An sRGB image is decoded whatever it feeds.** Cycles decodes an image by its
color space before any node reads it, so Roughness fed by an sRGB image,
directly or through Math, Separate Color or any other node, reads decoded
values, and one image may feed both a color and a data input. The reader is
authored `srgb_texture` to match. Because a data map left at Blender's default
sRGB is usually a tagging mistake, validation, and so the export, warns and
names the image (`srgb_data_image_notices`). A chain takes the role of the
surface input it ends in, as the exported graph does, so an sRGB image that
reaches only color inputs, or is read through its Alpha output, draws no
warning. RealityKit has no mapping for the lowercase `raw` token,
and Reality Composer Pro replaces a material whose reader carries it with a
striped placeholder, so it is never authored.

**`srgb_rec709_display` becomes `srgb_rec709_scene`.** Blender 5.2 authors
`colorSpace:name = "srgb_rec709_display"`, which RealityKit has no alias for.
The postprocess renames it to `srgb_rec709_scene`, the same sRGB transfer on
Rec.709 primaries (`_retag_unmapped_color_space_names`). Attribute-level
`colorSpace` metadata on an image reader keeps MaterialX's `srgb_texture`. Preflight accepts the
sRGB family (`srgb_texture`, `srgb_rec709_scene`) and the linear Rec.709
family (`lin_rec709`, `lin_rec709_scene`) on color textures.

**Non-Color on a color input** is the one place the exporter overrides your
tagging: Blender applies no transfer function, so the input reads scene-linear
texels, and the exporter authors that as `lin_rec709` with a warning.

An input is `color` if it is one of the names in `_COLOR_TEXTURE_INPUTS`
(`baseColor`, `emissiveColor`, `subsurfaceColor`, `sheenColor`,
`specularColor`, the unlit `color` and their variants) and `data` otherwise;
normal maps are always `data`. The material prim and stage root get
`colorSpace:name = "lin_rec709_scene"`, and every colour constant, vertex colour
and linear texture read is authored unconverted under that tag, which is why a
file whose working space is Linear Rec.2020 or ACEScg is refused
(`working_color_space_refusal`).

### Opacity, transparency, and cutout

| Trigger | What happens | Told? |
|---|---|---|
| Principled Alpha linked, or constant `< 0.999` | material treated as transparent | **Silent** |
| Material not transparent | `opacity` not authored | **Silent** |
| Transparent, and `usd_stage_alpha_cutout_threshold` is a float in `[0, 1]` | `opacityThreshold` authored → **cutout** | **Silent** |
| Transparent otherwise | no threshold → **blend** | **Silent** |
| Base Color textures all `premul` | `hasPremultipliedAlpha = true` | **Silent** |
| Base Color mixes premultiplied and straight textures | **export fails** | **Error** |
| Premultiplied base color with an AVIF encode or resize override | **export fails**; select PNG | **Error** |

Transparency is read from the Alpha input (`material_has_transparency`), not
from `surface_render_method`, which only chooses how Eevee renders it. A Mix
Shader with a Transparent BSDF is also transparent; see
[Shader closures](#shader-closures). **Cutout is opt-in and never inferred**:
neither render method declares a threshold, so set the custom property
`usd_stage_alpha_cutout_threshold`; invalid values are ignored silently.
`hasPremultipliedAlpha` is one flag per material, so a mix of conventions on
Base Color is refused, and Blender 5.2's AVIF writer does not preserve the
premultiplied relationship (`require_safe_texture_alpha_staging_policy`).

### Image Color and alpha

Cycles stores every image with associated alpha and divides it back out only
when the node's Alpha output is used, so an image's Color output depends on
whether Alpha is wired. The export follows Cycles (`image_premultiplies_color`):

| Image | What the node's outputs read | Told? |
|---|---|---|
| Straight alpha, sRGB, file has alpha, Alpha output unused | Color = sRGB decode of (encoded colour × alpha), read at computed coordinates | **Silent** |
| Straight alpha, linear color space, file has alpha, Alpha output unused | Color = linear colour × alpha, read at computed coordinates | **Silent** |
| Straight alpha, Alpha output used | Color as stored; Alpha the fourth channel | **Silent** |
| Premultiplied, Alpha output unused | Color as stored | **Silent** |
| Premultiplied, Alpha output used, at computed coordinates | **export fails**; set the image's Alpha to Straight | **Error** |
| Non-Color, Channel Packed or alpha mode None | Color as stored | **Silent** |
| A file with no alpha channel | Color as stored; Alpha reads 1.0 for every consumer | **Silent** |

EEVEE premultiplies after the sRGB decode rather than before it, so an sRGB
straight-alpha image can look different in EEVEE's viewport; the export
matches Cycles, which renders every bake.

### Normal maps

A Normal Map is Cycles' `svm_node_normal_map` in tangent space: the colour
decodes to `c = 2 (colour − 0.5)`, green negated for DirectX, and Strength `s`
scales it to `(s cₓ, s c_y, mix(1, c_z, saturate(s)))` before it is normalized.

| Trigger | What happens | Told? |
|---|---|---|
| Normal Map wired straight into Normal or Coat Normal, OpenGL, a plain UV image, constant Strength 1.0 | `ND_image_vector3` → `ND_normal_map_decode` → `normal` | **Silent** |
| The same with a constant Strength ≠ 1.0 | the decode, then the strength scale and a renormalize (`_apply_tangent_space_normal_strength`) | **Silent** |
| Any other tangent-space Normal Map: DirectX, a linked Strength, or a colour from computed coordinates, Box projection, a Mix or any other node | the decode and strength authored as graph nodes (`_normal_map_tangent_expr`) | **Silent** |
| A Normal Map output feeding anything but Normal or Coat Normal directly | the tangent-space normal turned into a Blender world normal, `x T + y B + z N`, normalized | **Silent** |
| Any other expression into Normal or Coat Normal: Bump, Geometry Normal, Vector Math, a Normal Map through a Mix | the world normal projected onto the tangent, bitangent and normal readers (`world_normal_to_tangent_space`) | **Silent** |
| Object or world space | **export fails** with bake advice | **Error** |
| UV Map field naming a map other than the render UV map | **export fails**; RealityKit builds tangents from the render UV map only | **Error** |
| Base Original and Strength ≠ 1 on a material that displaces vertices | **export fails**; Cycles then blends toward the undisplaced normal on smooth faces, which the export cannot tell apart | **Error** |

The gate is `normal_map_refusal`, shared by the validator and the resolver.
The reader carries `inputs:default = (0.5, 0.5, 1.0)`, so a texture that fails
to resolve shades flat.

### Specular and IOR

Cycles gives the dielectric lobe a reflectance at normal incidence of
`F0 = ((IOR − 1) / (IOR + 1))² × 2 × Specular IOR Level`. RealityKit reads
`F0 = 0.08 × specular`, so `specular` carries Cycles' F0
(`pbr2_specular_inputs`):

| Trigger | What happens | Told? |
|---|---|---|
| IOR and Specular IOR Level constant | `specular = min(1, F0 / 0.08)` | **Silent** |
| Cycles' F0 above 0.08 (IOR above about 1.79 at level 0.5), surface not fully metallic | `specular = 1`; the highlight renders dimmer than in Cycles | **Warning** |
| IOR or Specular IOR Level linked | the same formula as graph nodes, clamped to [0, 1] | **Warning** when the surface is not fully metallic |
| Constant achromatic Specular Tint > 1, *Clamp Overbright Specular Tint* on | clamped to `[1, 1, 1]` for this export only | **Warning** |
| Same, setting off | **export fails** | **Error** |
| Coloured or linked overbright Specular Tint | **export fails** regardless of the setting | **Error** |

Blender's defaults, IOR 1.5 at level 0.5, give F0 = 0.04 and `specular = 0.5`.
The tint clamp is the exporter's only value rewrite
(`safe_overbright_achromatic_specular_tint`): a coloured value is never clamped
because that would shift hue, and the warning says the `.blend` file was not
changed. Control: `clamp_specular_tint`.

### Emission, subsurface, sheen, and diffuse roughness

| Trigger | What happens | Told? |
|---|---|---|
| Emission Strength ≈ 1.0 | strength dropped | **Silent** |
| Constant strength + emission texture | folded into the texture's `scale` | **Silent** |
| Constant strength + constant color | multiplied into `emissiveColor` | **Silent** |
| Linked strength | `combine3` + `multiply` nodes | **Silent** |
| Subsurface weight but no subsurface color | Base Color copied into `subsurfaceColor` | **Silent** |
| Sheen Weight at or below Cycles' cutoff of 1e-5 | `sheenColor` not authored | **Silent** |
| Sheen Weight above the cutoff, Tint constant | `sheenColor = Tint × Weight` | **Warning** |
| Sheen Weight linked, or Tint linked while Weight is on | `multiply(tint, combine3(weight, weight, weight))` | **Warning** |
| The same over a black, non-metallic base at Specular IOR Level 0 (the Sheen BSDF) | as above | **Silent** |
| Diffuse Roughness above 0, or linked | not authored; the surface shades as Lambert | **Warning** |

Any authored `sheenColor` switches RealityKit to a sheen shading that replaces
the specular lobe, so the warning says the metallic reflection and the
dielectric highlight disappear wherever sheen is on. Any authored
`baseDiffuseRoughness` lights the diffuse from the environment about a third as
brightly as Lambert, further from Cycles' rough diffuse than Lambert is, so
Diffuse Roughness is never authored; a Lighting & Shadows bake keeps it. The
gate for these warnings is `principled_notices`.

Blender 5.2's Principled has no subsurface color input, so that copy is a
reconstruction, not a translation.

### UVs and texture transforms

| Trigger | What happens | Told? |
|---|---|---|
| No UV named | `texcoord` node, the render UV map | **Silent** |
| UV map named that is the render UV map of every mesh using the material | `texcoord` node | **Silent** |
| UV map named that none of those meshes renders with | `geompropvalue` with `geomprop = <name>` | **Silent** |
| A named UV map a mesh lacks, that is the render UV map on only some meshes, or that is not the render UV map and is called `st` or is not a valid primvar name | **export fails**, naming the map | **Error** |
| Point Mapping node with identity values | no transform authored | **Silent** |
| Non-identity Point Mapping without X or Y rotation or a zero X or Y scale | one `place2d`, rotation converted to degrees | **Silent** |
| Two textures, same effective mapping and UV set | one shared `place2d` | **Silent** |
| Two distinct non-default mappings | **export fails** (validator and preflight) | **Error** |
| An explicit `place2d` plus a Mapping, or two explicit `place2d` nodes | **export fails** | **Error** |
| Image coordinates from any other node; a Mapping in Texture, Vector or Normal mode, with linked sockets, with X or Y rotation or with a zero X or Y scale; a straight-alpha image [premultiplied as Cycles hands it](#image-color-and-alpha); or any image inside a Bump height or a computed Normal Map | read at the computed coordinate, no `place2d`, not counted toward the one transform (`_computed_image_expr`) | **Silent** |

Blender's USD exporter writes each mesh's render UV map as `primvars:st`, which
`texcoord` reads, and every other UV map under its own name, so a name resolves
against the meshes that use the material (`uv_set_resolution`). A UV map called
`UVMap` that is not the render map reads its own primvar.

RealityKit honors one 2D texture transform per material, and its `place2d`
has no operation order, so only a Point mapping it can express keeps the UV
transform. Validation, authoring
and preflight share one definition of *distinct*
(`Plugin/export/materials/mapping.py`): an explicitly authored identity
`place2d` still consumes the slot, and the same transform on two UV maps
counts as two.

### Type coercion

| Trigger | What happens | Told? |
|---|---|---|
| Texture output type ≠ input type | the texture hint is rewritten (`_coerce_texture_spec_for_input`) | **Warning** |
| A colour into a float input, including an image's Color into Roughness, Metallic, AO or Alpha | Blender's gray conversion: a dot product with (0.21263909, 0.71516913, 0.07219274) | — |
| A vector into a float input | the mean of its components | — |
| Remaining type mismatch | `convert` node, or a verified chain for `vector4 → color3` | **Warning** |
| No `convert` exists for the pair | **export fails**: `No MaterialX conversion exists` | **Error** |
| No nodedef satisfies the requested signature | **export fails** rather than picking a near miss | **Error** |
| 3-component color into `color4`, or vector into `vector4` | padded with `1.0` or `0.0` | **Silent** |
| List into a float input | element `[0]` | **Silent** |

Only a Separate Color or Separate XYZ node, or an image's Alpha output, reads a
single channel. The gray weights are Cycles' `linear_rgb_to_gray` for the
Linear Rec.709 working space, the only one the export accepts.

---

## Which Blender nodes export

### Where the node lists live

The gate is the module constants in `Plugin/nodes/validate.py`
(`SUPPORTED_TYPES`, `BAKE_TYPES`, `UNSUPPORTED_TYPES`,
`ALLOWED_UI_TYPES`, `SUPPORTED_MATH_OPERATIONS`,
`SUPPORTED_VECTOR_MATH_OPERATIONS`) plus the hair, closure, reader,
displacement and driver rules in `Plugin/export/materials/extract/` (`core.py` and its section modules),
where `_resolve_socket_value` decides what the export contains. Referenceable
nodedefs are in `Plugin/manifest/rk_nodes_manifest.json` (1,232, of which the
56 flagged `policy.editor_unresolvable` are refused). Non-`RK_*` node groups and
unrecognized node types are refused. Per-node verdicts:
[FEATURE_SUPPORT.md](FEATURE_SUPPORT.md#shader-nodes); platform side:
[APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported).

Some supported types export only in certain configurations; the conditions
for each are in the [shader nodes table](FEATURE_SUPPORT.md#shader-nodes), and
under strict validation every refusal is an error.

### Surface readers

Texture Coordinate, Geometry, Fresnel and Layer Weight export the outputs
RealityKit has a reader for and refuse the rest by name (`_reader_node_issues`
and `reader_refusal`; authored by `_reader_node_expr`):

| Blender output | Reader | Note |
|---|---|---|
| Texture Coordinate ▸ UV | `ND_texcoord_vector3`, the render UV map | |
| Texture Coordinate ▸ Object, Generated | `ND_position_vector3`, object space | Generated **warns**: Blender's bounding-box normalization is not applied. Object with the node's Object field set is **refused**: the export has only the shaded mesh's own object space |
| Geometry ▸ Position, Normal | `ND_position_vector3`, `ND_normal_vector3`, world space, turned into Blender's axes | see below |
| Geometry ▸ Incoming | `ND_realitykit_surface_view_direction`, normalized and turned into Blender's axes | points toward the viewer, as Blender's does |
| Geometry ▸ Backfacing | `1 − ND_realitykit_is_front_facing` | |
| Fresnel ▸ Factor | Cycles' `fresnel_dielectric_cos` over the view direction and world normal | linked Normal **refused** |
| Layer Weight ▸ Fresnel, Facing | Cycles' `node_layer_weight`, constant Blend folded | linked Normal or Blend **refused** |
| Geometry ▸ Tangent | — | **refused**: Blender's is a radial tangent around the object's Z axis, built from Generated coordinates, not the UV tangent RealityKit reads |
| Texture Coordinate ▸ Window, Camera, Reflection; Geometry ▸ True Normal, Parametric, Pointiness, Random Per Island | — | **refused** |

RealityKit's world readers are Y-up: the export's root prim turns Blender's Z-up
world by −90° about X, so a reader returns `(x, z, −y)` of Blender's vector.
`blender_world_vector` turns each world-space output back to `(x, −z, y)`, so
Vector Math constants and colour reads of a normal agree with Blender. World
readers return the space of the entity's anchor
([platform](APPLE_PLATFORM_CONTRACT.md#which-materialx-nodes-are-supported)),
so this assumes the asset sits unrotated in that space. Fresnel and Layer
Weight use only dot products, which the turn does not change, and Camera Data
reads object-space position through the model-to-view matrix, which needs no
turn.

Fresnel and Layer Weight take the absolute cosine, as Blender does, and match
a port of the Cycles functions value for value.

### Vector Math and Combine XYZ

Vector Math passes for the 27 operations in `SUPPORTED_VECTOR_MATH_OPERATIONS`,
each an exact MaterialX node or a composition transcribed from Cycles; Combine
XYZ builds a vector from three float expressions.

| Trigger | What happens | Told? |
|---|---|---|
| A float wired into a vector socket | broadcast to three components, as in Blender | **Silent** |
| Scale, or any composition multiplying a vector by a float | vector × vector `multiply`, the float broadcast through `combine3` | **Silent** |
| Modulo | **export fails**: Blender truncates where MaterialX floors | **Error** |
| Wrap, Snap | **export fails**: their per-component zero guard has no MaterialX form | **Error** |

The broadcast avoids MaterialX's vector-times-float node, which multiplies by
the wrong operand when the float is connected.

### Drivers over frame

A scripted driver whose expression uses only `frame`, numbers, `+ - * /`, unary
minus and `sin`, `cos`, `tan`, `abs`, `floor`, `ceil`, `sqrt`, `min`, `max`,
`pow` exports as arithmetic over RealityKit's time reader, with `frame` as
`ND_time_float × fps` at the scene's frame rate (`driver_expression_expr`).

| Trigger | What happens | Told? |
|---|---|---|
| Driver on a Value node's output, or a float input of Math, Vector Math, Mix, Clamp, Map Range, Hue/Saturation/Value, Brightness/Contrast, Invert, Fresnel, Combine Color or Combine XYZ | exported over the time reader | **Warning**: it runs on the material's own clock from the moment it renders, not on the timeline |
| Driver on any other socket | **export fails**, naming driver and socket | **Error** |
| Driver with variables, non-scripted, or outside the grammar | **export fails**, naming driver and socket | **Error** |

The gate is `_driver_issues`. The authored trees evaluate to the same values as
the expression at `frame = time × fps`.

### Hair

A Principled Hair BSDF with Direct coloring on the active output terminates in
`ND_realitykit_hair_surfaceshader`. Four controls map (`_HAIR_MAPPED_SOCKETS`):

| Blender input | Hair-surface input | |
|---|---|---|
| Color | `baseColor` | |
| Roughness | `primaryRoughness` **and** `secondaryRoughness` | |
| Reflection | `primarySpecular` | Huang model only |
| Secondary Reflection | `secondarySpecular` | Huang model only |

Disabled sockets are never exported, so under the default Chiang model, which
has no lobe weights, only colour and roughness are authored and there is no
secondary lobe. Inputs resolve through the expression tree; Cycles' per-lobe
roughness scaling is not reproduced, and no shift is authored. Under Huang,
Reflection and Secondary Reflection map at half their value, because the hair
surface's lobe weight saturates at 1 with 0.5 its neutral lobe
(`HAIR_LOBE_WEIGHT_SCALE`), and `secondarySpecularColor` is authored white
whenever the secondary lobe is mapped, since its default of black would hide
it.

**The strand direction is +U.** The hair surface reads `tangent` in its UV
tangent frame, so the export authors the constant `(1, 0, 0)`
(`HAIR_STRAND_TANGENT`). **Each strand or card must run along +U of its UV
map**, and a mesh with no UV map has no strand direction.

| Trigger | What happens | Told? |
|---|---|---|
| Melanin or Absorption coefficient coloring | **export fails**; there is no colour to read without the scattering model | **Error** |
| `Hair BSDF` (single-lobe Cycles shader) | **export fails**, pointing at Principled Hair BSDF | **Error** |
| A hair BSDF that does not terminate the material | **export fails** | **Error** |
| Any exported hair surface | two-lobe approximation without the transmission lobe, cuticle Offset or radial roughness; strands must run along +U, and a mesh with no UV map has no strand direction | **Warning** |
| The Huang model | Aspect Ratio dropped | **Warning** |
| A linked input the surface has no form for (Tint, Offset, Radial Roughness, Coat, IOR, Transmission, Random controls) | dropped, named | **Warning** |

Hair curve objects are not exported, so use this on mesh hair cards.

### Nodes transcribed from Cycles

Each of these is authored from the Cycles kernel function named, and matches
that function value for value: against a port of it, or against what Cycles
bakes.

| Node | How it exports |
|---|---|
| Gamma | each channel above 0 raised to Gamma, the rest unchanged; Gamma 0 gives white (`svm_math_gamma_color`) |
| Checker Texture | Fac is the floored modulo by 2 of the sum of the floors of the nudged, scaled coordinate, which is Cycles' parity test; Color mixes Color2 toward Color1 by it (`svm_checker`) |
| Camera Data | the object-space position through `ND_realitykit_surface_model_to_view`, with z negated because Cycles' camera looks down +Z (`svm_node_camera`) |
| Mapping | `R(v × S) + L`, `safe_divide(Rᵀ(v − L), S)`, `R(v × S)` or `safe_normalize(R safe_divide(v, S))`, with R the Euler XYZ rotation (`svm_mapping`) |
| Image Texture, computed coordinates | `ND_image` read at the xy of the resolved coordinate, Color premultiplied where [Cycles hands it so](#image-color-and-alpha); Alpha from a four-channel read, 1 for a file without alpha |
| Environment Texture | the Vector, or the world position when unlinked, normalized and mapped by `direction_to_equirectangular` (with `compatible_atan2`, so a vertical direction reads u = 0.5) or `direction_to_mirrorball`, then read as a computed image that repeats (`svm_node_tex_environment`) |
| Normal Map | see [normal maps](#normal-maps) (`svm_node_normal_map`) |
| Hue/Saturation/Value | `rgbtohsv`, hue `fract(h + Hue + 0.5)`, saturation saturated, value scaled, `hsvtorgb`, mixed by Fac and floored at 0 (`svm_node_hsv`); exact but for the half-precision hue error of at most 2.4e-4 ([platform](APPLE_PLATFORM_CONTRACT.md#colour-nodes-compute-in-half-precision)) |
| Separate Color, HSV | Cycles' `rgb_to_hsv`, including the hue wrap and the zero-maximum case RealityKit's `rgbtohsv` omits |
| RGB to BW, and every colour into a float input | the gray conversion under [type coercion](#type-coercion) (`linear_rgb_to_gray`) |
| Mix, Mix Color | the factor saturated when Clamp Factor is on (always for Mix Color), the colour clamped by Clamp Result or Use Clamp, Non-Uniform vector mixes per component (`svm_node_mix`) |
| Clamp | Min Max, or Range with inverted bounds swapped (`svm_node_clamp`) |
| Map Range, Linear | divides only when From Min ≠ From Max, and clamps with swapped bounds when To is inverted; Vector mode per component (`svm_map_range`) |
| Brightness/Contrast | `max((1 + Contrast) c + Bright − Contrast / 2, 0)` per component (`svm_brightness_contrast`) |
| Math, Vector Math | Cycles' guards: safe divide, square root, power and logarithm, clamped arcsine and arccosine, `compatible_atan2`, floored modulo, safe normalize, reflect and refract; Round is `floor(x + 0.5)` |
| Gradient Texture | all seven types, saturated (`svm_gradient`); unlinked, it samples object position in place of Generated, with a warning |
| Noise Texture | `ND_fractal3d` summing Blender's `floor(Detail) + 1` octaves with a fractional Detail blending in one more, normalized to Blender's range; Color from Blender's per-channel offsets (`noise_fbm`). The hash is MaterialX's, so the pattern differs, with a warning |
| Voronoi Texture | `ND_worleynoise3d` F1 Distance, Randomness clamped to [0, 1], Normalize honoured; the hash differs, with a warning |
| Image Texture, Box | the object-space normal picks per-side weights in seven zones by Projection Blend, and three reads of the file at flipped coordinate pairs are summed (`svm_node_tex_image_box`); unlinked, the coordinate is the UVs |
| Attribute | Float and Vector attributes through `geompropvalue`, colour attributes through the vertex-colour reader, UV maps through the UV reader; Fac is the mean of three components, or the float, or U (`svm_node_attr_surface_eval`) |
| Object Info ▸ Random | `geompropvalue` of `blenderObjectRandom`, which `_author_object_random` writes on each mesh: `hash_uint2(hash_string(object name), 0) / 0xFFFFFFFF` |
| Bump | `normalize(N − Distance ∇h)` blended toward N by Strength (`svm_node_set_bump`), with ∇h the height's surface gradient along the UV parametrization: the height is evaluated again one UV step of 0.001 along U and V, and `_author_uv_derivatives` writes each mesh's object-space dP/du and dP/dv per corner to turn those differences into a gradient |

A Bump height reaches every image through the computed read, so the UV step
applies to it, and a height that reads a mesh attribute is refused because an
attribute cannot be evaluated one step away. Math Fract is authored as
`x − floor(x)` because `ND_fract_float` has no implementation in RealityKit's
1.39 library, and RGB Curves is refused because `ND_curveadjust_*` has none
either. Colour nodes run in half precision on the platform, so data built
through Combine Color or a colour Mix loses precision
([platform](APPLE_PLATFORM_CONTRACT.md#colour-nodes-compute-in-half-precision)).

### Shader closures

Diffuse, Glossy, Metallic, Sheen and Subsurface Scattering export through a
stand-in for the Principled BSDF Cycles evaluates each as
(`principled_preset`), so every Principled rule above applies, reported under
the node's own socket names:

| Shader | Principled BSDF | Exact in Cycles |
|---|---|---|
| Diffuse | Base Color, Diffuse Roughness, Specular IOR Level 0, which removes the specular lobe | yes; a Roughness above 0 warns, as Diffuse Roughness does |
| Metallic, F82 Tint | Metallic 1, Base Color, Edge Tint as Specular Tint, Roughness, anisotropy, thin film | yes; Physical Conductor is refused |
| Sheen | black Base Color, Specular IOR Level 0, Sheen Weight 1, Sheen Tint from Color, Sheen Roughness | yes |
| Glossy | Metallic 1, Base Color from Color, Roughness | no: Glossy has no Fresnel, and the export warns |
| Subsurface Scattering | Subsurface Weight 1 with Color, Radius, Scale, IOR as Subsurface IOR, Anisotropy; Specular IOR Level 0; Roughness as the square root of the node's, because Principled squares it | only under Random Walk (Skin) with Color at most 1; other methods warn that Cycles scatters with the node's IOR where the Principled BSDF uses 1 |

One Mix or Add Shader directly on the Material Output has a RealityKit form in
three shapes (`resolve_surface_closure`); Blender's Mix Shader is
`(1 − Factor) × A + Factor × B`, with Factor clamped to [0, 1]:

| Shape | What happens | Told? |
|---|---|---|
| Mix Shader of one of the shaders above and a white Transparent BSDF | opacity = Alpha × (1 − Factor), or Alpha × Factor when the Transparent BSDF is on the factor-0 side, with Alpha and Factor each clamped to [0, 1]; the factor may be linked | **Silent** |
| Add Shader of one of the shaders above and an Emission shader | the Emission's Color × Strength is added to the Principled's emission | **Silent** |
| Transparent BSDF with a tinted or linked Color | **export fails**; RealityKit opacity has no colour | **Error** |
| Mix Shader of two of the shaders above, differing only in Base Color or only in Metallic | one surface whose differing input is `mix(A, B, Factor)` (`principled_mix`), exact | **Silent** |
| The same with more than one input differing, or any other input | those inputs blended, and named; Cycles sums the two surfaces, which a blend of values matches only approximately | **Warning** |
| The same with differing linked Normal, Coat Normal or Tangent | **export fails**; two normals do not blend into a normal | **Error** |
| Mix Shader of a Principled BSDF and an Emission | **export fails**; use an Add Shader | **Error** |
| Add Shader over a Principled whose emission is linked | **export fails**; put the emission in one place | **Error** |
| Add Shader over a Principled whose Alpha is below 1 or linked | **export fails**; RealityKit dims emission by opacity, where Cycles adds it at full strength | **Error** |
| An unconnected input, other operands, or a nested closure | **export fails**, naming the operands | **Error** |

### Vertex displacement

The Material Output's Displacement socket exports as RealityKit's geometry
modifier (`ND_realitykit_geometrymodifier_2_0_vertexshader`) on the material's
`realitykit:vertex` output. It takes a model-space offset, so an
**Object**-space Displacement node maps directly to the object-space normal ×
`(Height − Midlevel) × Scale`, and Vector Displacement to
`(Vector − Midlevel) × Scale` (`displacement_refusal` gates it):

| Trigger | What happens | Told? |
|---|---|---|
| Displacement Method **Bump Only** | **export fails**; nothing to move | **Error** |
| Socket fed by anything but a Displacement or Vector Displacement node | **export fails** | **Error** |
| **World** space, or Vector Displacement in **Tangent** space | **export fails**; only Object space has a form | **Error** |
| Displacement with a linked **Normal** | **export fails**; the offset follows the mesh normal only | **Error** |
| Displacement Method **Both** | vertices move, the bump half is dropped | **Warning** |
| Any exported displacement | normals are not recomputed, and the mesh needs enough vertices to show the shape | **Warning** |

### Node groups, reroutes, and muted nodes

- **Only RealityKit node groups are accepted, and their contents are not
  validated.** Validation does not descend into groups. A group counts as
  RealityKit's when its tree carries `rk_node_id` or matches a catalog name
  (`_is_rk_group`); any other group fails with `Non-RealityKit node group used.`
- **Muted nodes and muted links are refused, not honoured.** Blender bypasses
  them, but extraction would evaluate them as live. Unmute or delete them.
- **Reroutes are transparent**, but the advisory pass still lists a used
  Reroute as `unrecognized; export may differ`. The export is unaffected.

---

## The texture pipeline

| Stage | Module | Entry point |
|---|---|---|
| Datablock → absolute path | `materials/extract/core.py` | `_resolve_image_path` |
| MaterialX reader authoring | `materials/textures.py` | `_create_texture_connection` |
| Copy/transcode into the output tree | `export/usd_textures.py` | `_stage_texture_source` |

Staging runs before and after material rewrite. Where staged textures land and
how they are named: [EXPORT_PIPELINE.md](EXPORT_PIPELINE.md#where-textures-are-staged).

### How image paths resolve

*Current Blender pixels win* (`_resolve_image_path`):

| Image state | Behavior |
|---|---|
| Clean, file-backed | absolute path used directly |
| Packed | packed bytes written verbatim to a temp file |
| Generated, or dirty | live pixels snapshotted (generated: `.exr` if float, else `.png`) |
| Dirty tiled, sequence or movie | **export fails**: *must be baked to a single current frame* |
| Relative `//` path | resolved via `bpy.path.abspath` |
| Missing file | **export fails**: `Texture file not found` |

UDIM sets are not supported; bake them to a single texture.

### Format conversion

PNG, JPEG and OpenEXR are copied through unchanged
(`_APPLE_TEXTURE_OUTPUT_EXTENSIONS`); the decision is
`_effective_texture_override`:

| Source | Overrides off | Overrides on |
|---|---|---|
| `.exr` | byte-copied | byte-copied; the override is ignored with a warning |
| `.hdr` | **export fails**; convert to OpenEXR | same |
| `.png`, `.jpg`, `.jpeg` | byte-copied | encoded to the chosen *Image Format* and resolution |
| `.avif`, `.tif`, `.tga`, `.bmp`, `.webp` and other inputs | converted to PNG | converted to PNG, or to AVIF if AVIF output is selected |

An AVIF source is never copied through, because Reality Composer Pro 3 refuses
AVIF on import; selecting AVIF output warns for the same reason, and a failed
AVIF encode retries as PNG (see
[APPLE_PLATFORM_CONTRACT.md](APPLE_PLATFORM_CONTRACT.md#texture-formats)).
Conversions are validated by a real decode before replacing anything. Opt-in
resizing clamps the longest edge and never upscales; see
[SETTINGS.md](SETTINGS.md#texture-settings).

### Reader authoring and channel extraction

The reader follows what the shader input needs, not what the file contains
(`_image_output_hint`):

| Situation | Reader |
|---|---|
| normal map | `ND_image_vector3` |
| a scalar input (roughness, metallic, occlusion) fed the Color output | `ND_image_color3`, then the [gray conversion](#type-coercion) |
| a Separate Color channel | `ND_image_color3`, then a channel read |
| an input that needs alpha | `ND_image_color4`, then a channel read |
| everything else | `ND_image_color3` |

A channel read is a `convert` to a vector (`ND_convert_color3_vector3` or
`ND_convert_color4_vector4`) followed by a `dotproduct` with a unit mask
(`_create_swizzle_output`, `_create_separate4_outputs`). One file is one
reader: the reader cache (`_texture_cache_key`) ignores the channel, and the
`convert` is named after the reader, so channel reads of one file share both.
Four-channel vector readers and extractors (`ND_image_vector4` and kin) are
never authored, because Reality Composer Pro replaces such a material with a
striped placeholder.

Image Texture Extension **Extend**, **Clip** and **Mirror** become
`uaddressmode`/`vaddressmode` `"clamp"`, `"constant"` and `"mirror"`;
Interpolation **Closest** and **Cubic**/**Smart** become `filtertype`
`"closest"` and `"cubic"` (`_image_node_sampling`).

### When a texture has no alpha

A file has no alpha when the datablock's channel count or the file header says
so, or its alpha mode is None (`_image_source_alpha`). Its Alpha output reads
1.0, as in Blender, for every consumer: the resolver folds it to the constant,
and a read that still reaches authoring is written as 1.0 with a warning naming
the file and input. An unknown count stays permissive.

### Worked example

An sRGB albedo, a Non-Color ORM (Roughness from G, Metallic from B) and a
Non-Color tangent normal map export as, abridged:

```
def Material "Mat" (customData = { dictionary USDStage = { string surfaceProfile = "realitykit_pbr2" } }) {
    uniform token colorSpace:name = "lin_rec709_scene"
    string config:mtlx:version = "1.39"
    def Shader "pbr_surfaceshader_1" {
        uniform token info:id = "ND_realitykit_pbr_surfaceshader_2_0"
        color3f inputs:baseColor.connect = <Image.outputs:out>
        float   inputs:metallic.connect  = <channel_metallic_b.outputs:out>
        float   inputs:roughness.connect = <channel_roughness_g.outputs:out>
        float3  inputs:normal.connect    = <NormalMap_normal.outputs:out>
    }
    def Shader "Image"   { ND_image_color3  inputs:file ( colorSpace = "srgb_texture" ) }
    def Shader "Image_1" { ND_image_color3  inputs:file }
    def Shader "convert_Image_1_vector3" { ND_convert_color3_vector3 }
    def Shader "channel_metallic_b"  { ND_dotproduct_vector3  inputs:in2 = (0, 0, 1) }
    def Shader "channel_roughness_g" { ND_dotproduct_vector3  inputs:in2 = (0, 1, 0) }
    def Shader "Image_2" { ND_image_vector3  inputs:default = (0.5, 0.5, 1) }
    def Shader "NormalMap_normal" { ND_normal_map_decode }
}
```

---

## Diagnostics and errors

Material failures reach the CLI as the codes in [CLI.md](CLI.md#error-codes),
usually `UNSUPPORTED_MATERIAL_NODES` from validation and `EXPORT_FAILED` from
the rewrite. Preflight issue codes travel inside the message and in the
diagnostics sidecar ([CLI.md](CLI.md#diagnostics-files)), the only place
`info` findings appear.

### Fatal material messages

Besides the messages quoted in the sections above:

| Message | Source |
|---|---|
| `No Blender material mapping was available for this bound USD Material.` | `rewrite_materials` |
| `Material extraction failed: ...`, `MaterialX graph construction failed: ...`, `MaterialX authoring failed: ...` | `rewrite_materials` |
| `Unsupported Blender image color space '<x>' for '<input>'` | `_materialx_file_colorspace` |
| `Material '<n>' requires N distinct non-default texture mappings, ...`, `... combines an explicit MaterialX place2d node with a Blender texture Mapping transform.`, `... contains N explicit MaterialX place2d nodes.` | `require_realitykit_mapping_contract` |
| `... RealityKit has one material-level hasPremultipliedAlpha flag. ...` | `_require_safe_material_texture_policy` |
| `Premultiplied base-color texture '<p>' cannot be encoded or resized as AVIF safely ...` | `require_safe_texture_alpha_staging_policy` |
| `Material '<n>' at <path> is defined inside a variant.`, `... exists only inside a read-only OpenUSD prototype.` | `_require_safe_material_path` |
| `Cannot safely rewrite inactive variant material binding(s): ...` | `_variant_bound_material_prims` |
| `Texture file not found: <p>`, `Texture '<p>' uses Radiance HDR. ...` | `_stage_texture_source` |
| `Dirty <source> image '<n>' must be baked to a single current frame ...` | `_resolve_image_path` |
| `RealityKit OS 27 preflight failed with N error(s): ...` | `_require_realitykit_preflight` |
