"""OpenAI-compatible backend (Chat Completions with tools, streaming).

One class covers OpenAI, OpenRouter, Groq, Ollama and any other server that speaks the
Chat Completions protocol: point OPENAI_BASE_URL at it. Note that OpenAI's GPT-6 models
only expose tools through the Responses API, so use gpt-5.5 or earlier with this backend.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from .llm import KEEPALIVE, LLMEvent, ToolCall, ToolSpec

log = logging.getLogger(__name__)


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, model: str = "gpt-5.5", base_url: str = "", temperature: float = 0.3):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            http_client=DefaultAsyncHttpxClient(limits=httpx.Limits(**KEEPALIVE)),
        )
        self.model = model
        self.temperature = temperature

    # --- history helpers ------------------------------------------------------------
    def user_message(self, text: str) -> dict:
        return {"role": "user", "content": text}

    def assistant_message(self, text: str) -> dict:
        return {"role": "assistant", "content": text}

    def tool_results(self, results: list[tuple[ToolCall, dict]]) -> list[dict]:
        # Chat Completions wants one "tool" message per call, so this returns a list.
        return [{"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)} for call, result in results]

    async def warm(self) -> None:
        try:
            await self.client.models.list()
        except Exception as exc:
            log.debug("warm-up failed: %s", exc)

    async def stream(
        self, system_static: str, system_dynamic: str, history: list[Any], tools: list[ToolSpec]
    ) -> AsyncIterator[LLMEvent]:
        messages = [{"role": "system", "content": system_static + "\n" + system_dynamic}, *history]
        tool_params = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
            }
            for t in tools
        ]
        chunks = await self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tool_params, stream=True, temperature=self.temperature
        )
        async for ev in consume_chunks(chunks):
            yield ev


async def consume_chunks(chunks: AsyncIterator[Any]) -> AsyncIterator[LLMEvent]:
    """Turn streamed Chat Completions chunks into LLMEvents plus one assistant message."""
    text = ""
    calls: dict[int, dict] = {}  # index -> {"id", "name", "arguments"}
    async for chunk in chunks:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            text += delta.content
            yield LLMEvent("text", text=delta.content)
        for tc in delta.tool_calls or []:
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            if tc.function is not None:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

    tool_calls = []
    for idx in sorted(calls):
        c = calls[idx]
        c["id"] = c["id"] or f"call_{uuid.uuid4().hex[:8]}"
        try:
            args = json.loads(c["arguments"] or "{}")
        except json.JSONDecodeError:
            log.warning("unparseable tool arguments for %s: %r", c["name"], c["arguments"])
            args = {}
        tool_calls.append(
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
        )
        yield LLMEvent(
            "tool_call", call=ToolCall(id=c["id"], name=c["name"], args=args if isinstance(args, dict) else {})
        )

    if not text and not tool_calls:
        return
    message: dict = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    yield LLMEvent("assistant", message=message)
