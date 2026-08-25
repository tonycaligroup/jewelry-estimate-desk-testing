#!/usr/bin/env python3
"""Derive a customer route from an inbound Gmail message without using names."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from email.utils import getaddresses
from pathlib import Path
from typing import Any


MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+$")


def normalize_address(value: str, field: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field} must not contain line breaks")
    parsed = [(name, address) for name, address in getaddresses([value]) if address]
    if len(parsed) != 1 or not EMAIL_RE.fullmatch(parsed[0][1]):
        raise ValueError(f"{field} must contain exactly one email address")
    return parsed[0][1].lower()


def email_identity_key(address: str) -> str:
    normalized = normalize_address(address, "email identity")
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
            values.append(value)
    return values


def require_one_header(message: dict[str, Any], name: str) -> str:
    values = header_values(message, name)
    if len(values) != 1 or not values[0].strip():
        raise ValueError(f"Gmail message must contain exactly one {name} header")
    return values[0].strip()


def build_route(message: dict[str, Any], outbound_mailbox: str) -> dict[str, Any]:
    gmail_message_id = message.get("id")
    thread_id = message.get("threadId")
    if not isinstance(gmail_message_id, str) or not gmail_message_id.strip():
        raise ValueError("Gmail message id must be a non-empty string")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("Gmail threadId must be a non-empty string")

    mailbox = normalize_address(outbound_mailbox, "outbound mailbox")
    recipient = normalize_address(require_one_header(message, "From"), "Gmail From header")
    if recipient == mailbox:
        raise ValueError("message is outbound from the shop mailbox, not a customer reply")

    original_message_id = require_one_header(message, "Message-ID")
    if not re.fullmatch(r"<[^<>\s]+>", original_message_id):
        raise ValueError("Gmail Message-ID header must be one RFC Message-ID")
    subject = require_one_header(message, "Subject")
    if "\r" in subject or "\n" in subject:
        raise ValueError("Gmail Subject header must not contain line breaks")

    references: list[str] = []
    for value in header_values(message, "References"):
        for message_id in MESSAGE_ID_RE.findall(value):
            if message_id not in references:
                references.append(message_id)

    return {
        "channel": "gmail",
        "mailbox": mailbox,
        "recipient": recipient,
        "identity_key": email_identity_key(recipient),
        "gmail_message_id": gmail_message_id.strip(),
        "thread_id": thread_id.strip(),
        "original_message_id": original_message_id,
        "original_subject": subject,
        "references": references,
    }


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", type=Path, help="full Gmail message resource JSON")
    parser.add_argument("outbound_mailbox")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        write_object(
            args.output, build_route(read_object(args.message), args.outbound_mailbox)
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
