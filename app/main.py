"""FastAPI entrypoint: static UI, text API, SQLite chat history and the WebSocket voice loop."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
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
DB_PATH = Path(__file__).parent.parent / "axiom_chat.db"
MAX_SESSIONS = 200

# =========================================================
# DATENBANK-MANAGER (SQLite für 7-Tage Chat-Historie)
# =========================================================
class ChatDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_message(self, uid: str, role: str, text: str):
        if not uid or not text.strip():
            return
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO chat_history (uid, role, text, timestamp) VALUES (?, ?, ?, ?)",
                (uid, role, text.strip(), datetime.utcnow().isoformat())
            )
            conn.commit()
        self.cleanup_old_messages()

    def get_history(self, uid: str) -> list[dict]:
        self.cleanup_old_messages()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT role, text FROM chat_history WHERE uid = ? ORDER BY id ASC",
                (uid,)
            )
            rows = cursor.fetchall()
            return [{"role": r[0], "text": r[1]} for r in rows]

    def cleanup_old_messages(self):
        """Löscht alle Nachrichten, die älter als 7 Tage sind."""
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM chat_history WHERE timestamp < ?", (cutoff,))
            conn.commit()

db = ChatDatabase(DB_PATH)


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
            log.warning("No default calendar configured.")
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
        
        # System-Prompt für die Jarvis-Persönlichkeit & Deutsch erzwingen
        session.system_prompt = (
            "Du bist AXIOM, eine hochmoderne, zuvorkommende KI im Stile von JARVIS aus Iron Man. "
            "Antworte IMMER auf Deutsch. Sprich den Benutzer IMMER höflich mit 'Sir' an. "
            "Halte dich präzise, professionell und auf den Punkt."
        )

        if uid:
            store = self.user_preferences.setdefault(uid, dict(DEFAULT_PREFERENCES))
            session.preferences = dict(store)
            session.preference_store = store
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
        except Exception as exc:
            log.warning("could not pre-synthesise acknowledgements (%s)", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = Runtime(get_settings())
    app.state.rt = rt
    await asyncio.gather(rt.warm(), rt.build_ack_audio())
    yield


app = FastAPI(title="AXIOM AI Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(chat.router)
app.include_router(calendar.router)

# --- Endpunkte für Historie und Nachrichten-Speicherung ---
@app.get("/api/history")
async def get_chat_history(request: Request):
    uid = request.cookies.get(UID_COOKIE)
    if not uid:
        return JSONResponse([])
    return JSONResponse(db.get_history(uid))

@app.post("/api/history/save")
async def save_chat_message(request: Request):
    uid = request.cookies.get(UID_COOKIE)
    if not uid:
        return JSONResponse({"status": "ignored"})
    data = await request.json()
    role = data.get("role")
    text = data.get("text")
    if role and text:
        db.save_message(uid, role, text)
    return JSONResponse({"status": "ok"})


@app.get("/")
async def index(request: Request):
    resp = FileResponse(STATIC / "index.html")
    if not request.cookies.get(UID_COOKIE):
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
