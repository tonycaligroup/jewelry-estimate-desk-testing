#!/usr/bin/env python3
"""The jeweler's own pricing model, read into the rate card (the owner, 9 September 2026).

At setup the desk asks for whatever the shop has: a spreadsheet exported as
CSV, a PDF, a text file, or a pasted paragraph. One model call reads it into
the desk's schema (rate_card.SCHEMA), numbers only; the desk fills what it
can match, keeps anything else under "other" with the jeweler's own label,
writes the profile, journals every value, and says what it filled and what
stayed blank. Nothing is invented: a rate the document does not state stays
blank for the jeweler to fill on the Rates tab.

    python3 scripts/rates_intake.py --workspace <ws> --file <path>
    python3 scripts/rates_intake.py --workspace <ws> --text "14k yellow gold $65/g, bench $90/hr, ..."
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import judge
import rate_card

MAX_CHARS = 60000


def read_document(path: Path) -> str:
    """Plain text from a text, CSV, or PDF file; other kinds are refused with a plain reason."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".json", ".tsv"):
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8", errors="replace"))))
        return "\n".join(" | ".join(c.strip() for c in row) for row in rows)[:MAX_CHARS]
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise ValueError("reading a PDF needs pypdf (pip install pypdf); or export it as text or CSV") from exc
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_CHARS]
    raise ValueError(f"{path.name}: I can read text, CSV, and PDF files; export a spreadsheet as CSV, or paste the numbers as text")


def _schema_lines() -> str:
    lines = []
    for block in rate_card.SCHEMA:
        for label, key, notes in block["fields"]:
            unit = block["unit"] or ("text" if key in block.get("text", set()) else "")
            lines.append(f"- {key}: {label} ({block['section']}{', ' + unit if unit else ''}){'; ' + notes if notes else ''}")
    return "\n".join(lines)


def check_values(value: dict[str, Any]) -> dict[str, Any]:
    found = value.get("values")
    if not isinstance(found, dict):
        raise ValueError("values must be an object of key to number")
    out: dict[str, Any] = {}
    known = {key for block in rate_card.SCHEMA for _l, key, _n in block["fields"]}
    for key, number in found.items():
        key = str(key).strip()
        if key in ("model",):
            if str(number).strip().lower() in ("cost_plus_multiplier", "target_margin"):
                out[key] = str(number).strip().lower()
            continue
        if key == "spot_enabled":
            out[key] = "yes" if str(number).strip().lower() in ("yes", "true", "on", "1") else "no"
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            continue
        if key not in known and "/" not in key:
            key = "other/" + key
        out[key] = float(number)
    unknown = value.get("unmatched")
    return {"values": out, "unmatched": [str(u)[:120] for u in unknown][:40] if isinstance(unknown, list) else []}


def parse(text: str, model: str | None = None, runner=subprocess.run, openclaw: str | None = None) -> dict[str, Any]:
    """One model call: the document's numbers onto the schema keys; anything it cannot place is listed, never guessed."""
    prompt = (
        "You read a jewelry shop's own pricing notes into a fixed rate card. Map every stated rate onto the keys "
        "below, numbers only (no currency signs), in the unit the key names; convert a percentage markup to a "
        "multiplier (25% -> 1.25) and a target margin to a decimal (30% -> 0.3). Never invent or estimate a number "
        "the notes do not state; a key the notes do not cover is simply absent. A rate the notes state that fits no "
        "key goes under a new key of your own in the same style (lowercase words joined by underscores) prefixed "
        "with its section and a slash, for example \"colored stones per carat/lab_grown_spinel\" or "
        "\"fees/laser_welding\". List in unmatched any line you could not place. Answer with one JSON object only: "
        '{"values": {"<key>": <number>, ...}, "unmatched": ["<line>", ...]}.\n\nKEYS:\n' + _schema_lines()
        + "\n\nTHE SHOP'S PRICING NOTES:\n" + text[:MAX_CHARS]
    )
    return judge.ask_json(prompt, check_values, model, runner, openclaw)


def intake(workspace: Path, text: str, model: str | None = None, runner=subprocess.run, openclaw: str | None = None) -> dict[str, Any]:
    profile_path = Path(workspace) / "estimate-desk" / "shop-profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    read = parse(text, model, runner, openclaw)
    updated, changes, errors = rate_card.apply_values(profile, read["values"], "intake")
    import validate_profile

    problems = [e for e in (validate_profile.validate_profile(updated).get("errors") or []) if str(e).startswith("pricing.")]
    if problems:
        return {"outcome": "refused", "errors": errors + problems, "read": read}
    profile_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
    rate_card.journal(workspace, changes)
    blank = [key for block in rate_card.SCHEMA for _l, key, _n in block["fields"]
             if key not in read["values"] and rate_card._read_value(updated, block, key) in (None, "")]
    return {"outcome": "filled", "filled": [c["key"] for c in changes], "still_blank": blank, "unmatched": read["unmatched"], "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--file", type=Path, default=None)
    parser.add_argument("--text", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)
    if not args.file and not args.text:
        parser.error("give --file or --text")
    try:
        text = read_document(args.file) if args.file else str(args.text)
        result = intake(args.workspace.resolve(), text, args.model)
    except (OSError, ValueError, judge.JudgmentError) as exc:
        print(json.dumps({"outcome": "failed", "error": str(exc)[:300]}))
        return 1
    print(json.dumps(result, indent=1))
    return 0 if result.get("outcome") == "filled" else 1


if __name__ == "__main__":
    sys.exit(main())
