"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    llm_provider: str = (
        "auto"  # auto | anthropic | gemini | openai  (auto = first provider whose key is set, in that order)
    )
    anthropic_api_key: str = ""
    anthropic_model: str = (
        "claude-opus-5"  # correct in every test run; claude-haiku-4-5 is faster but fabricated a booking once
    )
    anthropic_effort: str = "low"  # low | medium | high | xhigh | max; low keeps spoken turns snappy
    anthropic_thinking: str = (
        "off"  # off | adaptive  (off measured ~0.9-1.4 s to first token on Opus 5 vs 1.2-1.9 s adaptive)
    )
    openai_api_key: str = ""
    openai_model: str = "gpt-5.5"
    openai_base_url: str = (
        ""  # e.g. https://openrouter.ai/api/v1, https://api.groq.com/openai/v1, http://localhost:11434/v1
    )
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_thinking_level: str = "low"
    gemini_fallback_model: str = "gemini-flash-lite-latest"  # used on 503/429 from the primary model

    # Calendar
    calendar_creds_json: str = ""
    calendar_creds_file: str = "token.json"
    google_calendar_id: str = "primary"
    holiday_calendar_id: str = (
        "auto"  # auto = Google's regional holiday calendar for the timezone; none = off; or a calendar id
    )
    # "Sign in with Google" for visitors: an OAuth client JSON (Desktop client works for localhost,
    # a Web client with the callback URL registered is needed for a public deployment).
    google_oauth_client_json: str = ""
    google_oauth_client_file: str = "credentials.json"
    public_base_url: str = ""  # e.g. https://smart-scheduler.onrender.com; derived from the request if empty

    # Voice
    stt_provider: str = (
        "auto"  # auto | deepgram | browser  (auto = deepgram if its key is set, else the browser's recogniser)
    )
    deepgram_stt_model: str = "nova-3"
    stt_endpointing_ms: int = 300  # silence that ends an utterance; the browser's own recogniser waits ~1 s
    stt_final_grace_ms: int = 500  # extra wait after that, so "I want a meeting ... for Friday" stays one utterance
    tts_enabled: bool = True
    tts_provider: str = "auto"  # auto | deepgram | google | browser  (auto = deepgram if its key is set, else google)
    deepgram_api_key: str = ""
    deepgram_voice: str = "aura-2-thalia-en"
    tts_voice: str = "en-US-Chirp3-HD-Aoede"
    tts_language: str = "en-US"
    tts_sample_rate: int = 24000
    ack_enabled: bool = True  # play a cached "Okay." shortly after the transcript is final
    ack_delay_ms: int = 500  # a beat before the "Okay." so it sounds like a listener, not a machine; skipped if the reply is ready first
    speculation_enabled: bool = True  # start the model on interim transcripts, commit when the final matches

    # Scheduling defaults
    default_timezone: str = "Asia/Kolkata"
    # Time in 24-hour format (9 AM and 6 PM)
    work_day_start: int = 9
    work_day_end: int = 18
    slot_step_minutes: int = 30

    port: int = 8080


@lru_cache
def get_settings() -> Settings:
    return Settings()
