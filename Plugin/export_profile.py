"""Artist-facing Blender UI export-profile routing.

The CLI keeps its explicit ``export`` and ``bake-export`` commands. Blender's
panel presents those pipelines as two output material types with contextual
options and resolves them here before invoking either existing operator.
"""

from __future__ import annotations

from dataclasses import dataclass


MATERIAL_TYPE_PBR = "REALITYKIT_PBR"
MATERIAL_TYPE_UNLIT = "REALITYKIT_UNLIT"

PBR_PROCESSING_TRANSLATE = "TRANSLATE"
PBR_PROCESSING_BAKE = "BAKE"

UNLIT_APPEARANCE_COLOR = "MATERIAL_COLOR"
UNLIT_APPEARANCE_LIGHTING = "LIGHTING_SHADOWS"

PIPELINE_DIRECT = "DIRECT"
PIPELINE_BAKE = "BAKE"


@dataclass(frozen=True)
class ExportRoute:
    pipeline: str
    bake_mode: str | None = None


def resolve_ui_export_route(settings) -> ExportRoute:
    """Map the artist-facing selection onto the existing export pipelines."""
    material_type = getattr(settings, "ui_material_type", MATERIAL_TYPE_PBR)
    if material_type == MATERIAL_TYPE_PBR:
        processing = getattr(
            settings,
            "ui_pbr_processing",
            PBR_PROCESSING_TRANSLATE,
        )
        if processing == PBR_PROCESSING_BAKE:
            return ExportRoute(PIPELINE_BAKE, "LIT_ALBEDO")
        return ExportRoute(PIPELINE_DIRECT)

    appearance = getattr(
        settings,
        "ui_unlit_appearance",
        UNLIT_APPEARANCE_COLOR,
    )
    if appearance == UNLIT_APPEARANCE_LIGHTING:
        return ExportRoute(PIPELINE_BAKE, "LIT_IBL")
    return ExportRoute(PIPELINE_BAKE, "UNLIT_ALBEDO")


def ui_bake_profile_label(settings) -> str | None:
    """The panel label of a Profile that bakes materials, or None when it translates."""
    route = resolve_ui_export_route(settings)
    if route.pipeline != PIPELINE_BAKE:
        return None
    return {
        "LIT_ALBEDO": "RealityKit PBR ▸ Bake Materials",
        "UNLIT_ALBEDO": "RealityKit Unlit ▸ Material Color Only",
        "LIT_IBL": "RealityKit Unlit ▸ Lighting & Shadows",
    }[route.bake_mode]
