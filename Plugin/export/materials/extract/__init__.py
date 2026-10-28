"""Blender material extraction: node graphs in, normalized material data out."""

from .core import (
    collect_material_warnings,
    extract_blender_material_data,
)
from .images import (
    begin_image_staging_session,
    cleanup_image_staging_session,
)

__all__ = [
    "extract_blender_material_data",
    "collect_material_warnings",
    "begin_image_staging_session",
    "cleanup_image_staging_session",
]
