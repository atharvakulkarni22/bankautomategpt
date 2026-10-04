"""Guards and approval gates for risky actions."""

from .checks import Verdict, check

__all__ = ["Verdict", "check"]
