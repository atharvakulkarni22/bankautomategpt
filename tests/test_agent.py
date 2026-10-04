import json
from types import SimpleNamespace as NS

import pytest
from conftest import BANK_PASSWORD, BANK_USER
from pydantic import ValidationError

from lba import safety
from lba.agent import Action, AgentLLM, Recorder, run_discovery
from lba.agent.llm import ACT_TOOL, SYSTEM_PROMPT, NoActionError, build_prompt
from lba.llm import ToolCall
from lba.llm.base import LLMResponse
from lba.surface import BrowserSurface, Observation, SurfaceError, Target, Values

SECRETS = {"BANK_USER": "teller-xyz", "BANK_PASSWORD": "hunter2-pw"}
USER_BOX = Target(css='input[name="user"]')
MEMBER_BOX = Target(css='input[name="mid"]')
SEARCH = Target(role="button", name="Search")


# --------------------------------------------------------------------- Action


@pytest.mark.parametrize(
    "fields",
    [
        {"action": "click"},  # no target
        {"action": "type", "target": {"css": "a"}},  # no text
        {"action": "read", "target": {"css": "a"}},  # no output_name
        {"action": "read", "target": {"css": "a"}, "output_name": "not valid!"},
        {"action": "ask_human", "text": "  "},
        {"action": "fly"},
        {"action": "done", "surprise": 1},
        {"action": "click", "target": {"css": "a", "text": "b"}},  # two strategies
    ],
)
def test_invalid_actions_are_rejected(fields):
    with pytest.raises(ValidationError):
        Action.model_validate(fields)


def test_valid_actions_and_summary():
    action = Action.model_validate(
        {"action": "type", "target": {"css": "input"}, "text": "{{member_id}}", "reason": "enter id"}
    )
    assert action.summary() == "type Target(css='input') text='{{member_id}}'"
    assert Action.model_validate({"action": "done"}).reason == ""
    assert Action.model_validate({"action": "read", "target": {"text": "x"}, "output_name": "balance"})


def test_tool_schema_is_plain_json_every_provider_accepts():
    schema = ACT_TOOL.parameters
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text and "anyOf" not in text
    assert set(schema["properties"]) == {"action", "target", "text", "output_name", "reason"}
    assert set(schema["properties"]["target"]["properties"]) == {"role", "name", "label", "text", "css", "exact"}
    assert schema["properties"]["action"]["enum"] == ["click", "type", "read", "wait", "done", "ask_human"]
    assert schema["required"] == ["action"]


# ------------------------------------------------------------------ AgentLLM


def observation(tree="- button Go"):
    return Observation(url="http://bank/home", title="Home", tree=tree, screenshot=b"\x89PNG-fake")


class FakeClient:
    def __init__(self, calls):
        self.calls, self.seen = calls, []

    def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
        self.seen.append(NS(system=system, messages=messages, tools=tools, force_tool=force_tool, max_tokens=max_tokens))
        return LLMResponse(text="hmm", tool_calls=self.calls)


def test_propose_sends_goal_names_and_screenshot_but_no_values():
    client = FakeClient([ToolCall("1", "act", {"action": "done"})])
    history = [(1, "click Target(text='Go')", "clicked")]
    raw = AgentLLM(client).propose("find the balance", ["member_id"], ["BANK_USER"], history, observation(), 2, 25)
    assert raw == {"action": "done"}

    call = client.seen[0]
    assert call.force_tool == "act" and call.tools == [ACT_TOOL] and call.system == SYSTEM_PROMPT
    message = call.messages[0]
    assert message.images == [b"\x89PNG-fake"]
    for expected in ("find the balance", "{{member_id}}", "{{secret:BANK_USER}}", "1. click Target(text='Go') -> clicked",
                     "this is step 2 of at most 25", "http://bank/home", "- button Go"):
        assert expected in message.text


def test_propose_can_skip_the_screenshot():
    client = FakeClient([ToolCall("1", "act", {"action": "done"})])
    AgentLLM(client, use_screenshot=False).propose("g", [], [], [], observation(), 1, 25)
    assert client.seen[0].messages[0].images == []


def test_propose_reports_a_reply_without_the_tool_call():
    with pytest.raises(NoActionError, match="did not call the act tool"):
        AgentLLM(FakeClient([])).propose("g", [], [], [], observation(), 1, 25)
    with pytest.raises(NoActionError):
        AgentLLM(FakeClient([ToolCall("1", "other_tool", {})])).propose("g", [], [], [], observation(), 1, 25)


def test_prompt_with_nothing_yet():
    text = build_prompt("g", [], [], [], observation(), 1, 25)
    assert "INPUTS you may type: none" in text and "(none yet)" in text


# ------------------------------------------------------------------- Recorder


def test_recorder_saves_after_every_step(tmp_path):
    recorder = Recorder("Find the balance!", ["member_id"], "http://bank/", "fake-model", tmp_path)
    assert recorder.path.parent == tmp_path and recorder.path.name.endswith("-find-the-balance.json")
    action = Action.model_validate({"action": "click", "target": {"role": "button", "name": "Go"}, "reason": "go"})
    recorder.add_step(1, "http://bank/a", "go", "ok", "clicked", action=action, candidates=[Target(css="b")])
    on_disk = json.loads(recorder.path.read_text(encoding="utf-8"))  # already saved, not yet finished
    assert on_disk["stop_reason"] is None and len(on_disk["steps"]) == 1
    assert on_disk["steps"][0]["action"] == {"action": "click", "target": {"role": "button", "name": "Go"}, "reason": "go"}
    assert on_disk["steps"][0]["target_candidates"] == [{"css": "b"}]

    recorder.add_step(2, "http://bank/a", "", "invalid", "bad", raw={"action": "click"})
    recorder.finish("done", {"balance": "$1"}, "all good")
    final = json.loads(recorder.path.read_text(encoding="utf-8"))
    assert final["inputs"] == ["member_id"] and final["outputs"] == {"balance": "$1"}
    assert final["stop_reason"] == "done" and final["steps"][1]["raw"] == {"action": "click"}
    assert not list(tmp_path.glob("*.tmp"))


# ------------------------------------------------------------------ the loop


class FakeSurface:
    """Stands in for a browser: records what it is asked to do."""

    def __init__(self, fail_on=None, goto_error=None):
        self.calls, self.fail_on, self.goto_error = [], fail_on, goto_error

    def goto(self, url):
        if self.goto_error:
            raise SurfaceError(self.goto_error)
        self.calls.append(("goto", url))

    def observe(self):
        return observation()

    def describe(self, target):
        return [Target(css="#fallback")]

    def _act(self, name, target, *rest):
        if self.fail_on == name:
            raise SurfaceError("No element matches it.")
        self.calls.append((name, target, *rest))

    def click(self, target):
        self._act("click", target)

    def type(self, target, text):
        self._act("type", target, text)

    def wait_for(self, target, timeout_ms=None):
        self._act("wait", target)

    def read(self, target):
        self._act("read", target)
        return "$12,450.75"


class ScriptedLLM:
    """Answers with a fixed list of actions (or exceptions) instead of calling an AI."""

    def __init__(self, script):
        self.script, self.histories = list(script), []

    def propose(self, goal, input_names, secret_names, history, observation, step, max_steps):
        self.histories.append(list(history))
        item = self.script.pop(0) if self.script else {"action": "click", "target": {"css": "a"}}
        if isinstance(item, Exception):
            raise item
        return item


def discover(tmp_path, script, surface=None, **options):
    values = options.pop("values", Values(inputs={"member_id": "1001"}, secrets=SECRETS))
    surface = surface or FakeSurface()
    llm = ScriptedLLM(script)
    recorder = Recorder("goal", values.input_names, "http://bank/", "fake", tmp_path)
    result = run_discovery("goal", surface, llm, recorder, values, "http://bank/", **options)
    return result, surface, llm, json.loads(recorder.path.read_text(encoding="utf-8"))


def type_step(css, text):
    return {"action": "type", "target": {"css": css}, "text": text, "reason": "fill in"}


def test_happy_path_records_placeholders_candidates_and_outputs(tmp_path):
    script = [
        type_step("input", "{{secret:BANK_USER}}"),
        type_step("input", "{{member_id}}"),
        {"action": "click", "target": {"role": "button", "name": "Search"}, "reason": "search"},
        {"action": "read", "target": {"text": "$"}, "output_name": "savings_balance", "reason": "read it"},
        {"action": "done", "text": "Balance read.", "reason": "finished"},
    ]
    result, surface, llm, recording = discover(tmp_path, script)

    assert (result.stop_reason, result.steps, result.message) == ("done", 5, "Balance read.")
    assert result.outputs == {"savings_balance": "$12,450.75"}
    assert surface.calls[0] == ("goto", "http://bank/")
    assert [c[0] for c in surface.calls[1:]] == ["type", "type", "click", "read"]
    assert surface.calls[1][2] == "{{secret:BANK_USER}}"  # the surface gets placeholders and swaps them itself
    assert [s["status"] for s in recording["steps"]] == ["ok"] * 5
    assert recording["steps"][0]["target_candidates"] == [{"css": "#fallback"}]
    assert recording["stop_reason"] == "done" and recording["outputs"] == result.outputs
    assert len(llm.histories[-1]) == 4 and "read savings_balance" in llm.histories[-1][3][2]


def test_literal_values_are_turned_back_into_placeholders(tmp_path):
    script = [type_step("input", "1001"), type_step("input", "teller-xyz"), {"action": "done"}]
    result, surface, _, recording = discover(tmp_path, script)
    assert [c[2] for c in surface.calls if c[0] == "type"] == ["{{member_id}}", "{{secret:BANK_USER}}"]
    text = json.dumps(recording)
    assert "teller-xyz" not in text and '"text": "1001"' not in text


def test_ask_human_stops_with_the_question(tmp_path):
    script = [{"action": "ask_human", "text": "Which account?", "reason": "unclear"}]
    result, _, _, recording = discover(tmp_path, script)
    assert (result.stop_reason, result.message, result.steps) == ("ask_human", "Which account?", 1)
    assert recording["message"] == "Which account?"


def test_stops_at_the_step_limit(tmp_path):
    result, surface, _, _ = discover(tmp_path, [], max_steps=3)  # the script endlessly clicks
    assert (result.stop_reason, result.steps) == ("max_steps", 3)
    assert [c[0] for c in surface.calls].count("click") == 3


def test_stops_at_the_time_limit(tmp_path):
    ticks = iter(range(0, 10_000, 100))  # every look at the clock is 100 "seconds" later
    result, _, _, recording = discover(tmp_path, [], max_seconds=250, clock=lambda: next(ticks))
    assert result.stop_reason == "timeout" and result.steps >= 1
    assert recording["stop_reason"] == "timeout"


def test_invalid_actions_are_fed_back_and_the_run_continues(tmp_path):
    script = [{"action": "click"}, NoActionError("no tool call"), {"action": "done"}]
    result, surface, llm, recording = discover(tmp_path, script)
    assert result.stop_reason == "done" and result.steps == 3
    assert [s["status"] for s in recording["steps"]] == ["invalid", "invalid", "ok"]
    assert recording["steps"][0]["raw"] == {"action": "click"}
    assert "INVALID" in llm.histories[1][0][2] and "needs: target" in llm.histories[1][0][2]
    assert not [c for c in surface.calls if c[0] == "click"]  # nothing ran


def test_surface_errors_are_recorded_and_shown_to_the_ai(tmp_path):
    script = [{"action": "click", "target": {"text": "Missing"}, "reason": "try"}, {"action": "done"}]
    result, _, llm, recording = discover(tmp_path, script, surface=FakeSurface(fail_on="click"))
    assert recording["steps"][0]["status"] == "error"
    assert llm.histories[1][0][2].startswith("ERROR: No element matches it.")
    assert result.stop_reason == "done"


def test_safety_can_block_an_action(tmp_path):
    script = [{"action": "click", "target": {"text": "Transfer"}, "reason": "go"}, {"action": "done"}]
    no = lambda action: safety.Verdict(False, "money movement needs approval")
    result, surface, llm, recording = discover(tmp_path, script, safety_check=no)
    assert recording["steps"][0]["status"] == "blocked"
    assert "BLOCKED by safety: money movement needs approval" in llm.histories[1][0][2]
    assert not [c for c in surface.calls if c[0] == "click"]


def test_safety_stub_allows_everything():
    assert safety.check(Action.model_validate({"action": "done"})).allowed


def test_llm_failure_stops_cleanly_and_keeps_the_recording(tmp_path):
    result, _, _, recording = discover(tmp_path, [{"action": "click", "target": {"text": "a"}}, RuntimeError("boom")])
    assert result.stop_reason == "error" and "RuntimeError: boom" in result.message
    assert recording["stop_reason"] == "error" and len(recording["steps"]) == 1


def test_cannot_open_the_start_page(tmp_path):
    result, _, llm, _ = discover(tmp_path, [], surface=FakeSurface(goto_error="connection refused"))
    assert result.stop_reason == "error" and "connection refused" in result.message
    assert result.steps == 0 and llm.histories == []


# ------------------------------------------- the whole thing, with a real browser


def test_full_run_against_the_real_bank(tmp_path, bank_url):
    """Sign on with secrets, look up a member by input, read two values; nothing real is leaked."""
    values = Values(inputs={"member_id": "1001"}, secrets={"BANK_USER": BANK_USER, "BANK_PASSWORD": BANK_PASSWORD})
    script = [
        type_step('input[name="user"]', "{{secret:BANK_USER}}"),
        type_step('input[name="pw"]', "{{secret:BANK_PASSWORD}}"),
        {"action": "click", "target": {"role": "button", "name": "Sign On"}, "reason": "sign on"},
        {"action": "wait", "target": {"role": "link", "name": "Sign Off"}, "reason": "signed on"},
        type_step('input[name="mid"]', "{{member_id}}"),
        {"action": "click", "target": {"role": "button", "name": "Search"}, "reason": "search"},
        {"action": "wait", "target": {"role": "heading", "name": "Member Details"}, "reason": "page loaded"},
        {"action": "read", "target": {"text": "Priya Sharma"}, "output_name": "member_name", "reason": "name"},
        {"action": "read", "target": {"text": "$12,450.75"}, "output_name": "savings_balance", "reason": "balance"},
        {"action": "done", "text": "Read the member's details.", "reason": "finished"},
    ]
    trees = []

    class SpyLLM(ScriptedLLM):
        def propose(self, goal, input_names, secret_names, history, observation, step, max_steps):
            trees.append(observation.tree)
            return super().propose(goal, input_names, secret_names, history, observation, step, max_steps)

    recorder = Recorder("look up a member", ["member_id"], bank_url, "fake", tmp_path)
    with BrowserSurface(timeout_ms=4000, values=values) as surface:
        result = run_discovery("look up a member", surface, SpyLLM(script), recorder, values, bank_url + "/")

    assert result.stop_reason == "done", result.message
    assert result.outputs == {"member_name": "Priya Sharma", "savings_balance": "$12,450.75"}

    recording_text = recorder.path.read_text(encoding="utf-8")
    recording = json.loads(recording_text)
    assert BANK_USER not in recording_text and BANK_PASSWORD not in recording_text
    assert "{{secret:BANK_USER}}" in recording_text and "{{member_id}}" in recording_text
    assert all(BANK_USER not in tree and BANK_PASSWORD not in tree for tree in trees)  # the AI never saw them

    first_look = trees[0]  # the sign-on page
    assert 'textbox "User name:"' in first_look
    assert 'css=input[name="pw"]' in first_look  # the unlabelled password box is listed
    search_step = next(s for s in recording["steps"] if s["action"].get("target") == {"role": "button", "name": "Search"})
    assert {"role": "button", "name": "Search"} in search_step["target_candidates"]  # fallbacks were stored
    assert any("css" in c for c in search_step["target_candidates"])
