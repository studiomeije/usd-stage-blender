"""
RealityKit node catalog and validation.
"""

_needs_reload = "bpy" in locals()

import bpy  # noqa: E402,F401 - its presence in locals() detects a reload

from . import metadata  # noqa: E402
from . import validate  # noqa: E402

if _needs_reload:
    import importlib
    metadata = importlib.reload(metadata)
    validate = importlib.reload(validate)

__all__ = [
    "metadata",
    "validate",
]
