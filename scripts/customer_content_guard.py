#!/usr/bin/env python3
"""Reject owner-only jeweler pricing information in customer-facing text."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# These phrases identify owner-only pricing logic, not ordinary customer-safe
# product specifications. This is deliberately fail-closed: a rejected draft
# must be rewritten without the confidential language before it can be sent.
CONFIDENTIAL_PRICING_PATTERNS = (
    re.compile(r"\bassum(?:e|ed|ing|ption|ptions)\b", re.I),
    re.compile(r"\b(?:cogs|cost basis|our costs?|jeweler(?:'s)? costs?)\b", re.I),
    re.compile(r"\b(?:markup|margin|vendor|manufacturer)\b", re.I),
    re.compile(r"\b(?:bench labor|labor rate|component costs?)\b", re.I),
    re.compile(r"\b(?:per gram|per carat|\$/g|\$/ct)\b", re.I),
    re.compile(
        r"\b(?:metal|stone|diamond|casting|setting|finishing|engraving)\s+"
        r"(?:cost|rate|price)\b",
        re.I,
    ),
)


def validate_customer_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("customer-facing text must not be empty")
    for pattern in CONFIDENTIAL_PRICING_PATTERNS:
        if pattern.search(text):
            raise ValueError("customer-facing text contains owner-only pricing information")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text_file", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_customer_text(args.text_file.read_text(encoding="utf-8"))
        print(json.dumps({"safe": True}, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "safe": False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
