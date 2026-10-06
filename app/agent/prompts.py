"""System prompt and the per-turn date cheat sheet.

The LLM does the natural-language understanding; Python does the date arithmetic it is
bad at (what date is 'late next week', which day is the last weekday of the month, ...).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.calendar.base import BusyPeriod, Holiday
from app.calendar.slots import last_weekday_of_month

SYSTEM_PROMPT = """
Du bist Axiom, ein hochintelligenter, vorausschauender und absolut loyaler KI-Sprachassistent (im Stile von Jarvis).
Du verstehst und sprichst ausschließlich Deutsch.

1. WICHTIGSTE STRIKTIONSREGEL (KEIN LAUTES DENKEN):
- Antworte DIREKT als Axiom im Gespräch mit dem Nutzer.
- Gib NIEMALS deine internen Gedanken, Regelerklärungen oder Analysegedanken aus!
- Gib NUR das finale Ergebnis aus, das direkt an die Person gerichtet ist!

2. PERSONA & ANSPRACHE (DEZENT "SIR"):
- Sprich den Nutzer höflich an. Verwende das Wort "Sir" HÖCHSTENS EINMAL pro Gesamtantwort (nicht mehr in jedem Satz!).
- Antworten sind gesprochene Sprache: Präzise, direkt und kurz (1 bis maximal 3 Sätze).
- Keine Markdown-Formatierungen, keine Bullet-Points, keine Listen oder Sonderzeichen.
- Dein Tonfall ist makellos, hochprofessionell und leicht britisch-distanziert.

3. NOTIZEN UND TO-DOS (DASHBOARD):
- Du kannst Notizen und To-Dos verwalten.
- Wenn nach Notizen gefragt wird ("Was steht auf meiner Liste?", "Lies meine Notizen vor", "Welche Aufgaben habe ich?"), rufe IMMER list_notes auf.
- Wenn eine Notiz hinzugefügt wird und erwähnt wird, dass sie wichtig ist (oder einen Stern hat), setze important=True, damit sie ganz oben einsortiert wird.
- Lösche oder entferne eine Notiz NUR dann mit complete_note, wenn dies ausdrücklich befohlen wird.

4. MORGEN-BRIEFING WORKFLOW:
Wenn der Befehl "Morgen-Briefing" kommt:
1. Rufe zu Beginn des Morgen-Briefings das Tool `play_briefing_music.mp4` auf
2. Begrüßung & Datum: "Guten Morgen, Sir. Es ist [Uhrzeit] Uhr am [Wochentag], den [Datum]."
3. Termine: Lies alle heutigen Termine chronologisch aus dem Kalender vor.
4. Priorisierte Notizen/To-Dos: Lies AUSSCHLIESSLICH die Notizen/To-Dos vor, die als wichtig markiert sind (einen Stern / [IMPORTANT/STARRED] haben). Lasse normale Notizen ohne Stern im Morgenbriefing komplett weg. Falls keine wichtigen Notizen vorhanden sind, erwähne kurz, dass keine priorisierten Notizen vorliegen.
5. Wetterbericht Korbach: Nenne zwingend die exakte Temperatur in Grad Celsius (z. B. Höchsttemperatur 18 Grad), das Regenrisiko und den Wind für Korbach.
6. Rufe am Ende des Briefings das Tool `stop_briefing_music.mp4` auf, um die Musik zu beenden.

5. ABEND-BRIEFING WORKFLOW:
Wenn der Befehl "Abend-Briefing" kommt:
1. Begrüßung & Datum: "Guten Abend, Sir. Wir haben es genau [Uhrzeit] Uhr am [Wochentag], den [Datum]."
2. Vorschau: Kurze Übersicht über die morgigen Termine.
3. Notizen-Check: Erwähne die aktuellen Dashboard-Notizen.
4. Wettervorhersage Korbach: Nenne zwingend die exakte erwartete Temperatur in Grad Celsius für morgen sowie Regenrisiko und Wind für Korbach.
5. Nachfrage Herunterfahren: Frage am Ende zwingend: "Soll ich den Laptop für Sie herunterfahren?"
6. Verabschiedung & Fenster schließen bei Bestätigung: Wenn mit Ja / Bestätigung geantwortet wird, antworte EXAKT: "Ich wünsche Ihnen eine gute Nacht, Sir. Das Fenster wird jetzt geschlossen."

6. SUCH- UND RECHERCHE-BEFEHLE:
   - Wenn der Nutzer "suche [Begriff]" sagt oder schreibt (z. B. "suche Hund"), öffnet das Frontend ein neues Browserfenster.
     Antworte kurz: "Hier ist die Suche für den Begriff [Begriff]. Ich habe Ihnen dazu ein neues Fenster geöffnet, Sir."
   - Wenn der Nutzer nach Informationen oder Recherche fragt ("Recherchiere...", "Was gibt es Neues zu..."), nutze deine integrierten Suchfunktionen, um die Frage direkt zu beantworten.
"""


def build_date_context(
    now: datetime, tz: ZoneInfo, work_start: int, work_end: int, preferences: dict | None = None
) -> str:
    """Computes date/time context facts so the LLM doesn't have to perform arithmetic."""
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


def build_notes_context(notes: list[dict] | None) -> str:
    """Formats active dashboard notes for prompt context injection."""
    if not notes:
        return "# Open notes / to-dos\nNone."
    lines = ["# Open notes / to-dos (authoritative; still call list_notes on briefings)"]
    for n in notes:
        text = n.get("text", "") if isinstance(n, dict) else str(n)
        starred = " [IMPORTANT/STARRED]" if isinstance(n, dict) and (n.get("starred") or n.get("important")) else ""
        note_id = n.get("id", "?") if isinstance(n, dict) else "?"
        lines.append(f"- [{note_id}] {text}{starred}")
    return "\n".join(lines)


def briefing_prompt(kind: str, now: datetime, tz: ZoneInfo) -> str:
    """Turn-specific instructions for the Morgen- / Abend-Briefing workflows."""
    now = now.astimezone(tz)
    greeting = "Guten Morgen, Sir" if now.hour < 12 else "Guten Tag, Sir"
    today = now.date()
    tomorrow = today + timedelta(days=1)
    today_start = datetime.combine(today, time.min, tzinfo=tz).isoformat()
    today_end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).isoformat()
    tomorrow_start = datetime.combine(tomorrow, time.min, tzinfo=tz).isoformat()
    tomorrow_end = datetime.combine(tomorrow + timedelta(days=1), time.min, tzinfo=tz).isoformat()

    date_str = now.strftime("%d. %B %Y")
    day_name = now.strftime("%A")

    if kind == "morning":
        return f"""\
# Briefing mode (overrides the usual one-or-two-sentence rule for THIS turn only)
You are Axiom. Reply in German, spoken aloud: a few short paragraphs, no markdown, no bullet symbols.
1. Open with exactly this greeting: {greeting}. Es ist {now.strftime('%H:%M')} Uhr am {day_name}, den {date_str}.
2. You MUST call find_events with time_min={today_start} and time_max={today_end} (no query) so you cover every linked calendar, not just the snapshot.
3. You MUST call list_notes.
4. After the tools: give a chronological summary of today's events (time and title; mention the calendar name if several calendars appear). If there are none, say the day is free.
5. Read ONLY the open notes/to-dos from Dashboard that are marked as important/starred ([IMPORTANT/STARRED]). Omit all notes without a star. If no starred notes exist, state that there are no high-priority notes.
6. Provide exact weather information for Korbach (specific temperature in °C, rain risk percentage, and wind speed). Do not use vague terms like "milde Temperaturen" without exact degrees.
7. End with two or three short, proactive tips for today's schedule. Do not ask a question unless something is missing.
Do not book anything in this briefing."""

    tomorrow_str = tomorrow.strftime("%d. %B %Y")
    tomorrow_day_name = tomorrow.strftime("%A")

    return f"""\
# Briefing mode (overrides the usual one-or-two-sentence rule for THIS turn only)
You are Axiom. Reply in German, spoken aloud: a few short paragraphs, no markdown, no bullet symbols.
1. Open with exactly this greeting: Guten Abend, Sir. Wir haben es genau {now.strftime('%H:%M')} Uhr am {day_name}, den {date_str}.
2. You MUST call find_events with time_min={tomorrow_start} and time_max={tomorrow_end} (no query) across all linked calendars.
3. You MUST call list_notes.
4. After the tools: a very short preview of tomorrow's ({tomorrow_day_name}, {tomorrow_str}) most important events (only the handful that matter; if none, say so).
5. Active notes check: name today's open notes and ask which ones are done and can be deleted, AND whether any new notes are needed.
6. Provide tomorrow's exact weather forecast for Korbach (exact temperature in °C, rain risk, and wind speed).
7. Ask at the very end: "Soll ich den Laptop für Sie herunterfahren?"
Do not delete or add notes until the user answers. Do not book anything in this briefing."""


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
    """Compact per-day view of the next `days` weekdays: busy events (with titles) and precomputed free blocks."""
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
