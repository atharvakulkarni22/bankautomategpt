"""Turning Targets into Playwright locators, and Playwright elements into Targets.

resolve():          Target  -> the one matching element (main page AND iframes)
describe_element(): element -> several candidate Targets, best first, so the
                    recorder can store fallbacks in case one stops working.
unnamed_controls(): form fields the accessibility tree cannot name, with a css
                    selector for each (so the agent can still target them).
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


def locate_first(page, targets: list[Target]) -> int:
    """Index of the first Target that finds exactly one element (checked in order, right now).

    This is how fallbacks work: the primary is tried first, and each later Target is
    only looked at if everything before it found nothing or was ambiguous.
    """
    reasons = []
    for index, target in enumerate(targets):
        try:
            resolve(page, target)
            return index
        except (TargetNotFound, AmbiguousTarget) as error:
            reasons.append(str(error))
    raise TargetNotFound(f"None of the {len(targets)} target(s) matched: " + " | ".join(reasons))


def element_visible(page, target: Target) -> bool:
    """True if any element matching the Target is visible (hidden leftovers in the page do not count)."""
    for frame in page.frames:
        try:
            locator = locator_in_frame(frame, target)
            for i in range(min(locator.count(), 20)):
                if locator.nth(i).is_visible():
                    return True
        except PlaywrightError:
            continue  # frame is navigating or was removed
    return False


# ------------------------------------------------------------------ JavaScript

# Builds a CSS selector for an element: #id, else tag[name="..."], else a chain
# like body > table:nth-of-type(2) > ... Pasted inside the scripts below.
_CSS_PATH_FN = r"""
  const cssPath = (el) => {
    const doc = el.ownerDocument;
    const tag = el.tagName.toLowerCase();
    const unique = (sel) => { try { return doc.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
    if (el.id && unique('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    const name = el.getAttribute('name');
    if (name) {
      const sel = tag + '[name="' + name.replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
      if (unique(sel)) return sel;
    }
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
    return parts.join(' > ');
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
"""

# The same script under a public name: the human recorder (bag.handoff) reuses it, so a human's
# click is described with the very same css path that the rest of the system would use.
CSS_PATH_JS = _CSS_PATH_FN

# Facts about one element that Playwright cannot give us directly.
_FACTS_JS = (
    "(el) => {\n"
    + _CSS_PATH_FN
    + r"""
  const tag = el.tagName.toLowerCase();

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
  return { label, text, css: cssPath(el) };
}
"""
)

# Visible fields with no label, aria-label, title or placeholder: the accessibility
# tree shows them as a bare "textbox". Report each with a css selector and the
# text next to it (the cell on its left, else its table row).
_UNNAMED_JS = (
    "() => {\n"
    + _CSS_PATH_FN
    + r"""
  const out = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
    if (!el.getClientRects().length) continue;  // not visible
    const named = (el.labels && el.labels.length) || el.getAttribute('aria-label')
      || el.getAttribute('aria-labelledby') || el.getAttribute('title') || el.getAttribute('placeholder');
    if (named) continue;
    const role = tag === 'select' ? 'combobox' : type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : 'textbox';
    const cell = el.closest('td, th');
    let near = cell && cell.previousElementSibling ? clean(cell.previousElementSibling.innerText) : '';
    if (!near) { const row = el.closest('tr'); near = row ? clean(row.innerText) : ''; }
    out.push({ role, near: near.slice(0, 40), css: cssPath(el) });
  }
  return out;
}
"""
)


def unnamed_controls(page) -> list[dict]:
    """Fields the accessibility tree cannot name: [{role, near, css}, ...] across all frames."""
    found = []
    for frame in page.frames:
        try:
            found += frame.evaluate(_UNNAMED_JS)
        except PlaywrightError:
            continue  # frame is navigating or was removed
    return found


def format_unnamed_controls(controls: list[dict]) -> str:
    """Text block for the observation. Empty string when there is nothing to report."""
    if not controls:
        return ""
    lines = ["Fields with no accessible name (target these with css):"]
    for c in controls:
        near = f' next to "{c["near"]}"' if c["near"] else ""
        lines.append(f'- {c["role"]}{near}: css={c["css"]}')
    return "\n".join(lines)


# ------------------------------------------------------------ describe_element

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
