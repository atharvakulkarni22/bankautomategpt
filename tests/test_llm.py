import json
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest
from google.genai import types

from lba.llm import LLMConfigError, Message, ToolCall, ToolSpec, get_client
from lba.llm.anthropic_client import AnthropicClient
from lba.llm.gemini_client import GeminiClient
from lba.llm.openai_client import OpenAIClient

TOOL = ToolSpec("click", "Click a button", {"type": "object", "properties": {"label": {"type": "string"}}})
CALL = ToolCall("c1", "click", {"label": "OK"})


def history():
    """user asks -> model calls two tools -> we answer both."""
    two_calls = [CALL, ToolCall("c2", "click", {"label": "Search"})]
    return [
        Message("user", "go"),
        Message("assistant", "ok", tool_calls=two_calls),
        Message("tool", "done 1", tool_call_id="c1", tool_name="click"),
        Message("tool", "done 2", tool_call_id="c2", tool_name="click"),
    ]


# --------------------------------------------------------------------- factory


def test_factory_picks_each_provider():
    anthropic = get_client(env={"LBA_PROVIDER": "anthropic", "LBA_MODEL": "m", "ANTHROPIC_API_KEY": "k"})
    gemini = get_client(env={"LBA_PROVIDER": "gemini", "LBA_MODEL": "m", "GEMINI_API_KEY": "k"})
    openai = get_client(env={"LBA_PROVIDER": "openai", "LBA_MODEL": "m", "OPENAI_API_KEY": "k"})
    assert isinstance(anthropic, AnthropicClient)
    assert isinstance(gemini, GeminiClient)
    assert isinstance(openai, OpenAIClient)


def test_factory_defaults_to_anthropic_and_arguments_win():
    env = {"LBA_MODEL": "m", "ANTHROPIC_API_KEY": "k", "GEMINI_API_KEY": "k"}
    assert isinstance(get_client(env=env), AnthropicClient)
    assert isinstance(get_client(provider="gemini", env=env), GeminiClient)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"LBA_PROVIDER": "bogus", "LBA_MODEL": "m"}, "Unknown LBA_PROVIDER"),
        ({"LBA_PROVIDER": "gemini"}, "LBA_MODEL is not set"),
        ({"LBA_PROVIDER": "gemini", "LBA_MODEL": "m"}, "GEMINI_API_KEY is not set"),
        ({"LBA_PROVIDER": "openai", "LBA_MODEL": "m"}, "OPENAI_API_KEY is not set"),
    ],
)
def test_factory_gives_clear_errors(env, message):
    with pytest.raises(LLMConfigError, match=message):
        get_client(env=env)


def test_openai_compatible_server_needs_no_key():
    env = {"LBA_PROVIDER": "openai", "LBA_MODEL": "llama3", "LBA_BASE_URL": "http://localhost:11434/v1"}
    client = get_client(env=env)
    assert str(client.client.base_url).startswith("http://localhost:11434/v1")


# ------------------------------------------------------------------- anthropic


def test_anthropic_request_and_reply():
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        blocks = [NS(type="text", text="hi"), NS(type="tool_use", id="t1", name="click", input={"label": "OK"})]
        return NS(content=blocks, stop_reason="tool_use")

    client = AnthropicClient("claude-x", client=NS(messages=NS(create=create)))
    reply = client.complete("sys", history(), tools=[TOOL])

    assert seen["system"] == "sys" and seen["model"] == "claude-x" and seen["max_tokens"] > 0
    assert seen["tools"][0]["input_schema"] == TOOL.parameters
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["user", "assistant", "user"]  # both tool results merged into one user turn
    assert [b["tool_use_id"] for b in seen["messages"][2]["content"]] == ["c1", "c2"]
    assert reply.text == "hi"
    assert reply.tool_calls == [ToolCall("t1", "click", {"label": "OK"})]


def anthropic_call(model, **options):
    """Run one complete() against a fake SDK and return what was sent."""
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return NS(content=[], stop_reason="end_turn")

    client = AnthropicClient(model, client=NS(messages=NS(create=create)), effort=options.pop("effort", None))
    client.complete("sys", [Message("user", "look", images=[b"PNGDATA"])], tools=[TOOL], force_tool="click")
    return seen


def test_anthropic_sends_images_and_forces_the_tool_where_allowed():
    sent = anthropic_call("claude-sonnet-5")  # an older model that still allows forced tool use
    assert sent["tool_choice"] == {"type": "tool", "name": "click"}
    image, text = sent["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/png"
    assert image["source"]["data"] == "UE5HREFUQQ=="  # base64 of PNGDATA
    assert text == {"type": "text", "text": "look"}


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"])
def test_anthropic_skips_forced_tool_on_models_that_reject_it(model):
    assert "tool_choice" not in anthropic_call(model)  # these answer HTTP 400 to a forced tool choice


def test_anthropic_effort_is_optional():
    assert "output_config" not in anthropic_call("claude-sonnet-5-5")
    assert anthropic_call("claude-sonnet-5-5", effort="low")["output_config"] == {"effort": "low"}


def test_factory_passes_effort_to_anthropic():
    env = {"LBA_PROVIDER": "anthropic", "LBA_MODEL": "m", "ANTHROPIC_API_KEY": "k", "LBA_EFFORT": " low "}
    assert get_client(env=env).effort == "low"
    assert get_client(env={**env, "LBA_EFFORT": ""}).effort is None


# ---------------------------------------------------------------------- openai


def test_openai_request_and_reply():
    seen = {}
    message = NS(content=None, tool_calls=[NS(id="t1", function=NS(name="click", arguments='{"label": "OK"}'))])

    def create(**kwargs):
        seen.update(kwargs)
        return NS(choices=[NS(message=message, finish_reason="tool_calls")])

    client = OpenAIClient("gpt-x", client=NS(chat=NS(completions=NS(create=create))))
    reply = client.complete("sys", history(), tools=[TOOL])

    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert "max_tokens" not in seen  # left out unless requested
    assert seen["tools"][0]["function"]["parameters"] == TOOL.parameters
    assistant = seen["messages"][2]
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"label": "OK"}
    assert [m["role"] for m in seen["messages"][3:]] == ["tool", "tool"]
    assert reply.tool_calls == [ToolCall("t1", "click", {"label": "OK"})]
    assert reply.text == ""


def test_openai_sends_images_and_forces_the_tool():
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return NS(choices=[NS(message=NS(content="ok", tool_calls=None), finish_reason="stop")])

    client = OpenAIClient("gpt-x", client=NS(chat=NS(completions=NS(create=create))))
    client.complete("sys", [Message("user", "look", images=[b"PNGDATA"])], tools=[TOOL], force_tool="click")
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "click"}}
    text, image = seen["messages"][1]["content"]
    assert text == {"type": "text", "text": "look"}
    assert image["image_url"]["url"] == "data:image/png;base64,UE5HREFUQQ=="


# ---------------------------------------------------------------------- gemini


def test_gemini_request_and_reply():
    seen = {}
    parts = [
        types.Part(text="thinking...", thought=True),
        types.Part(text="hi"),
        types.Part.from_function_call(name="click", args={"label": "OK"}),
    ]
    content = types.Content(role="model", parts=parts)

    def generate_content(**kwargs):
        seen.update(kwargs)
        return NS(candidates=[NS(content=content, finish_reason="STOP")])

    client = GeminiClient("gemini-x", client=NS(models=NS(generate_content=generate_content)))
    reply = client.complete("sys", history(), tools=[TOOL], max_tokens=50)

    assert seen["config"].system_instruction == "sys"
    assert seen["config"].max_output_tokens == 50
    declaration = seen["config"].tools[0].function_declarations[0]
    assert declaration.name == "click" and declaration.parameters_json_schema == TOOL.parameters
    sent = seen["contents"]
    assert [c.role for c in sent] == ["user", "model", "user"]  # both tool results merged
    assert len(sent[2].parts) == 2
    assert sent[2].parts[0].function_response.name == "click"
    assert reply.text == "hi"  # the "thought" part is not shown as text
    assert reply.tool_calls[0].name == "click" and reply.tool_calls[0].arguments == {"label": "OK"}
    assert reply.raw is content


def test_gemini_sends_images_and_forces_the_tool():
    seen = {}

    def generate_content(**kwargs):
        seen.update(kwargs)
        return NS(candidates=[NS(content=types.Content(role="model", parts=[types.Part(text="ok")]), finish_reason="STOP")])

    client = GeminiClient("gemini-x", client=NS(models=NS(generate_content=generate_content)))
    client.complete("sys", [Message("user", "look", images=[b"PNGDATA"])], tools=[TOOL], force_tool="click")
    mode = seen["config"].tool_config.function_calling_config
    assert str(mode.mode).endswith("ANY") and mode.allowed_function_names == ["click"]
    text_part, image_part = seen["contents"][0].parts
    assert text_part.text == "look"
    assert image_part.inline_data.data == b"PNGDATA" and image_part.inline_data.mime_type == "image/png"


def test_gemini_resends_its_own_reply_unchanged():
    original = types.Content(role="model", parts=[types.Part.from_function_call(name="click", args={})])
    messages = [Message("user", "go"), Message("assistant", tool_calls=[CALL], raw=original)]
    assert GeminiClient._contents(messages)[1] is original


# ------------------------------------------------------------ project-wide rule


def test_replay_never_imports_the_llm_layer():
    # lba.artifact is included: replay depends on it, so it must stay free of the LLM too.
    code = "import sys, lba.replay, lba.artifact; sys.exit(any(m.startswith(('lba.llm', 'lba.agent', 'anthropic', 'openai', 'google.genai')) for m in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_the_replay_command_never_loads_the_llm_either():
    """Run the real CLI entry point for `lba replay` and check what ended up imported."""
    code = """
import sys
from typer.testing import CliRunner
from lba.cli import app

CliRunner().invoke(app, ['replay', 'no-such-artifact'])  # refuses early, after all its imports ran
loaded = [m for m in sys.modules if m.startswith(('lba.llm', 'lba.agent', 'anthropic', 'openai', 'google.genai'))]
sys.exit(1 if loaded else 0)
"""
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
