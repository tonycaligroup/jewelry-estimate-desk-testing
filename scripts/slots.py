#!/usr/bin/env python3
"""Candidate meeting times from the shop's declared windows, checked live.

WORKFLOW.md 6.7: every booking is calendar-checked and owner-approved, and the
approval must show the times. This module turns `scheduling.windows` into
concrete slots inside the offer window, drops anything the live free/busy
query marks busy, and returns two or three labelled options the owner can
read on the card. No judgment involved; the model only reports what the
customer asked for.

Window entries in the shop profile look like
`{"days": ["mon", "tue", "wed"], "start": "10:00", "end": "17:00"}` (`day`
with one name also works). Times are local to `scheduling.timezone`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import appointment_options
import calendar_query

DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
DEFAULT_DURATION_MINUTES = 30
MAX_OPTIONS = 3


def _minutes(value: Any) -> int:
    text = str(value or "").strip()
    hours, _, mins = text.partition(":")
    if not hours.isdigit() or (mins and not mins.isdigit()):
        raise ValueError(f"window time {text!r} must be HH:MM")
    hour, minute = int(hours), int(mins or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"window time {text!r} is out of range")
    return hour * 60 + minute


def parse_windows(scheduling: dict[str, Any]) -> list[tuple[set[int], int, int]]:
    """Declared availability as (weekdays, start minute, end minute) triples."""
    windows = scheduling.get("windows") or []
    parsed: list[tuple[set[int], int, int]] = []
    for entry in windows:
        if not isinstance(entry, dict):
            continue
        raw_days = entry.get("days", entry.get("day"))
        names = raw_days if isinstance(raw_days, list) else [raw_days]
        days = {DAY_NAMES[str(name).strip().lower()] for name in names if str(name).strip().lower() in DAY_NAMES}
        try:
            start, end = _minutes(entry.get("start")), _minutes(entry.get("end"))
        except ValueError:
            continue
        if days and end > start:
            parsed.append((days, start, end))
    return parsed


def duration_minutes(scheduling: dict[str, Any]) -> int:
    durations = scheduling.get("durations_minutes")
    if isinstance(durations, dict):
        for key in ("consultation", "appointment", "meeting", "default"):
            value = durations.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
        for value in durations.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
    return DEFAULT_DURATION_MINUTES


def candidate_slots(
    scheduling: dict[str, Any], now: datetime, limit: int = MAX_OPTIONS * 4
) -> list[dict[str, str]]:
    """Slots inside the windows over the offer period, earliest first."""
    zone = ZoneInfo(scheduling.get("timezone") or "UTC")
    windows = parse_windows(scheduling)
    if not windows:
        return []
    length = timedelta(minutes=duration_minutes(scheduling))
    buffer = timedelta(minutes=int(scheduling.get("buffer_minutes") or 0))
    notice = timedelta(minutes=int(scheduling.get("minimum_notice_minutes") or 0))
    days_ahead = int(scheduling.get("meeting_offer_window_days") or 7)
    local_now = now.astimezone(zone)
    earliest = local_now + notice
    slots: list[dict[str, str]] = []
    per_day_cap = 2  # two per day keeps the options spread over the week
    for offset in range(days_ahead + 1):
        day = (local_now + timedelta(days=offset)).date()
        taken = 0
        for days, start, end in sorted(windows, key=lambda w: w[1]):
            if day.weekday() not in days:
                continue
            cursor = datetime(day.year, day.month, day.day, start // 60, start % 60, tzinfo=zone)
            close = datetime(day.year, day.month, day.day, end // 60, end % 60, tzinfo=zone)
            while cursor + length <= close and taken < per_day_cap:
                if cursor >= earliest:
                    slots.append({"start": cursor.isoformat(), "end": (cursor + length).isoformat()})
                    taken += 1
                    if len(slots) >= limit:
                        return slots
                cursor += length + buffer
    return slots


def preferred_slots(scheduling: dict[str, Any], requested: list[str], now: datetime) -> list[dict[str, str]]:
    """The customer's own resolved times as slots, kept only when inside the windows."""
    zone = ZoneInfo(scheduling.get("timezone") or "UTC")
    windows = parse_windows(scheduling)
    length = timedelta(minutes=duration_minutes(scheduling))
    notice = timedelta(minutes=int(scheduling.get("minimum_notice_minutes") or 0))
    earliest = now.astimezone(zone) + notice
    slots: list[dict[str, str]] = []
    for text in requested:
        try:
            start = datetime.strptime(str(text), "%Y-%m-%dT%H:%M").replace(tzinfo=zone)
        except (TypeError, ValueError):
            continue
        end = start + length
        minute = start.hour * 60 + start.minute
        inside = any(
            start.weekday() in days and minute >= w_start and minute + int(length.total_seconds() // 60) <= w_end
            for days, w_start, w_end in windows
        )
        if inside and start >= earliest:
            slot = {"start": start.isoformat(), "end": end.isoformat()}
            if slot not in slots:
                slots.append(slot)
    return slots


def offer_times(
    profile: dict[str, Any],
    token: str,
    out_dir: Path,
    now: datetime | None = None,
    opener: Callable[..., Any] | None = None,
    requested: list[str] | None = None,
) -> dict[str, Any]:
    """Live-checked options for the owner's card, or an empty list with a reason.

    A time the customer asked for comes first whenever it is inside the
    declared windows and free; the earliest other free slots fill the rest.
    """
    scheduling = profile.get("scheduling") or {}
    calendar_id = scheduling.get("calendar")
    if not calendar_id or not parse_windows(scheduling):
        return {"options": [], "reason": "no calendar or declared windows configured"}
    zone = ZoneInfo(scheduling.get("timezone") or "UTC")
    current = (now or datetime.now(tz=zone)).astimezone(zone)
    days_ahead = int(scheduling.get("meeting_offer_window_days") or 7)
    time_min = current.replace(second=0, microsecond=0).isoformat()
    time_max = (current + timedelta(days=days_ahead)).replace(second=0, microsecond=0).isoformat()
    kwargs = {"opener": opener} if opener else {}
    receipt = calendar_query.query_freebusy(time_min, time_max, scheduling["timezone"], calendar_id, token, **kwargs)
    out_dir.mkdir(parents=True, exist_ok=True)
    calendar_query.write_private(out_dir / "calendar-receipt.json", receipt)
    busy = [
        (calendar_query.parse_timestamp(b["start"], "busy.start"), calendar_query.parse_timestamp(b["end"], "busy.end"))
        for b in receipt["response_body"]["calendars"][calendar_id].get("busy", [])
        if isinstance(b, dict)
    ]
    query_end = calendar_query.parse_timestamp(time_max, "time_max")
    free: list[dict[str, str]] = []
    for slot in preferred_slots(scheduling, requested or [], current) + candidate_slots(scheduling, current):
        if slot in free:
            continue
        start = calendar_query.parse_timestamp(slot["start"], "slot.start")
        end = calendar_query.parse_timestamp(slot["end"], "slot.end")
        if end > query_end:
            continue
        if any(start < b_end and end > b_start for b_start, b_end in busy):
            continue
        free.append(slot)
        if len(free) >= MAX_OPTIONS:
            break
    (out_dir / "calendar-candidate-slots.json").write_text(json.dumps(free), encoding="utf-8")
    options = appointment_options.build_options(receipt, free, scheduling["timezone"], days_ahead, now=current)
    calendar_query.write_private(out_dir / "calendar-options.json", options)
    return {"options": options["options"], "reason": "" if options["options"] else "no free slot inside the declared windows"}
