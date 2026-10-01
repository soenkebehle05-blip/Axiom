"""Provider-neutral LLM interface used by the agent loop.

Each backend owns its native history format (the agent only appends what the backend
hands back), converts the shared ToolSpecs to its own tool schema, and streams a flat
sequence of LLMEvents: text deltas, completed tool calls, and finally the assistant message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMEvent:
    kind: str  # "text" | "tool_call" | "assistant"
    text: str = ""
    call: ToolCall | None = None
    message: Any = None  # native assistant message to append to history (kind == "assistant")


class LLM(Protocol):
    model: str

    def user_message(self, text: str) -> Any: ...

    def tool_results(self, results: list[tuple[ToolCall, dict]]) -> Any: ...  # one message, or a list of messages

    def assistant_message(
        self, text: str
    ) -> Any: ...  # a plain assistant turn the server wrote itself (templated replies)

    def stream(
        self, system_static: str, system_dynamic: str, history: list[Any], tools: list[ToolSpec]
    ) -> AsyncIterator[LLMEvent]: ...

    async def warm(self) -> None: ...  # open/keep a TLS connection so the first turn does not pay the handshake


# httpx drops idle keep-alive connections after 5 s by default, so every conversational turn would
# reconnect (~200-300 ms of TCP+TLS). Keep them for ten minutes instead.
KEEPALIVE = {"max_keepalive_connections": 10, "max_connections": 50, "keepalive_expiry": 600.0}


def build_llm(settings) -> LLM:
    """Pick the provider from settings (LLM_PROVIDER=auto|anthropic|gemini|openai)."""
    provider = settings.llm_provider.lower()
    if provider == "auto":
        if not (settings.anthropic_api_key or settings.gemini_api_key or settings.openai_api_key):
            raise RuntimeError("No LLM key configured: set ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENAI_API_KEY")
        provider = "anthropic" if settings.anthropic_api_key else "gemini" if settings.gemini_api_key else "openai"
    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured")
        from .llm_claude import ClaudeLLM

        return ClaudeLLM(
            settings.anthropic_api_key, settings.anthropic_model, settings.anthropic_effort, settings.anthropic_thinking
        )
    if provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        from .llm_gemini import GeminiLLM

        return GeminiLLM(
            settings.gemini_api_key,
            settings.gemini_model,
            settings.gemini_thinking_level,
            settings.gemini_fallback_model,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        from .llm_openai import OpenAICompatibleLLM

        return OpenAICompatibleLLM(settings.openai_api_key, settings.openai_model, settings.openai_base_url)
    raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}")
