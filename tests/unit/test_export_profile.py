from types import SimpleNamespace

import pytest

from Plugin.export_profile import (
    MATERIAL_TYPE_PBR,
    MATERIAL_TYPE_UNLIT,
    PBR_PROCESSING_BAKE,
    PBR_PROCESSING_TRANSLATE,
    PIPELINE_BAKE,
    PIPELINE_DIRECT,
    UNLIT_APPEARANCE_COLOR,
    UNLIT_APPEARANCE_LIGHTING,
    resolve_ui_export_route,
)


@pytest.mark.parametrize(
    ("settings", "pipeline", "bake_mode"),
    [
        (
            SimpleNamespace(
                ui_material_type=MATERIAL_TYPE_PBR,
                ui_pbr_processing=PBR_PROCESSING_TRANSLATE,
            ),
            PIPELINE_DIRECT,
            None,
        ),
        (
            SimpleNamespace(
                ui_material_type=MATERIAL_TYPE_PBR,
                ui_pbr_processing=PBR_PROCESSING_BAKE,
            ),
            PIPELINE_BAKE,
            "LIT_ALBEDO",
        ),
        (
            SimpleNamespace(
                ui_material_type=MATERIAL_TYPE_UNLIT,
                ui_unlit_appearance=UNLIT_APPEARANCE_COLOR,
            ),
            PIPELINE_BAKE,
            "UNLIT_ALBEDO",
        ),
        (
            SimpleNamespace(
                ui_material_type=MATERIAL_TYPE_UNLIT,
                ui_unlit_appearance=UNLIT_APPEARANCE_LIGHTING,
            ),
            PIPELINE_BAKE,
            "LIT_IBL",
        ),
    ],
)
def test_artist_profile_routes_to_existing_pipeline(settings, pipeline, bake_mode):
    route = resolve_ui_export_route(settings)

    assert route.pipeline == pipeline
    assert route.bake_mode == bake_mode


def test_artist_profile_defaults_to_direct_realitykit_pbr():
    route = resolve_ui_export_route(SimpleNamespace())

    assert route.pipeline == PIPELINE_DIRECT
    assert route.bake_mode is None
