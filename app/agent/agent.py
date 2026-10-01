"""The agent loop: user text -> LLM (streaming) -> tool calls -> LLM -> spoken reply.

`run_turn` is an async generator of AgentEvents so the caller can start TTS on the first
sentence while the model is still generating. Provider-agnostic: see llm.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.calendar.base import CalendarClient

from .llm import LLM, ToolCall
from .prompts import SYSTEM_PROMPT, build_calendar_snapshot, build_date_context
from .session import Session
from .tools import SIDE_EFFECT_TOOLS, TOOL_SPECS, ToolRunner

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
SNAPSHOT_TTL_S = 60  # how long the calendar snapshot in the prompt is reused before re-reading the calendar
SNAPSHOT_DAYS = 14


@dataclass
class AgentEvent:
    type: str  # "text" | "tool_call" | "tool_result" | "done"
    data: Any = None


class Agent:
    def __init__(
        self,
        llm: LLM,
        calendar: CalendarClient,
        session: Session,
        work_start: int = 9,
        work_end: int = 18,
        step_minutes: int = 30,
        commit_gate: asyncio.Event | None = None,
    ):
        self.llm = llm
        self.calendar = calendar
        self.session = session
        self.work_start, self.work_end = work_start, work_end
        self.step_minutes = step_minutes
        self.tools = ToolRunner(calendar, session, work_start, work_end, step_minutes, commit_gate)
        self.owner: str | None = None  # browser uid that created this agent (used to drop sessions on calendar swap)
        self._refresh_task: asyncio.Task | None = None
        self._snapshot_lock = asyncio.Lock()

    def fork(self, commit_gate: asyncio.Event) -> Agent:
        """A speculative copy: same LLM/calendar, copied session, side-effect tools held behind the gate."""
        agent = Agent(
            self.llm, self.calendar, self.session.copy(), self.work_start, self.work_end, self.step_minutes, commit_gate
        )
        agent.owner = self.owner
        return agent

    def refresh_snapshot_in_background(self) -> None:
        """Re-read the calendar without blocking the current turn (stale-while-revalidate)."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self.refresh_snapshot(force=True))

    async def current_snapshot(self, now: datetime) -> str:
        """The snapshot for this turn: a cached one at once (refreshing behind the scenes if stale), or a fresh read
        only when there is none yet. Initial turns may wait if prefetch has not finished."""
        sess = self.session
        if sess.snapshot:
            if time.time() - sess.snapshot_at >= SNAPSHOT_TTL_S:
                self.refresh_snapshot_in_background()
            return sess.snapshot
        return await self.refresh_snapshot(now)

    async def refresh_snapshot(self, now: datetime | None = None, force: bool = False) -> str:
        """Read the next two weeks of the calendar into the prompt snapshot (cached for SNAPSHOT_TTL_S)."""
        async with self._snapshot_lock:
            return await self._load_snapshot(now, force)

    async def _load_snapshot(self, now: datetime | None, force: bool) -> str:
        sess = self.session
        if not force and sess.snapshot and time.time() - sess.snapshot_at < SNAPSHOT_TTL_S:
            return sess.snapshot
        now = now or datetime.now(sess.tz)
        try:
            until = now + timedelta(days=SNAPSHOT_DAYS)
            busy = await asyncio.to_thread(self.calendar.busy_periods, now, until)
            try:
                holidays = await asyncio.to_thread(self.calendar.holidays, now, until)
            except Exception as exc:  # holiday calendar is a nicety; never block the snapshot on it
                log.warning("holiday lookup failed: %s", exc)
                holidays = []
            sess.snapshot = build_calendar_snapshot(
                busy, now, sess.tz, self.work_start, self.work_end, SNAPSHOT_DAYS, holidays=holidays
            )
            sess.snapshot_at = time.time()
        except Exception as exc:  # the tools still work without it
            log.warning("calendar snapshot unavailable: %s", exc)
            sess.snapshot = ""
        return sess.snapshot

    async def run_turn(self, user_text: str, now: datetime | None = None) -> AsyncIterator[AgentEvent]:
        now = now or datetime.now(self.session.tz)
        snapshot = await self.current_snapshot(now)
        date_context = build_date_context(
            now, self.session.tz, self.work_start, self.work_end, self.session.preferences
        )
        if snapshot:
            date_context += "\n\n" + snapshot
        self.session.history.append(self.llm.user_message(user_text))

        full_text = ""
        for _round in range(MAX_TOOL_ROUNDS + 1):
            calls: list[ToolCall] = []
            if full_text and not full_text[-1].isspace():
                full_text += " "  # the bridging sentence before a tool call and the answer after it
                yield AgentEvent("text", " ")
            async for ev in self.llm.stream(SYSTEM_PROMPT, date_context, self.session.history, TOOL_SPECS):
                if ev.kind == "text":
                    full_text += ev.text
                    yield AgentEvent("text", ev.text)
                elif ev.kind == "tool_call" and ev.call is not None:
                    calls.append(ev.call)
                elif ev.kind == "assistant":
                    self.session.history.append(ev.message)
            if not calls:
                break
            results: list[tuple[ToolCall, dict]] = []
            for call in calls:
                yield AgentEvent("tool_call", {"name": call.name, "args": call.args})
                result = (
                    {"error": "Tool round limit reached; this call was not executed."}
                    if _round == MAX_TOOL_ROUNDS
                    else await self.tools.dispatch(call.name, call.args, now)
                )
                yield AgentEvent("tool_result", {"name": call.name, "result": result})
                results.append((call, result))
            msgs = self.llm.tool_results(results)
            self.session.history.extend(msgs if isinstance(msgs, list) else [msgs])
            if _round == MAX_TOOL_ROUNDS:
                reply = "I couldn't finish that search. Could you narrow down the day or time?"
                full_text += (" " if full_text and not full_text[-1].isspace() else "") + reply
                yield AgentEvent("text", reply)
                self.session.history.append(self.llm.assistant_message(reply))
                break
            if any(c.name == "create_event" and "created" in r for c, r in results):
                self.refresh_snapshot_in_background()  # so the next turn does not offer the slot just booked

            # Bookings and preference saves need no second model call to phrase: say it from a template.
            templated = _confirmation(results)
            if templated:
                if full_text and not full_text[-1].isspace():
                    full_text += " "
                    yield AgentEvent("text", " ")
                full_text += templated
                yield AgentEvent("text", templated)
                self.session.history.append(self.llm.assistant_message(templated))
                break

        yield AgentEvent("done", full_text.strip())


def _confirmation(results: list[tuple[ToolCall, dict]]) -> str | None:
    """Templated spoken confirmation when a round consisted only of successful side-effect tools."""

    def _spoken_time(dt: datetime) -> str:
        return dt.strftime("%I %p" if dt.minute == 0 else "%I:%M %p").lstrip("0")

    def _ordinal(day: int) -> str:
        return "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

    def _spoken_duration(minutes: int) -> str:
        h, m = divmod(minutes, 60)
        if h == 0:
            return f"{m} minutes"
        hours = "an hour" if h == 1 else f"{h} hours"
        if m == 30:
            return "an hour and a half" if h == 1 else f"{h} and a half hours"
        return hours if m == 0 else f"{hours} and {m} minutes"

    if not results or any(c.name not in SIDE_EFFECT_TOOLS or "error" in r for c, r in results):
        return None
    parts: list[str] = []
    for call, result in results:
        if call.name == "create_event" and "created" in result:
            ev = result["created"]
            start, end = datetime.fromisoformat(ev["start"]), datetime.fromisoformat(ev["end"])
            parts.append(
                f"Done, {ev['title']} is booked for {start:%A} the {start.day}{_ordinal(start.day)} at "
                f"{_spoken_time(start)} for {_spoken_duration(int((end - start).total_seconds() // 60))}."
            )
            if result.get("holiday"):
                parts.append(f"Just so you know, that day is {result['holiday']}, a public holiday.")
            if result.get("overlaps"):
                titles = " and ".join(c["title"] for c in result["overlaps"])
                parts.append(f"It overlaps {titles}, which I left in place.")
        elif call.name == "remember_preference":
            parts.append("Noted, I'll remember that.")
    return " ".join(parts) if parts else None
