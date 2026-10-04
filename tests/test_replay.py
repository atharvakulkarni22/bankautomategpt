import json
from decimal import Decimal

import pytest
from conftest import BANK_PASSWORD, BANK_USER, StubGuard
from typer.testing import CliRunner

from bag.safety import Decision, Guard, SafetyConfig
from bag.agent import Recorder, run_discovery
from bag.artifact import Artifact, approve_artifact, build_artifact, load_artifact, save_artifact
from bag.artifact.builder import default_interruptions, default_outcomes
from bag.cli import app
from bag.replay import (
    BUSINESS_OUTCOME,
    FAILURE,
    SUCCESS,
    ReplayRefused,
    Replayer,
    TransientError,
    classify,
    prepare_run,
    resolve_start_url,
)
from bag.replay.errors import LocatorNotFound, UnexpectedState
from bag.surface import (
    AmbiguousTarget,
    BrowserSurface,
    Observation,
    SurfaceError,
    SurfaceTimeout,
    Target,
    TargetNotFound,
    Values,
)

REAL_SECRETS = {"BANK_USER": BANK_USER, "BANK_PASSWORD": BANK_PASSWORD}


# --------------------------------------------------------------- test doubles


class FakeClock:
    """Time that only moves when something sleeps, so tests never really wait."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSurface:
    """A pretend app. `present` are the Targets locate() can find; `texts` are visible texts."""

    def __init__(self):
        self.url = "http://bank/login"
        self.present, self.texts = set(), set()
        self.reads, self.on_click, self.errors = {}, {}, {}
        self.actions, self.pauses, self.gone_to = [], [], []
        self.frames = []  # addresses loaded in iframes
        self.goto_error = None

    def goto(self, url):
        if self.goto_error:
            raise self.goto_error
        self.gone_to.append(url)

    def locate(self, targets):
        for index, target in enumerate(targets):
            if target in self.present:
                return index
        raise TargetNotFound("nothing matched " + ", ".join(str(t) for t in targets))

    def is_visible(self, target):
        return target.text in self.texts

    def current_url(self):
        return self.url

    def frame_urls(self):
        return list(self.frames)

    def pause(self, seconds):
        self.pauses.append(seconds)

    def _do(self, kind, target, *rest):
        self.actions.append((kind, target, *rest))
        failures = self.errors.get((kind, target))
        if failures:
            raise failures.pop(0)

    def click(self, target):
        self._do("click", target)
        if target in self.on_click:
            self.on_click[target](self)

    def type(self, target, text):
        self._do("type", target, text)

    def wait_for(self, target, timeout_ms=None):
        self._do("wait", target)

    def read(self, target):
        self._do("read", target)
        return self.reads[target]

    def observe(self):
        return Observation(url=self.url, title="t", tree="tree", screenshot=b"\x89PNG-fake")


def loc(primary, *fallbacks):
    return {"primary": primary, "fallbacks": list(fallbacks)}


def make_artifact(steps, *, status="approved", inputs=(), outputs=(), **rest):
    data = {
        "metadata": {"name": "demo", "version": 1, "app": "Bank", "status": status, "start_url": "http://bank/login"},
        "inputs": list(inputs), "steps": steps, "outputs": list(outputs),
    }
    data.update(rest)
    return Artifact.model_validate(data)


def replayer(artifact, surface, clock, tmp_path, **options):
    options.setdefault("timeout_s", 10.0)
    options.setdefault("guard", StubGuard())
    return Replayer(artifact, surface, clock=clock.time, sleep=clock.sleep, screenshot_dir=tmp_path, **options)


BUTTON = {"role": "button", "name": "Go"}
GO = Target(role="button", name="Go")
FALLBACK = Target(text="Go", exact=True)
POPUP_TEXT = "System maintenance notice"
OK = Target(role="button", name="OK")
click_go = {"action": "click", "locator": loc(BUTTON, {"text": "Go", "exact": True})}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def surface():
    s = FakeSurface()
    s.present = {GO}
    return s


# ---------------------------------------------------------------- prepare_run


def input_artifact(status="approved", **changes):
    inputs = [{"name": "member_id", "type": "string", "pattern": "[0-9]{4}"}, {"name": "amount", "type": "decimal"}]
    steps = [
        {"action": "type", "locator": loc({"css": "a"}), "text": "{{member_id}}"},
        {"action": "type", "locator": loc({"css": "b"}), "text": "{{amount}}"},
        {"action": "type", "locator": loc({"css": "c"}), "text": "{{secret:BANK_PASSWORD}}"},
    ]
    return make_artifact(steps, status=status, inputs=inputs, **changes)


def test_prepare_refuses_anything_not_approved():
    with pytest.raises(ReplayRefused, match="not approved.*bag approve demo"):
        prepare_run(input_artifact(status="draft"), {"member_id": "1001", "amount": "5"}, ["BANK_PASSWORD"])


def test_prepare_checks_and_normalises_inputs():
    clean = prepare_run(input_artifact(), {"member_id": " 1001 ", "amount": "12.50"}, ["BANK_PASSWORD"])
    assert clean == {"member_id": "1001", "amount": "12.50"}


@pytest.mark.parametrize(
    "inputs, message",
    [
        ({"member_id": "1001"}, "Missing input.*amount"),
        ({"member_id": "1001", "amount": "1", "extra": "x"}, "Unknown input.*extra"),
        ({"member_id": "12", "amount": "1"}, "does not match the required pattern"),
        ({"member_id": "1001", "amount": "lots"}, "must be a decimal"),
    ],
)
def test_prepare_refuses_bad_inputs_without_echoing_them(inputs, message):
    with pytest.raises(ReplayRefused, match=message) as error:
        prepare_run(input_artifact(), inputs, ["BANK_PASSWORD"])
    assert "lots" not in str(error.value)


def test_prepare_refuses_when_a_secret_is_unavailable():
    with pytest.raises(ReplayRefused, match="Secret.*BANK_PASSWORD"):
        prepare_run(input_artifact(), {"member_id": "1001", "amount": "1"}, ["BANK_USER"])


def test_start_url_takes_the_host_from_bank_url():
    assert resolve_start_url("http://127.0.0.1:5000/login", "http://10.0.0.5:8080") == "http://10.0.0.5:8080/login"
    assert resolve_start_url("http://127.0.0.1:5000/login", None) == "http://127.0.0.1:5000/login"


# ------------------------------------------------------------- the happy path


def test_runs_steps_in_order_checks_expected_and_casts_outputs(surface, clock, tmp_path):
    surface.present |= {Target(css="user"), Target(css="balance"), Target(css="name"), Target(css="count")}
    surface.reads = {Target(css="balance"): "$12,450.75", Target(css="name"): " Priya Sharma ", Target(css="count"): "3"}
    surface.on_click[GO] = lambda s: setattr(s, "url", "http://bank/home")
    steps = [
        {"action": "type", "locator": loc({"css": "user"}), "text": "{{secret:BANK_USER}}"},
        {"action": "click", "locator": loc(BUTTON), "expected": {"url_contains": "/home"}},
        {"action": "read", "locator": loc({"css": "balance"}), "output_name": "savings_balance"},
        {"action": "read", "locator": loc({"css": "name"}), "output_name": "member_name"},
        {"action": "read", "locator": loc({"css": "count"}), "output_name": "accounts"},
    ]
    outputs = [{"name": "savings_balance", "type": "decimal"}, {"name": "member_name"}, {"name": "accounts", "type": "int"}]
    artifact = make_artifact(steps, outputs=outputs, success_check={"outputs_present": ["savings_balance", "member_name"]})

    result = replayer(artifact, surface, clock, tmp_path, start_url="http://bank/start").run()

    assert result.status == SUCCESS and result.failure is None
    assert result.artifact == "demo v1"
    assert result.outputs == {"savings_balance": Decimal("12450.75"), "member_name": "Priya Sharma", "accounts": 3}
    assert type(result.outputs["accounts"]) is int and type(result.outputs["savings_balance"]) is Decimal
    assert surface.gone_to == ["http://bank/start"]
    assert [a[0] for a in surface.actions] == ["type", "click", "read", "read", "read"]
    assert surface.actions[0][2] == "{{secret:BANK_USER}}"  # the surface, not the engine, fills in the secret
    assert [e.message for e in result.log if e.kind == "step"][:2] == ["type ok via primary", "click ok via primary"]


def test_the_engine_never_sees_real_values(surface, clock, tmp_path):
    surface.present.add(Target(css="a"))
    artifact = make_artifact([{"action": "type", "locator": loc({"css": "a"}), "text": "{{member_id}}"}],
                             inputs=[{"name": "member_id"}])
    replayer(artifact, surface, clock, tmp_path).run()
    assert surface.actions == [("type", Target(css="a"), "{{member_id}}")]


# ------------------------------------------------------------------ fallbacks


def test_primary_is_preferred_when_both_would_match(surface, clock, tmp_path):
    surface.present = {GO, FALLBACK}
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path).run()
    assert result.status == SUCCESS and surface.actions == [("click", GO)]
    assert not [e for e in result.log if e.kind == "fallback"]


def test_fallback_is_used_and_logged_when_the_primary_fails(surface, clock, tmp_path):
    surface.present = {FALLBACK}
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path).run()
    assert result.status == SUCCESS and surface.actions == [("click", FALLBACK)]  # acted on the fallback target
    fallback = [e for e in result.log if e.kind == "fallback"]
    assert len(fallback) == 1 and "fallback 1 matched" in fallback[0].message and fallback[0].step == 1
    assert "click ok via fallback 1" in [e.message for e in result.log if e.kind == "step"]
    assert clock.sleeps == []  # fallbacks cost no waiting: they are tried in the same look


def test_no_locator_matching_fails_with_details_and_a_screenshot(surface, clock, tmp_path):
    surface.present = set()
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, timeout_s=3).run()
    failure = result.failure
    assert result.status == FAILURE and failure.error == "LocatorNotFound"
    assert (failure.step, failure.phase, failure.action) == (1, "step", "click")
    assert "role='button'" in failure.expected and "1 fallback" in failure.expected
    assert "nothing matched" in failure.observed
    assert failure.screenshot.parent == tmp_path and failure.screenshot.read_bytes().startswith(b"\x89PNG")
    assert surface.actions == [] and clock.now >= 3  # it kept looking until the timeout
    assert set(clock.sleeps) == {0.2}  # looking again is polling, not retrying with backoff


# -------------------------------------------------------------------- retries


def test_transient_errors_are_retried_with_growing_backoff(surface, clock, tmp_path):
    surface.errors[("click", GO)] = [SurfaceTimeout("not ready"), SurfaceTimeout("still not ready")]
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path).run()
    assert result.status == SUCCESS
    assert len([a for a in surface.actions if a[0] == "click"]) == 3  # two failures, then it worked
    assert clock.sleeps == [0.5, 1.0]
    assert [e.kind for e in result.log] == ["retry", "retry", "step"]
    assert result.log[-1].message == "click ok via primary after 2 retries"


def test_retries_stop_after_two(surface, clock, tmp_path):
    surface.errors[("click", GO)] = [SurfaceTimeout("slow")] * 5
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path).run()
    assert result.status == FAILURE and result.failure.error == "TransientError"
    assert len([a for a in surface.actions if a[0] == "click"]) == 3  # 1 try + 2 retries, no more
    assert len([e for e in result.log if e.kind == "retry"]) == 2


def test_other_errors_are_not_retried(surface, clock, tmp_path):
    surface.errors[("click", GO)] = [SurfaceError("the page crashed")] * 3
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path).run()
    assert result.failure.error == "UnexpectedState" and "page crashed" in result.failure.observed
    assert len([a for a in surface.actions if a[0] == "click"]) == 1 and clock.sleeps == []


def test_backoff_uses_the_surface_pause_by_default(surface, tmp_path):
    surface.errors[("click", GO)] = [SurfaceTimeout("slow")]
    clock = FakeClock()
    result = Replayer(make_artifact([click_go]), surface, guard=StubGuard(), clock=clock.time, screenshot_dir=tmp_path).run()
    assert result.status == SUCCESS and surface.pauses == [0.5]


def test_classify_sorts_exceptions_into_kinds():
    assert isinstance(classify(SurfaceTimeout("x")), TransientError)
    assert isinstance(classify(TargetNotFound("x")), LocatorNotFound)
    assert isinstance(classify(AmbiguousTarget("x")), LocatorNotFound)
    assert isinstance(classify(SurfaceError("x")), UnexpectedState)
    already = UnexpectedState("x")
    assert classify(already) is already
    with pytest.raises(KeyError):  # a bug in our own code is not hidden as a "failed run"
        classify(KeyError("bug"))


# ------------------------------------------------- interruptions and outcomes


def popup_artifact(**changes):
    return make_artifact([click_go], known_interruptions=[i.model_dump(mode="json") for i in default_interruptions()], **changes)


def test_a_known_popup_is_dismissed_before_the_step(surface, clock, tmp_path):
    surface.texts, surface.present = {POPUP_TEXT}, {GO, OK}
    surface.on_click[OK] = lambda s: s.texts.discard(POPUP_TEXT)
    result = replayer(popup_artifact(), surface, clock, tmp_path).run()
    assert result.status == SUCCESS
    assert [a[:2] for a in surface.actions] == [("click", OK), ("click", GO)]  # the popup first, then the step
    interruption = [e for e in result.log if e.kind == "interruption"]
    assert len(interruption) == 1 and POPUP_TEXT in interruption[0].message


def test_a_popup_that_keeps_coming_back_fails_the_run(surface, clock, tmp_path):
    surface.texts, surface.present = {POPUP_TEXT}, {GO, OK}  # clicking OK never removes it
    result = replayer(popup_artifact(), surface, clock, tmp_path, max_interruptions=2).run()
    assert result.status == FAILURE and result.failure.error == "UnexpectedState"
    assert "keeps coming back" in result.failure.message
    assert len([a for a in surface.actions if a[1] == OK]) == 2


SEARCH, HEADING = Target(role="button", name="Search"), Target(role="heading", name="Member Details")
NOT_FOUND = [o.model_dump(mode="json") for o in default_outcomes()]


def test_a_known_outcome_ends_the_run_without_failing(surface, clock, tmp_path):
    surface.present = {SEARCH}  # the heading never shows up: the page says "No member found" instead
    surface.on_click[SEARCH] = lambda s: s.texts.add("No member found")
    steps = [
        {"action": "click", "locator": loc({"role": "button", "name": "Search"})},
        {"action": "wait", "locator": loc({"role": "heading", "name": "Member Details"})},
    ]
    result = replayer(make_artifact(steps, known_outcomes=NOT_FOUND), surface, clock, tmp_path).run()
    assert result.status == BUSINESS_OUTCOME and result.outcome_code == "NOT_FOUND"
    assert "No member found" in result.message and result.failure is None and result.outputs == {}
    assert clock.now < 10  # noticed while waiting for the heading, not after the timeout


def test_an_outcome_showing_after_the_last_step_is_still_noticed(surface, clock, tmp_path):
    surface.on_click[GO] = lambda s: s.texts.add("No member found")
    result = replayer(make_artifact([click_go], known_outcomes=NOT_FOUND), surface, clock, tmp_path).run()
    assert result.status == BUSINESS_OUTCOME and result.outcome_code == "NOT_FOUND"


# ------------------------------------------------------- expected and success


def test_a_step_that_did_not_work_is_caught_by_its_expected_state(surface, clock, tmp_path):
    step = {"action": "click", "locator": loc(BUTTON), "expected": {"url_contains": "/home"}}
    result = replayer(make_artifact([step]), surface, clock, tmp_path).run()  # the click does not change the url
    failure = result.failure
    assert result.status == FAILURE and failure.error == "UnexpectedState" and failure.step == 1
    assert failure.expected == "the page address to contain '/home'"
    assert failure.observed == "the address is http://bank/login"
    assert failure.screenshot.exists()


def test_expected_text_must_become_visible(surface, clock, tmp_path):
    step = {"action": "click", "locator": loc(BUTTON), "expected": {"text_visible": "Welcome"}}
    surface.on_click[GO] = lambda s: s.texts.add("Welcome")
    assert replayer(make_artifact([step]), surface, clock, tmp_path).run().status == SUCCESS

    surface.texts.clear(); surface.on_click.clear()
    failure = replayer(make_artifact([step]), surface, clock, tmp_path).run().failure
    assert failure.expected == "the text 'Welcome' to be visible"


def test_success_check_failures(surface, clock, tmp_path):
    surface.present.add(Target(css="v"))
    surface.reads[Target(css="v")] = "   "
    steps = [{"action": "read", "locator": loc({"css": "v"}), "output_name": "balance"}]
    artifact = make_artifact(steps, outputs=[{"name": "balance"}], success_check={"outputs_present": ["balance"]})
    failure = replayer(artifact, surface, clock, tmp_path).run().failure
    assert failure.error == "UnexpectedState" and failure.phase == "finish" and failure.step is None
    assert "output 'balance' is missing" in failure.message

    surface.reads[Target(css="v")] = "ok"
    artifact = make_artifact(steps, outputs=[{"name": "balance"}], success_check={"text_visible": "All done"})
    assert replayer(artifact, surface, clock, tmp_path).run().failure.expected == "the text 'All done' to be visible"


def test_a_value_that_does_not_fit_its_type_fails_without_echoing_it(surface, clock, tmp_path):
    surface.present.add(Target(css="v"))
    surface.reads[Target(css="v")] = "customer-data-123"
    steps = [{"action": "read", "locator": loc({"css": "v"}), "output_name": "balance"}]
    artifact = make_artifact(steps, outputs=[{"name": "balance", "type": "decimal"}])
    failure = replayer(artifact, surface, clock, tmp_path).run().failure
    assert failure.error == "UnexpectedState" and "balance" in failure.message
    for text in (failure.message, failure.expected, failure.observed):
        assert "customer-data-123" not in text


# ---------------------------------------------------------- safety, start-up


def test_safety_can_block_a_step(surface, clock, tmp_path):
    guard = StubGuard(Decision.BLOCK, "money movement needs approval")
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=guard).run()
    assert result.failure.error == "SafetyBlocked" and result.failure.step == 1
    assert "money movement needs approval" in result.failure.observed
    assert surface.actions == []


def test_cannot_open_the_start_page(surface, clock, tmp_path):
    surface.goto_error = SurfaceError("connection refused")
    failure = replayer(make_artifact([click_go]), surface, clock, tmp_path).run().failure
    assert (failure.step, failure.phase) == (None, "start") and "connection refused" in failure.observed


# --------------------------------------------- with a real browser and the bank


def real_artifact(status="approved", **changes):
    """The task from the discovery demo, written by hand: look up a member and read two values."""
    steps = [
        {"action": "type", "locator": loc({"role": "textbox", "name": "User name:"}, {"css": 'input[name="user"]'}),
         "text": "{{secret:BANK_USER}}"},
        {"action": "type", "locator": loc({"css": 'input[name="pw"]'}), "text": "{{secret:BANK_PASSWORD}}"},
        {"action": "click", "locator": loc({"role": "button", "name": "Sign On"}), "expected": {"url_contains": "/home"}},
        {"action": "wait", "locator": loc({"role": "link", "name": "Sign Off"})},
        {"action": "type", "locator": loc({"css": 'input[name="mid"]'}), "text": "{{member_id}}"},
        {"action": "click", "locator": loc({"role": "button", "name": "Search"})},
        {"action": "wait", "locator": loc({"role": "heading", "name": "Member Details"})},
        {"action": "read", "locator": loc({"css": "tr:nth-of-type(2) > td:nth-of-type(2)"}), "output_name": "member_name"},
        {"action": "read", "locator": loc({"css": "tr:nth-of-type(4) > td:nth-of-type(2)"}), "output_name": "savings_balance"},
    ]
    data = {
        "metadata": {"name": "member-balance", "version": 1, "app": "First Legacy Bank", "status": status,
                     "start_url": "http://127.0.0.1:5000/login"},
        "inputs": [{"name": "member_id", "pattern": "[0-9]{4}"}],
        "steps": steps,
        "outputs": [{"name": "member_name"}, {"name": "savings_balance", "type": "decimal"}],
        "success_check": {"outputs_present": ["member_name", "savings_balance"]},
        "known_outcomes": NOT_FOUND,
        "known_interruptions": [i.model_dump(mode="json") for i in default_interruptions()],
    }
    data.update(changes)
    return Artifact.model_validate(data)


def real_guard(bank_url):
    """The real guard with the bank as the only allowed site, and nobody to ask for approval."""
    return Guard(SafetyConfig(allowed_urls=[bank_url]), approver=lambda request: False)


def replay_for_real(artifact, bank_url, tmp_path, member_id="1001", path="/login"):
    secrets = Values(secrets=REAL_SECRETS)
    clean = prepare_run(artifact, {"member_id": member_id}, secrets.secret_names)
    with BrowserSurface(timeout_ms=3000, values=Values(inputs=clean, secrets=REAL_SECRETS)) as surface:
        return Replayer(artifact, surface, guard=real_guard(bank_url), start_url=bank_url + path, timeout_s=3,
                        screenshot_dir=tmp_path).run()


@pytest.mark.parametrize(
    "member_id, name, balance",
    [("1001", "Priya Sharma", "12450.75"), ("1003", "Elena Rossi", "250000.00"), ("1004", "Tomasz Nowak", "57.25")],
)
def test_one_artifact_serves_different_members(bank_url, tmp_path, member_id, name, balance):
    result = replay_for_real(real_artifact(), bank_url, tmp_path, member_id)
    assert result.status == SUCCESS, result.failure
    assert result.outputs == {"member_name": name, "savings_balance": Decimal(balance)}


def test_unknown_member_is_a_business_outcome_not_a_failure(bank_url, tmp_path):
    result = replay_for_real(real_artifact(), bank_url, tmp_path, "9999")
    assert result.status == BUSINESS_OUTCOME and result.outcome_code == "NOT_FOUND"
    assert result.failure is None and result.outputs == {}


def test_the_maintenance_popup_is_dismissed_automatically(bank_url, tmp_path):
    result = replay_for_real(real_artifact(), bank_url, tmp_path, path="/login?popup=1")
    assert result.status == SUCCESS, result.failure
    assert any(e.kind == "interruption" and "System maintenance notice" in e.message for e in result.log)


def test_a_stale_primary_locator_falls_back_to_the_next(bank_url, tmp_path):
    artifact = real_artifact()
    artifact.steps[5].locator.primary = Target(role="button", name="Find")  # the button was renamed
    artifact.steps[5].locator.fallbacks = [Target(role="button", name="Search")]
    result = replay_for_real(artifact, bank_url, tmp_path)
    assert result.status == SUCCESS, result.failure
    assert [e.step for e in result.log if e.kind == "fallback"] == [6]


def test_a_wrong_expectation_fails_with_a_real_screenshot(bank_url, tmp_path):
    artifact = real_artifact()
    artifact.steps[2].expected.url_contains = "/nowhere"
    result = replay_for_real(artifact, bank_url, tmp_path)
    failure = result.failure
    assert result.status == FAILURE and failure.step == 3 and failure.error == "UnexpectedState"
    assert failure.expected == "the page address to contain '/nowhere'" and "/home" in failure.observed
    assert failure.screenshot.read_bytes().startswith(b"\x89PNG")


def test_a_real_timeout_is_classified_as_transient(bank_url, tmp_path):
    # With the popup on and no interruption known, clicking Search is blocked: a timeout, so retried.
    artifact = real_artifact(known_interruptions=[])
    result = replay_for_real(artifact, bank_url, tmp_path, path="/login?popup=1")
    assert result.status == FAILURE and result.failure.step == 6 and result.failure.error == "TransientError"
    assert len([e for e in result.log if e.kind == "retry"]) == 2


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)

    def propose(self, *args):
        return self.script.pop(0)


def test_learn_once_with_the_agent_then_replay_for_another_member(bank_url, tmp_path):
    """The whole point of the project: discovery -> recording -> artifact -> approval -> LLM-free replay."""
    def typed(css, text):
        return {"action": "type", "target": {"css": css}, "text": text, "reason": "fill in"}

    def read(css, name):
        return {"action": "read", "target": {"css": css}, "output_name": name, "reason": "read it"}

    script = [
        typed('input[name="user"]', "{{secret:BANK_USER}}"),
        typed('input[name="pw"]', "{{secret:BANK_PASSWORD}}"),
        {"action": "click", "target": {"role": "button", "name": "Sign On"}, "reason": "sign on"},
        {"action": "wait", "target": {"role": "link", "name": "Sign Off"}, "reason": "signed on"},
        typed('input[name="mid"]', "{{member_id}}"),
        {"action": "click", "target": {"role": "button", "name": "Search"}, "reason": "search"},
        {"action": "wait", "target": {"role": "heading", "name": "Member Details"}, "reason": "loaded"},
        read("tr:nth-of-type(2) > td:nth-of-type(2)", "member_name"),
        read("tr:nth-of-type(4) > td:nth-of-type(2)", "savings_balance"),
        {"action": "done", "text": "done", "reason": "finished"},
    ]
    # 1. Discovery, with member 1001. (A scripted stand-in plays the AI here.)
    values = Values(inputs={"member_id": "1001"}, secrets=REAL_SECRETS)
    recorder = Recorder("Look up a member", ["member_id"], bank_url + "/", "fake", tmp_path / "recordings")
    with BrowserSurface(timeout_ms=4000, values=values) as surface:
        found = run_discovery("Look up a member", surface, ScriptedLLM(script), recorder, values, bank_url + "/",
                              guard=real_guard(bank_url))
    assert found.stop_reason == "done" and found.outputs["member_name"] == "Priya Sharma"

    # 2. Build the artifact, save it, review it (approve).
    recording = json.loads(recorder.path.read_text(encoding="utf-8"))
    built = build_artifact(recording, name="member-balance", values=Values(secrets=REAL_SECRETS))
    assert built.warnings == []  # the reads were found by position, not by the value they read
    path = save_artifact(built.artifact, tmp_path / "artifacts", values=Values(secrets=REAL_SECRETS))
    approve_artifact(path)

    # 3. Replay for a different member. No LLM involved anywhere from here on.
    artifact = load_artifact(path)
    result = replay_for_real(artifact, bank_url, tmp_path, member_id="1003")
    assert result.status == SUCCESS, result.failure
    assert result.outputs == {"member_name": "Elena Rossi", "savings_balance": Decimal("250000.00")}


# ------------------------------------------------------------------------- CLI


@pytest.fixture
def cli(tmp_path, monkeypatch, bank_url):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for name, value in REAL_SECRETS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BANK_URL", bank_url)  # the artifact remembers 127.0.0.1:5000; this is where the bank really is
    folder = tmp_path / "artifacts"
    save_artifact(real_artifact(), folder, values=Values(secrets=REAL_SECRETS))
    # The shipped config allows port 5000 only; the test bank is on a free port, so write its own rules.
    rules = tmp_path / "safety.yaml"
    rules.write_text(f"allowed_urls:\n  - {bank_url}\nrisky_button_names: [confirm]\naudit_log: {tmp_path / 'audit.jsonl'}\n",
                     encoding="utf-8")

    def run(*args):
        return CliRunner().invoke(app, ["replay", "--artifacts-dir", str(folder),
                                        "--evidence-dir", str(tmp_path / "evidence"), "--timeout", "3",
                                        "--safety-config", str(rules), *args])  # later options win

    run.folder, run.evidence, run.rules, run.audit = folder, tmp_path / "evidence", rules, tmp_path / "audit.jsonl"
    return run


def test_cli_replays_an_approved_artifact(cli):
    result = cli("member-balance", "--input", "member_id=1002")
    assert result.exit_code == 0, result.output
    assert "SUCCESS" in result.output and "member_name = Marcus Webb" in result.output
    assert "savings_balance = 830.10" in result.output
    assert "step 1: type ok via primary" in result.output


def test_cli_reports_a_business_outcome_with_exit_code_2(cli):
    result = cli("member-balance", "--input", "member_id=9999")
    assert result.exit_code == 2 and "BUSINESS OUTCOME: NOT_FOUND" in result.output


def test_cli_refuses_a_draft_and_bad_input_before_touching_the_app(cli):
    path = cli.folder / "member-balance.v1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: approved", "status: draft"), encoding="utf-8")
    draft = cli("member-balance", "--input", "member_id=1002")
    assert draft.exit_code == 1 and "not approved" in draft.output and "Replaying" not in draft.output

    approve_artifact(path)
    for args, message in ((["--input", "member_id=12"], "required pattern"), ([], "Missing input(s): member_id")):
        refused = cli("member-balance", *args)
        assert refused.exit_code == 1 and message in refused.output and "Replaying" not in refused.output


def test_cli_prints_where_a_failure_happened(cli):
    path = cli.folder / "member-balance.v1.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("url_contains: /home", "url_contains: /nowhere"), encoding="utf-8")
    result = cli("member-balance", "--input", "member_id=1002")
    assert result.exit_code == 1
    assert "FAILURE at step 3 (click): UnexpectedState" in result.output
    assert "expected: the page address to contain '/nowhere'" in result.output
    assert len(list(cli.evidence.glob("*/screenshots/failure-step3.png"))) == 1 and "screenshot:" in result.output
