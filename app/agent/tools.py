"""Tool declarations exposed to Gemini and their handlers.

Handlers are thin: validate/normalise arguments, call the calendar, shape the result.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timedelta

import httpx

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
            "List events from all of the user's linked calendars in a time range, optionally filtered by a "
            "title keyword. Each event may include a `calendar` name. For anchoring a search use "
            "find_available_slots before_event/after_event instead."
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
    ToolSpec(
        name="list_notes",
        description="List the user's currently open notes and to-dos.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="add_note",
        description="Add an open note or to-do (for example after the evening briefing).",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The note or to-do to keep"},
                "important": {"type": "boolean", "description": "True if the note is starred / high priority"},
            },
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="complete_note",
        description="Tick off / delete an open note once the user says it is done. Identify it by id or a text query.",
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Note id from list_notes"},
                "query": {"type": "string", "description": "Substring of the note text if the id is unknown"},
            },
        },
    ),
    ToolSpec(
        name="get_weather",
        description="Get exact weather forecast including temperature in °C, rain risk, and wind for a location.",
        input_schema={
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name, default Korbach"}
            },
        },
    ),
    ToolSpec(
        name="search_web",
        description="Search the internet for current events, info, or live data.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="play_briefing_music",
        description="Play background music for briefings from static file.",
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Audio file path under static, e.g. briefing_music.mp4"}
            },
        },
    ),
    ToolSpec(
        name="stop_briefing_music",
        description="Stop briefing background music.",
        input_schema={"type": "object", "properties": {}},
    ),
]


SIDE_EFFECT_TOOLS = {"create_event", "remember_preference", "add_note", "complete_note", "play_briefing_music", "stop_briefing_music"}
TOOL_NAMES = {tool.name for tool in TOOL_SPECS}


class ToolRunner:
    def __init__(
        self,
        calendar: CalendarClient,
        session: Session,
        work_start: int,
        work_end:
