"""RealityKit collider components, authored from Blender's rigid bodies.

An exported asset that a RealityKit app can tap, drag or collide with needs a
collision component, and until now that was hand-work in Reality Composer Pro
after every export. Blender already knows which objects are physical: the
artist enables Rigid Body on them. That is the source.

What it writes is RealityKit's own component, not ``UsdPhysics``. The
conformance probes settled which, and the answer is not the obvious one: a
mesh carrying ``PhysicsCollisionAPI``, ``PhysicsRigidBodyAPI`` and
``PhysicsMassAPI`` compiles to a ``.reality`` holding only Material, Mesh and
Scene - the physics is dropped without a word - while a ``RealityKitComponent``
prim compiles to a real ``CollisionShape``. See
``docs/APPLE_CONFORMANCE.md``.

The shape written is the one whose schema is known from assets Reality
Composer Pro itself produced: a box, sized from the object's own bounds. Any
other Blender collision shape is approximated by that box and reported, rather
than guessed at in a schema nobody has verified.
"""

from __future__ import annotations

from dataclasses import dataclass

from .object_scope import is_exportable_object
from .usd_scene import remap_prim_path
from .usd_utils import Gf, Sdf, Usd, UsdGeom, require_pxr

#: Collision filtering defaults, as Reality Composer Pro writes them: in every
#: group, colliding with everything.
_DEFAULT_GROUP = 1
_DEFAULT_MASK = 4294967295

#: ``Default`` is an ordinary collider; ``Trigger`` is a volume that reports
#: overlaps without stopping anything. Blender has no trigger concept.
_DEFAULT_TYPE = "Default"

#: The one shape whose USD spelling is known from Reality Composer Pro's own
#: output. Others exist - the exporter's own vocabulary lists Sphere and
#: Capsule - but their sizing attributes are unverified, and a collider of the
#: wrong size is worse than an honest box.
_BOX = "Box"


@dataclass(frozen=True)
class ColliderMember:
    """One object the artist marked physical in Blender."""

    object_name: str
    blender_shape: str


@dataclass(frozen=True)
class ColliderPlan:
    members: tuple[ColliderMember, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.members


def resolve_collider_plan(context, settings) -> ColliderPlan:
    """The objects whose Rigid Body settings ask for a collider."""
    if not bool(getattr(settings, "export_colliders", True)):
        return ColliderPlan()

    scene = getattr(context, "scene", None)
    members: list[ColliderMember] = []
    for obj in getattr(scene, "objects", None) or ():
        rigid_body = getattr(obj, "rigid_body", None)
        if rigid_body is None:
            continue
        if not is_exportable_object(obj, settings):
            continue
        members.append(
            ColliderMember(
                object_name=str(getattr(obj, "name", "")),
                blender_shape=str(getattr(rigid_body, "collision_shape", "BOX")),
            )
        )
    return ColliderPlan(members=tuple(members))


def author_colliders(
    stage,
    plan: ColliderPlan,
    object_map: dict | None,
    path_edits=(),
    diagnostics=None,
) -> tuple[str, ...]:
    """Author a Collider component under each member's prim.

    Returns the prim paths written. Sizing comes from the exported geometry
    rather than from Blender, so the box is already in the Apple spatial
    contract's axes and needs no conversion of its own.
    """
    if plan.is_empty:
        return ()
    require_pxr()

    if not object_map:
        if diagnostics:
            diagnostics.add_warning(
                "Colliders were requested but this export recorded no prim "
                "map, so there is no exact way to say which prim each object "
                "became. No collider was written."
            )
        return ()

    prim_paths = {
        str(name): remap_prim_path(str(path), path_edits)
        for path, name in object_map.items()
    }
    cache = UsdGeom.BBoxCache(
        Usd.TimeCode.EarliestTime(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
    )

    authored: list[str] = []
    approximated: list[str] = []
    for member in plan.members:
        path = prim_paths.get(member.object_name)
        prim = stage.GetPrimAtPath(path) if path else None
        if not prim:
            if diagnostics:
                diagnostics.add_warning(
                    f"No collider for {member.object_name!r}: it produced no "
                    "prim in this export."
                )
            continue

        bounds = _local_bounds(cache, prim)
        if bounds is None:
            if diagnostics:
                diagnostics.add_warning(
                    f"No collider for {member.object_name!r}: its bounds could "
                    "not be computed, so a box would be a guess."
                )
            continue

        extent, centre = bounds
        _author_one(stage, prim, extent, centre)
        authored.append(f"{prim.GetPath()}/Collider")
        if member.blender_shape != "BOX":
            approximated.append(f"{member.object_name} ({member.blender_shape})")

    if approximated and diagnostics:
        diagnostics.add_warning(
            "Collision shapes approximated as boxes, the one shape whose "
            f"RealityKit spelling is verified: {', '.join(sorted(approximated))}."
        )
    if authored and diagnostics:
        diagnostics.set_colliders(sorted(authored))
    return tuple(authored)


def _local_bounds(cache, prim):
    """``(extent, centre)`` of a prim's geometry in its own space."""
    try:
        box = cache.ComputeUntransformedBound(prim)
    except Exception:
        return None
    bounds = box.ComputeAlignedRange()
    if bounds.IsEmpty():
        return None
    minimum, maximum = bounds.GetMin(), bounds.GetMax()
    extent = tuple(float(maximum[i] - minimum[i]) for i in range(3))
    centre = tuple(float((maximum[i] + minimum[i]) / 2.0) for i in range(3))
    return extent, centre


def _author_one(stage, prim, extent, centre) -> None:
    """Write the component exactly as Reality Composer Pro spells it."""
    collider = stage.DefinePrim(prim.GetPath().AppendChild("Collider"),
                                "RealityKitComponent")

    identifier = collider.CreateAttribute(
        "info:id", Sdf.ValueTypeNames.Token, custom=False,
        variability=Sdf.VariabilityUniform,
    )
    identifier.Set("RealityKit.Collider")
    collider.CreateAttribute("group", Sdf.ValueTypeNames.UInt, custom=False).Set(
        _DEFAULT_GROUP
    )
    collider.CreateAttribute("mask", Sdf.ValueTypeNames.UInt, custom=False).Set(
        _DEFAULT_MASK
    )
    collider.CreateAttribute("type", Sdf.ValueTypeNames.Token, custom=False).Set(
        _DEFAULT_TYPE
    )

    shape = stage.DefinePrim(collider.GetPath().AppendChild("Shape"),
                             "RealityKitStruct")
    shape.CreateAttribute("shapeType", Sdf.ValueTypeNames.Token, custom=False).Set(
        _BOX
    )
    shape.CreateAttribute("extent", Sdf.ValueTypeNames.Float3, custom=False).Set(
        Gf.Vec3f(*extent)
    )

    # Only when the geometry is off-centre: an all-zero pose is noise.
    if any(abs(value) > 1e-6 for value in centre):
        pose = stage.DefinePrim(shape.GetPath().AppendChild("pose"),
                                "RealityKitStruct")
        pose.CreateAttribute(
            "position", Sdf.ValueTypeNames.Float3, custom=False
        ).Set(Gf.Vec3f(*centre))
