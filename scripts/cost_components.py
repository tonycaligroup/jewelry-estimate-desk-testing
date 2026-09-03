#!/usr/bin/env python3
"""Prepare and finalize approval cost components deterministically.

Pricing was the one phase with no bundled helper: the model had to resolve
rate keys, fetch and attach spot evidence, compute unit costs with the
validators' rounding, and derive the customer price itself. Every mismatch
came back as a rejection, and the model burned the cron budget re-reading
script source and rewriting the sheet. This module closes that gap.

`prepare` reads the authoritative record's specification, the shop profile,
and (when spot pricing is enabled) the spot price evidence, and writes a
current-state skeleton in which every rate is resolved from the shop's card
and every unit cost is computed exactly as the approval validators compute
it. Only quantities are left for the model to fill. Anything it cannot
resolve is listed under `unresolved` with the candidate keys, so the model
escalates instead of inventing a rate.

`finalize` takes the filled skeleton, normalizes every rate from the card
again, derives the proposed price from the configured pricing model, and
writes the current-state file that `workflow_safe.py request-approval`
accepts unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import approval_guard
import estimate_record
import pricing_model
import route_ownership


SCHEMA_VERSION = 1
LINE_GROUPS = ("metal_lines", "stone_lines", "labor_lines", "other_hard_cost_lines")
LINE_FIELDS = {
    "metal_lines": {"metal", "rate_key", "quantity_grams", "unit_cost", "spot_price_per_gram", "purity"},
    "stone_lines": {"stone", "rate_key", "quantity", "unit_cost"},
    "labor_lines": {"task", "hours", "rate"},
    "other_hard_cost_lines": {"label", "rate_key", "total_cost"},
}
SPOT_METAL_WORDS = {
    "gold": "gold",
    "platinum": "platinum",
    "silver": "silver",
    "sterling": "silver",
    "palladium": "palladium",
}
STONE_WORDS = (
    "sapphire", "diamond", "ruby", "emerald", "moissanite", "aquamarine",
    "morganite", "tanzanite", "amethyst", "topaz", "garnet", "opal", "pearl",
    "tourmaline", "spinel", "peridot", "citrine",
)
# Checked in order: specific phrases first, and "natural" only when stated.
ORIGIN_TOKENS = {
    "labgrown": ("lab", "grown"),
    "lab-grown": ("lab", "grown"),
    "lab grown": ("lab", "grown"),
    "labcreated": ("lab", "grown"),
    "synthetic": ("lab", "grown"),
    "natural": ("natural",),
    "mined": ("natural",),
}
KARAT_RE = re.compile(r"\b(\d{1,2})\s*[kK]\b")


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a model-authored specification into (lowercase path, value)."""
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key, inner in value.items():
            path = f"{prefix}.{key}".lower() if prefix else str(key).lower()
            items.extend(_flatten(inner, path))
        return items
    if isinstance(value, list):
        items = []
        for index, inner in enumerate(value):
            items.extend(_flatten(inner, f"{prefix}[{index}]"))
        return items
    return [(prefix, value)]


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        if match:
            return float(match.group())
    return None


def extract_metal(specification: Any) -> dict[str, Any]:
    """Find the primary metal, its karat, and its purity in a specification."""
    flat = _flatten(specification)
    metal_words: list[str] = []
    karat: int | None = None
    color: str | None = None
    for path, value in flat:
        text = str(value).lower() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
        if "karat" in path or "purity" in path:
            number = _number(value)
            if number is not None and 1 <= number <= 24:
                karat = int(number)
        match = KARAT_RE.search(text)
        if match and karat is None:
            karat = int(match.group(1))
        if "metal" in path or any(word in text for word in SPOT_METAL_WORDS):
            for word, spot in SPOT_METAL_WORDS.items():
                if word in _tokens(text) and spot not in metal_words:
                    metal_words.append(spot)
            for shade in ("white", "yellow", "rose"):
                if shade in _tokens(text) and ("metal" in path or "color" in path):
                    color = shade
    metal = metal_words[0] if metal_words else None
    purity: float | None = None
    if metal == "gold" and karat:
        purity = round(karat / 24, 3)
    elif metal == "silver":
        purity = 0.925
    elif metal in {"platinum", "palladium"}:
        purity = 0.95
    description_parts = []
    if metal == "gold" and karat:
        description_parts.append(f"{karat}K")
    if color:
        description_parts.append(color)
    if metal:
        description_parts.append(metal)
    return {
        "metal": metal,
        "karat": karat,
        "color": color,
        "purity": purity,
        "description": " ".join(description_parts) or None,
    }


def extract_center_stone(specification: Any) -> dict[str, Any]:
    """Find the center stone's type, origin, and carat in a specification."""
    flat = _flatten(specification)
    stone_type: str | None = None
    origin: tuple[str, ...] | None = None
    carat: float | None = None
    for path, value in flat:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        text = str(value).lower()
        tokens = _tokens(text)
        if stone_type is None and ("stone" in path or "gem" in path or any(w in tokens for w in STONE_WORDS)):
            for word in STONE_WORDS:
                if word in tokens:
                    stone_type = word
                    break
        if origin is None and ("origin" in path or "type" in path or "stone" in path):
            compact = re.sub(r"[^a-z]", "", text)
            for word, mapped in ORIGIN_TOKENS.items():
                if re.sub(r"[^a-z]", "", word) in compact:
                    origin = mapped
                    break
        if carat is None and "carat" in path and "melee" not in path and "accent" not in path:
            number = _number(value)
            if number is not None and 0 < number < 100:
                carat = number
        if carat is None and isinstance(value, str):
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:ct|carat)", text)
            if match and "melee" not in path and "accent" not in path:
                carat = float(match.group(1))
    description = " ".join(
        part for part in (
            "lab-grown" if origin == ("lab", "grown") else ("natural" if origin == ("natural",) else None),
            stone_type,
            f"{carat:g} ct" if carat is not None else None,
        ) if part
    )
    return {
        "stone_type": stone_type,
        "origin": origin,
        "carat": carat,
        "description": description or None,
    }


def match_rate_key(
    card: Any, required: set[str], preferred: set[str]
) -> tuple[str | None, list[str]]:
    """Resolve one card key from tokens, or return the ambiguous candidates."""
    if not isinstance(card, dict) or not required:
        return None, []
    candidates = [
        key for key in card
        if isinstance(key, str) and required <= _tokens(key)
    ]
    if preferred and len(candidates) > 1:
        # The key that shares the most descriptive tokens with the
        # specification wins (14k_white_gold over 14k_yellow_gold for a white
        # gold piece; lab_grown_sapphire over sapphire for a lab-grown stone).
        # A tie stays ambiguous.
        scored = sorted(
            candidates, key=lambda key: len(preferred & _tokens(key)), reverse=True
        )
        best = len(preferred & _tokens(scored[0]))
        if best > 0 and len(preferred & _tokens(scored[1])) < best:
            candidates = [scored[0]]
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, sorted(candidates)


def missing_rates(record: dict[str, Any], shop_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Rates the card lacks for this specification, in the order pricing needs them.

    Each entry names the card section, a key built from the specification's
    own words (so that once the owner's answer is saved under it the next
    match resolves), and the words to use when asking the owner.
    """
    specification = record.get("specification")
    if not isinstance(specification, dict) or not specification:
        raise ValueError("the record has no specification; record the thread review first")
    pricing = shop_profile.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    missing: list[dict[str, Any]] = []
    metal = extract_metal(specification)
    if metal["metal"] is not None and not _spot_enabled(pricing):
        required = {metal["metal"]}
        preferred: set[str] = set()
        if metal["karat"]:
            preferred |= {f"{metal['karat']}k", str(metal["karat"])}
        if metal["color"]:
            preferred.add(metal["color"])
        key, candidates = match_rate_key(pricing.get("metal_per_gram"), required, preferred)
        if key is None:
            parts = [
                f"{metal['karat']}k" if metal["karat"] else None,
                metal["color"],
                metal["metal"],
            ]
            missing.append({
                "rate_kind": "metal_per_gram",
                "line": "metal_lines[0]",
                "suggested_key": "_".join(part for part in parts if part),
                "description": metal["description"] or metal["metal"],
                "candidates": candidates,
            })
    stone = extract_center_stone(specification)
    if stone["stone_type"] is not None:
        preferred = set(stone["origin"] or ())
        key, candidates = match_rate_key(
            pricing.get("stones_per_carat"), {stone["stone_type"]}, preferred
        )
        if key is None:
            origin = stone["origin"] or ()
            words = (
                "lab-grown" if origin == ("lab", "grown")
                else "natural" if origin == ("natural",)
                else None
            )
            missing.append({
                "rate_kind": "stones_per_carat",
                "line": "stone_lines[0]",
                "suggested_key": "_".join([*origin, stone["stone_type"]]),
                "description": " ".join(w for w in (words, stone["stone_type"]) if w),
                "candidates": candidates,
            })
    return missing


def _catalog(card: Any) -> list[dict[str, Any]]:
    if not isinstance(card, dict):
        return []
    return [
        {"rate_key": key, "rate": card[key]}
        for key in sorted(card)
        if not isinstance(card[key], bool) and isinstance(card[key], (int, float))
    ]


def _spot_enabled(pricing: dict[str, Any]) -> bool:
    spot = pricing.get("spot_metal")
    return isinstance(spot, dict) and spot.get("enabled") is True


def _require_spot_evidence(evidence: Any, metal: str) -> float:
    if not isinstance(evidence, dict):
        raise ValueError(
            "spot pricing is enabled, so prepare needs the spot price evidence "
            "written by spot_price.py --output"
        )
    if evidence.get("unit") != "gram":
        raise ValueError(
            "spot price evidence must be per gram to price metal lines; "
            f"evidence unit is {evidence.get('unit')!r}"
        )
    prices = evidence.get("prices")
    if not isinstance(prices, dict) or metal not in prices:
        raise ValueError(f"spot price evidence has no price for {metal}")
    price = prices[metal]
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise ValueError(f"spot price evidence has no usable price for {metal}")
    return float(price)


def prepare(
    record: dict[str, Any],
    shop_profile: dict[str, Any],
    spot_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the cost-components skeleton with every rate resolved."""
    route_ownership.validate_record(record)
    specification = record.get("specification")
    if not isinstance(specification, dict) or not specification:
        raise ValueError(
            "the record has no specification; record the thread review first"
        )
    pricing = shop_profile.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    bench = pricing.get("bench_labor_per_hour")
    if isinstance(bench, bool) or not isinstance(bench, (int, float)):
        raise ValueError("bench_labor_per_hour is not configured; ask the owner")

    fill: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    spot_enabled = _spot_enabled(pricing)

    metal = extract_metal(specification)
    metal_line: dict[str, Any] = {
        "metal": metal["description"] or "metal (describe)",
        "rate_key": None,
        "quantity_grams": None,
        "unit_cost": None,
    }
    if metal["metal"] is None:
        unresolved.append({
            "line": "metal_lines[0]",
            "reason": "no precious metal found in the specification",
        })
    elif spot_enabled:
        spot_price = _require_spot_evidence(spot_evidence, metal["metal"])
        if metal["purity"] is None:
            unresolved.append({
                "line": "metal_lines[0]",
                "reason": f"karat or purity for {metal['metal']} is not in the specification",
            })
        else:
            metal_line.update({
                "rate_key": metal["metal"],
                "spot_price_per_gram": spot_price,
                "purity": metal["purity"],
                "unit_cost": round(spot_price * metal["purity"], 2),
            })
    else:
        required = {metal["metal"]}
        preferred: set[str] = set()
        if metal["karat"]:
            preferred |= {f"{metal['karat']}k", str(metal["karat"])}
        if metal["color"]:
            preferred.add(metal["color"])
        key, candidates = match_rate_key(pricing.get("metal_per_gram"), required, preferred)
        if key is None:
            unresolved.append({
                "line": "metal_lines[0]",
                "reason": "no single metal_per_gram rate matches the specification",
                "candidates": candidates,
            })
        else:
            metal_line["rate_key"] = key
            metal_line["unit_cost"] = float(pricing["metal_per_gram"][key])
    weights = pricing.get("typical_finished_weights")
    piece = str(specification.get("piece_type") or "").lower()
    if isinstance(weights, dict) and piece and isinstance(weights.get(piece), (int, float)) and not isinstance(weights.get(piece), bool):
        metal_line["quantity_grams"] = float(weights[piece])
        fill["metal_lines[0].quantity_grams"] = (
            f"prefilled {weights[piece]} g from typical_finished_weights.{piece}; "
            "adjust only if this design differs"
        )
    else:
        fill["metal_lines[0].quantity_grams"] = "finished grams of metal, estimated high"

    stone = extract_center_stone(specification)
    stone_lines: list[dict[str, Any]] = []
    if stone["stone_type"] is not None:
        stone_line: dict[str, Any] = {
            "stone": stone["description"] or stone["stone_type"],
            "rate_key": None,
            "quantity": stone["carat"],
            "unit_cost": None,
        }
        preferred = set(stone["origin"] or ())
        key, candidates = match_rate_key(
            pricing.get("stones_per_carat"), {stone["stone_type"]}, preferred
        )
        if key is None:
            unresolved.append({
                "line": "stone_lines[0]",
                "reason": "no single stones_per_carat rate matches the center stone",
                "candidates": candidates,
            })
        else:
            stone_line["rate_key"] = key
            stone_line["unit_cost"] = float(pricing["stones_per_carat"][key])
        if stone["carat"] is None:
            fill["stone_lines[0].quantity"] = "center stone carat weight"
        stone_lines.append(stone_line)

    labor_line = {"task": "bench labor", "hours": None, "rate": float(bench)}
    fill["labor_lines[0].hours"] = "bench hours for this piece, estimated high"

    skeleton: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "estimate_id": record["estimate_id"],
        "route": record["route"],
        "specification": specification,
        "proposed_price": None,
        "cost_components": {
            "metal_lines": [metal_line],
            "stone_lines": stone_lines,
            "labor_lines": [labor_line],
            "other_hard_cost_lines": [],
        },
        "fill": fill,
        "unresolved": unresolved,
        "fee_catalog": [
            {"label": item["rate_key"].replace("_", " "), "rate_key": item["rate_key"], "total_cost": float(item["rate"])}
            for item in _catalog(pricing.get("fees"))
        ],
        "stone_catalog": _catalog(pricing.get("stones_per_carat")),
        "metal_catalog": [] if spot_enabled else _catalog(pricing.get("metal_per_gram")),
    }
    if spot_enabled:
        skeleton["spot_price_evidence"] = spot_evidence
    return skeleton


def _filled_number(line: dict[str, Any], field: str, label: str) -> float:
    value = line.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"{label}.{field} must be filled with a non-negative number")
    return float(value)


def finalize(
    skeleton: dict[str, Any], shop_profile: dict[str, Any]
) -> dict[str, Any]:
    """Normalize every rate, derive the price, and emit the approval state."""
    pricing = shop_profile.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    unresolved = skeleton.get("unresolved") or []
    if unresolved:
        raise ValueError(
            "unresolved rates remain: "
            + "; ".join(f"{item.get('line')}: {item.get('reason')}" for item in unresolved)
            + ". Escalate for the rate instead of pricing without one"
        )
    components = skeleton.get("cost_components")
    if not isinstance(components, dict) or set(components) != approval_guard.COST_COMPONENT_FIELDS:
        raise ValueError("cost_components must contain exactly the four line arrays")
    spot_enabled = _spot_enabled(pricing)
    evidence = skeleton.get("spot_price_evidence")
    bench = pricing.get("bench_labor_per_hour")
    if isinstance(bench, bool) or not isinstance(bench, (int, float)):
        raise ValueError("bench_labor_per_hour is not configured; ask the owner")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for group in LINE_GROUPS:
        lines = components[group]
        if not isinstance(lines, list):
            raise ValueError(f"cost_components.{group} must be an array")
        normalized[group] = []
        for index, line in enumerate(lines):
            label = f"cost_components.{group}[{index}]"
            if not isinstance(line, dict):
                raise ValueError(f"{label} must be an object")
            line = {k: v for k, v in line.items() if k in LINE_FIELDS[group]}
            if group == "metal_lines":
                if not isinstance(line.get("metal"), str) or not line["metal"].strip():
                    raise ValueError(f"{label}.metal must describe the metal")
                _filled_number(line, "quantity_grams", label)
                if spot_enabled:
                    rate_key = line.get("rate_key")
                    if rate_key not in estimate_record.SPOT_METALS:
                        raise ValueError(
                            f"{label}.rate_key must name a spot metal (gold, silver, platinum, palladium)"
                        )
                    spot_price = _require_spot_evidence(evidence, rate_key)
                    purity = _filled_number(line, "purity", label)
                    if not 0 < purity <= 1:
                        raise ValueError(f"{label}.purity must be greater than 0 and at most 1")
                    line["spot_price_per_gram"] = spot_price
                    line["unit_cost"] = round(spot_price * purity, 2)
                else:
                    line.pop("spot_price_per_gram", None)
                    line.pop("purity", None)
                    line["unit_cost"] = estimate_record._card_rate(
                        pricing.get("metal_per_gram"), line.get("rate_key"), label
                    )
            elif group == "stone_lines":
                if not isinstance(line.get("stone"), str) or not line["stone"].strip():
                    raise ValueError(f"{label}.stone must describe the stone")
                _filled_number(line, "quantity", label)
                line["unit_cost"] = estimate_record._card_rate(
                    pricing.get("stones_per_carat"), line.get("rate_key"), label
                )
            elif group == "labor_lines":
                if not isinstance(line.get("task"), str) or not line["task"].strip():
                    raise ValueError(f"{label}.task must describe the work")
                _filled_number(line, "hours", label)
                line["rate"] = float(bench)
            else:
                rate = estimate_record._card_rate(
                    pricing.get("fees"), line.get("rate_key"), label
                )
                if not isinstance(line.get("label"), str) or not line["label"].strip():
                    line["label"] = str(line["rate_key"]).replace("_", " ")
                line["total_cost"] = rate
            normalized[group].append(line)

    provisional = approval_guard.build_internal_cost_sheet(normalized, 0.0)
    proposed_price = pricing_model.quote_price(provisional["hard_cost_total"], pricing)
    sheet = approval_guard.build_internal_cost_sheet(normalized, proposed_price)
    estimate_record.enforce_configured_price(sheet, proposed_price, shop_profile)
    estimate_record.enforce_rate_provenance(sheet, pricing, evidence)

    state: dict[str, Any] = {
        "estimate_id": skeleton.get("estimate_id"),
        "route": skeleton.get("route"),
        "specification": skeleton.get("specification"),
        "proposed_price": proposed_price,
        "cost_components": normalized,
    }
    if spot_enabled:
        state["spot_price_evidence"] = evidence
    approval_guard.binding_payload({**state, "internal_cost_sheet": sheet})
    return state


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--record-root", type=Path, default=estimate_record.default_record_root())
    prep.add_argument("--estimate-id", required=True)
    prep.add_argument("--shop-profile", type=Path, required=True)
    prep.add_argument("--spot-evidence", type=Path)
    prep.add_argument("--output", type=Path, required=True)
    fin = sub.add_parser("finalize")
    fin.add_argument("--input", type=Path, required=True)
    fin.add_argument("--shop-profile", type=Path, required=True)
    fin.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            record = read_object(
                estimate_record.record_path(args.record_root, args.estimate_id)
            )
            evidence = read_object(args.spot_evidence) if args.spot_evidence else None
            result = prepare(record, read_object(args.shop_profile), evidence)
        else:
            result = finalize(read_object(args.input), read_object(args.shop_profile))
        estimate_record.write_object(args.output, result)
        summary = {
            "output": str(args.output),
            "unresolved": result.get("unresolved", []),
            "fill": sorted(result.get("fill", {})),
            "proposed_price": result.get("proposed_price"),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
