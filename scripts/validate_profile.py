#!/usr/bin/env python3
"""Validate a Jewelry Estimate Desk runtime profile without third-party packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
VALID_MODES = {"retailer", "wholesale_middle_man", "both"}
VALID_PRICING_MODELS = {"cost_plus_multiplier", "target_margin"}
VALID_OWNER_CHANNELS = {"kolo_chat", "email", "sms"}


def _read_path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_profile(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"errors": ["profile must be a JSON object"], "missing_fields": []}

    errors: list[str] = []
    missing_fields: list[str] = []

    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    mode = _read_path(data, "shop.mode")
    if mode not in VALID_MODES:
        errors.append("shop.mode must be retailer, wholesale_middle_man, or both")

    shop_name = _read_path(data, "shop.name")
    if not isinstance(shop_name, str) or not shop_name.strip():
        errors.append("shop.name is required")

    for path in ("shop.outbound_mailbox",):
        value = _read_path(data, path)
        if not isinstance(value, str) or not EMAIL_RE.fullmatch(value):
            errors.append(f"{path} must be a valid email address")

    # Business address — required for calendar invites
    address = _read_path(data, "shop.address")
    if not isinstance(address, dict):
        errors.append(
            "shop.address is required (business address for calendar invites)"
        )
    else:
        for field in ("street", "city", "state", "zip"):
            value = address.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"shop.address.{field} is required")

    # Business website — optional but recommended
    website = _read_path(data, "shop.website")
    if not isinstance(website, str) or not website.strip():
        missing_fields.append("shop.website")

    stage = _read_path(data, "autonomy.trust_stage")
    if type(stage) is not int or stage not in {1, 2, 3}:
        errors.append("autonomy.trust_stage must be 1, 2, or 3")

    pricing_model = _read_path(data, "pricing.model")
    if pricing_model not in VALID_PRICING_MODELS:
        errors.append("pricing.model must be cost_plus_multiplier or target_margin")
    if pricing_model == "cost_plus_multiplier":
        multiplier = _read_path(data, "pricing.markup_multiplier")
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            errors.append("pricing.markup_multiplier must be a number greater than 1.0")
        elif multiplier <= 1:
            errors.append("pricing.markup_multiplier must be greater than 1.0")
    if pricing_model == "target_margin":
        margin = _read_path(data, "pricing.target_margin")
        if (
            isinstance(margin, bool)
            or not isinstance(margin, (int, float))
            or not 0 < margin < 1
        ):
            errors.append(
                "pricing.target_margin must be a decimal greater than 0 and less than 1"
            )

    spot = _read_path(data, "pricing.spot_metal")
    if not isinstance(spot, dict):
        errors.append("pricing.spot_metal is required")
    elif type(spot.get("enabled")) is not bool:
        errors.append("pricing.spot_metal.enabled must be true or false")
    elif spot["enabled"] is True:
        if spot.get("provider") not in {"stackerscan", "gold-api"}:
            errors.append("pricing.spot_metal.provider must be stackerscan or gold-api")
        if spot.get("refresh_frequency") not in {"per_estimate", "daily", "weekly"}:
            errors.append(
                "pricing.spot_metal.refresh_frequency must be per_estimate, daily, or weekly"
            )
        if spot.get("currency") != "USD":
            errors.append("pricing.spot_metal.currency must currently be USD")
        expected_unit = (
            "troy_oz" if spot.get("provider") == "gold-api" else {"gram", "troy_oz"}
        )
        if isinstance(expected_unit, set):
            if spot.get("unit") not in expected_unit:
                errors.append("pricing.spot_metal.unit must be gram or troy_oz")
        elif spot.get("unit") != expected_unit:
            errors.append(
                "gold-api spot pricing requires pricing.spot_metal.unit troy_oz"
            )

    requested_channel = _read_path(data, "owner_notifications.requested_channel")
    if requested_channel not in VALID_OWNER_CHANNELS:
        errors.append(
            "owner_notifications.requested_channel must be kolo_chat, email, or sms"
        )
    active_channel = _read_path(data, "owner_notifications.active_channel")
    if active_channel != "kolo_chat":
        errors.append(
            "owner_notifications.active_channel must be kolo_chat in this release"
        )
    inactive_reason = _read_path(data, "owner_notifications.inactive_reason")
    expected_reason = (
        None
        if requested_channel == "kolo_chat"
        else f"{requested_channel}_not_supported"
    )
    if inactive_reason != expected_reason:
        errors.append(
            "owner_notifications.inactive_reason does not match the requested channel"
        )
    if _read_path(data, "owner_notifications.email_verified") is not False:
        errors.append(
            "owner_notifications.email_verified must be false in this release"
        )
    if _read_path(data, "owner_notifications.sms_verified") is not False:
        errors.append("owner_notifications.sms_verified must be false in this release")
    if requested_channel == "email":
        destination = _read_path(data, "owner_notifications.email")
        if not isinstance(destination, str) or not EMAIL_RE.fullmatch(destination):
            errors.append(
                "owner_notifications.email must be valid when email is requested"
            )
    if requested_channel == "sms":
        destination = _read_path(data, "owner_notifications.sms")
        if not isinstance(destination, str) or not PHONE_RE.fullmatch(destination):
            errors.append("owner_notifications.sms must be E.164 when sms is requested")

    timezone = _read_path(data, "scheduling.timezone")
    if not isinstance(timezone, str) or not timezone:
        errors.append("scheduling.timezone must be a valid IANA timezone")
    else:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            errors.append("scheduling.timezone must be a valid IANA timezone")

    window_days = _read_path(data, "scheduling.meeting_offer_window_days")
    if type(window_days) is not int or not 1 <= window_days <= 30:
        errors.append(
            "scheduling.meeting_offer_window_days must be an integer from 1 to 30"
        )

    return {"errors": errors, "missing_fields": missing_fields, "ready": not errors}


def load_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError("profile must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    args = parser.parse_args(argv)

    try:
        data = load_profile(args.profile)
    except ValueError as exc:
        print(json.dumps({"ready": False, "errors": [str(exc)], "missing_fields": []}))
        return 2

    result = validate_profile(data)
    errors = result["errors"]
    missing_fields = result["missing_fields"]
    ready = not errors

    output = {"ready": ready, "errors": errors, "missing_fields": missing_fields}
    print(json.dumps(output, sort_keys=True))
    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
