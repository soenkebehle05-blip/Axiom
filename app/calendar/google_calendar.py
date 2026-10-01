"""Google Calendar API v3 client (OAuth2 user credentials)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import BusyPeriod, Event, Holiday

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Google's public holiday calendars, picked by the calendar's timezone. Readable with any authenticated token.
HOLIDAY_CALENDARS = {
    "Asia/Kolkata": "en.indian#holiday@group.v.calendar.google.com",
    "Asia/Singapore": "en.singapore#holiday@group.v.calendar.google.com",
    "Asia/Dubai": "en.ae#holiday@group.v.calendar.google.com",
    "Europe/London": "en.uk#holiday@group.v.calendar.google.com",
    "Europe/Berlin": "en.german#holiday@group.v.calendar.google.com",
    "Europe/Paris": "en.french#holiday@group.v.calendar.google.com",
    "America/": "en.usa#holiday@group.v.calendar.google.com",
    "Australia/": "en.australian#holiday@group.v.calendar.google.com",
}


def holiday_calendar_for(tz_name: str) -> str | None:
    for prefix, cal_id in HOLIDAY_CALENDARS.items():
        if tz_name == prefix or tz_name.startswith(prefix):
            return cal_id
    return None


class GoogleCalendar:
    def __init__(self, creds: Credentials, calendar_id: str = "primary", holiday_calendar_id: str | None = None):
        self.creds = creds
        self.calendar_id = calendar_id
        self.holiday_calendar_id = holiday_calendar_id  # None = no holiday awareness
        self._holiday_cache: tuple[str, float, list[Holiday]] | None = None
        self._request_lock = Lock()  # snapshot and tool workers share a non-thread-safe HTTP transport
        # cache_discovery=False avoids a noisy warning about the file cache in server envs.
        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _execute(self, request):
        with self._request_lock:
            return request.execute()

    def _get_calendar_ids(self) -> list[str]:
        """Returns all calendar IDs accessible by the user including sub-calendars."""
        calendar_ids = []
        page_token = None
        while True:
            resp = self._execute(
                self.service.calendarList().list(pageToken=page_token)
            )
            for item in resp.get("items", []):
                cal_id = item.get("id")
                if cal_id and cal_id != self.holiday_calendar_id:
                    calendar_ids.append(cal_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if not calendar_ids:
            calendar_ids = [self.calendar_id]
        return calendar_ids

    @classmethod
    def from_token(
        cls, token_json: str = "", token_file: str = "", calendar_id: str = "primary"
    ) -> GoogleCalendar | None:
        """Load authorized-user credentials from a JSON string or file; None if unavailable."""
        raw = token_json.strip()
        if not raw and token_file and Path(token_file).exists():
            raw = Path(token_file).read_text()
        if not raw:
            return None
        return cls.from_json(raw, calendar_id)

    @classmethod
    def from_json(cls, raw: str, calendar_id: str = "primary") -> GoogleCalendar:
        """Build a client from authorized-user JSON (the token.json written by scripts/authorize_google.py).

        Raises ValueError on anything that is not a usable token, so an upload can be rejected cleanly."""
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("not valid JSON") from exc
        missing = [
            k for k in ("client_id", "client_secret", "refresh_token") if not isinstance(info, dict) or not info.get(k)
        ]
        if missing:
            raise ValueError(
                f"token is missing {', '.join(missing)}; upload the token.json from scripts/authorize_google.py"
            )
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return cls(creds, calendar_id)

    def probe(self) -> dict:
        """Cheap call for validating the token; returns the calendar's identity for the UI."""
        cal = self._execute(self.service.calendars().get(calendarId=self.calendar_id))
        return {
            "id": cal.get("id", self.calendar_id),
            "summary": cal.get("summary", ""),
            "timezone": cal.get("timeZone", ""),
        }

    def holidays(self, start: datetime, end: datetime) -> list[Holiday]:
        """Public holidays in the range from Google's regional holiday calendar (observances are skipped)."""
        if not self.holiday_calendar_id:
            return []
        key = f"{self.holiday_calendar_id}:{start.date()}..{end.date()}"
        if self._holiday_cache and self._holiday_cache[0] == key and time.time() - self._holiday_cache[1] < 6 * 3600:
            return self._holiday_cache[2]
        resp = self._execute(
            self.service.events().list(
                calendarId=self.holiday_calendar_id,
                timeMin=start.astimezone(UTC).isoformat(),
                timeMax=end.astimezone(UTC).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=100,
            )
        )
        found: list[Holiday] = []
        for item in resp.get("items", []):
            desc = (item.get("description") or "").lower()
            day = item.get("start", {}).get("date")
            if day and ("public holiday" in desc or not desc):
                found.append(Holiday(date.fromisoformat(day), item.get("summary", "Holiday")))
        self._holiday_cache = (key, time.time(), found)
        return found

    def busy_periods(self, start: datetime, end: datetime) -> list[BusyPeriod]:
        # events.list (rather than freebusy) so conflicts can be explained by title.
        return [BusyPeriod(e.start, e.end, e.title) for e in self._list(start, end) if e.blocks_time]

    def search_events(self, start: datetime, end: datetime, query: str | None = None) -> list[Event]:
        return self._list(start, end, query)

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "") -> Event:
        body = {
            "id": uuid.uuid4().hex,
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "conferenceData": {
                "createRequest": {"requestId": uuid.uuid4().hex, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
            },
        }
        try:
            created = self._execute(
                self.service.events().insert(calendarId=self.calendar_id, body=body, conferenceDataVersion=1)
            )
        except HttpError as exc:
            # Only a definitive conference-validation rejection can be retried without Meet.
            # A timeout or 5xx may follow a successful insert; retrying could create a duplicate.
            if exc.resp.status != 400 or "conference" not in str(exc).lower():
                raise
            log.warning("insert with Meet failed (%s); retrying without conference", exc)
            body.pop("conferenceData")
            created = self._execute(self.service.events().insert(calendarId=self.calendar_id, body=body))
        return self._to_event(created)

    def _list(self, start: datetime, end: datetime, query: str | None = None) -> list[Event]:
        items: list[dict] = []
        calendar_tz = start.tzinfo
        calendar_ids = self._get_calendar_ids()

        for cal_id in calendar_ids:
            page_token = None
            while True:
                resp = self._execute(
                    self.service.events().list(
                        calendarId=cal_id,
                        timeMin=start.astimezone(UTC).isoformat(),
                        timeMax=end.astimezone(UTC).isoformat(),
                        singleEvents=True,
                        orderBy="startTime",
                        q=query or None,
                        maxResults=250,
                        pageToken=page_token,
                    )
                )
                items.extend(resp.get("items", []))
                if resp.get("timeZone"):
                    calendar_tz = ZoneInfo(resp["timeZone"])
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        events = []
        seen_ids = set()
        for item in items:
            item_id = item.get("id")
            if item_id in seen_ids:
                continue
            if item.get("status") == "cancelled":
                continue
            ev = self._to_event(item, calendar_tz)
            if ev is not None:
                events.append(ev)
                if item_id:
                    seen_ids.add(item_id)
        
        events.sort(key=lambda e: e.start)
        return events

    @staticmethod
    def _to_event(item: dict, calendar_tz=UTC) -> Event | None:
        start, end = item.get("start", {}), item.get("end", {})
        blocks_time = item.get("transparency") != "transparent" and not any(
            attendee.get("self") and attendee.get("responseStatus") == "declined"
            for attendee in item.get("attendees", [])
        )
        if "dateTime" not in start:
            # All-day ends are exclusive, in the calendar's timezone, just like timed busy periods.
            try:
                s = datetime.fromisoformat(start["date"]).replace(tzinfo=calendar_tz)
                e = datetime.fromisoformat(end["date"]).replace(tzinfo=calendar_tz)
            except (KeyError, ValueError):
                return None
            return Event(
                item["id"],
                item.get("summary", "(no title)"),
                s,
                e,
                item.get("description") or "",
                item.get("htmlLink", ""),
                blocks_time=blocks_time,
            )
        return Event(
            id=item["id"],
            title=item.get("summary", "(no title)"),
            start=datetime.fromisoformat(start["dateTime"]),
            end=datetime.fromisoformat(end["dateTime"]),
            description=item.get("description", "") or "",
            link=item.get("hangoutLink") or item.get("htmlLink", ""),
            blocks_time=blocks_time,
        )
