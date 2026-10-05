"""Gemini backend (google-genai SDK, streaming generate_content with function calling).

Production nicety: if the primary model is unavailable (503 capacity / 429 quota) the request
is retried on a fallback model, and the fallback stays active for a cooldown so a conversation
does not ping-pong between models.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from .llm import KEEPALIVE, LLMEvent, ToolCall, ToolSpec

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 503}
FALLBACK_COOLDOWN_S = 300
# Gemini 3 rejects function_call history without a thought signature. Calls produced by a model
# with thinking off (e.g. the 2.5 fallback) have none; this documented marker skips the validator.
SKIP_SIGNATURE = b"skip_thought_signature_validator"


def thinking_config(model: str, level: str) -> types.ThinkingConfig:
    """Gemini 3.x takes thinking_level; 2.5 takes a token budget (0 = off on Flash, fastest)."""
    if model.startswith("gemini-2"):
        return types.ThinkingConfig(thinking_budget=0 if level in ("minimal", "low") else -1)
    return types.ThinkingConfig(thinking_level=types.ThinkingLevel(level.upper()))


class GeminiLLM:
    def __init__(
        self, api_key: str, model: str, thinking_level: str = "low", fallback_model: str = "", temperature: float = 0.3
    ):
        self.client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(async_client_args={"limits": httpx.Limits(**KEEPALIVE)})
        )
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else ""
        self.thinking_level = thinking_level
        self.temperature = temperature
        self._fallback_until = 0.0
        self._no_thinking: set[str] = set()  # models that rejected our thinking config

    # --- history helpers ------------------------------------------------------------
    def user_message(self, text: str) -> types.Content:
        return types.Content(role="user", parts=[types.Part.from_text(text=text)])

    def assistant_message(self, text: str) -> types.Content:
        return types.Content(role="model", parts=[types.Part.from_text(text=text)])

    def tool_results(self, results: list[tuple[ToolCall, dict]]) -> types.Content:
        return types.Content(
            role="user", parts=[types.Part.from_function_response(name=c.name, response=r) for c, r in results]
        )

    async def warm(self) -> None:
        try:
            await self.client.aio.models.get(model=self.model)
        except Exception as exc:
            log.debug("warm-up failed: %s", exc)

    # --- streaming ------------------------------------------------------------------
    @property
    def active_model(self) -> str:
        return self.fallback_model if self.fallback_model and time.time() < self._fallback_until else self.model

    def _config(self, model: str, system: str, tools: list[ToolSpec]) -> types.GenerateContentConfig:
        decls = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.input_schema)
            for t in tools
        ]
        return types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=decls)],
            thinking_config=thinking_config(model, self.thinking_level),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def _open(self, model: str, system: str, history, tools):
        config = self._config(model, system, tools)
        if model in self._no_thinking:
            config.thinking_config = None
        try:
            stream = await self.client.aio.models.generate_content_stream(model=model, contents=history, config=config)
            first = await anext(stream)  # the SDK only sends the request on first iteration
        except errors.APIError as exc:
            # Aliases such as gemini-flash-lite-latest may resolve to a generation with a different thinking
            # parameter; drop the thinking config for that model and retry once.
            if exc.code != 400 or "thinking" not in str(exc).lower() or model in self._no_thinking:
                raise
            log.info("%s rejected the thinking config; retrying without it", model)
            self._no_thinking.add(model)
            config.thinking_config = None
            stream = await self.client.aio.models.generate_content_stream(model=model, contents=history, config=config)
            first = await anext(stream)
        return first, stream

    async def stream(
        self, system_static: str, system_dynamic: str, history: list[Any], tools: list[ToolSpec]
    ) -> AsyncIterator[LLMEvent]:
        system = system_static + "\n" + system_dynamic
        model = self.active_model
        try:
            first, stream = await self._open(model, system, history, tools)
        except StopAsyncIteration:
            return
        except errors.APIError as exc:
            # 503/429: capacity. 400 mentioning thought_signature: history came from the fallback model.
            history_mismatch = exc.code == 400 and "thought_signature" in str(exc)
            if (
                model == self.fallback_model
                or not self.fallback_model
                or not (exc.code in RETRYABLE or history_mismatch)
            ):
                raise
            log.warning(
                "%s failed (%s); using fallback %s for the next %ss",
                model,
                exc.code,
                self.fallback_model,
                FALLBACK_COOLDOWN_S,
            )
            self._fallback_until = time.time() + FALLBACK_COOLDOWN_S
            try:
                first, stream = await self._open(self.fallback_model, system, history, tools)
            except StopAsyncIteration:
                return

        async def chunks():
            yield first
            async for chunk in stream:
                yield chunk

        async for ev in consume_chunks(chunks()):
            yield ev


async def consume_chunks(chunks: AsyncIterator[types.GenerateContentResponse]) -> AsyncIterator[LLMEvent]:
    """Turn streamed Gemini chunks into LLMEvents and one assistant Content (kept separate for tests)."""
    parts: list[types.Part] = []
    async for chunk in chunks:
        for part in _parts(chunk):
            if part.function_call is not None:
                parts.append(part)
            elif part.text and not part.thought:
                yield LLMEvent("text", text=part.text)
                _merge_text(parts, part)
            elif part.thought_signature:
                _attach_signature(parts, part.thought_signature)
    if not parts:
        return
    for p in parts:
        if p.function_call is not None:
            if not p.thought_signature:
                p.thought_signature = SKIP_SIGNATURE
            fc = p.function_call
            yield LLMEvent(
                "tool_call",
                call=ToolCall(id=fc.id or uuid.uuid4().hex[:8], name=fc.name or "", args=dict(fc.args or {})),
            )
    yield LLMEvent("assistant", message=types.Content(role="model", parts=parts))


def _parts(chunk: types.GenerateContentResponse) -> list[types.Part]:
    if not chunk.candidates:
        return []
    content = chunk.candidates[0].content
    return list(content.parts) if content and content.parts else []


def _attach_signature(parts: list[types.Part], signature: bytes) -> None:
    """Streaming can deliver a thought signature in its own empty part; it belongs to the preceding part."""
    for prev in reversed(parts):
        if not prev.thought_signature:
            prev.thought_signature = signature
            return
    parts.append(types.Part(thought_signature=signature))


def _merge_text(parts: list[types.Part], part: types.Part) -> None:
    """Coalesce streamed text deltas into one Part, keeping any thought signature."""
    if parts and parts[-1].text is not None and parts[-1].function_call is None:
        parts[-1].text += part.text
        if part.thought_signature:
            parts[-1].thought_signature = part.thought_signature
    else:
        parts.append(types.Part(text=part.text, thought_signature=part.thought_signature))
