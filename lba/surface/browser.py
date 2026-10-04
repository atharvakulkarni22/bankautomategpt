"""BrowserSurface: the Surface interface implemented with Playwright (sync API)."""

import re
import time
from contextlib import contextmanager

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .base import Observation, SurfaceError, TargetNotFound
from .locators import describe_element, resolve
from .target import Target

# Playwright tags every line of its "ai" snapshot with things like [ref=f2e16]
# and [cursor=pointer]. Our Targets cannot use them, so they are just noise.
_NOISE_TAGS = re.compile(r"\s\[(?:ref|cursor)=[^\]]*\]")


class BrowserSurface:
    def __init__(self, headless: bool = True, timeout_ms: int = 10_000, viewport: dict | None = None):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._viewport = viewport or {"width": 1280, "height": 800}
        self._playwright = self._browser = self._page = None

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        # A fresh context = fresh cookies, so every run starts signed out.
        context = self._browser.new_context(viewport=self._viewport)
        self._page = context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def close(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._playwright = self._browser = self._page = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.close()

    @property
    def page(self):
        if self._page is None:
            raise SurfaceError("The browser is not started. Call start() or use 'with BrowserSurface():'.")
        return self._page

    # ------------------------------------------------------------------ helpers

    @contextmanager
    def _errors(self, doing: str):
        """Turn Playwright's long errors into a short SurfaceError."""
        try:
            yield
        except PlaywrightError as error:
            raise SurfaceError(f"{doing} failed: {str(error)[:500]}") from error

    def _find(self, target: Target, timeout_ms: int | None = None):
        """resolve(), but keep retrying while the element has not appeared yet."""
        deadline = time.monotonic() + (timeout_ms or self.timeout_ms) / 1000
        while True:
            try:
                return resolve(self.page, target)
            except TargetNotFound:
                if time.monotonic() >= deadline:
                    raise
                self.page.wait_for_timeout(100)

    # ------------------------------------------------------- the Surface methods

    def goto(self, url: str) -> None:
        with self._errors(f"goto {url}"):
            self.page.goto(url, wait_until="load")

    def observe(self) -> Observation:
        page = self.page
        with self._errors("observe"):
            page.wait_for_load_state("domcontentloaded")
            # mode="ai" includes the content of iframes inline under the iframe node.
            tree = page.locator("body").aria_snapshot(mode="ai")
            return Observation(
                url=page.url,
                title=page.title(),
                tree=_NOISE_TAGS.sub("", tree),
                screenshot=page.screenshot(type="png"),
            )

    def click(self, target: Target) -> None:
        locator = self._find(target)
        with self._errors(f"click {target}"):
            locator.click(timeout=self.timeout_ms)

    def type(self, target: Target, text: str) -> None:
        # The error message names the target, never the text (it may be a password).
        locator = self._find(target)
        with self._errors(f"type into {target}"):
            locator.fill(text, timeout=self.timeout_ms)

    def read(self, target: Target) -> str:
        locator = self._find(target)
        with self._errors(f"read {target}"):
            tag = locator.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea", "select"):
                return locator.input_value(timeout=self.timeout_ms)
            return locator.inner_text(timeout=self.timeout_ms).strip()

    def wait_for(self, target: Target, timeout_ms: int | None = None) -> None:
        timeout_ms = timeout_ms or self.timeout_ms
        locator = self._find(target, timeout_ms)
        with self._errors(f"wait for {target}"):
            locator.wait_for(state="visible", timeout=timeout_ms)

    # ------------------------------------------------------------ for recording

    def describe(self, target: Target) -> list[Target]:
        """Fallback Targets for the element this Target points at (see describe_element)."""
        locator = self._find(target)
        with self._errors(f"describe {target}"):
            return describe_element(locator)
