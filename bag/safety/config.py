"""The safety rules, loaded from config/safety.yaml and validated.

The rules live in a file, not in code, so a security reviewer can read and change
them without touching Python. If the file is missing or wrong, nothing runs:
with no rules, the safe answer is "no".
"""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_CONFIG_PATH = Path("config/safety.yaml")


class SafetyConfigError(Exception):
    """The safety config could not be loaded. The message says what to fix."""


def _compiles(pattern: str | None) -> str | None:
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"not a valid regular expression: {error}") from error
    return pattern


class ActionRule(BaseModel):
    """One kind of risky action. It matches when ALL the parts you filled in match."""

    model_config = ConfigDict(extra="forbid")

    # Shown to the human in the approval prompt and written to the audit log.
    name: str = Field(min_length=1)
    # Which kind of action: click, type, read or wait.
    action: Literal["click", "type", "read", "wait"]
    # Optional regular expression tested against the text being typed (placeholders such as
    # {{secret:BANK_PASSWORD}} are what is tested, never real values). Case-insensitive.
    text_matches: str | None = None
    # Optional regular expression tested against everything we know about the element:
    # its accessible name, label, visible text and css selector. Case-insensitive.
    target_matches: str | None = None

    _check_text = field_validator("text_matches")(_compiles)
    _check_target = field_validator("target_matches")(_compiles)


class SafetyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The only places the browser may be, main page AND iframes. Each entry is a web
    # address; a bare origin like http://127.0.0.1:5000 allows every page on it.
    allowed_urls: list[str] = Field(min_length=1)
    # Kinds of action that always need a human's yes. See ActionRule.
    risky_actions: list[ActionRule] = []
    # A click on an element whose name, label, text or css contains one of these WORDS
    # (whole words, any capitalisation) needs a human's yes.
    risky_button_names: list[str] = []
    # Elements to blur in every screenshot, as css selectors. Fields holding a secret and text
    # that looks like an account number are always blurred as well.
    blur_selectors: list[str] = ["input[type=password]"]
    # Turn screenshot blurring off only for debugging on fake data.
    blur_screenshots: bool = True
    # Every decision is appended here, one JSON object per line. Leave empty for no file.
    audit_log: Path | None = Path("evidence/safety-decisions.jsonl")

    @field_validator("allowed_urls")
    @classmethod
    def _urls_are_web_addresses(cls, urls):
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"'{url}' must be a web address like http://127.0.0.1:5000")
        return urls

    @field_validator("risky_button_names")
    @classmethod
    def _names_are_words(cls, names):
        if any(not name.strip() for name in names):
            raise ValueError("a risky button name must not be empty")
        return [name.strip() for name in names]


def load_safety_config(path=DEFAULT_CONFIG_PATH) -> SafetyConfig:
    """Read and validate the safety config. Raises SafetyConfigError with a readable message."""
    path = Path(path)
    if not path.is_file():
        raise SafetyConfigError(
            f"No safety config at {path}. Nothing runs without one: it says which sites the browser may "
            "visit and which actions need a human's approval."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise SafetyConfigError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise SafetyConfigError(f"{path} does not look like a safety config (expected a mapping at the top).")
    try:
        return SafetyConfig.model_validate(data)
    except ValidationError as error:
        problems = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()[:8]]
        raise SafetyConfigError(f"{path} is not a valid safety config:\n" + "\n".join(problems)) from error
