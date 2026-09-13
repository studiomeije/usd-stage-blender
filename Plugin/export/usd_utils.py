"""
Shared OpenUSD helpers for USD Stage for Blender.

Centralizes pxr imports so other modules can depend on a single source.
"""


try:
    from pxr import Usd, UsdShade, Sdf, Gf, UsdGeom, Vt
    PXR_AVAILABLE = True
except ImportError:
    Usd = UsdShade = Sdf = Gf = UsdGeom = Vt = None
    PXR_AVAILABLE = False


def require_pxr() -> None:
    """Raise a clear error when OpenUSD bindings are unavailable."""
    if not PXR_AVAILABLE:
        raise RuntimeError(
            "pxr (OpenUSD Python bindings) not available. "
            "Please install OpenUSD Python bindings to use material rewriting."
        )
