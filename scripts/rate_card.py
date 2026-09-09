#!/usr/bin/env python3
"""The rate card as a sheet: our fields, the jeweler's numbers (the owner, 9 September 2026).

The schema is the desk's: every cost the engine can use has a labelled row
under a section. Nothing ships with a number; the jeweler's own pricing
model fills what it can (rates_intake.py), the jeweler edits the Value
column on the "Rates" tab, and the desk reads it back every tick into the
shop profile. A row the schema lacks is added by the jeweler under any
section with a Field name; the desk derives its key from the words. Filled
rows sort to the top of their section.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HEADERS = ["Section", "Field", "Key", "Value", "Unit", "Notes", "Updated"]

# (section, profile path, unit, [(field label, key, notes)])
SCHEMA: list[dict[str, Any]] = [
    {"section": "pricing model", "path": "pricing", "unit": "", "text": {"model"}, "fields": [
        ("pricing model", "model", "cost_plus_multiplier or target_margin"),
        ("markup multiplier", "markup_multiplier", "cost x this = quote (1.25 = cost plus 25%)"),
        ("target margin", "target_margin", "decimal; quote = cost / (1 - margin)"),
    ]},
    {"section": "metal per gram", "path": "pricing.metal_per_gram", "unit": "$/g", "fields": [
        ("10K yellow gold", "10k_yellow_gold", ""), ("10K white gold", "10k_white_gold", ""),
        ("14K yellow gold", "14k_yellow_gold", ""), ("14K white gold", "14k_white_gold", ""), ("14K rose gold", "14k_rose_gold", ""),
        ("18K yellow gold", "18k_yellow_gold", ""), ("18K white gold", "18k_white_gold", ""), ("18K rose gold", "18k_rose_gold", ""),
        ("platinum", "platinum", ""), ("sterling silver", "sterling_silver", ""),
    ]},
    {"section": "spot metal", "path": "pricing", "unit": "", "text": {"spot_enabled"}, "fields": [
        ("spot pricing on", "spot_enabled", "yes or no; with spot on, the per-gram rows are ignored"),
        ("manufacturing factor", "metal_factor", "raw metal value x this (alloy, loss, refining); blank = 1"),
    ]},
    {"section": "diamond melee per carat", "path": "pricing.stones_per_carat", "unit": "$/ct", "fields": [
        ("natural round melee, up to 1.5 mm", "natural_diamond_melee_small", "GH/SI base"),
        ("natural round melee, 1.5 to 2.5 mm", "natural_diamond_melee_medium", "GH/SI base"),
        ("natural round melee, 2.5 to 4 mm", "natural_diamond_melee_large", "GH/SI base"),
        ("lab-grown round melee, up to 2.5 mm", "lab_grown_diamond_melee_small", ""),
        ("lab-grown round melee, 2.5 to 4 mm", "lab_grown_diamond_melee_large", ""),
        ("lab-grown diamond melee, any size", "lab_grown_diamond_melee", "used when no size is known"),
        ("natural diamond melee, any size", "natural_diamond_melee", "used when no size is known"),
        ("black diamond melee", "black_diamond_melee", ""),
        ("natural fancy-shape melee", "natural_fancy_diamond_melee", "baguette, princess, marquise, pear"),
        ("lab-grown fancy-shape melee", "lab_grown_fancy_diamond_melee", ""),
    ]},
    {"section": "colored stones per carat", "path": "pricing.stones_per_carat", "unit": "$/ct", "fields": [
        ("natural sapphire", "natural_sapphire", ""), ("lab-grown sapphire", "lab_grown_sapphire", ""),
        ("natural ruby", "natural_ruby", ""), ("lab-grown ruby", "lab_grown_ruby", ""),
        ("natural emerald", "natural_emerald", ""), ("lab-grown emerald", "lab_grown_emerald", ""),
        ("natural topaz", "natural_topaz", ""), ("natural amethyst", "natural_amethyst", ""), ("natural aquamarine", "natural_aquamarine", ""),
        ("natural garnet", "natural_garnet", ""), ("natural peridot", "natural_peridot", ""), ("natural citrine", "natural_citrine", ""),
        ("natural tsavorite", "natural_tsavorite", ""), ("natural tourmaline", "natural_tourmaline", ""),
    ]},
    {"section": "quality multipliers", "path": "pricing.multipliers", "unit": "x", "fields": [
        ("IJ / I1", "ij_i1", "against the GH/SI base"), ("HI / SI", "hi_si", ""), ("GH / SI", "gh_si", "the base, 1.0 if used"),
        ("GH / VS", "gh_vs", ""), ("FG / VS", "fg_vs", ""), ("EF / VS", "ef_vs", ""), ("EF / VVS", "ef_vvs", ""),
    ]},
    {"section": "setting labor per stone", "path": "pricing.setting_labor", "unit": "$/stone", "fields": [
        ("prong", "prong", ""), ("shared prong", "shared_prong", ""), ("pave", "pave", ""), ("micro pave", "micro_pave", ""),
        ("channel", "channel", ""), ("bezel", "bezel", ""), ("flush", "flush", ""), ("bead", "bead", ""),
        ("baguette channel", "baguette_channel", ""), ("fancy shape prong", "fancy_prong", ""),
    ]},
    {"section": "center stone setting", "path": "pricing.setting_labor", "unit": "$", "fields": [
        ("center under 0.50 ct", "center_under_0_50", ""), ("center 0.50 to 0.99 ct", "center_0_50_to_0_99", ""),
        ("center 1 to 1.99 ct", "center_1_to_1_99", ""), ("center 2 to 2.99 ct", "center_2_to_2_99", ""),
        ("center 3 to 4.99 ct", "center_3_to_4_99", ""), ("center 5 ct and up", "center_5_plus", ""),
        ("fancy shape, extra %", "fancy_shape_extra_pct", "percent on top"), ("bezel, extra %", "bezel_extra_pct", "percent on top"),
        ("fragile stone, extra %", "fragile_extra_pct", "emerald and the like; percent on top"),
    ]},
    {"section": "fees", "path": "pricing.fees", "unit": "$", "fields": [
        ("CAD, simple change", "cad_simple", ""), ("CAD, standard", "cad_standard", ""), ("CAD, complex", "cad_complex", ""),
        ("3D print", "print", ""), ("casting setup", "casting_setup", ""), ("casting per gram", "casting_per_gram", "$/g"),
        ("casting", "casting", "one flat casting fee, if that is how you charge"), ("setting", "setting", "one flat setting fee, if that is how you charge"),
        ("finishing, ring", "finish_ring", ""), ("finishing, pendant", "finish_pendant", ""), ("finishing, earrings", "finish_earrings", "per pair"),
        ("finishing, bracelet", "finish_bracelet", ""), ("engraving", "engraving", ""), ("rush", "rush", ""), ("shipping", "shipping", ""),
    ]},
    {"section": "labor", "path": "pricing", "unit": "$", "fields": [
        ("bench labor per hour", "bench_labor_per_hour", "the loaded productive hour"),
        ("minimum job charge", "minimum_job", "hard cost floor"),
        ("lab-grown diamond center, live quote from (ct)", "lab_center_live_quote_ct", "at or above this the desk asks you for the stone's price"),
    ]},
    {"section": "waste and contingency", "path": "pricing.allowances", "unit": "%", "fields": [
        ("metal waste %", "metal_waste_pct", ""), ("melee waste %", "melee_waste_pct", "extra stones ordered"),
        ("fragile stone waste %", "fragile_waste_pct", ""),
        ("contingency, simple %", "contingency_simple_pct", "before CAD"), ("contingency, normal %", "contingency_normal_pct", ""),
        ("contingency, complex %", "contingency_complex_pct", ""),
    ]},
    {"section": "typical finished weights", "path": "pricing.typical_finished_weights", "unit": "g", "fields": [
        ("ring", "ring", ""), ("band", "band", ""), ("pendant", "pendant", ""), ("earrings", "earrings", "per pair"),
        ("bracelet", "bracelet", ""), ("necklace", "necklace", ""),
    ]},
    {"section": "other", "path": "pricing.custom", "unit": "", "fields": []},
]
SECTIONS = {block["section"]: block for block in SCHEMA}
KEY_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+){0,5}")


def key_from_words(words: str) -> str:
    """'Lab-grown spinel' -> 'lab_grown_spinel'."""
    parts = re.findall(r"[a-z0-9]+", str(words or "").lower())
    return "_".join(parts[:6])


def _get(profile: dict[str, Any], path: str) -> Any:
    node: Any = profile
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set(profile: dict[str, Any], path: str, key: str, value: Any) -> None:
    node = profile
    for part in path.split("."):
        node = node.setdefault(part, {})
    if value is None:
        node.pop(key, None)
    else:
        node[key] = value


def _read_value(profile: dict[str, Any], block: dict[str, Any], key: str) -> Any:
    if key == "spot_enabled":
        spot = _get(profile, "pricing.spot_metal") or {}
        return "yes" if spot.get("enabled") else ""
    node = _get(profile, block["path"])
    return node.get(key) if isinstance(node, dict) else None


def _shown(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def rows_from_profile(profile: dict[str, Any]) -> list[list[str]]:
    """The whole tab, header first: every schema row, the profile's own extra keys, filled rows first in each section."""
    rows: list[list[str]] = [list(HEADERS)]
    stamps = (_get(profile, "pricing.rate_updates") or {}) if isinstance(_get(profile, "pricing.rate_updates"), dict) else {}
    for block in SCHEMA:
        section = block["section"]
        seen: set[str] = set()
        section_rows: list[list[str]] = []
        for label, key, notes in block["fields"]:
            seen.add(key)
            value = _read_value(profile, block, key)
            section_rows.append([section, label, key, _shown(value), block["unit"], notes, str(stamps.get(f"{block['path']}.{key}") or "")])
        node = _get(profile, block["path"])
        if isinstance(node, dict) and section != "pricing model" and section != "spot metal" and section != "labor":
            for key in sorted(node):
                if key in seen or key in ("rate_updates",) or not KEY_RE.fullmatch(str(key)) or isinstance(node[key], (dict, list)):
                    continue
                if block["path"] == "pricing" and key not in ("metal_factor", "minimum_job", "lab_center_live_quote_ct"):
                    continue  # the pricing block's own settings are not rates
                section_rows.append([section, key.replace("_", " "), key, _shown(node[key]), block["unit"], "added by you",
                                     str(stamps.get(f"{block['path']}.{key}") or "")])
        section_rows.sort(key=lambda r: (r[3] == "",))  # filled first, order kept otherwise
        rows.extend(section_rows)
    return rows


def profile_from_rows(rows: list[list[Any]], profile: dict[str, Any], source: str = "sheet") -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Read the Value column back: (updated profile, changes, errors). Blank clears; words where a number belongs are errors."""
    updated = json.loads(json.dumps(profile))
    changes: list[dict[str, Any]] = []
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    stamps = updated.setdefault("pricing", {}).setdefault("rate_updates", {})
    for row in rows[1:]:
        cells = [str(c) if c is not None else "" for c in row] + [""] * (len(HEADERS) - len(row))
        section, field, key, value = cells[0].strip().lower(), cells[1].strip(), cells[2].strip(), cells[3].strip()
        if not section and not field:
            continue
        block = SECTIONS.get(section)
        if block is None:
            block = SECTIONS["other"]
        if not key:
            key = key_from_words(field)
        if not key or not KEY_RE.fullmatch(key):
            if value:
                errors.append(f"{field or key}: I cannot make a key from that name")
            continue
        if key in block.get("text", set()):
            new: Any = value.strip().lower() or None
            if key == "spot_enabled":
                new = None if new is None else new in ("yes", "y", "true", "on", "1")
                current = bool((_get(updated, "pricing.spot_metal") or {}).get("enabled"))
                if new is not None and new != current:
                    updated.setdefault("pricing", {}).setdefault("spot_metal", {})["enabled"] = new
                    changes.append({"key": "pricing.spot_metal.enabled", "old": current, "new": new, "at": now, "source": source})
                continue
            if new is not None and new not in ("cost_plus_multiplier", "target_margin"):
                errors.append(f"{field}: {value!r} is not cost_plus_multiplier or target_margin")
                continue
            current = _get(updated, block["path"]).get(key) if isinstance(_get(updated, block["path"]), dict) else None
            if new is not None and new != current:
                _set(updated, block["path"], key, new)
                changes.append({"key": f"{block['path']}.{key}", "old": current, "new": new, "at": now, "source": source})
            continue
        if value:
            match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
            if not match or re.search(r"[a-z]{3,}", value.lower().replace("usd", "")):
                errors.append(f"{field}: {value!r} is not a number")
                continue
            number: Any = float(match.group(0))
            if value.strip().endswith("%") and block["unit"] != "%":
                number = number / 100.0
        else:
            number = None
        node = _get(updated, block["path"])
        current = node.get(key) if isinstance(node, dict) else None
        if number is None and current is None:
            continue
        if number is not None and current is not None and abs(float(current) - number) < 1e-9:
            continue
        _set(updated, block["path"], key, number)
        stamps[f"{block['path']}.{key}"] = now[:10]
        changes.append({"key": f"{block['path']}.{key}", "old": current, "new": number, "at": now, "source": source})
    return updated, changes, errors


def apply_values(profile: dict[str, Any], values: dict[str, Any], source: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Values keyed 'section/key' or by bare key (unique in the schema) onto the profile, through the same validation."""
    rows: list[list[str]] = [list(HEADERS)]
    index: dict[str, tuple[str, str]] = {}
    for block in SCHEMA:
        for label, key, _notes in block["fields"]:
            index.setdefault(key, (block["section"], label))
    for name, value in values.items():
        name = str(name)
        if "/" in name:
            section, key = name.split("/", 1)
            rows.append([section.strip().lower(), key.replace("_", " "), key_from_words(key), _shown(value), "", "", ""])
        elif name in index:
            section, label = index[name]
            rows.append([section, label, name, _shown(value), "", "", ""])
        else:
            rows.append(["other", name.replace("_", " "), key_from_words(name), _shown(value), "", "", ""])
    return profile_from_rows(rows, profile, source)


def journal(workspace: Path, changes: list[dict[str, Any]]) -> None:
    if not changes:
        return
    path = Path(workspace) / "estimate-desk" / "run-work" / "rates-journal.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        entries = []
    entries = (entries + changes)[-500:]
    path.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
