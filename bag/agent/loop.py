"""The discovery loop: observe -> ask the LLM -> guard -> act -> record, repeat.

Stops when the AI says done, asks for a human, or a limit is hit (steps, time).

Two safety layers run here: the guard decides whether each action may happen (and asks
a human when it must), and the redactor scrubs everything that is written down or shown
to the AI (step results, recordings, messages).
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from bag.safety import Decision, PageContext, Redactor
from bag.surface import SurfaceError

from .actions import Action, HistoryEntry, action_signature, history_entry, invalid_entry, normalize_action
from .llm import NoActionError

STOP_DONE = "done"
STOP_ASK_HUMAN = "ask_human"
STOP_MAX_STEPS = "max_steps"
STOP_TIMEOUT = "timeout"
STOP_ERROR = "error"
STOP_STUCK = "stuck"

STUCK_AFTER = 3
MAX_RESULT_CHARS = 300  # keep step results short in the history sent to the AI


@dataclass
class DiscoveryResult:
    stop_reason: str
    steps: int
    outputs: dict = field(default_factory=dict)
    recording_path: Path | None = None
    message: str = ""  # the question (ask_human), the summary (done) or the error
    interventions: list = field(default_factory=list)  # every time a human took over (bag.handoff.Intervention)


def _short(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _problem(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:3]:
        where = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        parts.append(f"{where}: {message}" if where else message)
    return _short("; ".join(parts))


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
    takeover=None,
    help_desk=None,
    run_log=None,
    max_steps=25,
    max_seconds=600,
    stuck_after=STUCK_AFTER,
    clock=time.monotonic,
) -> DiscoveryResult:
    """Let the AI explore until it finishes the goal or a limit is reached.

    `guard` must offer .check_url(url) and .authorize(action, context, run=, step=), as
    bag.safety.Guard does. There is deliberately no default: running without a guard
    must be a loud mistake, not a quiet one.

    `takeover` (optional, a bag.handoff.HumanTakeover) lets a person take the browser when the AI
    says ask_human, or when the same action has been tried `stuck_after` times in a row. The loop
    then carries on from the page the person leaves. `help_desk` (optional, anything with
    .request_help(...)) is where a request is saved when the run ends stuck and nobody can take over live.
    """
    redactor = redactor or Redactor(values)
    # One redactor for the whole run. The recorder may have been built with a different one
    # (one that does not know this run's secrets), so it is made to use this one: nothing can
    # slip into the recording through a mismatch.
    recorder.redactor = redactor
    desk = help_desk or takeover
    started = clock()
    outputs: dict = {}
    history: list = []  # HistoryEntry items shown to the AI each turn
    recent: list = []  # signatures of the latest attempts, for spotting a loop
    steps = 0
    taken_over: list = []  # the interventions of this run
    stop, message = STOP_MAX_STEPS, ""  # what happens if the loop simply runs out of steps

    def finish(stop_reason, steps_taken, text=""):
        text = redactor.text(text)
        recorder.finish(stop_reason, outputs, text)
        if run_log is not None:
            shot = None
            if stop_reason != STOP_DONE:
                try:
                    shot = run_log.screenshot(f"stopped-{stop_reason}", surface.observe().screenshot).name
                except SurfaceError:
                    shot = None
            run_log.step("agent", "stop", reason=stop_reason, message=text, screenshot=shot)
            run_log.finish(
                stop_reason, steps=steps_taken, outputs=redactor.data(outputs), message=text,
                recording=str(recorder.path), interventions=[item.id for item in taken_over],
            )
        return DiscoveryResult(stop_reason, steps_taken, redactor.data(outputs), recorder.path, text, taken_over)

    def record(index, url, reason, status, result, action=None, candidates=(), raw=None, human_events=None, repaired=None):
        recorder.add_step(index, url, reason, status, result, action=action, candidates=candidates, raw=raw,
                          human_events=human_events, repaired=repaired)
        if run_log is not None:
            run_log.step(
                "agent", "step", step=index, url=url, status=status, action=action.summary() if action else None,
                reason=reason, result=result, candidates=len(candidates), repaired=repaired,
            )

    def can_call_human():
        return takeover is not None and len(taken_over) < takeover.max_interventions

    def call_human(index, url, label, summary, question, record_reason, action, error=None, observed=None):
        nonlocal started
        waited_from = clock()
        taken = takeover.take_over(
            kind="discovery", goal=goal, label="discovery", step=index, action=label, reason=question,
            error=error, observed=observed,
        )
        started += clock() - waited_from  # the human's time must not count against the time limit
        taken_over.append(taken.intervention)
        if taken.aborted:
            return False
        shown = redactor.text(f"A human took over and did: {taken.summary}")
        record(index, url, record_reason, "human", shown, action=action, human_events=taken.events)
        history.append(HistoryEntry(index, summary, shown, f"a human took over - {shown}", "human"))
        recent.clear()
        return True

    def check_stuck(index, observation, signature, action, what):
        nonlocal stop, message
        recent.append(signature)
        del recent[:-stuck_after]
        if len(recent) < stuck_after or len(set(recent)) != 1:
            return None
        last = history[-1].result if history else ""
        text = redactor.text(f"Stuck: the same action was tried {stuck_after} times in a row: {what}. Last result: {last}")
        if run_log is not None:
            run_log.step("agent", "stuck", step=index, repeats=stuck_after, action=redactor.text(what), last_result=last)
        if action is not None:
            record(index, observation.url, action.reason, "stuck", text, action=action)
        live = can_call_human()
        if live and call_human(
            index, observation.url, "stuck", "stuck", text, action.reason if action else "", action,
            error="Stuck", observed=last,
        ):
            return True
        if desk is not None and not live:
            taken_over.append(desk.request_help(
                kind="discovery", goal=goal, label="discovery", step=index, action="stuck", reason=text,
                error="Stuck", observed=last,
            ))
        stop, message = STOP_STUCK, text
        return False

    if run_log is not None:
        run_log.step("agent", "start", goal=goal, url=start_url, inputs=values.input_names)

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
            record(index, observation.url, "", "invalid", problem)
            history.append(invalid_entry(index, "(no action)", problem))
            if check_stuck(index, observation, "(no tool call)", None, "no tool call") is False:
                break
            continue
        except Exception as error:  # provider or network failure: stop, keep the recording
            stop, message = STOP_ERROR, f"LLM call failed: {type(error).__name__}: {error}"
            break

        # 3. REPAIR the one mistake we know how to fix, then VALIDATE the answer.
        raw, repaired = normalize_action(raw)
        if repaired is not None and run_log is not None:
            run_log.step("agent", "repaired", step=index, **repaired)
        try:
            action = Action.model_validate(raw)
        except ValidationError as error:
            problem = _problem(error)
            sent = redactor.text(_short(json.dumps(raw, sort_keys=True, default=str), 200))
            record(index, observation.url, "", "invalid", f"INVALID ACTION: {problem}", raw=raw)
            history.append(invalid_entry(index, "(invalid action)", problem, sent))
            if check_stuck(index, observation, action_signature(raw), None, f"invalid action {sent}") is False:
                break
            continue
        action = action.protected(values)  # a literal real value becomes its placeholder

        # 3b. The same action again and again means the AI is going round in circles.
        if action.action not in ("done", "ask_human"):
            outcome = check_stuck(index, observation, action_signature(action), action, action.summary())
            if outcome is False:
                break
            if outcome is True:
                continue

        # 4. GUARD: may this happen? The fallbacks are looked up first (before a click changes the
        #    page) because they also tell the guard what the element is called.
        candidates = [] if action.action in ("wait", "done", "ask_human") else _describe(surface, action)
        context = _page_context(surface, observation.url, action, candidates)
        verdict = guard.authorize(action, context, run="discovery", step=index)
        if verdict.decision is not Decision.ALLOW:
            result = redactor.text(f"BLOCKED by safety: {verdict.reason}")
            record(index, observation.url, action.reason, "blocked", result, action=action, repaired=repaired)
            history.append(history_entry(index, action, "blocked", result, repaired))
            continue

        # 5a. The AI asked for a human. With takeover on, a person does the part the AI could not,
        #     and the run CONTINUES from the page they leave behind. Without it, the run stops here.
        if action.action == "ask_human" and can_call_human():
            if call_human(index, observation.url, "ask_human", action.summary(), action.text or "", action.reason, action):
                continue
            # The human gave up: fall through, and the run stops with the AI's question as before.

        # 5b. The AI can end the run itself.
        if action.action in ("done", "ask_human"):
            record(index, observation.url, action.reason, "ok", action.action, action=action, repaired=repaired)
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
        record(
            index, observation.url, action.reason, status, result, action=action, candidates=candidates,
            repaired=repaired,
        )
        history.append(history_entry(index, action, status, result, repaired))

    return finish(stop, steps, message)
