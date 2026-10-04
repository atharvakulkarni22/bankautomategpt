"""Browser layer (Playwright): sees and acts on the page.

Everything outside this package talks to apps through the Surface interface.
"""

from .base import AmbiguousTarget, Observation, Surface, SurfaceError, TargetNotFound
from .browser import BrowserSurface
from .locators import describe_element, resolve
from .target import Target

__all__ = [
    "AmbiguousTarget",
    "BrowserSurface",
    "Observation",
    "Surface",
    "SurfaceError",
    "Target",
    "TargetNotFound",
    "describe_element",
    "resolve",
]
