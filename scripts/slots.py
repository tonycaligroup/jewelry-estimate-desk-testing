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


def _inside_windows(scheduling: dict[str, Any], start: datetime, length: timedelta) -> bool:
    minute = start.hour * 60 + start.minute
    span = int(length.total_seconds() // 60)
    return any(
        start.weekday() in days and minute >= w_start and minute + span <= w_end
        for days, w_start, w_end in parse_windows(scheduling)
    )


def _slot(start: datetime, length: timedelta) -> dict[str, str]:
    return {"start": start.isoformat(), "end": (start + length).isoformat()}


def neighbour_slots(scheduling: dict[str, Any], requested: dict[str, str], now: datetime) -> list[dict[str, str]]:
    """Alternatives near a time the customer asked for: the half-hours around it
    that day, then the same clock time on the following working days."""
    zone = ZoneInfo(scheduling.get("timezone") or "UTC")
    length = timedelta(minutes=duration_minutes(scheduling))
    notice = timedelta(minutes=int(scheduling.get("minimum_notice_minutes") or 0))
    earliest = now.astimezone(zone) + notice
    asked = datetime.fromisoformat(requested["start"]).astimezone(zone)
    candidates: list[datetime] = []
    for step in (30, -30, 60, -60, 90, -90):
        candidates.append(asked + timedelta(minutes=step))
    days_ahead = int(scheduling.get("meeting_offer_window_days") or 7)
    for offset in range(1, days_ahead + 1):
        candidates.append(asked + timedelta(days=offset))
    found: list[dict[str, str]] = []
    for start in candidates:
        if start >= earliest and _inside_windows(scheduling, start, length):
            slot = _slot(start, length)
            if slot not in found:
                found.append(slot)
    return found


def all_window_slots(scheduling: dict[str, Any], now: datetime) -> list[dict[str, str]]:
    """Every slot inside the declared windows over the offer period, earliest first."""
    zone = ZoneInfo(scheduling.get("timezone") or "UTC")
    windows = parse_windows(scheduling)
    length = timedelta(minutes=duration_minutes(scheduling))
    buffer = timedelta(minutes=int(scheduling.get("buffer_minutes") or 0))
    notice = timedelta(minutes=int(scheduling.get("minimum_notice_minutes") or 0))
    days_ahead = int(scheduling.get("meeting_offer_window_days") or 7)
    local_now = now.astimezone(zone)
    earliest = local_now + notice
    found: list[dict[str, str]] = []
    for offset in range(days_ahead + 1):
        day = (local_now + timedelta(days=offset)).date()
        for days, w_start, w_end in sorted(windows, key=lambda w: w[1]):
            if day.weekday() not in days:
                continue
            cursor = datetime(day.year, day.month, day.day, w_start // 60, w_start % 60, tzinfo=zone)
            close = datetime(day.year, day.month, day.day, w_end // 60, w_end % 60, tzinfo=zone)
            while cursor + length <= close:
                if cursor >= earliest:
                    found.append(_slot(cursor, length))
                cursor += length + buffer
    return found


def spread_slots(slots_in_order: list[dict[str, str]], is_free: Callable[[dict[str, str]], bool]) -> list[dict[str, str]]:
    """When no time was asked for: the nearest days, not the whole week.

    Up to two free slots on the first day that has any (a morning and an
    afternoon when both exist, otherwise the first two), then the earliest
    free slot on the next day with one. Three at most.
    """
    found: list[dict[str, str]] = []
    per_day: dict[str, list[dict[str, str]]] = {}
    for slot in slots_in_order:
        if len(found) >= MAX_OPTIONS:
            break
        day = slot["start"][:10]
        taken = per_day.setdefault(day, [])
        if len(per_day) > 2 or (len(per_day) == 2 and day == list(per_day)[1] and taken):
            continue  # second day contributes one; a third day never
        if len(taken) >= 2:
            continue
        if taken and datetime.fromisoformat(slot["start"]).hour < 14 and datetime.fromisoformat(taken[0]["start"]).hour < 14:
            # Prefer an afternoon slot as the day's second time when one is free later on.
            later = next((s for s in slots_in_order if s["start"][:10] == day and datetime.fromisoformat(s["start"]).hour >= 14 and is_free(s)), None)
            if later is not None and later not in taken:
                taken.append(later)
                found.append(later)
                continue
        if not is_free(slot):
            continue
        taken.append(slot)
        found.append(slot)
    found.sort(key=lambda s: s["start"])
    return found[:MAX_OPTIONS]


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
    force_offer: bool = False,
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

    def is_free(slot: dict[str, str]) -> bool:
        start = calendar_query.parse_timestamp(slot["start"], "slot.start")
        end = calendar_query.parse_timestamp(slot["end"], "slot.end")
        if end > query_end:
            return False
        return not any(start < b_end and end > b_start for b_start, b_end in busy)

    asked = preferred_slots(scheduling, requested or [], current)
    mode = "offer"
    free: list[dict[str, str]] = []
    if force_offer and asked:
        # The owner named times to offer: keep every free one, in order.
        free = [s for s in asked if is_free(s)][:MAX_OPTIONS]
    elif asked and is_free(asked[0]):
        # Scenario 1: the customer's time works. One time, a yes-or-no card.
        mode, free = "book", [asked[0]]
    elif asked:
        # Scenario 3: asked for a time that is taken; offer times around it.
        free = [s for s in neighbour_slots(scheduling, asked[0], current) if is_free(s)][:MAX_OPTIONS]
    else:
        # Scenario 2: no time given; offer a spread across the day and the week.
        free = spread_slots(all_window_slots(scheduling, current), is_free)
    free.sort(key=lambda s: s["start"])
    (out_dir / "calendar-candidate-slots.json").write_text(json.dumps(free), encoding="utf-8")
    labelled: list[dict[str, Any]] = []
    if free:
        options = appointment_options.build_options(receipt, free, scheduling["timezone"], days_ahead, now=current)
        calendar_query.write_private(out_dir / "calendar-options.json", options)
        labelled = options["options"]
    return {
        "mode": mode,
        "options": labelled,
        "requested_slot": asked[0] if asked else None,
        "reason": "" if labelled else "no free slot inside the declared windows",
    }
