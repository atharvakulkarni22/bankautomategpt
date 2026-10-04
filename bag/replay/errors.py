"""What can go wrong during a replay, sorted into kinds.

The kind decides what happens next:

    TransientError    "try again in a moment" (slow page, element not ready) -> retried
    LocatorNotFound   none of the ways we know to find the element worked    -> run fails
    BusinessOutcome   the app gave a normal answer we expected (No member found)
                      -> the run ends, but this is NOT a failure
    SafetyBlocked     a safety rule said no                                  -> run fails
    UnexpectedState   anything else that does not match what we expected     -> run fails
"""

from bag.surface import AmbiguousTarget, SurfaceError, SurfaceTimeout, TargetNotFound


class ReplayRefused(Exception):
    """The run was refused before it started (not approved, bad input, missing secret).

    Nothing was touched in the app. This is a mistake by the caller, not a failed run.
    """


class ReplayError(Exception):
    """Base for everything that can go wrong once a run has started."""

    def __init__(self, message: str, *, expected: str | None = None, observed: str | None = None):
        super().__init__(message)
        self.expected = expected  # what we expected to find or happen
        self.observed = observed  # what we actually saw


class TransientError(ReplayError):
    """Probably temporary: a timeout, or an element that was not ready yet."""


class LocatorNotFound(ReplayError):
    """The primary locator and every fallback failed to find the element."""


class BusinessOutcome(ReplayError):
    """The app answered normally with a known result, such as NOT_FOUND."""

    def __init__(self, code: str, message: str, *, observed: str | None = None):
        super().__init__(message, expected="the normal path", observed=observed)
        self.code = code


class SafetyBlocked(ReplayError):
    """A safety check refused to let a step run."""


class UnexpectedState(ReplayError):
    """The app is not in the state the artifact expects."""


def classify(error: Exception) -> ReplayError:
    """Turn an exception from the surface into one of the kinds above.

    Anything that is not a surface or replay error is a bug in our own code, so it
    is re-raised unchanged instead of being hidden as a "failed run".
    """
    if isinstance(error, ReplayError):
        return error
    if isinstance(error, SurfaceTimeout):
        return TransientError(str(error), observed=str(error))
    if isinstance(error, (TargetNotFound, AmbiguousTarget)):
        return LocatorNotFound(str(error), observed=str(error))
    if isinstance(error, SurfaceError):
        return UnexpectedState(str(error), observed=str(error))
    raise error
