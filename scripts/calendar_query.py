#!/usr/bin/env python3
"""Query Google Calendar free/busy and persist privacy-minimal provider evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FREEBUSY_URL = "https://gateway.maton.ai/google-calendar/calendar/v3/freeBusy"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def write_private(path: Path, value: dict[str, Any]) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def query_freebusy(
    time_min: str,
    time_max: str,
    timezone_name: str,
    calendar_id: str,
    token: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    start = parse_timestamp(time_min, "time_min")
    end = parse_timestamp(time_max, "time_max")
    if end <= start:
        raise ValueError("time_max must be after time_min")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    if not isinstance(calendar_id, str) or not calendar_id or len(calendar_id) > 255:
        raise ValueError("calendar_id must contain 1-255 characters")
    if not token or any(character in token for character in "\r\n"):
        raise ValueError("MATON_API_KEY is missing or invalid")
    query = {
        "timeMin": time_min,
        "timeMax": time_max,
        "timeZone": timezone_name,
        "items": [{"id": calendar_id}],
    }
    request = urllib.request.Request(
        FREEBUSY_URL,
        data=json.dumps(query, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with opener(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
        request_id = response.headers.get("x-request-id")
        response_date = response.headers.get("date")
    if not isinstance(body, dict) or body.get("kind") != "calendar#freeBusy":
        raise ValueError("calendar provider returned an invalid free/busy response")
    if not REQUEST_ID_RE.fullmatch(request_id or ""):
        raise ValueError("calendar response lacks a valid x-request-id")
    try:
        parsedate_to_datetime(response_date or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("calendar response lacks a valid Date header") from exc
    if parse_timestamp(body.get("timeMin"), "response.timeMin") != start:
        raise ValueError("calendar response timeMin does not match the query")
    if parse_timestamp(body.get("timeMax"), "response.timeMax") != end:
        raise ValueError("calendar response timeMax does not match the query")
    calendars = body.get("calendars")
    calendar = calendars.get(calendar_id) if isinstance(calendars, dict) else None
    if not isinstance(calendar, dict) or not isinstance(calendar.get("busy"), list):
        raise ValueError("calendar response lacks the requested calendar busy array")
    return {
        "schema_version": 1,
        "provider": "google_calendar_freebusy",
        "provider_request_id": request_id,
        "response_date": response_date,
        "query": query,
        "response_body_sha256": canonical_hash(body),
        "response_body": body,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--time-min", required=True)
    parser.add_argument("--time-max", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--calendar-id", default="primary")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = query_freebusy(
            args.time_min,
            args.time_max,
            args.timezone,
            args.calendar_id,
            os.environ.get("MATON_API_KEY", ""),
        )
        write_private(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
