"""Calendar abstraction shared by the real Google client and the in-memory fake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass
class Event:
    id: str
    title: str
    start: datetime  # timezone-aware
    end: datetime  # timezone-aware
    description: str = ""
    link: str = ""
    blocks_time: bool = True
    calendar: str = ""  # display name of the source calendar, when known

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_minutes": int((self.end - self.start).total_seconds() // 60),
            **({"link": self.link} if self.link else {}),
            **({"calendar": self.calendar} if self.calendar else {}),
        }


@dataclass
class BusyPeriod:
    start: datetime
    end: datetime
    title: str = ""  # optional, used to explain conflicts


@dataclass
class Holiday:
    day: date
    name: str

    def to_dict(self) -> dict:
        return {"date": self.day.isoformat(), "name": self.name}


class CalendarClient(Protocol):
    """Blocking calendar operations. The agent wraps them in a thread."""

    def holidays(self, start: datetime, end: datetime) -> list[Holiday]: ...

    def busy_periods(self, start: datetime, end: datetime) -> list[BusyPeriod]: ...

    def search_events(self, start: datetime, end: datetime, query: str | None = None) -> list[Event]: ...

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "") -> Event: ...
