"""The replay engine: follows an approved artifact step by step. No LLM, ever.

Nothing here is clever. Every decision was made earlier, by the agent and by the
human who approved the artifact. The engine just carries the steps out carefully,
and when something is off it stops and says exactly what and where.

Before every action the guard decides ALLOW / NEEDS_APPROVAL / BLOCK, and everything
written to the log or to a failure report passes through the redactor first.

Retries and fallbacks are different things:

    FALLBACK  Same moment, same action, DIFFERENT way of finding the element.
              For when our description of the element went stale (a label changed,
              the layout moved). Tried in order on every look: primary first.
    RETRY     LATER, same locator, same action, once more after a short wait.
              For when the app was just slow or the element not ready yet.
              Only for TransientError, at most twice, waiting longer each time.
"""

import logging
import time
from types import SimpleNamespace
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from bag.artifact import Artifact, Locator, Step
from bag.safety import Decision, PageContext, Redactor
from bag.surface import Surface, SurfaceError, Target, TargetNotFound
from bag.surface.placeholders import PLACEHOLDER

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

logger = logging.getLogger("bag.replay")

SUCCESS = "SUCCESS"
BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
FAILURE = "FAILURE"

POLL_SECONDS = 0.2  # how often we look again while waiting for something
DEFAULT_SCREENSHOT_DIR = Path("evidence/screenshots")


# ------------------------------------------------------------------- results


@dataclass
class LogEntry:
    step: int | None  # step number (1-based); None for things before or after the steps
    kind: str  # step | fallback | retry | interruption
    message: str


@dataclass
class Failure:
    step: int | None  # which step failed (1-based); None if it was before or after the steps
    phase: str  # start | step | finish
    action: str | None  # what the step was doing (click, type, ...)
    error: str  # TransientError, LocatorNotFound, SafetyBlocked, UnexpectedState
    message: str
    expected: str | None = None
    observed: str | None = None
    screenshot: Path | None = None


@dataclass
class RunResult:
    status: str  # SUCCESS | BUSINESS_OUTCOME | FAILURE
    artifact: str  # "name vN"
    outputs: dict[str, object] = field(default_factory=dict)  # SUCCESS: cast to their types
    outcome_code: str | None = None  # BUSINESS_OUTCOME: e.g. NOT_FOUND
    message: str = ""  # BUSINESS_OUTCOME: the text that was seen
    failure: Failure | None = None  # FAILURE: where and why
    log: list[LogEntry] = field(default_factory=list)
    seconds: float = 0.0
    interventions: list = field(default_factory=list)  # every time a human took over (bag.handoff.Intervention)


# -------------------------------------------------------------- before a run


def prepare_run(artifact: Artifact, raw_inputs: Mapping[str, str], available_secrets: Iterable[str]) -> dict[str, str]:
    """Refuse to start unless everything is in order. Returns the checked inputs.

    Raises ReplayRefused (before the app is touched) if the artifact is not approved,
    an input is missing, unknown or invalid, or a secret the steps need is not set.
    Messages never repeat input values.
    """
    meta = artifact.metadata
    if meta.status != "approved":
        raise ReplayRefused(
            f"{meta.name} v{meta.version} is '{meta.status}', not approved. "
            f"Read it through, then run `bag approve {meta.name}`."
        )

    declared = {i.name: i for i in artifact.inputs}
    unknown = sorted(set(raw_inputs) - set(declared))
    if unknown:
        raise ReplayRefused(f"Unknown input(s): {', '.join(unknown)}. This artifact takes: {', '.join(declared) or 'none'}.")
    missing = sorted(set(declared) - set(raw_inputs))
    if missing:
        raise ReplayRefused(f"Missing input(s): {', '.join(missing)}. Pass them with --input name=value.")
    clean = {}
    for name, value in raw_inputs.items():
        try:
            clean[name] = declared[name].check(value)
        except ValueError as error:
            raise ReplayRefused(str(error)) from error

    available = set(available_secrets)
    needed = {m.group(2) for step in artifact.steps for m in PLACEHOLDER.finditer(step.text or "") if m.group(1)}
    absent = sorted(needed - available)
    if absent:
        raise ReplayRefused(
            f"Secret(s) not available: {', '.join(absent)}. Set them in .env (and list them in BAG_SECRET_NAMES if custom)."
        )
    return clean


def resolve_start_url(artifact_url: str, base_url: str | None) -> str:
    """The artifact remembers where the app was when it was recorded. BANK_URL says where it is now."""
    if not base_url:
        return artifact_url
    recorded, now = urlparse(artifact_url), urlparse(base_url)
    return urlunparse(recorded._replace(scheme=now.scheme, netloc=now.netloc))


# ----------------------------------------------------------------- the engine


class Replayer:
    """Runs one artifact once. Create a new Replayer for each run.

    `values` must be the same Values the surface was built with: the surface fills in
    {{placeholders}} just before typing, and this class checks the secrets exist.
    """

    def __init__(
        self,
        artifact: Artifact,
        surface: Surface,
        *,
        guard,
        redactor: Redactor | None = None,
        handoff=None,
        start_url: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        backoff_s: float = 0.5,
        max_interruptions: int = 10,
        clock=time.monotonic,
        sleep=None,
        screenshot_dir: Path = DEFAULT_SCREENSHOT_DIR,
    ):
        self.artifact = artifact
        self.surface = surface
        self.start_url = start_url or artifact.metadata.start_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.max_interruptions = max_interruptions
        # The guard (bag.safety.Guard) rules on every action. There is no default: a replay
        # without a guard must be a loud mistake, not a quiet one.
        self.guard = guard
        self.redactor = redactor or Redactor()  # scrubs every log line and failure report
        # Optional: a bag.handoff.HumanTakeover. Without one, a stuck step simply fails the run.
        self.handoff = handoff
        self.interventions: list = []
        self._handovers = 0
        self.clock = clock
        self.sleep = sleep or surface.pause
        self.screenshot_dir = Path(screenshot_dir)

        self.log: list[LogEntry] = []
        self.raw_outputs: dict[str, str] = {}
        self._interruptions_handled = 0
        self._step_number: int | None = None
        self._phase = "start"
        self._action: str | None = None

    # ------------------------------------------------------------------- run

    def run(self) -> RunResult:
        started = self.clock()
        name = f"{self.artifact.metadata.name} v{self.artifact.metadata.version}"
        try:
            start = self.guard.check_url(self.start_url)  # the browser may only start on an allowed site
            if start.decision is not Decision.ALLOW:
                raise SafetyBlocked("The start page was blocked by a safety rule.",
                                    expected="a start page on the allowlist", observed=start.reason)
            self.surface.goto(self.start_url)
            self._run_steps()
            self._phase, self._step_number, self._action = "finish", None, None
            self._watch(None)  # a popup or "No member found" may appear after the last step
            self._success_check()
            outputs = self._cast_outputs()
        except BusinessOutcome as outcome:
            self._log(self._step_number, "step", f"ended with business outcome {outcome.code}")
            return RunResult(BUSINESS_OUTCOME, name, outcome_code=outcome.code, message=self.redactor.text(str(outcome)),
                             log=self.log, seconds=self.clock() - started, interventions=self.interventions)
        except (SurfaceError, ReplayError) as error:
            problem = classify(error)
            return RunResult(FAILURE, name, failure=self._failure(problem), log=self.log, seconds=self.clock() - started,
                             interventions=self.interventions)
        return RunResult(SUCCESS, name, outputs=outputs, log=self.log, seconds=self.clock() - started,
                         interventions=self.interventions)

    # ----------------------------------------------------------------- steps

    def _run_steps(self) -> None:
        """Run every step. If a human can be called in, a stuck step is handed to them.

        Only LocatorNotFound and UnexpectedState are handed over: those mean "the page is not
        what the artifact expects", which a person can sort out. A safety block, a business
        outcome or a retried-out timeout are not things to hand to a human.
        """
        steps = self.artifact.steps
        number = 1
        while number <= len(steps):
            step = steps[number - 1]
            self._phase, self._step_number, self._action = "step", number, step.action
            try:
                self._run_step(number, step)
                number += 1
                continue
            except (LocatorNotFound, UnexpectedState) as problem:
                pending = problem

            while True:  # a human takes over; repeat if the page is still not right afterwards
                if not self._take_over(number, step, pending):
                    raise pending
                self._phase, self._step_number, self._action = "step", number, step.action
                if step.action == "read":
                    break  # the human cannot hand us a value, so read it again now that they have fixed the page
                try:
                    self._verify_expected(number, step)  # did the human really get the page to where the step leads?
                except UnexpectedState as again:
                    pending = again
                    continue
                number += 1  # the human did this step for us: carry on with the next one
                break

    def _take_over(self, number: int, step: Step, problem: ReplayError) -> bool:
        """Hand the browser to a human. Returns True if they finished and the run may continue."""
        if self.handoff is None or self._handovers >= self.handoff.max_interventions:
            return False
        self._handovers += 1
        meta = self.artifact.metadata
        result = self.handoff.take_over(
            kind="replay", goal=meta.description or meta.name, label=f"replay {meta.name} v{meta.version}",
            step=number, action=step.action, error=type(problem).__name__, reason=str(problem),
            expected=problem.expected, observed=problem.observed,
        )
        self.interventions.append(result.intervention)
        if result.aborted:
            self._log(number, "handoff", f"The human did not resume ({result.via}); the run ends here.")
            return False
        self._log(number, "handoff", f"A human took over (resumed via {result.via}) and did: {result.summary}")
        return True

    def _run_step(self, number: int, step: Step) -> None:
        self._authorize(number, step, step.locator)

        retries = 0
        while True:
            try:
                self._watch(number)
                target, matched = self._locate(number, step.locator, f"step {number} ({step.action})")
                self._act(step, target)
                self._verify_expected(number, step)
                again = f" after {retries} retr{'y' if retries == 1 else 'ies'}" if retries else ""
                self._log(number, "step", f"{step.action} ok via {matched}{again}")
                return
            except (SurfaceError, ReplayError) as error:
                problem = classify(error)
                if isinstance(problem, TransientError) and retries < self.max_retries:
                    delay = self.backoff_s * (2 ** retries)  # 0.5 s, then 1 s
                    self._log(number, "retry", f"{problem} Retrying in {delay:g}s ({retries + 1} of {self.max_retries}).")
                    self.sleep(delay)
                    retries += 1
                    continue
                if isinstance(problem, LocatorNotFound) and self._watch(number):
                    continue  # a popup was in the way: it is gone now, so look again
                if problem is error:
                    raise
                raise problem from error

    def _authorize(self, number: int | None, action, locator: Locator, what: str | None = None) -> None:
        """Ask the guard whether this action may happen. Raises SafetyBlocked unless it says ALLOW."""
        context = PageContext(
            url=self.surface.current_url(),
            frame_urls=self.surface.frame_urls() if hasattr(self.surface, "frame_urls") else [],
            targets=locator.ordered(),  # every way we know to describe the element, so its name is checked too
        )
        meta = self.artifact.metadata
        verdict = self.guard.authorize(action, context, run=f"replay {meta.name} v{meta.version}", step=number)
        if verdict.decision is not Decision.ALLOW:
            where = what or f"Step {number} ({action.action})"
            raise SafetyBlocked(f"{where} was blocked by a safety rule.",
                                expected="the action to be allowed", observed=verdict.reason or "blocked")

    def _act(self, step: Step, target: Target) -> None:
        # A click that times out did not happen: Playwright times out while waiting for the
        # element to be ready, before it clicks. That is why retrying a click is safe.
        if step.action == "click":
            self.surface.click(target)
        elif step.action == "type":
            self.surface.type(target, step.text)  # still holds {{placeholders}}; the surface fills them in
        elif step.action == "read":
            self.raw_outputs[step.output_name] = self.surface.read(target)
        else:  # wait
            self.surface.wait_for(target, timeout_ms=int(self.timeout_s * 1000))

    def _locate(self, number: int | None, locator: Locator, what: str, *, watch: bool = True) -> tuple[Target, str]:
        """Find the element: primary first, then each fallback, looking again until the timeout."""
        targets = locator.ordered()
        deadline = self.clock() + self.timeout_s
        while True:
            try:
                index = self.surface.locate(targets)
            except TargetNotFound as error:
                if self.clock() >= deadline:
                    raise LocatorNotFound(
                        f"Could not find the element for {what}.",
                        expected=f"an element matching {targets[0]} (or one of {len(targets) - 1} fallback(s))",
                        observed=str(error),
                    ) from error
                if watch and self._watch(number):  # a popup may be hiding things; it has been dismissed
                    continue
                self.sleep(POLL_SECONDS)
                continue
            if index == 0:
                return targets[0], "primary"
            self._log(number, "fallback", f"{what}: the primary locator failed; fallback {index} matched ({targets[index]}).")
            logger.warning("fallback %s used for %s", index, what)
            return targets[index], f"fallback {index}"

    # ------------------------------------------------- interruptions, outcomes

    def _watch(self, number: int | None) -> bool:
        """Dismiss any known interruption, then stop if a known outcome is showing.

        Returns True if an interruption was dismissed. Raises BusinessOutcome if the
        page shows an outcome text such as "No member found".
        """
        dismissed = False
        for _ in range(3):  # a second popup can appear behind the first
            hit = next((i for i in self.artifact.known_interruptions if self.surface.is_visible(Target(text=i.text))), None)
            if hit is None:
                break
            self._interruptions_handled += 1
            if self._interruptions_handled > self.max_interruptions:
                raise UnexpectedState(
                    f"The interruption '{hit.text}' keeps coming back.",
                    expected="the interruption to stay dismissed", observed=f"dismissed {self.max_interruptions} times already",
                )
            target, matched = self._locate(number, hit.locator, f"dismissing '{hit.text}'", watch=False)
            # Dismissing a popup is a click like any other, so the guard rules on it too.
            during = f" during step {number}" if number else " after the last step"
            self._authorize(number, SimpleNamespace(action="click", text=None), hit.locator,
                            what=f"Dismissing '{hit.text}'{during}")
            self.surface.click(target)
            self.surface.pause(POLL_SECONDS)
            self._log(number, "interruption", f"'{hit.text}' was showing; clicked {target} ({matched}).")
            dismissed = True

        for outcome in self.artifact.known_outcomes:
            if self.surface.is_visible(Target(text=outcome.text)):
                raise BusinessOutcome(outcome.outcome, f"The page says '{outcome.text}'.", observed=outcome.text)
        return dismissed

    # ------------------------------------------------------ expected, success

    def _verify_expected(self, number: int, step: Step) -> None:
        if step.expected is None:
            return
        deadline = self.clock() + self.timeout_s
        while True:
            problem = self._expectation_problem(step.expected.url_contains, step.expected.text_visible)
            if problem is None:
                return
            if self.clock() >= deadline:
                expected, observed = problem
                raise UnexpectedState(f"After step {number} ({step.action}) the app is not in the expected state.",
                                      expected=expected, observed=observed)
            if not self._watch(number):  # a popup may be what is in the way
                self.sleep(POLL_SECONDS)

    def _expectation_problem(self, url_contains: str | None, text_visible: str | None) -> tuple[str, str] | None:
        if url_contains is not None:
            url = self.surface.current_url()
            if url_contains not in url:
                return f"the page address to contain '{url_contains}'", f"the address is {url}"
        if text_visible is not None and not self.surface.is_visible(Target(text=text_visible)):
            return f"the text '{text_visible}' to be visible", "that text is not visible"
        return None

    def _success_check(self) -> None:
        check = self.artifact.success_check
        for name in check.outputs_present:
            if not self.raw_outputs.get(name, "").strip():
                raise UnexpectedState(f"The success check failed: output '{name}' is missing.",
                                      expected=f"output '{name}' to be read and not empty", observed="nothing was read")
        if check.text_visible is not None:
            deadline = self.clock() + self.timeout_s
            while self._expectation_problem(None, check.text_visible):
                if self.clock() >= deadline:
                    raise UnexpectedState("The success check failed.", expected=f"the text '{check.text_visible}' to be visible",
                                          observed="that text is not visible")
                self.sleep(POLL_SECONDS)

    def _cast_outputs(self) -> dict[str, object]:
        """Clean up each value read from the page and turn it into its declared type."""
        outputs: dict[str, object] = {}
        for output in self.artifact.outputs:
            try:
                clean = output.parse(self.raw_outputs[output.name])
            except (ValueError, KeyError) as error:
                # The text read is customer data, so it is deliberately not repeated here.
                raise UnexpectedState(f"Output '{output.name}' could not be read as a {output.type}.",
                                      expected=f"output '{output.name}' to be a {output.type}",
                                      observed="a value that does not fit (not shown: customer data)") from error
            outputs[output.name] = {"int": int, "decimal": Decimal}.get(output.type, str)(clean)
        return outputs

    # ------------------------------------------------------------- reporting

    def _log(self, step: int | None, kind: str, message: str) -> None:
        message = self.redactor.text(message)  # every log line is scrubbed before it is kept or printed
        self.log.append(LogEntry(step, kind, message))
        logger.info("step %s %s: %s", step, kind, message)

    def _failure(self, problem: ReplayError) -> Failure:
        return Failure(
            step=self._step_number,
            phase=self._phase,
            action=self._action,
            error=type(problem).__name__,
            message=self.redactor.text(str(problem)),
            expected=self.redactor.text(problem.expected) if problem.expected else None,
            observed=self.redactor.text(problem.observed) if problem.observed else None,
            screenshot=self._take_screenshot(),
        )

    def _take_screenshot(self) -> Path | None:
        """Save what the screen looked like at the moment of failure. Never fails the report.

        The surface hands back a screenshot that is already blurred (observe() does it), so a
        saved failure picture never shows a typed secret or an account number.
        """
        try:
            png = self.surface.observe().screenshot
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            when = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            meta = self.artifact.metadata
            where = f"step{self._step_number}" if self._step_number else self._phase
            path = self.screenshot_dir / f"{meta.name}-v{meta.version}-{when}-{where}.png"
            path.write_bytes(png)
            return path
        except (SurfaceError, OSError):
            return None
