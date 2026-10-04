"""Who is in control of the browser: the automation, or a human.

There are exactly three states and exactly three legal moves between them:

    AUTOMATION --(something went wrong)--> PAUSED_FOR_HUMAN
    PAUSED_FOR_HUMAN --(the human is given the browser)--> HUMAN
    HUMAN --(the human is done, press Enter)--> AUTOMATION

Anything else (say, AUTOMATION straight to HUMAN) is a bug, and raises. Every move is
logged, so afterwards there is a record of exactly when the machine stopped and when it
took over again.

If the human gives up instead of resuming, the run simply ends. That is not a state change:
it is logged as an event, and the controller stays in HUMAN.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from bag.safety import Redactor

logger = logging.getLogger("bag.handoff")


class HandoffError(Exception):
    """Something is wrong with the handoff itself (not with the bank)."""


class InvalidTransition(HandoffError):
    """A move between control states that is not allowed."""


class ControlState(str, Enum):
    AUTOMATION = "AUTOMATION"
    PAUSED_FOR_HUMAN = "PAUSED_FOR_HUMAN"
    HUMAN = "HUMAN"


ALLOWED = {
    ControlState.AUTOMATION: {ControlState.PAUSED_FOR_HUMAN},
    ControlState.PAUSED_FOR_HUMAN: {ControlState.HUMAN},
    ControlState.HUMAN: {ControlState.AUTOMATION},
}


@dataclass(frozen=True)
class Transition:
    time: str
    source: ControlState
    target: ControlState
    reason: str
    intervention: str | None = None  # which intervention this belongs to


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ControlController:
    def __init__(self, redactor: Redactor | None = None, log_path=None):
        self.state = ControlState.AUTOMATION
        self.history: list[Transition] = []
        self.redactor = redactor or Redactor()
        self.log_path = Path(log_path) if log_path else None  # one JSON line per transition, if given

    def transition(self, target: ControlState, reason: str, intervention: str | None = None) -> Transition:
        """Move to a new state, or raise InvalidTransition (and stay where we are)."""
        if target not in ALLOWED[self.state]:
            raise InvalidTransition(f"Cannot go from {self.state.value} to {target.value}.")
        reason = self.redactor.text(reason)
        move = Transition(_now(), self.state, target, reason, intervention)
        self.state = target
        self.history.append(move)
        logger.info("control %s -> %s: %s", move.source.value, move.target.value, reason)
        self._write({"kind": "transition", "time": move.time, "from": move.source.value,
                     "to": move.target.value, "reason": reason, "intervention": intervention})
        return move

    def note(self, event: str, reason: str, intervention: str | None = None) -> None:
        """Log something that is not a state change (for example: the human gave up)."""
        reason = self.redactor.text(reason)
        logger.info("control %s (%s): %s", event, self.state.value, reason)
        self._write({"kind": "event", "time": _now(), "event": event, "state": self.state.value,
                     "reason": reason, "intervention": intervention})

    def _write(self, record: dict) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
