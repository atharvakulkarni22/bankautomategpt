"""The script that watches a human, tested in a REAL browser against the real bank.

The "human" here is Playwright typing and clicking: to the page it is the same thing, because
the script listens to real DOM events. (Headless is fine for this: only HumanTakeover insists on a
visible window, since a person has to be able to see it.)
"""

import json

import pytest
from conftest import BANK_PASSWORD, BANK_USER
from test_replay import REAL_SECRETS

from bag.handoff import HUMAN_RECORDER_JS, HumanRecorder, summarize
from bag.safety import Redactor
from bag.surface import BrowserSurface, Target, Values


@pytest.fixture
def surface():
    with BrowserSurface(timeout_ms=3000, values=Values(secrets=REAL_SECRETS)) as s:
        yield s


@pytest.fixture
def recorder(surface):
    return HumanRecorder(surface, Redactor(Values(secrets=REAL_SECRETS)))


def sign_on(surface, bank_url):
    surface.goto(f"{bank_url}/login")
    surface.type(Target(css="input[name=user]"), "{{secret:BANK_USER}}")
    surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
    surface.click(Target(role="button", name="Sign On"))
    surface.wait_for(Target(role="link", name="Sign Off"))


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


def test_clicks_and_typing_are_captured_with_a_css_path_and_an_accessible_name(surface, recorder, bank_url):
    surface.goto(f"{bank_url}/login")
    recorder.start()
    surface.type(Target(css="input[name=user]"), "{{secret:BANK_USER}}")
    surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
    surface.click(Target(role="button", name="Sign On"))  # this loads a new page: the events must survive it
    surface.wait_for(Target(role="link", name="Sign Off"))

    events = recorder.collect()
    user, password = by_type(events, "input")
    assert (user["role"], user["name"], user["css"]) == ("textbox", "User name:", 'input[name="user"]')
    assert user["frame"] == "" and user["url"].endswith("/login") and user["t"] > 0
    assert user["value"] == "{{secret:BANK_USER}}"  # the real user name is scrubbed from what was typed
    assert (password["role"], password["css"], password["value"], password["masked"]) == (
        "textbox", 'input[name="pw"]', "[masked]", True)
    click = by_type(events, "click")[-1]
    assert (click["role"], click["name"]) == ("button", "Sign On") and click["tag"] == "input"


def test_a_password_is_never_read_by_the_page_script_at_all(surface, bank_url):
    # A redactor that knows NO secrets: if the password is missing from the events, it was the
    # script that refused to read it, not the redaction that cleaned it up afterwards.
    recorder = HumanRecorder(surface, Redactor(Values(secrets={})))
    surface.goto(f"{bank_url}/login")
    recorder.start()
    surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
    surface.page.locator("input[name=pw]").press_sequentially("more-secret-typing")
    raw = json.dumps(recorder.collect())
    assert BANK_PASSWORD not in raw and "more-secret-typing" not in raw and "[masked]" in raw


def test_window_human_events_is_the_in_page_list_and_collect_reads_it_back(surface, recorder, bank_url):
    surface.goto(f"{bank_url}/login")
    recorder.start()
    surface.click(Target(role="button", name="Sign On"))
    in_page = surface.evaluate_in_frames("window.__humanEvents.length")
    assert in_page and in_page[0] >= 1
    assert len(recorder.collect()) == in_page[0]


def test_typing_into_one_field_is_one_event(surface, recorder, bank_url):
    sign_on(surface, bank_url)
    recorder.start()
    surface.page.frame_locator("iframe").locator("input[name=mid]").press_sequentially("1001")  # four keystrokes
    inputs = by_type(recorder.collect(), "input")
    assert len(inputs) == 1 and inputs[0]["value"] == "1001" and inputs[0]["css"] == 'input[name="mid"]'


def test_events_in_an_iframe_survive_the_iframe_loading_a_new_page(surface, recorder, bank_url):
    sign_on(surface, bank_url)
    recorder.start()
    frame = surface.page.frame_locator("iframe")
    frame.locator("input[name=mid]").fill("1001")
    frame.get_by_role("button", name="Search").click()  # the iframe navigates to the member page: a NEW document
    frame.get_by_role("heading", name="Member Details").wait_for()
    frame.get_by_role("link", name="Open sub-account").click()  # ...and again
    frame.get_by_role("heading", name="Open sub-account").wait_for()

    events = recorder.collect()
    assert all(e["frame"] == "0" for e in events)  # every one of them happened in the first iframe
    assert [(e["type"], e["name"]) for e in events] == [
        ("input", ""), ("click", "Search"), ("click", "Open sub-account")]  # all three, in order, none lost
    assert events[0]["css"] == 'input[name="mid"]' and events[0]["value"] == "1001"
    assert events[1]["url"].endswith("/search") and events[2]["url"].endswith("/member/1001")  # different documents


def test_a_select_box_is_recorded_with_the_chosen_text(surface, recorder, bank_url):
    sign_on(surface, bank_url)
    surface.goto(f"{bank_url}/member/1001/subaccount")
    recorder.start()
    surface.page.locator("select[name=sa_type]").select_option("Checking")
    chosen = by_type(recorder.collect(), "input")
    assert len(chosen) == 1 and chosen[0]["value"] == "Checking" and chosen[0]["role"] == "combobox"
    assert chosen[0]["name"].startswith("Sub-account type")


def test_what_the_automation_typed_before_the_takeover_is_not_blamed_on_the_human(surface, recorder, bank_url):
    # The automation typed a member id, THEN a person takes over and clicks Search. Moving focus away from
    # that field makes the browser fire a late "change" event for it. The human did not type anything.
    sign_on(surface, bank_url)
    surface.type(Target(css='input[name="mid"]'), "1001")  # the automation, before the takeover
    recorder.start()
    surface.click(Target(role="button", name="Search"))  # the person
    assert [(e["type"], e["name"]) for e in recorder.collect()] == [("click", "Search")]


def test_nothing_is_recorded_before_start_or_after_stop(surface, recorder, bank_url):
    surface.goto(f"{bank_url}/login")
    surface.click(Target(text="Member Services Terminal"))  # before: not recorded
    recorder.start()
    assert recorder.collect() == []
    surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
    assert len(recorder.collect()) == 1
    recorder.stop()
    assert recorder.collect() == []  # stopping wipes what was kept
    surface.type(Target(css="input[name=user]"), "{{secret:BANK_USER}}")  # after: none of this is recorded,
    surface.click(Target(role="button", name="Sign On"))  # not even on the NEW page it loads
    surface.wait_for(Target(role="link", name="Sign Off"))
    assert recorder.collect() == []


def test_starting_again_begins_with_a_clean_slate(surface, recorder, bank_url):
    surface.goto(f"{bank_url}/login")
    recorder.start()
    surface.type(Target(css="input[name=user]"), "{{secret:BANK_USER}}")
    assert len(recorder.collect()) == 1
    recorder.start()  # the second takeover of a run must not inherit the first one's events
    assert recorder.collect() == []


def test_starting_twice_does_not_double_the_listeners(surface, recorder, bank_url):
    surface.goto(f"{bank_url}/login")
    recorder.start()
    recorder.start()
    surface.click(Target(role="button", name="Sign On"))
    assert len(by_type(recorder.collect(), "click")) == 1  # one click, one event, however often it was installed


def test_account_numbers_a_human_types_are_masked(surface, recorder, bank_url):
    sign_on(surface, bank_url)
    recorder.start()
    surface.page.frame_locator("iframe").locator("input[name=mid]").fill("1234567890123456")
    raw = json.dumps(recorder.collect())
    assert "1234567890123456" not in raw and "XXXXXXXXXXXX3456" in raw


# ---------------------------------------- the surface methods the recorder is built on


def test_add_init_script_runs_in_open_frames_and_in_pages_loaded_later(surface, bank_url):
    sign_on(surface, bank_url)  # the page now has an iframe
    surface.add_init_script("window.__marker = (window.__marker || 0) + 1; undefined")
    assert surface.evaluate_in_frames("window.__marker") == [1, 1]  # the page and its iframe, already open
    surface.goto(f"{bank_url}/home")
    assert surface.evaluate_in_frames("window.__marker") == [1, 1]  # a freshly loaded page got it too


def test_evaluate_in_frames_gives_one_answer_per_frame(surface, bank_url):
    sign_on(surface, bank_url)
    urls = surface.evaluate_in_frames("location.pathname")
    assert urls == ["/home", "/search"]


def test_bring_to_front_works(surface, bank_url):
    surface.goto(f"{bank_url}/login")
    surface.bring_to_front()


def test_the_script_is_plain_text_that_reuses_the_shared_css_path_code():
    assert "cssPath" in HUMAN_RECORDER_JS and "__humanEvents" in HUMAN_RECORDER_JS
    assert summarize([]) == "nothing was recorded"
