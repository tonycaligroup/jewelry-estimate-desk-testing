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
COST_COMPONENT_FIELDS = {
    "metal_lines",
    "stone_lines",
    "labor_lines",
    "other_hard_cost_lines",
}


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


def build_internal_cost_sheet(
    components: Any, proposed_price: float
) -> dict[str, Any]:
    """Build the exact approval schema from model-authored cost components."""
    if not isinstance(components, dict) or set(components) != COST_COMPONENT_FIELDS:
        raise ValueError("cost_components contains missing or unsupported fields")
    line_specs = {
        "metal_lines": ("metal", "quantity_grams", "unit_cost"),
        "stone_lines": ("stone", "quantity", "unit_cost"),
        "labor_lines": ("task", "hours", "rate"),
        "other_hard_cost_lines": ("label", "total_cost"),
    }
    optional_fields = {
        "metal_lines": ("rate_key", "spot_price_per_gram", "purity"),
        "stone_lines": ("rate_key",),
        "labor_lines": (),
        "other_hard_cost_lines": ("rate_key",),
    }
    result: dict[str, Any] = {}
    hard_cost_total = 0.0
    for group, fields in line_specs.items():
        lines = components[group]
        if not isinstance(lines, list):
            raise ValueError(f"cost_components.{group} must be an array")
        built_lines: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            allowed = set(fields) | set(optional_fields[group])
            if (
                not isinstance(line, dict)
                or not set(fields) <= set(line)
                or not set(line) <= allowed
            ):
                raise ValueError(
                    f"cost_components.{group}[{index}] contains missing or unsupported fields"
                )
            label = line[fields[0]]
            if not isinstance(label, str) or not label.strip():
                raise ValueError(
                    f"cost_components.{group}[{index}].{fields[0]} must be text"
                )
            built = dict(line)
            if group == "other_hard_cost_lines":
                total = _money(
                    line["total_cost"],
                    f"cost_components.{group}[{index}].total_cost",
                )
            else:
                quantity_field, unit_field = fields[1], fields[2]
                quantity = _money(
                    line[quantity_field],
                    f"cost_components.{group}[{index}].{quantity_field}",
                )
                unit_cost = _money(
                    line[unit_field],
                    f"cost_components.{group}[{index}].{unit_field}",
                )
                total = round(quantity * unit_cost, 2)
                built["total_cost"] = total
            hard_cost_total = round(hard_cost_total + total, 2)
            built_lines.append(built)
        result[group] = built_lines
    result["hard_cost_total"] = hard_cost_total
    result["customer_price"] = _money(proposed_price, "proposed_price")
    validate_internal_cost_sheet(result, proposed_price)
    return result


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
    # Provenance fields are carried through so the rate a line claims to have
    # used is bound with the approval and can be re-checked afterwards.
    provenance_fields = {
        "metal_lines": ("rate_key", "spot_price_per_gram", "purity"),
        "stone_lines": ("rate_key",),
        "labor_lines": (),
        "other_hard_cost_lines": ("rate_key",),
    }
    calculated = 0.0
    for group, fields in line_specs.items():
        lines = value[group]
        if not isinstance(lines, list):
            raise ValueError(f"internal_cost_sheet.{group} must be an array")
        for index, line in enumerate(lines):
            allowed = set(fields) | set(provenance_fields[group])
            if (
                not isinstance(line, dict)
                or not set(fields) <= set(line)
                or not set(line) <= allowed
            ):
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
            for field in ("spot_price_per_gram", "purity"):
                if field in line:
                    _money(line[field], f"internal_cost_sheet.{group}[{index}].{field}")
            if "rate_key" in line and (
                not isinstance(line["rate_key"], str) or not line["rate_key"].strip()
            ):
                raise ValueError(
                    f"internal_cost_sheet.{group}[{index}].rate_key must be text"
                )
            # A hand-authored sheet must not state a line total that its own
            # quantity and unit cost do not produce. Without this the owner can
            # approve, and the binding can lock in, a breakdown whose lines do
            # not multiply out.
            if len(fields) == 4:
                quantity_field, unit_field = fields[1], fields[2]
                expected_total = round(
                    float(line[quantity_field]) * float(line[unit_field]), 2
                )
                if abs(float(line["total_cost"]) - expected_total) > 0.01:
                    raise ValueError(
                        f"internal_cost_sheet.{group}[{index}].total_cost does not "
                        f"equal {quantity_field} times {unit_field}"
                    )
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


def owner_review(state: dict[str, Any]) -> dict[str, Any]:
    """Build the explicit jeweler-only cost view shown with an approval."""
    payload = binding_payload(state)
    sheet = payload["internal_cost_sheet"]
    hard_cost = float(sheet["hard_cost_total"])
    customer_price = float(sheet["customer_price"])
    return {
        "visibility": "jeweler_only_never_customer_facing",
        "specification": payload["specification"],
        "metal_costs": sheet["metal_lines"],
        "stone_costs": sheet["stone_lines"],
        "labor_costs": sheet["labor_lines"],
        "other_hard_costs": sheet["other_hard_cost_lines"],
        "hard_cost_total": hard_cost,
        "customer_price": customer_price,
        "estimated_gross_profit": round(customer_price - hard_cost, 2),
        "cost_and_labor_assumptions": (
            "Quantities, unit costs, labor hours, and labor rates shown above are "
            "the jeweler's approval assumptions."
        ),
    }


def new_estimate_id() -> str:
    return "jed-" + secrets.token_hex(8)


def build_request(state: dict[str, Any]) -> dict[str, Any]:
    binding_payload(state)
    result = dict(state)
    result["binding_hash"] = binding_hash(state)
    result["owner_review"] = owner_review(state)
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
