import pytest
from conftest import BANK_PASSWORD, BANK_USER
from pydantic import ValidationError

from bag.surface import (
    AmbiguousTarget,
    BrowserSurface,
    SurfaceError,
    SurfaceTimeout,
    Target,
    TargetNotFound,
    UnknownPlaceholder,
    Values,
    resolve,
)

MEMBER_ID_BOX = Target(css="input[name=mid]")
SEARCH_BUTTON = Target(role="button", name="Search")


@pytest.fixture
def surface():
    with BrowserSurface(timeout_ms=3000) as s:
        yield s


@pytest.fixture
def home(surface, bank_url):
    """A surface that is signed on and sitting on /home."""
    surface.goto(f"{bank_url}/login")
    surface.type(Target(css="input[name=user]"), BANK_USER)
    surface.type(Target(css="input[name=pw]"), BANK_PASSWORD)
    surface.click(Target(role="button", name="Sign On"))
    surface.wait_for(Target(role="link", name="Sign Off"))
    return surface


# ---------------------------------------------------------------------- Target


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"role": "button", "css": "a"}, {"text": "x", "label": "y"}, {"name": "x"}, {"css": "a", "name": "n"}],
)
def test_target_needs_exactly_one_strategy(kwargs):
    with pytest.raises(ValidationError):
        Target(**kwargs)


def test_target_is_hashable_and_prints_nicely():
    assert len({Target(css="a"), Target(css="a")}) == 1
    assert str(Target(role="button", name="Search")) == "Target(role='button', name='Search')"


# --------------------------------------------------------------------- resolve


def test_resolve_finds_elements_in_main_page_and_iframe(home):
    in_main = resolve(home.page, Target(role="link", name="Sign Off"))
    in_iframe = resolve(home.page, MEMBER_ID_BOX)
    assert in_main.count() == 1 and in_iframe.count() == 1
    assert in_main.element_handle().owner_frame() is home.page.main_frame
    assert in_iframe.element_handle().owner_frame().url.endswith("/search")


def test_resolve_refuses_to_guess(home):
    with pytest.raises(AmbiguousTarget, match="matches 2 elements"):
        resolve(home.page, Target(css="input"))  # the Member ID box AND the Search button
    with pytest.raises(TargetNotFound):
        resolve(home.page, Target(text="nothing like this"))


# --------------------------------------------------------------------- observe


def test_observe_includes_iframe_content_and_screenshot(home):
    seen = home.observe()
    assert seen.url.endswith("/home")
    assert seen.title == "First Legacy Bank - Home"
    assert "Sign Off" in seen.tree  # main page
    assert "Member ID" in seen.tree and 'button "Search"' in seen.tree  # inside the iframe
    assert "[ref=" not in seen.tree  # noise removed
    assert seen.screenshot.startswith(b"\x89PNG")


def test_observe_lists_fields_the_tree_cannot_name(home):
    tree = home.observe().tree
    assert "Fields with no accessible name (target these with css):" in tree
    assert '- textbox next to "Member ID": css=input[name="mid"]' in tree
    # ...and that css really works as a Target.
    home.type(Target(css='input[name="mid"]'), "1004")
    assert home.read(MEMBER_ID_BOX) == "1004"


def test_observe_has_no_unnamed_section_when_every_field_is_named(surface, bank_url):
    surface.goto(f"{bank_url}/login?popup=0")
    tree = surface.observe().tree
    assert 'css=input[name="pw"]' in tree  # the password box has no <label>
    assert 'css=input[name="user"]' not in tree  # the user name box has one


# ------------------------------------------------------- placeholders in the surface


def test_type_swaps_placeholders_for_real_values(bank_url):
    values = Values(inputs={"member_id": "1003"}, secrets={"BANK_USER": BANK_USER, "BANK_PASSWORD": BANK_PASSWORD})
    with BrowserSurface(timeout_ms=3000, values=values) as surface:
        surface.goto(f"{bank_url}/login")
        surface.type(Target(css="input[name=user]"), "{{secret:BANK_USER}}")
        surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
        surface.click(Target(role="button", name="Sign On"))
        surface.wait_for(Target(role="link", name="Sign Off"))
        surface.type(MEMBER_ID_BOX, "{{member_id}}")
        assert surface.read(MEMBER_ID_BOX) == "1003"


def test_unknown_placeholder_fails_before_touching_the_page(bank_url):
    values = Values(inputs={}, secrets={"BANK_USER": BANK_USER})
    with BrowserSurface(timeout_ms=3000, values=values) as surface:
        surface.goto(f"{bank_url}/login")
        with pytest.raises(UnknownPlaceholder):
            surface.type(Target(css="input[name=pw]"), "{{secret:BANK_PASSWORD}}")
        assert surface.read(Target(css="input[name=pw]")) == ""  # nothing was typed


def test_values_read_from_the_page_are_scrubbed_of_secrets(bank_url):
    values = Values(secrets={"BANK_USER": BANK_USER})
    with BrowserSurface(timeout_ms=3000, values=values) as surface:
        surface.goto(f"{bank_url}/login")
        surface.type(Target(css="input[name=user]"), BANK_USER)  # typed literally, on purpose
        assert surface.read(Target(css="input[name=user]")) == "{{secret:BANK_USER}}"
        assert BANK_USER not in surface.observe().tree


# ------------------------------------------------------------------ the actions


def test_type_click_read_and_wait(home):
    home.type(MEMBER_ID_BOX, "1001")
    assert home.read(MEMBER_ID_BOX) == "1001"  # read() returns an input's value
    home.click(SEARCH_BUTTON)
    home.wait_for(Target(role="heading", name="Member Details"))
    assert home.read(Target(text="Priya Sharma")) == "Priya Sharma"  # read() returns visible text
    assert home.read(Target(text="XXXX-XXXX-1001")) == "XXXX-XXXX-1001"


def test_wait_for_times_out_with_a_clear_error(home):
    with pytest.raises(TargetNotFound):
        home.wait_for(Target(text="never appears"), timeout_ms=300)


def test_errors_never_contain_typed_text(home):
    with pytest.raises(SurfaceError) as error:
        home.type(Target(css="input[name=missing]"), "s3cret-value")
    assert "s3cret-value" not in str(error.value)


def test_popup_blocks_clicks_until_ok(surface, bank_url):
    surface.goto(f"{bank_url}/login")
    surface.type(Target(css="input[name=user]"), BANK_USER)
    surface.type(Target(css="input[name=pw]"), BANK_PASSWORD)
    surface.click(Target(role="button", name="Sign On"))
    surface.goto(f"{bank_url}/home?popup=1")
    with pytest.raises(SurfaceTimeout, match="intercepts pointer events"):  # a timeout: worth retrying
        surface.click(SEARCH_BUTTON)
    surface.click(Target(role="button", name="OK"))
    surface.click(SEARCH_BUTTON)  # works now


# ------------------------------------------------------------ describe_element


def test_describe_gives_fallbacks_that_each_find_the_same_element(home):
    candidates = home.describe(SEARCH_BUTTON)
    kinds = {next(k for k in ("role", "label", "text", "css") if getattr(c, k) is not None) for c in candidates}
    assert {"role", "text", "css"} <= kinds
    assert Target(role="button", name="Search") in candidates
    expected = resolve(home.page, SEARCH_BUTTON).element_handle()
    for candidate in candidates:  # every fallback must find that very element
        found = resolve(home.page, candidate).element_handle()
        assert expected.evaluate("(a, b) => a === b", found), candidate


def test_describe_unlabelled_field_falls_back_to_css(home):
    # The Member ID box has no <label> and no accessible name, so only css can name it.
    assert home.describe(MEMBER_ID_BOX) == [Target(css='input[name="mid"]')]


def test_describe_finds_label_even_with_a_control_inside_it(home, bank_url):
    home.goto(f"{bank_url}/member/1001/subaccount")  # type <select> sits inside its <label>
    candidates = home.describe(Target(css="select[name=sa_type]"))
    assert any(c.label and c.label.startswith("Sub-account type") for c in candidates)


# ------------------------------------------------------- what replay relies on


def test_locate_tries_targets_in_order_and_skips_ambiguous_or_missing_ones(home):
    missing, ambiguous = Target(role="button", name="Nope"), Target(css="input")  # 0 and 2 matches
    assert home.locate([SEARCH_BUTTON, missing]) == 0  # the first one that works wins
    assert home.locate([missing, SEARCH_BUTTON]) == 1
    assert home.locate([missing, ambiguous, MEMBER_ID_BOX]) == 2
    with pytest.raises(TargetNotFound, match=r"None of the 2 target\(s\) matched.*matches 2 elements"):
        home.locate([missing, ambiguous])


def test_is_visible_ignores_hidden_leftovers(surface, bank_url):
    surface.goto(f"{bank_url}/login")
    surface.type(Target(css="input[name=user]"), BANK_USER)
    surface.type(Target(css="input[name=pw]"), BANK_PASSWORD)
    surface.click(Target(role="button", name="Sign On"))
    surface.goto(f"{bank_url}/home?popup=1")
    popup = Target(text="System maintenance notice")
    assert surface.is_visible(popup)
    surface.click(Target(role="button", name="OK"))
    assert not surface.is_visible(popup)  # still in the page, but hidden: it must not count
    assert surface.is_visible(Target(text="Member Services"))
    assert not surface.is_visible(Target(text="nothing like this"))


def test_current_url_and_pause(home):
    assert home.current_url().endswith("/home")
    home.pause(0.01)
