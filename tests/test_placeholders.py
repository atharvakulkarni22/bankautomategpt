import pytest

from bag.surface import UnknownPlaceholder, Values
from bag.surface.placeholders import DEFAULT_SECRET_NAMES, secret_names_from_env

SECRETS = {"BANK_USER": "teller-xyz", "BANK_PASSWORD": "hunter2-pw", "ANTHROPIC_API_KEY": "sk-not-for-pages"}


def make_values(**inputs):
    return Values(inputs=inputs, secrets=SECRETS)


def test_substitutes_inputs_and_secrets():
    values = make_values(member_id="1001")
    assert values.substitute("id={{member_id}}") == "id=1001"
    assert values.substitute("{{ secret:BANK_USER }}/{{secret:BANK_PASSWORD}}") == "teller-xyz/hunter2-pw"
    assert values.substitute("no placeholders here") == "no placeholders here"


def test_unknown_placeholders_fail_without_leaking_values():
    values = make_values(member_id="1001")
    with pytest.raises(UnknownPlaceholder) as error:
        values.substitute("{{amount}}")
    assert "member_id" in str(error.value) and "1001" not in str(error.value)
    with pytest.raises(UnknownPlaceholder) as error:
        values.substitute("{{secret:NOPE}}")
    assert "teller-xyz" not in str(error.value) and "hunter2-pw" not in str(error.value)


def test_only_allow_listed_secrets_can_be_requested():
    # The API key exists in the environment, but a page-injected prompt must not get it.
    values = make_values()
    assert values.secret_names == ["BANK_PASSWORD", "BANK_USER"]
    with pytest.raises(UnknownPlaceholder):
        values.substitute("{{secret:ANTHROPIC_API_KEY}}")


def test_allow_list_comes_from_the_environment():
    assert secret_names_from_env({}) == DEFAULT_SECRET_NAMES
    assert secret_names_from_env({"BAG_SECRET_NAMES": " A , B ,"}) == ("A", "B")
    values = Values(secrets={"A": "value-a", "BANK_USER": "x"}, secret_names=["A"])
    assert values.secret_names == ["A"]


def test_redact_scrubs_secrets_from_page_text():
    values = make_values()
    assert values.redact("Welcome teller-xyz!") == "Welcome {{secret:BANK_USER}}!"
    assert "hunter2-pw" not in values.redact("textbox: hunter2-pw")
    assert values.redact("nothing secret") == "nothing secret"


def test_short_secrets_are_not_scrubbed_everywhere():
    values = Values(secrets={"BANK_USER": "ab"})
    assert values.redact("a table about ab") == "a table about ab"


def test_protect_turns_literal_values_back_into_placeholders():
    values = make_values(member_id="1001")
    assert values.protect("1001") == "{{member_id}}"  # whole text equals an input
    assert values.protect("teller-xyz") == "{{secret:BANK_USER}}"
    assert values.protect("pw is hunter2-pw ok") == "pw is {{secret:BANK_PASSWORD}} ok"
    assert values.protect("member 1001") == "member 1001"  # only exact input matches are swapped
    assert values.protect("{{member_id}}") == "{{member_id}}"
