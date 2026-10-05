import inspect
import json

import pytest
import test_agent
from conftest import BANK_PASSWORD, BANK_USER, StubGuard
from test_replay import REAL_SECRETS
from typer.testing import CliRunner

from bag.agent import run_discovery
from bag.agent.actions import (
    Action,
    action_signature,
    history_entry,
    invalid_entry,
    is_password_field,
    narrate,
    normalize_action,
    tool_schema,
)
from bag.agent.llm import ACT_TOOL, SYSTEM_PROMPT, NoActionError, build_prompt
from bag.agent.loop import STUCK_AFTER
from bag.cli import app
from bag.handoff import HumanTakeover, find_intervention
from bag.logging import RunLogger
from bag.safety import Decision, Redactor
from bag.surface import Observation, Target, Values

SIGN_ON = {"role": "button", "name": "Sign On"}


def make_run_log(tmp_path):
    return RunLogger("discovery", "goal", tmp_path / "evidence", redactor=Redactor(Values(secrets={})))


def lines(run_log):
    return [json.loads(line) for line in run_log.steps_path.read_text(encoding="utf-8").splitlines()]


def page():
    return Observation(url="http://bank/home", title="Home", tree="- button Go", screenshot=b"\x89PNG")


def role_plus_text(reason="sign on"):
    return {"action": "click", "target": {"role": "button", "text": "Sign On"}, "reason": reason}


def role_plus_label(reason="x"):
    return {"action": "click", "target": {"role": "button", "label": "Go"}, "reason": reason}


def click(css, reason=""):
    return {"action": "click", "target": {"css": css}, "reason": reason}


def test_role_plus_text_is_repaired_to_role_plus_name():
    raw = {"action": "click", "target": {"role": "button", "text": "Sign On"}, "reason": "go"}
    fixed, repaired = normalize_action(raw)
    assert fixed["target"] == SIGN_ON and fixed["reason"] == "go"
    assert repaired == {"rule": "role+text -> role+name", "original": {"role": "button", "text": "Sign On"}, "fixed": SIGN_ON}
    assert Action.model_validate(fixed).target == Target(role="button", name="Sign On")
    assert raw["target"] == {"role": "button", "text": "Sign On"}


def test_the_repair_keeps_exact_and_ignores_empty_fields():
    fixed, repaired = normalize_action(
        {"action": "click", "target": {"role": "button", "text": "Go", "exact": True, "name": None, "label": None}}
    )
    assert fixed["target"] == {"role": "button", "exact": True, "name": "Go"} and repaired is not None


@pytest.mark.parametrize(
    "target",
    [
        {"role": "button", "text": "Go", "name": "Go"},
        {"role": "button", "text": "Go", "label": "Go"},
        {"role": "button", "text": "Go", "css": "a"},
        {"role": "button", "label": "Go"},
        {"role": "button", "css": "a"},
        {"role": "button"},
        {"text": "Go"},
        {"css": "a"},
        {"name": "Go"},
        {},
        None,
        "button Go",
    ],
)
def test_no_other_combination_is_guessed_at(target):
    raw = {"action": "click", "target": target}
    fixed, repaired = normalize_action(raw)
    assert fixed is raw and repaired is None


@pytest.mark.parametrize("raw", [None, "click", ["click"], 7])
def test_things_that_are_not_actions_pass_through_untouched(raw):
    assert normalize_action(raw) == (raw, None)


def test_the_target_error_for_role_plus_text_says_how_to_fix_it():
    with pytest.raises(ValueError, match=r"Buttons, links and textboxes are \{role, name\}, never \{role, text\}"):
        Target(role="button", text="Sign On")
    with pytest.raises(ValueError) as other:
        Target(label="a", css="b")
    assert "never {role, text}" not in str(other.value)


def run(tmp_path, script, **options):
    return test_agent.discover(tmp_path, script, **options)


def test_the_loop_repairs_role_plus_text_runs_it_and_logs_both_versions(tmp_path):
    run_log = make_run_log(tmp_path)
    result, surface, llm, recording = run(tmp_path, [role_plus_text(), {"action": "done"}], run_log=run_log)

    assert result.stop_reason == "done"
    assert [call for call in surface.calls if call[0] == "click"] == [("click", Target(role="button", name="Sign On"))]
    step = recording["steps"][0]
    assert step["status"] == "ok" and step["action"]["target"] == SIGN_ON
    assert step["repaired"] == {"rule": "role+text -> role+name", "original": {"role": "button", "text": "Sign On"}, "fixed": SIGN_ON}

    entries = lines(run_log)
    repaired = [e for e in entries if e["event"] == "repaired"]
    assert len(repaired) == 1 and repaired[0]["step"] == 1
    assert repaired[0]["original"] == {"role": "button", "text": "Sign On"} and repaired[0]["fixed"] == SIGN_ON
    assert [e["event"] for e in entries].index("repaired") < [e["event"] for e in entries].index("step")
    assert next(e for e in entries if e["event"] == "step")["repaired"]["fixed"] == SIGN_ON
    assert "(your {role, text} target was repaired to {role, name})" in llm.histories[1][0].line


def test_other_invalid_combinations_stay_invalid_and_are_not_repaired(tmp_path):
    run_log = make_run_log(tmp_path)
    result, surface, llm, recording = run(tmp_path, [role_plus_label(), {"action": "done"}], run_log=run_log)
    assert recording["steps"][0]["status"] == "invalid" and "repaired" not in recording["steps"][0]
    assert "got ['role', 'label']" in recording["steps"][0]["result"]
    assert not [e for e in lines(run_log) if e["event"] == "repaired"]
    assert not [call for call in surface.calls if call[0] == "click"]


def test_every_invalid_action_is_fed_back_in_the_next_prompt_with_examples(tmp_path):
    script = [role_plus_label(), {"action": "click"}, click("a"), {"action": "done"}]
    result, surface, llm, recording = run(tmp_path, script)

    first = build_prompt("goal", [], [], llm.histories[1], page(), 2, 25)
    assert ("Your last action was invalid: target: A Target needs exactly one of role, label, text, css "
            "(got ['role', 'label']). Valid target examples: {role, name} | {label} | {text} | {css}.") in first
    assert ".." not in first.split("Your last action was invalid")[1].split("Choose the next step")[0]
    second = build_prompt("goal", [], [], llm.histories[2], page(), 3, 25)
    assert "Your last action was invalid: action 'click' needs: target. Valid target examples:" in second
    assert "(you sent:" in second and '"role": "button"' in second
    third = build_prompt("goal", [], [], llm.histories[3], page(), 4, 25)
    assert "Your last action was invalid" not in third


def test_a_reply_with_no_tool_call_is_fed_back_too(tmp_path):
    result, surface, llm, recording = run(tmp_path, [NoActionError("no tool call"), {"action": "done"}])
    prompt = build_prompt("goal", [], [], llm.histories[1], page(), 2, 25)
    assert "Your last action was invalid: " in prompt and "no tool call" in prompt


def test_no_feedback_when_there_is_no_problem():
    assert "Your last action was invalid" not in build_prompt("goal", [], [], [], page(), 1, 25)
    plain = [(1, "click Target(text='Go')", "clicked")]
    assert "Your last action was invalid" not in build_prompt("goal", [], [], plain, page(), 2, 25)
    assert "1. click Target(text='Go') -> clicked" in build_prompt("goal", [], [], plain, page(), 2, 25)


def test_the_same_invalid_action_three_times_in_a_row_stops_the_run_as_stuck(tmp_path):
    script = [
        role_plus_label("first try"),
        {"target": {"label": "Go", "role": "button"}, "action": "click", "reason": "second try"},
        {"action": "click", "target": {"role": "button", "label": "Go", "css": None}, "reason": "third try"},
        {"action": "done"},
        {"action": "done"},
    ]
    result, surface, llm, recording = run(tmp_path, script)
    assert (result.stop_reason, result.steps) == ("stuck", 3)
    assert result.message.startswith("Stuck: the same action was tried 3 times in a row: invalid action")
    assert len(llm.script) == 2
    assert [s["status"] for s in recording["steps"]] == ["invalid", "invalid", "invalid"]
    assert recording["stop_reason"] == "stuck" and result.interventions == []


def test_a_valid_action_repeated_three_times_stops_before_the_third_is_run(tmp_path):
    result, surface, llm, recording = run(tmp_path, [click("a", "1"), click("a", "2"), click("a", "3"), {"action": "done"}])
    assert (result.stop_reason, result.steps) == ("stuck", 3)
    assert len([call for call in surface.calls if call[0] == "click"]) == 2
    assert [s["status"] for s in recording["steps"]] == ["ok", "ok", "stuck"]


def test_typing_the_same_password_three_times_is_a_loop_too(tmp_path):
    typed = {"action": "type", "target": {"css": 'input[name="pw"]'}, "text": "{{secret:BANK_PASSWORD}}"}
    result, surface, llm, recording = run(tmp_path, [typed, typed, typed, {"action": "done"}])
    assert result.stop_reason == "stuck" and len([c for c in surface.calls if c[0] == "type"]) == 2


def test_a_blocked_action_tried_three_times_counts(tmp_path):
    result, surface, llm, recording = run(
        tmp_path, [click("a")] * 3 + [{"action": "done"}], guard=StubGuard(Decision.BLOCK, "needs approval")
    )
    assert result.stop_reason == "stuck"
    assert [s["status"] for s in recording["steps"]] == ["blocked", "blocked", "stuck"]


def test_a_missing_tool_call_three_times_is_a_loop_too(tmp_path):
    script = [NoActionError("no tool call")] * 3 + [{"action": "done"}]
    result, surface, llm, recording = run(tmp_path, script)
    assert (result.stop_reason, result.steps) == ("stuck", 3)


def test_different_actions_in_between_reset_the_count(tmp_path):
    script = [click("a"), click("a"), click("b"), click("a"), click("a"), {"action": "done"}]
    result, surface, llm, recording = run(tmp_path, script)
    assert result.stop_reason == "done" and len([c for c in surface.calls if c[0] == "click"]) == 5


def test_how_many_repeats_count_as_stuck_can_be_changed(tmp_path):
    assert STUCK_AFTER == 3
    result, *_ = run(tmp_path, [click("a"), click("a"), {"action": "done"}], stuck_after=2)
    assert (result.stop_reason, result.steps) == ("stuck", 2)


def test_the_signature_ignores_the_reason_and_the_order_of_keys():
    one = action_signature({"action": "click", "reason": "a", "target": {"role": "button", "label": "Go"}})
    two = action_signature({"target": {"label": "Go", "role": "button", "css": None}, "reason": "b", "action": "click"})
    other = action_signature({"action": "click", "reason": "a", "target": {"role": "button", "label": "Stop"}})
    assert one == two and one != other
    assert action_signature(Action.model_validate(click("a", "x"))) == action_signature(Action.model_validate(click("a", "y")))


class LiveSurface(test_agent.FakeSurface):
    headless = False

    def add_init_script(self, script):
        pass

    def evaluate_in_frames(self, expression):
        return [[]] if "Collect" in expression else [None]

    def bring_to_front(self):
        pass

    def pause(self, seconds):
        pass


def desk_for(tmp_path, surface, run_log=None, **options):
    options.setdefault("sleep", lambda seconds: None)
    return HumanTakeover(
        surface, directory=tmp_path / "iv", redactor=Redactor(Values(secrets={})), require_headed=False,
        run_log=run_log, **options,
    )


def test_a_stuck_run_leaves_a_human_handoff_request_behind(tmp_path):
    run_log = make_run_log(tmp_path)
    surface = LiveSurface()
    desk = desk_for(tmp_path, surface, run_log)
    result, _, llm, recording = run(tmp_path, [click("a")] * 3, surface=surface, help_desk=desk, run_log=run_log)

    assert result.stop_reason == "stuck"
    (request,) = result.interventions
    assert (request.status, request.kind, request.step, request.action, request.error) == ("requested", "discovery", 3, "stuck", "Stuck")
    assert request.reason.startswith("Stuck: the same action was tried 3 times in a row")
    saved = json.loads(request.path.read_text(encoding="utf-8"))
    assert saved["status"] == "requested" and saved["run_id"] == run_log.run_id
    assert desk.controller.state.value == "PAUSED_FOR_HUMAN"
    events = [e["event"] for e in lines(run_log)]
    assert "stuck" in events and "intervention" in events
    final = json.loads(run_log.result_path.read_text(encoding="utf-8"))
    assert final["status"] == "stuck" and final["interventions"] == [request.id]
    assert sorted(final["screenshots"]) == ["handoff-step3.png", "stopped-stuck.png"]
    with pytest.raises(LookupError, match="Nothing is waiting"):
        find_intervention(tmp_path / "iv", None)


def test_a_stuck_run_with_no_desk_just_stops(tmp_path):
    result, *_ = run(tmp_path, [click("a")] * 3)
    assert result.stop_reason == "stuck" and result.interventions == []
    assert not (tmp_path / "iv").exists()


def test_with_a_live_takeover_a_stuck_run_asks_the_person_and_carries_on(tmp_path):
    surface = LiveSurface()
    takeover = desk_for(tmp_path, surface, enter_check=lambda: "")
    script = [click("a"), click("a"), click("a"), click("a"), click("a"), {"action": "done"}]
    result, _, llm, recording = run(tmp_path, script, surface=surface, takeover=takeover)

    assert result.stop_reason == "done"
    (intervention,) = result.interventions
    assert (intervention.status, intervention.error, intervention.action) == ("resumed", "Stuck", "stuck")
    statuses = [s["status"] for s in recording["steps"]]
    assert statuses[:5] == ["ok", "ok", "stuck", "human", "ok"]
    assert llm.histories[3][-1].status == "human" and "A human took over and did:" in llm.histories[3][-1].result


def test_a_person_who_gives_up_on_a_stuck_run_ends_it_without_a_second_request(tmp_path):
    surface = LiveSurface()
    takeover = desk_for(tmp_path, surface, enter_check=lambda: "q")
    result, *_ = run(tmp_path, [click("a")] * 3 + [{"action": "done"}], surface=surface, takeover=takeover)
    assert result.stop_reason == "stuck"
    assert [i.status for i in result.interventions] == ["aborted"]


def test_when_the_person_has_been_asked_too_often_a_request_is_saved_instead(tmp_path):
    surface = LiveSurface()
    takeover = desk_for(tmp_path, surface, enter_check=lambda: "", max_interventions=0)
    result, *_ = run(tmp_path, [click("a")] * 3, surface=surface, takeover=takeover)
    assert result.stop_reason == "stuck" and [i.status for i in result.interventions] == ["requested"]


@pytest.mark.parametrize(
    "target, text, expected",
    [
        ({"css": 'input[name="pw"]'}, "{{secret:BANK_PASSWORD}}", "typed into password field (value hidden) - OK"),
        ({"role": "textbox", "name": "Password"}, "{{member_id}}", "typed into password field (value hidden) - OK"),
        ({"css": "input[type=password]"}, "{{secret:X}}", "typed into password field (value hidden) - OK"),
        ({"role": "textbox", "name": "User name:"}, "{{secret:BANK_USER}}", 'typed into textbox "User name:" (value hidden) - OK'),
        ({"label": "Member ID"}, "{{member_id}}", 'typed into field labelled "Member ID" (value {{member_id}}) - OK'),
    ],
)
def test_a_typed_field_is_described_clearly_and_secrets_stay_hidden(target, text, expected):
    action = Action.model_validate({"action": "type", "target": target, "text": text})
    assert narrate(action, "ok", "typed") == expected


@pytest.mark.parametrize(
    "raw, status, result, expected",
    [
        ({"action": "click", "target": SIGN_ON}, "ok", "clicked", 'clicked button "Sign On" - OK'),
        ({"action": "click", "target": {"text": "Go"}}, "ok", "clicked", 'clicked element with text "Go" - OK'),
        ({"action": "click", "target": {"css": "a.b"}}, "ok", "clicked", "clicked element a.b - OK"),
        ({"action": "wait", "target": {"role": "link", "name": "Sign Off"}}, "ok", "x", 'waited for link "Sign Off" - OK'),
        ({"action": "read", "target": {"css": "td"}, "output_name": "b"}, "ok", "read b = '$1'", "read b = '$1' - OK"),
        ({"action": "click", "target": {"role": "button", "name": "Search"}}, "error", "ERROR: boom",
         'tried to click button "Search" - FAILED: ERROR: boom'),
        ({"action": "click", "target": {"role": "button", "name": "Confirm"}}, "blocked", "BLOCKED by safety: no",
         'tried to click button "Confirm" - BLOCKED by safety: no'),
        ({"action": "click", "target": {"role": "button", "name": "Passenger list"}}, "ok", "clicked",
         'clicked button "Passenger list" - OK'),
    ],
)
def test_every_kind_of_step_reads_as_a_plain_sentence(raw, status, result, expected):
    assert narrate(Action.model_validate(raw), status, result) == expected


def test_password_detection_does_not_trip_on_ordinary_words():
    assert is_password_field(Target(css='input[name="pw"]')) and is_password_field(Target(label="Your PASSWORD"))
    assert not is_password_field(Target(role="button", name="Passenger list"))
    assert not is_password_field(Target(text="Pass the test"))


def test_the_prompt_lists_a_typed_password_as_done_even_though_the_field_looks_empty():
    typed = Action.model_validate({"action": "type", "target": {"css": 'input[name="pw"]'}, "text": "{{secret:BANK_PASSWORD}}"})
    history = [history_entry(1, typed, "ok", "typed"), invalid_entry(2, "(invalid action)", "bad target", '{"x": 1}')]
    prompt = build_prompt("goal", [], [], history, page(), 3, 25)
    assert "1. typed into password field (value hidden) - OK" in prompt
    assert '2. (invalid action) - INVALID: bad target (you sent: {"x": 1})' in prompt
    assert history[0][:3] == (1, typed.summary(), "typed") and history[1][2] == "INVALID: bad target"


def test_the_act_tool_schema_explains_the_target_with_one_example_of_each_kind():
    description = tool_schema()["properties"]["target"]["description"]
    assert description == ACT_TOOL.parameters["properties"]["target"]["description"]
    for example in ('{"role": "button", "name": "Sign On"}', '{"label": "User name:"}', '{"text": "Member Details"}',
                    '{"css": "input[name=\\"mid\\"]"}'):
        assert example in description
    assert "role+name for buttons, links and textboxes (preferred)" in description
    assert "text only for plain text" in description and "Never combine role with text" in description
    assert "examples" not in json.dumps(tool_schema())
    assert set(tool_schema()["properties"]["target"]["properties"]) == {"role", "name", "label", "text", "css", "exact"}


def test_the_system_prompt_repeats_the_rules_that_keep_the_agent_out_of_trouble():
    assert 'never {role: "button", text: "Sign On"}' in SYSTEM_PROMPT
    assert "A password field always looks empty" in SYSTEM_PROMPT
    assert "three times in a row ends the run as stuck" in SYSTEM_PROMPT


def test_the_run_time_limit_is_600_seconds():
    assert inspect.signature(run_discovery).parameters["max_seconds"].default == 600
    helped = CliRunner().invoke(app, ["discover", "--help"])
    assert "[default: 600]" in " ".join(helped.output.split())


def scripted_client(script):
    from bag.llm.base import LLMResponse, ToolCall

    class Client:
        model = "scripted"

        def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
            return LLMResponse(tool_calls=[ToolCall("1", "act", script.pop(0))])

    return Client()


def discover_cli(tmp_path, monkeypatch, bank_url, script, *extra):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for name, value in REAL_SECRETS.items():
        monkeypatch.setenv(name, value)
    rules = tmp_path / "safety.yaml"
    rules.write_text(f"allowed_urls:\n  - {bank_url}\naudit_log: {tmp_path / 'audit.jsonl'}\n", encoding="utf-8")
    monkeypatch.setattr("bag.llm.get_client", lambda *a, **k: scripted_client(script))
    return CliRunner().invoke(app, [
        "discover", "--goal", "Sign on", "--start-url", bank_url, "--output-dir", str(tmp_path / "rec"),
        "--safety-config", str(rules), "--evidence-dir", str(tmp_path / "evidence"), "--run-id", "r1",
        "--interventions-dir", str(tmp_path / "iv"), *extra,
    ])


def test_the_real_bank_flow_survives_the_exact_mistake_gemini_makes(tmp_path, monkeypatch, bank_url):
    script = [
        {"action": "type", "target": {"css": 'input[name="user"]'}, "text": "{{secret:BANK_USER}}", "reason": "user"},
        {"action": "type", "target": {"css": 'input[name="pw"]'}, "text": "{{secret:BANK_PASSWORD}}", "reason": "password"},
        {"action": "click", "target": {"role": "button", "text": "Sign On"}, "reason": "sign on"},
        {"action": "wait", "target": {"role": "link", "name": "Sign Off"}, "reason": "signed on"},
        {"action": "done", "text": "signed on", "reason": "finished"},
    ]
    result = discover_cli(tmp_path, monkeypatch, bank_url, script)
    assert result.exit_code == 0, result.output
    assert "Stopped: done after 5 step(s)" in result.output

    entries = lines_of(tmp_path / "evidence" / "r1")
    repaired = [e for e in entries if e["event"] == "repaired"]
    assert len(repaired) == 1 and repaired[0]["step"] == 3 and repaired[0]["fixed"] == SIGN_ON
    recording = json.loads(next((tmp_path / "rec").glob("*.json")).read_text(encoding="utf-8"))
    assert recording["steps"][2]["status"] == "ok" and recording["steps"][2]["repaired"]["fixed"] == SIGN_ON
    assert BANK_USER not in json.dumps(entries) and BANK_PASSWORD not in json.dumps(entries)


def lines_of(folder):
    return [json.loads(line) for line in (folder / "steps.jsonl").read_text(encoding="utf-8").splitlines()]


def test_a_stuck_cli_run_stops_and_saves_a_help_request(tmp_path, monkeypatch, bank_url):
    script = [role_plus_label("a"), role_plus_label("b"), role_plus_label("c"), {"action": "done"}]
    result = discover_cli(tmp_path, monkeypatch, bank_url, script)
    assert result.exit_code == 1, result.output
    assert "Stopped: stuck after 3 step(s)." in result.output
    assert "Human help requested at step 3:" in result.output

    final = json.loads((tmp_path / "evidence" / "r1" / "result.json").read_text(encoding="utf-8"))
    assert final["status"] == "stuck" and len(final["interventions"]) == 1
    (saved,) = list((tmp_path / "iv").glob("*.json"))
    request = json.loads(saved.read_text(encoding="utf-8"))
    assert (request["status"], request["error"], request["run_id"]) == ("requested", "Stuck", "r1")
    assert "stuck" in [e["event"] for e in lines_of(tmp_path / "evidence" / "r1")]
