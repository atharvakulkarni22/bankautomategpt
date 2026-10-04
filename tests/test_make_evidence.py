import importlib.util
import json
import socket
from argparse import Namespace
from pathlib import Path

import pytest

from bag.artifact import load_artifact, resolve_artifact_path
from bag.safety import load_safety_config

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("make_evidence", ROOT / "scripts" / "make_evidence.py")
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def options(**changes):
    values = dict(bank_port=5057, replay_member="1003", discovery_member="1001", artifacts_dir=Path("artifacts"))
    values.update(changes)
    return Namespace(**values)


def test_there_are_four_runs_in_a_fixed_order_each_with_a_one_line_claim():
    ids = [run_id for run_id, _, _ in script.RUNS]
    assert ids == ["01-discovery", "02-replay", "03-replay-faults", "04-handoff"]
    for _, expected, proves in script.RUNS:
        assert expected and proves and "\n" not in proves


def test_the_sample_artifact_is_valid_approved_and_ready_to_replay(tmp_path):
    artifact = script.sample_artifact(3)
    assert (artifact.metadata.name, artifact.metadata.version, artifact.metadata.status) == (script.ARTIFACT, 3, "approved")
    assert [i.name for i in artifact.inputs] == ["member_id"]
    assert [(o.name, o.type) for o in artifact.outputs] == [("member_name", "string"), ("savings_balance", "decimal")]
    assert [s.action for s in artifact.steps] == ["type", "type", "click", "wait", "type", "click", "wait", "read", "read"]


def test_seeding_the_sample_twice_makes_two_versions(tmp_path):
    first = script.seed_sample_artifact(tmp_path)
    second = script.seed_sample_artifact(tmp_path)
    assert (first.name, second.name) == (f"{script.ARTIFACT}.v1.yaml", f"{script.ARTIFACT}.v2.yaml")
    assert load_artifact(second).metadata.version == 2
    assert resolve_artifact_path(script.ARTIFACT, tmp_path) == second


def test_the_handoff_artifact_is_the_source_plus_one_step_that_can_never_be_done(tmp_path):
    script.seed_sample_artifact(tmp_path)
    path = script.make_handoff_artifact(tmp_path)
    source = load_artifact(resolve_artifact_path(script.ARTIFACT, tmp_path))
    handoff = load_artifact(path)
    assert (handoff.metadata.name, handoff.metadata.version, handoff.metadata.status) == (script.HANDOFF_ARTIFACT, 1, "approved")
    assert len(handoff.steps) == len(source.steps) + 1
    last = handoff.steps[-1]
    assert last.action == "wait" and last.locator.primary.text == script.IMPOSSIBLE_TEXT
    assert handoff.steps[:-1] == source.steps and handoff.outputs == source.outputs
    assert script.make_handoff_artifact(tmp_path).name == f"{script.HANDOFF_ARTIFACT}.v2.yaml"
    assert source.metadata.name == script.ARTIFACT


def test_the_safety_config_it_writes_keeps_the_shipped_rules_but_points_at_this_bank(tmp_path):
    evidence = tmp_path / "evidence"
    path = script.write_safety_config(5057, evidence, tmp_path)
    written, shipped = load_safety_config(path), load_safety_config(ROOT / "config" / "safety.yaml")
    assert written.allowed_urls == ["http://127.0.0.1:5057", "http://localhost:5057"]
    assert written.audit_log == evidence / "safety-decisions.jsonl"
    assert written.risky_button_names == shipped.risky_button_names and written.risky_actions == shipped.risky_actions


def test_each_run_gets_the_right_command(tmp_path):
    safety, interventions = tmp_path / "safety.yaml", tmp_path / "iv"
    commands = script.run_commands(options(), tmp_path / "evidence", safety, interventions)
    assert list(commands) == ["01-discovery", "02-replay", "03-replay-faults", "04-handoff"]
    for run_id, argv in commands.items():
        assert argv[argv.index("--run-id") + 1] == run_id
        assert argv[argv.index("--evidence-dir") + 1] == str(tmp_path / "evidence")
        assert argv[argv.index("--safety-config") + 1] == str(safety)

    discover = commands["01-discovery"]
    assert discover[0] == "discover" and "member_id=1001" in discover and "--headed" not in discover
    assert discover[discover.index("--start-url") + 1] == "http://127.0.0.1:5057"

    replay = commands["02-replay"]
    assert replay[:2] == ["replay", script.ARTIFACT] and "member_id=1003" in replay and "--takeover" not in replay

    faults = commands["03-replay-faults"]
    assert faults[faults.index("--start-url") + 1] == "http://127.0.0.1:5057/login?popup=1&slow=1"

    handoff = commands["04-handoff"]
    assert handoff[1] == script.HANDOFF_ARTIFACT and "--headed" in handoff and "--takeover" in handoff
    assert handoff[handoff.index("--interventions-dir") + 1] == str(interventions)


def test_the_index_starts_with_every_run_not_run_yet(tmp_path):
    text = script.render_index(tmp_path)
    assert text.startswith("# Evidence index")
    assert text.count("not run yet") == 4 and "](01-discovery/)" not in text
    for run_id, _, proves in script.RUNS:
        assert f"| {run_id} | {proves} |" in text
    assert "scripts/make_evidence.sh" in text and "scripts/make_evidence.ps1" in text


def test_the_index_links_a_run_once_it_has_a_result(tmp_path):
    (tmp_path / "02-replay").mkdir()
    (tmp_path / "02-replay" / "result.json").write_text(json.dumps({"status": "SUCCESS", "seconds": 3.1}), encoding="utf-8")
    text = script.render_index(tmp_path)
    assert "| [02-replay](02-replay/) |" in text and "`SUCCESS` in 3.1 s" in text
    assert text.count("not run yet") == 3


def test_a_skipped_discovery_is_said_plainly_and_not_linked(tmp_path):
    text = script.render_index(tmp_path, skipped=("01-discovery",))
    assert "| 01-discovery |" in text and "skipped (a sample artifact was used instead)" in text
    assert "](01-discovery/)" not in text


def test_index_only_rewrites_the_index_and_nothing_else(tmp_path):
    assert script.main(["--evidence-dir", str(tmp_path / "evidence"), "--index-only"]) == 0
    assert [p.name for p in (tmp_path / "evidence").iterdir()] == ["INDEX.md"]


def test_the_port_check_tells_open_from_closed():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert script.port_is_open(port) is True
    assert script.port_is_open(port) is False


def test_the_bank_port_defaults_to_5000_and_can_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("BANK_PORT", raising=False)
    assert script.parse_args([]).bank_port == 5000
    monkeypatch.setenv("BANK_PORT", "5057")
    assert script.parse_args([]).bank_port == 5057
    assert script.parse_args(["--bank-port", "6000"]).bank_port == 6000


def test_the_wrappers_just_hand_over_to_the_python_script():
    shell = (ROOT / "scripts" / "make_evidence.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts" / "make_evidence.ps1").read_text(encoding="utf-8")
    assert shell.startswith("#!/usr/bin/env sh") and 'exec "$PY" scripts/make_evidence.py "$@"' in shell
    assert "scripts\\make_evidence.py @args" in powershell and "exit $LASTEXITCODE" in powershell
    assert b"\r\n" not in (ROOT / "scripts" / "make_evidence.sh").read_bytes()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


def test_the_committed_index_lists_the_four_runs():
    text = (ROOT / "evidence" / "INDEX.md").read_text(encoding="utf-8")
    for run_id, _, proves in script.RUNS:
        assert run_id in text and proves in text
