"""Tool declarations exposed to Gemini and their handlers.

Handlers are thin: validate/normalise arguments, call the calendar, shape the result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta

from app.calendar.base import CalendarClient
from app.calendar.slots import SlotQuery, find_alternatives, find_free_slots, free_blocks

from .llm import ToolSpec
from .session import Session

log = logging.getLogger(__name__)

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

_ISO = {"type": "string", "description": "ISO 8601 datetime with offset"}

TOOL_SPECS = [
    ToolSpec(
        name="find_available_slots",
        description=(
            "Free meeting slots in a time window: up to max_results slots, the busy events there, free_blocks, "
            "total_available, and alternatives when nothing fits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "window_start": _ISO,
                "window_end": _ISO,
                "duration_minutes": {"type": "integer", "description": "Meeting length in minutes"},
                "earliest_hour": {
                    "type": "number",
                    "description": "Earliest start hour, 24h (9.5 = 09:30)",
                },
                "latest_hour": {"type": "number", "description": "Latest end hour, 24h"},
                "exclude_weekdays": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Weekdays to skip, e.g. ['wed']",
                },
                "include_weekends": {"type": "boolean", "description": "Allow weekends"},
                "buffer_minutes": {
                    "type": "integer",
                    "description": "Free gap required before/after neighbouring events",
                },
                "max_results": {"type": "integer", "description": "Max slots to return (default 3)"},
                "exclude_holidays": {
                    "type": "boolean",
                    "description": "Skip public holidays (default: searched and labelled)",
                },
                "before_event": {
                    "type": "string",
                    "description": "Keyword of an event the meeting must end before (e.g. 'flight'); clips the window",
                },
                "after_event": {
                    "type": "string",
                    "description": "Keyword of an event the meeting must follow",
                },
                "after_event_days": {
                    "type": "integer",
                    "description": "With after_event: days after it to search (default 2)",
                },
            },
            "required": ["window_start", "window_end", "duration_minutes"],
        },
    ),
    ToolSpec(
        name="find_events",
        description=(
            "List calendar events in a time range, optionally filtered by a title keyword. "
            "For anchoring a search use find_available_slots before_event/after_event instead."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "time_min": _ISO,
                "time_max": _ISO,
                "query": {
                    "type": "string",
                    "description": "Keyword to match in the title",
                },
            },
            "required": ["time_min", "time_max"],
        },
    ),
    ToolSpec(
        name="create_event",
        description=(
            "Book a meeting. Refuses an overlap unless override_conflicts=true and a public holiday unless "
            "confirmed_holiday=true (both only after the user agreed)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": _ISO,
                "end": _ISO,
                "description": {"type": "string"},
                "override_conflicts": {
                    "type": "boolean",
                    "description": "Book over an existing event (it is kept); only when the user asked",
                },
                "confirmed_holiday": {
                    "type": "boolean",
                    "description": "User was told it is a holiday and still wants it",
                },
            },
            "required": ["title", "start", "end"],
        },
    ),
    ToolSpec(
        name="remember_preference",
        description="Store a lasting user preference, e.g. usual_meeting_minutes=30 or preferred_earliest_hour=10.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"],
        },
    ),
]


SIDE_EFFECT_TOOLS = {"create_event", "remember_preference"}
TOOL_NAMES = {tool.name for tool in TOOL_SPECS}


class ToolRunner:
    def __init__(
        self,
        calendar: CalendarClient,
        session: Session,
        work_start: int,
        work_end: int,
        step_minutes: int = 30,
        commit_gate: asyncio.Event | None = None,
    ):
        self.calendar = calendar
        self.session = session
        self.work_start = work_start
        self.work_end = work_end
        self.step_minutes = step_minutes
        # A speculative turn (started on an interim transcript) may read the calendar freely but must not
        # book or remember anything until the transcript is confirmed and the turn is committed.
        self.commit_gate = commit_gate

    async def dispatch(self, name: str, args: dict, now: datetime) -> dict:
        if name not in TOOL_NAMES:
            return {"error": f"unknown tool {name}"}
        handler = getattr(self, f"_{name}")
        if name in SIDE_EFFECT_TOOLS and self.commit_gate is not None:
            await self.commit_gate.wait()
        try:
            return await asyncio.to_thread(handler, args, now)
        except Exception as exc:  # surface tool errors to the model instead of crashing the turn
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}

    # --- handlers (sync; run in a worker thread) ---------------------------------
    def _find_available_slots(self, a: dict, now: datetime) -> dict:
        start, end = self._dt(a["window_start"]), self._dt(a["window_end"])
        if end <= start:
            return {"error": "window_end must be after window_start"}
        anchors: dict = {}
        if a.get("before_event"):
            ev = self._find_anchor(a["before_event"], start, end + timedelta(days=14))
            if ev is None:
                return {"error": f"no event matching {a['before_event']!r} found in the window", "slots": []}
            anchors["before_event"] = ev.to_dict()
            end = min(end, ev.start) if ev.start > start else ev.start
            start = min(start, end - timedelta(days=1)) if end <= start else start
        if a.get("after_event"):
            ev = self._find_anchor(a["after_event"], start - timedelta(days=14), end)
            if ev is None:
                return {"error": f"no event matching {a['after_event']!r} found in the window", "slots": []}
            anchors["after_event"] = ev.to_dict()
            # "a day or two after X" means the days following X's day, not the same afternoon.
            days_after = int(a.get("after_event_days") or 2)
            next_day = (ev.end + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            start = max(start, next_day)
            end = min(end, (next_day + timedelta(days=days_after - 1)).replace(hour=23, minute=59))
        if end <= start:
            return {"error": "the anchor event leaves no room in the requested window", "slots": [], **anchors}
        # Default hour bounds are the working day, but an explicit window inside one day (e.g. 19:00-21:00
        # for "in the evening") must not be clipped by them.
        earliest, latest = float(self.work_start), float(self.work_end)
        if (end - start) <= timedelta(hours=24):
            earliest = min(earliest, start.hour + start.minute / 60)
            latest = max(latest, end.hour + end.minute / 60 if (end.hour, end.minute) != (0, 0) else 24.0)
        query = SlotQuery(
            window_start=start,
            window_end=end,
            duration=timedelta(minutes=int(a["duration_minutes"])),
            earliest_hour=float(a.get("earliest_hour", earliest)),
            latest_hour=float(a.get("latest_hour", latest)),
            exclude_weekdays={
                WEEKDAYS[d[:3].lower()] for d in a.get("exclude_weekdays") or [] if d[:3].lower() in WEEKDAYS
            },
            include_weekends=bool(a.get("include_weekends", False)),
            buffer_minutes=int(a.get("buffer_minutes") or 0),
            step_minutes=self.step_minutes,
            max_results=min(int(a.get("max_results", 3)), 200),
        )
        busy = self.calendar.busy_periods(start - timedelta(days=1), end + timedelta(days=7))
        holidays = self.calendar.holidays(start, end + timedelta(days=7))
        if a.get("exclude_holidays"):
            query.exclude_dates = {h.day for h in holidays}
        slots = find_free_slots(busy, query, now)
        busy_in_window = sorted([b for b in busy if b.end > start and b.start < end], key=lambda b: b.start)
        total_available = len(find_free_slots(busy, replace(query, max_results=200, max_per_day=200), now))
        blocks = free_blocks(busy, query, now)
        result: dict = {
            "slots": [s.to_dict() for s in slots],
            "total_available": total_available,
            "window_is_free": not busy_in_window,
            "free_blocks": blocks[:10],
            "busy_in_window": [
                {"title": b.title, "start": b.start.isoformat(), "end": b.end.isoformat()} for b in busy_in_window
            ][:12],
        }
        if not busy_in_window and slots:
            result["presentation"] = (
                "The whole window is free: say so in the user's words ('Monday afternoon is free') and ask what time "
                "suits, instead of listing times."
            )
        elif total_available > 3:
            result["presentation"] = (
                f"{total_available} slots fit; the free blocks are listed. Do not pick three at random: describe the open "
                "stretches and ask ONE narrowing question (earlier or later, before or after lunch, which day)."
            )
        holiday_names = {h.day: h.name for h in holidays}
        for slot, dict_ in zip(slots, result["slots"], strict=True):
            if slot.start.date() in holiday_names:
                dict_["holiday"] = holiday_names[slot.start.date()]  # say the name when offering this slot
        in_window = [h for h in holidays if start.date() <= h.day <= end.date()]
        if in_window:
            result["holidays_in_window"] = [h.to_dict() for h in in_window]
        if anchors:
            result.update(anchors)
        if not slots:
            result["alternatives"] = find_alternatives(busy, query, now)
            result["note"] = "No slot satisfies all constraints. Offer the closest alternative and ask the user."
            if a.get("exclude_holidays") and in_window:
                result["note"] += " Public holidays were excluded at your request: " + ", ".join(
                    f"{h.day.isoformat()} ({h.name})" for h in in_window
                )
        elif any("holiday" in d for d in result["slots"]):
            result["note"] = (
                "Some slots fall on a public holiday (see each slot's `holiday`); mention the holiday by name when offering them."
            )
        self.session.last_offered_slots = result["slots"]
        return result

    def _find_anchor(self, query: str, start: datetime, end: datetime):
        events = self.calendar.search_events(start, end, query)
        return events[0] if events else None

    def _find_events(self, a: dict, now: datetime) -> dict:
        events = self.calendar.search_events(self._dt(a["time_min"]), self._dt(a["time_max"]), a.get("query") or None)
        return {"events": [e.to_dict() for e in events[:20]], "count": len(events)}

    def _create_event(self, a: dict, now: datetime) -> dict:
        start, end = self._dt(a["start"]), self._dt(a["end"])
        if end <= start:
            return {"error": "end must be after start"}
        if start < now:
            return {"error": "cannot book a meeting in the past"}
        clash = [b for b in self.calendar.busy_periods(start, end) if b.end > start and b.start < end]
        conflicts = [{"title": b.title, "start": b.start.isoformat(), "end": b.end.isoformat()} for b in clash]
        if clash and not a.get("override_conflicts"):
            return {
                "error": "that time conflicts with an existing event",
                "conflicts": conflicts,
                "note": (
                    "Tell the user what is there and offer other times. Suggest booking over the existing event only if "
                    "there are no alternatives; if the user asks to book over it anyway, call again with override_conflicts=true."
                ),
            }
        holiday = next((h for h in self.calendar.holidays(start, end) if h.day == start.date()), None)
        if holiday and not a.get("confirmed_holiday"):
            return {
                "error": f"{start:%A} {start.date().isoformat()} is {holiday.name}, a public holiday",
                "holiday": holiday.name,
                "note": "Tell the user and ask; if they still want it, call again with confirmed_holiday=true.",
            }
        ev = self.calendar.create_event(a["title"], start, end, a.get("description", ""))
        self.session.booked.append(ev.to_dict())
        self.session.snapshot_at = 0.0  # the snapshot in the prompt is now stale
        result = {"created": ev.to_dict()}
        if holiday:
            result["holiday"] = holiday.name  # relayed to the user in the confirmation
        if clash:
            result["overlaps"] = conflicts  # booked over these at the user's request; they were left in place
        return result

    def _remember_preference(self, a: dict, now: datetime) -> dict:
        self.session.remember(str(a["key"]), str(a["value"]))
        return {"saved": {a["key"]: a["value"]}}

    def _dt(self, value: str) -> datetime:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.session.tz)
        return dt.astimezone(self.session.tz)
