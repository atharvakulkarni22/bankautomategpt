"""Watching what a human does in the browser while they have taken over.

A small script is injected into EVERY frame. It listens for clicks and typing and keeps a
list in window.__humanEvents, each event with a css path and an accessible name (the same
way the rest of bag names elements). Password fields are masked: their value is never read.

One wrinkle: when the human clicks something that loads a new page (like Search, whose
result loads inside the iframe), the browser throws away window.__humanEvents with the old
page. So the script also saves its list to sessionStorage after every event, keyed by the
frame's position, and the new page picks it up again. Nothing is lost on the way.
"""

from bag.safety import Redactor
from bag.surface.locators import CSS_PATH_JS

HUMAN_RECORDER_JS = (
    "(() => {\n"
    + CSS_PATH_JS  # defines cssPath(el) and clean(text), shared with the rest of bag
    + r"""
  if (window.__bagHumanInstalled) return;       // this script may run twice in the same frame
  window.__bagHumanInstalled = true;

  const store = (() => { try { return window.sessionStorage; } catch (e) { return null; } })();
  // Where this frame sits in the page, e.g. "" for the page itself and "0" for its first iframe.
  const framePath = (() => {
    const path = [];
    try {
      let w = window;
      while (w !== w.top) { path.unshift(Array.prototype.indexOf.call(w.parent.frames, w)); w = w.parent; }
    } catch (e) { /* a cross-origin parent: keep what we have */ }
    return path.join('/');
  })();
  const PREFIX = '__bagHumanEvents:';
  const KEY = PREFIX + framePath;
  const FLAG = '__bagHumanOn';                   // recording only happens while this is set

  const read = (key) => { try { return store ? store.getItem(key) : null; } catch (e) { return null; } };
  const isOn = () => read(FLAG) === '1';
  let events = [];
  try { events = JSON.parse(read(KEY) || '[]'); } catch (e) { events = []; }
  window.__humanEvents = events;
  const save = () => { try { if (store) store.setItem(KEY, JSON.stringify(events)); } catch (e) { /* storage full or blocked */ } };
  const wipe = () => {
    try { if (store) Object.keys(store).filter((k) => k.startsWith(PREFIX)).forEach((k) => store.removeItem(k)); } catch (e) {}
    events.length = 0;
  };
  window.__bagHumanStart = () => { wipe(); try { store.setItem(FLAG, '1'); } catch (e) {} };
  window.__bagHumanStop = () => { wipe(); try { store.removeItem(FLAG); } catch (e) {} };
  window.__bagHumanCollect = () => events.slice();

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox' || type === 'radio') return type;
      return 'textbox';
    }
    return tag;
  };

  // An approximation of the accessible name: aria-label, aria-labelledby, the <label>, the
  // button's value, alt / title / placeholder, then the element's own text.
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const text = by.split(/\s+/).map((id) => { const n = document.getElementById(id); return n ? n.textContent : ''; }).join(' ');
      if (clean(text)) return clean(text);
    }
    if (el.labels && el.labels.length) {
      const copy = el.labels[0].cloneNode(true);
      copy.querySelectorAll('input,select,textarea,button').forEach((n) => n.remove());
      if (clean(copy.textContent)) return clean(copy.textContent);
    }
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['submit', 'button', 'reset'].includes(type)) return clean(el.value);
    const alt = el.getAttribute('alt') || el.getAttribute('title') || el.getAttribute('placeholder');
    if (alt) return clean(alt);
    if (tag === 'input' || tag === 'select' || tag === 'textarea') return '';
    const text = clean(el.innerText || el.textContent);
    return text.length <= 60 ? text : text.slice(0, 57) + '...';
  };

  const describe = (el) => ({ css: cssPath(el), name: nameOf(el), role: roleOf(el), tag: el.tagName.toLowerCase() });
  const push = (event) => {
    if (!isOn()) return;
    event.t = Date.now();
    event.url = location.href;
    event.frame = framePath;
    events.push(event);
    save();
  };

  const interactive = 'a, button, input, select, textarea, label, [role=button], [role=link], [onclick]';
  document.addEventListener('click', (ev) => {
    const target = ev.target;
    const el = target && target.closest ? (target.closest(interactive) || target) : target;
    if (el && el.nodeType === 1) push(Object.assign({ type: 'click' }, describe(el)));
  }, true);

  const onInput = (ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    const tag = el.tagName.toLowerCase();
    if (!['input', 'textarea', 'select'].includes(tag)) return;
    const type = (el.getAttribute('type') || '').toLowerCase();
    // Passwords (and card numbers, one-time codes) are never read: the value is not even looked at.
    const secret = type === 'password' || /cc-|one-time-code/.test(el.getAttribute('autocomplete') || '');
    let value;
    if (secret) value = '[masked]';
    else if (type === 'checkbox' || type === 'radio') value = el.checked ? 'checked' : 'unchecked';
    else if (tag === 'select') value = el.options[el.selectedIndex] ? clean(el.options[el.selectedIndex].text) : '';
    else value = el.value;
    const info = describe(el);
    const last = events[events.length - 1];
    if (isOn() && last && last.type === 'input' && last.css === info.css && last.frame === framePath) {
      last.value = value;                         // still typing in the same field: keep ONE event for it
      last.t = Date.now();
      save();
      return;
    }
    push(Object.assign({ type: 'input', value, masked: secret }, info));
  };
  // Only "input" is listened to, not "change". Browsers fire "input" for every kind of control (text,
  // select, checkbox), while "change" can arrive late, when focus leaves a field: it would wrongly credit
  // the human with something the automation typed before they took over.
  document.addEventListener('input', onInput, true);
})()
"""
)

START_JS = "window.__bagHumanStart && window.__bagHumanStart()"
STOP_JS = "window.__bagHumanStop && window.__bagHumanStop()"
COLLECT_JS = "window.__bagHumanCollect ? window.__bagHumanCollect() : []"


class HumanRecorder:
    """Starts, collects and stops the recording through the Surface (never through Playwright)."""

    def __init__(self, surface, redactor: Redactor | None = None):
        self.surface = surface
        self.redactor = redactor or Redactor()

    def start(self) -> None:
        """Install the script everywhere (now and for pages loaded later) and begin recording fresh."""
        self.surface.add_init_script(HUMAN_RECORDER_JS)
        self.surface.evaluate_in_frames(START_JS)

    def collect(self) -> list[dict]:
        """Everything the human did, oldest first, with secrets removed and account numbers masked."""
        batches = self.surface.evaluate_in_frames(COLLECT_JS)
        events = sorted((event for batch in batches for event in (batch or [])), key=lambda event: event.get("t", 0))
        return self.redactor.data(events)

    def stop(self) -> None:
        """Stop recording and wipe what was stored. The script stays installed but does nothing."""
        self.surface.evaluate_in_frames(STOP_JS)


def summarize(events: list[dict], limit: int = 400) -> str:
    """One line for logs and for the AI: what the human did. Typed VALUES are deliberately left out."""
    steps = []
    for event in events:
        label = event.get("name") or event.get("css") or event.get("tag") or "?"
        if event.get("type") == "click":
            line = f'click {event.get("role", "")} "{label}"'.replace("  ", " ")
        else:
            line = f'type into "{label}"' + (" (masked)" if event.get("masked") else "")
        if not steps or steps[-1] != line:  # the same action twice in a row is shown once
            steps.append(line)
    text = "; ".join(steps) if steps else "nothing was recorded"
    return text if len(text) <= limit else text[: limit - 3] + "..."
