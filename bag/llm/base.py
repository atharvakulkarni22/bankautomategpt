"""Provider-neutral types: the one vocabulary the rest of the project uses.

Anthropic, Gemini and OpenAI each want messages in a different shape. The agent
only ever talks in the types below; each adapter translates to and from its
provider. That way switching provider is a config change, not a code change.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolSpec:
    """A tool the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """The model asking us to run a tool."""

    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    """One entry in the conversation.

    role "user":      text from us
    role "assistant": the model's reply (text and/or tool_calls)
    role "tool":      the result of running a tool call (tool_call_id, tool_name, text)

    `raw` holds the provider's own copy of an assistant reply. Some providers
    (Gemini) need it handed back unchanged on the next turn, so always append
    `response.to_message()` to the history rather than rebuilding it.
    """

    role: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    raw: Any = None
    images: list[bytes] = field(default_factory=list)  # PNG bytes, sent with a "user" message


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    raw: Any = None

    def to_message(self) -> Message:
        return Message(role="assistant", text=self.text, tool_calls=self.tool_calls, raw=self.raw)


class LLMClient(Protocol):
    """What every provider adapter offers.

    force_tool="name" means "the model must answer by calling that tool". It is
    a request, not a promise: some models (for example claude-sonnet-5-5) reject
    forced tool use, so their adapters fall back to a normal request. Callers
    must therefore still cope with a reply that contains no tool call.
    """

    def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        force_tool: str | None = None,
    ) -> LLMResponse: ...
