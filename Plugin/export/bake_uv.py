"""The UV map a bake writes into.

A bake renders each face into the texels its UVs cover. Two faces that cover
the same texels, or a face whose UVs leave the 0-1 square and wrap onto texels
another face covers, get one shared texel. Whichever face bakes last wins, and
every other face then samples a colour that belongs somewhere else.

That is harmless only when the material looks the same on both faces: its
baked channels read nothing but image textures that repeat across the one UV
map the bake writes into. Lighting and shadows never do, and neither does a
procedural texture, a vertex colour or an object-space coordinate. When a
material needs its own texels, and the object's UV map does not give them, the
bake writes into a UV map generated with Smart UV Project instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Sequence

BAKE_UV_LAYER_NAME = "usdstage_bake"

# Blender meshes hold at most eight UV maps.
_MAX_UV_LAYERS = 8
# The raster the overlap test draws faces into. A shared region smaller than
# one texel of it is below what a bake of this size can show.
_OVERLAP_RASTER_SIZE = 1024
# Texel candidates tested per batch, to bound memory on dense meshes.
_RASTER_BATCH = 4_000_000
_RANGE_TOLERANCE = 1e-4
# Texels covered by more than one face, as a share of every covered texel, at
# which the layout counts as overlapping. Rounding along shared edges stays
# well below it.
_OVERLAP_SHARE = 0.001
_OVERLAP_MIN_TEXELS = 8
# Smart UV Project's default island angle limit, 66 degrees.
_SMART_PROJECT_ANGLE_LIMIT = 1.15192

LEAVES_UNIT_SQUARE = "leaves the 0-1 UV square"
OVERLAPS = "has faces that share UV space"

# Nodes whose outputs depend only on their inputs, so they keep a UV-periodic
# material periodic.
_PURE_NODE_TYPES = frozenset(
    {
        "REROUTE",
        "FRAME",
        "GROUP_INPUT",
        "GROUP_OUTPUT",
        "RGB",
        "VALUE",
        "MIX",
        "MIX_RGB",
        "MATH",
        "VECT_MATH",
        "MAP_RANGE",
        "CLAMP",
        "INVERT",
        "HUE_SAT",
        "BRIGHTCONTRAST",
        "GAMMA",
        "SEPARATE_COLOR",
        "COMBINE_COLOR",
        "SEPRGB",
        "COMBRGB",
        "SEPXYZ",
        "COMBXYZ",
        "VALTORGB",
        "RGBTOBW",
        "CURVE_RGB",
        "CURVE_FLOAT",
        "CURVE_VEC",
        "BLACKBODY",
        "WAVELENGTH",
    }
)
# Shading inputs that change how light reflects but not the colour a
# material-colour bake records.
_SHADING_ONLY_INPUTS = frozenset({"Normal", "Coat Normal", "Tangent", "Displacement"})


def uv_layout_problem(triangles) -> Optional[str]:
    """Why a set of UV triangles cannot hold a bake of their own, or None.

    ``triangles`` is an ``(n, 3, 2)`` array of UV coordinates.
    """
    import numpy as np

    uv = np.asarray(triangles, dtype=np.float64).reshape(-1, 3, 2)
    if not len(uv):
        return None
    if uv.min() < -_RANGE_TOLERANCE or uv.max() > 1.0 + _RANGE_TOLERANCE:
        return LEAVES_UNIT_SQUARE
    covered, shared = _raster_coverage(uv, _OVERLAP_RASTER_SIZE)
    if shared > max(_OVERLAP_MIN_TEXELS, _OVERLAP_SHARE * covered):
        return OVERLAPS
    return None


def _raster_coverage(uv, size: int) -> tuple:
    """(texels covered, texels covered more than once) for UV triangles.

    A texel belongs to a triangle when its centre lies strictly inside it, so a
    centre on an edge two triangles share belongs to neither.
    """
    import numpy as np

    points = uv * size
    a, b, c = points[:, 0], points[:, 1], points[:, 2]
    twice_area = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    keep = np.abs(twice_area) > 1e-12
    a, b, c, orientation = a[keep], b[keep], c[keep], np.sign(twice_area[keep])

    lo = np.minimum(np.minimum(a, b), c)
    hi = np.maximum(np.maximum(a, b), c)
    x0 = np.clip(np.ceil(lo[:, 0] - 0.5), 0, size - 1).astype(np.int64)
    x1 = np.clip(np.floor(hi[:, 0] - 0.5), 0, size - 1).astype(np.int64)
    y0 = np.clip(np.ceil(lo[:, 1] - 0.5), 0, size - 1).astype(np.int64)
    y1 = np.clip(np.floor(hi[:, 1] - 0.5), 0, size - 1).astype(np.int64)
    widths = np.maximum(x1 - x0 + 1, 0)
    heights = np.maximum(y1 - y0 + 1, 0)
    candidates = widths * heights

    counts = np.zeros(size * size, dtype=np.int32)
    ends = np.cumsum(candidates)
    start = 0
    while start < len(candidates):
        offset = ends[start - 1] if start else 0
        stop = int(np.searchsorted(ends, offset + _RASTER_BATCH, side="right"))
        stop = max(stop, start + 1)
        batch = slice(start, stop)
        per = candidates[batch]
        total = int(per.sum())
        if total:
            owner = np.repeat(np.arange(start, stop), per)
            first = np.repeat(np.cumsum(per) - per, per)
            local = np.arange(total) - first
            px = x0[owner] + local % widths[owner]
            py = y0[owner] + local // widths[owner]
            cx = px + 0.5
            cy = py + 0.5
            inside = np.ones(total, dtype=bool)
            for p, q in ((a, b), (b, c), (c, a)):
                edge = (q[owner, 0] - p[owner, 0]) * (cy - p[owner, 1]) - (
                    q[owner, 1] - p[owner, 1]
                ) * (cx - p[owner, 0])
                inside &= edge * orientation[owner] > 0.0
            texels = py[inside] * size + px[inside]
            counts += np.bincount(texels, minlength=size * size).astype(np.int32)
        start = stop
    return int(np.count_nonzero(counts)), int(np.count_nonzero(counts > 1))


def material_bake_is_uv_periodic(material, *, bake_uv: str, render_uv: Optional[str]) -> bool:
    """Whether a material-colour bake of ``material`` may share texels.

    True when everything that reaches the surface reads only image textures
    that repeat across ``bake_uv``, through nodes that are pure functions of
    their inputs. Faces then sample the same colour at the same UV, wherever
    they are. Anything else, including a node this does not recognise, is
    False, which only costs a generated UV map.
    """
    node_tree = getattr(material, "node_tree", None)
    if node_tree is None or not getattr(material, "use_nodes", True):
        return True
    output = _material_output(node_tree)
    if output is None:
        return True
    surface = output.inputs.get("Surface")
    if surface is None:
        return True
    return _socket_is_uv_periodic(surface, bake_uv, render_uv, set())


def _material_output(node_tree):
    try:
        output = node_tree.get_output_node("CYCLES")
    except Exception:
        output = None
    if output is not None:
        return output
    outputs = [node for node in node_tree.nodes if node.type == "OUTPUT_MATERIAL"]
    active = [node for node in outputs if getattr(node, "is_active_output", False)]
    return (active or outputs or [None])[0]


def _socket_is_uv_periodic(socket, bake_uv, render_uv, visiting) -> bool:
    for link in getattr(socket, "links", ()) or ():
        if getattr(link, "is_muted", False):
            continue
        if not _node_is_uv_periodic(link.from_node, link.from_socket, bake_uv, render_uv, visiting):
            return False
    return True


def _node_is_uv_periodic(node, from_socket, bake_uv, render_uv, visiting) -> bool:
    key = (
        node.as_pointer() if hasattr(node, "as_pointer") else id(node),
        getattr(from_socket, "identifier", None),
    )
    if key in visiting:
        return True
    visiting.add(key)
    node_type = getattr(node, "type", "")
    if getattr(node, "mute", False):
        # A muted node passes its first matching input through.
        return all(_socket_is_uv_periodic(i, bake_uv, render_uv, visiting) for i in node.inputs)

    if node_type == "TEX_IMAGE":
        if getattr(node, "extension", "REPEAT") != "REPEAT" or getattr(node, "projection", "FLAT") != "FLAT":
            return False
        vector = node.inputs.get("Vector")
        if vector is None or not vector.is_linked:
            # An unlinked Vector samples the render UV map.
            return render_uv == bake_uv
        return _socket_is_uv_periodic(vector, bake_uv, render_uv, visiting)
    if node_type == "UVMAP":
        if getattr(node, "from_instancer", False):
            return False
        return (getattr(node, "uv_map", "") or render_uv) == bake_uv
    if node_type == "TEX_COORD":
        if getattr(node, "from_instancer", False) or getattr(from_socket, "name", "") != "UV":
            return False
        return render_uv == bake_uv
    if node_type == "MAPPING":
        return _mapping_keeps_period(node) and all(
            _socket_is_uv_periodic(i, bake_uv, render_uv, visiting)
            for i in node.inputs
            if i.name == "Vector"
        )
    if node_type == "GROUP":
        tree = getattr(node, "node_tree", None)
        if tree is None:
            return True
        outputs = [n for n in tree.nodes if n.type == "GROUP_OUTPUT"]
        active = [n for n in outputs if getattr(n, "is_active_output", False)] or outputs
        if not active:
            return True
        inner = active[0].inputs.get(getattr(from_socket, "identifier", ""))
        if inner is None:
            inner = active[0].inputs.get(getattr(from_socket, "name", ""))
        if inner is not None and not _socket_is_uv_periodic(inner, bake_uv, render_uv, visiting):
            return False
        # Group inputs lead back out to this node's own inputs.
        return all(_socket_is_uv_periodic(i, bake_uv, render_uv, visiting) for i in node.inputs)
    if node_type in ("NORMAL_MAP", "BUMP"):
        return True
    if node_type in _PURE_NODE_TYPES or _is_shader_node(node):
        return all(
            _socket_is_uv_periodic(i, bake_uv, render_uv, visiting)
            for i in node.inputs
            if i.name not in _SHADING_ONLY_INPUTS
        )
    return False


def _is_shader_node(node) -> bool:
    outputs = list(getattr(node, "outputs", ()) or ())
    return bool(outputs) and all(getattr(o, "type", "") == "SHADER" for o in outputs)


def _mapping_keeps_period(node) -> bool:
    """A Mapping node keeps period 1 when it only shifts and tiles by whole numbers."""
    if getattr(node, "vector_type", "POINT") not in ("POINT", "TEXTURE"):
        return False
    rotation = node.inputs.get("Rotation")
    scale = node.inputs.get("Scale")
    location = node.inputs.get("Location")
    for socket in (rotation, scale, location):
        if socket is not None and socket.is_linked:
            return False
    if rotation is not None and any(abs(v) > 1e-6 for v in rotation.default_value):
        return False
    if scale is not None and any(abs(v - round(v)) > 1e-6 or round(v) == 0 for v in scale.default_value[:2]):
        return False
    return True


def mesh_uv_triangles(mesh, uv_layer_name: str, material_indices: Iterable[int]):
    """UV triangles of the faces whose material index is in ``material_indices``."""
    import numpy as np

    mesh.calc_loop_triangles()
    triangles = mesh.loop_triangles
    count = len(triangles)
    if not count:
        return np.zeros((0, 3, 2))
    loops = np.empty(count * 3, dtype=np.int32)
    triangles.foreach_get("loops", loops)
    materials = np.empty(count, dtype=np.int32)
    triangles.foreach_get("material_index", materials)
    layer = mesh.uv_layers[uv_layer_name]
    uv = np.empty(len(layer.uv) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uv)
    uv = uv.reshape(-1, 2)
    chosen = np.isin(materials, np.fromiter(material_indices, dtype=np.int32))
    return uv[loops.reshape(-1, 3)[chosen]]


class BakeUVLayers:
    """UV maps this run generated, for reuse and for removal after export."""

    def __init__(self):
        # (mesh, frozenset of projected material indices) -> layer name
        self._generated: Dict[tuple, str] = {}
        self.created: List[tuple] = []

    def lookup(self, mesh, material_indices) -> Optional[str]:
        return self._generated.get((_mesh_key(mesh), frozenset(material_indices)))

    def remember(self, mesh, material_indices, layer_name: str) -> None:
        self._generated[(_mesh_key(mesh), frozenset(material_indices))] = layer_name
        self.created.append((mesh, layer_name))

    def remove_all(self) -> None:
        for mesh, layer_name in reversed(self.created):
            try:
                layer = mesh.uv_layers.get(layer_name)
                if layer is not None:
                    mesh.uv_layers.remove(layer)
            except (ReferenceError, Exception):
                pass
        self.created.clear()
        self._generated.clear()


def _mesh_key(mesh):
    uid = getattr(mesh, "session_uid", None)
    return uid if uid is not None else mesh.as_pointer()


def generation_blocker(obj) -> Optional[str]:
    """Why a bake UV map cannot be added to ``obj``'s mesh, or None."""
    mesh = obj.data
    if getattr(mesh, "library", None) is not None:
        return f"its mesh '{mesh.name}' is linked from another .blend file"
    if len(mesh.uv_layers) >= _MAX_UV_LAYERS:
        return f"its mesh '{mesh.name}' already has {_MAX_UV_LAYERS} UV maps, Blender's limit"
    return None


def generate_bake_uv_layer(
    context,
    obj,
    *,
    source_uv: str,
    material_indices: Sequence[int],
    island_margin: float,
) -> str:
    """Add a UV map to ``obj``'s mesh with the given materials' faces unwrapped.

    The new map starts as a copy of ``source_uv``; only the faces of
    ``material_indices`` are re-unwrapped, each material into the whole 0-1
    square, because every material bakes into its own image. The render UV map
    and the active UV map are left as they were.
    """
    import bmesh
    import bpy

    mesh = obj.data
    uv_layers = mesh.uv_layers
    previous_active = uv_layers.active.name if uv_layers.active else source_uv
    source = uv_layers[source_uv]
    uv_layers.active = source
    name = _unique_layer_name(uv_layers)
    layer = uv_layers.new(name=name, do_init=True)
    name = layer.name
    uv_layers.active = uv_layers[name]
    try:
        with _editable(context, obj):
            bpy.ops.object.mode_set(mode="EDIT")
            try:
                bm = bmesh.from_edit_mesh(mesh)
                hidden = [face.index for face in bm.faces if face.hide]
                selected = {face.index for face in bm.faces if face.select}
                for face in bm.faces:
                    if face.hide:
                        face.hide_set(False)
                bm.select_mode = {"FACE"}
                for index in material_indices:
                    for face in bm.faces:
                        face.select_set(face.material_index == index)
                    bm.select_flush_mode()
                    bmesh.update_edit_mesh(mesh)
                    if not any(face.select for face in bm.faces):
                        continue
                    bpy.ops.uv.smart_project(
                        angle_limit=_SMART_PROJECT_ANGLE_LIMIT,
                        margin_method="FRACTION",
                        island_margin=float(island_margin),
                        area_weight=0.0,
                        correct_aspect=True,
                        scale_to_bounds=False,
                    )
                    bm = bmesh.from_edit_mesh(mesh)
                bm.faces.ensure_lookup_table()
                for face in bm.faces:
                    face.select_set(face.index in selected)
                for index in hidden:
                    bm.faces[index].hide_set(True)
                bmesh.update_edit_mesh(mesh)
            finally:
                bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        active = uv_layers.get(previous_active)
        if active is not None:
            uv_layers.active = active
    return name


def _unique_layer_name(uv_layers) -> str:
    name = BAKE_UV_LAYER_NAME
    suffix = 1
    while uv_layers.get(name) is not None:
        name = f"{BAKE_UV_LAYER_NAME}_{suffix}"
        suffix += 1
    return name


@contextmanager
def _editable(context, obj):
    """Make ``obj`` the only selected, visible, active object for edit mode."""
    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    previous_selection = [o for o in view_layer.objects if o.select_get()]
    hide_viewport = bool(obj.hide_viewport)
    hidden = bool(obj.hide_get())
    try:
        obj.hide_viewport = False
        obj.hide_set(False)
        for other in previous_selection:
            other.select_set(False)
        obj.select_set(True)
        view_layer.objects.active = obj
        yield
    finally:
        try:
            obj.select_set(False)
            for other in previous_selection:
                other.select_set(True)
            view_layer.objects.active = previous_active
            obj.hide_set(hidden)
            obj.hide_viewport = hide_viewport
        except (ReferenceError, Exception):
            pass


@contextmanager
def temporary_active_uv(obj, layer_name: str):
    """Make ``layer_name`` the UV map a bake of ``obj`` writes into."""
    uv_layers = obj.data.uv_layers
    previous = uv_layers.active.name if uv_layers.active else None
    uv_layers.active = uv_layers[layer_name]
    try:
        yield
    finally:
        restored = uv_layers.get(previous) if previous else None
        if restored is not None:
            uv_layers.active = restored
