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
    # Do not reject ordinary customer language such as "I assume Friday works."
    # Reject an assumptions section and assumptions explicitly tied to costing.
    re.compile(r"(?:^|\n)\s*(?:pricing\s+|cost(?:ing)?\s+)?assumptions?\s*:", re.I),
    re.compile(
        r"\b(?:pricing|cost(?:ing)?|jeweler(?:['’]s)?)\s+assumptions?\b|"
        r"\bassum(?:e|ed|ing)\b[^.\n]{0,40}\b(?:cost|rate|price|weight)\b|"
        r"\b(?:cost|rate|price|weight)\b[^.\n]{0,40}\bassum(?:e|ed|ing)\b",
        re.I,
    ),
    re.compile(r"\b(?:cogs|cost basis|our costs?|jeweler(?:['’]s)? costs?)\b", re.I),
    re.compile(r"\b(?:wholesale|trade)\s+cost\s+to\s+us\b", re.I),
    re.compile(
        r"\b(?:the\s+)?(?:price|amount)\s+we\s+(?:pay|paid)\b|\bwhat\s+we\s+paid\b",
        re.I,
    ),
    re.compile(
        r"\bwe\s+(?:paid|purchased|bought)\b[^.\n]{0,50}\bfor\s+\$\s*[\d,.]+",
        re.I,
    ),
    re.compile(r"\b(?:scrap|melt)(?:\s*/\s*(?:scrap|melt))?\s+value\b", re.I),
    re.compile(r"\b(?:markup|margin)\b", re.I),
    re.compile(
        r"\b(?:pricing|markup|price)\s+multiplier\b|\bmultiplier\b[^.\n]{0,30}\b(?:base|cost|price)\b",
        re.I,
    ),
    re.compile(r"\b(?:bench labor|labor rate|component costs?)\b", re.I),
    re.compile(
        r"\bper[- ]?(?:gram|carat|ounce|oz|pennyweight|dwt)\b|"
        r"\$(?:\s*[\d,.]+)?\s*/\s*(?:g|ct|oz|dwt)\b",
        re.I,
    ),
    # A generic mention of a manufacturer or supplier can be customer-safe
    # (for example, a warranty). Block identities and internal relationships.
    re.compile(
        r"\b(?:our\s+)?(?:vendor|manufacturer|supplier)(?:\s+name)?\s*(?:is|:)|"
        r"\bour\s+(?:vendor|manufacturer|supplier)\b|"
        r"\b(?:vendor|manufacturer|supplier)\s+(?:identity|cost|rate|price|quote)\b",
        re.I,
    ),
    re.compile(r"\b(?:jeweler(?:['’]s)?|bench)\s+(?:fee|charge)\b", re.I),
    re.compile(
        r"\b(?:metal|stone|diamond|casting|setting|finishing|engraving)\s+"
        r"(?:cost|rate|price|fee|charge)\b",
        re.I,
    ),
)

DOLLAR_AMOUNT_RE = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
CUSTOMER_TERMINOLOGY_PATTERNS = (re.compile(r"\bCAD\b", re.I),)


def validate_customer_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("customer-facing text must not be empty")
    for pattern in CONFIDENTIAL_PRICING_PATTERNS:
        if pattern.search(text):
            raise ValueError(
                "customer-facing text contains owner-only pricing information"
            )
    for pattern in CUSTOMER_TERMINOLOGY_PATTERNS:
        if pattern.search(text):
            raise ValueError(
                "customer-facing text must use design or visual rendering language"
            )
    return text


def validate_approved_price(text: str, approved_price: float) -> str:
    """Require every customer-visible dollar amount to equal the approved price."""
    validate_customer_text(text)
    if isinstance(approved_price, bool) or not isinstance(approved_price, (int, float)):
        raise ValueError("approved price must be numeric")
    amounts = [
        float(match.replace(",", "")) for match in DOLLAR_AMOUNT_RE.findall(text)
    ]
    if not amounts:
        raise ValueError("customer estimate must include the approved dollar price")
    if any(abs(amount - float(approved_price)) > 0.01 for amount in amounts):
        raise ValueError(
            "customer estimate contains a dollar amount other than the approved price"
        )
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
        print(
            json.dumps({"error": str(exc), "safe": False}, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
