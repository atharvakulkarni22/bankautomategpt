"""Pick the LLM provider from configuration (.env or real environment).

To switch provider, change two lines in .env:

    BAG_PROVIDER=gemini            # anthropic | gemini | openai
    BAG_MODEL=<model name for that provider>

and make sure that provider's key is set (ANTHROPIC_API_KEY, GEMINI_API_KEY or
OPENAI_API_KEY). For any other LLM, use BAG_PROVIDER=openai with BAG_BASE_URL
pointing at its OpenAI-compatible endpoint.
"""

import os

from .base import LLMClient

# provider -> environment variable holding its API key
KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class LLMConfigError(RuntimeError):
    """The LLM settings are missing or wrong. The message says how to fix it."""


def get_client(provider=None, model=None, env=None) -> LLMClient:
    """Build a client. Explicit arguments win over the environment."""
    env = os.environ if env is None else env
    provider = (provider or env.get("BAG_PROVIDER") or "anthropic").strip().lower()
    model = (model or env.get("BAG_MODEL") or "").strip()

    if provider not in KEY_VARS:
        raise LLMConfigError(f"Unknown BAG_PROVIDER '{provider}'. Choose one of: {', '.join(KEY_VARS)}.")
    if not model:
        raise LLMConfigError(f"BAG_MODEL is not set. Put the {provider} model name you want in .env.")

    base_url = (env.get("BAG_BASE_URL") or "").strip()
    api_key = (env.get(KEY_VARS[provider]) or "").strip()
    # Only a custom OpenAI-compatible server (e.g. Ollama) may go without a key.
    keyless_ok = provider == "openai" and bool(base_url)
    if not api_key and not keyless_ok:
        raise LLMConfigError(f"{KEY_VARS[provider]} is not set (needed for BAG_PROVIDER={provider}).")

    # Imported here so each SDK is only loaded if you actually use it.
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        # BAG_EFFORT (optional) trades thinking depth for speed on Claude models.
        return AnthropicClient(model, api_key=api_key, effort=(env.get("BAG_EFFORT") or "").strip() or None)
    if provider == "gemini":
        from .gemini_client import GeminiClient

        return GeminiClient(model, api_key=api_key)
    from .openai_client import OpenAIClient

    return OpenAIClient(model, api_key=api_key, base_url=base_url)
