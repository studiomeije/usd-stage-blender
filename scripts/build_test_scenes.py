"""Generate the small per-feature evaluation scenes in References/Blender.

Run with Blender, not python:

    /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
        --python scripts/build_test_scenes.py

Each scene proves ONE behaviour, and is built so a *wrong* result looks
obviously wrong. That is harder than it sounds, and this kit has shipped probes
that tested nothing: two textures that were byte-identical, a roughness map of
0.502 against a 0.5 default, an identity normal map. The rules below exist to
stop that recurring. See References/Blender/TEST_SCENES.md for what each scene
should look like in Reality Composer Pro.

Design rules:

- **Ship a control in frame.** A verdict about a continuous value needs a
  known-good twin differing in exactly one input, so the pass condition is a
  visible difference rather than an absolute nobody can eyeball.
- **Never say "compare against the Blender render."** Lights are dropped
  silently, so the Blender render is lit by a sun that never reaches the export
  while Reality Composer Pro relights under its own environment. A mismatch is
  guaranteed and proves nothing.
- **Roughness probes are metallic.** On a dielectric, roughness modulates a weak
  specular lobe. On metal it is sharp-versus-blurred reflection - a difference
  of kind, not degree.
- **No value within 0.1 of a Blender default**, and no flat-fill data texture.
  A probe that lands on the default passes when the feature is dropped.
- **Asymmetric, chiral geometry** wherever orientation or handedness is the
  verdict. A symmetric object cannot show a flip.
- **Few large texels for filtering probes.** Closest-versus-Linear needs a 16 px
  image, not a big one: at 256 px the texels are ~9 mm and you would have to put
  your nose on the surface to judge it.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import bpy

OUT = Path(__file__).resolve().parents[1] / "References" / "Blender"

#: Every image the kit writes, by SHA-256, so two probes meant to be told apart
#: cannot silently be the same picture. Staging deduplicates by digest.
_IMAGE_DIGESTS: dict[str, str] = {}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def fresh(cycles: bool = False, samples: int = 16) -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.scale_length = 1.0
    if cycles:
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = samples


def save(name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True)
    print(f"BUILT {name}.blend {path.stat().st_size}")


def _register(image) -> None:
    # Pack, or the pixels never reach the file. A GENERATED image's buffer is
    # not serialized: Blender regenerates it from generated_color on load, so
    # an unpacked probe texture opens as a flat fill and the scene silently
    # proves nothing. Every image in this kit shipped black once because of it.
    image.pack()
    digest = hashlib.sha256(bytes(int(c * 255) for c in image.pixels)).hexdigest()
    clash = next((n for n, d in _IMAGE_DIGESTS.items() if d == digest), None)
    if clash:
        raise SystemExit(
            f"image {image.name!r} is byte-identical to {clash!r}; two probes "
            "that must be told apart cannot be the same picture"
        )
    _IMAGE_DIGESTS[image.name] = digest


def grid_image(name: str, size: int = 128):
    """Four distinct quadrants plus a per-name corner stamp.

    The stamp encodes the image's name, so two grids built with the same
    parameters are still distinguishable - both to the eye and to the staging
    layer, which deduplicates by digest.
    """
    image = bpy.data.images.new(name, size, size)
    half = size // 2
    tag = int(hashlib.sha256(name.encode()).hexdigest()[:6], 16)
    stamp = ((tag & 0xFF) / 255.0, ((tag >> 8) & 0xFF) / 255.0,
             ((tag >> 16) & 0xFF) / 255.0)
    pixels = []
    for y in range(size):
        for x in range(size):
            r = 0.90 if x < half else 0.10
            g = 0.85 if y < half else 0.12
            b = 0.15 if (x < half) == (y < half) else 0.75
            # A corner stamp marks (0,0) and makes this image unique.
            if x < max(2, size // 16) and y < max(2, size // 16):
                r, g, b = stamp
            pixels += [r, g, b, 1.0]
    image.pixels = pixels
    _register(image)
    return image


def ramp_image(name: str, size: int = 64, *, axis: str = "x", data: bool = True):
    """A gradient. Flat fills cannot show a dropped link; a ramp can."""
    image = bpy.data.images.new(name, size, size, is_data=data)
    # A per-name offset on the flat channels, so two ramps built with the same
    # parameters are not the same picture. Small enough not to change the read.
    tag = int(hashlib.sha256(name.encode()).hexdigest()[:4], 16) / 65535.0
    pixels = []
    for y in range(size):
        for x in range(size):
            t = (x if axis == "x" else y) / (size - 1)
            # Distinct per channel: reading the wrong channel is then visible.
            pixels += [t, 0.15 + tag * 0.02, 0.85 - tag * 0.02, 1.0]
    image.pixels = pixels
    _register(image)
    return image


def normal_image(name: str, size: int = 64):
    """A strong tangent-space normal map - NOT the identity normal.

    An identity map (0.5, 0.5, 1.0) is indistinguishable from no normal map at
    all, so a probe built on it would prove nothing.
    """
    image = bpy.data.images.new(name, size, size, is_data=True)
    pixels = []
    for y in range(size):
        for x in range(size):
            # A lattice of round bumps: a strong perturbation on both axes.
            angle = math.sin((x / size) * math.pi * 6.0) * 0.7
            nx, ny = angle, math.sin((y / size) * math.pi * 6.0) * 0.7
            nz = math.sqrt(max(0.05, 1.0 - nx * nx - ny * ny))
            pixels += [nx * 0.5 + 0.5, ny * 0.5 + 0.5, nz, 1.0]
    image.pixels = pixels
    _register(image)
    return image


def cube(name: str, *, location=(0, 0, 0), size: float = 1.0):
    bpy.ops.mesh.primitive_cube_add(size=size, location=location)
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = f"{name}Mesh"
    return obj


def sphere(name: str, *, location=(0, 0, 0), radius: float = 0.5):
    """A 16x8 sphere: 114 verts, enough for a reflection, a fifth the cost of
    Blender's 482-vertex default."""
    bpy.ops.mesh.primitive_uv_sphere_add(
        segments=16, ring_count=8, radius=radius, location=location
    )
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = f"{name}Mesh"
    bpy.ops.object.shade_smooth()
    return obj


def unwrap(obj) -> None:
    """cube_project, never smart_project: smart_project repacks when geometry
    changes, so any UV-space claim can drift with no exporter change."""
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.cube_project(cube_size=1.0)
    bpy.ops.object.mode_set(mode='OBJECT')


def material(obj, name: str):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    obj.data.materials.append(mat)
    tree = mat.node_tree
    return tree, tree.nodes["Principled BSDF"], mat


def texture_node(tree, image, *, interpolation: str = 'Linear',
                 extension: str = 'REPEAT'):
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.interpolation = interpolation
    node.extension = extension
    return node


def sun(energy: float = 5.0) -> None:
    bpy.ops.object.light_add(type='SUN', location=(2, -2, 4))
    bpy.context.active_object.data.energy = energy


# --------------------------------------------------------------------------
# scenes
# --------------------------------------------------------------------------

def scene_orientation() -> None:
    """Y-up conversion and meter scale, on a shape that cannot hide a flip."""
    fresh()
    # An L: tall arm on +X, short foot on +Y. Chiral, so a mirrored or rotated
    # result is unmistakable; 1 m tall with its base on the floor.
    body = cube("Upright", location=(0, 0, 0.5), size=1.0)
    body.scale = (0.18, 0.18, 1.0)
    foot = cube("Foot", location=(0.34, 0, 0.09), size=1.0)
    foot.scale = (0.5, 0.16, 0.18)
    nub = cube("SideNub", location=(0, 0.3, 0.85), size=1.0)
    nub.scale = (0.14, 0.4, 0.14)
    for obj, colour in ((body, (0.8, 0.1, 0.1, 1)), (foot, (0.1, 0.7, 0.2, 1)),
                        (nub, (0.1, 0.3, 0.9, 1))):
        _tree, bsdf, _mat = material(obj, f"{obj.name}Surface")
        bsdf.inputs["Base Color"].default_value = colour
    sun()
    save("t01_orientation_scale")


def scene_uv_layout() -> None:
    """UVs reach the export and address the image the way Blender did."""
    fresh()
    obj = cube("UVCube", size=1.0)
    unwrap(obj)
    tree, bsdf, _mat = material(obj, "UVGrid")
    node = texture_node(tree, grid_image("uv_grid", 128))
    tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    sun()
    save("t02_uv_layout")


def scene_vertex_color() -> None:
    """Vertex colour reaches RealityKit through displayColor."""
    fresh()
    obj = cube("PaintedCube", size=1.0)
    mesh = obj.data
    layer = mesh.color_attributes.new(name="Paint", type='FLOAT_COLOR',
                                      domain='CORNER')
    # A per-corner ramp along z, so a flat fill or a dropped read is visible.
    for poly in mesh.polygons:
        for loop_index in poly.loop_indices:
            vertex = mesh.vertices[mesh.loops[loop_index].vertex_index]
            t = vertex.co.z + 0.5
            layer.data[loop_index].color = (t, 0.15, 1.0 - t, 1.0)
    tree, bsdf, _mat = material(obj, "VertexPaint")
    attr = tree.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = "Paint"
    tree.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    sun()
    save("t03_vertex_color")


def scene_multi_material() -> None:
    """Two materials on one mesh stay on their own faces."""
    fresh()
    obj = cube("DuoCube", size=1.0)
    tree_a, bsdf_a, _a = material(obj, "SlotRed")
    bsdf_a.inputs["Base Color"].default_value = (0.85, 0.08, 0.08, 1)
    mat_b = bpy.data.materials.new("SlotBlue")
    mat_b.use_nodes = True
    mat_b.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (
        0.08, 0.20, 0.90, 1
    )
    obj.data.materials.append(mat_b)
    # Alternating faces, so a swap or a collapse to one material is obvious.
    for index, poly in enumerate(obj.data.polygons):
        poly.material_index = index % 2
    sun()
    save("t04_multi_material")


def scene_metal_roughness() -> None:
    """Roughness reaches the surface. Metallic, so it reads as sharp vs blurred."""
    fresh()
    probe = sphere("RoughRamp", location=(-0.7, 0, 0.5))
    unwrap(probe)
    tree, bsdf, _mat = material(probe, "MetalRamp")
    bsdf.inputs["Metallic"].default_value = 1.0
    node = texture_node(tree, ramp_image("rough_ramp", 64))
    tree.links.new(node.outputs["Color"], bsdf.inputs["Roughness"])

    # The control: same metal, one fixed low roughness. Nothing else differs.
    control = sphere("MirrorControl", location=(0.7, 0, 0.5))
    _t, control_bsdf, _m = material(control, "MetalControl")
    control_bsdf.inputs["Metallic"].default_value = 1.0
    control_bsdf.inputs["Roughness"].default_value = 0.05
    sun()
    save("t05_metal_roughness")


def scene_normal_map() -> None:
    """A normal map perturbs shading. Strong ridges, never the identity normal."""
    fresh()
    probe = cube("BumpyPanel", location=(-0.7, 0, 0.5), size=1.0)
    probe.scale = (1.0, 0.08, 1.0)
    unwrap(probe)
    tree, bsdf, _mat = material(probe, "Ridged")
    bsdf.inputs["Roughness"].default_value = 0.25
    node = texture_node(tree, normal_image("ridges", 64))
    node.image.colorspace_settings.name = 'Non-Color'
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.inputs["Strength"].default_value = 1.0
    tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

    control = cube("FlatPanel", location=(0.7, 0, 0.5), size=1.0)
    control.scale = (1.0, 0.08, 1.0)
    _t, control_bsdf, _m = material(control, "Smooth")
    control_bsdf.inputs["Roughness"].default_value = 0.25

    # The same map at a third of the strength. The exporter expresses Strength
    # as a tangent-space mix toward the flat normal, so this panel must show the
    # same lattice at visibly less relief - never flat, never inverted.
    faint = cube("FaintPanel", location=(-2.1, 0, 0.5), size=1.0)
    faint.scale = (1.0, 0.08, 1.0)
    unwrap(faint)
    faint_tree, faint_bsdf, _fm = material(faint, "RidgedFaint")
    faint_bsdf.inputs["Roughness"].default_value = 0.25
    # The very same image datablock: only the Strength differs.
    faint_node = texture_node(faint_tree, node.image)
    faint_map = faint_tree.nodes.new("ShaderNodeNormalMap")
    faint_map.inputs["Strength"].default_value = 0.35
    faint_tree.links.new(faint_node.outputs["Color"], faint_map.inputs["Color"])
    faint_tree.links.new(faint_map.outputs["Normal"], faint_bsdf.inputs["Normal"])
    sun()
    save("t06_normal_map")


def scene_emission() -> None:
    """Emissive colour reaches the surface, against a non-emissive twin."""
    fresh()
    glow = sphere("Glowing", location=(-0.7, 0, 0.5))
    _t, bsdf, _m = material(glow, "Emissive")
    bsdf.inputs["Base Color"].default_value = (0.05, 0.05, 0.05, 1)
    bsdf.inputs["Emission Color"].default_value = (0.1, 1.0, 0.35, 1)
    bsdf.inputs["Emission Strength"].default_value = 3.0

    dark = sphere("Unlit", location=(0.7, 0, 0.5))
    _t2, dark_bsdf, _m2 = material(dark, "NotEmissive")
    dark_bsdf.inputs["Base Color"].default_value = (0.05, 0.05, 0.05, 1)
    sun(energy=1.0)
    save("t07_emission")


def scene_opacity() -> None:
    """Opacity reaches the surface, against an opaque twin."""
    fresh()
    glass = sphere("SeeThrough", location=(-0.7, 0, 0.5))
    _t, bsdf, mat = material(glass, "Translucent")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.35, 0.1, 1)
    bsdf.inputs["Alpha"].default_value = 0.35
    bsdf.inputs["Roughness"].default_value = 0.3
    mat.blend_method = 'BLEND'

    solid = sphere("Opaque", location=(0.7, 0, 0.5))
    _t2, solid_bsdf, _m2 = material(solid, "Solid")
    solid_bsdf.inputs["Base Color"].default_value = (0.9, 0.35, 0.1, 1)
    solid_bsdf.inputs["Roughness"].default_value = 0.3
    # A backdrop, so "transparent" means "you can see the bar through it".
    bar = cube("Backdrop", location=(0, 0.6, 0.5), size=1.0)
    bar.scale = (2.0, 0.05, 0.35)
    _t3, bar_bsdf, _m3 = material(bar, "BackdropStripe")
    bar_bsdf.inputs["Base Color"].default_value = (0.05, 0.85, 0.9, 1)
    sun()
    save("t08_opacity")


def scene_wrap_filter() -> None:
    """Extension and interpolation modes reach the image reader.

    16 px, so one texel is roughly 60 mm on this cube: Closest reads as hard
    squares at normal framing. At 256 px the texels are millimetres and the
    difference is invisible without putting your nose on the surface.
    """
    fresh()
    tiny = grid_image("filter_probe", 16)

    repeat_linear = cube("RepeatLinear", location=(-0.7, 0, 0.5), size=1.0)
    unwrap(repeat_linear)
    tree, bsdf, _m = material(repeat_linear, "RepeatLinear")
    node = texture_node(tree, tiny, interpolation='Linear', extension='REPEAT')
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (3.0, 3.0, 1.0)
    texcoord = tree.nodes.new("ShaderNodeTexCoord")
    tree.links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])

    clip_closest = cube("ClipClosest", location=(0.7, 0, 0.5), size=1.0)
    unwrap(clip_closest)
    tree2, bsdf2, _m2 = material(clip_closest, "ClipClosest")
    node2 = texture_node(tree2, tiny, interpolation='Closest', extension='CLIP')
    mapping2 = tree2.nodes.new("ShaderNodeMapping")
    mapping2.inputs["Scale"].default_value = (3.0, 3.0, 1.0)
    texcoord2 = tree2.nodes.new("ShaderNodeTexCoord")
    tree2.links.new(texcoord2.outputs["UV"], mapping2.inputs["Vector"])
    tree2.links.new(mapping2.outputs["Vector"], node2.inputs["Vector"])
    tree2.links.new(node2.outputs["Color"], bsdf2.inputs["Base Color"])
    sun()
    save("t09_wrap_filter")


def scene_texture_transform() -> None:
    """A Mapping node's scale and rotation reach the UV transform."""
    fresh()
    obj = cube("MappedCube", size=1.0)
    unwrap(obj)
    tree, bsdf, _m = material(obj, "Mapped")
    node = texture_node(tree, grid_image("transform_grid", 128))
    mapping = tree.nodes.new("ShaderNodeMapping")
    # 3x tiling plus a 30 degree rotation: dropped, inverted and un-rotated
    # results all land somewhere visibly different.
    mapping.inputs["Scale"].default_value = (3.0, 3.0, 1.0)
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(30.0))
    texcoord = tree.nodes.new("ShaderNodeTexCoord")
    tree.links.new(texcoord.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    sun()
    save("t10_texture_transform")


def scene_transform_animation() -> None:
    """Object animation survives as takes."""
    fresh()
    obj = cube("Mover", location=(0, 0, 0.5), size=0.6)
    _t, bsdf, _m = material(obj, "MoverSurface")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.5, 0.1, 1)
    obj.animation_data_create()

    slide = bpy.data.actions.new("SlideRight")
    obj.animation_data.action = slide
    for frame, x in ((1, -1.2), (24, 1.2)):
        obj.location.x = x
        obj.keyframe_insert("location", index=0, frame=frame)

    lift = bpy.data.actions.new("LiftUp")
    obj.animation_data.action = lift
    for frame, z in ((1, 0.5), (24, 1.8)):
        obj.location.z = z
        obj.keyframe_insert("location", index=2, frame=frame)

    obj.animation_data.action = None
    for action in (slide, lift):
        track = obj.animation_data.nla_tracks.new()
        track.name = action.name
        track.strips.new(action.name, 1, action)

    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 24
    sun()
    save("t11_transform_animation")


def scene_skinned() -> None:
    """Armature skinning survives, and the bend is unmistakable."""
    fresh()
    bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.18, depth=2.0,
                                        location=(0, 0, 1.0))
    mesh_obj = bpy.context.active_object
    mesh_obj.name = "Limb"
    mesh_obj.data.name = "LimbMesh"
    # Loop cuts so the bend deforms rather than pivoting rigidly.
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.subdivide(number_cuts=6)
    bpy.ops.object.mode_set(mode='OBJECT')
    _t, bsdf, _m = material(mesh_obj, "LimbSurface")
    bsdf.inputs["Base Color"].default_value = (0.8, 0.75, 0.2, 1)

    bpy.ops.object.armature_add(location=(0, 0, 0))
    rig = bpy.context.active_object
    rig.name = "Rig"
    bpy.ops.object.mode_set(mode='EDIT')
    root = rig.data.edit_bones[0]
    root.name = "lower"
    root.head = (0, 0, 0)
    root.tail = (0, 0, 1.0)
    upper = rig.data.edit_bones.new("upper")
    upper.head = (0, 0, 1.0)
    upper.tail = (0, 0, 2.0)
    upper.parent = root
    bpy.ops.object.mode_set(mode='OBJECT')

    mesh_obj.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type='ARMATURE_AUTO')

    rig.animation_data_create()
    action = bpy.data.actions.new("Bend")
    rig.animation_data.action = action
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='POSE')
    bone = rig.pose.bones["upper"]
    bone.rotation_mode = 'XYZ'
    for frame, angle in ((1, 0.0), (24, math.radians(75.0))):
        bone.rotation_euler = (angle, 0, 0)
        bone.keyframe_insert("rotation_euler", frame=frame)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = 24
    sun()
    save("t12_skinned_limb")


def scene_shape_keys() -> None:
    """Shape keys reach USD and can be driven in the editor."""
    fresh()
    obj = sphere("KeyedBall", location=(0, 0, 0.6), radius=0.5)
    _t, bsdf, _m = material(obj, "KeyedSurface")
    bsdf.inputs["Base Color"].default_value = (0.2, 0.55, 0.9, 1)
    obj.shape_key_add(name="Basis", from_mix=False)

    squash = obj.shape_key_add(name="Squash", from_mix=False)
    for vertex in squash.data:
        vertex.co.z *= 0.35
    lean = obj.shape_key_add(name="Lean", from_mix=False)
    for vertex in lean.data:
        vertex.co.x += 0.9 * (vertex.co.z + 0.5)
    sun()
    save("t13_shape_keys")


def scene_dropped_lights_cameras() -> None:
    """Lights and cameras vanish with no warning. The mesh proves the export ran."""
    fresh()
    obj = cube("SurvivingCube", location=(0, 0, 0.5), size=1.0)
    _t, bsdf, _m = material(obj, "Survivor")
    bsdf.inputs["Base Color"].default_value = (0.85, 0.75, 0.2, 1)
    bpy.ops.object.light_add(type='POINT', location=(1.5, -1.5, 2.0))
    bpy.context.active_object.name = "ShouldVanish_Point"
    bpy.ops.object.light_add(type='AREA', location=(-1.5, 1.5, 2.0))
    bpy.context.active_object.name = "ShouldVanish_Area"
    bpy.ops.object.camera_add(location=(0, -4, 1.5), rotation=(1.3, 0, 0))
    bpy.context.active_object.name = "ShouldVanish_Camera"
    save("t14_dropped_lights_cameras")


def scene_dropped_curves() -> None:
    """Curve, text and point-cloud objects vanish with no warning."""
    fresh()
    obj = cube("SurvivingCube", location=(0, 0, 0.5), size=0.8)
    _t, bsdf, _m = material(obj, "Survivor")
    bsdf.inputs["Base Color"].default_value = (0.2, 0.8, 0.5, 1)
    # Curve objects drop. Text is NOT in this scene: Blender converts a Text
    # object to a mesh, so it survives the export and would muddy the verdict.
    bpy.ops.curve.primitive_bezier_circle_add(radius=0.9, location=(1.6, 0, 0.5))
    bpy.context.active_object.name = "ShouldVanish_Curve"
    bpy.ops.curve.primitive_nurbs_path_add(location=(-1.6, 0, 0.5))
    bpy.context.active_object.name = "ShouldVanish_Path"
    sun()
    save("t15_dropped_curves")


def scene_dropped_world() -> None:
    """The world material vanishes with no warning."""
    fresh()
    obj = sphere("MirrorBall", location=(0, 0, 0.6), radius=0.5)
    _t, bsdf, _m = material(obj, "Chrome")
    bsdf.inputs["Metallic"].default_value = 1.0
    bsdf.inputs["Roughness"].default_value = 0.05
    world = bpy.data.worlds.new("LoudWorld")
    world.use_nodes = True
    background = world.node_tree.nodes["Background"]
    # Saturated magenta at high strength: if the world reached the export, a
    # chrome ball could not possibly look neutral.
    background.inputs["Color"].default_value = (1.0, 0.0, 0.7, 1.0)
    background.inputs["Strength"].default_value = 4.0
    bpy.context.scene.world = world
    save("t16_dropped_world")


def scene_procedural_noise() -> None:
    """Noise exports as a MaterialX procedural - approximated, not refused.

    Checker, Brick and Wave refuse; Noise and Voronoi are translated and
    warned about. The control makes the approximation legible: if the noise
    were dropped, both spheres would have the same uniform finish.
    """
    fresh()
    probe = sphere("NoisyMetal", location=(-0.7, 0, 0.5))
    tree, bsdf, _m = material(probe, "ProceduralRough")
    bsdf.inputs["Metallic"].default_value = 1.0
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.name = "ObjectSpaceNoise"
    noise.inputs["Scale"].default_value = 12.0
    tree.links.new(noise.outputs["Fac"], bsdf.inputs["Roughness"])

    control = sphere("EvenMetal", location=(0.7, 0, 0.5))
    _t, control_bsdf, _cm = material(control, "EvenRough")
    control_bsdf.inputs["Metallic"].default_value = 1.0
    control_bsdf.inputs["Roughness"].default_value = 0.35
    sun()
    save("t17_procedural_noise")


def scene_refused_mix_shader() -> None:
    """A Mix Shader with no RealityKit form refuses the export and points at the bake.

    Its second shader is a Toon BSDF: two Principled-like shaders would blend
    into one surface and export.
    """
    fresh()
    obj = cube("MixedCube", location=(0, 0, 0.5), size=1.0)
    tree, bsdf, mat = material(obj, "Mixed")
    second = tree.nodes.new("ShaderNodeBsdfToon")
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.inputs["Fac"].default_value = 0.5
    output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
    tree.links.new(bsdf.outputs["BSDF"], mix.inputs[1])
    tree.links.new(second.outputs["BSDF"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
    sun()
    save("t18_refused_mix_shader")


def scene_cm_scale_refusal() -> None:
    """A centimetre scene is refused, not silently exported 100x too large.

    This guard is why an artist working in centimetres does not ship an asset
    the size of a building. The scene is otherwise identical to t01, so the two
    isolate the unit setting.
    """
    fresh()
    bpy.context.scene.unit_settings.scale_length = 0.01
    body = cube("Upright", location=(0, 0, 50.0), size=100.0)
    body.scale = (0.18, 0.18, 1.0)
    _t, bsdf, _m = material(body, "CmSurface")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.4, 0.1, 1)
    sun()
    save("t19_cm_scale_refusal")


def scene_bake_mask_mix() -> None:
    """The bake lane's fixture: a mask-driven blend between two colours.

    Direct export authors the mask texture driving a live MaterialX mix;
    ``bake-export`` bakes that blend into a base-colour texture. The mask is a
    gradient, so a bake that lost the mix is a flat colour rather than a blend.

    The Factor takes the ramp's red channel alone. Wiring the whole colour into
    the float socket lets Blender fold the ramp's constant green and blue into
    the factor, which measured as a pink smear from 0.17 to 0.38 rather than a
    red-to-blue swing - in Blender's own render as much as in the bake.
    """
    fresh(cycles=True, samples=16)
    obj = cube("MaskedCube", location=(0, 0, 0.5), size=1.0)
    unwrap(obj)
    tree, bsdf, _m = material(obj, "MaskedMix")
    mask = texture_node(tree, ramp_image("bake_mask", 64, data=True))
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = 'RGBA'
    mix.inputs["A"].default_value = (0.95, 0.15, 0.05, 1.0)
    mix.inputs["B"].default_value = (0.05, 0.35, 0.95, 1.0)
    tree.links.new(mask.outputs["Color"], separate.inputs["Color"])
    tree.links.new(separate.outputs["Red"], mix.inputs["Factor"])
    tree.links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])
    sun()
    save("t20_bake_mask_mix")


def scene_specular_tint_refusal() -> None:
    """A coloured, overbright Specular Tint is refused as a value policy.

    This replaces a 16 MB rigged character whose only job was to trip this
    refusal. The exporter clamps an overbright *achromatic* tint only when
    Normalize Unsupported Values is on; a coloured tint is refused outright,
    which is the case worth pinning.
    """
    fresh()
    obj = sphere("TintedBall", location=(0, 0, 0.6))
    _t, bsdf, _m = material(obj, "TintedSurface")
    bsdf.inputs["Base Color"].default_value = (0.7, 0.7, 0.75, 1)
    # Coloured and overbright: refused regardless of the normalization setting.
    bsdf.inputs["Specular Tint"].default_value = (1.8, 0.4, 0.4, 1.0)
    sun()
    save("t21_specular_tint_refusal")


def scene_surface_readers() -> None:
    """Surface readers: Blender input nodes exported as MaterialX readers.

    Fresnel and Layer Weight are transcribed from Cycles over the world normal
    and Apple's surface view direction, so their look is predictable: a bright
    rim on a dark sphere. The third sphere exists because one fact is not
    recorded anywhere this exporter can read: whether Apple's view direction
    points from the surface toward the viewer, as Blender's Incoming does. It
    shows (dot(Incoming, Normal) + 1) / 2 as emission, which is white at the
    centre if the two agree and black at the centre if they do not. The cube
    reads its UV set through Texture Coordinate; red grows along U and green
    along V on every face.
    """
    fresh()

    rim = sphere("FresnelRim", location=(-2.1, 0, 0.5))
    tree, bsdf, _m = material(rim, "FresnelRim")
    bsdf.inputs["Roughness"].default_value = 0.6
    fresnel = tree.nodes.new("ShaderNodeFresnel")
    fresnel.inputs["IOR"].default_value = 1.45
    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = 'RGBA'
    mix.inputs["A"].default_value = (0.03, 0.03, 0.03, 1.0)
    mix.inputs["B"].default_value = (1.0, 1.0, 1.0, 1.0)
    tree.links.new(fresnel.outputs["Factor"], mix.inputs["Factor"])
    tree.links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])

    facing = sphere("FacingRim", location=(-0.7, 0, 0.5))
    tree, bsdf, _m = material(facing, "FacingRim")
    bsdf.inputs["Roughness"].default_value = 0.6
    layer = tree.nodes.new("ShaderNodeLayerWeight")
    layer.inputs["Blend"].default_value = 0.5
    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = 'RGBA'
    mix.inputs["A"].default_value = (0.03, 0.03, 0.03, 1.0)
    mix.inputs["B"].default_value = (0.9, 0.05, 0.05, 1.0)
    tree.links.new(layer.outputs["Facing"], mix.inputs["Factor"])
    tree.links.new(mix.outputs["Result"], bsdf.inputs["Base Color"])

    probe = sphere("IncomingSign", location=(0.7, 0, 0.5))
    tree, bsdf, _m = material(probe, "IncomingSign")
    bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    bsdf.inputs["Emission Strength"].default_value = 1.0
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    dot = tree.nodes.new("ShaderNodeVectorMath")
    dot.operation = 'DOT_PRODUCT'
    tree.links.new(geometry.outputs["Incoming"], dot.inputs[0])
    tree.links.new(geometry.outputs["Normal"], dot.inputs[1])
    plus_one = tree.nodes.new("ShaderNodeMath")
    plus_one.operation = 'ADD'
    plus_one.inputs[1].default_value = 1.0
    tree.links.new(dot.outputs["Value"], plus_one.inputs[0])
    half = tree.nodes.new("ShaderNodeMath")
    half.operation = 'MULTIPLY'
    half.inputs[1].default_value = 0.5
    tree.links.new(plus_one.outputs["Value"], half.inputs[0])
    tree.links.new(half.outputs["Value"], bsdf.inputs["Emission Color"])

    uv_cube = cube("UVColor", location=(2.1, 0, 0.5), size=1.0)
    unwrap(uv_cube)
    tree, bsdf, _m = material(uv_cube, "UVColor")
    bsdf.inputs["Roughness"].default_value = 0.8
    coords = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    combine = tree.nodes.new("ShaderNodeCombineColor")
    tree.links.new(coords.outputs["UV"], separate.inputs["Vector"])
    tree.links.new(separate.outputs["X"], combine.inputs["Red"])
    tree.links.new(separate.outputs["Y"], combine.inputs["Green"])
    combine.inputs["Blue"].default_value = 0.0
    tree.links.new(combine.outputs["Color"], bsdf.inputs["Base Color"])

    sun()
    save("t25_surface_readers")


def scene_vector_math() -> None:
    """Vector Math and Combine XYZ, exported as MaterialX vector nodes.

    Left sphere: the world normal reflected about the vertical axis and shown
    as a colour, so the top and bottom halves swap their green tint against a
    plain normal sphere; the arithmetic is Reflect, Normalize and Scale.
    Middle sphere: the world normal as a colour, the control. Right cube: a
    Combine XYZ of (0.9, 0.2, 0.1) scaled by Length of the UV vector, so the
    colour is a warm tint whose brightness grows toward the (1, 1) corner of
    each face.
    """
    fresh()

    def normal_as_colour(tree, bsdf, vector_output):
        # (v * 0.5 + 0.5) as a colour: Scale, then Add a constant, then split
        # and recombine so the vector reaches Base Color as a colour.
        half = tree.nodes.new("ShaderNodeVectorMath")
        half.operation = 'SCALE'
        half.inputs["Scale"].default_value = 0.5
        tree.links.new(vector_output, half.inputs[0])
        offset = tree.nodes.new("ShaderNodeVectorMath")
        offset.operation = 'ADD'
        offset.inputs[1].default_value = (0.5, 0.5, 0.5)
        tree.links.new(half.outputs["Vector"], offset.inputs[0])
        separate = tree.nodes.new("ShaderNodeSeparateXYZ")
        combine = tree.nodes.new("ShaderNodeCombineColor")
        tree.links.new(offset.outputs["Vector"], separate.inputs["Vector"])
        tree.links.new(separate.outputs["X"], combine.inputs["Red"])
        tree.links.new(separate.outputs["Y"], combine.inputs["Green"])
        tree.links.new(separate.outputs["Z"], combine.inputs["Blue"])
        tree.links.new(combine.outputs["Color"], bsdf.inputs["Base Color"])
        bsdf.inputs["Roughness"].default_value = 0.9

    reflected = sphere("ReflectedNormal", location=(-1.4, 0, 0.5))
    tree, bsdf, _m = material(reflected, "ReflectedNormal")
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    reflect = tree.nodes.new("ShaderNodeVectorMath")
    reflect.operation = 'REFLECT'
    reflect.inputs[1].default_value = (0.0, 0.0, 1.0)
    tree.links.new(geometry.outputs["Normal"], reflect.inputs[0])
    normalized = tree.nodes.new("ShaderNodeVectorMath")
    normalized.operation = 'NORMALIZE'
    tree.links.new(reflect.outputs["Vector"], normalized.inputs[0])
    normal_as_colour(tree, bsdf, normalized.outputs["Vector"])

    control = sphere("PlainNormal", location=(0.0, 0, 0.5))
    tree, bsdf, _m = material(control, "PlainNormal")
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    normal_as_colour(tree, bsdf, geometry.outputs["Normal"])

    tinted = cube("TintByLength", location=(1.4, 0, 0.5), size=1.0)
    unwrap(tinted)
    tree, bsdf, _m = material(tinted, "TintByLength")
    bsdf.inputs["Roughness"].default_value = 0.9
    coords = tree.nodes.new("ShaderNodeTexCoord")
    length = tree.nodes.new("ShaderNodeVectorMath")
    length.operation = 'LENGTH'
    tree.links.new(coords.outputs["UV"], length.inputs[0])
    tint = tree.nodes.new("ShaderNodeCombineXYZ")
    tint.inputs["X"].default_value = 0.9
    tint.inputs["Y"].default_value = 0.2
    tint.inputs["Z"].default_value = 0.1
    scaled = tree.nodes.new("ShaderNodeVectorMath")
    scaled.operation = 'SCALE'
    tree.links.new(tint.outputs["Vector"], scaled.inputs[0])
    tree.links.new(length.outputs["Value"], scaled.inputs["Scale"])
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    combine = tree.nodes.new("ShaderNodeCombineColor")
    tree.links.new(scaled.outputs["Vector"], separate.inputs["Vector"])
    tree.links.new(separate.outputs["X"], combine.inputs["Red"])
    tree.links.new(separate.outputs["Y"], combine.inputs["Green"])
    tree.links.new(separate.outputs["Z"], combine.inputs["Blue"])
    tree.links.new(combine.outputs["Color"], bsdf.inputs["Base Color"])

    sun()
    save("t26_vector_math")


def scene_frame_drivers() -> None:
    """Scripted drivers over ``frame``, exported as RealityKit's time reader.

    Blender's background process never evaluates a driver, so the exporter
    turns ``frame`` into ``time * fps`` on the material's own clock. Left sphere: a Value node
    driven by ``sin(frame * 0.1) * 2 + 2`` feeds Emission Strength, so the glow
    pulses between off and bright with a period of 2 * pi / 0.1 frames, which
    is 2.6 seconds at the scene's 24 fps. Right cube: a Mix node whose Factor
    is driven directly by ``frame / 48 - floor(frame / 48)``, a sawtooth that
    ramps the cube from red to blue over two seconds and snaps back.
    """
    fresh()
    bpy.context.scene.render.fps = 24
    bpy.context.scene.render.fps_base = 1.0

    def drive(socket, expression: str) -> None:
        fcurve = socket.driver_add("default_value")
        fcurve.driver.type = 'SCRIPTED'
        fcurve.driver.expression = expression

    glow = sphere("PulsingGlow", location=(-0.9, 0, 0.5))
    tree, bsdf, _m = material(glow, "PulsingGlow")
    bsdf.inputs["Base Color"].default_value = (0.05, 0.05, 0.05, 1)
    bsdf.inputs["Emission Color"].default_value = (0.1, 1.0, 0.35, 1)
    pulse = tree.nodes.new("ShaderNodeValue")
    pulse.name = "Pulse"
    pulse.label = "Pulse"
    drive(pulse.outputs[0], "sin(frame * 0.1) * 2 + 2")
    tree.links.new(pulse.outputs[0], bsdf.inputs["Emission Strength"])

    ramp = cube("SawtoothMix", location=(0.9, 0, 0.5), size=1.0)
    tree, bsdf, _m = material(ramp, "SawtoothMix")
    bsdf.inputs["Roughness"].default_value = 0.9
    mix = tree.nodes.new("ShaderNodeMix")
    mix.name = "Mix"
    mix.data_type = 'RGBA'
    mix.blend_type = 'MIX'
    colour_a = next(s for s in mix.inputs if s.name == "A" and s.type == 'RGBA')
    colour_b = next(s for s in mix.inputs if s.name == "B" and s.type == 'RGBA')
    colour_a.default_value = (0.9, 0.05, 0.05, 1)
    colour_b.default_value = (0.05, 0.05, 0.9, 1)
    drive(mix.inputs[0], "frame / 48 - floor(frame / 48)")
    result = next(s for s in mix.outputs if s.name == "Result" and s.type == 'RGBA')
    tree.links.new(result, bsdf.inputs["Base Color"])
    sun(energy=1.0)
    save("t27_frame_drivers")


def scene_vertex_displacement() -> None:
    """The Displacement socket, exported as RealityKit's geometry modifier.

    Four objects. Far left: a sphere inflated by a constant Displacement
    (Height 1, Midlevel 0, Scale 0.1, Object space), so its radius grows from
    0.5 to 0.6 against the control sphere beside it. Third: a dense sphere
    whose Height reads the red channel of a four-quadrant grid around a
    Midlevel of 0.5, so bright quadrants bulge out and dark ones sink in.
    Right: a 48 x 48 grid whose Height is sin(10 x + frame / 4), a ripple
    that travels along X while the scene plays; the driver exports as the
    time reader. The wave sits in Object space with Scale 0.08, so crests
    stand 8 cm proud of the plane.
    """
    fresh()

    def displace(tree, bsdf_material, *, height, midlevel, scale):
        bsdf_material.displacement_method = 'DISPLACEMENT'
        node = tree.nodes.new("ShaderNodeDisplacement")
        node.space = 'OBJECT'
        node.inputs["Midlevel"].default_value = midlevel
        node.inputs["Scale"].default_value = scale
        if hasattr(height, "is_output"):
            tree.links.new(height, node.inputs["Height"])
        else:
            node.inputs["Height"].default_value = height
        output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
        tree.links.new(node.outputs["Displacement"], output.inputs["Displacement"])
        return node

    inflated = sphere("Inflated", location=(-1.8, 0, 0.5))
    tree, bsdf, mat = material(inflated, "Inflated")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.45, 0.1, 1)
    displace(tree, mat, height=1.0, midlevel=0.0, scale=0.1)

    control = sphere("Control", location=(-0.6, 0, 0.5))
    _t, bsdf, _m = material(control, "Control")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.45, 0.1, 1)

    bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, radius=0.5, location=(0.6, 0, 0.5))
    bumped = bpy.context.active_object
    bumped.name = "Bumped"
    bumped.data.name = "BumpedMesh"
    bpy.ops.object.shade_smooth()
    tree, bsdf, mat = material(bumped, "Bumped")
    bsdf.inputs["Base Color"].default_value = (0.3, 0.6, 0.9, 1)
    height_image = grid_image("height_grid", 64)
    height_image.colorspace_settings.name = 'Non-Color'
    grid = texture_node(tree, height_image)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(grid.outputs["Color"], separate.inputs["Color"])
    displace(tree, mat, height=separate.outputs["Red"], midlevel=0.5, scale=0.15)

    bpy.ops.mesh.primitive_grid_add(x_subdivisions=48, y_subdivisions=48, size=1.2, location=(2.0, 0, 0.2))
    wave = bpy.context.active_object
    wave.name = "Wave"
    wave.data.name = "WaveMesh"
    bpy.ops.object.shade_smooth()
    tree, bsdf, mat = material(wave, "Wave")
    bsdf.inputs["Base Color"].default_value = (0.2, 0.8, 0.4, 1)
    bsdf.inputs["Roughness"].default_value = 0.4
    geometry = tree.nodes.new("ShaderNodeNewGeometry")
    separate_xyz = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(geometry.outputs["Position"], separate_xyz.inputs["Vector"])
    frequency = tree.nodes.new("ShaderNodeMath")
    frequency.operation = 'MULTIPLY'
    frequency.inputs[1].default_value = 10.0
    tree.links.new(separate_xyz.outputs["X"], frequency.inputs[0])
    phase = tree.nodes.new("ShaderNodeValue")
    phase.name = "Phase"
    phase.label = "Phase"
    fcurve = phase.outputs[0].driver_add("default_value")
    fcurve.driver.type = 'SCRIPTED'
    fcurve.driver.expression = "frame / 4"
    shifted = tree.nodes.new("ShaderNodeMath")
    shifted.operation = 'ADD'
    tree.links.new(frequency.outputs["Value"], shifted.inputs[0])
    tree.links.new(phase.outputs[0], shifted.inputs[1])
    ripple = tree.nodes.new("ShaderNodeMath")
    ripple.operation = 'SINE'
    tree.links.new(shifted.outputs["Value"], ripple.inputs[0])
    displace(tree, mat, height=ripple.outputs["Value"], midlevel=0.0, scale=0.08)

    sun(energy=3.0)
    save("t28_vertex_displacement")


def scene_shader_closures() -> None:
    """Mix Shader with a Transparent BSDF, and Add Shader with an Emission.

    Left sphere: a Principled BSDF mixed with a white Transparent BSDF at a
    constant factor of 0.6, so the surface keeps 40 % opacity and the cyan bar
    shows through it. Middle cube: the Transparent BSDF on the factor-0 side
    and the factor read from the red channel of a four-quadrant grid, whose red
    splits at U = 0.5, so every face is nearly opaque on one half and nearly
    clear on the other. Right sphere: a dark
    Principled BSDF plus an Emission shader through an Add Shader, glowing
    green at strength 2 like t07's emitter.
    """
    fresh()
    bar = cube("Backdrop", location=(0, 0.6, 0.5), size=1.0)
    bar.scale = (3.4, 0.05, 0.35)
    _t, bar_bsdf, _m = material(bar, "BackdropStripe")
    bar_bsdf.inputs["Base Color"].default_value = (0.05, 0.85, 0.9, 1)

    def output_of(tree):
        return next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')

    faded = sphere("FadedMix", location=(-1.4, 0, 0.5))
    tree, bsdf, mat = material(faded, "FadedMix")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.35, 0.1, 1)
    bsdf.inputs["Roughness"].default_value = 0.3
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mix = tree.nodes.new("ShaderNodeMixShader")
    mix.inputs["Fac"].default_value = 0.6
    tree.links.new(bsdf.outputs["BSDF"], mix.inputs[1])
    tree.links.new(transparent.outputs["BSDF"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output_of(tree).inputs["Surface"])
    mat.blend_method = 'BLEND'

    # A cube, not a sphere: the mask splits the red channel at U = 0.5, and a
    # UV sphere's visible face straddles that seam near its silhouette, so the
    # split reads as a uniform haze. cube_project gives every face a full 0-1
    # UV, which is the instrument t20 proved for a texture-driven mask.
    masked = cube("MaskedMix", location=(0.0, 0, 0.5), size=1.0)
    unwrap(masked)
    tree, bsdf, mat = material(masked, "MaskedMix")
    bsdf.inputs["Base Color"].default_value = (0.3, 0.6, 0.9, 1)
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    mask_image = grid_image("closure_mask", 64)
    mask_image.colorspace_settings.name = 'Non-Color'
    grid = texture_node(tree, mask_image)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    tree.links.new(grid.outputs["Color"], separate.inputs["Color"])
    mix = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(separate.outputs["Red"], mix.inputs["Fac"])
    tree.links.new(transparent.outputs["BSDF"], mix.inputs[1])
    tree.links.new(bsdf.outputs["BSDF"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output_of(tree).inputs["Surface"])
    mat.blend_method = 'BLEND'

    glow = sphere("AddedGlow", location=(1.4, 0, 0.5))
    tree, bsdf, _m = material(glow, "AddedGlow")
    bsdf.inputs["Base Color"].default_value = (0.05, 0.05, 0.05, 1)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = (0.1, 1.0, 0.35, 1)
    emission.inputs["Strength"].default_value = 2.0
    add = tree.nodes.new("ShaderNodeAddShader")
    tree.links.new(bsdf.outputs["BSDF"], add.inputs[0])
    tree.links.new(emission.outputs["Emission"], add.inputs[1])
    tree.links.new(add.outputs["Shader"], output_of(tree).inputs["Surface"])
    sun(energy=1.0)
    save("t29_shader_closures")


def scene_hair() -> None:
    """A Principled Hair BSDF surface, exported as RealityKit's hair surface.

    Four cylinders, each a stand-in for a hair strand, lit by one sun from the
    front-left and above. Two upright ones carry a Principled Hair BSDF with
    Direct coloring at the same warm brown, one smooth (Roughness 0.15) and one
    rough (Roughness 0.6); a third upright one carries a plain Principled BSDF
    at the same colour and the smooth roughness, as the control; the fourth
    carries the smooth hair material and is tilted 35 degrees, which is the
    probe for the space the strand direction is written in. A hair surface scatters
    around the strand rather than off it, so its highlight should read
    differently from the control's, and the rough strand's should be the
    broader of the two. What the platform uses for the strand direction is not
    measured: the surface honours whatever its ``tangent`` input carries and
    never substitutes the geometry's own, so the exporter authors the mesh's UV
    tangent and every strand's UVs here run along its axis.
    """
    fresh()

    def strand(name, x):
        bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.12, depth=1.0, location=(x, 0, 0.5))
        obj = bpy.context.active_object
        obj.name = name
        obj.data.name = f"{name}Mesh"
        bpy.ops.object.shade_smooth()
        # The hair surface takes its strand direction from the mesh's UV
        # tangent, and Blender's cylinder runs U around the circumference,
        # which points the highlight along the strand instead of across it.
        # Rewrite the UVs so U runs up the axis - the +U convention the
        # exporter documents - and give the caps their own flat projection so
        # their tangent is not degenerate.
        mesh = obj.data
        uv = mesh.uv_layers.active.data
        for poly in mesh.polygons:
            heights = [mesh.vertices[mesh.loops[i].vertex_index].co.z for i in poly.loop_indices]
            is_cap = max(heights) - min(heights) < 1e-6
            for index in poly.loop_indices:
                co = mesh.vertices[mesh.loops[index].vertex_index].co
                if is_cap:
                    uv[index].uv = (co.x + 0.5, co.y + 0.5)
                else:
                    uv[index].uv = (
                        co.z + 0.5,
                        math.atan2(co.y, co.x) / (2.0 * math.pi) + 0.5,
                    )
        return obj

    def hair_material(obj, name, *, roughness):
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        obj.data.materials.append(mat)
        tree = mat.node_tree
        tree.nodes.remove(tree.nodes["Principled BSDF"])
        hair = tree.nodes.new("ShaderNodeBsdfHairPrincipled")
        hair.parametrization = 'COLOR'
        hair.inputs["Color"].default_value = (0.35, 0.12, 0.03, 1)
        hair.inputs["Roughness"].default_value = roughness
        output = next(n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL')
        tree.links.new(hair.outputs[0], output.inputs["Surface"])

    hair_material(strand("HairSmooth", -0.45), "HairSmooth", roughness=0.15)
    hair_material(strand("HairRough", 0.0), "HairRough", roughness=0.6)

    # The same smooth material on a strand that is not upright. The strand
    # direction is written in object space, so this is what says whether the
    # platform transforms it: its highlight must run across its own tilted
    # length, not across the world's vertical.
    tilted = strand("HairTilted", 0.9)
    tilted.rotation_euler = (math.radians(35.0), 0.0, 0.0)
    tilted.location = (0.9, 0.0, 0.6)
    hair_material(tilted, "HairTilted", roughness=0.15)

    control = strand("PlainStrand", 0.45)
    _t, bsdf, _m = material(control, "PlainStrand")
    bsdf.inputs["Base Color"].default_value = (0.35, 0.12, 0.03, 1)
    bsdf.inputs["Roughness"].default_value = 0.15

    sun(energy=4.0)
    save("t30_hair")


def scene_vector_rotate() -> None:
    """Vector Rotate, exported as Rodrigues' rotation in MaterialX nodes.

    Every cube shows its UV coordinate, turned by Vector Rotate, as a colour,
    and each rotation is chosen to equal a plain arrangement of U and V that a
    control cube builds with Separate XYZ, Math and Combine Color (the t26
    nodes). So each rotated cube must look exactly like its control.

    Left three: the control (1 - V, U, 0), then Z Axis by 90 degrees about
    (0.5, 0.5, 0), then Axis Angle about the non-unit axis (0, 0, 2) by
    -90 degrees with Invert on, its angle computed by a Math node so the
    sine and cosine run on the platform rather than folding. Right two: the
    control (0, U, V), then Euler XYZ of (90, 0, 90) degrees about
    (0.5, 0.5, 0.5).

    A wrong angle unit leaves a cube showing plain (U, V); a wrong direction
    or a dropped Invert swaps which edges are bright; a dropped Center turns
    it mostly black; an Euler order applied backwards turns it bright green.
    """
    fresh()

    def uv_cube(name, x):
        obj = cube(name, location=(x, 0, 0.5), size=1.0)
        unwrap(obj)
        tree, bsdf, _m = material(obj, name)
        bsdf.inputs["Roughness"].default_value = 0.9
        coords = tree.nodes.new("ShaderNodeTexCoord")
        return tree, bsdf, coords.outputs["UV"]

    def show(tree, bsdf, vector_output, red=None, green=None, blue=None):
        # Channels default to the vector's own X, Y and Z.
        separate = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(vector_output, separate.inputs["Vector"])
        combine = tree.nodes.new("ShaderNodeCombineColor")
        for channel, override, own in (("Red", red, "X"), ("Green", green, "Y"), ("Blue", blue, "Z")):
            source = override(tree, separate) if override else separate.outputs[own]
            if isinstance(source, float):
                combine.inputs[channel].default_value = source
            else:
                tree.links.new(source, combine.inputs[channel])
        tree.links.new(combine.outputs["Color"], bsdf.inputs["Base Color"])

    def one_minus_v(tree, separate):
        subtract = tree.nodes.new("ShaderNodeMath")
        subtract.operation = 'SUBTRACT'
        subtract.inputs[0].default_value = 1.0
        tree.links.new(separate.outputs["Y"], subtract.inputs[1])
        return subtract.outputs["Value"]

    def rotated(tree, uv, configure):
        rotate = tree.nodes.new("ShaderNodeVectorRotate")
        tree.links.new(uv, rotate.inputs["Vector"])
        configure(tree, rotate)
        return rotate.outputs["Vector"]

    tree, bsdf, uv = uv_cube("TurnControl", -2.8)
    show(tree, bsdf, uv, red=one_minus_v, green=lambda _t, sep: sep.outputs["X"], blue=lambda *_: 0.0)

    def z_axis(_tree, rotate):
        rotate.rotation_type = 'Z_AXIS'
        rotate.inputs["Center"].default_value = (0.5, 0.5, 0.0)
        rotate.inputs["Angle"].default_value = math.radians(90.0)

    tree, bsdf, uv = uv_cube("RotateZAxis", -1.4)
    show(tree, bsdf, rotated(tree, uv, z_axis))

    def inverted_axis_angle(tree, rotate):
        rotate.rotation_type = 'AXIS_ANGLE'
        rotate.invert = True
        rotate.inputs["Center"].default_value = (0.5, 0.5, 0.0)
        rotate.inputs["Axis"].default_value = (0.0, 0.0, 2.0)
        angle = tree.nodes.new("ShaderNodeMath")
        angle.operation = 'MULTIPLY'
        angle.inputs[0].default_value = math.radians(-45.0)
        angle.inputs[1].default_value = 2.0
        tree.links.new(angle.outputs["Value"], rotate.inputs["Angle"])

    tree, bsdf, uv = uv_cube("RotateAxisAngleInverted", 0.0)
    show(tree, bsdf, rotated(tree, uv, inverted_axis_angle))

    tree, bsdf, uv = uv_cube("EulerControl", 1.4)
    show(tree, bsdf, uv, red=lambda *_: 0.0, green=lambda _t, sep: sep.outputs["X"], blue=lambda _t, sep: sep.outputs["Y"])

    def euler(_tree, rotate):
        rotate.rotation_type = 'EULER_XYZ'
        rotate.inputs["Center"].default_value = (0.5, 0.5, 0.5)
        rotate.inputs["Rotation"].default_value = (math.radians(90.0), 0.0, math.radians(90.0))

    tree, bsdf, uv = uv_cube("RotateEuler", 2.8)
    show(tree, bsdf, rotated(tree, uv, euler))
    sun()
    save("t31_vector_rotate")


def _emissive(tree, bsdf, colour_output):
    """Show a value as light: black base, the value as emission at strength 1."""
    bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["Specular IOR Level"].default_value = 0.0
    tree.links.new(colour_output, bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = 1.0


def _math(tree, operation, a, b=None):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    for index, value in ((0, a), (1, b)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = value
        else:
            tree.links.new(value, node.inputs[index])
    return node.outputs["Value"]


def scene_gamma_checker() -> None:
    """Gamma and Checker Texture, transcribed from Cycles.

    Left pair: a UV gradient shown as colour (red along U, green along V),
    then the same gradient through Gamma 2.2, whose midtones must be darker
    while the black corner and the fully red and green edges stay put; the
    third cube raises each channel to 2.2 with Math Power nodes, the control
    the Gamma cube must match. Right: a Checker Texture on UVs at Scale 4,
    orange and navy, exactly four by four checks per face with a navy check in
    the corner where U and V are both 0.
    """
    fresh()

    def uv_colour(tree):
        coords = tree.nodes.new("ShaderNodeTexCoord")
        separate = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(coords.outputs["UV"], separate.inputs["Vector"])
        return coords, separate

    plain = cube("GradientPlain", location=(-2.1, 0, 0.5), size=1.0)
    unwrap(plain)
    tree, bsdf, _m = material(plain, "GradientPlain")
    coords, _separate = uv_colour(tree)
    _emissive(tree, bsdf, coords.outputs["UV"])

    gamma_cube = cube("GradientGamma", location=(-0.7, 0, 0.5), size=1.0)
    unwrap(gamma_cube)
    tree, bsdf, _m = material(gamma_cube, "GradientGamma")
    coords, _separate = uv_colour(tree)
    gamma = tree.nodes.new("ShaderNodeGamma")
    gamma.inputs["Gamma"].default_value = 2.2
    tree.links.new(coords.outputs["UV"], gamma.inputs["Color"])
    _emissive(tree, bsdf, gamma.outputs["Color"])

    power_cube = cube("GradientPower", location=(0.7, 0, 0.5), size=1.0)
    unwrap(power_cube)
    tree, bsdf, _m = material(power_cube, "GradientPower")
    _coords, separate = uv_colour(tree)
    combine = tree.nodes.new("ShaderNodeCombineColor")
    tree.links.new(_math(tree, 'POWER', separate.outputs["X"], 2.2), combine.inputs["Red"])
    tree.links.new(_math(tree, 'POWER', separate.outputs["Y"], 2.2), combine.inputs["Green"])
    _emissive(tree, bsdf, combine.outputs["Color"])

    checker_cube = cube("Checks", location=(2.1, 0, 0.5), size=1.0)
    unwrap(checker_cube)
    tree, bsdf, _m = material(checker_cube, "Checks")
    coords = tree.nodes.new("ShaderNodeTexCoord")
    checker = tree.nodes.new("ShaderNodeTexChecker")
    checker.inputs["Scale"].default_value = 4.0
    checker.inputs["Color1"].default_value = (0.95, 0.45, 0.05, 1.0)
    checker.inputs["Color2"].default_value = (0.02, 0.05, 0.3, 1.0)
    tree.links.new(coords.outputs["UV"], checker.inputs["Vector"])
    _emissive(tree, bsdf, checker.outputs["Color"])
    save("t32_gamma_checker")


def scene_computed_image_coordinates() -> None:
    """Image coordinates computed in the graph, and Box projection.

    Left: the control, a grid through a constant Mapping (scale 2, shift 0.25
    along U), the UV transform t10 verifies. Second: the same grid with its
    coordinates built by Vector Math, UV times 2 plus (0.25, 0, 0). Third: the
    Mapping again, its Location fed by a Combine XYZ, which moves it off the
    UV transform onto the computed path. All three must show the same grid.
    Right: a sphere and a cube sampling the grid by Box projection from object
    coordinates at Projection Blend 0.3.
    """
    fresh()
    grid = grid_image("coordinate_grid", 128)

    def panel(name, x):
        obj = cube(name, location=(x, 0, 0.5), size=1.0)
        unwrap(obj)
        tree, bsdf, _m = material(obj, name)
        node = texture_node(tree, grid)
        _emissive(tree, bsdf, node.outputs["Color"])
        return tree, node, tree.nodes.new("ShaderNodeTexCoord")

    tree, node, coords = panel("MappedControl", -2.8)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (2.0, 2.0, 1.0)
    mapping.inputs["Location"].default_value = (0.25, 0.0, 0.0)
    tree.links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])

    tree, node, coords = panel("VectorMathCoords", -1.4)
    scaled = tree.nodes.new("ShaderNodeVectorMath")
    scaled.operation = 'SCALE'
    scaled.inputs["Scale"].default_value = 2.0
    shifted = tree.nodes.new("ShaderNodeVectorMath")
    shifted.operation = 'ADD'
    shifted.inputs[1].default_value = (0.25, 0.0, 0.0)
    tree.links.new(coords.outputs["UV"], scaled.inputs[0])
    tree.links.new(scaled.outputs["Vector"], shifted.inputs[0])
    tree.links.new(shifted.outputs["Vector"], node.inputs["Vector"])

    tree, node, coords = panel("LinkedMapping", 0.0)
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (2.0, 2.0, 1.0)
    location = tree.nodes.new("ShaderNodeCombineXYZ")
    location.inputs["X"].default_value = 0.25
    tree.links.new(location.outputs["Vector"], mapping.inputs["Location"])
    tree.links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], node.inputs["Vector"])

    for name, maker, x in (("BoxSphere", sphere, 1.4), ("BoxCube", cube, 2.8)):
        obj = maker(name, location=(x, 0, 0.5))
        tree, bsdf, _m = material(obj, name)
        node = texture_node(tree, grid)
        node.projection = 'BOX'
        node.projection_blend = 0.3
        coords = tree.nodes.new("ShaderNodeTexCoord")
        tree.links.new(coords.outputs["Object"], node.inputs["Vector"])
        _emissive(tree, bsdf, node.outputs["Color"])
    save("t33_computed_image_coordinates")


def scene_camera_attribute_random() -> None:
    """Camera Data, the Attribute node and Object Info > Random.

    Back row, two 3 m floors seen from the front: the left shows View Distance
    as repeating bright-to-dark rings every half metre, centred below the
    camera, so they stay circles as the camera moves; the right shows View Z
    Depth the same way, as straight bands parallel to the screen that turn
    with the camera. Front: a cube showing a float point attribute running 0
    to 1 from its -X face to its +X face, beside a control showing object X
    plus 0.5, which must match; then four cubes sharing one material that
    shows Object Info Random as grey.
    """
    fresh()
    for name, output, x in (("DistanceRings", "View Distance", -1.6), ("DepthBands", "View Z Depth", 1.6)):
        bpy.ops.mesh.primitive_plane_add(size=3.0, location=(x, 3.0, 0.0))
        floor = bpy.context.active_object
        floor.name = name
        tree, bsdf, _m = material(floor, name)
        camera = tree.nodes.new("ShaderNodeCameraData")
        fraction = _math(tree, 'FRACT', _math(tree, 'MULTIPLY', camera.outputs[output], 2.0))
        _emissive(tree, bsdf, fraction)

    attributed = cube("WearAttribute", location=(-2.1, 0, 0.5), size=1.0)
    mesh = attributed.data
    wear = mesh.attributes.new("wear", 'FLOAT', 'POINT')
    for index, vertex in enumerate(mesh.vertices):
        wear.data[index].value = vertex.co.x + 0.5
    tree, bsdf, _m = material(attributed, "WearAttribute")
    attribute = tree.nodes.new("ShaderNodeAttribute")
    attribute.attribute_type = 'GEOMETRY'
    attribute.attribute_name = "wear"
    _emissive(tree, bsdf, attribute.outputs["Fac"])

    control = cube("WearControl", location=(-0.7, 0, 0.5), size=1.0)
    tree, bsdf, _m = material(control, "WearControl")
    coords = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(coords.outputs["Object"], separate.inputs["Vector"])
    _emissive(tree, bsdf, _math(tree, 'ADD', separate.outputs["X"], 0.5))

    shared = bpy.data.materials.new("ObjectRandom")
    shared.use_nodes = True
    tree = shared.node_tree
    bsdf = tree.nodes["Principled BSDF"]
    info = tree.nodes.new("ShaderNodeObjectInfo")
    _emissive(tree, bsdf, info.outputs["Random"])
    for index, name in enumerate(("RandomAlpha", "RandomBravo", "RandomCharlie", "RandomDelta")):
        obj = cube(name, location=(0.7 + index * 0.8, 0, 0.3), size=0.6)
        obj.data.materials.append(shared)
    save("t34_camera_attribute_random")


def scene_bsdf_presets() -> None:
    """Diffuse, Metallic, Glossy, Sheen and Subsurface Scattering, and a Mix
    Shader of two Principled BSDFs, each above the Principled BSDF it exports
    as.

    Front row, left to right: Diffuse (green, roughness 0.6), Metallic (copper,
    white Edge Tint), Glossy (gold), Sheen (violet), Subsurface Scattering
    (skin), and a Mix Shader of a red dielectric and a blue metal by U. Back
    row: the controls. Each front sphere must look like the sphere behind it;
    Glossy is the one allowed difference, a brighter rim.
    """
    fresh()

    def pair(name, x, build, control):
        front = sphere(name, location=(x, 0, 0.5))
        unwrap(front)
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        front.data.materials.append(mat)
        tree = mat.node_tree
        tree.nodes.remove(tree.nodes["Principled BSDF"])
        shader = build(tree)
        tree.links.new(shader, tree.nodes["Material Output"].inputs["Surface"])
        back = sphere(f"{name}Control", location=(x, 1.3, 0.5))
        unwrap(back)
        tree, bsdf, _m = material(back, f"{name}Control")
        control(tree, bsdf)

    def diffuse(tree):
        node = tree.nodes.new("ShaderNodeBsdfDiffuse")
        node.inputs["Color"].default_value = (0.1, 0.6, 0.2, 1.0)
        node.inputs["Roughness"].default_value = 0.6
        return node.outputs["BSDF"]

    def diffuse_control(tree, bsdf):
        bsdf.inputs["Base Color"].default_value = (0.1, 0.6, 0.2, 1.0)
        bsdf.inputs["Diffuse Roughness"].default_value = 0.6
        bsdf.inputs["Specular IOR Level"].default_value = 0.0

    def metallic(tree):
        node = tree.nodes.new("ShaderNodeBsdfMetallic")
        node.inputs["Base Color"].default_value = (0.95, 0.64, 0.54, 1.0)
        node.inputs["Edge Tint"].default_value = (1.0, 1.0, 1.0, 1.0)
        node.inputs["Roughness"].default_value = 0.25
        return node.outputs["BSDF"]

    def metallic_control(tree, bsdf):
        bsdf.inputs["Base Color"].default_value = (0.95, 0.64, 0.54, 1.0)
        bsdf.inputs["Metallic"].default_value = 1.0
        bsdf.inputs["Roughness"].default_value = 0.25

    def glossy(tree):
        node = tree.nodes.new("ShaderNodeBsdfAnisotropic")
        node.inputs["Color"].default_value = (0.9, 0.7, 0.2, 1.0)
        node.inputs["Roughness"].default_value = 0.3
        return node.outputs["BSDF"]

    def glossy_control(tree, bsdf):
        bsdf.inputs["Base Color"].default_value = (0.9, 0.7, 0.2, 1.0)
        bsdf.inputs["Metallic"].default_value = 1.0
        bsdf.inputs["Roughness"].default_value = 0.3

    def sheen(tree):
        node = tree.nodes.new("ShaderNodeBsdfSheen")
        node.inputs["Color"].default_value = (0.5, 0.2, 0.9, 1.0)
        return node.outputs["BSDF"]

    def sheen_control(tree, bsdf):
        bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
        bsdf.inputs["Sheen Weight"].default_value = 1.0
        bsdf.inputs["Sheen Tint"].default_value = (0.5, 0.2, 0.9, 1.0)

    def skin(tree):
        node = tree.nodes.new("ShaderNodeSubsurfaceScattering")
        node.inputs["Color"].default_value = (0.85, 0.55, 0.45, 1.0)
        node.inputs["Scale"].default_value = 0.05
        node.inputs["Roughness"].default_value = 0.49
        return node.outputs["BSSRDF"]

    def skin_control(tree, bsdf):
        bsdf.inputs["Base Color"].default_value = (0.85, 0.55, 0.45, 1.0)
        bsdf.inputs["Subsurface Weight"].default_value = 1.0
        bsdf.inputs["Subsurface Scale"].default_value = 0.05
        bsdf.inputs["Roughness"].default_value = 0.7
        bsdf.inputs["Specular IOR Level"].default_value = 0.0

    def mix(tree):
        red = tree.nodes.new("ShaderNodeBsdfPrincipled")
        red.inputs["Base Color"].default_value = (0.8, 0.05, 0.05, 1.0)
        blue = tree.nodes.new("ShaderNodeBsdfPrincipled")
        blue.inputs["Base Color"].default_value = (0.1, 0.3, 0.9, 1.0)
        blue.inputs["Metallic"].default_value = 1.0
        coords = tree.nodes.new("ShaderNodeTexCoord")
        separate = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(coords.outputs["UV"], separate.inputs["Vector"])
        node = tree.nodes.new("ShaderNodeMixShader")
        tree.links.new(separate.outputs["X"], node.inputs["Fac"])
        tree.links.new(red.outputs["BSDF"], node.inputs[1])
        tree.links.new(blue.outputs["BSDF"], node.inputs[2])
        return node.outputs["Shader"]

    def mix_control(tree, bsdf):
        coords = tree.nodes.new("ShaderNodeTexCoord")
        separate = tree.nodes.new("ShaderNodeSeparateXYZ")
        tree.links.new(coords.outputs["UV"], separate.inputs["Vector"])
        colour = tree.nodes.new("ShaderNodeMix")
        colour.data_type = 'RGBA'
        a = next(s for s in colour.inputs if s.name == "A" and s.type == 'RGBA')
        b = next(s for s in colour.inputs if s.name == "B" and s.type == 'RGBA')
        a.default_value = (0.8, 0.05, 0.05, 1.0)
        b.default_value = (0.1, 0.3, 0.9, 1.0)
        tree.links.new(separate.outputs["X"], colour.inputs[0])
        result = next(s for s in colour.outputs if s.name == "Result" and s.type == 'RGBA')
        tree.links.new(result, bsdf.inputs["Base Color"])
        tree.links.new(separate.outputs["X"], bsdf.inputs["Metallic"])

    for index, (name, build, control) in enumerate((
        ("PresetDiffuse", diffuse, diffuse_control), ("PresetMetallic", metallic, metallic_control),
        ("PresetGlossy", glossy, glossy_control), ("PresetSheen", sheen, sheen_control),
        ("PresetSkin", skin, skin_control), ("MixedPrincipled", mix, mix_control),
    )):
        pair(name, -3.0 + index * 1.2, build, control)
    sun()
    save("t35_bsdf_presets")


def _bump_height(tree, amplitude=0.5, frequency=6.0 * math.pi):
    """h = amplitude (sin(f u) + sin(f v)) of the UVs, as Math nodes."""
    coords = tree.nodes.new("ShaderNodeTexCoord")
    separate = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(coords.outputs["UV"], separate.inputs["Vector"])
    u = _math(tree, 'SINE', _math(tree, 'MULTIPLY', separate.outputs["X"], frequency))
    v = _math(tree, 'SINE', _math(tree, 'MULTIPLY', separate.outputs["Y"], frequency))
    return _math(tree, 'MULTIPLY', _math(tree, 'ADD', u, v), amplitude)


def scene_bump() -> None:
    """Bump, against a normal map of the same relief.

    Upright 1 m panels facing the front, each UV-mapped 0 to 1. The height is
    ``0.5 (sin(6 pi u) + sin(6 pi v))``, three bumps each way, with Distance
    0.02. Left: the control, a tangent-space normal map computed from that
    height's exact derivative, through the Normal Map path t06 verifies.
    Middle: the Bump node on the same height into the Principled Normal. The
    two panels must shade the same, bumps where bumps are and hollows where
    hollows are. Right: the Bump's world normal shown as colour, (n + 1) / 2:
    purple where flat, with the red channel falling on the side of each bump
    that faces +X and the blue channel falling on the side that faces up.
    """
    fresh()
    distance, frequency, amplitude, size = 0.02, 6.0 * math.pi, 0.5, 128

    image = bpy.data.images.new("bump_reference_normals", size, size, is_data=True)
    pixels = []
    for y in range(size):
        for x in range(size):
            u, v = (x + 0.5) / size, (y + 0.5) / size
            nx = -distance * amplitude * frequency * math.cos(frequency * u)
            ny = -distance * amplitude * frequency * math.cos(frequency * v)
            length = math.sqrt(nx * nx + ny * ny + 1.0)
            pixels += [nx / length * 0.5 + 0.5, ny / length * 0.5 + 0.5, 1.0 / length * 0.5 + 0.5, 1.0]
    image.pixels = pixels
    image.colorspace_settings.name = 'Non-Color'
    _register(image)

    def panel(name, x):
        bpy.ops.mesh.primitive_plane_add(size=1.0, location=(x, 0.0, 0.6), rotation=(math.pi / 2, 0.0, 0.0))
        obj = bpy.context.active_object
        obj.name = name
        tree, bsdf, _m = material(obj, name)
        bsdf.inputs["Base Color"].default_value = (0.6, 0.6, 0.6, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.3
        return tree, bsdf

    tree, bsdf = panel("NormalMapControl", -1.3)
    node = texture_node(tree, image)
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tree.links.new(node.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])

    tree, bsdf = panel("BumpPanel", 0.0)
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Distance"].default_value = distance
    tree.links.new(_bump_height(tree, amplitude, frequency), bump.inputs["Height"])
    tree.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])

    tree, bsdf = panel("BumpNormalColour", 1.3)
    bump = tree.nodes.new("ShaderNodeBump")
    bump.inputs["Distance"].default_value = distance
    tree.links.new(_bump_height(tree, amplitude, frequency), bump.inputs["Height"])
    half = tree.nodes.new("ShaderNodeVectorMath")
    half.operation = 'MULTIPLY_ADD'
    half.inputs[1].default_value = (0.5, 0.5, 0.5)
    half.inputs[2].default_value = (0.5, 0.5, 0.5)
    tree.links.new(bump.outputs["Normal"], half.inputs[0])
    _emissive(tree, bsdf, half.outputs["Vector"])
    sun()
    save("t36_bump")


def scene_collection_instances() -> None:
    """Collection instances, written as USD instances by default.

    One pawn, a cone body with its head pushed toward +X so a turn shows, with
    two materials. It lives in a collection kept out of the scene and is placed
    four times by collection-instance empties along X: as built, turned 90
    degrees, turned 180 degrees, and at 0.6 scale. Behind the first, at +Y, an
    ordinary mesh object with its own copy of the geometry is the control.
    """
    fresh()
    source = bpy.data.collections.new("Pawn")
    bpy.ops.mesh.primitive_cone_add(vertices=24, radius1=0.3, depth=0.8, location=(0.0, 0.0, 0.4))
    body = bpy.context.active_object
    body.name = "PawnBody"
    body.data.name = "PawnBodyMesh"
    _t, bsdf, _m = material(body, "PawnOrange")
    bsdf.inputs["Base Color"].default_value = (0.9, 0.35, 0.05, 1.0)
    head = sphere("PawnHead", location=(0.18, 0.0, 0.9), radius=0.16)
    _t, bsdf, _m = material(head, "PawnBlue")
    bsdf.inputs["Base Color"].default_value = (0.05, 0.25, 0.9, 1.0)
    for obj in (body, head):
        for owner in list(obj.users_collection):
            owner.objects.unlink(obj)
        source.objects.link(obj)

    placements = (
        ("PawnAsBuilt", -1.5, 0.0, 1.0),
        ("PawnTurned90", -0.5, 90.0, 1.0),
        ("PawnTurned180", 0.5, 180.0, 1.0),
        ("PawnSmall", 1.5, 0.0, 0.6),
    )
    for name, x, turn, scale in placements:
        empty = bpy.data.objects.new(name, None)
        empty.instance_type = 'COLLECTION'
        empty.instance_collection = source
        empty.location = (x, 0.0, 0.0)
        empty.rotation_euler = (0.0, 0.0, math.radians(turn))
        empty.scale = (scale, scale, scale)
        bpy.context.scene.collection.objects.link(empty)

    for original, name in ((body, "ControlBody"), (head, "ControlHead")):
        copy = original.copy()
        copy.data = original.data.copy()
        copy.name = name
        copy.location = (original.location.x - 1.5, 1.2, original.location.z)
        bpy.context.scene.collection.objects.link(copy)
    sun()
    save("t37_collection_instances")


def scene_unicode_names() -> None:
    """Object and material names outside ASCII, kept by Allow Unicode.

    Four cubes in a row, each named in a different script with a material
    named the same way: accented Latin (red), Cyrillic (blue), Chinese
    (green), and a plain ASCII control (grey) on the right.
    """
    fresh()
    names = (
        ("Chaise_été", "Rouge_été", (0.8, 0.05, 0.05, 1.0)),
        ("Кресло", "Синий", (0.05, 0.1, 0.8, 1.0)),
        ("椅子", "緑色", (0.05, 0.6, 0.1, 1.0)),
        ("PlainChair", "PlainGrey", (0.5, 0.5, 0.5, 1.0)),
    )
    for index, (object_name, material_name, colour) in enumerate(names):
        obj = cube(object_name, location=(index * 1.2 - 1.8, 0.0, 0.4), size=0.8)
        _t, bsdf, _m = material(obj, material_name)
        bsdf.inputs["Base Color"].default_value = colour
    sun()
    save("t38_unicode_names")


SCENES = (
    scene_orientation,
    scene_uv_layout,
    scene_vertex_color,
    scene_multi_material,
    scene_metal_roughness,
    scene_normal_map,
    scene_emission,
    scene_opacity,
    scene_wrap_filter,
    scene_texture_transform,
    scene_transform_animation,
    scene_skinned,
    scene_shape_keys,
    scene_dropped_lights_cameras,
    scene_dropped_curves,
    scene_dropped_world,
    scene_procedural_noise,
    scene_refused_mix_shader,
    scene_cm_scale_refusal,
    scene_bake_mask_mix,
    scene_specular_tint_refusal,
    scene_surface_readers,
    scene_vector_math,
    scene_frame_drivers,
    scene_vertex_displacement,
    scene_shader_closures,
    scene_hair,
    scene_vector_rotate,
    scene_gamma_checker,
    scene_computed_image_coordinates,
    scene_camera_attribute_random,
    scene_bsdf_presets,
    scene_bump,
    scene_collection_instances,
    scene_unicode_names,
)


def _verify_written_scenes() -> None:
    """Reopen every scene and check its textures survived the save.

    The digest guard above runs on in-memory pixels, so it cannot see a texture
    that fails to serialize. This measures what actually shipped: a probe image
    with one distinct texel is a flat fill, which is the exact appearance its
    own expectation defines as failure.
    """

    failures = []
    for path in sorted(OUT.glob("t*.blend")):
        bpy.ops.wm.open_mainfile(filepath=str(path))
        for image in bpy.data.images:
            if image.name == "Render Result" or not image.has_data:
                continue
            pixels = list(image.pixels)
            texels = {
                tuple(pixels[i:i + 4])
                for i in range(0, min(len(pixels), 40000), 4)
            }
            if len(texels) < 2:
                failures.append(f"{path.name}: {image.name} is a flat fill")
    if failures:
        raise SystemExit(
            "textures did not survive the save:\n  " + "\n  ".join(failures)
        )
    print("VERIFIED every written scene keeps its textures")


def main() -> None:
    for builder in SCENES:
        builder()
    _verify_written_scenes()
    print(f"DONE {len(SCENES)} scenes -> {OUT}")


if __name__ == "__main__":
    main()
