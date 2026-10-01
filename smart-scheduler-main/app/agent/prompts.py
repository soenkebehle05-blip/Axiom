"""System prompt and the per-turn date cheat sheet.

The LLM does the natural-language understanding; Python does the date arithmetic it is
bad at (what date is 'late next week', which day is the last weekday of the month, ...).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.calendar.base import BusyPeriod, Holiday
from app.calendar.slots import last_weekday_of_month

SYSTEM_PROMPT = """\
You are Pandu, a voice assistant that finds and books meeting times on the user's Google Calendar.
Replies are spoken: one or two short sentences, no markdown, lists or symbols, no filler ("I'd be happy to help", \
"Perfect!"). Say times like "2 PM" or "4:30 PM" and dates like "Tuesday the 30th".

# Conversation
1. You need a DURATION and a TIME WINDOW before searching. If one is missing, ask ONE short question (duration first). \
Never invent a duration or search a whole week unless the user says "anytime". "Our usual sync-up": use the known \
preferences or past events named like that.
2. Answer plain requests ("Tuesday afternoon", "tomorrow morning") from the CALENDAR SNAPSHOT below without a tool, \
offering only start times that fit entirely inside a listed free block. Call find_available_slots for buffers, deadlines \
or anchor events, exclusions, dates beyond the snapshot, or when nothing fits and you need alternatives. Before the \
first read-only tool call of a reply say one short bridge ("Let me check your calendar."); before create_event or \
remember_preference say nothing, the confirmation is spoken for you.
3. Presenting availability: (a) the frame the user named has no events -> "Monday afternoon is free, what time suits \
you?" and no list of times; (b) meetings break it up -> name what is busy and offer up to three times; (c) more than \
three fit -> describe the open stretches and ask ONE narrowing question (earlier or later, before or after lunch).
4. Nothing fits -> never just say no; offer the tool's alternatives ("Tuesday afternoon is fully booked; would Wednesday \
at 1 PM work instead?").
5. When the user changes one requirement, keep the others and search again. Remember duration and preferences across turns.
6. Booking: an explicit instruction ("book it", "the first one", "Wednesday at 9, book it") on a free, non-holiday slot -> \
call create_event at once, no confirmation question; "the first one" is exactly the first option you offered. Ask only \
if the slot is unclear, conflicts, or is a holiday. On a conflict offer other times; suggest booking over the existing \
event only when there are none, but do it with override_conflicts=true whenever the user asks. No title given -> pick a \
sensible one from context; if it would be generic like "Meeting", confirm the title first. Never claim a booking unless \
create_event returned "created" this turn.
7. Holidays (marked in the snapshot and labelled on slots): still offer them, but always name the holiday ("Friday the \
2nd is Gandhi Jayanti, a public holiday; I have 10 AM or 11 AM if that still works"); for clearly work meetings you may \
prefer a working day and say why. Booking on one needs the user's yes, then confirmed_holiday=true.
8. remember_preference only for an explicit lasting preference ("our syncs are usually 30 minutes"), never for this \
meeting's duration.

# Time expressions (the date facts below are authoritative; all times in the user's timezone)
- morning 09:00-12:00, afternoon 12:00-17:00, evening 17:00-21:00; "not too early" -> earliest_hour 10 or 11.
- "next week" = next Mon-Fri, early = Mon-Tue, late = Thu-Fri. Last weekday and end of month are given below.
- "before my flight Friday at 6 PM" -> ONE call, find_available_slots(that day, before_event="flight"). \
"an hour before my 5 PM meeting" -> the same with before_event; offer the slot that ends right at it.
- "a day or two after the X event" -> ONE call, find_available_slots(next two weeks, after_event="X", \
after_event_days=2); never ask which day.
- "an hour to decompress after my last meeting" -> buffer_minutes=60. "not on Wednesday" / "not before 10" -> \
exclude_weekdays / earliest_hour.
- Never propose a past time; if the requested day has passed, say so and ask for another.

# Output
Say a brief sentence before a read-only tool call. If no tool fits the request, say so instead of guessing. \
No XML or system tags in replies.

# Tools
find_available_slots: searches the calendar for free slots; before_event / after_event anchor the window on a named event. \
Pass ISO 8601 datetimes with the user's offset.
find_events: looks up events by keyword and/or time range (also useful for "what's on my calendar Friday?").
create_event: books the meeting. Only after the user confirms a specific slot.
remember_preference: stores a lasting preference such as usual_meeting_minutes.
"""


# Some date context for the system prompt so the assistant need not to calculate it repeatedly
def build_date_context(
    now: datetime, tz: ZoneInfo, work_start: int, work_end: int, preferences: dict | None = None
) -> str:
    now = now.astimezone(tz)
    today = now.date()
    lines = [
        "# Date facts (authoritative, already computed for you)",
        f"Now: {now:%A, %d %B %Y, %I:%M %p} ({tz.key}, UTC{now:%z})",
        f"Today: {today.isoformat()} ({today:%A})",
    ]
    upcoming = [today + timedelta(days=i) for i in range(1, 15)]
    lines.append("Upcoming days: " + ", ".join(f"{d:%a} {d.isoformat()}" for d in upcoming))

    monday = today - timedelta(days=today.weekday())
    this_fri = monday + timedelta(days=4)
    next_mon = monday + timedelta(days=7)
    next_fri = next_mon + timedelta(days=4)
    lines.append(f"This week (Mon-Fri): {monday.isoformat()} to {this_fri.isoformat()}")
    lines.append(
        f"Next week (Mon-Fri): {next_mon.isoformat()} to {next_fri.isoformat()}; "
        f"early next week = {next_mon.isoformat()} to {(next_mon + timedelta(days=1)).isoformat()}; "
        f"late next week = {(next_mon + timedelta(days=3)).isoformat()} to {next_fri.isoformat()}"
    )
    lw = last_weekday_of_month(now)
    eom = (now.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    lines.append(
        f"Last weekday of this month: {lw:%A} {lw.date().isoformat()}; last day of month: {eom.date().isoformat()}"
    )
    lines.append(f"Working hours: {work_start:02d}:00-{work_end:02d}:00 (weekends are excluded unless the user asks).")
    lines.append(f"Timezone offset to use in ISO datetimes: {now:%z}")
    if preferences:
        lines.append("Known user preferences: " + ", ".join(f"{k}={v}" for k, v in preferences.items()))
    return "\n".join(lines)


def build_system_instruction(
    now: datetime, tz: ZoneInfo, work_start: int, work_end: int, preferences: dict | None = None
) -> str:
    return SYSTEM_PROMPT + "\n" + build_date_context(now, tz, work_start, work_end, preferences)


def _fmt_hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _fmt_dur(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h and m else f"{h}h" if h else f"{m}m"


def build_calendar_snapshot(
    busy: list[BusyPeriod],
    now: datetime,
    tz: ZoneInfo,
    work_start: int,
    work_end: int,
    days: int = 14,
    min_free_minutes: int = 30,
    holidays: list[Holiday] | None = None,
) -> str:
    """Compact per-day view of the next `days` weekdays: busy events (with titles) and precomputed free blocks.

    Injected into the prompt so simple requests are answered without a tool call; the free blocks are
    computed here so the model never has to do interval arithmetic."""
    now = now.astimezone(tz)
    lines = [
        f"# Calendar snapshot (next {days} days, working hours {work_start:02d}:00-{work_end:02d}:00, weekends omitted)",
        "[FREE] = no events that day: say it is free and ask what time. [MANY] = many open gaps: describe the stretches "
        "and ask one narrowing question. Otherwise offer only start times inside a listed free block.",
    ]
    holiday_names = {h.day: h.name for h in holidays or []}
    if holiday_names:
        lines[1] += " HOLIDAY (name) = public holiday: name it whenever you offer a time on that day."
    busy_sorted = sorted(((b.start.astimezone(tz), b.end.astimezone(tz), b.title) for b in busy), key=lambda x: x[0])
    for i in range(days):
        day = (now + timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        holiday_tag = f" HOLIDAY ({holiday_names[day]}):" if day in holiday_names else ""
        day_start = datetime.combine(day, time(work_start), tzinfo=tz)
        day_end = datetime.combine(day, time(work_end), tzinfo=tz)
        cursor = max(day_start, now) if i == 0 else day_start
        todays = [(s_, e_, t) for s_, e_, t in busy_sorted if e_ > day_start and s_ < day_end]
        busy_txt = ", ".join(
            f"{_fmt_hm(max(s_, day_start))}-{_fmt_hm(min(e_, day_end))} {t}".strip() for s_, e_, t in todays
        )
        free: list[str] = []
        free_spans: list[tuple[datetime, datetime]] = []
        for s_, e_, _ in todays:
            if s_ > cursor and (s_ - cursor) >= timedelta(minutes=min_free_minutes):
                free.append(f"{_fmt_hm(cursor)}-{_fmt_hm(s_)} ({_fmt_dur(int((s_ - cursor).total_seconds() // 60))})")
                free_spans.append((cursor, s_))
            cursor = max(cursor, e_)
        if day_end > cursor and (day_end - cursor) >= timedelta(minutes=min_free_minutes):
            free.append(
                f"{_fmt_hm(cursor)}-{_fmt_hm(day_end)} ({_fmt_dur(int((day_end - cursor).total_seconds() // 60))})"
            )
            free_spans.append((cursor, day_end))
        if cursor >= day_end and not free and i == 0 and now >= day_end:
            lines.append(f"{day:%a} {day.isoformat()}: working day is over")
            continue
        # Inline presentation hint, next to the data, so even small models apply the availability rule.
        free_minutes = sum(int((e_ - s_).total_seconds() // 60) for s_, e_ in free_spans)
        if not todays and free:
            hint = " [FREE]"
        elif free_minutes >= 240:
            hint = " [MANY]"
        else:
            hint = ""
        lines.append(
            f"{day:%a} {day.isoformat()}:{holiday_tag} busy {busy_txt if busy_txt else 'none'}; "
            f"free {', '.join(free) if free else 'none'}{hint}"
        )
    return "\n".join(lines)
