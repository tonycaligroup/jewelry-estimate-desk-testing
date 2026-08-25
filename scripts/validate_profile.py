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
VALID_MODES = {"retailer", "wholesale_middle_man", "both"}


def _read_path(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def validate_profile(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["profile must be a JSON object"]

    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    mode = _read_path(data, "shop.mode")
    if mode not in VALID_MODES:
        errors.append("shop.mode must be retailer, wholesale_middle_man, or both")

    for path in ("shop.approver_email", "shop.outbound_mailbox"):
        value = _read_path(data, path)
        if not isinstance(value, str) or not EMAIL_RE.fullmatch(value):
            errors.append(f"{path} must be a valid email address")

    stage = _read_path(data, "autonomy.trust_stage")
    if type(stage) is not int or stage not in {1, 2, 3}:
        errors.append("autonomy.trust_stage must be 1, 2, or 3")

    multiplier = _read_path(data, "pricing.markup_multiplier")
    if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
        errors.append("pricing.markup_multiplier must be a number greater than 1.0")
    elif multiplier <= 1:
        errors.append("pricing.markup_multiplier must be greater than 1.0")

    timezone = _read_path(data, "scheduling.timezone")
    if not isinstance(timezone, str) or not timezone:
        errors.append("scheduling.timezone must be a valid IANA timezone")
    else:
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            errors.append("scheduling.timezone must be a valid IANA timezone")

    return errors


def load_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc
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
        print(json.dumps({"ready": False, "errors": [str(exc)]}))
        return 2

    errors = validate_profile(data)
    print(json.dumps({"ready": not errors, "errors": errors}, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
