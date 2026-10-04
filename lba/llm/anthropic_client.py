"""Adapter for Anthropic (Claude)."""

import base64

from .base import LLMResponse, Message, ToolCall, ToolSpec

DEFAULT_MAX_TOKENS = 4096  # Anthropic requires a limit on every request

# These models answer HTTP 400 to a forced tool_choice, so for them we send a
# normal request and rely on the prompt to ask for the tool.
NO_FORCED_TOOL = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5", "claude-sonnet-5-5")


class AnthropicClient:
    def __init__(self, model, api_key=None, client=None, effort=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.client = client
        self.effort = effort  # optional: low | medium | high | xhigh | max (thinking depth and speed)

    def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": self._messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            if force_tool and not self.model.startswith(NO_FORCED_TOOL):
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        response = self.client.messages.create(**kwargs)

        text, calls = "", []
        for block in response.content:
            if block.type == "text":
                text += block.text
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
        return LLMResponse(text=text, tool_calls=calls, stop_reason=response.stop_reason, raw=response.content)

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict]:
        out = []
        for m in messages:
            if m.role == "assistant":
                content = []
                if m.text:
                    content.append({"type": "text", "text": m.text})
                for c in m.tool_calls:
                    content.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments})
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text}
                # Anthropic wants all results for one turn in a single user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"][-1].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.images:
                images = [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.standard_b64encode(png).decode()}}
                    for png in m.images
                ]
                out.append({"role": "user", "content": images + [{"type": "text", "text": m.text}]})
            else:
                out.append({"role": "user", "content": m.text})
        return out
