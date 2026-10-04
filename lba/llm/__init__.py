"""Provider-neutral LLM access. Use get_client(); never import an SDK elsewhere.

Only discovery (lba/agent) may use this package. Replay must never import it.
"""

from .base import LLMClient, LLMResponse, Message, ToolCall, ToolSpec
from .factory import LLMConfigError, get_client

__all__ = ["LLMClient", "LLMConfigError", "LLMResponse", "Message", "ToolCall", "ToolSpec", "get_client"]
