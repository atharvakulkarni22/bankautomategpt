"""Safety checks that run on every action before it touches the app.

This is a stub: it allows everything. Later it will block or pause risky steps
(for example money movements) and require human approval.
"""

from dataclasses import dataclass


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""


def check(action) -> Verdict:
    """Decide whether `action` may run. Currently always yes."""
    return Verdict(allowed=True)
