#!/usr/bin/env python3
"""Validate whether a Gmail route belongs to one durable Kolo estimate record."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import inbox_claim


ESTIMATE_ID_RE = re.compile(r"^jed-[0-9a-f]{16}$")
VALID_STATUSES = {
    "awaiting_specs",
    "pending_approval",
    "estimate_sent",
    "appointment_booked",
    "approved",
    "declined",
    "manual_review",
    "dormant",
}


def require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be non-empty text")
    return value


def validate_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("estimate record must use schema_version 1")
    estimate_id = require_text(record.get("estimate_id"), "estimate_id")
    if not ESTIMATE_ID_RE.fullmatch(estimate_id):
        raise ValueError("estimate record has invalid estimate_id")
    if record.get("status") not in VALID_STATUSES:
        raise ValueError("estimate record has invalid status")
    route = record.get("route")
    if not isinstance(route, dict):
        raise ValueError("estimate record route must be an object")
    require_text(route.get("thread_id"), "record.route.thread_id")
    require_text(route.get("identity_key"), "record.route.identity_key")
    require_text(route.get("gmail_message_id"), "record.route.gmail_message_id")
    return record


def decide(route: Any, records: Any, claim_root: Path) -> dict[str, str]:
    if not isinstance(route, dict):
        raise ValueError("route must be a JSON object")
    thread_id = require_text(route.get("thread_id"), "route.thread_id")
    identity_key = require_text(route.get("identity_key"), "route.identity_key")
    if not isinstance(records, list):
        raise ValueError("records must be a JSON array")
    try:
        valid_records = [validate_record(record) for record in records]
    except ValueError as exc:
        return {"decision": "manual_review", "reason_code": "invalid_ownership_record", "detail": str(exc)}

    matches = [record for record in valid_records if record["route"]["thread_id"] == thread_id]
    if not matches:
        return {"decision": "unowned", "reason_code": "no_thread_record"}
    if len(matches) != 1:
        return {"decision": "manual_review", "reason_code": "ambiguous_thread_ownership"}

    record = matches[0]
    if record["route"]["identity_key"] != identity_key:
        return {"decision": "manual_review", "reason_code": "identity_mismatch"}
    initiating_id = record["route"]["gmail_message_id"]
    try:
        claim = inbox_claim.read_state(inbox_claim.claim_path(claim_root, initiating_id))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"decision": "manual_review", "reason_code": "missing_initiating_claim"}
    if claim.get("message_id_sha256") != inbox_claim.claim_key(initiating_id):
        return {"decision": "manual_review", "reason_code": "initiating_claim_mismatch"}
    if claim.get("status") != "processed":
        return {"decision": "manual_review", "reason_code": "initiating_claim_not_processed"}
    if record["status"] in {"declined", "dormant", "manual_review"}:
        return {
            "decision": "owned_manual_review",
            "reason_code": "terminal_estimate_response",
            "estimate_id": record["estimate_id"],
        }
    return {
        "decision": "owned",
        "reason_code": "exact_route_and_claim_match",
        "estimate_id": record["estimate_id"],
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("route", type=Path)
    parser.add_argument("records", type=Path)
    parser.add_argument("--claim-root", type=Path, default=inbox_claim.default_claim_root())
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                decide(read_json(args.route), read_json(args.records), args.claim_root),
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
