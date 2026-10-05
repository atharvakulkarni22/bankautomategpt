"""The Action model: the one thing the AI can ask for, one step at a time.

The `act` tool the model calls has this class as its input schema.
"""

import json
import re
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bag.surface import Target

VALID_TARGET_EXAMPLES = "{role, name} | {label} | {text} | {css}"

TARGET_GUIDANCE = (
    "The element to act on (needed for click, type, read, wait). Use role+name for buttons, links and textboxes "
    '(preferred), for example {"role": "button", "name": "Sign On"}. Use label for a field that has a label, for '
    'example {"label": "User name:"}. Use text only for plain text that is not a control, for example '
    '{"text": "Member Details"}. Use css only for fields with no accessible name, for example '
    '{"css": "input[name=\\"mid\\"]"}. Never combine role with text: a button is {role, name}, never {role, text}.'
)

# What each action needs besides `reason`.
REQUIRED_FIELDS = {
    "click": ["target"],
    "type": ["target", "text"],
    "read": ["target", "output_name"],
    "wait": ["target"],
    "done": [],
    "ask_human": ["text"],
}


class Action(BaseModel):
    """The next step. Always fill in `reason`; the other fields depend on `action`."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["click", "type", "read", "wait", "done", "ask_human"] = Field(
        description="click: press a button or link. type: enter text in a field. read: read a value from the page. "
        "wait: wait for an element to appear. done: the goal is achieved. ask_human: stuck or need a decision."
    )
    target: Target | None = Field(None, description=TARGET_GUIDANCE)
    text: str | None = Field(
        None,
        description="type: the text to enter, using {{input_name}} or {{secret:NAME}} placeholders. "
        "ask_human: your question. done: a short summary.",
    )
    output_name: str | None = Field(
        None, description="read: a short snake_case name for the value, e.g. savings_balance."
    )
    reason: str = Field("", description="One short sentence: why this step.")

    @model_validator(mode="after")
    def _has_what_the_action_needs(self):
        missing = [name for name in REQUIRED_FIELDS[self.action] if getattr(self, name) is None]
        if missing:
            raise ValueError(f"action '{self.action}' needs: {', '.join(missing)}")
        if self.action == "ask_human" and not self.text.strip():
            raise ValueError("action 'ask_human' needs a non-empty question in text")
        if self.output_name is not None and not self.output_name.isidentifier():
            raise ValueError("output_name must be a simple name like savings_balance")
        return self

    def protected(self, values) -> "Action":
        """Copy with any real value in `text` turned back into a placeholder (see Values.protect)."""
        if self.text is None:
            return self
        return self.model_copy(update={"text": values.protect(self.text)})

    def summary(self) -> str:
        """One line for the AI's step history, e.g.  type Target(css='...') text='{{member_id}}'."""
        parts = [self.action]
        if self.target is not None:
            parts.append(str(self.target))
        if self.text is not None:
            parts.append(f"text={self.text!r}")
        if self.output_name is not None:
            parts.append(f"output_name={self.output_name}")
        return " ".join(parts)


def repair_target(target):
    if not isinstance(target, dict):
        return None
    used = {key for key, value in target.items() if value is not None}
    if {"role", "text"} <= used and used <= {"role", "text", "exact"}:
        fixed = {key: value for key, value in target.items() if value is not None and key != "text"}
        fixed["name"] = target["text"]
        return fixed
    return None


def normalize_action(raw):
    if not isinstance(raw, dict):
        return raw, None
    fixed = repair_target(raw.get("target"))
    if fixed is None:
        return raw, None
    return {**raw, "target": fixed}, {"rule": "role+text -> role+name", "original": raw["target"], "fixed": fixed}


def _drop_none(value):
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def action_signature(item) -> str:
    data = item.model_dump(mode="json") if isinstance(item, Action) else item
    data = _drop_none({key: value for key, value in data.items() if key != "reason"}) if isinstance(data, dict) else data
    return json.dumps(data, sort_keys=True, default=str)


PASSWORD_LIKE = re.compile(r"password|passcode|passwd|\bpw\b", re.IGNORECASE)


def is_password_field(target) -> bool:
    return any(PASSWORD_LIKE.search(part) for part in (target.name, target.label, target.text, target.css) if part)


def describe_target(target) -> str:
    if target.role is not None:
        return f'{target.role} "{target.name}"' if target.name else target.role
    if target.label is not None:
        return f'field labelled "{target.label}"'
    if target.text is not None:
        return f'element with text "{target.text}"'
    return f"element {target.css}"


def _attempt(action, where) -> str:
    verbs = {"click": "click", "type": "type into", "read": "read", "wait": "wait for"}
    return f"tried to {verbs.get(action.action, action.action)} {where}" if where else f"tried to {action.action}"


def narrate(action, status, result, repaired=None) -> str:
    where = None
    if action.target is not None:
        where = "password field" if is_password_field(action.target) else describe_target(action.target)
    if status == "ok":
        if action.action == "click":
            line = f"clicked {where} - OK"
        elif action.action == "type":
            hidden = "{{secret:" in (action.text or "") or is_password_field(action.target)
            shown = "(value hidden)" if hidden else f"(value {action.text})"
            line = f"typed into {where} {shown} - OK"
        elif action.action == "read":
            line = f"{result} - OK"
        elif action.action == "wait":
            line = f"waited for {where} - OK"
        else:
            line = f"{action.action} - OK"
    elif status == "blocked":
        line = f"{_attempt(action, where)} - {result}"
    else:
        line = f"{_attempt(action, where)} - FAILED: {result}"
    if repaired:
        line += " (your {role, text} target was repaired to {role, name})"
    return line


class HistoryEntry(NamedTuple):
    index: int
    summary: str
    result: str
    line: str
    status: str
    problem: str = ""


def history_entry(index, action, status, result, repaired=None) -> HistoryEntry:
    return HistoryEntry(index, action.summary(), result, narrate(action, status, result, repaired), status)


def invalid_entry(index, summary, problem, sent=None) -> HistoryEntry:
    suffix = f" (you sent: {sent})" if sent else ""
    return HistoryEntry(index, summary, f"INVALID: {problem}", f"{summary} - INVALID: {problem}{suffix}", "invalid", problem)


def tool_schema() -> dict:
    """JSON Schema for the `act` tool, simplified so every LLM provider accepts it.

    pydantic writes $ref/$defs and "anyOf: [X, null]" for optional fields. Some
    providers choke on those, so they are inlined and flattened here.
    """
    schema = Action.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node, names_are_keys=False):
        if names_are_keys:  # the "properties" dict: keys are field names, keep them all
            return {name: walk(sub) for name, sub in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].split("/")[-1]])
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:  # "X or null" -> just X (the field is simply optional)
                rest = {k: v for k, v in node.items() if k != "anyOf"}
                return {**walk(options[0]), **walk(rest)}
        cleaned = {}
        for key, value in node.items():
            if key == "default" or (key == "title" and isinstance(value, str)):
                continue  # noise for the model
            cleaned[key] = walk(value, names_are_keys=(key == "properties"))
        return cleaned

    return walk(schema)
