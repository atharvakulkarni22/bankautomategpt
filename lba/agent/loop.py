"""The discovery loop: observe -> ask the LLM -> check -> act -> record, repeat.

Stops when the AI says done, asks for a human, or a limit is hit (steps, time).
"""

import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from lba import safety
from lba.surface import SurfaceError

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


def run_discovery(
    goal,
    surface,
    agent_llm,
    recorder,
    values,
    start_url,
    *,
    max_steps=25,
    max_seconds=180,
    safety_check=safety.check,
    clock=time.monotonic,
) -> DiscoveryResult:
    """Let the AI explore until it finishes the goal or a limit is reached."""
    started = clock()
    outputs: dict = {}
    history: list = []  # (index, action summary, result) shown to the AI each turn
    steps = 0
    stop, message = STOP_MAX_STEPS, ""  # what happens if the loop simply runs out of steps

    try:
        surface.goto(start_url)
    except SurfaceError as error:
        recorder.finish(STOP_ERROR, outputs, f"Could not open {start_url}: {error}")
        return DiscoveryResult(STOP_ERROR, 0, outputs, recorder.path, f"Could not open {start_url}: {error}")

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
            recorder.add_step(index, observation.url, "", "invalid", str(error))
            history.append((index, "(no action)", f"INVALID: {error}"))
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

        # 4. CHECK it is safe.
        verdict = safety_check(action)
        if not verdict.allowed:
            result = f"BLOCKED by safety: {verdict.reason}"
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
        candidates = [] if action.action == "wait" else _describe(surface, action)  # before a click changes the page
        try:
            result, status = _execute(surface, action, outputs), "ok"
            if action.action == "wait":
                candidates = _describe(surface, action)
        except SurfaceError as error:
            result, status = f"ERROR: {_short(error, 500)}", "error"
        recorder.add_step(
            index, observation.url, action.reason, status, result, action=action, candidates=candidates
        )
        history.append((index, action.summary(), result))

    recorder.finish(stop, outputs, message)
    return DiscoveryResult(stop, steps, outputs, recorder.path, message)
