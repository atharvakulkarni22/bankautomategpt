"""The Action model: the one thing the AI can ask for, one step at a time.

The `act` tool the model calls has this class as its input schema.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bag.surface import Target

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
    target: Target | None = Field(None, description="The element to act on. Needed for click, type, read, wait.")
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
