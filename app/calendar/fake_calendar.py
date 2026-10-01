"""In-memory calendar with a realistic seed. Used by the unit tests, and by
scripts/seed_calendar.py to create the same events in a real Google Calendar for the demo.

The seed is relative to "now" so the assignment's test scenarios always have something
to bite on: a fully booked Tuesday afternoon, a 'Project Alpha Kick-off', a Friday 5 PM
meeting, a busy 'tomorrow morning', and recurring 'Weekly Sync' history (usual duration).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .base import BusyPeriod, Event, Holiday

# Real Indian public holidays, so the seeded scenarios (the Friday flight is 2 October) exercise holiday handling.
SEED_HOLIDAYS = [Holiday(date(2026, 10, 2), "Mahatma Gandhi Jayanti"), Holiday(date(2026, 10, 20), "Dussehra")]


class FakeCalendar:
    def __init__(self, events: list[Event] | None = None, holidays: list[Holiday] | None = None):
        self.events: list[Event] = list(events or [])
        self._holidays: list[Holiday] = list(SEED_HOLIDAYS if holidays is None else holidays)

    # --- CalendarClient protocol -------------------------------------------------
    def holidays(self, start: datetime, end: datetime) -> list[Holiday]:
        return [h for h in self._holidays if start.date() <= h.day <= end.date()]

    def busy_periods(self, start: datetime, end: datetime) -> list[BusyPeriod]:
        return [
            BusyPeriod(e.start, e.end, e.title)
            for e in sorted(self.events, key=lambda e: e.start)
            if e.blocks_time and e.end > start and e.start < end
        ]

    def search_events(self, start: datetime, end: datetime, query: str | None = None) -> list[Event]:
        q = (query or "").lower().strip()
        return [
            e
            for e in sorted(self.events, key=lambda e: e.start)
            if e.end > start and e.start < end and (not q or _fuzzy_match(q, e.title + " " + e.description))
        ]

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "") -> Event:
        ev = Event(
            id=uuid.uuid4().hex[:10],
            title=title,
            start=start,
            end=end,
            description=description,
            link=f"https://calendar.google.com/demo/{uuid.uuid4().hex[:6]}",
        )
        self.events.append(ev)
        return ev


def _fuzzy_match(query: str, text: str) -> bool:
    text = text.lower()
    words = [w for w in query.replace("'", "").split() if len(w) > 2]
    return all(w in text for w in words) if words else query in text


def seeded_calendar(now: datetime, tz: ZoneInfo) -> FakeCalendar:
    return FakeCalendar(seed_events(now, tz))


def seed_events(now: datetime, tz: ZoneInfo) -> list[Event]:
    """Demo events relative to `now`, covering every scenario in the assignment."""
    today = now.astimezone(tz).date()

    def at(day, hh, mm=0):
        return datetime.combine(day, time(hh, mm), tzinfo=tz)

    def next_weekday(weekday: int, min_days_ahead: int = 1):
        d = today + timedelta(days=min_days_ahead)
        while d.weekday() != weekday:
            d += timedelta(days=1)
        return d

    tomorrow = today + timedelta(days=1)
    while tomorrow.weekday() >= 5:  # keep "tomorrow morning" scenario on a weekday
        tomorrow += timedelta(days=1)
    tue = next_weekday(1)
    thu = next_weekday(3)
    fri = next_weekday(4)

    events: list[Event] = []
    n = 0

    def add(title, start, end, desc=""):
        nonlocal n
        n += 1
        events.append(Event(id=f"demo-{n}", title=title, start=start, end=end, description=desc))

    # Tomorrow morning: 30-min gaps at 09:30 and 11:00, but no free hour before lunch.
    add("Daily stand-up", at(tomorrow, 9, 0), at(tomorrow, 9, 30))
    add("Design review", at(tomorrow, 10, 0), at(tomorrow, 11, 0))
    add("1:1 with Aman", at(tomorrow, 11, 30), at(tomorrow, 12, 30))
    add("Vendor call", at(tomorrow, 15, 0), at(tomorrow, 16, 0))

    # Tuesday afternoon fully booked (conflict-resolution scenario).
    if tue != tomorrow:
        add("Daily stand-up", at(tue, 9, 0), at(tue, 9, 30))
    add("Quarterly planning", at(tue, 13, 0), at(tue, 15, 30))
    add("Hiring panel", at(tue, 15, 30), at(tue, 17, 0))
    add("Ops retro", at(tue, 17, 0), at(tue, 18, 0))

    # Anchor events for relative-time scenarios.
    add("Project Alpha Kick-off", at(thu, 10, 0), at(thu, 11, 0), "Kick-off with the Alpha team")
    add("Client review", at(fri, 17, 0), at(fri, 18, 0), "Weekly client review")
    add("Flight to Mumbai", at(fri, 18, 0), at(fri, 20, 0), "6 PM flight")

    # Past history so 'our usual sync-up' can be inferred (30 minutes).
    for weeks_ago in (1, 2, 3):
        d = today - timedelta(days=7 * weeks_ago)
        while d.weekday() != 0:
            d -= timedelta(days=1)
        add("Weekly Sync with Aman", at(d, 16, 0), at(d, 16, 30), "Usual sync-up")

    return events
