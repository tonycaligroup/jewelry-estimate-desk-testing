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
STONE_WORDS = tuple(estimate_record.FANCY_DIAMOND_COLORS[index] + " diamond"
                    for index in range(len(estimate_record.FANCY_DIAMOND_COLORS))) + (
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
    words = [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]
    tokens = set(words)
    for width in range(2, min(4, len(words)) + 1):
        tokens.update(" ".join(words[index:index + width]) for index in range(len(words) - width + 1))
    return tokens


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


PAVE_WORDS = ("pave", "pavé", "melee", "micro pave", "micropave", "accent", "cluster", "eternity", "encrusted",
              "shoulder", "side stone", "side-stone", "small diamond", "small stone", "tiny",
              "channel set", "channel-set", "channel", "all the way around", "all around", "around the band",
              "in the middle of the band", "bead set", "bead-set")
CENTER_WORDS = ("center stone", "centre stone", "main stone", "solitaire", "halo", "feature stone", "focal")


def has_center_stone(specification: Any) -> bool:
    """Whether the piece has one main stone to price per carat.

    The customer's words decide when they are explicit (center_stone yes or
    no from the extractor); otherwise small pave or melee stones with no
    stated carat mean there is no center stone, and anything else keeps the
    old assumption that a named stone is the center stone.
    """
    if not isinstance(specification, dict):
        return True
    explicit = str(specification.get("center_stone") or "").strip().lower()
    if explicit in {"no", "none", "false"}:
        return False
    if explicit in {"yes", "true"}:
        return True
    words = " ".join(
        str(specification.get(key) or "").lower()
        for key in ("setting_style", "notes", "accent_stones", "stone_count", "stone_type", "dimensions", "piece_type")
    )
    # The design words come before a stated carat: "0.2 ct, channel-set
    # eternity" is the total of small stones, not one stone (live, 8
    # September 2026: the desk asked for "the carat weight of the center
    # stone" on a men's channel-set band).
    if any(word in words for word in PAVE_WORDS) and not any(word in words for word in CENTER_WORDS):
        return False
    if specification.get("stone_carat") not in (None, "", []):
        return True
    if any(word in words for word in PAVE_WORDS):
        return False
    # Stones described only as accents, with no carat and nothing called a
    # center or main stone, are accents: nothing to price per carat.
    if specification.get("accent_stones") not in (None, "", []) and "center" not in words and "main stone" not in words:
        return False
    # Small millimetre sizes count only when they describe the stones, not a
    # band or shank ("stones about 1mm" yes; "2mm band" no).
    for match in re.finditer(r"\b(?:0?\.\d+|1(?:\.\d+)?|2(?:\.\d+)?)\s*mm\b", words):
        window = words[max(0, match.start() - 40): match.end() + 40]
        if any(w in window for w in ("stone", "diamond", "sapphire", "ruby", "emerald", "gem")) and not any(
            w in window for w in ("band", "shank", "wide", "width", "thick")
        ) and "center" not in words:
            return False
    return True


NO_STONE_WORDS = ("no stones", "without stones", "no diamonds", "no gems", "plain band", "no gemstones")


def extract_center_stone(specification: Any) -> dict[str, Any]:
    """Find the center stone's type, origin, and carat in a specification."""
    if estimate_record.customer_supplies_stone(specification):
        # The customer's own stone costs the shop nothing.
        return {"stone_type": None, "origin": None, "carat": None, "description": None}
    if not has_center_stone(specification):
        return {"stone_type": None, "origin": None, "carat": None, "description": None}
    flat = _flatten(specification)
    stone_type: str | None = None
    origin: tuple[str, ...] | None = None
    carat: float | None = None
    for path, value in flat:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        if any(word in path for word in ("accent", "melee", "reference", "note", "photo", "scheduling")):
            # The halo's diamonds, a photo's reading, and a note are not the center stone. The ledger's derived
            # specification lists keys in name order, so accent_stones came before stone_type and a sapphire
            # pair priced as diamonds (9 September 2026).
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


def center_stone_card(card: Any) -> dict[str, Any]:
    """The stone card without melee entries: a center stone is never priced as melee."""
    if not isinstance(card, dict):
        return {}
    return {key: value for key, value in card.items() if "melee" not in str(key).lower()}


def live_quote_key(stone: dict[str, Any], pricing: dict[str, Any]) -> str | None:
    """The key for a lab-grown diamond center at or above the jeweler's live-quote line, else None."""
    threshold = pricing.get("lab_center_live_quote_ct") if isinstance(pricing, dict) else None
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        return None
    if stone.get("stone_type") != "diamond" or stone.get("origin") != ("lab", "grown") or not stone.get("carat"):
        return None
    if float(stone["carat"]) < float(threshold):
        return None
    return f"lab_grown_diamond_center_{str(float(stone['carat'])).replace('.', '_').rstrip('0').rstrip('_')}ct"


def with_one_time_rates(pricing: Any, record: dict[str, Any]) -> Any:
    """The card plus the rates the owner gave for this estimate only ("use 450 once", 9 September 2026)."""
    once = record.get("one_time_rates") if isinstance(record, dict) and isinstance(record.get("one_time_rates"), dict) else {}
    if not isinstance(pricing, dict) or not once:
        return pricing
    merged = dict(pricing)
    for kind, rates in once.items():
        if isinstance(rates, dict):
            merged[kind] = {**(pricing.get(kind) if isinstance(pricing.get(kind), dict) else {}), **rates}
    return merged


def missing_rates(record: dict[str, Any], shop_profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Rates the card lacks for this specification, in the order pricing needs them.

    Each entry names the card section, a key built from the specification's
    own words (so that once the owner's answer is saved under it the next
    match resolves), and the words to use when asking the owner.
    """
    specification = record.get("specification")
    if not isinstance(specification, dict) or not specification:
        raise ValueError("the record has no specification; record the thread review first")
    pricing = with_one_time_rates(shop_profile.get("pricing"), record)
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    missing: list[dict[str, Any]] = []
    for index, piece in enumerate(estimate_record.pieces_of(specification)):
        for item in _missing_rates_for_piece(piece, index, pricing):
            if not any(m["rate_kind"] == item["rate_kind"] and m["suggested_key"] == item["suggested_key"] for m in missing):
                missing.append(item)
    return missing


def _missing_rates_for_piece(specification: dict[str, Any], index: int, pricing: dict[str, Any]) -> list[dict[str, Any]]:
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
                "line": f"metal_lines[{index}]",
                "suggested_key": "_".join(part for part in parts if part),
                "description": metal["description"] or metal["metal"],
                "candidates": candidates,
            })
    stone = extract_center_stone(specification)
    if stone["stone_type"] is not None:
        preferred = set(stone["origin"] or ())
        key, candidates = match_rate_key(
            center_stone_card(pricing.get("stones_per_carat")), {stone["stone_type"]}, preferred
        )
        live = live_quote_key(stone, pricing)
        if live and live not in (pricing.get("stones_per_carat") or {}):
            missing.append({
                "rate_kind": "stones_per_carat", "line": f"stone_lines[{index}]", "suggested_key": live,
                "description": f"a {stone['carat']:g} ct lab-grown diamond center (live quote, above your {float(pricing.get('lab_center_live_quote_ct')):g} ct line)",
                "candidates": [],
            })
            key = live
        if key is None:
            origin = stone["origin"] or ()
            words = (
                "lab-grown" if origin == ("lab", "grown")
                else "natural" if origin == ("natural",)
                else None
            )
            missing.append({
                "rate_kind": "stones_per_carat",
                "line": f"stone_lines[{index}]",
                "suggested_key": "_".join([*origin, stone["stone_type"]]).replace("-", "_").replace(" ", "_"),
                "description": " ".join(w for w in (words, stone["stone_type"]) if w),
                "candidates": candidates,
            })
    else:
        missing.extend(missing_accent_rates(specification, pricing))
    return missing


# Round brilliant millimetres to carats, the trade's standard chart (geometry, not a price; interpolated between rows).
MM_TO_CT = [(0.8, 0.0025), (0.9, 0.004), (1.0, 0.005), (1.1, 0.006), (1.2, 0.008), (1.3, 0.010), (1.4, 0.012), (1.5, 0.015),
            (1.6, 0.018), (1.7, 0.020), (1.8, 0.025), (1.9, 0.027), (2.0, 0.030), (2.1, 0.035), (2.2, 0.040), (2.3, 0.045),
            (2.4, 0.050), (2.5, 0.060), (2.6, 0.065), (2.7, 0.070), (2.8, 0.080), (2.9, 0.090), (3.0, 0.10), (3.2, 0.12),
            (3.4, 0.15), (3.5, 0.16), (3.8, 0.20), (4.0, 0.24), (4.5, 0.35), (5.0, 0.48)]
FRAGILE_STONES = ("emerald", "opal", "tanzanite", "pearl", "turquoise", "kunzite", "apatite")
_COUNT_MM_RE = re.compile(r"(?i)\b(\d{1,3})\s*(?:x|×|pcs?|pieces?|stones?)?\s*(?:of\s+)?(\d(?:\.\d)?)\s*mm\b")


def carats_for_mm(mm: float) -> float | None:
    """Carats of one round stone of this diameter, from the chart; None outside it."""
    if mm < MM_TO_CT[0][0] or mm > MM_TO_CT[-1][0]:
        return None
    for (m1, c1), (m2, c2) in zip(MM_TO_CT, MM_TO_CT[1:]):
        if m1 <= mm <= m2:
            return round(c1 + (c2 - c1) * (mm - m1) / (m2 - m1), 4) if m2 != m1 else c1
    return None


def accent_count_and_size(specification: dict[str, Any]) -> tuple[int, float] | None:
    """'18 x 1.3mm diamonds' in the customer's or owner's words: (count, millimetres), else None."""
    text = " ".join(str(specification.get(k) or "") for k in ("accent_stones", "notes", "setting_style"))
    match = _COUNT_MM_RE.search(text)
    if not match:
        return None
    count, mm = int(match.group(1)), float(match.group(2))
    return (count, mm) if 0 < count <= 500 and 0.5 <= mm <= 6.0 else None


def melee_band_key(origin: str, mm: float) -> str:
    """The rate card key for round melee of this size: small up to 1.5 mm natural (2.5 mm lab), medium to 2.5, large to 4."""
    if origin == "lab-grown":
        return "lab_grown_diamond_melee_small" if mm <= 2.5 else "lab_grown_diamond_melee_large"
    return "natural_diamond_melee_small" if mm <= 1.5 else "natural_diamond_melee_medium" if mm <= 2.5 else "natural_diamond_melee_large"


def sized_melee(specification: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any] | None:
    """When the words give a count and a millimetre size and the card has that size band: the accent line, priced from it."""
    found = accent_count_and_size(specification)
    if not found:
        return None
    count, mm = found
    each = carats_for_mm(mm)
    if each is None:
        return None
    origin_raw = str(specification.get("stone_origin") or specification.get("accent_stone_origin") or "").lower()
    origin = "lab-grown" if origin_raw.startswith("lab") else "natural" if origin_raw == "natural" else ""
    if not origin:
        return None
    key = melee_band_key(origin, mm)
    card = pricing.get("stones_per_carat") if isinstance(pricing.get("stones_per_carat"), dict) else {}
    if not isinstance(card.get(key), (int, float)) or isinstance(card.get(key), bool):
        return None
    return {"key": key, "carats": round(count * each, 3), "count": count, "mm": mm, "each": each}


def _pct(pricing: dict[str, Any], section: str, key: str) -> float | None:
    block = pricing.get(section)
    value = block.get(key) if isinstance(block, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _setting_rate(pricing: dict[str, Any], key: str) -> float | None:
    return _pct(pricing, "setting_labor", key)


def _center_band(carat: float) -> str:
    if carat < 0.5:
        return "center_under_0_50"
    if carat < 1:
        return "center_0_50_to_0_99"
    if carat < 2:
        return "center_1_to_1_99"
    if carat < 3:
        return "center_2_to_2_99"
    if carat < 5:
        return "center_3_to_4_99"
    return "center_5_plus"


def _complexity(specification: dict[str, Any]) -> str:
    text = " ".join(str(specification.get(k) or "") for k in ("setting_style", "notes", "accent_stones", "engraving")).lower()
    if any(w in text for w in ("micro pave", "micro-pave", "intricate", "filigree", "hand engraved", "complex", "eternity", "cluster")):
        return "complex"
    if any(w in text for w in ("plain", "simple", "solitaire", "band only")) and not specification.get("accent_stones"):
        return "simple"
    return "normal"


def apply_allowances(normalized: dict[str, list[dict[str, Any]]], pricing: dict[str, Any], specification: dict[str, Any]) -> list[dict[str, Any]]:
    """The rules the jeweler's own numbers switch on (9 September 2026): setting labor, waste, contingency, the minimum job.

    Each rule runs only when its rate is on the card, and each line it adds
    says what it was computed from, so the provenance check can redo the
    arithmetic. Nothing here invents a number.
    """
    added: list[dict[str, Any]] = []
    spec = specification if isinstance(specification, dict) else {}
    metal_total = sum(float(l.get("quantity_grams") or 0) * float(l.get("unit_cost") or 0) for l in normalized.get("metal_lines") or [])
    stone_lines = normalized.get("stone_lines") or []
    labor_total = sum(float(l.get("hours") or 0) * float(l.get("rate") or 0) for l in normalized.get("labor_lines") or [])
    fees_total = sum(float(l.get("total_cost") or 0) for l in normalized.get("other_hard_cost_lines") or [])
    stones_total = sum(float(l.get("quantity") or 0) * float(l.get("unit_cost") or 0) for l in stone_lines)

    def line(label: str, section: str, key: str, kind: str, rate: float, basis: float, total: float) -> None:
        if total > 0:
            added.append({"label": label, "rate_key": f"allowance:{section}:{key}", "kind": kind, "rate": rate, "basis": round(basis, 4),
                          "total_cost": round(total, 2)})

    # Center stone setting by carat band, with the extras the card names.
    center = extract_center_stone(spec)
    if center.get("stone_type") and center.get("carat") and not estimate_record.customer_supplies_stone(spec):
        band = _center_band(float(center["carat"]))
        rate = _setting_rate(pricing, band)
        if rate is not None:
            extras = 0.0
            shape = str(spec.get("stone_shape") or spec.get("stone_cut") or "").lower()
            if shape and shape != "round" and _setting_rate(pricing, "fancy_shape_extra_pct"):
                extras += _setting_rate(pricing, "fancy_shape_extra_pct") or 0
            if "bezel" in str(spec.get("setting_style") or "").lower() and _setting_rate(pricing, "bezel_extra_pct"):
                extras += _setting_rate(pricing, "bezel_extra_pct") or 0
            if str(center["stone_type"]).lower() in FRAGILE_STONES and _setting_rate(pricing, "fragile_extra_pct"):
                extras += _setting_rate(pricing, "fragile_extra_pct") or 0
            stones = 2 if estimate_record.is_pair(spec) else 1
            line(f"center stone setting ({band.replace('_', ' ').replace('center ', '')} ct" + (f", +{extras:g}%" if extras else "") + ")",
                 "setting_labor", band, "setting", rate, extras + 100 * (stones - 1), rate * (1 + extras / 100) * stones)
    # Melee setting per stone, when the count is known and the style has a rate.
    sized = accent_count_and_size(spec)
    if sized:
        # The melee's own words first ("18 x 1.3mm, pave"), then the piece's setting.
        style_words = str(spec.get("accent_stones") or "").lower() + " | " + str(spec.get("setting_style") or "").lower()
        style = next((k for k in ("micro_pave", "shared_prong", "baguette_channel", "pave", "channel", "bezel", "flush", "bead", "prong")
                      if k.replace("_", " ") in style_words or k.replace("_", "-") in style_words), None)
        rate = _setting_rate(pricing, style) if style else None
        if rate is not None:
            line(f"setting {sized[0]} stones, {style.replace('_', ' ')}", "setting_labor", style, "per_stone", rate, float(sized[0]), rate * sized[0])
    # Waste on metal, melee, and fragile stones.
    pct = _pct(pricing, "allowances", "metal_waste_pct")
    if pct and metal_total:
        line(f"metal waste {pct:g}%", "allowances", "metal_waste_pct", "pct", pct, metal_total, metal_total * pct / 100)
    pct = _pct(pricing, "allowances", "melee_waste_pct")
    melee_total = sum(float(l.get("quantity") or 0) * float(l.get("unit_cost") or 0) for l in stone_lines
                      if any(w in str(l.get("rate_key") or "").lower() for w in ("melee", "accent", "pave")))
    if pct and melee_total:
        line(f"melee waste {pct:g}%", "allowances", "melee_waste_pct", "pct", pct, melee_total, melee_total * pct / 100)
    pct = _pct(pricing, "allowances", "fragile_waste_pct")
    fragile_total = sum(float(l.get("quantity") or 0) * float(l.get("unit_cost") or 0) for l in stone_lines
                        if any(w in str(l.get("stone") or "").lower() for w in FRAGILE_STONES))
    if pct and fragile_total:
        line(f"fragile stone waste {pct:g}%", "allowances", "fragile_waste_pct", "pct", pct, fragile_total, fragile_total * pct / 100)
    # Contingency before CAD, by how complex the piece reads.
    subtotal = metal_total + stones_total + labor_total + fees_total + sum(float(l["total_cost"]) for l in added)
    complexity = _complexity(spec)
    pct = _pct(pricing, "allowances", f"contingency_{complexity}_pct")
    if pct and subtotal:
        line(f"contingency {pct:g}% ({complexity})", "allowances", f"contingency_{complexity}_pct", "pct", pct, subtotal, subtotal * pct / 100)
        subtotal += subtotal * pct / 100
    # The minimum job charge tops the hard cost up.
    minimum = pricing.get("minimum_job")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and minimum > subtotal > 0:
        line("minimum job charge", "pricing", "minimum_job", "minimum", float(minimum), subtotal, float(minimum) - subtotal)
    return added


def accent_stone_needs(specification: dict[str, Any]) -> list[tuple[str, str]]:
    """(origin word, stone word) pairs for small stones the piece carries, from the customer's words."""
    text = " ".join(
        str(specification.get(key) or "").lower()
        for key in ("stone_type", "accent_stones", "setting_style", "notes", "piece_type", "stone_color")
    )
    if any(word in text for word in NO_STONE_WORDS) and not str(specification.get("stone_type") or "").strip():
        return []
    origin_raw = str(specification.get("stone_origin") or "").lower().replace("_", "-").replace(" ", "-")
    origin = "lab-grown" if origin_raw.startswith("lab") else ("natural" if origin_raw == "natural" else "")
    stones = [w for w in STONE_WORDS if re.search(rf"\b{w}s?\b", text)]
    if not stones and any(w in text for w in ("tennis", "pave", "pavé", "melee", "eternity", "halo")):
        stones = ["diamond"]
    return [(origin, stone) for stone in stones]


def missing_accent_rates(specification: dict[str, Any], pricing: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-carat rates the card lacks for small stones (pave, melee, tennis rows).

    Asked only once the origin is known; before that the customer is asked,
    not the owner. A key on the card counts when it names the stone and the
    origin, or the stone and 'melee' or 'accent' with no other origin word.
    """
    card = pricing.get("stones_per_carat")
    if not isinstance(card, dict):
        card = {}
    missing: list[dict[str, Any]] = []
    for origin, stone in accent_stone_needs(specification):
        if not origin:
            continue
        origin_token = "lab" if origin == "lab-grown" else "natural"
        other = "natural" if origin_token == "lab" else "lab"
        found = False
        for key in card:
            tokens = set(re.split(r"[^a-z0-9]+", str(key).lower()))
            if stone in tokens and (origin_token in tokens or ("lab" in tokens and origin_token == "lab")) and other not in tokens:
                if any(t in tokens for t in ("melee", "accent", "pave", "small")) or "center" not in tokens:
                    found = True
                    break
        if not found:
            missing.append({
                "rate_kind": "stones_per_carat",
                "line": "stone_lines[accent]",
                "suggested_key": f"{origin_token}_grown_{stone}_melee" if origin_token == "lab" else f"natural_{stone}_melee",
                "description": f"{origin} {stone} melee (small accent or pave stones, per carat)",
                "candidates": [k for k in card if stone in str(k).lower()],
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


def prior_quantities(record: dict[str, Any]) -> list[dict[str, Any]]:
    """After "second piece", every piece already quoted keeps the numbers it was quoted on.

    The sent estimate is a commitment (WORKFLOW.md 6.8): its grams, hours,
    center carat, fees and accent stones come back from the archived cost
    sheet (`estimate_history[-1]`), one entry per prior piece in order, in
    the shape of a quantities answer. The model is asked only about the new
    piece. Rates still come from today's profile. An empty list means
    nothing is frozen (not a second-piece reopen, or no archived sheet).
    """
    if not isinstance(record, dict) or record.get("reopened_for") != "second_piece":
        return []
    history = record.get("estimate_history") or []
    prior = history[-1] if history and isinstance(history[-1], dict) else {}
    sheet, spec = prior.get("internal_cost_sheet"), prior.get("specification")
    if not isinstance(sheet, dict) or not isinstance(spec, dict) or not spec:
        return []
    pieces = estimate_record.pieces_of(spec)
    if not pieces:
        return []
    labels = [estimate_record.piece_label(spec, i) for i in range(len(pieces))] if len(pieces) > 1 else [""]

    def owner_of(text: Any) -> int | None:
        if len(labels) == 1:
            return 0
        for i, label in enumerate(labels):
            if str(text).endswith(f" ({label})"):
                return i
        return None

    result = [{"finished_grams": None, "bench_hours": None, "center_carat": None, "fees": [], "accents": []}
              for _ in labels]
    metal, labor = sheet.get("metal_lines") or [], sheet.get("labor_lines") or []
    for i in range(len(labels)):
        if i < len(metal) and isinstance(metal[i], dict):
            result[i]["finished_grams"] = _number(metal[i].get("quantity_grams"))
        if i < len(labor) and isinstance(labor[i], dict):
            result[i]["bench_hours"] = _number(labor[i].get("hours"))
    # Center lines come first, one per piece with a stone, in piece order;
    # accent lines follow (that is how `prepare` and `price` build them).
    with_center = [i for i, piece in enumerate(pieces) if extract_center_stone(piece)["stone_type"] is not None]
    stones = [line for line in (sheet.get("stone_lines") or []) if isinstance(line, dict)]
    for i, line in zip(with_center, stones[:len(with_center)]):
        result[i]["center_carat"] = _number(line.get("quantity"))
    for line in stones[len(with_center):]:
        i = owner_of(line.get("stone"))
        if i is not None and line.get("rate_key") and _number(line.get("quantity")):
            result[i]["accents"].append({"key": str(line["rate_key"]), "carats": _number(line.get("quantity"))})
    for line in sheet.get("other_hard_cost_lines") or []:
        if not isinstance(line, dict) or not line.get("rate_key"):
            continue
        i = owner_of(line.get("label"))
        if i is not None:
            result[i]["fees"].append(str(line["rate_key"]))
    return [r for r in result if r["finished_grams"] and r["bench_hours"]]


# Facts that change nothing a bench jeweler would weigh, count or time:
# a twin piece may differ in these and still take the quoted piece's numbers.
COSMETIC_KEYS = frozenset({
    "metal_color", "finish", "engraving", "notes", "stone_color", "stone_clarity", "stone_cut",
    "certificate", "reference_images", "scheduling_intent", "event_date", "budget",
})


def _plain(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def is_twin(piece: dict[str, Any], other: dict[str, Any]) -> bool:
    """The same piece in another colour or finish: every fact that drives quantities agrees.

    Live, 7 September 2026: "the same band in rose gold" was weighed again
    by the model (12 g became 10 g, 1.8 ct became 1.2 ct). Two pieces are
    twins when their piece type is the same, every non-cosmetic fact they
    both state is equal, and they agree on whether there are stones at all.
    A fact only one of them states is not a disagreement (the re-read of
    "same as the first" is usually the thinner one), except the stone facts,
    which must be stated on both or on neither.
    """
    if _plain(piece.get("piece_type") or "") != _plain(other.get("piece_type") or "") or not piece.get("piece_type"):
        return False
    for key in set(piece) | set(other):
        if key in COSMETIC_KEYS or key == "pieces":
            continue
        mine, theirs = piece.get(key), other.get(key)
        if key not in piece or mine in (None, "", []):
            continue  # the thinner re-read left it out; the quoted piece's fact stands
        if key not in other:
            return False  # a fact stated only for the new piece ("7 mm wide") is a difference
        if _plain(mine) != _plain(theirs if theirs is not None else ""):
            return False
    # A band with melee and a plain band are not twins, however alike otherwise:
    # the stone facts must be stated on both or on neither.
    return all(bool(piece.get(key)) == bool(other.get(key)) for key in ("stone_type", "accent_stones"))


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
    pricing = with_one_time_rates(shop_profile.get("pricing"), record)
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    bench = pricing.get("bench_labor_per_hour")
    if isinstance(bench, bool) or not isinstance(bench, (int, float)):
        raise ValueError("bench_labor_per_hour is not configured; ask the owner")

    fill: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []
    spot_enabled = _spot_enabled(pricing)
    pieces = estimate_record.pieces_of(specification)
    multi = len(pieces) > 1
    metal_lines: list[dict[str, Any]] = []
    stone_lines: list[dict[str, Any]] = []
    labor_lines: list[dict[str, Any]] = []
    piece_map: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces):
        label = estimate_record.piece_label(specification, index) if multi else ""
        tag = f" ({label})" if multi else ""
        this = "this piece" if not multi else f"the {label}"
        metal = extract_metal(piece)
        metal_line: dict[str, Any] = {
            "metal": (metal["description"] or "metal (describe)") + tag,
            "rate_key": None,
            "quantity_grams": None,
            "unit_cost": None,
        }
        m = f"metal_lines[{index}]"
        if metal["metal"] is None:
            unresolved.append({"line": m, "reason": f"no precious metal found in the specification{tag}"})
        elif spot_enabled:
            spot_price = _require_spot_evidence(spot_evidence, metal["metal"])
            if metal["purity"] is None:
                unresolved.append({"line": m, "reason": f"karat or purity for {metal['metal']} is not in the specification{tag}"})
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
                unresolved.append({"line": m, "reason": f"no single metal_per_gram rate matches the specification{tag}",
                                   "candidates": candidates})
            else:
                metal_line["rate_key"] = key
                metal_line["unit_cost"] = float(pricing["metal_per_gram"][key])
        weights = pricing.get("typical_finished_weights")
        kind = str(piece.get("piece_type") or "").lower()
        if isinstance(weights, dict) and kind and isinstance(weights.get(kind), (int, float)) and not isinstance(weights.get(kind), bool):
            metal_line["quantity_grams"] = float(weights[kind])
            fill[f"{m}.quantity_grams"] = (
                f"prefilled {weights[kind]} g from typical_finished_weights.{kind}; "
                "adjust only if this design differs"
            )
        else:
            fill[f"{m}.quantity_grams"] = f"finished grams of metal, estimated high" + (f", for {this}" if multi else "")
        metal_lines.append(metal_line)

        stone = extract_center_stone(piece)
        center_index = None
        if stone["stone_type"] is not None:
            carats = stone["carat"]
            basis = str(piece.get("stone_carat_basis") or "").lower()
            if carats and estimate_record.is_pair(piece) and basis == "each":
                carats = round(float(carats) * 2, 3)  # two stones, the carat given for each
            stone_line: dict[str, Any] = {
                "stone": (stone["description"] or stone["stone_type"]) + (" (two stones)" if carats != stone["carat"] else "") + tag,
                "rate_key": None,
                "quantity": carats,
                "unit_cost": None,
            }
            preferred = set(stone["origin"] or ())
            key, candidates = match_rate_key(
                center_stone_card(pricing.get("stones_per_carat")), {stone["stone_type"]}, preferred
            )
            live = live_quote_key(stone, pricing)
            if live and live not in (pricing.get("stones_per_carat") or {}):
                key, candidates = None, []  # asked for, this stone's own price (the jeweler's threshold)
            elif live:
                key = live
            center_index = len(stone_lines)
            sl = f"stone_lines[{center_index}]"
            if key is None:
                unresolved.append({"line": sl, "reason": f"no single stones_per_carat rate matches the center stone{tag}",
                                   "candidates": candidates})
            else:
                stone_line["rate_key"] = key
                stone_line["unit_cost"] = float(pricing["stones_per_carat"][key])
            if stone["carat"] is None:
                fill[f"{sl}.quantity"] = "center stone carat weight" + (f" for {this}" if multi else "")
            stone_lines.append(stone_line)

        if stone["stone_type"] is None and not (
            estimate_record.customer_supplies_stone(piece) and not piece.get("accent_stones")
        ):
            for need in missing_accent_rates(piece, pricing):
                unresolved.append({"line": need["line"], "reason": f"no stones_per_carat rate for {need['description']}{tag}",
                                   "candidates": need.get("candidates", [])})
        labor_lines.append({"task": "bench labor" + tag, "hours": None, "rate": float(bench)})
        fill[f"labor_lines[{index}].hours"] = "bench hours for this piece, estimated high" if not multi else f"bench hours for {this}, estimated high"
        piece_map.append({"index": index, "label": label or kind or "piece", "metal_line": index, "labor_line": index,
                          "center_stone_line": center_index, "needs_carat": center_index is not None and stone["carat"] is None,
                          "stone_origin": str(piece.get("stone_origin") or "")})
    if multi:
        # The pieces already quoted keep their numbers; only the new one is estimated.
        for info, prior in zip(piece_map, prior_quantities(record)):
            info["prior_quantities"] = prior
        # A new piece that is a quoted piece in another colour takes its numbers too.
        for info in piece_map:
            if info.get("prior_quantities"):
                continue
            for quoted in piece_map:
                if quoted.get("prior_quantities") and is_twin(pieces[info["index"]], pieces[quoted["index"]]):
                    info["prior_quantities"] = {**quoted["prior_quantities"], "twin_of": quoted["label"]}
                    break
    # The same missing rate for two pieces is one question, not two.
    seen: set[str] = set()
    deduped = []
    for item in unresolved:
        key = item["reason"].split(" (")[0] + "|" + json.dumps(item.get("candidates", []), sort_keys=True)
        if key in seen and multi:
            continue
        seen.add(key)
        deduped.append(item)
    unresolved = deduped

    skeleton: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "estimate_id": record["estimate_id"],
        "route": record["route"],
        "specification": specification,
        "proposed_price": None,
        "cost_components": {
            "metal_lines": metal_lines,
            "stone_lines": stone_lines,
            "labor_lines": labor_lines,
            "other_hard_cost_lines": [],
        },
        "pieces": piece_map,
        "fill": fill,
        "unresolved": unresolved,
        "fee_catalog": [
            {"label": item["rate_key"].replace("_", " "), "rate_key": item["rate_key"], "total_cost": float(item["rate"])}
            for item in _catalog(pricing.get("fees"))
        ],
        "stone_catalog": _catalog(pricing.get("stones_per_carat")),
        "one_time_rates": record.get("one_time_rates") if isinstance(record.get("one_time_rates"), dict) else {},
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
    pricing = with_one_time_rates(shop_profile.get("pricing"), {"one_time_rates": skeleton.get("one_time_rates") or {}})
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
                if str(line.get("rate_key") or "").startswith("allowance:"):
                    continue  # recomputed below from the card
                rate = estimate_record._card_rate(
                    pricing.get("fees"), line.get("rate_key"), label
                )
                if not isinstance(line.get("label"), str) or not line["label"].strip():
                    line["label"] = str(line["rate_key"]).replace("_", " ")
                line["total_cost"] = rate
            normalized[group].append(line)

    normalized["other_hard_cost_lines"] = [l for l in normalized["other_hard_cost_lines"] if not str(l.get("rate_key") or "").startswith("allowance:")]
    normalized["other_hard_cost_lines"].extend(apply_allowances(normalized, pricing, skeleton.get("specification") or {}))
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
