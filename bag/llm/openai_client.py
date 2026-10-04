"""Adapter for OpenAI, and for ANY server that speaks the OpenAI chat format.

Set BAG_BASE_URL to reach those: Ollama, LM Studio, OpenRouter, Groq, Together,
vLLM and many others all offer an OpenAI-compatible endpoint. This one adapter is
how the project supports "any other LLM".
"""

import base64
import json

from .base import LLMResponse, Message, ToolCall, ToolSpec


class OpenAIClient:
    def __init__(self, model, api_key=None, base_url=None, client=None):
        if client is None:
            import openai

            # Local servers (Ollama etc.) need no key, but the SDK insists on one.
            client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url or None)
        self.model = model
        self.client = client

    def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
        kwargs = {"model": self.model, "messages": self._messages(system, messages)}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            if force_tool:
                kwargs["tool_choice"] = {"type": "function", "function": {"name": force_tool}}
        # Only sent when asked for: newer OpenAI models and some servers disagree
        # about the parameter name, so the default is to leave it out.
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=json.loads(c.function.arguments or "{}"))
            for c in (choice.message.tool_calls or [])
        ]
        return LLMResponse(
            text=choice.message.content or "", tool_calls=calls, stop_reason=choice.finish_reason, raw=choice.message
        )

    @staticmethod
    def _messages(system: str, messages: list[Message]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "assistant":
                entry = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in m.tool_calls
                    ]
                out.append(entry)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
            elif m.images:
                parts = [{"type": "text", "text": m.text}] + [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}
                    for png in m.images
                ]
                out.append({"role": "user", "content": parts})
            else:
                out.append({"role": "user", "content": m.text})
        return out
