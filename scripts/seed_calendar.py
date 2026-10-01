"""Create the demo events in your real Google Calendar so every assignment scenario has data.

Usage: python scripts/seed_calendar.py            # creates the events
       python scripts/seed_calendar.py --delete   # removes previously seeded events (tagged in description)
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calendar.fake_calendar import seed_events
from app.calendar.google_calendar import GoogleCalendar
from app.config import get_settings

TAG = "[smart-scheduler demo]"


def main() -> None:
    s = get_settings()
    cal = GoogleCalendar.from_token(s.calendar_creds_json, s.calendar_creds_file, s.google_calendar_id)
    if cal is None:
        sys.exit("No Google credentials. Run `python scripts/authorize_google.py` first.")
    tz = ZoneInfo(s.default_timezone)
    now = datetime.now(tz)

    if "--delete" in sys.argv:
        removed = 0
        for ev in cal.search_events(now - timedelta(days=60), now + timedelta(days=60), "smart-scheduler demo"):
            cal.service.events().delete(calendarId=cal.calendar_id, eventId=ev.id).execute()
            removed += 1
        print(f"Deleted {removed} seeded events.")
        return

    for ev in seed_events(now, tz):
        created = cal.create_event(ev.title, ev.start, ev.end, f"{ev.description} {TAG}".strip())
        print(f"  + {created.start:%a %d %b %H:%M}-{created.end:%H:%M}  {created.title}")
    print("Done. Open Google Calendar to check.")


if __name__ == "__main__":
    main()
