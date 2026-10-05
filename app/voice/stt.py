"""Server-side speech-to-text: one Deepgram Nova-3 streaming session per listening window.

The browser streams raw 16-bit PCM while the user is listening; the server forwards it to Deepgram
and turns the transcript messages into two kinds of events:
  ("partial", text)                     interim words, used for display and speculative generation
  ("final", text, speech_end_ago_ms)    the utterance is over (endpointing / utterance end); run the turn
The session is opened on listen_start and closed on listen_stop, so silence between turns is not
billed and the assistant's own voice is never transcribed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)

Event = tuple  # ("partial", text) | ("final", text, speech_end_ago_ms) | ("speech_started",)
Listener = Callable[[Event], Awaitable[None]]


class TranscriptAggregator:
    """Turns Deepgram's message stream into partial/final events (pure logic, unit-tested)."""

    def __init__(self, stream_started_at: float | None = None):
        self.stream_started_at = stream_started_at or time.time()
        self.finals: list[str] = []  # is_final segments since the last utterance boundary
        self.last_audio_end = 0.0  # seconds of audio at the end of the latest final segment

    def handle(self, message: dict, now: float | None = None) -> list[Event]:
        now = now or time.time()
        kind = message.get("type")
        if kind == "SpeechStarted":
            return [("speech_started",)]
        if kind == "UtteranceEnd":
            return self._flush(now) if self.finals else []
        if kind != "Results":
            return []
        alt = ((message.get("channel") or {}).get("alternatives") or [{}])[0]
        text = (alt.get("transcript") or "").strip()
        if message.get("is_final"):
            if text:
                self.finals.append(text)
                self.last_audio_end = float(message.get("start", 0.0)) + float(message.get("duration", 0.0))
            if message.get("speech_final") and self.finals:
                return self._flush(now)
            return []
        if not text:
            return []
        return [("partial", " ".join([*self.finals, text]))]

    def _flush(self, now: float) -> list[Event]:
        text, self.finals = " ".join(self.finals), []
        # Wall-clock moment the speech ended, assuming audio was streamed in real time.
        speech_end_wall = self.stream_started_at + self.last_audio_end
        ago_ms = max(0, int((now - speech_end_wall) * 1000))
        return [("final", text, ago_ms)]


class DeepgramSTT:
    """A live transcription session over Deepgram's WebSocket API."""

    def __init__(
        self,
        api_key: str,
        on_event: Listener,
        model: str = "nova-3",
        sample_rate: int = 24000,
        endpointing_ms: int = 300,
        language: str = "en",
        final_grace_ms: int = 500,
    ):
        self.api_key = api_key
        self.on_event = on_event
        # After Deepgram declares an utterance final, wait this long for the speaker to continue ("...for Friday")
        # before running the turn; anything that arrives in the meantime is merged into the same utterance.
        self.final_grace_s = final_grace_ms / 1000
        self._pending_final: tuple[str, int] | None = None
        self._grace_task: asyncio.Task | None = None
        self.params = {
            "model": model,
            "language": language,
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "channels": 1,
            "interim_results": "true",
            "smart_format": "true",
            "vad_events": "true",
            "endpointing": endpointing_ms,
            "utterance_end_ms": 1000,
        }
        self.ws = None
        self.receiver: asyncio.Task | None = None
        self.aggregator = TranscriptAggregator()
        self._audio_started = False

    @property
    def url(self) -> str:
        query = "&".join(f"{k}={v}" for k, v in self.params.items())
        return f"wss://api.deepgram.com/v1/listen?{query}"

    async def start(self) -> None:
        self.ws = await websockets.connect(
            self.url, additional_headers={"Authorization": f"Token {self.api_key}"}, open_timeout=10
        )
        self.aggregator = TranscriptAggregator(time.time())
        self._audio_started = False
        self.receiver = asyncio.create_task(self._receive())

    async def send_audio(self, pcm: bytes) -> None:
        if self.ws is not None:
            if not self._audio_started:  # the audio clock starts with the first frame, not at connect time
                self._audio_started = True
                self.aggregator.stream_started_at = time.time()
            await self.ws.send(pcm)

    async def stop(self) -> None:
        ws, self.ws = self.ws, None
        if ws is None:
            return
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))  # Deepgram flushes the last results, then closes
            if self.receiver is not None:
                await asyncio.wait_for(self.receiver, timeout=3)
        except Exception as exc:
            log.debug("STT stop: %s", exc)
            if self.receiver is not None:
                self.receiver.cancel()
        finally:
            await ws.close()
            await self._flush_pending()  # the mic is closing: whatever was said is the utterance

    async def _receive(self) -> None:
        if self.ws is None:
            raise RuntimeError("WebSocket connection is not established.")
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    continue
                await self.handle_message(json.loads(raw))
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:
            log.warning("STT session error: %s", exc)
            await self.on_event(("error", str(exc)))

    async def handle_message(self, message: dict) -> None:
        """Route one Deepgram message, holding finals for the grace period so pauses don't split sentences."""
        has_words = False
        if message.get("type") == "Results":  # UtteranceEnd carries `channel` as a list, so check the type first
            alt = ((message.get("channel") or {}).get("alternatives") or [{}])[0]
            has_words = bool((alt.get("transcript") or "").strip())
        if self._pending_final and (has_words or message.get("type") == "SpeechStarted"):
            # The speaker continued: pull the held text back in front of what follows.
            self._cancel_grace()
            held, _ = self._pending_final
            self._pending_final = None
            self.aggregator.finals.insert(0, held)
        # Restore held text before aggregation, so a new final flushes it exactly once.
        for event in self.aggregator.handle(message):
            if event[0] == "final":
                self._pending_final = (event[1], event[2])
                self._cancel_grace()
                self._grace_task = asyncio.create_task(self._emit_after_grace())
            else:
                await self.on_event(event)

    async def _emit_after_grace(self) -> None:
        await asyncio.sleep(self.final_grace_s)
        await self._flush_pending(extra_ms=int(self.final_grace_s * 1000))

    async def _flush_pending(self, extra_ms: int = 0) -> None:
        pending, self._pending_final = self._pending_final, None
        self._cancel_grace(current=True)
        if pending:
            text, ago = pending
            await self.on_event(("final", text, ago + extra_ms))

    def _cancel_grace(self, current: bool = False) -> None:
        task, self._grace_task = self._grace_task, None
        if task and not task.done() and not (current and task is asyncio.current_task()):
            task.cancel()
