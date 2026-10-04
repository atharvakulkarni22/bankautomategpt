"""Proof that the guard and redaction are really wired into BOTH the replay engine and the CLI.

The safety rules themselves are tested in test_safety.py. Here the question is different: is the
guard asked before every action, and does everything that gets written down pass redaction?
"""

import json

import pytest
from conftest import BANK_PASSWORD, BANK_USER, StubGuard
from test_replay import (  # the fakes and fixtures the replay tests already use
    BUTTON,
    GO,
    OK,
    POPUP_TEXT,
    REAL_SECRETS,
    FakeClock,
    FakeSurface,
    click_go,
    cli,  # noqa: F401  (a fixture, imported so pytest can find it here too)
    loc,
    make_artifact,
    popup_artifact,
    real_artifact,
    replayer,
)
from typer.testing import CliRunner

from bag.artifact import Artifact
from bag.cli import app
from bag.replay import FAILURE, SUCCESS, Replayer, prepare_run
from bag.safety import ActionRule, AuditLog, Guard, Redactor, SafetyConfig
from bag.surface import BrowserSurface, SurfaceTimeout, Target, Values


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def surface():
    s = FakeSurface()
    s.present = {GO}
    return s


def fake_guard(approver=None, audit=None, redactor=None, **config):
    """The REAL guard, with the pretend bank as the only allowed site."""
    config = {"allowed_urls": ["http://bank"], **config}
    return Guard(SafetyConfig(**config), approver=approver or (lambda request: False), audit=audit, redactor=redactor)


# ---------------------------------------------------- the guard rules every step


def test_replay_asks_a_human_before_a_risky_click_and_stops_on_no(surface, clock, tmp_path):
    asked = []
    guard = fake_guard(approver=lambda request: asked.append(request) or False, risky_button_names=["go"])
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=guard).run()
    assert result.status == FAILURE and result.failure.error == "SafetyBlocked" and result.failure.step == 1
    assert "A human did not approve" in result.failure.observed
    assert surface.actions == []  # the click never reached the page
    assert [(r.run, r.step) for r in asked] == [("replay demo v1", 1)]


def test_replay_goes_ahead_once_a_human_says_yes_and_asks_only_once(surface, clock, tmp_path):
    asked = []
    guard = fake_guard(approver=lambda request: asked.append(request) or True, risky_button_names=["go"])
    surface.errors[("click", GO)] = [SurfaceTimeout("not ready")]  # one retry: must not ask again
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=guard).run()
    assert result.status == SUCCESS and len(asked) == 1
    assert len([a for a in surface.actions if a[0] == "click"]) == 2


def test_the_guard_sees_the_names_in_the_fallbacks_too(surface, clock, tmp_path):
    step = {"action": "click", "locator": loc({"role": "button", "name": "Go"}, {"text": "Confirm", "exact": True})}
    result = replayer(make_artifact([step]), surface, clock, tmp_path, guard=fake_guard(risky_button_names=["confirm"])).run()
    assert result.failure.error == "SafetyBlocked"  # the primary looks harmless, but a fallback is named Confirm


def test_a_page_outside_the_allowlist_blocks_the_step(surface, clock, tmp_path):
    asked = []
    surface.url = "http://evil.example/login"
    guard = fake_guard(approver=lambda request: asked.append(request) or True)
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=guard).run()
    assert result.failure.error == "SafetyBlocked" and "not on the allowlist" in result.failure.observed
    assert surface.actions == [] and asked == []  # a BLOCK is never put to a human


def test_a_start_page_outside_the_allowlist_is_never_opened(surface, clock, tmp_path):
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=fake_guard(),
                      start_url="http://evil.example/").run()
    assert (result.failure.error, result.failure.phase, result.failure.step) == ("SafetyBlocked", "start", None)
    assert surface.gone_to == []  # the browser never moved


def test_a_foreign_iframe_blocks_the_step(surface, clock, tmp_path):
    surface.frames = ["http://bank/search", "http://evil.example/login-form"]
    result = replayer(make_artifact([click_go]), surface, clock, tmp_path, guard=fake_guard()).run()
    assert result.failure.error == "SafetyBlocked" and "evil.example" in result.failure.observed and surface.actions == []


def test_dismissing_a_popup_goes_through_the_guard_too(surface, clock, tmp_path):
    surface.texts, surface.present = {POPUP_TEXT}, {GO, OK}
    guard = fake_guard(risky_button_names=["ok"])  # a contrived rule: pressing OK needs approval
    result = replayer(popup_artifact(), surface, clock, tmp_path, guard=guard).run()
    assert result.failure.error == "SafetyBlocked"
    assert result.failure.message == "Dismissing 'System maintenance notice' during step 1 was blocked by a safety rule."
    assert surface.actions == []


# ------------------------------------------------------- redaction in replay


def test_replay_logs_and_failure_reports_are_redacted(surface, clock, tmp_path):
    surface.url = "http://bank/home?account=1234567890123456&user=teller-xyz"
    surface.errors[("click", GO)] = [SurfaceTimeout("could not click for account 9990-0000-1001")]
    redactor = Redactor(Values(secrets={"BANK_USER": "teller-xyz"}))
    step = {"action": "click", "locator": loc(BUTTON), "expected": {"url_contains": "/nowhere"}}
    result = replayer(make_artifact([step]), surface, clock, tmp_path, guard=fake_guard(), redactor=redactor).run()

    failure = result.failure
    everything = " ".join([e.message for e in result.log] + [failure.message, failure.expected, failure.observed])
    for leaked in ("1234567890123456", "teller-xyz", "9990-0000-1001"):
        assert leaked not in everything
    assert "XXXXXXXXXXXX3456" in failure.observed and "{{secret:BANK_USER}}" in failure.observed
    assert "XXXX-XXXX-1001" in [e.message for e in result.log if e.kind == "retry"][0]


def test_every_replay_decision_is_audited(surface, clock, tmp_path):
    redactor = Redactor(Values(secrets={}))
    audit = tmp_path / "audit.jsonl"
    guard = fake_guard(audit=AuditLog(audit, redactor), redactor=redactor, approver=lambda request: True,
                       risky_button_names=["go"])
    steps = [{"action": "type", "locator": loc({"css": "box"}), "text": "{{member_id}}"}, click_go]
    surface.present.add(Target(css="box"))
    result = replayer(make_artifact(steps, inputs=[{"name": "member_id"}]), surface, clock, tmp_path, guard=guard).run()
    assert result.status == SUCCESS
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [(l["run"], l["step"], l["checked"], l["final"]) for l in lines] == [
        ("replay demo v1", 1, "ALLOW", "ALLOW"), ("replay demo v1", 2, "NEEDS_APPROVAL", "ALLOW")]


def test_the_guard_is_not_optional(surface, clock, tmp_path):
    with pytest.raises(TypeError, match="guard"):  # forgetting it must be a loud mistake
        Replayer(make_artifact([click_go]), surface, clock=clock.time)


# ------------------------ a real flow where the guard stops a real "Confirm" click


def subaccount_artifact():
    """Sign on, find a member, then open a sub-account: type an amount, Continue, Confirm, read the number."""
    data = real_artifact().model_dump(mode="json")
    more = [
        {"action": "click", "locator": loc({"role": "link", "name": "Open sub-account"})},
        {"action": "type", "locator": loc({"css": 'input[name="amt"]'}), "text": "25"},
        {"action": "click", "locator": loc({"role": "button", "name": "Continue"})},
        {"action": "click", "locator": loc({"role": "button", "name": "Confirm"})},
        {"action": "read", "locator": loc({"css": "p > font > b"}), "output_name": "sub_account"},
    ]
    data["steps"] = data["steps"][:7] + more  # sign on, search, wait for Member Details, then the new steps
    data.update(outputs=[{"name": "sub_account"}], success_check={"outputs_present": ["sub_account"]})
    data["metadata"]["name"] = "open-subaccount"
    return Artifact.model_validate(data)


def guarded_replay(bank_url, tmp_path, approver, member_id):
    config = SafetyConfig(
        allowed_urls=[bank_url], risky_button_names=["confirm"],
        risky_actions=[ActionRule(name="Entering a money amount", action="type", target_matches=r"\bamt\b")],
    )
    redactor = Redactor(Values(secrets=REAL_SECRETS))
    audit = tmp_path / "audit.jsonl"
    guard = Guard(config, approver=approver, audit=AuditLog(audit, redactor), redactor=redactor)
    artifact = subaccount_artifact()
    clean = prepare_run(artifact, {"member_id": member_id}, Values(secrets=REAL_SECRETS).secret_names)
    with BrowserSurface(timeout_ms=3000, values=Values(inputs=clean, secrets=REAL_SECRETS)) as surface:
        result = Replayer(artifact, surface, guard=guard, redactor=redactor, start_url=bank_url + "/login",
                          timeout_s=3, screenshot_dir=tmp_path).run()
    return result, [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]


def test_a_human_who_says_no_to_confirm_stops_the_real_run_before_the_click(bank_url, tmp_path):
    asked = []

    def approver(request):
        asked.append(request)
        return "confirm" not in request.reason.lower()  # fine with the amount, not with Confirm

    result, audit = guarded_replay(bank_url, tmp_path, approver, "1002")
    assert result.status == FAILURE and result.failure.error == "SafetyBlocked" and result.failure.step == 11
    assert [r.rule for r in asked] == ["Entering a money amount", "risky_button_names"]
    assert audit[-1]["step"] == 11 and (audit[-1]["checked"], audit[-1]["final"]) == ("NEEDS_APPROVAL", "BLOCK")
    assert not any(line["step"] == 12 for line in audit)  # nothing after the refused click was even considered


def test_a_human_who_says_yes_lets_the_real_run_finish(bank_url, tmp_path):
    result, audit = guarded_replay(bank_url, tmp_path, lambda request: True, "1002")
    assert result.status == SUCCESS, result.failure
    assert result.outputs == {"sub_account": "SA-1002-01"}  # -01: the refused run above created nothing
    assert [line["step"] for line in audit if line["approved_by"] == "human"] == [9, 11]  # the amount, and Confirm
    assert all(line["final"] == "ALLOW" for line in audit)


# ----------------------------------------------------------------- the CLI


def test_cli_uses_the_safety_config_it_is_given(cli, tmp_path):  # noqa: F811
    result = cli("member-balance", "--input", "member_id=1002")
    assert result.exit_code == 0
    audit = [json.loads(line) for line in cli.audit.read_text(encoding="utf-8").splitlines()]
    assert len(audit) == 9 and all(line["final"] == "ALLOW" for line in audit)  # one audited decision per step


def test_cli_blocks_a_site_that_is_not_on_the_allowlist(cli, tmp_path):  # noqa: F811
    other = tmp_path / "other.yaml"
    other.write_text("allowed_urls:\n  - http://127.0.0.1:1\n", encoding="utf-8")
    result = cli("member-balance", "--input", "member_id=1002", "--safety-config", str(other))
    assert result.exit_code == 1
    assert "FAILURE" in result.output and "SafetyBlocked" in result.output and "not on the allowlist" in result.output


def test_cli_refuses_to_run_with_no_safety_config(cli, tmp_path):  # noqa: F811
    result = cli("member-balance", "--input", "member_id=1002", "--safety-config", str(tmp_path / "nope.yaml"))
    assert result.exit_code == 1 and "Nothing runs without one" in result.output and "Replaying" not in result.output


def test_cli_discover_runs_the_whole_guarded_loop(tmp_path, monkeypatch, bank_url):
    """`bag discover` with a scripted stand-in for the AI: real browser, real guard, real redaction."""
    from bag.llm.base import LLMResponse, ToolCall

    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for name, value in REAL_SECRETS.items():
        monkeypatch.setenv(name, value)
    rules = tmp_path / "safety.yaml"
    rules.write_text(f"allowed_urls:\n  - {bank_url}\naudit_log: {tmp_path / 'audit.jsonl'}\n", encoding="utf-8")

    script = [
        {"action": "type", "target": {"css": 'input[name="user"]'}, "text": "{{secret:BANK_USER}}", "reason": "user"},
        {"action": "type", "target": {"css": 'input[name="pw"]'}, "text": "{{secret:BANK_PASSWORD}}", "reason": "password"},
        {"action": "click", "target": {"role": "button", "name": "Sign On"}, "reason": "sign on"},
        {"action": "wait", "target": {"role": "link", "name": "Sign Off"}, "reason": "signed on"},
        {"action": "done", "text": "signed on", "reason": "finished"},
    ]
    seen_images = []

    class ScriptedClient:
        model = "scripted"

        def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
            seen_images.extend(messages[0].images)
            return LLMResponse(tool_calls=[ToolCall("1", "act", script.pop(0))])

    monkeypatch.setattr("bag.llm.get_client", lambda *a, **k: ScriptedClient())
    result = CliRunner().invoke(app, ["discover", "--goal", "Sign on", "--start-url", bank_url, "--max-steps", "8",
                                      "--output-dir", str(tmp_path / "rec"), "--safety-config", str(rules)])
    assert result.exit_code == 0, result.output
    assert "Stopped: done after 5 step(s)" in result.output

    recording = next((tmp_path / "rec").glob("*.json")).read_text(encoding="utf-8")
    assert BANK_USER not in recording and BANK_PASSWORD not in recording and "{{secret:BANK_USER}}" in recording
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert BANK_USER not in audit and BANK_PASSWORD not in audit and len(audit.splitlines()) == 5
    assert seen_images and all(image.startswith(b"\x89PNG") for image in seen_images)  # screenshots did reach the AI


def test_cli_discover_also_refuses_to_run_with_no_safety_config(tmp_path, monkeypatch, bank_url):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    result = CliRunner().invoke(app, ["discover", "--goal", "x", "--start-url", bank_url,
                                      "--safety-config", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1 and "Nothing runs without one" in result.output
