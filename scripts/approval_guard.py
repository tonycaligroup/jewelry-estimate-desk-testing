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


BINDING_FIELDS = (
    "estimate_id",
    "route",
    "specification",
    "proposed_price",
    "internal_cost_sheet",
)
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
        raise ValueError(
            "estimate_id must match jed- followed by 16 lowercase hex characters"
        )
    proposed_price = state["proposed_price"]
    if isinstance(proposed_price, bool) or not isinstance(proposed_price, (int, float)):
        raise ValueError("proposed_price must be numeric")
    validate_internal_cost_sheet(state["internal_cost_sheet"], proposed_price)
    return {field: state[field] for field in BINDING_FIELDS}


def _money(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return float(value)


def validate_internal_cost_sheet(value: Any, proposed_price: float) -> dict[str, Any]:
    """Validate the owner-only cost sheet that is bound to approval."""
    if not isinstance(value, dict):
        raise ValueError("internal_cost_sheet must be an object")
    expected = {
        "metal_lines",
        "stone_lines",
        "labor_lines",
        "other_hard_cost_lines",
        "hard_cost_total",
        "customer_price",
    }
    if set(value) != expected:
        raise ValueError("internal_cost_sheet contains missing or unsupported fields")
    line_specs = {
        "metal_lines": ("metal", "quantity_grams", "unit_cost", "total_cost"),
        "stone_lines": ("stone", "quantity", "unit_cost", "total_cost"),
        "labor_lines": ("task", "hours", "rate", "total_cost"),
        "other_hard_cost_lines": ("label", "total_cost"),
    }
    calculated = 0.0
    for group, fields in line_specs.items():
        lines = value[group]
        if not isinstance(lines, list):
            raise ValueError(f"internal_cost_sheet.{group} must be an array")
        for index, line in enumerate(lines):
            if not isinstance(line, dict) or set(line) != set(fields):
                raise ValueError(
                    f"internal_cost_sheet.{group}[{index}] contains missing or unsupported fields"
                )
            label = line[fields[0]]
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"internal_cost_sheet.{group}[{index}].{fields[0]} must be text"
                )
            for field in fields[1:]:
                _money(line[field], f"internal_cost_sheet.{group}[{index}].{field}")
            calculated += float(line["total_cost"])
    hard_cost_total = _money(
        value["hard_cost_total"], "internal_cost_sheet.hard_cost_total"
    )
    customer_price = _money(
        value["customer_price"], "internal_cost_sheet.customer_price"
    )
    if abs(calculated - hard_cost_total) > 0.01:
        raise ValueError(
            "internal_cost_sheet.hard_cost_total does not equal its cost lines"
        )
    if abs(customer_price - float(proposed_price)) > 0.01:
        raise ValueError("internal_cost_sheet.customer_price must equal proposed_price")
    return value


def binding_hash(state: dict[str, Any]) -> str:
    encoded = canonical_json(binding_payload(state)).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def new_estimate_id() -> str:
    return "jed-" + secrets.token_hex(8)


def build_request(state: dict[str, Any]) -> dict[str, Any]:
    binding_payload(state)
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
        errors.append(
            "route, specification, proposed price, or internal cost sheet changed after approval"
        )
    if approved.get("estimate_id") != current.get("estimate_id"):
        errors.append("estimate_id changed after approval")
    proposed_price = current.get("proposed_price")
    if (
        not isinstance(price, bool)
        and isinstance(price, (int, float))
        and not isinstance(proposed_price, bool)
        and isinstance(proposed_price, (int, float))
        and abs(float(price) - float(proposed_price)) > 0.01
    ):
        errors.append("owner_approved_price does not match the bound proposed_price")
    return not errors, errors


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
        valid, errors = verify_execution(
            read_object(args.approved), read_object(args.current)
        )
        print(json.dumps({"valid": valid, "errors": errors}, sort_keys=True))
        return 0 if valid else 3
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
