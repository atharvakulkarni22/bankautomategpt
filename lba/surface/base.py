"""The Surface interface: the ONLY way the rest of the project touches an app.

The agent, the recorder and the replay engine call these six methods and use
Targets. They never import Playwright. BrowserSurface (browser.py) implements
the interface for web pages; a future surface could drive a desktop app instead.
"""

from typing import Protocol

from pydantic import BaseModel, Field

from .target import Target


class SurfaceError(Exception):
    """Something went wrong talking to the app. The message is safe to show the agent."""


class TargetNotFound(SurfaceError):
    """No element matches the Target."""


class AmbiguousTarget(SurfaceError):
    """More than one element matches, so we refuse to guess (a wrong click could move money)."""


class SurfaceTimeout(SurfaceError):
    """The app was too slow, or the element was not ready in time (not visible, not enabled,
    covered by something). Waiting and trying again often works, so replay retries these."""


class Observation(BaseModel):
    """What the surface sees right now."""

    url: str
    title: str
    tree: str  # accessibility tree as text, including the content of iframes
    screenshot: bytes = Field(repr=False)  # PNG


class Surface(Protocol):
    def observe(self) -> Observation: ...

    def click(self, target: Target) -> None: ...

    def type(self, target: Target, text: str) -> None:
        """`text` may contain {{placeholders}}; the surface fills in real values (see placeholders.py)."""
        ...

    def read(self, target: Target) -> str: ...

    def wait_for(self, target: Target, timeout_ms: int | None = None) -> None: ...

    def goto(self, url: str) -> None: ...

    def describe(self, target: Target) -> list[Target]:
        """Other Targets that find the same element, best first. The recorder stores these as fallbacks."""
        ...

    # The next four are what replay needs. They look at the page right now and never wait.

    def locate(self, targets: list[Target]) -> int:
        """Index of the first Target that finds exactly one element. Raises TargetNotFound if none does."""
        ...

    def is_visible(self, target: Target) -> bool:
        """True if at least one element matching the Target is visible."""
        ...

    def current_url(self) -> str: ...

    def pause(self, seconds: float) -> None:
        """Wait, while still letting the app run (a plain time.sleep can stall a browser)."""
        ...
