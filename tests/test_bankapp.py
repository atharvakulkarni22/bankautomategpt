import re

import pytest

from lba.bankapp import app as bank_module
from lba.bankapp.app import create_app

USER = "teller-test"
PASSWORD = "not-a-real-password"


class FakeClock:
    """Stands in for the time module so tests don't really wait."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # A developer's own fault switches must not leak into the tests.
    for name in ("BANK_POPUP", "BANK_SLOW", "BANK_PERM", "SESSION_TTL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(bank_module, "time", fake)
    return fake


def make_client(**extra):
    app = create_app({"BANK_USER": USER, "BANK_PASSWORD": PASSWORD, **extra})
    return app.test_client()


def sign_on(client):
    return client.post("/login", data={"user": USER, "pw": PASSWORD})


@pytest.fixture
def client():
    c = make_client()
    sign_on(c)
    return c


def text(response):
    return response.get_data(as_text=True)


# ---------------------------------------------------------------- login / pages


def test_missing_credentials_refuses_to_start(monkeypatch):
    monkeypatch.delenv("BANK_USER", raising=False)
    monkeypatch.delenv("BANK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        create_app({})


def test_wrong_password_stays_on_login():
    c = make_client()
    response = c.post("/login", data={"user": USER, "pw": "wrong"})
    assert "Invalid user name or password" in text(response)
    assert c.get("/home").status_code == 302  # still signed out


def test_pages_need_sign_on():
    c = make_client()
    for url in ("/home", "/search", "/member/1001", "/member/1001/subaccount"):
        response = c.get(url)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")


def test_home_has_search_iframe(client):
    html = text(client.get("/home"))
    assert '<iframe src="/search"' in html
    assert "System maintenance notice" not in html


def test_no_id_attributes_anywhere(client):
    urls = ["/login", "/home?popup=1", "/search", "/member/1001", "/member/9999",
            "/member/1001/subaccount", "/member/1001/subaccount?perm=deny"]
    for url in urls:
        assert not re.search(r"\sid\s*=", text(client.get(url)), re.I), url


# --------------------------------------------------------------------- members


def test_search_goes_to_member_details(client):
    response = client.get("/search?mid=1001", follow_redirects=True)
    html = text(response)
    assert "Member Details" in html
    assert "Priya Sharma" in html
    assert "XXXX-XXXX-1001" in html
    assert "9990-0000-1001" not in html  # full number must never be shown
    assert "Savings Balance" in html and "$12,450.75" in html


def test_unknown_member(client):
    html = text(client.get("/search?mid=nope", follow_redirects=True))
    assert "No member found" in html


def test_blank_search_asks_for_an_id(client):
    assert "Please enter a Member ID" in text(client.get("/search?mid="))


# ----------------------------------------------------------------- sub-accounts


def open_form(**overrides):
    data = {"sa_type": "Savings", "nick": "Rainy day", "amt": "25"}
    data.update(overrides)
    return data


def test_subaccount_flow_needs_confirmation(client):
    confirm_page = client.post("/member/1001/subaccount", data=open_form())
    html = text(confirm_page)
    assert "Confirm sub-account" in html and 'value="Confirm"' in html
    # Nothing was created yet.
    assert "Sub-accounts" not in text(client.get("/member/1001"))

    done = client.post("/member/1001/subaccount/confirm", data=open_form())
    assert "SA-1001-01" in text(done)
    assert "SA-1001-01" in text(client.get("/member/1001"))

    again = client.post("/member/1001/subaccount/confirm", data=open_form())
    assert "SA-1001-02" in text(again)


def test_subaccount_rejects_bad_input(client):
    for bad in (open_form(amt="abc"), open_form(amt="-5"), open_form(sa_type="Hacked"),
                open_form(nick="x" * 21), open_form(amt="nan")):
        for url in ("/member/1001/subaccount", "/member/1001/subaccount/confirm"):
            html = text(client.post(url, data=bad))
            assert "SA-1001" not in html
            assert "Open sub-account" in html


def test_subaccount_escapes_user_text(client):
    html = text(client.post("/member/1001/subaccount", data=open_form(nick="<script>x</script>")))
    assert "<script>x</script>" not in html


def test_subaccount_for_unknown_member(client):
    assert "No member found" in text(client.get("/member/9999/subaccount"))


# ---------------------------------------------------------------------- faults


def test_popup_flag_is_remembered_and_can_be_turned_off(client):
    assert "System maintenance notice" in text(client.get("/home?popup=1"))
    assert "System maintenance notice" in text(client.get("/home"))  # sticky
    assert "System maintenance notice" not in text(client.get("/home?popup=0"))


def test_popup_from_environment(monkeypatch):
    monkeypatch.setenv("BANK_POPUP", "1")
    c = make_client()
    sign_on(c)
    assert "System maintenance notice" in text(c.get("/home"))


def test_perm_deny_blocks_subaccounts_only(client):
    response = client.get("/member/1001/subaccount?perm=deny")
    assert response.status_code == 403
    assert "permission" in text(response)
    assert client.post("/member/1001/subaccount/confirm", data=open_form()).status_code == 403
    assert client.get("/member/1001").status_code == 200  # viewing still works


def test_slow_waits_and_is_capped(client, clock):
    client.get("/home?slow=2")
    assert clock.slept == [2.0]
    client.get("/home?slow=999")
    assert clock.slept[-1] == 30.0


def test_session_timeout(clock):
    c = make_client(SESSION_TTL=60)
    sign_on(c)
    assert c.get("/home").status_code == 200
    clock.now += 61
    response = c.get("/home")
    assert response.status_code == 302
    assert "expired=1" in response.headers["Location"]
    assert "timed out" in text(c.get("/login?expired=1"))
    assert c.get("/home").status_code == 302  # really signed out
    sign_on(c)  # signing on again works
    assert c.get("/home").status_code == 200
