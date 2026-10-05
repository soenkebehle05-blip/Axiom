"""FastAPI entrypoint: static UI, text API and the WebSocket voice loop."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.agent import Agent
from app.agent.llm import build_llm
from app.agent.session import DEFAULT_PREFERENCES, Session
from app.calendar.google_calendar import GoogleCalendar, holiday_calendar_for
from app.config import Settings, get_settings
from app.routers import calendar, chat
from app.voice.pipeline import ACK_PHRASES
from app.voice.tts import TTS, build_tts
from app.web import UID_COOKIE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")

STATIC = Path(__file__).parent / "static"
MAX_SESSIONS = 200


class Runtime:
    """Process-wide dependencies built once at startup."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = build_llm(settings)
        # The calendar may be connected later from the UI (POST /api/calendar/token); until then the
        # agent refuses to run rather than falling back to anything fake.
        self.calendar: GoogleCalendar | None = None
        self.calendar_source = "none"
        self.calendar_info: dict = {}
        try:
            self.calendar = GoogleCalendar.from_token(
                settings.calendar_creds_json, settings.calendar_creds_file, settings.google_calendar_id
            )
            if self.calendar is not None:
                self.calendar_source = "env" if settings.calendar_creds_json.strip() else "file"
                self.calendar.holiday_calendar_id = self.holiday_calendar(settings.default_timezone)
        except Exception as exc:
            log.warning("Stored calendar token is unusable (%s); connect one from the UI", exc)
        if self.calendar is None:
            log.warning(
                "No default calendar: visitors must sign in with Google from the UI, or set CALENDAR_CREDS_FILE/JSON"
            )
        self.tts: TTS | None = None
        try:
            self.tts = build_tts(settings)
        except Exception as exc:  # no key / no ADC / API disabled -> browser voice fallback
            log.warning("Server TTS unavailable (%s); falling back to browser speech synthesis", exc)
        self.sessions: dict[str, Agent] = {}
        self.user_preferences: dict[str, dict[str, str]] = {}
        self.user_notes: dict[str, list[dict]] = {}  # browser uid -> open notes / to-dos
        self.user_calendars: dict[str, tuple[GoogleCalendar, dict]] = {}  # browser uid -> connected calendar
        self.ack_audio: dict[str, bytes] = {}
        self._background: set[asyncio.Task[None]] = set()  # strong refs so fire-and-forget tasks are not GC'd
        log.info(
            "calendar=%s tts=%s model=%s",
            self.calendar_source,
            self.tts.name if self.tts else "browser",
            self.llm.model,
        )

    # One default calendar is available via env/file token. Any visitor can additionally connect
    # their own calendar, tied to their browser cookie, which then takes precedence for their sessions.
    def calendar_for(self, uid: str | None) -> tuple[GoogleCalendar | None, str, dict]:
        if uid and uid in self.user_calendars:
            cal, info = self.user_calendars[uid]
            return cal, "you", info
        return self.calendar, self.calendar_source, self.calendar_info

    def calendar_status(self, uid: str | None = None) -> dict:
        cal, source, info = self.calendar_for(uid)
        return {
            "connected": cal is not None,
            "source": source,  # "you" | "env" | "file" | "none"
            "calendar": info,
            "oauth_available": self.oauth_client_config() is not None,
        }

    def holiday_calendar(self, tz_name: str) -> str | None:
        setting = self.settings.holiday_calendar_id.strip()
        if setting.lower() in ("", "none", "off"):
            return None
        return holiday_calendar_for(tz_name) if setting.lower() == "auto" else setting

    async def connect_user_calendar(self, uid: str, raw_token_json: str) -> dict:
        """Validate a token against Google and attach that calendar to this browser."""
        calendar = await asyncio.to_thread(GoogleCalendar.from_json, raw_token_json, self.settings.google_calendar_id)
        info = await asyncio.to_thread(calendar.probe)  # raises if Google rejects the token
        calendar.holiday_calendar_id = self.holiday_calendar(info.get("timezone") or self.settings.default_timezone)
        self.user_calendars[uid] = (calendar, info)
        self._drop_sessions_for(uid)
        log.info("calendar connected for a visitor: %s", info.get("summary") or info.get("id"))
        return self.calendar_status(uid)

    def disconnect_user_calendar(self, uid: str) -> dict:
        self.user_calendars.pop(uid, None)
        self._drop_sessions_for(uid)
        return self.calendar_status(uid)

    def oauth_client_config(self) -> dict | None:
        """{'client_id','client_secret'} from GOOGLE_OAUTH_CLIENT_JSON or the client file; None if absent."""
        raw = self.settings.google_oauth_client_json.strip()
        path = Path(self.settings.google_oauth_client_file)
        if not raw and path.exists():
            raw = path.read_text()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            inner = data.get("web") or data.get("installed") or data
            return {"client_id": inner["client_id"], "client_secret": inner["client_secret"]}
        except (ValueError, KeyError, AttributeError):
            log.warning("GOOGLE_OAUTH_CLIENT_JSON / %s is not a Google OAuth client JSON", path)
            return None

    def _drop_sessions_for(self, uid: str) -> None:
        for sid in [sid for sid, agent in self.sessions.items() if agent.owner == uid]:
            del self.sessions[sid]

    @property
    def stt_provider(self) -> str:
        p = self.settings.stt_provider.lower()
        if p == "auto":
            p = "deepgram" if self.settings.deepgram_api_key else "browser"
        return p if p == "deepgram" and self.settings.deepgram_api_key else "browser"

    def new_agent(self, tz_name: str | None, uid: str | None = None) -> Agent:
        calendar, _, _ = self.calendar_for(uid)
        if calendar is None:
            raise HTTPException(409, "calendar_not_connected")
        try:
            tz = ZoneInfo(tz_name or self.settings.default_timezone)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo(self.settings.default_timezone)
        s = self.settings
        session = Session(tz=tz)
        if uid:
            store = self.user_preferences.setdefault(uid, dict(DEFAULT_PREFERENCES))
            session.preferences = dict(store)
            session.preference_store = store
            session.notes_store = self.user_notes.setdefault(uid, [])
        agent = Agent(self.llm, calendar, session, s.work_day_start, s.work_day_end, s.slot_step_minutes)
        agent.owner = uid
        if len(self.sessions) >= MAX_SESSIONS:  # simple bound for the in-memory store
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[agent.session.id] = agent
        return agent

    async def warm(self) -> None:
        """Open connections to the model and TTS so the next turn skips TCP/TLS setup (best effort)."""
        await asyncio.gather(self.llm.warm(), *([self.tts.warm()] if self.tts else []), return_exceptions=True)

    def spawn(self, coro) -> None:
        """Run a fire-and-forget coroutine."""
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def warm_in_background(self) -> None:
        self.spawn(self.warm())

    # For caching acknowledgement audio clips
    async def build_ack_audio(self) -> None:
        if self.tts is None or not self.settings.ack_enabled:
            return
        try:
            clips = await asyncio.gather(*(self.tts.synthesize(p) for p in ACK_PHRASES))
            self.ack_audio = dict(zip(ACK_PHRASES, clips, strict=False))
            log.info("cached %d acknowledgement clips", len(self.ack_audio))
        except Exception as exc:
            log.warning("could not pre-synthesise acknowledgements (%s); the browser will speak them", exc)


# Warming up the runtime (model and TTS) before handling requests
@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = Runtime(get_settings())
    app.state.rt = rt
    await asyncio.gather(rt.warm(), rt.build_ack_audio())
    yield


app = FastAPI(title="Smart Scheduler AI Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(chat.router)
app.include_router(calendar.router)


@app.get("/")
async def index(request: Request):
    resp = FileResponse(STATIC / "index.html")
    if not request.cookies.get(UID_COOKIE):
        # Lax: still sent on the top-level redirect back from Google's consent page.
        resp.set_cookie(UID_COOKIE, secrets.token_urlsafe(16), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax")
    return resp


@app.get("/health")
async def health():
    rt: Runtime = app.state.rt
    return {
        "ok": True,
        "default_calendar": rt.calendar_source if rt.calendar else "not connected",
        "visitor_calendars": len(rt.user_calendars),
        "tts": rt.tts.name if rt.tts else "browser",
        "stt": rt.stt_provider,
        "model": rt.llm.model,
    }
