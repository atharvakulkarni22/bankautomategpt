"""Adapter for Google Gemini (google-genai SDK)."""

from google.genai import types

from .base import LLMResponse, Message, ToolCall, ToolSpec


class GeminiClient:
    def __init__(self, model, api_key=None, client=None):
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self.model = model
        self.client = client

    def complete(self, system, messages, tools=None, max_tokens=None, force_tool=None):
        config = types.GenerateContentConfig(system_instruction=system)
        if max_tokens:
            config.max_output_tokens = max_tokens
        if tools:
            declarations = [
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ]
            config.tools = [types.Tool(function_declarations=declarations)]
            if force_tool:
                config.tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=[force_tool])
                )
        response = self.client.models.generate_content(
            model=self.model, contents=self._contents(messages), config=config
        )

        candidate = response.candidates[0]
        text, calls = "", []
        for i, part in enumerate(candidate.content.parts or []):
            if part.function_call:
                fc = part.function_call
                # Gemini may not give calls an id, so make one up.
                calls.append(ToolCall(id=fc.id or f"call_{i}", name=fc.name, arguments=dict(fc.args or {})))
            elif part.text and not part.thought:
                text += part.text
        return LLMResponse(text=text, tool_calls=calls, stop_reason=str(candidate.finish_reason), raw=candidate.content)

    @staticmethod
    def _contents(messages: list[Message]) -> list:
        out = []
        for m in messages:
            if m.role == "assistant":
                if isinstance(m.raw, types.Content):
                    # Reuse Gemini's own reply: it carries "thought signatures"
                    # that Gemini needs back when we answer a tool call.
                    out.append(m.raw)
                    continue
                parts = [types.Part(text=m.text)] if m.text else []
                parts += [types.Part.from_function_call(name=c.name, args=c.arguments) for c in m.tool_calls]
                out.append(types.Content(role="model", parts=parts))
            elif m.role == "tool":
                part = types.Part.from_function_response(name=m.tool_name, response={"result": m.text})
                # Results for one turn go together in a single "user" entry.
                if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[-1].function_response:
                    out[-1].parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
            else:
                parts = [types.Part(text=m.text)]
                parts += [types.Part.from_bytes(data=png, mime_type="image/png") for png in m.images]
                out.append(types.Content(role="user", parts=parts))
        return out
