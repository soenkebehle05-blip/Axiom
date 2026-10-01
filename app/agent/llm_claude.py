"""Claude backend (official `anthropic` SDK, streaming Messages API with tool use)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx

from .llm import KEEPALIVE, LLMEvent, ToolCall, ToolSpec

log = logging.getLogger(__name__)

# used when anthropic's model refuses a request; triggers a server-side fallback for model routing
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models older than the 4.6 generation take budget_tokens instead of adaptive thinking and no effort param.
LEGACY_THINKING_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-3")
# Server-side refusal fallbacks are a 5-family feature.
FALLBACK_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")


class ClaudeLLM:
    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-5",
        effort: str = "low",
        thinking: str = "adaptive",
        max_tokens: int = 1024,
    ):
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, http_client=anthropic.DefaultAsyncHttpxClient(limits=httpx.Limits(**KEEPALIVE))
        )
        self.model = model
        self.effort = effort
        self.thinking = thinking  # "adaptive" | "off"
        self.max_tokens = max_tokens  # spoken replies are short; keeps a runaway answer bounded

    def _request_kwargs(self) -> dict:
        legacy = self.model.startswith(LEGACY_THINKING_PREFIXES)
        kwargs: dict = {}
        if not legacy:
            kwargs["output_config"] = {"effort": self.effort}
            kwargs["thinking"] = {"type": "disabled"} if self.thinking == "off" else {"type": "adaptive"}
        if self.model.startswith(FALLBACK_PREFIXES):
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"  # a safety refusal is re-run server-side on a fallback model
        return kwargs

    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def assistant_message(self, text: str) -> dict:
        return {"role": "assistant", "content": text}

    def tool_results(self, results: list[tuple[ToolCall, dict]]) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result),
                    "is_error": "error" in result,
                }
                for call, result in results
            ],
        }

    async def warm(self) -> None:
        try:
            await self.client.models.list(limit=1)  # no tokens billed; establishes the TLS connection
        except Exception as exc:  # warming is best effort
            log.debug("warm-up failed: %s", exc)

    async def stream(
        self, system_static: str, system_dynamic: str, history: list[Any], tools: list[ToolSpec]
    ) -> AsyncIterator[LLMEvent]:
        # Static prompt first with a cache breakpoint; the per-turn date facts come after it so
        # they never invalidate the cached prefix (tools -> system -> messages).
        system = [
            {"type": "text", "text": system_static, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_dynamic},
        ]
        tool_params = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
        kwargs = self._request_kwargs()
        api = self.client.beta.messages if "betas" in kwargs else self.client.messages
        async with api.stream(
            model=self.model, max_tokens=self.max_tokens, system=system, tools=tool_params, messages=history, **kwargs
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    yield LLMEvent("text", text=event.text)
            message = await stream.get_final_message()

        if message.stop_reason == "refusal":
            log.warning("Claude refused the turn (%s)", getattr(message, "stop_details", None))
        elif message.stop_reason == "max_tokens":
            log.warning("Claude hit max_tokens; tool calls in this turn are not executed")
        else:
            for block in message.content:
                if block.type == "tool_use":
                    args = block.input if isinstance(block.input, dict) else {}
                    yield LLMEvent("tool_call", call=ToolCall(id=block.id, name=block.name, args=args))
        yield LLMEvent("assistant", message={"role": "assistant", "content": message.content})
