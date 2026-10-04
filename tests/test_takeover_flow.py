"""Human takeover end to end: a real browser, the real bank, the real guard.

The "person" is a function that acts on the page while the run is paused (through the same
surface, which is enough: the page cannot tell, and the recorder listens to real DOM events).
When it returns, that is the person pressing Enter. Returning "q" is typing q to give up.
"""

import json
from decimal import Decimal

import pytest
from conftest import BANK_PASSWORD, BANK_USER, StubGuard
from test_handoff import write_intervention
from test_replay import (
    GO,
    OK,
    POPUP_TEXT,
    REAL_SECRETS,
    FakeClock,
    FakeSurface,
    cli,  # noqa: F401  (a fixture, imported so pytest can find it here too)
    click_go,
    loc,
    make_artifact,
    popup_artifact,
    real_artifact,
    real_guard,
    replayer,
)
from typer.testing import CliRunner

from bag.agent import Recorder, run_discovery
from bag.artifact import Artifact
from bag.cli import app
from bag.handoff import HumanTakeover
from bag.replay import BUSINESS_OUTCOME, FAILURE, SUCCESS, Replayer, prepare_run
from bag.safety import Decision, Redactor
from bag.surface import BrowserSurface, SurfaceTimeout, Target, Values

SEARCH = Target(role="button", name="Search")


def nothing(surface, turn):
    """A person who looks at the page and does nothing."""


def click_search(surface, turn):
    surface.click(SEARCH)


def broken_search_artifact(**step_changes):
    """The real member lookup, but step 6 (click Search) looks for a button that does not exist."""
    data = real_artifact().model_dump(mode="json")
    data["steps"][5]["locator"] = {"primary": {"role": "button", "name": "Find"}, "fallbacks": []}
    data["steps"][5].update(step_changes)
    return Artifact.model_validate(data)


def replay_with_human(artifact, bank_url, tmp_path, human, *, member_id="1001", max_interventions=3):
    """Replay with takeover switched on. `human(surface, turn)` is what the person does on their nth turn."""
    secrets = Values(secrets=REAL_SECRETS)
    values = Values(inputs=prepare_run(artifact, {"member_id": member_id}, secrets.secret_names), secrets=REAL_SECRETS)
    redactor = Redactor(values)
    turns = []
    with BrowserSurface(timeout_ms=2500, values=values) as surface:

        def keyboard():
            turns.append(1)
            answer = human(surface, len(turns))  # the person acts...
            return "" if answer is None else answer  # ...then presses Enter (or types what the function returned)

        takeover = HumanTakeover(surface, directory=tmp_path / "iv", redactor=redactor, require_headed=False,
                                 enter_check=keyboard, max_interventions=max_interventions)
        result = Replayer(artifact, surface, guard=real_guard(bank_url), redactor=redactor, handoff=takeover,
                          start_url=bank_url + "/login", timeout_s=1.5, screenshot_dir=tmp_path).run()
    return result, takeover, turns


# -------------------------------------------------------------------- replay


def test_a_human_fixes_a_stuck_step_and_the_run_carries_on(bank_url, tmp_path):
    result, takeover, turns = replay_with_human(broken_search_artifact(), bank_url, tmp_path, click_search)

    assert result.status == SUCCESS, result.failure  # steps 7, 8 and 9 ran after the person's click
    assert result.outputs == {"member_name": "Priya Sharma", "savings_balance": Decimal("12450.75")}
    assert turns == [1]  # asked a person once

    (intervention,) = result.interventions
    assert (intervention.step, intervention.action, intervention.error) == (6, "click", "LocatorNotFound")
    assert (intervention.status, intervention.resumed_by, intervention.event_count) == ("resumed", "enter", 1)
    assert intervention.summary == 'click button "Search"'
    assert intervention.goal == real_artifact().metadata.description or intervention.goal == "member-balance"
    on_disk = json.loads(intervention.path.read_text(encoding="utf-8"))
    assert on_disk["reason"].startswith("Could not find the element for step 6") and on_disk["url"].endswith("/home")
    assert intervention.screenshot.read_bytes().startswith(b"\x89PNG")

    states = [(m.source.value, m.target.value) for m in takeover.controller.history]
    assert states == [("AUTOMATION", "PAUSED_FOR_HUMAN"), ("PAUSED_FOR_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION")]
    handoff = [e for e in result.log if e.kind == "handoff"]
    assert len(handoff) == 1 and handoff[0].step == 6 and 'click button "Search"' in handoff[0].message


def test_the_page_must_be_where_the_step_leads_or_the_human_is_asked_again(bank_url, tmp_path):
    artifact = broken_search_artifact(expected={"text_visible": "Member Details"})

    def human(surface, turn):
        if turn == 2:  # the first time they do nothing useful; the second time they do the click
            surface.click(SEARCH)

    result, takeover, turns = replay_with_human(artifact, bank_url, tmp_path, human)
    assert result.status == SUCCESS, result.failure
    assert turns == [1, 1]
    assert [(i.step, i.error) for i in result.interventions] == [(6, "LocatorNotFound"), (6, "UnexpectedState")]
    assert [m.target.value for m in takeover.controller.history] == ["PAUSED_FOR_HUMAN", "HUMAN", "AUTOMATION"] * 2


def test_a_run_does_not_ask_a_human_forever(bank_url, tmp_path):
    artifact = broken_search_artifact(expected={"text_visible": "Member Details"})
    result, takeover, turns = replay_with_human(artifact, bank_url, tmp_path, nothing, max_interventions=1)
    assert result.status == FAILURE and result.failure.error == "UnexpectedState" and result.failure.step == 6
    assert "not in the expected state" in result.failure.message
    assert len(result.interventions) == 1 and turns == [1]  # one chance was all it got


def test_a_read_step_is_read_again_because_a_human_cannot_hand_over_a_value(bank_url, tmp_path):
    data = real_artifact().model_dump(mode="json")
    data["steps"] = data["steps"][:4] + data["steps"][7:]  # sign on, then straight to reading: the page is not ready
    artifact = Artifact.model_validate(data)

    def human(surface, turn):
        surface.type(Target(css='input[name="mid"]'), "1003")  # their own choice of member, typed in the iframe
        surface.click(SEARCH)  # the iframe loads a new page

    result, takeover, _ = replay_with_human(artifact, bank_url, tmp_path, human, member_id="1001")
    assert result.status == SUCCESS, result.failure
    assert result.outputs == {"member_name": "Elena Rossi", "savings_balance": Decimal("250000.00")}  # what THEY looked up
    (intervention,) = result.interventions
    assert (intervention.step, intervention.error) == (5, "LocatorNotFound")
    assert 'click button "Search"' in intervention.summary and "mid" in intervention.summary
    events = json.loads(intervention.events_file.read_text(encoding="utf-8"))
    assert [(e["type"], e["frame"]) for e in events] == [("input", "0"), ("click", "0")]  # both survived the iframe loading
    assert events[0]["value"] == "1003"


def test_a_human_who_gives_up_ends_the_run_with_the_original_failure(bank_url, tmp_path):
    result, takeover, turns = replay_with_human(broken_search_artifact(), bank_url, tmp_path, lambda s, n: "q")
    assert result.status == FAILURE and result.failure.error == "LocatorNotFound" and result.failure.step == 6
    assert [i.status for i in result.interventions] == ["aborted"]
    assert any("did not resume" in e.message for e in result.log if e.kind == "handoff")
    assert takeover.controller.state.value == "HUMAN"  # giving up is logged as an event, not a move back


def test_a_human_who_never_comes_back_does_not_hang_the_run(bank_url, tmp_path):
    artifact = broken_search_artifact()
    secrets = Values(secrets=REAL_SECRETS)
    values = Values(inputs=prepare_run(artifact, {"member_id": "1001"}, secrets.secret_names), secrets=REAL_SECRETS)
    with BrowserSurface(timeout_ms=2500, values=values) as surface:
        takeover = HumanTakeover(surface, directory=tmp_path / "iv", require_headed=False, timeout_s=0.6, poll_s=0.1,
                                 enter_check=lambda: None)  # nobody ever answers
        result = Replayer(artifact, surface, guard=real_guard(bank_url), handoff=takeover, start_url=bank_url + "/login",
                          timeout_s=1.5, screenshot_dir=tmp_path).run()
    assert result.status == FAILURE and [i.status for i in result.interventions] == ["timed_out"]


@pytest.mark.parametrize("problem", ["safety", "business_outcome", "timeout", "nothing_wrong"])
def test_only_locator_and_state_problems_are_handed_to_a_human(tmp_path, problem):
    surface, clock = FakeSurface(), FakeClock()
    surface.present = {GO}
    takeover = HumanTakeover(surface, directory=tmp_path / "iv", require_headed=False)
    takeover.take_over = lambda **kwargs: pytest.fail("a person must not be called for this")
    options = {"handoff": takeover}
    artifact = make_artifact([click_go])
    if problem == "safety":
        options["guard"] = StubGuard(Decision.BLOCK, "needs approval")
    elif problem == "business_outcome":
        surface.on_click[GO] = lambda s: s.texts.add("No member found")
        artifact = make_artifact([click_go], known_outcomes=[{"text": "No member found", "outcome": "NOT_FOUND"}])
    elif problem == "timeout":
        surface.errors[("click", GO)] = [SurfaceTimeout("slow")] * 5

    result = replayer(artifact, surface, clock, tmp_path, **options).run()
    assert result.status == {"safety": FAILURE, "business_outcome": BUSINESS_OUTCOME, "timeout": FAILURE,
                             "nothing_wrong": SUCCESS}[problem]
    assert result.interventions == []


def test_without_a_handoff_a_stuck_step_still_just_fails(bank_url, tmp_path):
    secrets = Values(secrets=REAL_SECRETS)
    artifact = broken_search_artifact()
    values = Values(inputs=prepare_run(artifact, {"member_id": "1001"}, secrets.secret_names), secrets=REAL_SECRETS)
    with BrowserSurface(timeout_ms=2500, values=values) as surface:
        result = Replayer(artifact, surface, guard=real_guard(bank_url), start_url=bank_url + "/login", timeout_s=1.5,
                          screenshot_dir=tmp_path).run()
    assert result.status == FAILURE and result.failure.error == "LocatorNotFound" and result.interventions == []


def test_after_a_takeover_the_guard_still_checks_the_next_step(bank_url, tmp_path):
    """A person may wander anywhere. The automation will not carry on from a page that is not on the allowlist."""

    def human(surface, turn):
        surface.goto("about:blank")  # not an allowed page

    result, _, _ = replay_with_human(broken_search_artifact(), bank_url, tmp_path, human)
    assert result.status == FAILURE
    assert result.failure.error == "SafetyBlocked" and "not on the allowlist" in result.failure.observed


# ------------------------------------------------------------------ discovery


class ScriptedLLM:
    def __init__(self, script):
        self.script, self.histories = list(script), []

    def propose(self, goal, input_names, secret_names, history, observation, step, max_steps):
        self.histories.append(list(history))
        return self.script.pop(0)


ASK = {"action": "ask_human", "text": "Please sign on for me.", "reason": "I cannot find the credentials"}
DONE = {"action": "done", "text": "finished", "reason": "the page is ready"}


def sign_on_by_hand(surface, turn):
    surface.type(Target(css='input[name="user"]'), BANK_USER)  # typed for real, the way a person would
    surface.type(Target(css='input[name="pw"]'), BANK_PASSWORD)
    surface.click(Target(role="button", name="Sign On"))


def discover_with_human(bank_url, tmp_path, script, human, *, clock=None, max_interventions=3):
    values = Values(secrets=REAL_SECRETS)
    redactor = Redactor(values)
    llm = ScriptedLLM(script)
    recorder = Recorder("Sign on", [], bank_url, "fake", tmp_path / "rec", redactor)
    with BrowserSurface(timeout_ms=2500, values=values) as surface:
        takeover = HumanTakeover(surface, directory=tmp_path / "iv", redactor=redactor, require_headed=False,
                                 max_interventions=max_interventions, enter_check=lambda: human(surface, 0) or "")
        extra = {"clock": clock} if clock else {}
        result = run_discovery("Sign on", surface, llm, recorder, values, bank_url + "/", guard=real_guard(bank_url),
                               redactor=redactor, takeover=takeover, max_steps=6, **extra)
    return result, llm, json.loads(recorder.path.read_text(encoding="utf-8"))


def test_when_the_ai_asks_for_help_a_human_helps_and_discovery_continues(bank_url, tmp_path):
    result, llm, recording = discover_with_human(bank_url, tmp_path, [ASK, DONE], sign_on_by_hand)

    assert result.stop_reason == "done" and result.steps == 2  # it did not stop at ask_human: it carried on
    (intervention,) = result.interventions
    assert (intervention.kind, intervention.step, intervention.status) == ("discovery", 1, "resumed")
    assert intervention.reason == "Please sign on for me."

    told = llm.histories[1][0][2]  # what the AI is told on its next turn
    assert told.startswith("A human took over and did:")
    # (The bank's password box has no label, so it is named by its css path. Still marked as masked.)
    for expected in ('type into "User name:"', 'type into "input[name="pw"]" (masked)', 'click button "Sign On"'):
        assert expected in told
    assert BANK_USER not in told and BANK_PASSWORD not in told

    human_step = recording["steps"][0]
    assert human_step["status"] == "human" and human_step["action"]["action"] == "ask_human"
    values = [e.get("value") for e in human_step["human_events"] if e["type"] == "input"]
    assert values == ["{{secret:BANK_USER}}", "[masked]"]  # the user name is a placeholder, the password never read
    assert BANK_USER not in json.dumps(recording) and BANK_PASSWORD not in json.dumps(recording)


def test_the_time_a_human_takes_does_not_count_against_the_time_limit(bank_url, tmp_path):
    now = [0.0]

    def slow_human(surface, turn):
        now[0] += 10_000  # the person took almost three hours; the limit is 180 seconds
        sign_on_by_hand(surface, turn)

    result, _, _ = discover_with_human(bank_url, tmp_path, [ASK, DONE], slow_human, clock=lambda: now[0])
    assert result.stop_reason == "done"  # without the adjustment this would be "timeout"


def test_a_human_who_gives_up_leaves_the_ai_question_as_the_end_of_the_run(bank_url, tmp_path):
    result, llm, recording = discover_with_human(bank_url, tmp_path, [ASK, DONE], lambda s, n: "q")
    assert result.stop_reason == "ask_human" and result.message == "Please sign on for me."
    assert [i.status for i in result.interventions] == ["aborted"]
    assert len(llm.histories) == 1 and recording["steps"][-1]["status"] == "ok"  # the ask_human step, recorded as before


def test_discovery_stops_asking_a_human_after_the_limit(bank_url, tmp_path):
    result, llm, _ = discover_with_human(bank_url, tmp_path, [ASK, ASK, DONE], sign_on_by_hand, max_interventions=1)
    assert result.stop_reason == "ask_human" and len(result.interventions) == 1  # the second request ends the run


# ------------------------------------------------------------------------- CLI


@pytest.mark.parametrize("command", [["discover", "--goal", "x"], ["replay", "member-balance", "--input", "member_id=1002"]])
def test_takeover_needs_a_visible_browser(cli, command):  # noqa: F811
    result = cli(*command[1:], "--takeover") if command[0] == "replay" else CliRunner().invoke(app, [*command, "--takeover"])
    assert result.exit_code == 1 and "--takeover needs --headed" in result.output
    assert "Replaying" not in result.output


def run_resume(folder, *args):
    return CliRunner().invoke(app, ["resume", *args, "--interventions-dir", str(folder)])


def test_bag_resume_drops_the_flag_the_waiting_run_looks_for(tmp_path):
    write_intervention(tmp_path, "20261005-100000-replay-demo-step3")
    write_intervention(tmp_path, "20261005-090000-replay-old-step1", status="resumed")
    result = run_resume(tmp_path)  # no id: the one that is waiting
    assert result.exit_code == 0 and "Resume requested for 20261005-100000-replay-demo-step3" in result.output
    assert (tmp_path / "20261005-100000-replay-demo-step3.resume").exists()
    assert not (tmp_path / "20261005-090000-replay-old-step1.resume").exists()


def test_bag_resume_accepts_the_start_of_an_id(tmp_path):
    write_intervention(tmp_path, "20261005-100000-replay-demo-step3")
    write_intervention(tmp_path, "20261005-110000-replay-demo-step5")
    assert run_resume(tmp_path, "20261005-1100").exit_code == 0
    assert (tmp_path / "20261005-110000-replay-demo-step5.resume").exists()
    assert not (tmp_path / "20261005-100000-replay-demo-step3.resume").exists()


@pytest.mark.parametrize(
    "setup, args, message",
    [
        ([], [], "Nothing is waiting"),
        ([("a-one", "waiting"), ("a-two", "waiting")], [], "More than one run is waiting"),
        ([("a-one", "waiting")], ["zzz"], "No intervention 'zzz'"),
        ([("a-done", "resumed")], ["a-done"], "is not waiting"),
    ],
)
def test_bag_resume_explains_what_is_wrong_and_changes_nothing(tmp_path, setup, args, message):
    for name, status in setup:
        write_intervention(tmp_path, name, status=status)
    result = run_resume(tmp_path, *args)
    assert result.exit_code == 1 and message in result.output
    assert not list(tmp_path.glob("*.resume"))
