"""Per-conversation state: LLM history, preferences, last offered slots."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_PREFERENCES: dict[str, str] = {"usual_meeting_minutes": "30"}


@dataclass
class Session:
    tz: ZoneInfo
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    history: list[Any] = field(default_factory=list)  # provider-native messages
    preferences: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PREFERENCES))
    preference_store: dict[str, str] | None = field(default=None, repr=False)
    last_offered_slots: list[dict] = field(default_factory=list)
    booked: list[dict] = field(default_factory=list)
    snapshot: str = ""  # calendar snapshot text injected into the prompt (see prompts.build_calendar_snapshot)
    snapshot_at: float = 0.0  # time.time() when it was fetched; 0 = stale

    def copy(self) -> Session:
        return Session(
            tz=self.tz,
            id=self.id,
            history=list(self.history),
            preferences=dict(self.preferences),
            preference_store=self.preference_store,
            last_offered_slots=list(self.last_offered_slots),
            booked=list(self.booked),
            snapshot=self.snapshot,
            snapshot_at=self.snapshot_at,
        )

    def remember(self, key: str, value: str) -> None:
        self.preferences[key] = value
        if self.preference_store is not None:
            self.preference_store[key] = value
