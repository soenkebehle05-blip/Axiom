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

# Speichermedien für Langzeitgedächtnis und Chat-Verlauf auf dem Server
MEMORY_FILE = Path(__file__).parent / "memory.json"
CHAT_FILE = Path(__file__).parent / "chat_history.json"

# System-Prompt für die KI-Persönlichkeit Axiom
AXIOM_SYSTEM_PROMPT = """
Du bist Axiom, der hochintelligente, treue und zuvorkommende KI-Assistent.
- Sprich den Nutzer ausnahmslos mit "Sir" an.
- Dein Tonfall ist stets höflich, präzise, leicht britisch-distanziert und professionell.
- WICHTIG (Langzeitgedächtnis): Wenn der Nutzer persönliche Vorlieben, Wünsche, Ausrüstungsgegenstände oder Fakten nennt (z. B. "ich liebe Marmelade", "für meinen Triathlon brauche ich X", "merk dir Y"), merkst du dir diese Informationen dauerhaft auf dem Server.
- Wenn der Nutzer dich nach seinen gespeicherten Sachen oder Vorbereitungen fragt (z. B. "Ich habe heute einen Triathlon, frag mich ab / sag mir was ich brauche"), rufst du diese Fakten aus deinem Gedächtnis ab und zählst sie ihm auf.
"""


def load_json_file(file_path: Path) -> dict:
    if file_path.exists():
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json_file(file_path: Path, data: dict) -> None:
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Runtime:
    """Process-wide dependencies built once at startup."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = build_llm(settings)
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
        except Exception as exc:
            log.warning("Server TTS unavailable (%s); falling back to browser speech synthesis", exc)
        self.sessions: dict[str, Agent] = {}
        self.user_preferences: dict[str, dict[str, str]] = {}
        self.user_calendars: dict[str, tuple[GoogleCalendar, dict]] = {}
        self.ack_audio: dict[str, bytes] = {}
        self._background: set[asyncio.Task[None]] = set()
        log.info(
            "calendar=%s tts=%s model=%s",
            self.calendar_source,
            self.tts.name if self.tts else "browser",
            self.llm.model,
        )

    def calendar_for(self, uid: str | None) -> tuple[GoogleCalendar | None, str, dict]:
        if uid and uid in self.user_calendars:
            cal, info = self.user_calendars[uid]
            return cal, "you", info
        return self.calendar, self.calendar_source, self.calendar_info

    def calendar_status(self, uid: str | None = None) -> dict:
        cal, source, info = self.calendar_for(uid)
        return {
            "connected": cal is not None,
            "source": source,
            "calendar": info,
            "oauth_available": self.oauth_client_config() is not None,
        }

    def holiday_calendar(self, tz_name: str) -> str | None:
        setting = self.settings.holiday_calendar_id.strip()
        if setting.lower() in ("", "none", "off"):
            return None
        return holiday_calendar_for(tz_name) if setting.lower() == "auto" else setting

    async def connect_user_calendar(self, uid: str, raw_token_json: str) -> dict:
        calendar = await asyncio.to_thread(GoogleCalendar.from_json, raw_token_json, self.settings.google_calendar_id)
        info = await asyncio.to_thread(calendar.probe)
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
        session.system_prompt = AXIOM_SYSTEM_PROMPT
        
        if uid:
            store = self.user_preferences.setdefault(uid, dict(DEFAULT_PREFERENCES))
            session.preferences = dict(store)
            session.preference_store = store
            
            memories = load_json_file(MEMORY_FILE).get(uid, [])
            if memories:
                session.user_memories = memories

        agent = Agent(self.llm, calendar, session, s.work_day_start, s.work_day_end, s.slot_step_minutes)
        agent.owner = uid
        if len(self.sessions) >= MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)))
        self.sessions[agent.session.id] = agent
        return agent

    async def warm(self) -> None:
        await asyncio.gather(self.llm.warm(), *([self.tts.warm()] if self.tts else []), return_exceptions=True)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def warm_in_background(self) -> None:
        self.spawn(self.warm())

    async def build_ack_audio(self) -> None:
        if self.tts is None or not self.settings.ack_enabled:
            return
        try:
            clips = await asyncio.gather(*(self.tts.synthesize(p) for p in ACK_PHRASES))
            self.ack_audio = dict(zip(ACK_PHRASES, clips, strict=False))
            log.info("cached %d acknowledgement clips", len(self.ack_audio))
        except Exception as exc:
            log.warning("could not pre-synthesise acknowledgements (%s); the browser will speak them", exc)


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
        resp.set_cookie(UID_COOKIE, secrets.token_urlsafe(16), max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


@app.delete("/api/chat/clear")
async def clear_chat(request: Request):
    uid = request.cookies.get(UID_COOKIE)
    rt: Runtime = app.state.rt
    if uid:
        chats = load_json_file(CHAT_FILE)
        chats.pop(uid, None)
        save_json_file(CHAT_FILE, chats)
        rt._drop_sessions_for(uid)
    return {"ok": True, "message": "Der Chatverlauf wurde gelöscht, Sir."}


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
