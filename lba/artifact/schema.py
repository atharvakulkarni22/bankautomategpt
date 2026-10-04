"""The artifact: a saved, reusable description of ONE task in a legacy app.

Discovery (the AI) works the task out once. The artifact is what it leaves
behind: plain data that the replay engine can follow later with no AI at all.
It is stored as a YAML file, so a human can read it, review it and edit it.

Read it top to bottom like a recipe:

    metadata            what this is and whether a human approved it
    inputs              the values you must supply each time (e.g. member_id)
    steps               the actions to perform, in order
    outputs             the values that come back (e.g. savings_balance)
    success_check       how to tell the run really worked
    known_outcomes      pages that mean "finished, but with a different answer"
    known_interruptions things that pop up and must be dismissed

Real credentials never appear here. Anything secret is a placeholder such as
{{secret:BANK_PASSWORD}}, filled in from .env at replay time.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from lba.surface.placeholders import PLACEHOLDER
from lba.surface.target import Target

ValueType = Literal["string", "int", "decimal"]
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Strict(BaseModel):
    # A typo in a hand-edited YAML file (say "fallback:" instead of "fallbacks:")
    # is an error, not something silently ignored.
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------- metadata


class Metadata(_Strict):
    # Short id made of lowercase letters, digits, "-" and "_". It becomes the file
    # name: artifacts/<name>.v<version>.yaml
    name: str
    # Whole number starting at 1. If the task changes, make a NEW version instead
    # of editing an old one, so older runs can still be explained.
    version: int = Field(1, ge=1)
    # The human name of the application this task runs in.
    app: str
    # One or two sentences: what this task does.
    description: str = ""
    # draft    = built from a recording; nobody has checked it yet.
    # approved = a human reviewed it (lba approve) and it may be replayed.
    status: Literal["draft", "approved"] = "draft"
    # The page replay begins on. Replay may override the host (BANK_URL).
    start_url: str
    # The recording file this was built from, so you can trace it back. Optional.
    source_recording: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_file_safe(cls, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise ValueError("name may only use lowercase letters, digits, '-' and '_'")
        return name


# --------------------------------------------------------------------- inputs


class Input(_Strict):
    # How steps refer to it: {{name}}. Letters, digits and "_" only.
    name: str
    # What kind of value it is. The replay engine checks the value before it
    # touches the app: string = any text, int = whole number, decimal = number
    # that may have a fractional part (money).
    type: ValueType = "string"
    # Plain-English help for whoever supplies the value.
    description: str = ""
    # Optional regular expression the WHOLE value must match, e.g. "[0-9]{4,8}"
    # for a member id. Catches typos before anything is typed into a bank.
    pattern: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, name):
        if not IDENTIFIER.match(name):
            raise ValueError(f"'{name}' must be letters, digits and underscores, not starting with a digit")
        return name

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, pattern):
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"not a valid regular expression: {error}") from error
        return pattern

    def check(self, value: str) -> str:
        """Validate a supplied value and return it in a standard form. Raises ValueError."""
        text = value.strip()
        if self.pattern is not None and not re.fullmatch(self.pattern, text):
            raise ValueError(f"input '{self.name}' does not match the required pattern {self.pattern}")
        try:
            if self.type == "int":
                return str(int(text))
            if self.type == "decimal":
                number = Decimal(text)
                if not number.is_finite():
                    raise InvalidOperation
                return str(number)
        except (ValueError, InvalidOperation) as error:
            raise ValueError(f"input '{self.name}' must be a {self.type}") from error
        return text


# -------------------------------------------------------------------- locators


class Locator(_Strict):
    # The best way to find the element. Targets are tried in order of how
    # meaningful they are: role+name, then label, then text, then css.
    primary: Target
    # Backup ways to find the SAME element, used in order if the primary stops
    # working (say the page layout shifts). Replay only uses these when it
    # has to, and logs when it does.
    fallbacks: list[Target] = []
    # Why this element: the reason the agent gave, so a reviewer can follow the thinking.
    why: str = ""

    def ordered(self) -> list[Target]:
        """The primary followed by the fallbacks: the order replay tries them in."""
        return [self.primary, *self.fallbacks]

    # Keep the YAML tidy: leave out `exact: false` and other defaults on targets.
    @field_serializer("primary")
    def _slim_primary(self, target):
        return target.model_dump(exclude_none=True, exclude_defaults=True)

    @field_serializer("fallbacks")
    def _slim_fallbacks(self, targets):
        return [t.model_dump(exclude_none=True, exclude_defaults=True) for t in targets]


# ----------------------------------------------------------------------- steps


class Expected(_Strict):
    # What the world should look like right AFTER the step. Replay checks it, so
    # a step that quietly did nothing is caught at once, not three steps later.
    # The page address must contain this text, e.g. "/home". Optional.
    url_contains: str | None = None
    # This text must be visible somewhere on the page (including iframes). Optional.
    text_visible: str | None = None


class Step(_Strict):
    # click = press a button/link, type = enter text, read = read a value off the
    # page into an output, wait = wait until the element shows up.
    action: Literal["click", "type", "read", "wait"]
    # Which element the action is on.
    locator: Locator
    # Only for "type": the text to enter. May hold {{input_name}} and
    # {{secret:NAME}} placeholders, filled in at replay time.
    text: str | None = None
    # Only for "read": the name of the output the value goes into.
    output_name: str | None = None
    # Optional check that the step worked. See Expected.
    expected: Expected | None = None

    @model_validator(mode="after")
    def _fields_match_the_action(self):
        if self.action == "type" and self.text is None:
            raise ValueError("a 'type' step needs text")
        if self.action != "type" and self.text is not None:
            raise ValueError(f"a '{self.action}' step must not have text")
        if self.action == "read" and not self.output_name:
            raise ValueError("a 'read' step needs output_name")
        if self.action != "read" and self.output_name is not None:
            raise ValueError(f"a '{self.action}' step must not have output_name")
        return self


# --------------------------------------------------------------------- outputs


class Output(_Strict):
    # Matches the output_name of the "read" step that fills it.
    name: str
    # What kind of value it is. A decimal is cleaned up ("$12,450.75" becomes
    # "12450.75") so callers get a number, not a formatted string.
    type: ValueType = "string"
    # Plain-English help for whoever uses the result.
    description: str = ""
    # Optional regular expression picking the value out of the text that was
    # read. If it has a group, the first group is used, else the whole match.
    # Example: "Balance: (.*)" when the page says "Balance: $5.00".
    extract: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_identifier(cls, name):
        if not IDENTIFIER.match(name):
            raise ValueError(f"'{name}' must be letters, digits and underscores, not starting with a digit")
        return name

    @field_validator("extract")
    @classmethod
    def _extract_compiles(cls, extract):
        if extract is not None:
            try:
                re.compile(extract)
            except re.error as error:
                raise ValueError(f"not a valid regular expression: {error}") from error
        return extract

    def parse(self, raw: str) -> str:
        """Turn text read from the page into the output's standard form. Raises ValueError.

        Error messages never repeat the text itself: it is customer data.
        """
        text = raw.strip()
        if self.extract is not None:
            match = re.search(self.extract, text)
            if not match:
                raise ValueError(f"output '{self.name}': the extract pattern did not match the text read")
            text = (match.group(1) if match.re.groups else match.group(0)).strip()
        try:
            if self.type == "int":
                return str(int(re.sub(r"[,\s]", "", text)))
            if self.type == "decimal":
                return str(Decimal(re.sub(r"[^\d.\-]", "", text)))  # drop $, commas, spaces
        except (ValueError, InvalidOperation) as error:
            raise ValueError(f"output '{self.name}' could not be read as a {self.type}") from error
        return text


class SuccessCheck(_Strict):
    # Outputs that must all have been read (and be non-empty) for the run to count as a success.
    outputs_present: list[str] = []
    # Text that must be visible at the very end. Optional.
    text_visible: str | None = None


# ------------------------------------------------- outcomes and interruptions


class Outcome(_Strict):
    # If this text shows up on the page, the task is over but the answer is not the
    # usual one. Example: "No member found".
    text: str
    # A short label for that result, in CAPITALS_WITH_UNDERSCORES, e.g. NOT_FOUND.
    # Replay reports it instead of treating the run as broken.
    outcome: str
    # Plain-English meaning, for humans.
    description: str = ""

    @field_validator("outcome")
    @classmethod
    def _outcome_is_a_label(cls, outcome):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", outcome):
            raise ValueError("outcome must be CAPITALS_WITH_UNDERSCORES, e.g. NOT_FOUND")
        return outcome


class Interruption(_Strict):
    # If this text appears on the page at any point, something unplanned got in the way.
    # Example: "System maintenance notice".
    text: str
    # What to do about it. For now only "click": press the element in `locator`.
    action: Literal["click"] = "click"
    # The element to act on, e.g. the OK button of the popup.
    locator: Locator


# -------------------------------------------------------------------- artifact


class Artifact(_Strict):
    metadata: Metadata
    inputs: list[Input] = []
    steps: list[Step] = Field(min_length=1)
    outputs: list[Output] = []
    success_check: SuccessCheck = SuccessCheck()
    known_outcomes: list[Outcome] = []
    known_interruptions: list[Interruption] = []

    @model_validator(mode="after")
    def _everything_fits_together(self):
        input_names = [i.name for i in self.inputs]
        output_names = [o.name for o in self.outputs]
        for kind, names in (("input", input_names), ("output", output_names)):
            if len(set(names)) != len(names):
                raise ValueError(f"two {kind}s have the same name")

        # Every {{placeholder}} typed by a step must be a declared input.
        # (Secrets are checked at replay time against the allow-list in .env.)
        for number, step in enumerate(self.steps, start=1):
            for match in PLACEHOLDER.finditer(step.text or ""):
                is_secret, name = match.group(1), match.group(2)
                if not is_secret and name not in input_names:
                    raise ValueError(f"step {number} uses {{{{{name}}}}} but no input has that name")

        # Reads and outputs must pair up.
        read_names = {s.output_name for s in self.steps if s.action == "read"}
        for name in read_names - set(output_names):
            raise ValueError(f"a read step fills '{name}' but no output has that name")
        for name in set(output_names) - read_names:
            raise ValueError(f"output '{name}' is never read by any step")
        for name in self.success_check.outputs_present:
            if name not in output_names:
                raise ValueError(f"success_check needs output '{name}' which does not exist")
        return self
