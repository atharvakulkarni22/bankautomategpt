"""Handoff without a real browser: the state machine, the resume mechanics, the takeover story."""

import json

import pytest
from conftest import StubGuard  # noqa: F401  (kept so this file can grow guard-based tests)
from test_replay import FakeClock
from typer.testing import CliRunner

from bag.cli import app
from bag.handoff import (
    ControlController,
    ControlState,
    HandoffError,
    HumanTakeover,
    InvalidTransition,
    LineReader,
    find_intervention,
    list_interventions,
    make_enter_checker,
    request_resume,
    summarize,
)
from bag.safety import Redactor
from bag.surface import BrowserSurface, Observation, SurfaceError, Values

A, P, H = ControlState.AUTOMATION, ControlState.PAUSED_FOR_HUMAN, ControlState.HUMAN


# ------------------------------------------------------------ the state machine


def test_the_three_legal_moves_are_logged_in_order(tmp_path):
    log = tmp_path / "control.jsonl"
    controller = ControlController(Redactor(Values(secrets={})), log)
    assert controller.state is A
    controller.transition(P, "step 6 could not be found", "iv-1")
    controller.transition(H, "the human has the browser", "iv-1")
    controller.transition(A, "the human finished", "iv-1")
    assert controller.state is A
    assert [(m.source, m.target, m.reason) for m in controller.history] == [
        (A, P, "step 6 could not be found"), (P, H, "the human has the browser"), (H, A, "the human finished")]
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(l["kind"], l["from"], l["to"], l["intervention"]) for l in lines] == [
        ("transition", "AUTOMATION", "PAUSED_FOR_HUMAN", "iv-1"),
        ("transition", "PAUSED_FOR_HUMAN", "HUMAN", "iv-1"),
        ("transition", "HUMAN", "AUTOMATION", "iv-1")]
    assert all(l["time"] for l in lines)


@pytest.mark.parametrize(
    "start_path, illegal",
    # Of the nine possible moves between three states, only three are legal (tested above); these are the other six.
    [([], H), ([], A), ([P], A), ([P], P), ([P, H], P), ([P, H], H)],
)
def test_every_other_move_is_refused_and_changes_nothing(start_path, illegal):
    controller = ControlController(Redactor(Values(secrets={})))
    for state in start_path:
        controller.transition(state, "setup")
    before, count = controller.state, len(controller.history)
    with pytest.raises(InvalidTransition, match=f"Cannot go from {before.value} to {illegal.value}"):
        controller.transition(illegal, "not allowed")
    assert controller.state is before and len(controller.history) == count


def test_transitions_and_notes_are_redacted(tmp_path):
    log = tmp_path / "control.jsonl"
    controller = ControlController(Redactor(Values(secrets={"BANK_USER": "teller-xyz"})), log)
    controller.transition(P, "user teller-xyz, account 1234567890123456")
    controller.note("gave_up", "again teller-xyz")
    text = log.read_text(encoding="utf-8")
    assert "teller-xyz" not in text and "1234567890123456" not in text
    assert "{{secret:BANK_USER}}" in text and "XXXXXXXXXXXX3456" in text
    assert json.loads(text.splitlines()[1])["kind"] == "event"


# ----------------------------------------------------------- reading the keyboard


def test_a_line_is_returned_only_once_enter_is_pressed():
    keys = list("ab")
    reader = LineReader(ready=lambda: bool(keys), read_char=lambda: keys.pop(0))
    assert reader.poll() is None  # "ab" typed, no Enter yet: nothing, and no waiting
    keys.extend(["c", "\r"])
    assert reader.poll() == "abc"
    keys.append("\n")
    assert reader.poll() == ""  # a bare Enter is an empty line
    assert reader.poll() is None


def test_backspace_edits_the_line_and_ctrl_c_still_interrupts():
    keys = list("qx\b\r")
    reader = LineReader(ready=lambda: bool(keys), read_char=lambda: keys.pop(0))
    assert reader.poll() == "q"
    keys.append("\x03")
    with pytest.raises(KeyboardInterrupt):
        reader.poll()


def test_with_no_terminal_the_keyboard_is_never_read():
    class Pipe:
        def isatty(self):
            return False

    assert make_enter_checker(Pipe())() is None  # only the resume flag file can end the wait then


# ------------------------------------------------------- the interventions folder


def write_intervention(folder, name, status="waiting", **extra):
    folder.mkdir(parents=True, exist_ok=True)
    data = {"id": name, "status": status, "step": 3, "reason": "the page was not what we expected", **extra}
    (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_finding_the_intervention_to_resume(tmp_path):
    write_intervention(tmp_path, "20261005-100000-replay-demo-step3")
    write_intervention(tmp_path, "20261005-110000-replay-demo-step5", status="resumed")
    (tmp_path / "20261005-100000-replay-demo-step3.human-events.json").write_text("[]", encoding="utf-8")  # not an intervention
    (tmp_path / "junk.json").write_text("not json", encoding="utf-8")
    assert [i["id"] for i in list_interventions(tmp_path)] == ["20261005-100000-replay-demo-step3", "20261005-110000-replay-demo-step5"]
    assert find_intervention(tmp_path, None)["id"] == "20261005-100000-replay-demo-step3"  # the only one waiting
    assert find_intervention(tmp_path, "20261005-1000")["id"].endswith("step3")  # an unambiguous start of an id works


def test_resume_lookup_errors_say_what_is_wrong(tmp_path):
    with pytest.raises(LookupError, match="Nothing is waiting"):
        find_intervention(tmp_path, None)
    write_intervention(tmp_path, "a-one")
    write_intervention(tmp_path, "a-two")
    write_intervention(tmp_path, "b-done", status="resumed")
    with pytest.raises(LookupError, match="More than one run is waiting.*a-one.*a-two"):
        find_intervention(tmp_path, None)
    with pytest.raises(LookupError, match="matches several"):
        find_intervention(tmp_path, "a-")
    with pytest.raises(LookupError, match="No intervention 'zzz'"):
        find_intervention(tmp_path, "zzz")
    with pytest.raises(LookupError, match="not waiting.*resumed"):
        find_intervention(tmp_path, "b-done")


# ------------------------------------------------------------- the takeover story


class PausableSurface:
    """A pretend headed browser: it records what the takeover asks of it."""

    headless = False

    def __init__(self):
        self.url = "http://bank/home"
        self.scripts, self.evaluated, self.pauses = [], [], []
        self.events = []  # what the "human" did, as the recorder script would report it
        self.fronted = 0
        self.close_after = None  # raise SurfaceError (a closed window) after this many pauses
        self.fail_install = False

    def observe(self):
        return Observation(url=self.url, title="t", tree="tree", screenshot=b"\x89PNG-fake")

    def add_init_script(self, script):
        if self.fail_install:
            raise SurfaceError("the window is gone")
        self.scripts.append(script)

    def evaluate_in_frames(self, expression):
        self.evaluated.append(expression)
        return [list(self.events)] if "Collect" in expression else [None]

    def bring_to_front(self):
        self.fronted += 1

    def pause(self, seconds):
        self.pauses.append(seconds)
        if self.close_after is not None and len(self.pauses) >= self.close_after:
            raise SurfaceError("the browser window was closed")


def make_takeover(tmp_path, surface=None, answers=(), **options):
    """A takeover whose 'keyboard' gives the scripted answers: None (nothing yet), '' (Enter) or text."""
    surface = surface or PausableSurface()
    keys, clock, printed = iter(answers), FakeClock(), []
    options.setdefault("redactor", Redactor(Values(secrets={})))
    takeover = HumanTakeover(
        surface, directory=tmp_path / "iv", enter_check=lambda: next(keys, None), clock=clock.time,
        sleep=clock.sleep, out=printed.append, **options,
    )
    return takeover, surface, clock, printed


def go(takeover, **overrides):
    arguments = dict(kind="replay", goal="Look up a member", label="replay demo v1", step=6, action="click",
                     reason="Could not find the element for step 6.", error="LocatorNotFound",
                     expected="a button named Search", observed="nothing matched")
    arguments.update(overrides)
    return takeover.take_over(**arguments)


def test_pressing_enter_hands_the_browser_back(tmp_path):
    takeover, surface, clock, printed = make_takeover(tmp_path, answers=[None, None, ""])
    surface.events = [{"t": 1, "type": "click", "role": "button", "name": "Search", "css": "input"},
                      {"t": 2, "type": "input", "name": "Member ID", "css": "input[name=mid]", "value": "1001"}]
    result = go(takeover)

    assert (result.aborted, result.via) == (False, "enter")
    assert [(m.source, m.target) for m in takeover.controller.history] == [(A, P), (P, H), (H, A)]
    assert takeover.controller.state is A
    assert result.summary == 'click button "Search"; type into "Member ID"'  # what was done, never the typed value
    assert surface.fronted == 1 and len(surface.scripts) == 1  # the window was raised, the recorder installed
    assert len(clock.sleeps) == 2  # it polled twice, then saw Enter

    shown = "\n".join(printed)
    assert "=== HUMAN TAKEOVER ===" in shown and f"bag resume {result.intervention.id}" in shown
    assert '"error": "LocatorNotFound"' in shown  # the intervention itself is printed
    assert "Resuming automation. The human did: click button" in shown


def test_the_intervention_file_says_what_a_human_needs_to_know(tmp_path):
    takeover, surface, _, _ = make_takeover(tmp_path, answers=[""])
    surface.events = [{"t": 1, "type": "click", "role": "link", "name": "Home", "css": "a"}]
    intervention = go(takeover).intervention
    data = json.loads(intervention.path.read_text(encoding="utf-8"))

    assert data["id"] == intervention.id and intervention.path.parent == tmp_path / "iv"
    assert (data["goal"], data["kind"], data["step"], data["action"]) == ("Look up a member", "replay", 6, "click")
    assert data["reason"] == "Could not find the element for step 6." and data["error"] == "LocatorNotFound"
    assert data["expected"] == "a button named Search" and data["observed"] == "nothing matched"
    assert data["url"] == "http://bank/home"
    assert data["screenshot"].endswith(".png") and (tmp_path / "iv" / f"{intervention.id}.png").read_bytes().startswith(b"\x89PNG")
    assert (data["status"], data["resumed_by"], data["event_count"]) == ("resumed", "enter", 1)
    assert json.loads((tmp_path / "iv" / f"{intervention.id}.human-events.json").read_text(encoding="utf-8"))[0]["name"] == "Home"


def test_the_file_says_waiting_while_the_human_works(tmp_path):
    seen = {}
    takeover, surface, _, _ = make_takeover(tmp_path)

    def while_the_human_works():
        # This runs inside the wait: it is the moment a second terminal would run `bag resume`.
        files = sorted((tmp_path / "iv").glob("*.json"))
        seen["status"] = json.loads(files[0].read_text(encoding="utf-8"))["status"]
        seen["state"] = takeover.controller.state
        return ""

    takeover.enter_check = while_the_human_works
    go(takeover)
    assert seen == {"status": "waiting", "state": H}


def test_bag_resume_from_a_second_terminal_ends_the_wait(tmp_path):
    runner, polls = CliRunner(), []

    def a_second_terminal_runs_bag_resume(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            result = runner.invoke(app, ["resume", "--interventions-dir", str(tmp_path / "iv")])
            assert result.exit_code == 0 and "Resume requested" in result.output

    takeover, surface, clock, _ = make_takeover(tmp_path)  # no keyboard input at all
    takeover.sleep = a_second_terminal_runs_bag_resume
    result = go(takeover)
    assert (result.aborted, result.via) == (False, "flag")
    assert not list((tmp_path / "iv").glob("*.resume"))  # the flag was used up: it cannot resume the next pause
    assert json.loads(result.intervention.path.read_text(encoding="utf-8"))["resumed_by"] == "flag"


def test_a_flag_meant_for_another_pause_is_left_alone(tmp_path):
    iv = tmp_path / "iv"
    iv.mkdir()
    (iv / "some-other-run.resume").write_text("resume", encoding="utf-8")
    takeover, surface, _, _ = make_takeover(tmp_path, answers=[None, ""])  # only Enter ends THIS pause
    result = go(takeover)
    assert result.via == "enter" and (iv / "some-other-run.resume").exists()


@pytest.mark.parametrize("typed", ["q", "Q", " quit ", "abort"])
def test_typing_q_gives_up_instead_of_resuming(tmp_path, typed):
    takeover, surface, _, printed = make_takeover(tmp_path, answers=[typed])
    surface.events = [{"t": 1, "type": "click", "role": "button", "name": "Cancel", "css": "b"}]
    result = go(takeover)
    assert (result.aborted, result.via) == (True, "abort")
    assert takeover.controller.state is H  # giving up is an event, not a move back to AUTOMATION
    assert [m.target for m in takeover.controller.history] == [P, H]
    assert result.intervention.status == "aborted" and result.intervention.event_count == 1
    assert "Not resuming: the human gave up" in "\n".join(printed)


def test_no_answer_in_time_ends_the_wait(tmp_path):
    takeover, surface, clock, _ = make_takeover(tmp_path, timeout_s=5)
    result = go(takeover)
    assert (result.aborted, result.via, result.intervention.status) == (True, "timeout", "timed_out")
    assert 5 <= clock.now < 6  # it waited the five seconds, and not much more


def test_a_closed_browser_window_ends_the_wait(tmp_path):
    takeover, surface, _, _ = make_takeover(tmp_path)
    takeover.sleep = surface.pause  # like the real thing: waiting is the browser's own pause, which fails once the window is gone
    surface.close_after = 3
    result = go(takeover)
    assert (result.aborted, result.via, result.intervention.status) == (True, "closed", "browser_closed")
    assert result.events == []


def test_a_browser_that_cannot_be_handed_over_is_reported_not_crashed(tmp_path):
    takeover, surface, _, _ = make_takeover(tmp_path)
    surface.fail_install = True
    result = go(takeover)
    assert result.aborted and result.intervention.status == "browser_closed"
    assert takeover.controller.state is P  # it never got as far as the human


def test_only_a_visible_browser_can_be_handed_to_a_person(tmp_path):
    with pytest.raises(HandoffError, match="needs a visible browser window"):
        HumanTakeover(BrowserSurface(headless=True), directory=tmp_path)
    with pytest.raises(HandoffError, match="--headed"):
        HumanTakeover(object(), directory=tmp_path)  # a surface that does not say it is visible is not trusted
    HumanTakeover(BrowserSurface(headless=False), directory=tmp_path)  # fine (the window is not opened here)


def test_everything_a_person_reads_is_redacted(tmp_path):
    redactor = Redactor(Values(secrets={"BANK_USER": "teller-xyz"}))
    takeover, surface, _, printed = make_takeover(tmp_path, answers=[""], redactor=redactor)
    surface.url = "http://bank/home?acct=1234567890123456"
    surface.events = [{"t": 1, "type": "input", "name": "User", "css": "i", "value": "teller-xyz"}]
    result = go(takeover, reason="user teller-xyz had account 9990-0000-1001", observed="saw 1234 5678 9012 3456")

    everything = (result.intervention.path.read_text(encoding="utf-8") + "\n".join(printed)
                  + (tmp_path / "iv" / f"{result.intervention.id}.human-events.json").read_text(encoding="utf-8")
                  + (tmp_path / "iv" / "transitions.jsonl").read_text(encoding="utf-8"))
    for leaked in ("teller-xyz", "1234567890123456", "9990-0000-1001", "1234 5678 9012 3456"):
        assert leaked not in everything
    assert "{{secret:BANK_USER}}" in everything and "XXXX-XXXX-1001" in everything


def test_two_takeovers_in_one_run_are_both_legal_and_get_separate_files(tmp_path):
    takeover, surface, _, _ = make_takeover(tmp_path, answers=["", ""])
    first, second = go(takeover), go(takeover)  # same label, step and second: ids must not collide
    assert first.intervention.id != second.intervention.id and second.intervention.id.endswith("-2")
    assert len(list((tmp_path / "iv").glob("*.json"))) == 2
    assert [m.target for m in takeover.controller.history] == [P, H, A, P, H, A]


def test_the_recorder_is_started_fresh_and_stopped_afterwards(tmp_path):
    takeover, surface, _, _ = make_takeover(tmp_path, answers=[""])
    go(takeover)
    assert [e for e in surface.evaluated if "Start" in e] and [e for e in surface.evaluated if "Stop" in e]
    assert surface.evaluated.index(next(e for e in surface.evaluated if "Start" in e)) < \
        surface.evaluated.index(next(e for e in surface.evaluated if "Stop" in e))


# -------------------------------------------------------------------- summarising


def test_the_summary_leaves_out_typed_values_and_repeats():
    events = [
        {"type": "click", "role": "button", "name": "Search", "css": "a"},
        {"type": "click", "role": "button", "name": "Search", "css": "a"},
        {"type": "input", "name": "User name:", "css": "u", "value": "secret-ish"},
        {"type": "input", "name": "Password", "css": "p", "value": "[masked]", "masked": True},
        {"type": "click", "css": "td:nth-of-type(2)", "tag": "td"},
    ]
    text = summarize(events)
    assert text == 'click button "Search"; type into "User name:"; type into "Password" (masked); click "td:nth-of-type(2)"'
    assert "secret-ish" not in text
    assert summarize([]) == "nothing was recorded"
    assert summarize([{"type": "click", "name": "x" * 500}], limit=50).endswith("...") and len(summarize([{"type": "click", "name": "x" * 500}], limit=50)) == 50
