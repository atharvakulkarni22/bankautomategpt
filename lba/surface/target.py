"""Target: a description of ONE element on screen, in a way that survives replays.

A Target uses exactly one strategy:

    role (+ name)  an accessibility role and its name, e.g. button "Search"
    label          the text of the field's <label>
    text           visible text of the element
    css            a CSS selector (the last resort)

It says nothing about HOW to find the element. That is the surface's job, so the
same Target can be stored in an artifact and used later without an LLM.
"""

from pydantic import BaseModel, ConfigDict, model_validator


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # frozen = hashable, safe to compare

    role: str | None = None
    name: str | None = None  # only together with role
    label: str | None = None
    text: str | None = None
    css: str | None = None
    # True = whole-text match; False (default) = substring, ignoring case.
    # Applies to name, label and text; css ignores it.
    exact: bool = False

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
