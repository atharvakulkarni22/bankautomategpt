"""Guards and approval gates for risky actions, plus redaction of sensitive data.

Must not import bag.surface (the surface imports bag.safety.redact).
"""

from .audit import AuditLog
from .config import DEFAULT_CONFIG_PATH, ActionRule, SafetyConfig, SafetyConfigError, load_safety_config
from .guard import (
    ApprovalRequest,
    Decision,
    Guard,
    PageContext,
    Verdict,
    check,
    describe_action,
    terminal_approver,
)
from .redact import Redactor, blur_boxes, has_account_number, mask_account_numbers

__all__ = [
    "ActionRule",
    "ApprovalRequest",
    "AuditLog",
    "DEFAULT_CONFIG_PATH",
    "Decision",
    "Guard",
    "PageContext",
    "Redactor",
    "SafetyConfig",
    "SafetyConfigError",
    "Verdict",
    "blur_boxes",
    "check",
    "describe_action",
    "has_account_number",
    "load_safety_config",
    "mask_account_numbers",
    "terminal_approver",
]
