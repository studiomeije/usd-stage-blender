# Evaluation scenes

Thirty-seven small `.blend` files, one behaviour each, for checking by eye what
no automated test can: whether an export **looks right** in Reality Composer
Pro.

Regenerate the scenes:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python scripts/build_test_scenes.py
```

Export all of them:

```bash
scripts/export_test_scenes.sh
```

That writes `References/RealityComposerProProject/Export/<scene>/<scene>.usdz`,
one directory per scene and nothing else: each directory is emptied first, so
the folder never accumulates formats, textures or diagnostics from earlier
runs. The scenes judged on their motion, `t11_transform_animation`,
`t12_skinned_limb` and `t23_cube_with_4_animations`, export with animation and
the RCP clip library on, both off by default. The three refusal scenes leave
only their diagnostics behind, which is the expected result.

The whole folder is gitignored and reproducible, so delete it whenever you
like. Reality Composer Pro imports a copy of each file into its own project, so
the folder can be rewritten while the editor is open; re-import a scene to pick
up its new export.

Import the results into the Reality Composer Pro project yourself. Under the
project package, the imported `.import` packages are the editor's own and
outlive the exports they came from, so a stale one has to be removed in the
editor rather than on disk. If the editor starts failing or crashing after
you remove scenes, see [When the editor misbehaves](#when-the-editor-misbehaves).

## When the editor misbehaves

Four Reality Composer Pro behaviours read exactly like an export defect. Rule
them out before reporting one.

- **Chinese names show as `¿`.** The hierarchy draws `椅子` as `¿¿`, one mark
  per character, while accented Latin and Cyrillic names show as written. The
  name itself arrives intact: the saved `.import` stores it as `\u6905\u5b50`
  and names its mesh files `椅子Mesh`. Only the editor's display is wrong.
- **A corrected file keeps failing to import.** When an import fails part way,
  the editor keeps the `.import` package with its records but no geometry
  buffers, and importing the corrected file under the same name does not
  rebuild it. It keeps reporting
  `Failed reading file .../geometry/….tm_buffers/…`. Remove the import in the
  editor, or import the file under a new name.
- **Every scene crashes the editor on open.** The editor stores each project's
  window layout at
  `~/Library/Application Support/Reality Composer Pro/Project Settings/<hash>.json`,
  keyed by the project's path. Once the scenes that layout points at are gone,
  the editor crashes restoring it; its log under
  `~/Library/Application Support/Reality Composer Pro/Logs/` reads
  `Failed to restore previously open window layout`. Quit the editor and move
  that file aside. It is regenerated on the next open.
- **Imports succeed, then every scene crashes on open with nothing logged.**
  The project's root entity, `world.tm_entity`, was deleted along with the
  scenes. It is tracked in this repository, so restore it with the editor
  closed:

  ```bash
  git checkout -- References/RealityComposerProProject/RealityComposerProProject.realitycomposerpro/world.tm_entity
  ```

## How to read a verdict

The console is not a verdict. Exports carry no `UsdPreviewSurface` network, so
the editor has no fallback to convert: a `Cannot convert UsdPreviewSurface`
line, or ``error: Cannot convert from `Unknown` to `RGB`!`` on
`t06_normal_map`, means an export reintroduced that network and is worth
reporting. Why the network is gone is in
[`MATERIAL_TRANSLATION.md`](../../docs/MATERIAL_TRANSLATION.md#no-usdpreviewsurface-network).

Every row names three outcomes, because two is not enough:

- **Correct** — what you should see.
- **Wrong** — the feature reached the file but carries the wrong data. This is
  the dangerous one: the asset looks plausible.
- **Not built** — Reality Composer Pro's grey/cyan diagonal placeholder stripes,
  or a flat default-grey surface. The whole shader graph was discarded, every
  texture binding with it.

**Do not compare against the Blender render.** Lights are dropped silently, so
your Blender render is lit by a sun that never reaches the export while Reality
Composer Pro relights the asset under its own environment. A mismatch is
guaranteed and tells you nothing. Every scene that needs a reference carries its
own control object in frame instead.

## Geometry

| Scene | Correct | Wrong | Not built |
|---|---|---|---|
| `t01_orientation_scale` | An upright red post 1 m tall, base on the floor, green foot pointing one way and a blue nub the other | Lying on its side (up-axis lost), or 100× too big (unit scale lost), or the foot and nub swapped (handedness flipped) | — |
| `t02_uv_layout` | A cube whose faces each show the four-quadrant grid, white corner stamp at one corner | Faces stretched, rotated, or all showing the same quadrant | Grey cube |
| `t03_vertex_color` | Cube ramping blue at the bottom to orange-red at the top. The top is authored `(1, 0.15, 0)` linear, so a warm orange under the editor's light is right, not a hue shift | Flat grey or flat single colour (the colour attribute was not read), or a much darker, duller ramp (the attribute was read as sRGB) | Grey cube |
| `t04_multi_material` | Alternating red and blue faces | All one colour (slots collapsed) or the two swapped | Placeholder stripes |
| `t37_collection_instances` | Five orange cone pawns with blue heads. A row of four along X: the first with its head toward +X, the second turned a quarter, the third turned half (head toward -X), the fourth smaller. Behind the first, the control pawn, identical to it. The four in the row are USD instances of one pawn: the import's `prototypes` folder holds one entity each for the body and the head, the row's entities reference them, and only the control carries its own mesh | Fewer than four pawns in the row (instances dropped), any pawn grey, striped or missing its blue head (materials lost inside the prototype), or all four facing the same way or the same size (instance transforms lost); an import error means instancing must be turned off by default | Grey pawns |
| `t38_unicode_names` | Four cubes, left to right red, blue, green and grey. The hierarchy names the first two `Chaise_été` and `Кресло`, and the third `¿¿`, the editor's display of `椅子` (see [When the editor misbehaves](#when-the-editor-misbehaves)); the materials are `Rouge_été`, `Синий` and `緑色`, and the grey `PlainChair` is the ASCII control | The import refused, a cube missing, or a cube grey, white or striped while the control is fine; names made of underscores mean Allow Unicode was off | Grey cubes |

## Materials and textures

| Scene | Correct | Wrong | Not built |
|---|---|---|---|
| `t05_metal_roughness` | Left sphere's reflection goes from blurred to sharp, with one hard vertical seam near its silhouette; right sphere is a sharp mirror everywhere. The seam is the fixture's cube-projection UVs changing axis on a sphere, and Blender's own render shows it in the same place | Both spheres identical (roughness never arrived), or the left sphere uniformly blurred with no seam at all (the ramp collapsed to a constant) | Grey spheres |
| `t06_normal_map` | Three panels. Middle: a 6×6 lattice of round bumps whose rims catch the light. Right: flat. Far left: the same lattice from the same map at Strength 0.35, visibly shallower but still bumps: the tangent xy is scaled by 0.35 and z lifted toward 1, as Cycles does, then renormalized. | Middle and left both flat (map dropped); bumps reading as dimples under a light from above (green channel flipped); or the far-left panel as strong as the middle (Strength ignored) or flat (Strength collapsed the map) | Grey panels |
| `t07_emission` | Left sphere glows a pale cyan-white; right stays dark. The emitter is authored `(0.1, 1.0, 0.35)` at strength 3, so green and blue saturate under the editor's tone mapping and only the red channel keeps the tint; the exported value is exactly `(0.3, 3, 1.05)` | Both dark (emission dropped), or the left sphere a dim green (strength was dropped and only the colour arrived) | Grey spheres |
| `t08_opacity` | Left sphere shows the cyan bar through it; right hides it | Both opaque (alpha dropped) or both transparent | Grey spheres |
| `t09_wrap_filter` | Left cube: grid tiled 3×, smooth edges. Right: hard-edged blocky texels, and the area outside 0–1 UV is empty rather than repeating | Right cube tiling like the left (Clip became Repeat) or smooth (Closest became Linear) | Placeholder stripes |
| `t10_texture_transform` | Grid tiled 3× and rotated 30° | Untiled and unrotated (transform dropped), or tiled ⅓× (the transform was inverted) | Placeholder stripes |
| `t17_procedural_noise` | Left sphere's reflection varies blotchily; right is evenly blurred. Export warns the noise is approximated | Both spheres identical — the noise was dropped despite the warning | Grey spheres |
| `t26_vector_math` | Middle sphere: the world normal as colour, (n + 1) / 2 in Blender's axes: lavender on top (+Z), olive underneath, pink on the right (+X), teal on the left, and purple facing you, because Blender's front faces -Y. Left sphere: the same reflected about the vertical axis, so olive on top and lavender underneath while the sides and front keep their colour. Right cube: a warm orange tint over most of each face, since the tint times a UV length above one clips, darkening to near black only in the corner nearest UV (0, 0) | Left and middle spheres identical (Reflect dropped); green on top and lavender facing you on the middle sphere (a world vector left in RealityKit's Y-up axes); the left sphere black or white (Normalize or Scale wrong); the cube flat (Length or Combine XYZ lost) | Placeholder stripes |
| `t27_frame_drivers` | **Press Play**; the viewport advances shader time only while playing. Left sphere: a green glow that pulses from dark to bright and back every **2.6 seconds** (`sin(frame * 0.1)` at 24 fps), starting mid-brightness the moment the scene renders. Right cube: ramps from red to blue over **two seconds**, snaps back to red, and repeats. Export warns twice that the drivers run on the material's own clock | Either object static (the driver exported as its resting value: a dark sphere, a red cube); the pulse or ramp at a different period (the time reader does not count seconds, or fps was folded wrongly); the cube fading blue to red (Factor inverted) | Grey sphere and cube |
| `t28_vertex_displacement` | Four objects. Far left: an orange sphere visibly larger, a fifth wider, than the identical orange control beside it (constant Displacement, Scale 0.1 on a radius of 0.5). Third: a blue sphere with two quadrants bulging out and two sunk in, following its grid texture; the 12 cm step where quadrants meet folds the surface over itself, so thin black slivers at that seam and the poles are the fold's back faces, not a defect. Right: a green plane with ripples about 6 cm apart that **travel along X while the scene plays** (`sin(10 x + frame / 4)`; press Play). Shading on every displaced object still follows the original surface, so the bumps and ripples read from their silhouette and from shadowing, not from lighting | The two orange spheres the same size (the modifier did not run, or the normal reader points nowhere in the vertex stage); the inflated sphere **smaller** (the normal reader points inward); the blue sphere smooth (the texture does not sample in the vertex stage); the plane flat or its ripples static | Grey spheres and plane |
| `t29_shader_closures` | Left: an orange sphere at 40 % opacity, the cyan bar visible through it (Mix Shader with a Transparent BSDF, factor 0.6). Middle: a blue cube whose every face is nearly opaque on one half and nearly clear on the other, the bar showing through the clear halves, with a hard vertical edge between them (factor from a texture's red channel, Transparent BSDF on the factor-0 side). Right: a dark sphere glowing green like t07's emitter (Add Shader with an Emission at strength 2) | Left opaque (the mix folded to the surface) or fully clear (the factor's complement was dropped); the middle cube uniformly hazy (the factor collapsed to one value) or its opaque and clear halves swapped (factor side inverted); the right sphere dark (the added emission was lost) | Grey spheres |
| `t30_hair` | Four brown cylinders standing in for strands. Three upright, built along X as smooth hair, rough hair and a plain Principled control; the fourth tilted 35 degrees with the smooth hair material. Each strand's U runs up its axis, the +U convention the hair mapping documents, and the export gives the hair surface the constant strand direction (1, 0, 0) in its tangent frame. A hair highlight is a ring around the strand, and it lands on the strand only where a light sits near the plane square to the strand's axis; Reality Composer Pro's default environment is brightest above, so its rings mostly fall past the strand ends. Under that lighting the smooth strand shows a soft highlight toward its top, the rough strand and the tilted strand read nearly matte, and the control shows ordinary streaky environment reflections down its length. Under a light level with the strands, the smooth strand carries a thin ring across its width, the rough one a broad band, and the tilted strand a ring square to its own tilted axis | All four identical (the hair surface was not built and the material fell back); the smooth and rough strands identical (Roughness did not reach both lobes); the hair strands showing the control's streaky reflections; under a level light, a highlight line running down the length of a strand instead of around it (the direction is +V, not +U), or the tilted strand's ring square to the world's vertical (the strand direction is not reaching the surface in its tangent frame) | Grey cylinders |
| `t31_vector_rotate` | Five matte cubes in a row, each face a colour gradient. The first three look identical, face for face: red strongest along one edge and fading across the face (1 − V), green rising toward the neighbouring edge (U), no blue. The last two look identical: no red, green rising along U, blue rising along V. Each rotated cube is judged only against the control on its left, never against an absolute colour | A rotated cube with red along U and green along V (the angle was read as degrees, so nothing turned); red and green running along the other edges than on the control (the rotation turned the wrong way, or the middle cube lost its Invert); no red on the second or third cube (Center dropped); the last cube bright green all over (the Euler rotations ran in reverse order) | Grey cubes |
| `t32_gamma_checker` | Four cubes, every face lit from within, so lighting plays no part. First: red rising along U and green along V. Second (Gamma 2.2) and third (each channel raised to 2.2 by Math Power nodes, the control) look identical: darker through the middle of each face than the first cube, with the black corner and the fully red and green edges unchanged. Right: exactly four by four orange and navy checks on every face, navy in two opposite corners and orange in the other two | The second and third cubes differing (Gamma exported wrongly) or matching the first (Gamma dropped); checker stripes, a different count, or orange where navy belongs (parity inverted) | Grey cubes |
| `t33_computed_image_coordinates` | Five objects, all lit from within. The first three cubes show the same grid, face for face: the four-colour quadrant grid tiled twice and shifted a quarter of the way along U, stamp included. The first uses the UV transform t10 verifies; the second builds its coordinates with Vector Math; the third feeds its Mapping's Location from a node. Fourth, a sphere, and fifth, a cube, sample the grid by Box projection from object coordinates: each cube face shows the grid unmirrored with a readable stamp, and the sphere shows patches meeting in short soft seams (Projection Blend 0.3) | The second or third cube untiled, unshifted, or tiled differently from the first (a computed coordinate was dropped); a mirrored stamp or stretched streaks on the box-projected cube (wrong side weights or flips); hard seams on the sphere (Blend dropped) | Placeholder stripes |
| `t34_camera_attribute_random` | Back: two floors of repeating grey bands, half a metre apart. On the left floor the bands are rings centred below the camera and stay circles as you orbit (View Distance); on the right they are straight bands parallel to the bottom of the screen that turn as you orbit (View Z Depth). Front left: the attribute cube and its control look identical, black on the -X side fading to white at +X. Front right: four small cubes sharing one material, greys set by Object Info Random: from left, darkest (0.08), brightest (0.67), then two mid greys with the third (0.34) slightly lighter than the fourth (0.27), the values Cycles gives objects with those names | Rings off-centre or curved bands on the right floor (the view matrix and the position disagree); both floors alike; the attribute cube flat or unlike its control (the attribute primvar is not read); all four small cubes black (Random not written) or in another order (the hash differs from Cycles) | Grey floors and cubes |
| `t35_bsdf_presets` | Two rows of six spheres; each front sphere looks like the one behind it. From left: matte green (Diffuse), copper metal reflecting the environment (Metallic), gold metal (Glossy, the one pair allowed to differ, with a brighter rim in front), violet sheen over black (Sheen), soft peach (Subsurface Scattering), and a sphere turning from red plastic on one side to blue metal on the other (a Mix Shader of two Principled BSDFs by U). The export warns that both Diffuse spheres drop Diffuse Roughness, that `PresetSkin` uses Random Walk rather than Random Walk (Skin), and that `MixedPrincipled` blends two differing inputs, Base Color and Metallic, which match Cycles only approximately between the two sides; the mixed pair is judged against its control, not against Cycles | Any other pair that differs: a preset falling back to grey, or losing its specular, roughness or sheen; a black metal on either row (a `sheenColor` is being authored); the mixed sphere uniform, all red or all blue | Grey spheres |
| `t36_bump` | Three upright panels facing you. Left (a normal map computed from the height, through the path t06 verifies) and middle (the Bump node on the same height) shade identically: a three by three field of soft bumps and hollows lit from the same side. Right: the Bump's normal shown as colour in Blender's axes, purple where flat because the panel faces -Y, with three by three bumps each pinker on its right-facing slope and bluer on its left-facing slope. The left and middle panels need a viewport with directional or reflected light to show relief at all; under flat lighting both read plain grey, which proves nothing | The middle panel flat while the left shows relief (the Bump dropped), hollows where the left shows bumps (the gradient's sign is inverted), or lit from another side (the tangent frame is swapped); the right panel lavender blue (the normal left in RealityKit's Y-up axes), flat, or pinker on left-facing slopes | Grey panels |
| `t25_surface_readers` | Four objects. Far left: a dark sphere with a bright white rim (Fresnel). Second: a dark sphere with a red rim (Layer Weight Facing). Third: the view-direction probe, a sphere lit from within, **white at the centre** fading to light grey at the rim (a linear 0.5 displays at about three-quarter brightness). Right: a cube whose faces run black to red along U and black to green along V (Texture Coordinate UV) | Either rim sphere uniform; the probe **black at the centre**, which would mean the view direction's sign changed on the platform, or **flat white**, which would mean it is no longer unit length after the exporter's normalize; the cube flat or its gradients swapped | Placeholder stripes |

The noise scene will **not** match Blender pixel for pixel. That is expected and
warned about; the verdict is only whether it varies at all.

## Animation

| Scene | Correct | Wrong | Not built |
|---|---|---|---|
| `t11_transform_animation` | Two takes: one slides the cube left-to-right, one lifts it | One take only, or both playing the same motion | — |
| `t12_skinned_limb` | The cylinder bends 75° at its midpoint, deforming smoothly | Bends rigidly at the joint, or does not move | — |
| `t13_shape_keys` | `Squash` and `Lean` appear as drivable shapes; driving `Squash` flattens the ball, `Lean` shears it sideways | Driving one produces the other's motion (targets mismatched); shading does not follow the deformation, which is expected — see below | — |

Blend-shape shading is a known Reality Composer Pro limitation: it discards
normal offsets on import, so a driven shape moves its silhouette while lighting
stays at the rest pose. Not an exporter defect.

## Dropped without a warning

These are the highest-value scenes: nothing in the export tells you, so the only
way to notice is to look.

| Scene | Correct | Wrong |
|---|---|---|
| `t14_dropped_lights_cameras` | **Only the yellow cube.** Both lights and the camera are absent from the outliner, and the export reported no warning | Any of them present — the doc is wrong |
| `t15_dropped_curves` | **Only the green cube.** The Bezier circle and NURBS path are gone, and nothing warned | Curves present — the doc is wrong |
| `t16_dropped_world` | A chrome ball reflecting Reality Composer Pro's own environment — **not** magenta | Magenta reflections would mean the world reached the export, which would be new behaviour |

`t16` is deliberately built so its failure is loud: the Blender world is
saturated magenta at strength 4. A chrome ball could not reflect that neutrally
if the world had survived.

## The bake lane

| Scene | Correct | Wrong | Not built |
|---|---|---|---|
| `t20_bake_mask_mix` | Both paths succeed and look the same: direct export authors the mask texture driving a live blend between the two colours, and `bake-export --bake-mode LIT_ALBEDO` bakes that blend into the base colour. Either way the cube ramps from red on one side to blue on the other | A flat single colour, or a faint pink smear, on either path | Placeholder stripes |

```bash
usdstage bake-export References/Blender/t20_bake_mask_mix.blend \
  -o out.usdz --format USDZ --bake-mode LIT_ALBEDO --resolution 256
```

Use **RealityKit PBR ▸ Bake Materials** (`LIT_ALBEDO`) here, not the Lighting & Shadows default:
this scene's single sun bakes black sides under `LIT_IBL`, which defeats a
colour check.

## Refusals — no import needed

| Scene | Expected |
|---|---|
| `t18_refused_mix_shader` | Export **stops**. The message names the Mix Shader and its operands, a Principled BSDF and a Toon BSDF, and points at the **RealityKit Unlit > Lighting & Shadows** bake. Judge whether it tells you enough to act |
| `t19_cm_scale_refusal` | Export **stops** with `Scene unit scale is 0.01, but the RealityKit export contract fixes metersPerUnit at 1.0…`. This guard is why working in centimetres does not ship an asset 100× too large. Judge whether the message tells you how to fix it |
| `t21_specular_tint_refusal` | Export **stops** with `UNSUPPORTED_MATERIAL_NODES`, naming Principled *Specular Tint*: its colour is tinted and brighter than 1, which is refused as a value-policy rather than a surface limitation. Nothing is written. This is the fixture `scripts/verify_apple_platform.sh` uses as its expected rejection, so a change here changes that script |

## The lifecycle check

Worth doing once per release on any textured, animated scene — `t13_shape_keys`
or `t12_skinned_limb` are good candidates. Import it, **save the project, close
it, reopen it**, then open the material in the shader graph editor. Materials
must still be editable graphs rather than flattened, and animation must survive.
Nothing automated reaches this, and it is where round-trip damage shows up.

## Also doubling as automated fixtures

These two are driven by `tests/conftest.py` and
`scripts/verify_apple_platform.sh` as well as by eye. Changing them changes
what the suite exports, so edit them only deliberately.

| Scene | Correct | Wrong | Not built |
|---|---|---|---|
| `t22_red_cube` | A single red cube with one material | Any other colour, or more than one material — the suite's simplest fixture is no longer simple | Grey cube |
| `t23_cube_with_4_animations` | Four takes on one cube, each a distinct motion | Fewer than four takes, or two playing the same motion | — |

## What is not covered

Root prim naming and selection-only are settings, not scenes — point any scene at them rather than storing a variant. USDZ image
formats, custom properties and the diagnostics sidecar are checked by the
automated suite, not by eye.

*These scenes exercise the exporter, not Reality Composer Pro's correctness. A
scene that looks right proves the export survived one path through one build;
it is not a compatibility claim.*
