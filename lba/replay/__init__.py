"""Runs an artifact deterministically, without an LLM.

This package must never import lba.llm or lba.agent (a test enforces it).
"""

from .engine import (
    BUSINESS_OUTCOME,
    FAILURE,
    SUCCESS,
    Failure,
    LogEntry,
    Replayer,
    RunResult,
    prepare_run,
    resolve_start_url,
)
from .errors import (
    BusinessOutcome,
    LocatorNotFound,
    ReplayError,
    ReplayRefused,
    SafetyBlocked,
    TransientError,
    UnexpectedState,
    classify,
)

__all__ = [
    "BUSINESS_OUTCOME",
    "BusinessOutcome",
    "FAILURE",
    "Failure",
    "LocatorNotFound",
    "LogEntry",
    "ReplayError",
    "ReplayRefused",
    "Replayer",
    "RunResult",
    "SUCCESS",
    "SafetyBlocked",
    "TransientError",
    "UnexpectedState",
    "classify",
    "prepare_run",
    "resolve_start_url",
]
