"""Pure slot-finding logic. No I/O, fully unit-testable.

Given busy periods and a `SlotQuery`, finds free slots inside working hours and,
when nothing fits, proposes alternatives so the agent can resolve conflicts gracefully.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from .base import BusyPeriod


@dataclass
class SlotQuery:
    window_start: datetime
    window_end: datetime
    duration: timedelta
    earliest_hour: float = 9.0  # slot may not start before this hour (e.g. 9.5 = 09:30)
    latest_hour: float = 18.0  # slot must end by this hour
    exclude_weekdays: set[int] = field(default_factory=set)  # 0=Mon .. 6=Sun
    include_weekends: bool = False
    exclude_dates: set[date] = field(default_factory=set)  # e.g. public holidays
    buffer_minutes: int = 0  # required gap between a slot and neighbouring events
    step_minutes: int = 30
    max_results: int = 3
    max_per_day: int = 2

    def __post_init__(self) -> None:
        if self.duration <= timedelta(0) or self.step_minutes <= 0:
            raise ValueError("duration and step_minutes must be positive")
        if not 0 <= self.earliest_hour < self.latest_hour <= 24:
            raise ValueError("hours must satisfy 0 <= earliest_hour < latest_hour <= 24")
        if self.buffer_minutes < 0 or self.max_results <= 0 or self.max_per_day <= 0:
            raise ValueError("buffer must be nonnegative and result limits must be positive")


@dataclass
class Slot:
    start: datetime
    end: datetime

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": f"{self.start:%a %d %b}, {_fmt_time(self.start)} - {_fmt_time(self.end)}",
        }


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _at_hour(day: date, hour: float, tz) -> datetime:
    """Include midnight at hour 24 and round fractional hours without producing minute 60."""
    return datetime.combine(day, time.min, tzinfo=tz) + timedelta(minutes=round(hour * 60))


def _merge(periods: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    periods = sorted(p for p in periods if p[1] > p[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in periods:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _free_intervals(
    day_start: datetime, day_end: datetime, busy: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    free: list[tuple[datetime, datetime]] = []
    cursor = day_start
    for s, e in busy:
        if e <= cursor:
            continue
        if s >= day_end:
            break
        if s > cursor:
            free.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < day_end:
        free.append((cursor, day_end))
    return free


def _round_up(dt: datetime, step: timedelta) -> datetime:
    """Round up to the next multiple of `step` within the day (keeps tz)."""
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = dt - midnight
    steps = -(-elapsed // step)  # ceil division
    return midnight + steps * step


def find_free_slots(busy: list[BusyPeriod], query: SlotQuery, now: datetime | None = None) -> list[Slot]:
    """Return free slots (chronological, capped per day and overall)."""
    step = timedelta(minutes=query.step_minutes)
    slots: list[Slot] = []
    counts: dict[date, int] = {}
    for start, free_end in _query_free_intervals(busy, query, now):
        day = start.date()
        while start + query.duration <= free_end and counts.get(day, 0) < query.max_per_day:
            end = start + query.duration
            slots.append(Slot(start, end))
            counts[day] = counts.get(day, 0) + 1
            if len(slots) >= query.max_results:
                return slots
            # Jump straight to the next nonoverlapping grid point instead of building all candidates.
            start = _round_up(end, step)
    return slots


def free_blocks(busy: list[BusyPeriod], query: SlotQuery, now: datetime | None = None) -> list[dict]:
    """Free intervals (within the query's hours) per day of the window, regardless of the meeting length.

    Lets the agent say "Monday afternoon is free" or "you have 2 to 5 open" instead of listing arbitrary starts."""
    return [
        {
            "date": start.date().isoformat(),
            "from": _fmt_time(start),
            "to": _fmt_time(end),
            "minutes": int((end - start).total_seconds() // 60),
        }
        for start, end in _query_free_intervals(busy, query, now)
        if end - start >= query.duration
    ]


def _query_free_intervals(
    busy: list[BusyPeriod], query: SlotQuery, now: datetime | None
) -> Iterator[tuple[datetime, datetime]]:
    """Apply the same hours, exclusions, and buffers to slot search and free-block summaries."""
    tz = query.window_start.tzinfo
    buffer = timedelta(minutes=query.buffer_minutes)
    busy_iv = _merge([(b.start.astimezone(tz) - buffer, b.end.astimezone(tz) + buffer) for b in busy])
    day = query.window_start.astimezone(tz).date()
    last_day = query.window_end.astimezone(tz).date()
    while day <= last_day:
        weekday = day.weekday()
        skip = (
            weekday in query.exclude_weekdays
            or (weekday >= 5 and not query.include_weekends)
            or day in query.exclude_dates
        )
        if not skip:
            day_start = max(_at_hour(day, query.earliest_hour, tz), query.window_start)
            day_end = min(_at_hour(day, query.latest_hour, tz), query.window_end)
            if now is not None:
                day_start = max(day_start, now.astimezone(tz))
            yield from _free_intervals(day_start, day_end, busy_iv)
        day += timedelta(days=1)


def find_alternatives(busy: list[BusyPeriod], query: SlotQuery, now: datetime | None = None) -> list[dict]:
    """When the strict query yields nothing, try progressively relaxed queries.

    Returns a list of {"strategy": str, "slots": [...]} entries (only non-empty ones),
    so the agent can say e.g. "Tuesday is fully booked, would Wednesday morning work?".
    """
    alternatives: list[dict] = []

    # 1. Same time-of-day preference, following days.
    later = replace(
        query,
        window_start=query.window_end,
        window_end=query.window_end + timedelta(days=5),
        max_results=3,
    )
    later_slots = find_free_slots(busy, later, now)
    if later_slots:
        alternatives.append(
            {"strategy": "same time preference on the following days", "slots": [s.to_dict() for s in later_slots]}
        )

    # 2. Same days, any time within the working day.
    relaxed = replace(
        query, earliest_hour=min(query.earliest_hour, 9.0), latest_hour=max(query.latest_hour, 18.0), max_results=3
    )
    if (relaxed.earliest_hour, relaxed.latest_hour) != (query.earliest_hour, query.latest_hour):
        relaxed_slots = find_free_slots(busy, relaxed, now)
        if relaxed_slots:
            alternatives.append(
                {"strategy": "same day(s), outside the preferred hours", "slots": [s.to_dict() for s in relaxed_slots]}
            )

    # 3. Shorter meeting on the requested days.
    if query.duration > timedelta(minutes=30):
        shorter = replace(query, duration=timedelta(minutes=30), max_results=2)
        shorter_slots = find_free_slots(busy, shorter, now)
        if shorter_slots:
            alternatives.append(
                {
                    "strategy": "a shorter 30-minute meeting in the requested window",
                    "slots": [s.to_dict() for s in shorter_slots],
                }
            )

    return alternatives


def last_weekday_of_month(any_day: datetime) -> datetime:
    """Last Mon-Fri day of the month containing `any_day` (same tz, midnight)."""
    first_next = (any_day.replace(day=1) + timedelta(days=32)).replace(day=1)
    d = first_next - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.replace(hour=0, minute=0, second=0, microsecond=0)
