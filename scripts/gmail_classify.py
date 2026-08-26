#!/usr/bin/env python3
"""Conservatively classify deterministic Gmail system messages from headers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from email.utils import getaddresses
from pathlib import Path
from typing import Any


EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+$")
AUTO_REPLY_SUBJECT_RE = re.compile(
    r"^\s*(automatic reply|auto reply|out of office|away from the office)\s*:?",
    re.IGNORECASE,
)


def header_values(message: dict[str, Any], name: str) -> list[str]:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Gmail message payload must be an object")
    headers = payload.get("headers")
    if not isinstance(headers, list):
        raise ValueError("Gmail message payload.headers must be an array")
    values: list[str] = []
    for header in headers:
        if not isinstance(header, dict):
            raise ValueError("each Gmail header must be an object")
        if str(header.get("name", "")).lower() == name.lower():
            value = header.get("value")
            if not isinstance(value, str):
                raise ValueError(f"Gmail {name} header must be text")
            values.append(value.strip())
    return values


def one_header(message: dict[str, Any], name: str, required: bool = False) -> str:
    values = header_values(message, name)
    if len(values) > 1:
        raise ValueError(f"Gmail message contains multiple {name} headers")
    if not values:
        if required:
            raise ValueError(f"Gmail message is missing {name} header")
        return ""
    return values[0]


def sender_address(message: dict[str, Any]) -> str:
    value = one_header(message, "From", required=True)
    parsed = [(name, address) for name, address in getaddresses([value]) if address]
    if len(parsed) != 1 or not EMAIL_RE.fullmatch(parsed[0][1]):
        raise ValueError("Gmail From header must contain exactly one email address")
    return parsed[0][1].lower()


def classify(message: dict[str, Any]) -> dict[str, str]:
    sender = sender_address(message)
    subject = one_header(message, "Subject")
    auto_submitted = one_header(message, "Auto-Submitted").lower()
    content_type = one_header(message, "Content-Type").lower()
    sender_local = sender.split("@", 1)[0]
    dsn_sender = sender_local in {"mailer-daemon", "postmaster"}
    dsn_content = "report-type=delivery-status" in content_type
    dsn_subject = any(
        phrase in subject.lower()
        for phrase in ("delivery status notification", "undeliverable", "delivery failure")
    )
    if dsn_sender and (dsn_content or dsn_subject or auto_submitted == "auto-generated"):
        return {"classification": "dsn_candidate", "reason_code": "delivery_status_headers"}

    auto_header = auto_submitted not in {"", "no"}
    vendor_auto_header = bool(
        header_values(message, "X-Autoreply")
        or header_values(message, "X-Autorespond")
        or header_values(message, "X-Auto-Response-Suppress")
    )
    if auto_header or vendor_auto_header or AUTO_REPLY_SUBJECT_RE.match(subject):
        return {"classification": "auto_reply", "reason_code": "automatic_reply_headers"}

    return {"classification": "customer_or_uncertain", "reason_code": "requires_routing"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", type=Path)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.message.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Gmail message must be a JSON object")
        print(json.dumps(classify(value), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
