"""The discovery loop: observe -> ask the LLM -> guard -> act -> record, repeat.

Stops when the AI says done, asks for a human, or a limit is hit (steps, time).

Two safety layers run here: the guard decides whether each action may happen (and asks
a human when it must), and the redactor scrubs everything that is written down or shown
to the AI (step results, recordings, messages).
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from bag.safety import Decision, PageContext, Redactor
from bag.surface import SurfaceError

from .actions import Action
from .llm import NoActionError

STOP_DONE = "done"
STOP_ASK_HUMAN = "ask_human"
STOP_MAX_STEPS = "max_steps"
STOP_TIMEOUT = "timeout"
STOP_ERROR = "error"

MAX_RESULT_CHARS = 300  # keep step results short in the history sent to the AI


@dataclass
class DiscoveryResult:
    stop_reason: str
    steps: int
    outputs: dict = field(default_factory=dict)
    recording_path: Path | None = None
    message: str = ""  # the question (ask_human), the summary (done) or the error


def _short(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _execute(surface, action: Action, outputs: dict) -> str:
    """Run one non-final action on the surface. Returns a short result for the history."""
    if action.action == "click":
        surface.click(action.target)
        return "clicked"
    if action.action == "type":
        surface.type(action.target, action.text)  # text still holds {{placeholders}}
        return "typed"
    if action.action == "read":
        value = surface.read(action.target)
        outputs[action.output_name] = value
        return _short(f"read {action.output_name} = {value!r}")
    surface.wait_for(action.target)  # action.action == "wait"
    return "element appeared"


def _describe(surface, action: Action):
    """Fallback Targets for the action's element. Never fails the step."""
    try:
        return surface.describe(action.target)
    except SurfaceError:
        return []


def _page_context(surface, url: str, action: Action, candidates) -> PageContext:
    """What the guard needs to know: the page, its iframes, and every way to describe the element."""
    targets = list(dict.fromkeys([*candidates, *([action.target] if action.target else [])]))
    frames = surface.frame_urls() if hasattr(surface, "frame_urls") else []
    return PageContext(url=url, frame_urls=frames, targets=targets)


def run_discovery(
    goal,
    surface,
    agent_llm,
    recorder,
    values,
    start_url,
    *,
    guard,
    redactor=None,
    max_steps=25,
    max_seconds=180,
    clock=time.monotonic,
) -> DiscoveryResult:
    """Let the AI explore until it finishes the goal or a limit is reached.

    `guard` must offer .check_url(url) and .authorize(action, context, run=, step=), as
    bag.safety.Guard does. There is deliberately no default: running without a guard
    must be a loud mistake, not a quiet one.
    """
    redactor = redactor or Redactor(values)
    # One redactor for the whole run. The recorder may have been built with a different one
    # (one that does not know this run's secrets), so it is made to use this one: nothing can
    # slip into the recording through a mismatch.
    recorder.redactor = redactor
    started = clock()
    outputs: dict = {}
    history: list = []  # (index, action summary, result) shown to the AI each turn
    steps = 0
    stop, message = STOP_MAX_STEPS, ""  # what happens if the loop simply runs out of steps

    def finish(stop_reason, steps_taken, text=""):
        text = redactor.text(text)
        recorder.finish(stop_reason, outputs, text)
        return DiscoveryResult(stop_reason, steps_taken, redactor.data(outputs), recorder.path, text)

    # The browser may only start on an allowed site.
    verdict = guard.check_url(start_url)
    if verdict.decision is not Decision.ALLOW:
        return finish(STOP_ERROR, 0, f"Start URL blocked by safety: {verdict.reason}")
    try:
        surface.goto(start_url)
    except SurfaceError as error:
        return finish(STOP_ERROR, 0, f"Could not open {start_url}: {error}")

    for index in range(1, max_steps + 1):
        if clock() - started >= max_seconds:
            stop = STOP_TIMEOUT
            break
        steps = index

        # 1. LOOK at the page.
        try:
            observation = surface.observe()
        except SurfaceError as error:
            stop, message = STOP_ERROR, f"Could not observe the page: {error}"
            break

        # 2. ASK the AI what to do next.
        try:
            raw = agent_llm.propose(
                goal, values.input_names, values.secret_names, history, observation, index, max_steps
            )
        except NoActionError as error:
            problem = redactor.text(error)
            recorder.add_step(index, observation.url, "", "invalid", problem)
            history.append((index, "(no action)", f"INVALID: {problem}"))
            continue
        except Exception as error:  # provider or network failure: stop, keep the recording
            stop, message = STOP_ERROR, f"LLM call failed: {type(error).__name__}: {error}"
            break

        # 3. VALIDATE the answer.
        try:
            action = Action.model_validate(raw)
        except ValidationError as error:
            problem = _short(error.errors()[0]["msg"])
            recorder.add_step(index, observation.url, "", "invalid", f"INVALID ACTION: {problem}", raw=raw)
            history.append((index, "(invalid action)", f"INVALID: {problem}"))
            continue
        action = action.protected(values)  # a literal real value becomes its placeholder

        # 4. GUARD: may this happen? The fallbacks are looked up first (before a click changes the
        #    page) because they also tell the guard what the element is called.
        candidates = [] if action.action in ("wait", "done", "ask_human") else _describe(surface, action)
        context = _page_context(surface, observation.url, action, candidates)
        verdict = guard.authorize(action, context, run="discovery", step=index)
        if verdict.decision is not Decision.ALLOW:
            result = redactor.text(f"BLOCKED by safety: {verdict.reason}")
            recorder.add_step(index, observation.url, action.reason, "blocked", result, action=action)
            history.append((index, action.summary(), result))
            continue

        # 5. The AI can end the run itself.
        if action.action in ("done", "ask_human"):
            recorder.add_step(index, observation.url, action.reason, "ok", action.action, action=action)
            stop = STOP_DONE if action.action == "done" else STOP_ASK_HUMAN
            message = action.text or ""
            break

        # 6. ACT on the page, then RECORD what happened.
        try:
            result, status = _execute(surface, action, outputs), "ok"
            if action.action == "wait":
                candidates = _describe(surface, action)
        except SurfaceError as error:
            result, status = f"ERROR: {_short(error, 500)}", "error"
        result = redactor.text(result)  # a value that was read may hold an account number
        recorder.add_step(
            index, observation.url, action.reason, status, result, action=action, candidates=candidates
        )
        history.append((index, action.summary(), result))

    return finish(stop, steps, message)
