"""The guard: looks at every action BEFORE it touches the bank and answers

    ALLOW           go ahead
    NEEDS_APPROVAL  a human must say yes first
    BLOCK           never, whatever anyone says

The rules come from config/safety.yaml. The guard works on duck-typed actions (anything
with .action and optionally .text), so the discovery loop's Action and the replay
engine's Step both pass through the same checks. It must not import bag.surface.
"""

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from .audit import AuditLog
from .config import SafetyConfig
from .redact import Redactor

DEFAULT_PORTS = {"http": 80, "https": 443}
NO_TOUCH_ACTIONS = {"done", "ask_human"}  # these end a run; they never touch the page


class Decision(str, Enum):
    ALLOW = "ALLOW"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""
    rule: str = ""  # which rule decided: "allowed_urls", "risky_button_names" or a risky_actions name
    approved_by: str | None = None  # "human" once someone said yes to a NEEDS_APPROVAL

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


@dataclass
class PageContext:
    """What the guard knows about the moment an action is about to happen."""

    url: str  # the page the browser is on
    frame_urls: Sequence[str] = ()  # pages inside iframes (a bank page can hide another site in a frame)
    targets: Sequence = ()  # every known description of the element: role+name, label, text, css


@dataclass
class ApprovalRequest:
    """What a human is shown. Every string is already redacted."""

    run: str
    step: int | None
    action: str
    url: str
    reason: str
    rule: str


def _target_strings(target) -> list[str]:
    """The words a Target is made of (duck-typed so this module never imports bag.surface)."""
    return [str(value) for name in ("name", "label", "text", "css") if (value := getattr(target, name, None))]


def describe_action(action, context: PageContext) -> str:
    parts = [str(getattr(action, "action", "?"))]
    if context.targets:
        parts.append(str(context.targets[0]))
    text = getattr(action, "text", None)
    if text is not None:
        parts.append(f"text={text!r}")  # a placeholder such as {{member_id}}, never a real value
    return " ".join(parts)


def terminal_approver(request: ApprovalRequest, *, input_fn=input, interactive: bool | None = None, write=print) -> bool:
    """Ask the person at the terminal. With nobody there to ask, the answer is no."""
    if interactive is None:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    write("\n=== APPROVAL NEEDED ===")
    write(f"  Run:    {request.run}" + (f", step {request.step}" if request.step else ""))
    write(f"  Action: {request.action}")
    write(f"  Page:   {request.url}")
    write(f"  Why:    {request.reason}")
    if not interactive:
        write("  There is no terminal to ask on, so this action is DENIED.")
        return False
    try:
        answer = input_fn("Approve this action? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


class Guard:
    def __init__(
        self,
        config: SafetyConfig,
        approver=terminal_approver,
        audit: AuditLog | None = None,
        redactor: Redactor | None = None,
    ):
        self.config = config
        self.approver = approver
        self.audit = audit
        self.redactor = redactor or Redactor()
        self._origins = [self._parse(url) for url in config.allowed_urls]
        # Whole-word, case-insensitive: "pay" matches "Pay now" but not "Payments" or "Repay".
        self._button_patterns = [
            (re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE), name) for name in config.risky_button_names
        ]

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _parse(url: str):
        parsed = urlparse(url)
        port = parsed.port or DEFAULT_PORTS[parsed.scheme]
        return parsed.scheme, parsed.hostname.lower(), port, parsed.path.rstrip("/")

    def _url_allowed(self, url: str, *, frame: bool = False) -> bool:
        if frame and (url == "" or url.startswith("about:")):
            # An empty frame (about:blank), or one that has been created but has not started loading
            # (the browser then reports its address as ""), shows nothing from anywhere. Once it loads a
            # real page its address is real, and the next action checks that. Treating "" as foreign made
            # a perfectly normal page load look like an attack. (The MAIN page is never given this pass.)
            return True
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        if parsed.scheme not in DEFAULT_PORTS or not parsed.hostname:
            return False  # file:, javascript:, data: and friends
        path = parsed.path or "/"
        for scheme, host, allowed_port, allowed_path in self._origins:
            if (parsed.scheme, parsed.hostname.lower(), port or DEFAULT_PORTS[parsed.scheme]) != (scheme, host, allowed_port):
                continue
            if allowed_path == "" or path == allowed_path or path.startswith(allowed_path + "/"):
                return True
        return False

    def check_url(self, url: str) -> Verdict:
        """Is the browser allowed to go here at all? Used before navigating to a start page."""
        if self._url_allowed(url):
            return Verdict(Decision.ALLOW)
        return Verdict(Decision.BLOCK, f"{url} is not on the allowlist.", rule="allowed_urls")

    def check(self, action, context: PageContext) -> Verdict:
        """Decide about one action. Pure: no prompting, no logging, no side effects."""
        kind = getattr(action, "action", None)
        if kind in NO_TOUCH_ACTIONS:
            return Verdict(Decision.ALLOW)

        # 1. The browser must be on an allowed site, including inside every iframe.
        if not self._url_allowed(context.url):
            return Verdict(Decision.BLOCK, f"The page {context.url!r} is not on the allowlist.", rule="allowed_urls")
        for frame_url in context.frame_urls:
            if not self._url_allowed(frame_url, frame=True):
                return Verdict(Decision.BLOCK, f"A frame on the page loads {frame_url!r}, which is not on the allowlist.",
                               rule="allowed_urls")

        # 2. Risky kinds of action need a human's yes.
        text = getattr(action, "text", None) or ""
        everything = " ".join(s for target in context.targets for s in _target_strings(target))
        reasons = []
        for rule in self.config.risky_actions:
            if rule.action != kind:
                continue
            if rule.text_matches and not re.search(rule.text_matches, text, re.IGNORECASE):
                continue
            if rule.target_matches and not re.search(rule.target_matches, everything, re.IGNORECASE):
                continue
            reasons.append((rule.name, rule.name))

        # 3. A click on a risky-looking button needs a human's yes.
        if kind == "click":
            for pattern, name in self._button_patterns:
                if any(pattern.search(s) for target in context.targets for s in _target_strings(target)):
                    reasons.append((f"Clicking something named like '{name}'", "risky_button_names"))
                    break

        if reasons:
            return Verdict(Decision.NEEDS_APPROVAL, "; ".join(reason for reason, _ in reasons), rule=reasons[0][1])
        return Verdict(Decision.ALLOW)

    # ------------------------------------------------------- decide and record

    def authorize(self, action, context: PageContext, *, run: str = "", step: int | None = None) -> Verdict:
        """check(), then ask a human if needed, then write the audit log. Returns the FINAL verdict.

        The result is ALLOW or BLOCK. NEEDS_APPROVAL never comes back: it becomes ALLOW (a human
        said yes) or BLOCK (a human said no, or there was nobody to ask).
        """
        first = self.check(action, context)
        final = first
        if first.decision is Decision.NEEDS_APPROVAL:
            request = ApprovalRequest(
                run=run, step=step, action=self.redactor.text(describe_action(action, context)),
                url=self.redactor.text(context.url), reason=self.redactor.text(first.reason), rule=first.rule,
            )
            if self.approver(request):
                final = Verdict(Decision.ALLOW, first.reason, first.rule, approved_by="human")
            else:
                final = Verdict(Decision.BLOCK, f"A human did not approve: {first.reason}", first.rule)
        self._record(action, context, first, final, run, step)
        return final

    def _record(self, action, context, first: Verdict, final: Verdict, run, step) -> None:
        if self.audit is None:
            return
        self.audit.write({
            "run": run, "step": step, "action": describe_action(action, context), "url": context.url,
            "checked": first.decision.value, "final": final.decision.value, "rule": first.rule,
            "reason": first.reason, "approved_by": final.approved_by,
        })


def check(action, context: PageContext, config: SafetyConfig) -> Verdict:
    """guard.check(action, page_context): the pure decision, for callers that only need ALLOW / NEEDS_APPROVAL / BLOCK."""
    return Guard(config).check(action, context)
