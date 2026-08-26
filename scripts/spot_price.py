#!/usr/bin/env python3
"""Fetch and cache owner-only precious-metal spot prices."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


FREQUENCY_SECONDS = {"per_estimate": 0, "daily": 86400, "weekly": 604800}
METAL_KEYS = {
    "gold": ("xau", "XAU"),
    "silver": ("xag", "XAG"),
    "platinum": ("xpt", "XPT"),
    "palladium": ("xpd", "XPD"),
}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _request_json(
    url: str, opener: Callable[..., Any] = urllib.request.urlopen
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("spot-price provider returned a non-object response")
    return value


def fetch_prices(
    provider: str,
    metals: list[str],
    currency: str = "USD",
    unit: str = "gram",
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not metals or any(metal not in METAL_KEYS for metal in metals):
        raise ValueError("metals must contain supported precious-metal names")
    if currency != "USD":
        raise ValueError("this skill currently supports USD spot pricing only")
    if unit not in {"gram", "troy_oz"}:
        raise ValueError("unit must be gram or troy_oz")
    prices: dict[str, float] = {}
    provider_timestamp: Any = None
    if provider == "stackerscan":
        query = urllib.parse.urlencode({"base": currency, "unit": unit})
        data = _request_json(
            f"https://www.stackerscan.com/api/premiums/metal-prices?{query}", opener
        )
        if (
            data.get("success") is not True
            or data.get("base") != currency
            or data.get("unit") != unit
        ):
            raise ValueError("StackerScan response metadata does not match the request")
        raw_metals = data.get("metals")
        if not isinstance(raw_metals, dict):
            raise ValueError("StackerScan response lacks metals")
        for metal in metals:
            quote = raw_metals.get(METAL_KEYS[metal][0])
            price = quote.get("close") if isinstance(quote, dict) else None
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or price <= 0
            ):
                raise ValueError(f"StackerScan response lacks a valid {metal} close")
            prices[metal] = float(price)
        provider_timestamp = data.get("timestamp")
    elif provider == "gold-api":
        if unit != "troy_oz":
            raise ValueError("gold-api provider supports troy_oz in this skill")
        for metal in metals:
            symbol = METAL_KEYS[metal][1]
            data = _request_json(f"https://api.gold-api.com/price/{symbol}", opener)
            price = data.get("price")
            if data.get("symbol") != symbol or data.get("currency") != currency:
                raise ValueError(
                    f"gold-api response metadata does not match {symbol}/{currency}"
                )
            if (
                isinstance(price, bool)
                or not isinstance(price, (int, float))
                or price <= 0
            ):
                raise ValueError(f"gold-api response lacks a valid {metal} price")
            prices[metal] = float(price)
            provider_timestamp = data.get("updatedAt")
    else:
        raise ValueError("provider must be stackerscan or gold-api")
    return {
        "schema_version": 1,
        "provider": provider,
        "currency": currency,
        "unit": unit,
        "prices": prices,
        "provider_timestamp": provider_timestamp,
        "fetched_at_epoch": int(time.time()),
    }


def get_prices(
    cache_path: Path,
    provider: str,
    frequency: str,
    metals: list[str],
    currency: str = "USD",
    unit: str = "gram",
    now_epoch: int | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if frequency not in FREQUENCY_SECONDS:
        raise ValueError("frequency must be per_estimate, daily, or weekly")
    now = int(time.time()) if now_epoch is None else now_epoch
    if frequency != "per_estimate" and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            isinstance(cached, dict)
            and cached.get("provider") == provider
            and cached.get("currency") == currency
            and cached.get("unit") == unit
            and set(cached.get("prices", {})) >= set(metals)
            and type(cached.get("fetched_at_epoch")) is int
            and 0 <= now - cached["fetched_at_epoch"] < FREQUENCY_SECONDS[frequency]
        ):
            return cached
    result = fetch_prices(provider, metals, currency, unit, opener)
    result["fetched_at_epoch"] = now
    atomic_write(cache_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--provider", choices=("stackerscan", "gold-api"), required=True
    )
    parser.add_argument("--frequency", choices=tuple(FREQUENCY_SECONDS), required=True)
    parser.add_argument("--metal", action="append", dest="metals", required=True)
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--unit", choices=("gram", "troy_oz"), default="gram")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        value = get_prices(
            args.cache,
            args.provider,
            args.frequency,
            args.metals,
            args.currency,
            args.unit,
        )
        if args.output:
            atomic_write(args.output, value)
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
