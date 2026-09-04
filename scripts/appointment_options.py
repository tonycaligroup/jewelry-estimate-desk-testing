#!/usr/bin/env python3
"""Validate provider-backed free slots and derive exact local labels."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import calendar_query


def build_options(
    receipt: Any,
    slots: Any,
    timezone_name: str,
    window_days: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        raise ValueError("calendar receipt must use schema_version 1")
    if receipt.get("provider") != "google_calendar_freebusy":
        raise ValueError("calendar receipt has an unsupported provider")
    if not calendar_query.REQUEST_ID_RE.fullmatch(
        receipt.get("provider_request_id", "")
    ):
        raise ValueError("calendar receipt lacks a valid provider request ID")
    response_body = receipt.get("response_body")
    if receipt.get("response_body_sha256") != calendar_query.canonical_hash(
        response_body
    ):
        raise ValueError("calendar receipt response hash does not match")
    query = receipt.get("query")
    if not isinstance(query, dict) or query.get("timeZone") != timezone_name:
        raise ValueError("calendar receipt timezone does not match")
    if type(window_days) is not int or not 1 <= window_days <= 30:
        raise ValueError("window_days must be an integer from 1 to 30")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    current = datetime.now(zone) if now is None else now.astimezone(zone)
    try:
        checked = parsedate_to_datetime(receipt["response_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("calendar receipt response_date is invalid") from exc
    if abs((current - checked.astimezone(zone)).total_seconds()) > 300:
        raise ValueError("calendar availability is older than five minutes")
    if not isinstance(slots, list) or not 1 <= len(slots) <= 3:
        raise ValueError("slots must contain one to three live options")
    query_start = calendar_query.parse_timestamp(query.get("timeMin"), "query.timeMin")
    query_end = calendar_query.parse_timestamp(query.get("timeMax"), "query.timeMax")
    if (
        not isinstance(response_body, dict)
        or response_body.get("kind") != "calendar#freeBusy"
    ):
        raise ValueError("calendar receipt contains an invalid response kind")
    if (
        calendar_query.parse_timestamp(response_body.get("timeMin"), "response.timeMin")
        != query_start
    ):
        raise ValueError("calendar receipt response timeMin does not match its query")
    if (
        calendar_query.parse_timestamp(response_body.get("timeMax"), "response.timeMax")
        != query_end
    ):
        raise ValueError("calendar receipt response timeMax does not match its query")
    items = query.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise ValueError("calendar receipt query must name exactly one calendar")
    calendar_id = items[0].get("id")
    calendars = (
        response_body.get("calendars") if isinstance(response_body, dict) else None
    )
    calendar = calendars.get(calendar_id) if isinstance(calendars, dict) else None
    calendar_query.require_readable_calendar(calendar)
    busy = calendar["busy"]
    busy_ranges = []
    for item in busy:
        if not isinstance(item, dict):
            raise ValueError("calendar receipt contains an invalid busy range")
        busy_start = calendar_query.parse_timestamp(item.get("start"), "busy.start")
        busy_end = calendar_query.parse_timestamp(item.get("end"), "busy.end")
        if busy_end <= busy_start:
            raise ValueError("calendar receipt contains an invalid busy range")
        busy_ranges.append((busy_start, busy_end))
    output = []
    seen = set()
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict) or set(slot) != {"start", "end"}:
            raise ValueError(f"slots[{index}] must contain only start and end")
        start = calendar_query.parse_timestamp(slot["start"], f"slots[{index}].start")
        end = calendar_query.parse_timestamp(slot["end"], f"slots[{index}].end")
        if end <= start:
            raise ValueError(f"slots[{index}] has invalid timestamps")
        if start < query_start or end > query_end:
            raise ValueError(f"slots[{index}] is outside the live calendar query")
        if any(
            start < busy_end and end > busy_start
            for busy_start, busy_end in busy_ranges
        ):
            raise ValueError(f"slots[{index}] overlaps live calendar busy time")
        local_start = start.astimezone(zone)
        if local_start < current or local_start > current + timedelta(days=window_days):
            raise ValueError(f"slots[{index}] is outside the near-term meeting window")
        key = (start.isoformat(), end.isoformat())
        if key in seen:
            raise ValueError("slots must not contain duplicates")
        seen.add(key)
        label = f"{local_start.strftime('%A, %B')} {local_start.day} at {local_start.strftime('%-I:%M %p')} {local_start.tzname()}"
        output.append(
            {"start": start.isoformat(), "end": end.isoformat(), "label": label}
        )
    return {
        "schema_version": 1,
        "timezone": timezone_name,
        "calendar_checked_at": checked.isoformat(),
        "provider_request_id": receipt["provider_request_id"],
        "response_body_sha256": receipt["response_body_sha256"],
        "meeting_offer_window_days": window_days,
        "options": output,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=Path)
    parser.add_argument("slots", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--window-days", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        value = build_options(
            json.loads(args.receipt.read_text(encoding="utf-8")),
            json.loads(args.slots.read_text(encoding="utf-8")),
            args.timezone,
            args.window_days,
        )
        args.output.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        args.output.chmod(0o600)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
