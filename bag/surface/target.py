"""Target: a description of ONE element on screen, in a way that survives replays.

A Target uses exactly one strategy:

    role (+ name)  an accessibility role and its name, e.g. button "Search"
    label          the text of the field's <label>
    text           visible text of the element
    css            a CSS selector (the last resort)

It says nothing about HOW to find the element. That is the surface's job, so the
same Target can be stored in an artifact and used later without an LLM.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Target(BaseModel):
    """One element on the page. Set exactly one of role, label, text or css."""

    model_config = ConfigDict(extra="forbid", frozen=True)  # frozen = hashable, safe to compare

    # The descriptions below are shown to the AI (they end up in its tool schema).
    role: str | None = Field(None, description="Accessibility role, e.g. button, link, textbox, heading, combobox. Use with name.")
    name: str | None = Field(None, description="Accessible name of the element, as shown in the tree. Only together with role.")
    label: str | None = Field(None, description="Text of the field's <label>.")
    text: str | None = Field(None, description="Visible text of the element.")
    css: str | None = Field(None, description="CSS selector. Last resort; use the ones listed for fields with no accessible name.")
    # True = whole-text match; False (default) = substring, ignoring case.
    # Applies to name, label and text; css ignores it.
    exact: bool = Field(False, description="True: match the whole name/label/text. False: substring match.")

    @model_validator(mode="after")
    def _exactly_one_strategy(self):
        used = [s for s in ("role", "label", "text", "css") if getattr(self, s) is not None]
        if len(used) != 1:
            raise ValueError(f"A Target needs exactly one of role, label, text, css (got {used or 'none'}).")
        if self.name is not None and self.role is None:
            raise ValueError("'name' only makes sense together with 'role'.")
        return self

    def __str__(self):
        parts = [f"{k}={v!r}" for k, v in self.model_dump(exclude_none=True, exclude_defaults=True).items()]
        return "Target(" + ", ".join(parts) + ")"
