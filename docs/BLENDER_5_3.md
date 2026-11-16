# Blender 5.3 and USD Stage

Blender 5.3 is the minimum supported version. The extension manifest and the
runtime gate reject older versions; CI and the node-group build/parity tools
use 5.3.0. Existing `.blend` scenes can be opened in 5.3. Historical measurements
in the material documentation retain the Blender version they were measured on.

Sources: Blender's [5.3 release page](https://www.blender.org/download/releases/5-3/),
[Pipeline & I/O notes](https://developer.blender.org/docs/release_notes/5.3/pipeline_io/)
and [bug fixes](https://developer.blender.org/docs/release_notes/5.3/bugfixes/).

## Collection Importer: a source-asset workflow

Blender can attach a USD importer to a collection. Its source path and import
settings are saved in the `.blend`, and Reload reads an updated source file.
This is useful when assembling assets from another DCC before exporting them
to RealityKit with USD Stage.

1. Select the destination collection and open **Collection Properties →
   Importers**. Add a USD source and configure its import settings.
2. Import the asset. Use Reload after the source file changes.
3. Export the assembled scene through USD Stage as usual.

Imported objects belong to an archive library and are linked/read-only. Use
Blender's **Make Local** operation before editing their geometry or materials;
local copies no longer provide the same source-reload workflow. Keep authored
overrides and additions in separate local collections when retaining a
reloadable source.

`tests/integration/test_blender53_usd.py` verifies source reload, settings
persistence across a `.blend` save/reopen, and export of the resulting geometry
through USD Stage. That proves the geometry path. It does not establish a
MaterialX round trip: Blender importing our RealityKit shader graphs, preserving
all USD variants, or baking linked materials needs separate validation.

The importer complements our exporter. There is no custom import button or
automatic source reload in USD Stage: Blender already supplies those controls,
and an export uses the state the artist has loaded.

## Native improvements already used by our export

| Blender change | Effect in USD Stage | Evidence and limits |
|---|---|---|
| Normals retain Blender's point, face or corner representation | Smooth meshes can use fewer normal values instead of repeating them per face corner. Our existing `export_normals=True` uses this automatically. | Integration coverage confirms a smooth cube exports eight vertex normals for eight points after our post-processing. Final rendering still needs visual validation. |
| Matrix4d primvar import/export | Matrix attributes can pass through for downstream USD tools. | A `FLOAT4X4` mesh attribute survives our export as `matrix4d[]`. This does not imply a RealityKit material can read it. |
| Playback FPS metadata fix | Native animation export now writes the scene playback rate. | Integration coverage checks both `framesPerSecond` and `timeCodesPerSecond` at 30. Our payload timing preservation remains necessary. |
| Simulation-node material export fix | Native export should retain materials produced by simulation nodes more reliably. | Listed by Blender; no simulation-specific regression has been measured here yet. |
| Imported `primvars:normals` retained as normals | Better source-asset shading when importing USD. | Listed by Blender; arbitrary imported normal layouts still need visual testing. |

## Gaussian splats

Blender 5.3 can import the `UsdVolParticleField3DGaussianSplat` schema. This adds
a Blender input format, not a RealityKit output path. USD Stage still excludes
point-cloud and volume objects from native export and rejects unsupported
geometry in the resulting stage. Do not treat a successful Blender import as
proof that a splat can be exported for RealityKit. Supporting one would need
an explicit conversion or a separate renderer and is outside this update.

## Next useful integration

Validate a representative material-bearing source asset through Collection
Importer → Reload → Make Local → USD Stage translation and baking. That will
show whether any importer-specific guidance or tooling is needed beyond
Blender's own UI. Geometry reload and re-export already work without a new
plug-in feature.
