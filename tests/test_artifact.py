import json

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from lba.artifact import (
    Artifact,
    ArtifactError,
    BuildError,
    Input,
    Locator,
    Output,
    approve_artifact,
    build_artifact,
    list_artifacts,
    load_artifact,
    load_recording,
    next_version,
    resolve_artifact_path,
    save_artifact,
)
from lba.cli import app
from lba.surface import Target, Values

SECRETS = {"BANK_USER": "teller-xyz", "BANK_PASSWORD": "hunter2-pw"}


# ------------------------------------------------------------------ test data


def action(kind, target=None, text=None, output_name=None, reason=""):
    data = {"action": kind}
    if target is not None:
        data["target"] = target
    if text is not None:
        data["text"] = text
    if output_name is not None:
        data["output_name"] = output_name
    if reason:
        data["reason"] = reason
    return data


def recorded(index, act, status="ok", url="http://127.0.0.1:5000/home", candidates=(), reason="because", result="ok"):
    step = {"index": index, "time": "t", "url": url, "reason": reason, "action": act,
            "target_candidates": list(candidates), "status": status, "result": result}
    if act is None:
        step["raw"] = {"action": "click"}
    return step


def good_recording():
    login, home = "http://127.0.0.1:5000/login", "http://127.0.0.1:5000/home"
    return {
        "version": 1, "goal": "Look up a member's balance", "inputs": ["member_id"],
        "start_url": "http://127.0.0.1:5000", "model": "fake", "stop_reason": "done",
        "outputs": {"member_name": "Priya Sharma", "savings_balance": "$12,450.75"}, "steps": [
            recorded(1, action("type", {"css": 'input[name="user"]'}, "{{secret:BANK_USER}}"), url=login),
            recorded(2, action("type", {"css": 'input[name="pw"]'}, "{{secret:BANK_PASSWORD}}"), url=login),
            recorded(3, action("click", {"role": "button", "name": "Sign On"}), url=login),
            recorded(4, action("wait", {"role": "link", "name": "Sign Off"}), url=home),
            recorded(5, action("type", {"css": 'input[name="mid"]'}, "{{member_id}}"), url=home),
            recorded(6, action("click", {"role": "button", "name": "Search"}, reason="search"), url=home,
                     candidates=[{"css": "body > a"}, {"text": "Search", "exact": True}, {"role": "button", "name": "Search"}]),
            recorded(7, action("wait", {"role": "heading", "name": "Member Details"}), url=home),
            recorded(8, action("read", {"label": "Name"}, output_name="member_name", reason="the name"),
                     url="http://127.0.0.1:5000/member/1001"),
            recorded(9, action("read", {"label": "Savings Balance"}, output_name="savings_balance"),
                     url="http://127.0.0.1:5000/member/1001"),
            recorded(10, action("done", reason="finished"), url="http://127.0.0.1:5000/member/1001"),
        ],
    }


def real_timed_out_recording():
    """Shaped like a real run (gemini flash-lite, 2026-10-04) that never got past typing and timed out."""
    login = "http://127.0.0.1:5000/login"
    bad = "INVALID ACTION: Value error, A Target needs exactly one of role, label, text, css (got ['role', 'text'])."
    return {
        "version": 1, "goal": "Log in and find the savings balance for the member", "inputs": ["member_id"],
        "start_url": "http://127.0.0.1:5000", "model": "gemini-3.5-flash-lite", "stop_reason": "timeout",
        "outputs": {}, "steps": [
            recorded(1, None, "invalid", login, result=bad),
            recorded(2, action("type", {"role": "textbox", "name": "User name:"}, "{{secret:BANK_USER}}"), url=login,
                     candidates=[{"role": "textbox", "name": "User name:"}, {"label": "User name:"}, {"css": 'input[name="user"]'}]),
            recorded(3, action("type", {"css": 'input[name="pw"]'}, "{{secret:BANK_PASSWORD}}"), url=login,
                     candidates=[{"css": 'input[name="pw"]'}]),
            recorded(4, None, "invalid", login, result=bad),
            recorded(5, None, "invalid", login, result=bad),
            recorded(6, action("type", {"css": 'input[name="pw"]'}, "{{secret:BANK_PASSWORD}}"), url=login,
                     candidates=[{"css": 'input[name="pw"]'}]),
            recorded(7, None, "invalid", login, result=bad),
        ],
    }


def build(recording=None, **options):
    options.setdefault("values", Values(secrets=SECRETS))
    return build_artifact(recording or good_recording(), name="member-balance", **options)


def minimal_artifact(**changes):
    data = {
        "metadata": {"name": "demo", "version": 1, "app": "Bank", "start_url": "http://x/"},
        "inputs": [{"name": "member_id"}],
        "steps": [
            {"action": "type", "locator": {"primary": {"css": "input"}}, "text": "{{member_id}}"},
            {"action": "read", "locator": {"primary": {"label": "Name"}}, "output_name": "member_name"},
        ],
        "outputs": [{"name": "member_name"}],
    }
    data.update(changes)
    return data


# ---------------------------------------------------------------------- schema


def test_minimal_artifact_is_valid():
    artifact = Artifact.model_validate(minimal_artifact())
    assert artifact.metadata.status == "draft"
    assert artifact.steps[0].locator.ordered() == [Target(css="input")]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"steps": []}, "at least 1"),
        ({"steps": [{"action": "type", "locator": {"primary": {"css": "a"}}}]}, "needs text"),
        ({"steps": [{"action": "click", "locator": {"primary": {"css": "a"}}, "text": "x"}]}, "must not have text"),
        ({"steps": [{"action": "read", "locator": {"primary": {"css": "a"}}}]}, "needs output_name"),
        ({"steps": [{"action": "click", "locator": {"primary": {"css": "a"}}, "output_name": "x"}]}, "must not have output_name"),
        ({"steps": [{"action": "type", "locator": {"primary": {"css": "a"}}, "text": "{{nope}}"}]}, "no input has that name"),
        ({"outputs": [{"name": "member_name"}, {"name": "extra"}]}, "never read"),
        ({"outputs": []}, "no output has that name"),
        ({"inputs": [{"name": "a"}, {"name": "a"}]}, "same name"),
        ({"inputs": [{"name": "bad name"}]}, "letters, digits"),
        ({"inputs": [{"name": "x", "pattern": "("}]}, "regular expression"),
        ({"success_check": {"outputs_present": ["ghost"]}}, "does not exist"),
        ({"known_outcomes": [{"text": "x", "outcome": "not found"}]}, "CAPITALS"),
        ({"steps": [{"action": "fly", "locator": {"primary": {"css": "a"}}}]}, "Input should be"),
        ({"surprise": 1}, "Extra inputs"),
        ({"steps": [{"action": "click", "locator": {"primary": {"css": "a", "text": "b"}}}]}, "exactly one"),
    ],
)
def test_invalid_artifacts_are_rejected(change, message):
    with pytest.raises(ValidationError, match=message):
        Artifact.model_validate(minimal_artifact(**change))


def test_secret_placeholders_are_allowed_in_steps():
    steps = [{"action": "type", "locator": {"primary": {"css": "a"}}, "text": "{{secret:BANK_PASSWORD}}"}]
    assert Artifact.model_validate(minimal_artifact(steps=steps + minimal_artifact()["steps"]))


def test_metadata_name_must_be_file_safe():
    bad = minimal_artifact()
    bad["metadata"]["name"] = "Has Spaces/../x"
    with pytest.raises(ValidationError, match="lowercase"):
        Artifact.model_validate(bad)


def test_input_check_validates_and_normalises():
    assert Input(name="id", pattern="[0-9]{4}").check(" 1001 ") == "1001"
    with pytest.raises(ValueError, match="pattern"):
        Input(name="id", pattern="[0-9]{4}").check("12")
    assert Input(name="n", type="int").check("007") == "7"
    with pytest.raises(ValueError, match="int"):
        Input(name="n", type="int").check("1.5")
    assert Input(name="amount", type="decimal").check("12.50") == "12.50"
    for bad in ("abc", "NaN", "inf"):
        with pytest.raises(ValueError, match="decimal"):
            Input(name="amount", type="decimal").check(bad)
    assert Input(name="s").check("anything goes") == "anything goes"


def test_output_parse_cleans_values_without_echoing_them():
    assert Output(name="b", type="decimal").parse("$12,450.75") == "12450.75"
    assert Output(name="b", type="decimal").parse(" -$5.00 ") == "-5.00"
    assert Output(name="n", type="int").parse("1,234") == "1234"
    assert Output(name="s").parse("  Priya Sharma ") == "Priya Sharma"
    assert Output(name="b", type="decimal", extract=r"Balance: (.*)").parse("Balance: $5.00") == "5.00"
    assert Output(name="b", extract=r"\d+").parse("abc 42 def") == "42"  # no group: whole match
    with pytest.raises(ValueError) as error:
        Output(name="b", type="decimal").parse("secret-customer-text")
    assert "secret-customer-text" not in str(error.value)
    with pytest.raises(ValueError, match="did not match"):
        Output(name="b", extract=r"Balance: (.*)").parse("nothing here")
    with pytest.raises(ValidationError, match="regular expression"):
        Output(name="b", extract="(")


# --------------------------------------------------------------------- builder


def test_builds_a_valid_draft_from_a_good_recording():
    result = build()
    artifact = result.artifact
    assert (artifact.metadata.name, artifact.metadata.version, artifact.metadata.status) == ("member-balance", 1, "draft")
    assert artifact.metadata.description == "Look up a member's balance"
    assert artifact.metadata.start_url == "http://127.0.0.1:5000"
    assert [s.action for s in artifact.steps] == ["type", "type", "click", "wait", "type", "click", "wait", "read", "read"]
    assert [i.name for i in artifact.inputs] == ["member_id"]
    assert [(o.name, o.type) for o in artifact.outputs] == [("member_name", "string"), ("savings_balance", "decimal")]
    assert artifact.success_check.outputs_present == ["member_name", "savings_balance"]
    assert result.warnings == []  # nothing odd about this recording


def test_only_successful_steps_are_kept():
    recording = good_recording()
    recording["steps"].insert(3, recorded(99, None, "invalid", result="INVALID"))
    recording["steps"].insert(3, recorded(98, action("click", {"text": "Missing"}), "error", result="ERROR: nope"))
    recording["steps"].insert(3, recorded(97, action("click", {"text": "Blocked"}), "blocked", result="BLOCKED"))
    result = build(recording)
    assert len(result.artifact.steps) == 9  # same as before: the three bad ones left out
    assert "Left out 3 step(s) that failed or were invalid." in result.warnings


def test_locators_are_ordered_role_label_text_css_without_repeats():
    recording = good_recording()
    messy = [{"css": "body > a"}, {"text": "Search", "exact": True}, {"label": "Go"}, {"role": "button", "name": "Search"}]
    recording["steps"][5] = recorded(6, action("click", {"css": "body > a"}, reason="search"), candidates=messy)
    locator = build(recording).artifact.steps[5].locator
    assert locator.primary == Target(role="button", name="Search")
    assert locator.fallbacks == [Target(label="Go"), Target(text="Search", exact=True), Target(css="body > a")]
    assert locator.why == "because"


def test_a_role_without_a_name_ranks_below_text():
    recording = good_recording()
    recording["steps"][5] = recorded(6, action("click", {"role": "textbox"}),
                                     candidates=[{"css": "a"}, {"text": "Go"}])
    assert build(recording).artifact.steps[5].locator.ordered() == [Target(text="Go"), Target(role="textbox"), Target(css="a")]


def test_literal_values_become_placeholders():
    recording = good_recording()
    recording["steps"][0] = recorded(1, action("type", {"css": "input"}, "teller-xyz"), reason="user is teller-xyz")
    recording["steps"][4] = recorded(5, action("type", {"css": "input"}, "1001"))
    artifact = build(recording, values=Values(inputs={"member_id": "1001"}, secrets=SECRETS)).artifact
    assert artifact.steps[0].text == "{{secret:BANK_USER}}"
    assert artifact.steps[0].locator.why == "user is {{secret:BANK_USER}}"
    assert artifact.steps[4].text == "{{member_id}}"
    dumped = json.dumps(artifact.model_dump(mode="json"))
    assert "teller-xyz" not in dumped and "hunter2-pw" not in dumped


def test_secrets_stay_as_secret_placeholders():
    texts = [s.text for s in build().artifact.steps if s.action == "type"]
    assert texts == ["{{secret:BANK_USER}}", "{{secret:BANK_PASSWORD}}", "{{member_id}}"]


def test_defaults_for_popup_and_not_found_are_added():
    artifact = build().artifact
    popup = artifact.known_interruptions[0]
    assert popup.text == "System maintenance notice" and popup.action == "click"
    assert popup.locator.primary == Target(role="button", name="OK")
    assert popup.locator.fallbacks == [Target(text="OK", exact=True)]
    assert [(o.text, o.outcome) for o in artifact.known_outcomes] == [("No member found", "NOT_FOUND")]


def test_expected_state_uses_the_new_path_but_never_an_id():
    steps = build().artifact.steps
    assert steps[2].expected.url_contains == "/home"  # sign on moved /login -> /home
    assert steps[0].expected is None and steps[5].expected is None  # address did not change
    assert steps[6].expected.url_contains == "/member"  # /member/1001 is cut before the id


def test_inputs_are_found_from_placeholders_too():
    recording = good_recording()
    recording["inputs"] = []  # the recording forgot to declare it
    assert [i.name for i in build(recording).artifact.inputs] == ["member_id"]


def test_repeated_identical_typing_is_merged():
    result = build(real_timed_out_recording())
    steps = result.artifact.steps
    assert [(s.action, s.text) for s in steps] == [("type", "{{secret:BANK_USER}}"), ("type", "{{secret:BANK_PASSWORD}}")]


def test_a_real_timed_out_recording_still_builds_a_draft_with_warnings():
    result = build(real_timed_out_recording())
    assert result.artifact.metadata.status == "draft"
    assert result.artifact.steps[0].locator.ordered() == [
        Target(role="textbox", name="User name:"), Target(label="User name:"), Target(css='input[name="user"]')]
    assert "The recording stopped with 'timeout', not 'done', so the artifact may be incomplete." in result.warnings
    assert "Left out 4 step(s) that failed or were invalid." in result.warnings
    assert "Merged 1 repeated 'type' step(s) into one." in result.warnings
    assert "No step reads a value, so the artifact has no outputs." in result.warnings


def test_warns_when_an_output_is_found_by_its_own_value():
    recording = good_recording()
    recording["steps"][8] = recorded(9, action("read", {"text": "$12,450.75"}, output_name="savings_balance"))
    warnings = build(recording).warnings
    assert any("savings_balance" in w and "its own value" in w for w in warnings)


def test_nothing_to_build_from_is_an_error():
    recording = good_recording()
    recording["steps"] = [recorded(1, None, "invalid"), recorded(2, action("done"))]
    with pytest.raises(BuildError, match="no successful steps"):
        build(recording)


def test_default_name_comes_from_the_goal():
    from lba.artifact import default_name

    assert default_name({"goal": "Log in and find the savings balance for the member"}) == "log-in-and-find-the-savings-balance-for"
    assert default_name({"goal": "!!!"}) == "task"


def test_load_recording_rejects_non_recordings(tmp_path):
    for content in ("not json", '{"hello": 1}', "[]"):
        path = tmp_path / "r.json"
        path.write_text(content)
        with pytest.raises(BuildError):
            load_recording(path)
    with pytest.raises(BuildError, match="Cannot read"):
        load_recording(tmp_path / "missing.json")
    path.write_text(json.dumps(good_recording()))
    assert load_recording(path)["goal"] == "Look up a member's balance"


# ----------------------------------------------------------------------- store


def test_save_writes_yaml_that_loads_back_identically(tmp_path):
    artifact = build().artifact
    path = save_artifact(artifact, tmp_path, values=Values(secrets=SECRETS))
    assert path == tmp_path / "member-balance.v1.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# lba artifact")
    assert "exact: false" not in text and "null" not in text  # tidy: no defaults or nulls
    assert "{{secret:BANK_USER}}" in text and "teller-xyz" not in text
    assert yaml.safe_load(text)["metadata"]["status"] == "draft"
    assert load_artifact(path) == artifact


def test_save_never_overwrites_and_never_writes_secrets(tmp_path):
    artifact = build().artifact
    save_artifact(artifact, tmp_path, values=Values(secrets=SECRETS))
    with pytest.raises(ArtifactError, match="already exists"):
        save_artifact(artifact, tmp_path, values=Values(secrets=SECRETS))
    assert save_artifact(artifact, tmp_path, overwrite=True, values=Values(secrets=SECRETS))

    leaky = artifact.model_copy(deep=True)
    leaky.steps[0].locator = Locator(primary=Target(text="teller-xyz"))
    with pytest.raises(ArtifactError, match="real secret"):
        save_artifact(leaky, tmp_path / "other", values=Values(secrets=SECRETS))
    assert not (tmp_path / "other").exists()


def test_load_gives_readable_errors(tmp_path):
    (tmp_path / "broken.yaml").write_text("a: [unclosed")
    with pytest.raises(ArtifactError, match="not valid YAML"):
        load_artifact(tmp_path / "broken.yaml")
    (tmp_path / "list.yaml").write_text("- 1\n- 2\n")
    with pytest.raises(ArtifactError, match="expected a mapping"):
        load_artifact(tmp_path / "list.yaml")
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(minimal_artifact(steps=[])))
    with pytest.raises(ArtifactError, match="steps"):
        load_artifact(tmp_path / "bad.yaml")
    with pytest.raises(ArtifactError, match="Cannot read"):
        load_artifact(tmp_path / "missing.yaml")


def test_file_name_must_match_what_is_inside(tmp_path):
    path = save_artifact(build().artifact, tmp_path, values=Values(secrets=SECRETS))
    renamed = path.rename(tmp_path / "other-name.v1.yaml")
    with pytest.raises(ArtifactError, match="Rename the file"):
        load_artifact(renamed)
    assert load_artifact(renamed.rename(tmp_path / "copy.yaml"))  # a free-form file name is fine


def test_versions_resolve_and_list(tmp_path):
    values = Values(secrets=SECRETS)
    assert next_version(tmp_path, "member-balance") == 1
    for version in (1, 2, 10):
        save_artifact(build(version=version).artifact, tmp_path, values=values)
    (tmp_path / "junk.yaml").write_text("nope: [")
    assert next_version(tmp_path, "member-balance") == 11  # numeric, not alphabetical (v10 > v2)
    assert resolve_artifact_path("member-balance", tmp_path).name == "member-balance.v10.yaml"
    assert resolve_artifact_path("member-balance.v2", tmp_path).name == "member-balance.v2.yaml"
    assert resolve_artifact_path(str(tmp_path / "member-balance.v1.yaml"), tmp_path).name == "member-balance.v1.yaml"
    with pytest.raises(ArtifactError, match="No artifact 'ghost'"):
        resolve_artifact_path("ghost", tmp_path)

    entries = list_artifacts(tmp_path)
    assert [e.path.name for e in entries] == ["junk.yaml", "member-balance.v1.yaml", "member-balance.v10.yaml", "member-balance.v2.yaml"]
    assert entries[0].error and entries[1].artifact.metadata.version == 1  # a broken file is listed, not fatal
    assert list_artifacts(tmp_path / "nowhere") == []


def test_approve_changes_only_the_status(tmp_path):
    path = save_artifact(build().artifact, tmp_path, values=Values(secrets=SECRETS))
    before = load_artifact(path)
    approved = approve_artifact(path)
    after = load_artifact(path)
    assert approved.metadata.status == after.metadata.status == "approved"
    assert after.model_dump() == {**before.model_dump(), "metadata": {**before.model_dump()["metadata"], "status": "approved"}}
    assert approve_artifact(path).metadata.status == "approved"  # approving twice is harmless


# ------------------------------------------------------------------------- CLI


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Run lba commands against a temp folder, with no real .env and fake secrets."""
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    recording = tmp_path / "run.json"
    recording.write_text(json.dumps(good_recording()), encoding="utf-8")
    folder = tmp_path / "artifacts"

    def run(*args, **kwargs):
        return CliRunner().invoke(app, [*args, "--artifacts-dir", str(folder)] if args[0] != "build" else
                                  [*args, "--artifacts-dir", str(folder)], **kwargs)

    run.recording, run.folder = recording, folder
    return run


def test_build_list_approve_end_to_end(cli):
    empty = cli("list")
    assert "No artifacts" in empty.output

    built = cli("build", str(cli.recording), "--name", "member-balance")
    assert built.exit_code == 0, built.output
    assert "Built member-balance v1 (draft): 9 step(s), 1 input(s), 2 output(s)." in built.output
    assert "lba approve member-balance" in built.output
    assert (cli.folder / "member-balance.v1.yaml").exists()

    listing = cli("list").output
    assert "member-balance" in listing and "draft" in listing and "member_id" in listing

    declined = cli("approve", "member-balance", input="n\n")
    assert declined.exit_code == 1 and "Not approved" in declined.output
    assert load_artifact(cli.folder / "member-balance.v1.yaml").metadata.status == "draft"

    approved = cli("approve", "member-balance", input="y\n")
    assert approved.exit_code == 0 and "Approved member-balance v1." in approved.output
    for expected in ("Inputs:  member_id (string)", "savings_balance (decimal)", "1. type Target(css='input[name=\"user\"]')",
                     "text='{{secret:BANK_USER}}'", "Outcome:      'No member found' -> NOT_FOUND", "Interruption: 'System maintenance notice'"):
        assert expected in approved.output
    assert "approved" in cli("list").output
    assert "already approved" in cli("approve", "member-balance").output


def test_build_picks_the_next_version_and_never_overwrites(cli):
    cli("build", str(cli.recording), "--name", "member-balance")
    second = cli("build", str(cli.recording), "--name", "member-balance")
    assert "member-balance v2" in second.output
    clash = cli("build", str(cli.recording), "--name", "member-balance", "--version", "1")
    assert clash.exit_code == 1 and "already exists" in clash.output


def test_build_shows_warnings_and_reports_bad_input(cli, tmp_path):
    timed_out = tmp_path / "late.json"
    timed_out.write_text(json.dumps(real_timed_out_recording()))
    built = cli("build", str(timed_out), "--name", "late")
    assert built.exit_code == 0 and "Warning: The recording stopped with 'timeout'" in built.output

    not_a_recording = tmp_path / "x.json"
    not_a_recording.write_text("nope")
    failed = cli("build", str(not_a_recording))
    assert failed.exit_code == 1 and "not valid JSON" in failed.output


def test_build_scrubs_values_given_with_input(cli, tmp_path):
    recording = good_recording()
    recording["steps"][4] = recorded(5, action("type", {"css": 'input[name="mid"]'}, "1001"))
    path = tmp_path / "literal.json"
    path.write_text(json.dumps(recording))
    assert cli("build", str(path), "--name", "scrubbed", "--input", "member_id=1001").exit_code == 0
    assert "{{member_id}}" in (cli.folder / "scrubbed.v1.yaml").read_text(encoding="utf-8")


def test_approve_unknown_artifact_fails_cleanly(cli):
    result = cli("approve", "ghost")
    assert result.exit_code == 1 and "No artifact 'ghost'" in result.output


def test_list_shows_broken_files_without_crashing(cli):
    cli.folder.mkdir()
    (cli.folder / "broken.v1.yaml").write_text("a: [")
    assert "INVALID" in cli("list").output


def test_a_locator_made_from_the_value_read_is_ranked_last():
    recording = good_recording()
    bound = [{"role": "cell", "name": "Priya Sharma"}, {"text": "Priya Sharma", "exact": True}, {"css": "tr > td"}]
    recording["steps"][7] = recorded(8, action("read", {"role": "cell", "name": "Priya Sharma"}, output_name="member_name"),
                                     candidates=bound)
    result = build(recording)
    locator = result.artifact.steps[7].locator
    assert locator.primary == Target(css="tr > td")  # works for any member
    assert locator.fallbacks == [Target(role="cell", name="Priya Sharma"), Target(text="Priya Sharma", exact=True)]
    assert result.warnings == []  # something usable was left, so nothing to warn about


def test_warns_when_every_locator_is_made_from_the_value_read():
    recording = good_recording()
    recording["steps"][7] = recorded(8, action("read", {"role": "cell", "name": "Priya Sharma"}, output_name="member_name"))
    assert any("located by its own value" in w for w in build(recording).warnings)
