import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml
from PIL import Image, ImageChops, ImageStat

from bag.safety import (
    ActionRule,
    ApprovalRequest,
    AuditLog,
    Decision,
    Guard,
    PageContext,
    Redactor,
    SafetyConfig,
    SafetyConfigError,
    blur_boxes,
    check,
    has_account_number,
    load_safety_config,
    mask_account_numbers,
    terminal_approver,
)
from bag.surface import BrowserSurface, Target, Values

BANK = "http://127.0.0.1:5000"
SHIPPED_CONFIG = Path(__file__).parent.parent / "config" / "safety.yaml"


def make_guard(**changes):
    config = {"allowed_urls": [BANK], "risky_button_names": ["confirm", "pay", "send money"]}
    config.update(changes)
    return Guard(SafetyConfig(**config), approver=lambda request: False)


def action(kind="click", text=None):
    return NS(action=kind, text=text)


def context(url=f"{BANK}/home", frames=(), targets=()):
    """A page context. `targets` may be one Target or a list of them."""
    targets = list(targets) if isinstance(targets, (list, tuple)) else [targets]
    return PageContext(url=url, frame_urls=list(frames), targets=targets)


# ---------------------------------------------------------------------- config


def test_the_shipped_config_is_valid_and_strict():
    config = load_safety_config(SHIPPED_CONFIG)
    assert config.allowed_urls and config.blur_screenshots
    assert "confirm" in config.risky_button_names and config.risky_actions


@pytest.mark.parametrize(
    "content, message",
    [
        ("a: [unclosed", "not valid YAML"),
        ("- 1\n- 2\n", "expected a mapping"),
        ("risky_button_names: []\n", "allowed_urls"),
        ("allowed_urls: []\n", "at least 1"),
        ("allowed_urls: [example.com]\n", "web address"),
        ("allowed_urls: ['ftp://example.com']\n", "web address"),
        ("allowed_urls: ['http://']\n", "web address"),
        ("allowed_urls: ['http://a.test']\nrisky_button_names: ['  ']\n", "must not be empty"),
        ("allowed_urls: ['http://a.test']\nsurprise: 1\n", "Extra inputs"),
        ("allowed_urls: ['http://a.test']\nrisky_actions: [{name: x, action: fly}]\n", "risky_actions"),
        ("allowed_urls: ['http://a.test']\nrisky_actions: [{name: x, action: type, target_matches: '('}]\n", "regular expression"),
    ],
)
def test_a_bad_config_is_refused_with_a_clear_message(tmp_path, content, message):
    path = tmp_path / "safety.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SafetyConfigError, match=message):
        load_safety_config(path)


def test_a_missing_config_is_refused_not_ignored(tmp_path):
    with pytest.raises(SafetyConfigError, match="Nothing runs without one"):
        load_safety_config(tmp_path / "nope.yaml")


# ------------------------------------------------------------------ URL rules


@pytest.mark.parametrize(
    "url",
    [
        f"{BANK}/home",
        f"{BANK}/",
        f"{BANK}",
        "http://127.0.0.1:5000/member/1001?x=1#top",
    ],
)
def test_pages_on_the_allowlist_are_allowed(url):
    assert make_guard().check(action(), context(url)).decision is Decision.ALLOW


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:5001/home",  # another port
        "https://127.0.0.1:5000/home",  # another scheme
        "http://localhost:5000/home",  # same machine, different name: not listed
        "http://127.0.0.1.evil.test:5000/home",  # a look-alike host that starts the same
        "http://127.0.0.1:5000@evil.test/home",  # the part before @ is a user name, not the host
        "http://evil.test/?next=http://127.0.0.1:5000",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        "about:blank",  # the main page must be a real allowed page
        "",
        "not a url",
    ],
)
def test_everything_else_is_blocked(url):
    verdict = make_guard().check(action(), context(url))
    assert verdict.decision is Decision.BLOCK and verdict.rule == "allowed_urls"


def test_default_ports_and_path_prefixes():
    guard = make_guard(allowed_urls=["https://bank.test", "http://h.test/app"])
    assert guard.check(action(), context("https://bank.test:443/x")).decision is Decision.ALLOW
    assert guard.check(action(), context("http://h.test/app")).decision is Decision.ALLOW
    assert guard.check(action(), context("http://h.test/app/page")).decision is Decision.ALLOW
    assert guard.check(action(), context("http://h.test/apple")).decision is Decision.BLOCK  # not the same folder
    assert guard.check(action(), context("http://h.test/")).decision is Decision.BLOCK


def test_a_frame_from_another_site_blocks_the_page():
    guard = make_guard()
    assert guard.check(action(), context(frames=[f"{BANK}/search", "about:blank", "about:srcdoc"])).decision is Decision.ALLOW
    verdict = guard.check(action(), context(frames=[f"{BANK}/search", "http://evil.test/login-form"]))
    assert verdict.decision is Decision.BLOCK and "evil.test" in verdict.reason


def test_actions_that_never_touch_the_page_are_always_allowed():
    for kind in ("done", "ask_human"):
        assert make_guard().check(action(kind), context("http://evil.test/")).decision is Decision.ALLOW


def test_check_url_is_for_the_start_page():
    guard = make_guard()
    assert guard.check_url(f"{BANK}/login").decision is Decision.ALLOW
    blocked = guard.check_url("http://evil.test/")
    assert blocked.decision is Decision.BLOCK and "not on the allowlist" in blocked.reason


# ------------------------------------------------------------ risky buttons


@pytest.mark.parametrize(
    "target",
    [
        Target(role="button", name="Confirm"),
        Target(role="button", name="CONFIRM"),
        Target(role="button", name="Please confirm transfer"),
        Target(role="button", name="Pay now"),
        Target(label="Send money"),
        Target(text="Confirm"),
        Target(css="#confirm-btn"),
        Target(css='input[value="Confirm"]'),
    ],
)
def test_clicking_a_risky_button_needs_approval(target):
    verdict = make_guard().check(action("click"), context(targets=[target]))
    assert verdict.decision is Decision.NEEDS_APPROVAL and verdict.rule == "risky_button_names"


@pytest.mark.parametrize(
    "target",
    [
        Target(role="button", name="Search"),
        Target(role="link", name="Payments"),  # "pay" is a whole-word rule: Payments is a menu, not a payment
        Target(role="button", name="Repay"),
        Target(role="button", name="Sign On"),
        Target(css="input[name=mid]"),
    ],
)
def test_ordinary_clicks_are_allowed(target):
    assert make_guard().check(action("click"), context(targets=[target])).decision is Decision.ALLOW


def test_every_known_name_of_the_element_is_checked():
    # The agent may have found the button by css, and replay may have a bland primary: the fallback
    # that carries the real name must still be seen.
    targets = [Target(css="body > table > tr > td > input"), Target(role="button", name="Confirm")]
    assert make_guard().check(action("click"), context(targets=targets)).decision is Decision.NEEDS_APPROVAL


def test_the_button_rule_applies_to_clicks_only():
    typing = make_guard().check(action("type", "x"), context(targets=[Target(role="button", name="Confirm")]))
    assert typing.decision is Decision.ALLOW


# ------------------------------------------------------------- risky actions


def test_risky_action_rules_match_on_kind_target_and_text():
    rules = [
        ActionRule(name="Entering a money amount", action="type", target_matches=r"\b(amount|amt)\b"),
        ActionRule(name="Typing a secret", action="type", text_matches=r"\{\{secret:"),
        ActionRule(name="Secret into a money field", action="type", text_matches="secret", target_matches="amt"),
    ]
    guard = make_guard(risky_actions=rules)
    amt = [Target(css='input[name="amt"]')]
    assert guard.check(action("type", "25"), context(targets=amt)).reason == "Entering a money amount"
    assert guard.check(action("click"), context(targets=amt)).decision is Decision.ALLOW  # a click is not typing
    assert guard.check(action("type", "bob"), context(targets=[Target(css="input[name=nick]")])).decision is Decision.ALLOW
    secret = guard.check(action("type", "{{secret:BANK_PASSWORD}}"), context(targets=[Target(css="input[name=pw]")]))
    assert secret.decision is Decision.NEEDS_APPROVAL and secret.reason == "Typing a secret"
    both = guard.check(action("type", "{{secret:X}}"), context(targets=amt))
    assert both.reason.count(";") == 2  # all three rules matched: every reason is reported


def test_the_shipped_rules_stop_the_sub_account_flow_at_the_right_places():
    guard = Guard(load_safety_config(SHIPPED_CONFIG), approver=lambda request: False)
    page = f"{BANK}/home"
    amount = guard.check(action("type", "{{amount}}"), context(page, (), Target(css='input[name="amt"]')))
    confirm = guard.check(action("click"), context(page, (), Target(role="button", name="Confirm")))
    search = guard.check(action("click"), context(page, (), Target(role="button", name="Search")))
    pin = guard.check(action("type", "{{pin}}"), context(page, (), Target(label="Enter your PIN")))
    assert [v.decision for v in (amount, confirm, search, pin)] == [
        Decision.NEEDS_APPROVAL, Decision.NEEDS_APPROVAL, Decision.ALLOW, Decision.NEEDS_APPROVAL]


def test_block_beats_needs_approval():
    verdict = make_guard().check(action("click"), context("http://evil.test/", targets=[Target(role="button", name="Confirm")]))
    assert verdict.decision is Decision.BLOCK


def test_the_module_level_check_function():
    config = SafetyConfig(allowed_urls=[BANK], risky_button_names=["confirm"])
    risky = context(targets=[Target(text="Confirm")])
    assert check(action(), risky, config).decision is Decision.NEEDS_APPROVAL
    assert check(action(), context("http://evil.test/"), config).decision is Decision.BLOCK
    assert check(action(), context(targets=[Target(text="Search")]), config).decision is Decision.ALLOW


# ------------------------------------------------- approval and the audit log


def guard_with_audit(tmp_path, approver, values=None, **config):
    redactor = Redactor(values or Values(secrets={}))
    log = AuditLog(tmp_path / "audit.jsonl", redactor)
    config = {"allowed_urls": [BANK], "risky_button_names": ["confirm"], **config}
    return Guard(SafetyConfig(**config), approver=approver, audit=log, redactor=redactor), tmp_path / "audit.jsonl"


def read_audit(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_allow_and_block_never_ask_a_human(tmp_path):
    asked = []
    guard, _ = guard_with_audit(tmp_path, lambda request: asked.append(request) or True)
    assert guard.authorize(action(), context(targets=[Target(text="Search")])).decision is Decision.ALLOW
    assert guard.authorize(action(), context("http://evil.test/")).decision is Decision.BLOCK
    assert asked == []


def test_a_yes_turns_needs_approval_into_allow(tmp_path):
    asked = []
    guard, _ = guard_with_audit(tmp_path, lambda request: asked.append(request) or True)
    verdict = guard.authorize(action(), context(targets=[Target(role="button", name="Confirm")]), run="replay demo v1", step=6)
    assert verdict.decision is Decision.ALLOW and verdict.approved_by == "human"
    request = asked[0]
    assert isinstance(request, ApprovalRequest) and (request.run, request.step) == ("replay demo v1", 6)
    assert "Confirm" in request.action and request.url == f"{BANK}/home" and request.rule == "risky_button_names"


def test_a_no_turns_needs_approval_into_block(tmp_path):
    guard, _ = guard_with_audit(tmp_path, lambda request: False)
    verdict = guard.authorize(action(), context(targets=[Target(role="button", name="Confirm")]))
    assert verdict.decision is Decision.BLOCK and verdict.approved_by is None
    assert verdict.reason.startswith("A human did not approve:") and verdict.rule == "risky_button_names"


def test_every_decision_is_written_to_the_audit_log(tmp_path):
    answers = iter([True, False])
    guard, path = guard_with_audit(tmp_path, lambda request: next(answers))
    risky = context(targets=[Target(role="button", name="Confirm")])
    guard.authorize(action(), context(targets=[Target(text="Search")]), run="r", step=1)
    guard.authorize(action(), risky, run="r", step=2)
    guard.authorize(action(), risky, run="r", step=3)
    guard.authorize(action(), context("http://evil.test/"), run="r", step=4)
    lines = read_audit(path)
    assert [(l["step"], l["checked"], l["final"], l["approved_by"]) for l in lines] == [
        (1, "ALLOW", "ALLOW", None),
        (2, "NEEDS_APPROVAL", "ALLOW", "human"),
        (3, "NEEDS_APPROVAL", "BLOCK", None),
        (4, "BLOCK", "BLOCK", None),
    ]
    assert lines[1]["rule"] == "risky_button_names" and "Confirm" in lines[1]["action"] and lines[1]["time"]


def test_the_audit_log_and_the_prompt_are_redacted(tmp_path):
    values = Values(secrets={"BANK_USER": "teller-xyz"})
    asked = []
    guard, path = guard_with_audit(tmp_path, lambda request: asked.append(request) or False, values=values)
    page = f"{BANK}/home?account=1234567890123456&user=teller-xyz"
    guard.authorize(action("click"), context(page, (), Target(role="button", name="Confirm 9990-0000-1001")), run="r")
    text = path.read_text(encoding="utf-8") + repr(asked[0])
    for leaked in ("1234567890123456", "teller-xyz", "9990-0000-1001"):
        assert leaked not in text
    assert "XXXXXXXXXXXX3456" in text and "{{secret:BANK_USER}}" in text and "XXXX-XXXX-1001" in text


def test_a_guard_without_an_audit_log_still_decides():
    guard = make_guard()
    assert guard.authorize(action(), context(targets=[Target(text="Search")])).decision is Decision.ALLOW


# --------------------------------------------------------- terminal approval


def request():
    return ApprovalRequest(run="replay demo v1", step=3, action="click Target(name='Confirm')",
                           url=f"{BANK}/home", reason="Clicking something named like 'confirm'", rule="risky_button_names")


def test_with_no_terminal_the_answer_is_no_and_nobody_is_asked():
    printed = []

    def never(prompt):
        raise AssertionError("must not ask when there is no terminal")

    assert terminal_approver(request(), input_fn=never, interactive=False, write=printed.append) is False
    assert any("DENIED" in line for line in printed)


@pytest.mark.parametrize("answer, expected", [("y", True), ("YES", True), (" yes ", True), ("n", False),
                                              ("", False), ("sure", False), ("yy", False)])
def test_only_a_clear_yes_approves(answer, expected):
    assert terminal_approver(request(), input_fn=lambda prompt: answer, interactive=True, write=lambda line: None) is expected


def test_the_prompt_shows_what_will_happen_and_why():
    printed = []
    terminal_approver(request(), input_fn=lambda prompt: "n", interactive=True, write=printed.append)
    shown = "\n".join(printed)
    for expected in ("APPROVAL NEEDED", "replay demo v1, step 3", "click Target(name='Confirm')", f"{BANK}/home",
                     "named like 'confirm'"):
        assert expected in shown


def test_closing_the_input_counts_as_no():
    def closed(prompt):
        raise EOFError

    assert terminal_approver(request(), input_fn=closed, interactive=True, write=lambda line: None) is False


# ----------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "before, after",
    [
        ("9990-0000-1001", "XXXX-XXXX-1001"),
        ("1234 5678 9012 3456", "XXXX XXXX XXXX 3456"),
        ("1234567890123456", "XXXXXXXXXXXX3456"),
        ("Account 123456789 ok", "Account XXXXX6789 ok"),
        ("two: 1111222233334444 and 5555-6666-7777", "two: XXXXXXXXXXXX4444 and XXXX-XXXX-7777"),
        ("(1234567890123456)", "(XXXXXXXXXXXX3456)"),
    ],
)
def test_account_numbers_keep_only_their_last_four_digits(before, after):
    assert mask_account_numbers(before) == after
    assert has_account_number(before)
    assert mask_account_numbers(after) == after  # safe to run twice


@pytest.mark.parametrize(
    "text",
    [
        "member 1001",
        "$12,450.75",
        "250000.00",
        "12345678.90",  # a large amount, not an account
        "Savings Balance 1,234,567.89",
        "XXXX-XXXX-1001",
        "step 5 of 25",
        "http://127.0.0.1:5000/login",
        "2026-10-04T17:56:20+00:00",
        "evidence/screenshots/member-balance-v1-20261004-175620-step3.png",
        "evidence/recordings/20261004-174858-log-in-and-find-the-savings-balance-for-.json",
        "version12345678x",
        "",
    ],
)
def test_ordinary_text_is_left_alone(text):
    assert mask_account_numbers(text) == text and not has_account_number(text)


def test_redactor_removes_secrets_and_masks_accounts_together():
    redactor = Redactor(Values(secrets={"BANK_USER": "teller-xyz", "BANK_PASSWORD": "hunter2-pw"}))
    text = "user teller-xyz typed hunter2-pw into 1234 5678 9012 3456"
    assert redactor.text(text) == "user {{secret:BANK_USER}} typed {{secret:BANK_PASSWORD}} into XXXX XXXX XXXX 3456"


def test_redactor_cleans_every_string_inside_nested_data():
    redactor = Redactor(Values(secrets={"BANK_USER": "teller-xyz"}))
    data = {"a": "teller-xyz", "b": ["1234567890123456", {"c": "ok"}, 7, None], "d": 1.5, "teller-xyz": "key stays"}
    assert redactor.data(data) == {"a": "{{secret:BANK_USER}}", "b": ["XXXXXXXXXXXX3456", {"c": "ok"}, 7, None],
                                   "d": 1.5, "teller-xyz": "key stays"}


def test_a_default_redactor_knows_the_secrets_in_the_environment(monkeypatch):
    monkeypatch.setenv("BANK_PASSWORD", "hunter2-pw")
    assert Redactor().text("pw is hunter2-pw") == "pw is {{secret:BANK_PASSWORD}}"


def stripes(width=200, height=100):
    """A black and white striped picture: sharp detail, like text."""
    image = Image.new("RGB", (width, height), "white")
    for x in range(0, width, 4):
        for y in range(height):
            for dx in range(2):
                image.putpixel((x + dx, y), (0, 0, 0))
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def crop(png, box):
    return Image.open(BytesIO(png)).convert("RGB").crop(box)


def test_blur_destroys_detail_inside_the_box_and_nothing_outside():
    original = stripes()
    blurred = blur_boxes(original, [(50, 20, 60, 30)])
    inside = (55, 25, 105, 45)  # well within the box
    assert ImageStat.Stat(crop(original, inside).convert("L")).stddev[0] > 100  # sharp stripes
    assert ImageStat.Stat(crop(blurred, inside).convert("L")).stddev[0] < 25  # smeared into grey
    outside = ImageChops.difference(crop(original, (0, 0, 200, 100)), crop(blurred, (0, 0, 200, 100)))
    assert outside.crop((0, 0, 40, 100)).getbbox() is None  # left of the box (and its 3px padding)
    assert outside.crop((120, 0, 200, 100)).getbbox() is None
    assert outside.crop((0, 60, 200, 100)).getbbox() is None


def test_blur_accepts_dicts_clips_boxes_and_returns_a_valid_png():
    original = stripes()
    odd = [{"x": -20, "y": -20, "width": 50, "height": 50}, (180, 80, 100, 100), (500, 500, 10, 10), (10, 10, 0, 0)]
    result = blur_boxes(original, odd)
    assert Image.open(BytesIO(result)).size == (200, 100)
    assert result != original  # the two boxes that overlap the picture were blurred; the far-away one was ignored


def test_blur_with_no_boxes_returns_the_original_bytes():
    original = stripes()
    assert blur_boxes(original, []) is original


# ------------------------------------- the surface: blurred screenshots, real browser

PAGE = """
<body style="margin:0;font:16px Arial">
<input id="pw" type="password" value="hunter2-pw" style="position:absolute;left:20px;top:20px;width:200px">
<input id="user" type="text" value="teller-xyz" style="position:absolute;left:20px;top:60px;width:200px">
<input id="plain" type="text" value="hello there" style="position:absolute;left:20px;top:100px;width:200px">
<p id="acct" style="position:absolute;left:20px;top:140px;margin:0">Account 1234 5678 9012 3456</p>
<p id="note" style="position:absolute;left:20px;top:200px;margin:0">Nothing sensitive in this sentence</p>
<iframe id="frame" style="position:absolute;left:400px;top:20px;width:300px;height:80px;border:0"
        srcdoc="<p style='margin:0;font:16px Arial'>Account 1234 5678 9012 3456</p>"></iframe>
</body>
"""
SECRETS = {"BANK_USER": "teller-xyz", "BANK_PASSWORD": "hunter2-pw"}


@pytest.fixture
def sensitive_page():
    with BrowserSurface(timeout_ms=3000, values=Values(secrets=SECRETS)) as surface:
        surface.page.set_content(PAGE)
        yield surface


def region(surface, selector):
    box = surface.page.locator(selector).bounding_box()
    return box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]


def touches(boxes, area):
    left, top, right, bottom = area
    return any(x < right and x + w > left and y < bottom and y + h > top for x, y, w, h in boxes)


def test_the_surface_finds_passwords_secret_fields_and_account_numbers(sensitive_page):
    boxes = sensitive_page.sensitive_boxes()
    assert touches(boxes, region(sensitive_page, "#pw"))  # a password field
    assert touches(boxes, region(sensitive_page, "#user"))  # a field whose value is a secret
    assert touches(boxes, region(sensitive_page, "#acct"))  # text that looks like an account number
    assert touches(boxes, region(sensitive_page, "#frame"))  # the same, inside an iframe (offset correctly)
    assert not touches(boxes, region(sensitive_page, "#plain"))  # ordinary text is left alone
    assert not touches(boxes, region(sensitive_page, "#note"))


def test_observe_returns_a_screenshot_with_those_spots_blurred(sensitive_page):
    blurred = sensitive_page.observe().screenshot
    sensitive_page.blur_screenshots = False
    raw = sensitive_page.observe().screenshot

    def changed(selector):
        return ImageChops.difference(crop(raw, region(sensitive_page, selector)), crop(blurred, region(sensitive_page, selector))).getbbox()

    for hidden in ("#pw", "#user", "#acct", "#frame"):
        assert changed(hidden) is not None, hidden
    for untouched in ("#plain", "#note"):
        assert changed(untouched) is None, untouched


def test_the_outline_shown_to_the_ai_masks_account_numbers_and_secrets(sensitive_page):
    tree = sensitive_page.observe().tree
    assert "1234 5678 9012 3456" not in tree and "XXXX XXXX XXXX 3456" in tree
    assert "hunter2-pw" not in tree and "teller-xyz" not in tree


def test_extra_selectors_from_the_config_are_blurred(sensitive_page):
    sensitive_page.blur_selectors = ("#note",)
    assert touches(sensitive_page.sensitive_boxes(), region(sensitive_page, "#note"))


def test_blurring_can_be_turned_off_for_debugging():
    with BrowserSurface(timeout_ms=3000, values=Values(secrets=SECRETS), blur_screenshots=False) as surface:
        surface.page.set_content(PAGE)
        raw = surface.page.screenshot(type="png")
        assert surface.observe().screenshot == raw


def test_frame_urls_lists_what_the_iframes_load(sensitive_page, bank_url):
    assert sensitive_page.frame_urls() == ["about:srcdoc"]
    sensitive_page.goto(f"{bank_url}/login")
    assert sensitive_page.frame_urls() == []
