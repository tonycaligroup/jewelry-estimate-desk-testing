#!/usr/bin/env python3
"""Create and verify immutable approval bindings for jewelry estimates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path
from typing import Any


BINDING_FIELDS = ("estimate_id", "route", "specification")
ESTIMATE_ID_RE = re.compile(r"^jed-[0-9a-f]{16}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def binding_payload(state: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in BINDING_FIELDS if field not in state]
    if missing:
        raise ValueError(f"missing approval binding fields: {', '.join(missing)}")
    if not isinstance(state["estimate_id"], str) or not ESTIMATE_ID_RE.fullmatch(
        state["estimate_id"]
    ):
        raise ValueError("estimate_id must match jed- followed by 16 lowercase hex characters")
    return {field: state[field] for field in BINDING_FIELDS}


def binding_hash(state: dict[str, Any]) -> str:
    encoded = canonical_json(binding_payload(state)).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def new_estimate_id() -> str:
    return "jed-" + secrets.token_hex(8)


def build_request(state: dict[str, Any]) -> dict[str, Any]:
    proposed_price = state.get("proposed_price")
    if isinstance(proposed_price, bool) or not isinstance(proposed_price, (int, float)):
        raise ValueError("proposed_price must be numeric")
    result = dict(state)
    result["binding_hash"] = binding_hash(state)
    return result


def verify_execution(
    approved: dict[str, Any], current: dict[str, Any]
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if approved.get("approval_status") != "approved":
        errors.append("approval_status is not approved")
    price = approved.get("owner_approved_price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        errors.append("owner_approved_price must be numeric")
    expected = approved.get("binding_hash")
    actual = binding_hash(current)
    if expected != actual:
        errors.append("recipient, route, or specification changed after approval")
    if approved.get("estimate_id") != current.get("estimate_id"):
        errors.append("estimate_id changed after approval")
    return not errors, errors


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("new-id")
    create = sub.add_parser("create")
    create.add_argument("state", type=Path)
    create.add_argument("output", type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("approved", type=Path)
    verify.add_argument("current", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "new-id":
            print(new_estimate_id())
            return 0
        if args.command == "create":
            write_object(args.output, build_request(read_object(args.state)))
            return 0
        valid, errors = verify_execution(read_object(args.approved), read_object(args.current))
        print(json.dumps({"valid": valid, "errors": errors}, sort_keys=True))
        return 0 if valid else 3
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
