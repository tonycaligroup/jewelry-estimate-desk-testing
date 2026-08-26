#!/usr/bin/env python3
"""Apply the configured shop pricing model deterministically."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


def quote_price(hard_cost_total: Any, pricing: Any) -> float:
    if (
        isinstance(hard_cost_total, bool)
        or not isinstance(hard_cost_total, (int, float))
        or hard_cost_total < 0
    ):
        raise ValueError("hard_cost_total must be a non-negative number")
    if not isinstance(pricing, dict):
        raise ValueError("pricing must be an object")
    cost = Decimal(str(hard_cost_total))
    model = pricing.get("model")
    if model == "cost_plus_multiplier":
        value = pricing.get("markup_multiplier")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 1:
            raise ValueError("markup_multiplier must be greater than 1")
        quote = cost * Decimal(str(value))
    elif model == "target_margin":
        value = pricing.get("target_margin")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < value < 1
        ):
            raise ValueError("target_margin must be a decimal between 0 and 1")
        quote = cost / (Decimal("1") - Decimal(str(value)))
    else:
        raise ValueError("pricing.model must be cost_plus_multiplier or target_margin")
    return float(quote.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hard-cost-total", type=float, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        price = quote_price(args.hard_cost_total, profile.get("pricing"))
        print(json.dumps({"customer_price": price}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
