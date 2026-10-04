"""BrowserSurface: the Surface interface implemented with Playwright (sync API)."""

import re
import time
from contextlib import contextmanager

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from bag.safety.redact import blur_boxes, has_account_number, mask_account_numbers

from .base import Observation, SurfaceError, SurfaceTimeout, TargetNotFound
from .locators import (
    describe_element,
    element_visible,
    format_unnamed_controls,
    locate_first,
    resolve,
    unnamed_controls,
)
from .placeholders import Values
from .target import Target

# Playwright tags every line of its "ai" snapshot with things like [ref=f2e16]
# and [cursor=pointer]. Our Targets cannot use them, so they are just noise.
_NOISE_TAGS = re.compile(r"\s\[(?:ref|cursor)=[^\]]*\]")


# Finds the parts of a page that may hold sensitive data, in this frame's own coordinates:
# elements matching the configured selectors, the current value of every field, and text
# that contains a long run of digits. Python then decides which of those are really sensitive,
# so no secret ever has to be handed to the page's own JavaScript.
_BOXES_JS = r"""
(selectors) => {
  const box = (r) => ({ x: r.left, y: r.top, width: r.width, height: r.height });
  const selected = [];
  for (const selector of selectors) {
    try { document.querySelectorAll(selector).forEach((el) => selected.push(box(el.getBoundingClientRect()))); }
    catch (e) { /* a bad selector in the config must not stop the run */ }
  }
  const fields = [];
  for (const el of document.querySelectorAll('input, textarea')) {
    if (el.value) fields.push({ box: box(el.getBoundingClientRect()), value: el.value });
  }
  const texts = [];
  if (document.body) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (/(\d[\s-]?){8,}/.test(node.nodeValue)) {
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const r of range.getClientRects()) texts.push({ box: box(r), text: node.nodeValue });
      }
    }
  }
  return { selected, fields, texts };
}
"""


class BrowserSurface:
    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 10_000,
        viewport: dict | None = None,
        values: Values | None = None,
        blur_screenshots: bool = True,
        blur_selectors: tuple[str, ...] = ("input[type=password]",),
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Real inputs and secrets for {{placeholders}}. Default: secrets from the environment.
        self.values = values or Values()
        # Every screenshot this surface returns has sensitive spots blurred (see sensitive_boxes).
        self.blur_screenshots = blur_screenshots
        self.blur_selectors = tuple(blur_selectors)
        self._viewport = viewport or {"width": 1280, "height": 800}
        self._playwright = self._browser = self._context = self._page = None

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        # A fresh context = fresh cookies, so every run starts signed out.
        self._context = self._browser.new_context(viewport=self._viewport)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def close(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None

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
        except PlaywrightTimeoutError as error:  # must come first: it is a kind of PlaywrightError
            raise SurfaceTimeout(f"{doing} failed: {str(error)[:500]}") from error
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
            tree = _NOISE_TAGS.sub("", page.locator("body").aria_snapshot(mode="ai"))
            # Fields the tree cannot name (no label) are listed separately with a css selector.
            unnamed = format_unnamed_controls(unnamed_controls(page))
            if unnamed:
                tree += "\n\n" + unnamed
            screenshot = page.screenshot(type="png")
            if self.blur_screenshots:  # if the sensitive spots cannot be found, this raises: never send an unblurred image
                screenshot = blur_boxes(screenshot, self.sensitive_boxes())
            return Observation(
                url=page.url,
                title=mask_account_numbers(self.values.redact(page.title())),
                # A typed secret must never reach the AI, and neither must a full account number.
                tree=mask_account_numbers(self.values.redact(tree)),
                screenshot=screenshot,
            )

    def click(self, target: Target) -> None:
        locator = self._find(target)
        with self._errors(f"click {target}"):
            locator.click(timeout=self.timeout_ms)

    def type(self, target: Target, text: str) -> None:
        # Swap placeholders for real values only now, as late as possible. Done before
        # touching the page so an unknown placeholder fails fast. Error messages name
        # the target, never the text (it may be a password).
        text = self.values.substitute(text)
        locator = self._find(target)
        with self._errors(f"type into {target}"):
            locator.fill(text, timeout=self.timeout_ms)

    def read(self, target: Target) -> str:
        locator = self._find(target)
        with self._errors(f"read {target}"):
            tag = locator.evaluate("el => el.tagName.toLowerCase()")
            if tag in ("input", "textarea", "select"):
                value = locator.input_value(timeout=self.timeout_ms)
            else:
                value = locator.inner_text(timeout=self.timeout_ms).strip()
            return self.values.redact(value)

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

    # ---------------------------------------------------------- for replay

    def locate(self, targets: list[Target]) -> int:
        """Which of the Targets (checked in order) finds exactly one element right now."""
        with self._errors("locate"):
            return locate_first(self.page, targets)

    def is_visible(self, target: Target) -> bool:
        with self._errors(f"check visibility of {target}"):
            return element_visible(self.page, target)

    def current_url(self) -> str:
        return self.page.url

    def pause(self, seconds: float) -> None:
        # Wrapped so that a browser window closed by a human surfaces as a SurfaceError, not a raw crash.
        with self._errors("pause"):
            self.page.wait_for_timeout(seconds * 1000)

    # ------------------------------------------------------- for human takeover

    def add_init_script(self, script: str) -> None:
        """Run `script` in every page and frame loaded from now on, AND in the frames already open.

        (Playwright's own add_init_script only affects documents loaded later, so the open frames
        are handled by hand.) The script must be safe to run twice in the same frame.
        """
        with self._errors("add_init_script"):
            self._context.add_init_script(script=script)
            for frame in self.page.frames:
                try:
                    frame.evaluate(script)
                except PlaywrightError:
                    continue  # a frame that is navigating gets the script from the init script instead

    def evaluate_in_frames(self, expression: str) -> list:
        """Evaluate a JavaScript expression in the main page and every iframe. One result per frame."""
        results = []
        with self._errors("evaluate_in_frames"):
            for frame in self.page.frames:
                try:
                    results.append(frame.evaluate(expression))
                except PlaywrightError:
                    continue  # the frame went away while we were looking
        return results

    def bring_to_front(self) -> None:
        """Raise the browser window, so a human who has to take over can see it."""
        with self._errors("bring_to_front"):
            self.page.bring_to_front()

    # ------------------------------------------------------ for the safety layer

    def frame_urls(self) -> list[str]:
        """The addresses loaded inside iframes. The guard checks them against its allowlist."""
        page = self.page
        return [frame.url for frame in page.frames if frame != page.main_frame]

    def sensitive_boxes(self) -> list[tuple[float, float, float, float]]:
        """Screen rectangles (x, y, width, height) to blur: selectors from the config, fields
        that hold a secret, and text that looks like an account number. Iframes included."""
        boxes = []
        page = self.page
        for frame in page.frames:
            if frame == page.main_frame:
                dx = dy = 0.0
            else:
                frame_box = frame.frame_element().bounding_box()  # where the iframe sits on the page
                if frame_box is None:
                    continue  # the iframe is not displayed, so nothing in it is on screen
                dx, dy = frame_box["x"], frame_box["y"]
            found = frame.evaluate(_BOXES_JS, list(self.blur_selectors))

            def place(box):
                return (box["x"] + dx, box["y"] + dy, box["width"], box["height"])

            boxes += [place(b) for b in found["selected"]]
            boxes += [place(f["box"]) for f in found["fields"]
                      if self.values.redact(f["value"]) != f["value"] or has_account_number(f["value"])]
            boxes += [place(t["box"]) for t in found["texts"] if has_account_number(t["text"])]
        return boxes
