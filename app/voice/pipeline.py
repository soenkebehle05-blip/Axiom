"""Per-connection voice pipeline: acknowledgement, speculative turns, streaming text + TTS.

Latency tricks used by production voice agents, applied here:
- ack:          a cached "Okay." is sent after a short delay unless the reply is ready first.
- speculation:  the model starts on the interim transcript; output is buffered and released ("committed")
                when the final transcript matches, otherwise the run is cancelled. Side-effecting tools
                (booking, remembering) wait behind the commit gate, so a cancelled guess never books anything.
- streaming:    text deltas go out as they arrive, sentences go to TTS as soon as they are complete, and the
                bridging sentence before a tool call is flushed to TTS immediately.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.agent import Agent
from app.agent.tools import SIDE_EFFECT_TOOLS

from .chunker import SentenceChunker
from .tts import TTS, queue_iter

log = logging.getLogger(__name__)

ACK_PHRASES = ["Okay.", "Sure.", "Mm-hm."]
MIN_SPECULATION_WORDS = 2
IDLE_FLUSH_S = 0.4  # tokens paused this long -> speak the clause we have rather than wait for the sentence
HOLD_FIRST_S = 0.3  # how long a bridge-like first sentence waits to learn whether a booking follows
BRIDGE_MAX_CHARS = 48  # longer first sentences are real content and are spoken at once

Sender = Callable[[Any], Awaitable[None]]  # sends a dict (JSON) or bytes (audio) to the client


def describe_error(exc: Exception) -> dict:
    """Turn a provider exception into something a user can act on (free-tier limits are the common case)."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    if "429" in lowered or "resource_exhausted" in lowered or "quota" in lowered or "rate limit" in lowered:
        return {
            "code": "rate_limit",
            "message": "The model's rate limit was reached. Wait a minute and try again; if it persists, check the provider's quota and billing.",
            "detail": text[:300],
        }
    if "503" in lowered or "unavailable" in lowered or "high demand" in lowered or "overloaded" in lowered:
        return {
            "code": "unavailable",
            "message": "The model is temporarily unavailable (capacity). Try again in a moment.",
            "detail": text[:300],
        }
    return {"code": "error", "message": text[:300]}


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", "", text.casefold()).split())


class Turn:
    """One agent turn. Speculative turns buffer their output until committed."""

    def __init__(self, pipeline: VoicePipeline, agent: Agent, text: str, speculative: bool, briefing: str | None = None):
        self.pipeline = pipeline
        self.agent = agent
        self.text = text
        self.speculative = speculative
        self.briefing = briefing
        self.committed = not speculative
        self.buffer: list[Any] = []
        self.gate = asyncio.Event()
        if not speculative:
            self.gate.set()
        self.task = asyncio.create_task(self._run())

    @property
    def done(self) -> bool:
        return self.task.done()

    def cancel(self) -> None:
        self.task.cancel()

    async def commit(self) -> None:
        """Release buffered output in order, then let further output flow live."""
        async with self.pipeline.lock:
            self.committed = True
            buffered, self.buffer = self.buffer, []
            for msg in buffered:
                await self.pipeline.raw_send(msg)
        self.gate.set()

    async def _emit(self, msg: Any) -> None:
        if self.committed:
            await self.pipeline.send(msg)
        else:
            self.buffer.append(msg)

    async def _run(self) -> None:
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        chunker = SentenceChunker()
        tts = self.pipeline.tts

        async def speak() -> None:
            if tts is None:
                return
            try:
                first = True
                async for audio in tts.synthesize_stream(queue_iter(sentences)):
                    if first:
                        self.pipeline.cancel_pending_ack()  # the real reply is ready; no "Okay." needed
                        await self._emit({"type": "reply_audio_start"})
                        first = False
                    await self._emit(audio)
            except Exception as exc:
                log.warning("TTS stream failed: %s", exc)
                await self._emit({"type": "tts_error", "message": str(exc)})

        speaker = asyncio.create_task(speak())
        t0 = time.perf_counter()
        booked_before = len(self.agent.session.booked)
        # The agent generator runs in its own task and feeds a queue, so the consumer below can wait with a
        # timeout (an idle flush) without cancelling the generator mid-step.
        events: asyncio.Queue[Any] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for ev in self.agent.run_turn(self.text, briefing=self.briefing):
                    events.put_nowait(ev)
            except Exception as exc:  # surfaced to the client by the consumer
                events.put_nowait(exc)
            finally:
                events.put_nowait(None)

        producer = asyncio.create_task(produce())
        # The first sentence of a model round is usually a bridge ("Let me check your calendar."). It is held
        # briefly: dropped if a booking/preference call follows (their confirmation is templated and immediate),
        # spoken as soon as a read-only tool call, more text, or HOLD_FIRST_S arrives.
        held: str | None = None
        held_at = 0.0
        first_of_round = True

        def release_held() -> None:
            nonlocal held
            if held is not None:
                sentences.put_nowait(held)
                held = None

        def queue(sentence: str) -> None:
            nonlocal held, held_at, first_of_round
            if first_of_round and held is None and len(sentence) <= BRIDGE_MAX_CHARS:
                held, held_at, first_of_round = sentence, time.perf_counter(), False  # could be a bridge: wait a beat
                return
            release_held()
            first_of_round = False
            sentences.put_nowait(sentence)

        try:
            while True:
                try:
                    ev = await asyncio.wait_for(events.get(), IDLE_FLUSH_S)
                except TimeoutError:
                    if held is not None and time.perf_counter() - held_at >= HOLD_FIRST_S:
                        release_held()
                    for s in chunker.idle_flush():
                        queue(s)
                    continue
                if ev is None:
                    break
                if isinstance(ev, Exception):
                    raise ev
                if ev.type == "text":
                    await self._emit({"type": "token", "text": ev.data})
                    for s in chunker.feed(ev.data):
                        queue(s)
                    if held is not None and time.perf_counter() - held_at >= HOLD_FIRST_S:
                        release_held()
                elif ev.type == "tool_call":
                    if ev.data["name"] in SIDE_EFFECT_TOOLS:
                        held = None  # the bridge would only collide with the templated confirmation
                        chunker.flush()
                    else:
                        release_held()
                        for s in chunker.flush():  # say "Let me check..." while the tool runs
                            sentences.put_nowait(s)
                    await self._emit({"type": "tool_call", **ev.data})
                elif ev.type == "tool_result":
                    first_of_round = True  # the next model round starts fresh
                    await self._emit({"type": "tool_result", **ev.data})
                elif ev.type == "done":
                    release_held()
                    for s in chunker.flush():
                        sentences.put_nowait(s)
                    if tts is None:
                        self.pipeline.cancel_pending_ack()  # the browser speaks the reply from here
                    await self._emit(
                        {
                            "type": "turn_end",
                            "text": ev.data,
                            "llm_ms": int((time.perf_counter() - t0) * 1000),
                            "speculative": self.speculative,
                            # After a booking the conversation has reached its goal: do not reopen the mic.
                            "listen_after": len(self.agent.session.booked) == booked_before,
                        }
                    )
            sentences.put_nowait(None)
            await speaker
            await self._emit({"type": "audio_end"})
        except Exception as exc:
            log.exception("turn failed")
            self.pipeline.cancel_pending_ack()
            await self._emit({"type": "error", **describe_error(exc)})
        finally:
            # Also drain children if cancellation happens during TTS playback or a WebSocket send fails.
            producer.cancel()
            speaker.cancel()
            await asyncio.gather(producer, speaker, return_exceptions=True)


class VoicePipeline:
    def __init__(
        self,
        raw_send: Sender,
        agent: Agent,
        tts: TTS | None,
        ack_audio: dict[str, bytes],
        ack_enabled: bool = True,
        speculation_enabled: bool = True,
        ack_delay_s: float = 0.5,
    ):
        self.raw_send = raw_send
        self.agent = agent
        self.tts = tts
        self.ack_audio = ack_audio
        self.ack_enabled = ack_enabled
        self.ack_delay_s = ack_delay_s
        self.speculation_enabled = speculation_enabled
        self.lock = asyncio.Lock()
        self.current: Turn | None = None
        self._pending_ack: asyncio.Task | None = None
        # Speculation instrumentation: how often the guess is used, discarded, or replaced by a newer partial.
        self.stats = {"started": 0, "hit": 0, "miss": 0, "cancelled": 0, "ack_skipped": 0}

    async def send(self, msg: Any) -> None:
        async with self.lock:
            await self.raw_send(msg)

    async def on_partial(self, text: str) -> None:
        """Interim transcript: start (or keep) a speculative turn for it."""
        if not self.speculation_enabled or len(text.split()) < MIN_SPECULATION_WORDS:
            return
        if self.current and self.current.speculative and normalise(self.current.text) == normalise(text):
            return
        if self.current and self.current.speculative and not self.current.committed:
            self.stats["cancelled"] += 1  # a newer partial replaced a guess still in flight
        self._cancel_current()
        self.stats["started"] += 1
        self.current = Turn(self, self.agent.fork(asyncio.Event()), text, speculative=True)
        self.current.agent.tools.commit_gate = self.current.gate

    async def on_final(self, text: str, briefing: str | None = None) -> None:
        """Final transcript: commit a matching guess or schedule an acknowledgement and start a live turn."""
        cur = self.current
        if not briefing and cur and cur.speculative and not cur.committed and normalise(cur.text) == normalise(text):
            self.stats["hit"] += 1
            if cur.buffer:
                self.stats["ack_skipped"] += 1  # the real reply is already waiting; an "Okay." would only delay it
            else:
                await self._ack()
            self.agent = cur.agent  # the speculative session becomes the real one
            await cur.commit()
            await self._send_stats()
            return
        if cur and cur.speculative and not cur.committed:
            self.stats["miss"] += 1
        self._cancel_current()  # before scheduling the ack: cancelling the old turn also clears pending acks
        await self._ack()
        self.current = Turn(self, self.agent, text, speculative=False, briefing=briefing)
        if self.speculation_enabled:
            await self._send_stats()

    async def _send_stats(self) -> None:
        await self.send({"type": "speculation", **self.stats})

    def on_cancel(self) -> None:
        self._cancel_current()

    def close(self) -> None:
        self._cancel_current()
        if self.stats["started"]:
            log.info("speculation stats for this connection: %s", self.stats)

    async def aclose(self) -> None:
        """Cancel and drain the active turn before replacing or closing a connection."""
        tasks = [task for task in (self.current.task if self.current else None, self._pending_ack) if task]
        self.close()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _cancel_current(self) -> None:
        self.cancel_pending_ack()
        if self.current and not self.current.done:
            self.current.cancel()
        self.current = None

    def cancel_pending_ack(self) -> None:
        if self._pending_ack and not self._pending_ack.done():
            self._pending_ack.cancel()
        self._pending_ack = None

    async def _ack(self) -> None:
        """Play the acknowledgement after a short beat, unless the reply's audio is ready sooner."""
        if not self.ack_enabled:
            return
        self.cancel_pending_ack()
        if self.ack_delay_s <= 0:
            await self._send_ack()
            return
        self._pending_ack = asyncio.create_task(self._delayed_ack())

    async def _delayed_ack(self) -> None:
        await asyncio.sleep(self.ack_delay_s)
        await self._send_ack()

    async def _send_ack(self) -> None:
        phrase = secrets.choice(ACK_PHRASES)
        # Keep the role marker and its PCM together; reply audio can arrive concurrently.
        async with self.lock:
            await self.raw_send({"type": "ack", "text": phrase, "audio": phrase in self.ack_audio})
            if phrase in self.ack_audio:
                await self.raw_send(self.ack_audio[phrase])
