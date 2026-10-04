"""Turning Targets into Playwright locators, and Playwright elements into Targets.

resolve():          Target  -> the one matching element (main page AND iframes)
describe_element(): element -> several candidate Targets, best first, so the
                    recorder can store fallbacks in case one stops working.
"""

import re

from playwright.sync_api import Error as PlaywrightError

from .base import AmbiguousTarget, SurfaceError, TargetNotFound
from .target import Target


def locator_in_frame(frame, target: Target):
    """Build a locator that searches inside one frame (the main page is a frame too)."""
    if target.role is not None:
        return frame.get_by_role(target.role, name=target.name, exact=target.exact)
    if target.label is not None:
        return frame.get_by_label(target.label, exact=target.exact)
    if target.text is not None:
        return frame.get_by_text(target.text, exact=target.exact)
    return frame.locator(target.css)


def resolve(page, target: Target):
    """Find the single element a Target points at, searching every frame.

    Counts matches right now (it does not wait; BrowserSurface adds the waiting).
    Raises TargetNotFound for zero matches and AmbiguousTarget for more than one.
    """
    found = []  # (frame, locator, how many matches in that frame)
    for frame in page.frames:  # main page first, then each iframe
        try:
            locator = locator_in_frame(frame, target)
            count = locator.count()
        except PlaywrightError:
            continue  # frame is navigating or was removed; treat as no match
        if count:
            found.append((frame, locator, count))

    total = sum(count for _, _, count in found)
    if total == 0:
        raise TargetNotFound(f"No element matches {target}.")
    if total > 1:
        where = ", ".join(f"{count} in {frame.url}" for frame, _, count in found)
        raise AmbiguousTarget(f"{target} matches {total} elements ({where}). Use a more specific Target.")
    return found[0][1]


# ------------------------------------------------------------ describe_element

# Facts about an element that Playwright cannot give us directly.
_FACTS_JS = r"""
(el) => {
  const doc = el.ownerDocument;
  const tag = el.tagName.toLowerCase();
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // Label text, ignoring any form control sitting inside the <label>.
  let label = null;
  if (el.labels && el.labels.length) {
    const copy = el.labels[0].cloneNode(true);
    copy.querySelectorAll('input,select,textarea,button').forEach((n) => n.remove());
    label = clean(copy.textContent) || null;
  }

  // Visible text. Buttons made from <input> show their value instead.
  let text = null;
  if (tag === 'input' && ['submit', 'button', 'reset'].includes(el.type)) {
    text = clean(el.value) || null;
  } else if (!['input', 'select', 'textarea'].includes(tag)) {
    const t = clean(el.innerText);
    text = t && t.length <= 60 ? t : null;
  }

  // A CSS path: #id, else tag[name], else a chain like body > table:nth-of-type(2) > ...
  const unique = (sel) => { try { return doc.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  let css = null;
  if (el.id && unique('#' + CSS.escape(el.id))) css = '#' + CSS.escape(el.id);
  const name = el.getAttribute('name');
  if (!css && name) {
    const sel = tag + '[name="' + name.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
    if (unique(sel)) css = sel;
  }
  if (!css) {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== doc.documentElement; node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      if (node === doc.body) break;
    }
    css = parts.join(' > ');
  }
  return { label, text, css };
}
"""

# First line of an element's aria snapshot looks like:  - button "Search"
_ARIA_FIRST_LINE = re.compile(r'^- ([A-Za-z]+)(?: "((?:[^"\\]|\\.)*)")?')
_NOT_REAL_ROLES = {"text", "generic", "iframe"}


def _role_and_name(locator):
    first_line = locator.aria_snapshot().strip().splitlines()[0]
    match = _ARIA_FIRST_LINE.match(first_line)
    if not match or match.group(1) in _NOT_REAL_ROLES or not match.group(2):
        return None, None  # no usable accessible name
    return match.group(1), match.group(2).replace('\\"', '"')


def _points_at(page, target: Target, handle) -> bool:
    """True if the Target finds exactly this one element (nothing else)."""
    try:
        other = resolve(page, target).element_handle(timeout=1000)
        try:
            return bool(handle.evaluate("(a, b) => a === b", other))
        finally:
            other.dispose()
    except (SurfaceError, PlaywrightError):
        return False


def describe_element(locator) -> list[Target]:
    """Candidate Targets for the element, most meaningful first.

    Every candidate is checked: it must find exactly this element and no other.
    Order: role+name, label, text, css. For each kind, the loose (substring)
    version is preferred and the exact version is the fallback.
    """
    handle = locator.element_handle()
    try:
        page = handle.owner_frame().page
        facts = handle.evaluate(_FACTS_JS)
        role, name = _role_and_name(locator)

        groups = []
        if role:
            groups.append([Target(role=role, name=name), Target(role=role, name=name, exact=True)])
        if facts["label"]:
            groups.append([Target(label=facts["label"]), Target(label=facts["label"], exact=True)])
        if facts["text"]:
            groups.append([Target(text=facts["text"]), Target(text=facts["text"], exact=True)])
        groups.append([Target(css=facts["css"])])

        candidates = []
        for group in groups:
            for target in group:
                if _points_at(page, target, handle):
                    candidates.append(target)
                    break
        return candidates
    finally:
        handle.dispose()
