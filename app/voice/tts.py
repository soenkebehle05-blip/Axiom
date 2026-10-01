"""Text-to-speech providers behind one small interface.

All providers yield raw 16-bit little-endian PCM at `sample_rate`, which the browser plays straight
through Web Audio. `synthesize_stream` takes the sentences as they come out of the chunker and yields
audio chunks as soon as the service produces them; `synthesize` is the one-shot used for the cached
acknowledgement clips.

- GoogleTTS:   Cloud Text-to-Speech, Chirp 3 HD, one bidirectional gRPC stream per turn (needs a billed
               Google Cloud project + Application Default Credentials).
- DeepgramTTS: Aura-2 over HTTPS, one streamed request per sentence (free $200 signup credit, no card).
Anything that fails to construct falls back to the browser's own voice (see main.Runtime).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

CACHE_SENTENCES = 200
CACHE_BYTES = 10 * 1024 * 1024  # cap actual audio size, not an assumed sentence duration
KEEPALIVE = httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=600.0)


class TTS(Protocol):
    name: str
    sample_rate: int

    async def warm(self) -> None: ...

    async def synthesize(self, text: str) -> bytes: ...

    def synthesize_stream(self, sentences: AsyncIterator[str]) -> AsyncIterator[bytes]: ...


class GoogleTTS:
    name = "google"

    def __init__(self, voice: str, language: str, sample_rate: int = 24000, speaking_rate: float = 1.0):
        from google.cloud import texttospeech as tts  # imported lazily: only needed for this provider

        self._tts = tts
        self.client = tts.TextToSpeechAsyncClient()
        self.config = tts.StreamingSynthesizeConfig(
            voice=tts.VoiceSelectionParams(name=voice, language_code=language),
            streaming_audio_config=tts.StreamingAudioConfig(
                audio_encoding=tts.AudioEncoding.PCM, sample_rate_hertz=sample_rate, speaking_rate=speaking_rate
            ),
        )
        self.sample_rate = sample_rate

    async def warm(self) -> None:
        try:
            await self.client.list_voices(language_code=self.config.voice.language_code)  # opens the gRPC channel
        except Exception as exc:
            log.debug("TTS warm-up failed: %s", exc)

    async def synthesize(self, text: str) -> bytes:
        tts, voice = self._tts, self.config.voice
        try:
            resp = await self.client.synthesize_speech(
                input=tts.SynthesisInput(text=text),
                voice=voice,
                audio_config=tts.AudioConfig(audio_encoding=tts.AudioEncoding.PCM, sample_rate_hertz=self.sample_rate),
            )
            return bytes(resp.audio_content)
        except Exception:  # older API surface: LINEAR16 is a WAV container, strip the 44-byte header
            resp = await self.client.synthesize_speech(
                input=tts.SynthesisInput(text=text),
                voice=voice,
                audio_config=tts.AudioConfig(
                    audio_encoding=tts.AudioEncoding.LINEAR16, sample_rate_hertz=self.sample_rate
                ),
            )
            return bytes(resp.audio_content)[44:]

    async def synthesize_stream(self, sentences: AsyncIterator[str]) -> AsyncIterator[bytes]:
        tts = self._tts

        async def requests():
            yield tts.StreamingSynthesizeRequest(streaming_config=self.config)
            async for text in sentences:
                if text.strip():
                    yield tts.StreamingSynthesizeRequest(input=tts.StreamingSynthesisInput(text=text))

        responses = await self.client.streaming_synthesize(requests())
        async for resp in responses:
            if resp.audio_content:
                yield bytes(resp.audio_content)


class DeepgramTTS:
    """Aura-2 via `POST /v1/speak`, streamed. One request per sentence over a kept-alive connection."""

    name = "deepgram"

    def __init__(self, api_key: str, voice: str = "aura-2-thalia-en", sample_rate: int = 24000, base_url: str = ""):
        self.client = httpx.AsyncClient(
            base_url=base_url or "https://api.deepgram.com",
            headers={"Authorization": f"Token {api_key}"},
            limits=KEEPALIVE,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self.voice = voice
        self.sample_rate = sample_rate
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0

    @property
    def _params(self) -> dict:
        # container=none -> raw PCM frames rather than a WAV file, so chunks can be played as they arrive.
        return {"model": self.voice, "encoding": "linear16", "sample_rate": self.sample_rate, "container": "none"}

    async def warm(self) -> None:
        try:
            await self.client.get("/v1/projects")  # cheap authenticated call: opens and keeps the TLS connection
        except Exception as exc:
            log.debug("Deepgram warm-up failed: %s", exc)

    async def synthesize(self, text: str) -> bytes:
        return b"".join([chunk async for chunk in self._speak(text)])

    async def synthesize_stream(self, sentences: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for text in sentences:
            if text.strip():
                async for chunk in self._speak_cached(text):
                    yield chunk

    async def _speak_cached(self, text: str) -> AsyncIterator[bytes]:
        """Repeated sentences (bridges, standard questions) come straight from memory the second time."""
        key = text.strip()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            yield cached
            return
        parts: list[bytes] = []
        size = 0
        async for chunk in self._speak(text):
            size += len(chunk)
            if size <= CACHE_BYTES:
                parts.append(chunk)
            else:
                parts.clear()
            yield chunk
        if not size or size > CACHE_BYTES:
            return
        previous = self._cache.pop(key, b"")
        self._cache[key] = b"".join(parts)
        self._cache_bytes += size - len(previous)
        while len(self._cache) > CACHE_SENTENCES or self._cache_bytes > CACHE_BYTES:
            _, audio = self._cache.popitem(last=False)
            self._cache_bytes -= len(audio)

    async def _speak(self, text: str) -> AsyncIterator[bytes]:
        carry = b""  # HTTP chunk boundaries can split a 16-bit sample; never emit an odd number of bytes
        async with self.client.stream("POST", "/v1/speak", params=self._params, json={"text": text}) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"Deepgram TTS {resp.status_code}: {body}")
            async for chunk in resp.aiter_bytes():
                data = carry + chunk
                if len(data) % 2:
                    data, carry = data[:-1], data[-1:]
                else:
                    carry = b""
                if data:
                    yield data
        if carry:
            log.debug("dropping a trailing odd byte from Deepgram audio")


def build_tts(settings) -> TTS | None:
    """Pick the provider: TTS_PROVIDER=auto|deepgram|google|browser. `auto` prefers Deepgram when its key is set."""
    if not settings.tts_enabled:
        return None
    provider = settings.tts_provider.lower()
    if provider == "auto":
        provider = "deepgram" if settings.deepgram_api_key else "google"
    if provider in ("browser", "none", "off"):
        return None
    if provider == "deepgram":
        if not settings.deepgram_api_key:
            raise RuntimeError("TTS_PROVIDER=deepgram but DEEPGRAM_API_KEY is not set")
        return DeepgramTTS(settings.deepgram_api_key, settings.deepgram_voice, settings.tts_sample_rate)
    if provider == "google":
        return GoogleTTS(settings.tts_voice, settings.tts_language, settings.tts_sample_rate)
    raise RuntimeError(f"unknown TTS_PROVIDER {provider!r}")


async def queue_iter(q: asyncio.Queue[str | None]) -> AsyncIterator[str]:
    """Drain an asyncio.Queue until a None sentinel; used to bridge the agent stream to TTS."""
    while True:
        item = await q.get()
        if item is None:
            return
        yield item
