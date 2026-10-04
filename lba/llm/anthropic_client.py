"""Adapter for Anthropic (Claude)."""

from .base import LLMResponse, Message, ToolCall, ToolSpec

DEFAULT_MAX_TOKENS = 4096  # Anthropic requires a limit on every request


class AnthropicClient:
    def __init__(self, model, api_key=None, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.client = client

    def complete(self, system, messages, tools=None, max_tokens=None):
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
            else:
                out.append({"role": "user", "content": m.text})
        return out
