"""Replay the assignment's test scenarios against the agent (Gemini + your Google Calendar).

Usage: python scripts/run_scenarios.py [scenario-number ...]
Prints each conversation with the tool calls the agent made, for manual review.
Seed the calendar first with scripts/seed_calendar.py. Scenario 1 books a real event.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.agent import Agent
from app.agent.llm import build_llm
from app.agent.session import Session
from app.calendar.google_calendar import GoogleCalendar
from app.config import get_settings

SCENARIOS: list[tuple[str, list[str]]] = [
    (
        "Basic flow from the brief",
        ["I need to schedule a meeting.", "1 hour.", "Sometime on Tuesday afternoon.", "The first one works, book it."],
    ),
    (
        "Deadline: before Friday flight",
        ["I need to meet for 45 minutes sometime before my flight that leaves on Friday at 6 PM."],
    ),
    (
        "Anchored on a calendar event",
        [
            "Let's find a time for a quick 15-minute chat a day or two after the 'Project Alpha Kick-off' event on my calendar."
        ],
    ),
    ("Last weekday of the month", ["Can we schedule a 1-hour meeting for the last weekday of this month?"]),
    (
        "Vague with negative constraints",
        ["I'm free sometime next week, but not too early in the morning and not on Wednesday.", "30 minutes"],
    ),
    ("Usual sync-up (memory)", ["Let's schedule our usual sync-up.", "Sometime tomorrow"]),
    (
        "Evening with decompress buffer",
        [
            "Find a time in the evening, maybe after 7, but I need at least an hour to decompress after my last meeting of the day.",
            "Tomorrow, 30 minutes",
        ],
    ),
    (
        "Changing requirements mid-conversation",
        [
            "Find me a 30-minute slot for tomorrow morning.",
            "Actually, my colleague needs to join, so we'll need a full hour. Are any of those times still available for an hour?",
        ],
    ),
    ("Late next week", ["Book a 1 hour design review sometime late next week."]),
]


async def run(index: int, name: str, turns: list[str]) -> None:
    s = get_settings()
    tz = ZoneInfo(s.default_timezone)
    llm = build_llm(s)
    cal = GoogleCalendar.from_token(s.calendar_creds_json, s.calendar_creds_file, s.google_calendar_id)
    if cal is None:
        sys.exit("No Google credentials. Run `python scripts/authorize_google.py` first.")
    agent = Agent(llm, cal, Session(tz=tz), s.work_day_start, s.work_day_end, s.slot_step_minutes)
    print(f"\n=== Scenario {index}: {name} ===")
    for text in turns:
        print(f"\nUser: {text}")
        async for ev in agent.run_turn(text):
            if ev.type == "tool_call":
                print(f"  ⚙ {ev.data['name']}({ev.data['args']})")
            elif ev.type == "tool_result":
                r = ev.data["result"]
                summary = {k: (v if k != "busy_in_window" else f"{len(v)} busy") for k, v in r.items()}
                print(f"  ↳ {summary}")
            elif ev.type == "done":
                print(f"Bot: {ev.data}")


async def main() -> None:
    st = get_settings()
    if not (st.anthropic_api_key or st.gemini_api_key or st.openai_api_key):
        sys.exit("Set ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENAI_API_KEY (or put it in .env) first.")
    wanted = {int(a) for a in sys.argv[1:]} or set(range(1, len(SCENARIOS) + 1))
    for i, (name, turns) in enumerate(SCENARIOS, start=1):
        if i in wanted:
            await run(i, name, turns)


if __name__ == "__main__":
    asyncio.run(main())
