"""Handing the browser to a human, and taking it back.

This is the whole story of one takeover, in order:

    1. PAUSE      Something the automation cannot fix. State: AUTOMATION -> PAUSED_FOR_HUMAN.
                  A picture of the screen and an "intervention" file are written to
                  evidence/interventions/ and printed, so the human knows what went wrong.
    2. HAND OVER  The human recorder is switched on in every frame, the window is raised.
                  State: PAUSED_FOR_HUMAN -> HUMAN. The automation now only waits.
    3. WAIT       Until the person presses Enter in this terminal, or `bag resume` drops the
                  resume flag file. (Or gives up, the wait times out, or the window is closed.)
    4. COLLECT    What the human clicked and typed is read back out of the browser, logged and
                  saved. State: HUMAN -> AUTOMATION.

The caller (the replay engine or the discovery loop) then verifies that the page is in the
state it expects and carries on from the next step.
"""

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bag.safety import Redactor
from bag.surface import SurfaceError

from .human_recorder import HumanRecorder, summarize
from .resume import ABORT_WORDS, DEFAULT_DIR, flag_path, make_enter_checker
from .states import ControlController, ControlState, HandoffError

logger = logging.getLogger("bag.handoff")

POLL_SECONDS = 0.5  # how often the waiting run looks for Enter or the flag file


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Intervention:
    """What a human needs to know to help, and what happened. Written to <id>.json."""

    id: str
    path: Path
    kind: str  # "replay" or "discovery"
    label: str  # which run, e.g. "replay open-subaccount v1"
    goal: str  # what the run is trying to achieve
    step: int | None  # which step it was on (1-based)
    action: str | None  # what that step was doing
    error: str | None  # the kind of problem: LocatorNotFound, UnexpectedState, ...
    reason: str  # why the run stopped, in words
    expected: str | None
    observed: str | None
    url: str  # the page the browser was on
    created_at: str
    screenshot: Path | None = None
    status: str = "waiting"  # waiting -> resumed | aborted | timed_out | browser_closed; requested = saved for later, nobody waiting
    resumed_by: str | None = None  # "enter" or "flag"
    finished_at: str | None = None
    events_file: Path | None = None  # what the human did, as JSON
    event_count: int = 0
    summary: str = ""  # what the human did, in one line (typed values are never included)
    run_id: str | None = None

    def to_dict(self) -> dict:
        return {key: (str(value) if isinstance(value, Path) else value) for key, value in asdict(self).items()}


@dataclass
class TakeoverResult:
    aborted: bool  # True: the human gave up (or never answered): the run must end
    via: str  # enter | flag | abort | timeout | closed
    events: list  # what the human did, redacted
    summary: str
    intervention: Intervention


class HumanTakeover:
    def __init__(
        self,
        surface,
        *,
        directory=DEFAULT_DIR,
        redactor: Redactor | None = None,
        controller: ControlController | None = None,
        timeout_s: float | None = 1800,
        max_interventions: int = 3,
        poll_s: float = POLL_SECONDS,
        enter_check=None,
        clock=time.monotonic,
        sleep=None,
        out=print,
        require_headed: bool = True,
        run_log=None,
    ):
        # A human cannot work in a window they cannot see. A headless run that blocked forever waiting
        # for one would hang a scheduled job, so it is refused up front. (require_headed=False exists
        # only so automated tests can drive a headless browser.)
        if require_headed and getattr(surface, "headless", True):
            raise HandoffError("Human takeover needs a visible browser window. Run with --headed.")
        self.surface = surface
        self.directory = Path(directory)
        self.redactor = redactor or Redactor()
        self.run_log = run_log
        self._step: int | None = None
        self.controller = controller or ControlController(
            self.redactor, self.directory / "transitions.jsonl", sink=self._to_run_log
        )
        self.recorder = HumanRecorder(surface, self.redactor)
        self.timeout_s = timeout_s
        self.max_interventions = max_interventions  # per run: a step that keeps needing a human is a real problem
        self.poll_s = poll_s
        self.enter_check = enter_check or make_enter_checker()
        self.clock = clock
        self.sleep = sleep or surface.pause
        self.out = out

    # ----------------------------------------------------------------- the story

    def take_over(self, *, kind, goal, label, step, action, reason, error=None, expected=None, observed=None) -> TakeoverResult:
        """Pause, hand the browser to a human, wait, collect what they did. Returns when it is over."""
        # 1. PAUSE
        self._step = step
        intervention = self._new_intervention(kind, goal, label, step, action, reason, error, expected, observed)
        self.controller.transition(ControlState.PAUSED_FOR_HUMAN, f"{label}: {intervention.reason}", intervention.id)
        self._save(intervention)
        self._log("intervention", intervention.step, intervention=intervention.to_dict())
        self.out(json.dumps(intervention.to_dict(), indent=2, ensure_ascii=False))

        # 2. HAND OVER
        flag = flag_path(self.directory, intervention.id)
        flag.unlink(missing_ok=True)  # a flag left over from an earlier run must not resume this one
        try:
            self.recorder.start()
            self.surface.bring_to_front()
        except SurfaceError as error_:
            return self._finish(intervention, "closed", [], f"The browser could not be handed over: {error_}")
        self.controller.transition(ControlState.HUMAN, "the human has the browser; recording their actions", intervention.id)
        self.out(
            "\n=== HUMAN TAKEOVER ===\n"
            "Automation is paused and the browser is yours.\n"
            "  1. Do what the step needed, in the browser window.\n"
            f"  2. Then press Enter here, or run `bag resume {intervention.id}` in another terminal.\n"
            "  Type q and press Enter to give up on this run."
        )

        # 3. WAIT, 4. COLLECT
        how = self._wait(flag)
        try:
            events = self.recorder.collect()
        except SurfaceError:  # the window was closed: there is nothing left to read
            events = []
        try:
            self.recorder.stop()
        except SurfaceError:
            pass
        return self._finish(intervention, how, events)

    def request_help(self, *, kind, goal, label, step, action, reason, error=None, expected=None, observed=None) -> Intervention:
        self._step = step
        intervention = self._new_intervention(kind, goal, label, step, action, reason, error, expected, observed)
        self.controller.transition(ControlState.PAUSED_FOR_HUMAN, f"{label}: {intervention.reason}", intervention.id)
        intervention.status = "requested"
        self._save(intervention)
        self._log("intervention", step, intervention=intervention.to_dict())
        self.out(json.dumps(intervention.to_dict(), indent=2, ensure_ascii=False))
        self.out(f"Human help requested. Nobody is waiting live: the request is saved in {intervention.path}.")
        return intervention

    # ---------------------------------------------------------------- the pieces

    def _wait(self, flag: Path) -> str:
        """Block until Enter, the flag file, a give-up, the timeout, or a closed window."""
        deadline = self.clock() + self.timeout_s if self.timeout_s else None
        while True:
            if flag.exists():
                flag.unlink(missing_ok=True)  # used up: it must not resume the next pause too
                return "flag"
            line = self.enter_check()
            if line is not None:
                return "abort" if line.strip().lower() in ABORT_WORDS else "enter"
            if deadline is not None and self.clock() >= deadline:
                return "timeout"
            try:
                self.sleep(self.poll_s)  # keeps the browser responsive while the human works
            except SurfaceError:
                return "closed"

    def _finish(self, intervention: Intervention, how: str, events: list, problem: str | None = None) -> TakeoverResult:
        for event in events:
            logger.info("human event: %s", json.dumps(event, ensure_ascii=False))  # already redacted
        summary = summarize(events)
        if events:
            events_file = self.directory / f"{intervention.id}.human-events.json"
            events_file.write_text(json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8")
            intervention.events_file = events_file
        intervention.event_count = len(events)
        intervention.summary = self.redactor.text(summary)
        intervention.finished_at = _now()
        resumed = how in ("enter", "flag")
        intervention.status = {"enter": "resumed", "flag": "resumed", "abort": "aborted",
                               "timeout": "timed_out", "closed": "browser_closed"}[how]
        intervention.resumed_by = how if resumed else None
        self._save(intervention)
        self._log(
            "finished", intervention.step, intervention=intervention.id, status=intervention.status, via=how,
            event_count=len(events), summary=intervention.summary, events=events,
        )

        if resumed:
            self.controller.transition(
                ControlState.AUTOMATION, f"the human finished (via {how}); {len(events)} action(s) recorded", intervention.id)
            self.out(f"Resuming automation. The human did: {intervention.summary}")
        else:
            reason = problem or {"abort": "the human gave up", "timeout": "no answer from the human in time",
                                 "closed": "the browser window was closed"}[how]
            self.controller.note("gave_up", reason, intervention.id)
            self.out(f"Not resuming: {reason}.")
        return TakeoverResult(not resumed, how, events, intervention.summary, intervention)

    def _new_intervention(self, kind, goal, label, step, action, reason, error, expected, observed) -> Intervention:
        self.directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:30]
        base = f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{slug}" + (f"-step{step}" if step else "")
        name, counter = base, 1
        while (self.directory / f"{name}.json").exists():
            counter += 1
            name = f"{base}-{counter}"
        clean = self.redactor.text  # every field a person will read is scrubbed first

        try:
            observation = self.surface.observe()  # the screenshot comes back already blurred
            url = observation.url
            if self.run_log is not None:
                screenshot = self.run_log.screenshot(f"handoff-step{step}" if step else "handoff", observation.screenshot)
            else:
                screenshot = self.directory / f"{name}.png"
                screenshot.write_bytes(observation.screenshot)
        except (SurfaceError, OSError):
            url, screenshot = "(unknown)", None
        return Intervention(
            id=name, path=self.directory / f"{name}.json", kind=kind, label=clean(label), goal=clean(goal), step=step,
            action=action, error=error, reason=clean(reason), expected=clean(expected) if expected else None,
            observed=clean(observed) if observed else None, url=clean(url), created_at=_now(), screenshot=screenshot,
            run_id=self.run_log.run_id if self.run_log is not None else None,
        )

    def _log(self, event: str, step: int | None, **details) -> None:
        if self.run_log is not None:
            self.run_log.step("handoff", event, step=step, **details)

    def _to_run_log(self, record: dict) -> None:
        event = record.get("event", record["kind"])
        details = {key: value for key, value in record.items() if key not in ("kind", "time", "event")}
        self._log(event, self._step, **details)

    def _save(self, intervention: Intervention) -> None:
        temp = intervention.path.with_suffix(".tmp")  # write, then swap: a reader never sees half a file
        temp.write_text(json.dumps(intervention.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(intervention.path)
