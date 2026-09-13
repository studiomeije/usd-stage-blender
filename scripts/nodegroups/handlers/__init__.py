"""
Preview handlers for RealityKit NodeGroups.
"""

from typing import Callable, Optional

from . import mix
from . import pbr
from . import texture
from . import unlit


def get_preview_builder(entry: dict, node_id: str) -> Optional[Callable]:
    """Return a preview builder for the given node entry."""
    preview = entry.get("preview")
    if preview == "pbr" or node_id == "realitykit_pbr_surfaceshader":
        return pbr.build_preview
    if preview == "unlit" or node_id == "realitykit_unlit_surfaceshader":
        return unlit.build_preview
    if node_id == "mix":
        return mix.build_preview
    if texture.supports(node_id):
        return texture.build_preview
    return None
