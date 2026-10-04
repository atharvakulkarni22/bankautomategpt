import json
import re
import subprocess
import sys

import pytest
import test_agent
import test_handoff
import test_replay
from conftest import BANK_PASSWORD, BANK_USER
from test_replay import cli  # noqa: F401
from test_takeover_flow import broken_search_artifact, click_search

from bag.logging import RunLogError, RunLogger, make_run_id
from bag.replay import Replayer, prepare_run
from bag.safety import Redactor
from bag.handoff import HumanTakeover
from bag.surface import BrowserSurface, SurfaceTimeout, Values

SECRETS = {"BANK_USER": "teller-xyz"}


def make_logger(tmp_path, run_id=None, secrets=None, kind="replay", label="demo v1"):
    redactor = Redactor(Values(secrets=SECRETS if secrets is None else secrets))
    return RunLogger(kind, label, tmp_path / "evidence", run_id, redactor)


def lines(logger):
    return [json.loads(line) for line in logger.steps_path.read_text(encoding="utf-8").splitlines()]


def result_of(logger):
    return json.loads(logger.result_path.read_text(encoding="utf-8"))


def test_a_run_gets_its_own_folder_with_an_empty_steps_file(tmp_path):
    logger = make_logger(tmp_path, run_id="01-discovery")
    assert logger.directory == tmp_path / "evidence" / "01-discovery"
    assert logger.steps_path.read_text(encoding="utf-8") == ""
    assert not logger.screenshots_dir.exists()
    assert not logger.result_path.exists()


def test_each_step_is_one_numbered_json_line(tmp_path):
    logger = make_logger(tmp_path)
    logger.step("replay", "step", step=2, message="click ok via primary", attempts=1)
    logger.step("handoff", "transition", reason="paused")
    first, second = lines(logger)
    assert (first["seq"], first["source"], first["event"], first["step"]) == (1, "replay", "step", 2)
    assert first["message"] == "click ok via primary" and first["attempts"] == 1 and first["time"]
    assert (second["seq"], second["step"], second["reason"]) == (2, None, "paused")
    assert len(logger.steps_path.read_text(encoding="utf-8").splitlines()) == 2


def test_the_default_run_id_says_when_and_what(tmp_path):
    logger = make_logger(tmp_path)
    assert re.fullmatch(r"\d{8}-\d{6}-replay-demo-v1", logger.run_id)
    assert make_run_id("discovery", "Log in & read the balance!").endswith("-discovery-log-in-read-the-balance")


def test_two_automatic_ids_in_the_same_second_do_not_collide(tmp_path):
    first = make_logger(tmp_path)
    first.step("replay", "step")
    second = make_logger(tmp_path)
    assert second.run_id != first.run_id and second.run_id.startswith(first.run_id)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "a b", "..", ".hidden", "x..y"])
def test_unsafe_run_ids_are_refused(tmp_path, bad):
    with pytest.raises(RunLogError, match="not a usable run id"):
        make_logger(tmp_path, run_id=bad)
    assert not (tmp_path / "evidence").exists() or not list((tmp_path / "evidence").iterdir())


def test_an_explicit_run_id_that_is_taken_is_refused(tmp_path):
    first = make_logger(tmp_path, run_id="02-replay")
    first.step("replay", "step")
    with pytest.raises(RunLogError, match="already has files"):
        make_logger(tmp_path, run_id="02-replay")


def test_an_empty_folder_can_be_reused(tmp_path):
    (tmp_path / "evidence" / "02-replay").mkdir(parents=True)
    assert make_logger(tmp_path, run_id="02-replay").run_id == "02-replay"


def test_every_line_and_the_result_are_redacted(tmp_path):
    logger = make_logger(tmp_path)
    logger.step("replay", "failure", message="user teller-xyz, account 1234567890123456",
                events=[{"value": "teller-xyz"}, {"nested": ["9990-0000-1001"]}])
    logger.finish("FAILURE", message="again teller-xyz", account="1234 5678 9012 3456")
    text = logger.steps_path.read_text(encoding="utf-8") + logger.result_path.read_text(encoding="utf-8")
    for leaked in ("teller-xyz", "1234567890123456", "9990-0000-1001", "1234 5678 9012 3456"):
        assert leaked not in text
    assert "{{secret:BANK_USER}}" in text and "XXXXXXXXXXXX3456" in text and "XXXX-XXXX-1001" in text


def test_the_screenshots_folder_appears_only_with_the_first_screenshot(tmp_path):
    logger = make_logger(tmp_path)
    assert not logger.screenshots_dir.exists()
    first = logger.screenshot("failure-step3", b"\x89PNG-one")
    second = logger.screenshot("failure-step3", b"\x89PNG-two")
    assert (first.name, second.name) == ("failure-step3.png", "failure-step3-2.png")
    assert first.read_bytes() == b"\x89PNG-one" and second.read_bytes() == b"\x89PNG-two"


def test_a_screenshot_name_cannot_escape_the_folder(tmp_path):
    logger = make_logger(tmp_path)
    path = logger.screenshot("../../evil", b"png")
    assert path.parent == logger.screenshots_dir and path.exists()
    assert not (tmp_path / "evil.png").exists()


def test_finish_writes_the_result_with_what_the_caller_adds(tmp_path):
    from decimal import Decimal
    from pathlib import Path

    logger = make_logger(tmp_path)
    logger.step("replay", "step")
    logger.screenshot("failure-step1", b"png")
    path = logger.finish("FAILURE", artifact="demo v1", outputs={"balance": Decimal("1.50")}, where=Path("a/b"))
    result = result_of(logger)
    assert path == logger.result_path
    assert (result["run_id"], result["kind"], result["label"], result["status"]) == (logger.run_id, "replay", "demo v1", "FAILURE")
    assert result["lines_logged"] == 1 and result["screenshots"] == ["failure-step1.png"]
    assert result["started_at"] <= result["finished_at"] and result["seconds"] >= 0
    assert result["outputs"] == {"balance": "1.50"} and result["where"].replace("\\", "/") == "a/b"
    assert not list(logger.directory.glob("*.tmp"))


def fake_replay(tmp_path, artifact, surface=None, run_log=None, **options):
    if surface is None:
        surface = test_replay.FakeSurface()
        surface.present = {test_replay.GO}
    clock = test_replay.FakeClock()
    run_log = run_log or make_logger(tmp_path)
    result = test_replay.replayer(artifact, surface, clock, tmp_path, run_log=run_log, **options).run()
    return result, run_log, surface


READ_ARTIFACT_STEPS = [
    {"action": "type", "locator": test_replay.loc({"css": "box"}), "text": "{{member_id}}"},
    {"action": "click", "locator": test_replay.loc({"role": "button", "name": "Go"})},
    {"action": "read", "locator": test_replay.loc({"css": "value"}), "output_name": "balance"},
]


def readable_artifact(**extra):
    return test_replay.make_artifact(
        READ_ARTIFACT_STEPS, inputs=[{"name": "member_id"}], outputs=[{"name": "balance", "type": "decimal"}], **extra
    )


def readable_surface():
    from bag.surface import Target

    surface = test_replay.FakeSurface()
    surface.present = {test_replay.GO, Target(css="box"), Target(css="value")}
    surface.reads = {Target(css="value"): "$12,450.75"}
    return surface


def test_a_successful_replay_logs_each_step_and_makes_no_screenshots(tmp_path):
    result, run_log, _ = fake_replay(tmp_path, readable_artifact(), readable_surface())
    assert result.status == "SUCCESS"
    entries = lines(run_log)
    assert entries[0]["event"] == "start" and entries[0]["artifact"] == "demo v1" and entries[0]["url"]
    steps = [e for e in entries if e["event"] == "step"]
    assert [(e["step"], e["message"]) for e in steps] == [
        (1, "type ok via primary"), (2, "click ok via primary"), (3, "read ok via primary")]
    assert {e["source"] for e in entries} == {"replay"}
    final = result_of(run_log)
    assert final["status"] == "SUCCESS" and final["artifact"] == "demo v1" and final["outputs"] == {"balance": "12450.75"}
    assert final["failure"] is None and final["screenshots"] == [] and final["interventions"] == []
    assert not run_log.screenshots_dir.exists()


def test_a_failed_replay_saves_its_screenshot_in_the_run_folder(tmp_path):
    surface = test_replay.FakeSurface()
    surface.present = set()
    result, run_log, _ = fake_replay(tmp_path, test_replay.make_artifact([test_replay.click_go]), surface, timeout_s=2)
    assert result.status == "FAILURE"
    shot = run_log.screenshots_dir / "failure-step1.png"
    assert shot.read_bytes().startswith(b"\x89PNG") and result.failure.screenshot == shot
    failure = [e for e in lines(run_log) if e["event"] == "failure"][0]
    assert (failure["step"], failure["error"], failure["screenshot"]) == (1, "LocatorNotFound", "failure-step1.png")
    assert "nothing matched" in failure["observed"]
    final = result_of(run_log)
    assert final["status"] == "FAILURE" and final["failure"]["error"] == "LocatorNotFound"
    assert final["failure"]["screenshot"] == "failure-step1.png" and final["screenshots"] == ["failure-step1.png"]


def test_a_business_outcome_is_logged_without_a_screenshot(tmp_path):
    surface = test_replay.FakeSurface()
    surface.present = {test_replay.GO}
    surface.on_click[test_replay.GO] = lambda s: s.texts.add("No member found")
    artifact = test_replay.make_artifact(
        [test_replay.click_go], known_outcomes=[{"text": "No member found", "outcome": "NOT_FOUND"}]
    )
    result, run_log, _ = fake_replay(tmp_path, artifact, surface)
    final = result_of(run_log)
    assert final["status"] == "BUSINESS_OUTCOME" and final["outcome_code"] == "NOT_FOUND" and final["failure"] is None
    assert not run_log.screenshots_dir.exists() and final["screenshots"] == []


def test_fallbacks_retries_and_popups_each_leave_a_line(tmp_path):
    surface = test_replay.FakeSurface()
    surface.present = {test_replay.FALLBACK, test_replay.OK}
    surface.texts = {test_replay.POPUP_TEXT}
    surface.on_click[test_replay.OK] = lambda s: s.texts.discard(test_replay.POPUP_TEXT)
    surface.errors[("click", test_replay.FALLBACK)] = [SurfaceTimeout("not ready")]
    result, run_log, _ = fake_replay(tmp_path, test_replay.popup_artifact(), surface)
    assert result.status == "SUCCESS"
    events = [e["event"] for e in lines(run_log)]
    for expected in ("interruption", "fallback", "retry", "step"):
        assert expected in events
    retry = next(e for e in lines(run_log) if e["event"] == "retry")
    assert retry["step"] == 1 and "Retrying in 0.5s" in retry["message"]


def test_replay_lines_are_redacted_in_the_run_folder(tmp_path):
    surface = test_replay.FakeSurface()
    surface.present = {test_replay.GO}
    surface.url = "http://bank/home?account=1234567890123456&user=teller-xyz"
    surface.errors[("click", test_replay.GO)] = [SurfaceTimeout("could not click for account 9990-0000-1001")]
    step = {"action": "click", "locator": test_replay.loc(test_replay.BUTTON), "expected": {"url_contains": "/nowhere"}}
    redactor = Redactor(Values(secrets=SECRETS))
    run_log = make_logger(tmp_path)
    clock = test_replay.FakeClock()
    result = test_replay.replayer(
        test_replay.make_artifact([step]), surface, clock, tmp_path, redactor=redactor, run_log=run_log
    ).run()
    assert result.status == "FAILURE"
    text = run_log.steps_path.read_text(encoding="utf-8") + run_log.result_path.read_text(encoding="utf-8")
    for leaked in ("1234567890123456", "teller-xyz", "9990-0000-1001"):
        assert leaked not in text
    assert "XXXXXXXXXXXX3456" in text and "{{secret:BANK_USER}}" in text


def agent_run(tmp_path, script, run_log=None, **options):
    run_log = run_log or make_logger(tmp_path, kind="discovery", label="goal")
    outcome = test_agent.discover(tmp_path, script, run_log=run_log, **options)
    return outcome, run_log


def test_a_discovery_run_logs_every_step_and_its_stop(tmp_path):
    script = [
        {"action": "click"},
        {"action": "click", "target": {"text": "Go"}, "reason": "go"},
        {"action": "done", "text": "finished"},
    ]
    (result, surface, llm, recording), run_log = agent_run(tmp_path, script)
    assert result.stop_reason == "done"
    entries = lines(run_log)
    assert entries[0]["event"] == "start" and entries[0]["goal"] == "goal" and entries[0]["inputs"] == ["member_id"]
    steps = [e for e in entries if e["event"] == "step"]
    assert [(e["step"], e["status"]) for e in steps] == [(1, "invalid"), (2, "ok"), (3, "ok")]
    assert steps[1]["action"] == "click Target(text='Go')" and steps[1]["candidates"] == 1
    assert entries[-1]["event"] == "stop" and entries[-1]["reason"] == "done" and entries[-1]["screenshot"] is None
    final = result_of(run_log)
    assert final["status"] == "done" and final["steps"] == 3 and final["recording"].endswith(".json")
    assert final["screenshots"] == [] and not run_log.screenshots_dir.exists()
    assert {e["source"] for e in entries} == {"agent"}


@pytest.mark.parametrize(
    "script, options, reason",
    [
        ([], {"max_steps": 2}, "max_steps"),
        ([{"action": "ask_human", "text": "Which account?", "reason": "unclear"}], {}, "ask_human"),
    ],
)
def test_a_discovery_run_that_does_not_finish_keeps_a_screenshot(tmp_path, script, options, reason):
    (result, *_), run_log = agent_run(tmp_path, script, **options)
    assert result.stop_reason == reason
    shot = run_log.screenshots_dir / f"stopped-{reason}.png"
    assert shot.exists()
    assert lines(run_log)[-1]["screenshot"] == shot.name
    assert result_of(run_log)["status"] == reason and result_of(run_log)["screenshots"] == [shot.name]


def test_a_blocked_discovery_step_is_logged_as_blocked(tmp_path):
    from bag.safety import Decision

    from conftest import StubGuard

    script = [{"action": "click", "target": {"text": "Transfer"}, "reason": "go"}, {"action": "done"}]
    (result, *_), run_log = agent_run(tmp_path, script, guard=StubGuard(Decision.BLOCK, "needs approval"))
    blocked = [e for e in lines(run_log) if e.get("status") == "blocked"]
    assert len(blocked) == 1 and "needs approval" in blocked[0]["result"]


def test_discovery_lines_are_redacted_in_the_run_folder(tmp_path):
    class Reading(test_agent.FakeSurface):
        def read(self, target):
            return "Account 9990000010011234 held by teller-xyz"

    script = [{"action": "read", "target": {"text": "acct"}, "output_name": "account", "reason": "read it"},
              {"action": "done", "text": "the account was 1234567890123456"}]
    (result, *_), run_log = agent_run(tmp_path, script, surface=Reading())
    text = run_log.steps_path.read_text(encoding="utf-8") + run_log.result_path.read_text(encoding="utf-8")
    for leaked in ("9990000010011234", "teller-xyz", "1234567890123456"):
        assert leaked not in text
    assert "{{secret:BANK_USER}}" in text and "XXXXXXXXXXXX1234" in text


def handoff_run(tmp_path, answers=("",), events=(), **go_overrides):
    run_log = make_logger(tmp_path)
    takeover, surface, _, printed = test_handoff.make_takeover(
        tmp_path, answers=list(answers), run_log=run_log, redactor=Redactor(Values(secrets=SECRETS))
    )
    surface.events = list(events)
    result = test_handoff.go(takeover, **go_overrides)
    return result, run_log, takeover


def test_a_handoff_logs_every_transition_the_intervention_and_what_the_human_did(tmp_path):
    events = [{"t": 1, "type": "click", "role": "button", "name": "Search", "css": "input"}]
    result, run_log, _ = handoff_run(tmp_path, events=events)
    entries = lines(run_log)
    assert {e["source"] for e in entries} == {"handoff"}
    transitions = [(e["from"], e["to"]) for e in entries if e["event"] == "transition"]
    assert transitions == [("AUTOMATION", "PAUSED_FOR_HUMAN"), ("PAUSED_FOR_HUMAN", "HUMAN"), ("HUMAN", "AUTOMATION")]
    assert {e["step"] for e in entries} == {6}
    intervention = next(e for e in entries if e["event"] == "intervention")
    assert intervention["step"] == 6 and intervention["intervention"]["run_id"] == run_log.run_id
    assert intervention["intervention"]["status"] == "waiting" and intervention["intervention"]["error"] == "LocatorNotFound"
    finished = next(e for e in entries if e["event"] == "finished")
    assert (finished["status"], finished["via"], finished["event_count"]) == ("resumed", "enter", 1)
    assert finished["summary"] == 'click button "Search"' and finished["events"][0]["name"] == "Search"
    assert [e["event"] for e in entries].index("intervention") < [e["event"] for e in entries].index("finished")


def test_the_handoff_screenshot_goes_into_the_run_folder_not_beside_the_intervention(tmp_path):
    result, run_log, _ = handoff_run(tmp_path)
    assert result.intervention.screenshot == run_log.screenshots_dir / "handoff-step6.png"
    assert result.intervention.screenshot.read_bytes().startswith(b"\x89PNG")
    assert not list((tmp_path / "iv").glob("*.png"))
    assert json.loads(result.intervention.path.read_text(encoding="utf-8"))["run_id"] == run_log.run_id


def test_a_human_who_gives_up_is_logged(tmp_path):
    result, run_log, takeover = handoff_run(tmp_path, answers=["q"])
    gave_up = [e for e in lines(run_log) if e["event"] == "gave_up"]
    assert len(gave_up) == 1 and "the human gave up" in gave_up[0]["reason"]
    assert [e["status"] for e in lines(run_log) if e["event"] == "finished"] == ["aborted"]


def test_handoff_lines_are_redacted_in_the_run_folder(tmp_path):
    events = [{"t": 1, "type": "input", "name": "User", "css": "i", "value": "teller-xyz"}]
    result, run_log, _ = handoff_run(tmp_path, events=events, reason="user teller-xyz had account 9990-0000-1001")
    text = run_log.steps_path.read_text(encoding="utf-8")
    for leaked in ("teller-xyz", "9990-0000-1001"):
        assert leaked not in text
    assert "{{secret:BANK_USER}}" in text and "XXXX-XXXX-1001" in text


def test_a_real_takeover_run_leaves_one_complete_evidence_folder(bank_url, tmp_path):
    artifact = broken_search_artifact()
    secrets = Values(secrets=test_replay.REAL_SECRETS)
    values = Values(inputs=prepare_run(artifact, {"member_id": "1001"}, secrets.secret_names), secrets=test_replay.REAL_SECRETS)
    redactor = Redactor(values)
    run_log = RunLogger("replay", "takeover", tmp_path / "evidence", "t1", redactor)
    with BrowserSurface(timeout_ms=2500, values=values) as surface:
        takeover = HumanTakeover(
            surface, directory=tmp_path / "iv", redactor=redactor, require_headed=False, run_log=run_log,
            enter_check=lambda: click_search(surface, 1) or "",
        )
        result = Replayer(
            artifact, surface, guard=test_replay.real_guard(bank_url), redactor=redactor, handoff=takeover,
            run_log=run_log, start_url=bank_url + "/login", timeout_s=1.5,
        ).run()
    assert result.status == "SUCCESS", result.failure

    assert sorted(p.name for p in run_log.directory.iterdir()) == ["result.json", "screenshots", "steps.jsonl"]
    assert [p.name for p in run_log.screenshots_dir.iterdir()] == ["handoff-step6.png"]
    entries = lines(run_log)
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
    assert {"replay", "handoff"} <= {e["source"] for e in entries}
    final = result_of(run_log)
    assert final["status"] == "SUCCESS" and final["failure"] is None and len(final["interventions"]) == 1
    assert final["outputs"] == {"member_name": "Priya Sharma", "savings_balance": "12450.75"}
    text = run_log.steps_path.read_text(encoding="utf-8") + run_log.result_path.read_text(encoding="utf-8")
    assert BANK_USER not in text and BANK_PASSWORD not in text


def test_the_package_can_be_run_as_a_module():
    done = subprocess.run([sys.executable, "-m", "bag", "--help"], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and "Bank Automate GPT" in done.stdout


def test_the_logger_module_pulls_in_no_llm_code():
    code = """
import sys
import bag.logging
loaded = [m for m in sys.modules if m.startswith(('bag.llm', 'bag.agent', 'anthropic', 'openai', 'google.genai'))]
sys.exit(1 if loaded else 0)
"""
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_bag_replay_makes_an_evidence_folder(cli):
    result = cli("member-balance", "--input", "member_id=1002", "--run-id", "r1")
    assert result.exit_code == 0, result.output
    folder = cli.evidence / "r1"
    assert f"Evidence saved to {folder}" in result.output
    final = result_of_path(folder)
    assert final["status"] == "SUCCESS" and final["outputs"]["member_name"] == "Marcus Webb"
    entries = [json.loads(line) for line in (folder / "steps.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entries[0]["event"] == "start" and len([e for e in entries if e["event"] == "step"]) == 9
    assert not (folder / "screenshots").exists()


def test_bag_replay_refuses_a_run_id_that_is_taken(cli):
    assert cli("member-balance", "--input", "member_id=1002", "--run-id", "r1").exit_code == 0
    again = cli("member-balance", "--input", "member_id=1002", "--run-id", "r1")
    assert again.exit_code == 1 and "already has files" in again.output


def test_bag_replay_names_the_folder_itself_when_no_run_id_is_given(cli):
    assert cli("member-balance", "--input", "member_id=1002").exit_code == 0
    (folder,) = cli.evidence.iterdir()
    assert re.fullmatch(r"\d{8}-\d{6}-replay-member-balance-v1", folder.name)


def test_a_replay_that_is_refused_leaves_no_evidence_folder(cli):
    assert cli("member-balance", "--input", "member_id=12").exit_code == 1
    assert not cli.evidence.exists()


def result_of_path(folder):
    return json.loads((folder / "result.json").read_text(encoding="utf-8"))
