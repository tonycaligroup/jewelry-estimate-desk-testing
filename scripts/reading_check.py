#!/usr/bin/env python3
"""The model's reading of a customer, cross-checked against the customer's own words.

ARCHITECTURE-OPTIONS.md E' (built 6 September 2026). No model call: the
check is code, so two calls to the same model cannot misread the same way.
It compares only what the text states plainly (a count of ring sizes, a
carat figure, a word like "lab-grown" or "my grandmother's") with what the
reading holds. A disagreement is never priced; it becomes one confirming
line in the follow-up (WORKFLOW.md 6.2: the gate asks, nothing is invented).
Absence of a word never counts: a customer who names no origin is asked for
it by the gate as before.
"""

from __future__ import annotations

import re
from typing import Any

import estimate_record

PREFIX = "confirm."

# The customer-facing question for each disagreement, used by the plain
# follow-up and offered to the drafting model as the bullet to write.
QUESTIONS = {
    "piece_count": "you mentioned more than one size or piece; could you confirm how many pieces you would like quoted, and which size goes with which?",
    "finger_size": "could you confirm the ring size, since we read one size and your message mentions another?",
    "stone_carat": "could you confirm the carat weight of the center stone?",
    "stone_origin": "could you confirm whether you would like a lab-grown or a natural stone?",
    "customer_stone": "you mentioned a stone of your own; could you confirm you would like us to set that stone rather than supply one?",
}

_SIZE_RE = re.compile(r"\bsize\s*(?:of\s*)?(\d{1,2}(?:\.\d)?|\d{1,2}\s*[½¼¾]|\d{1,2}\s*1/2)\b", re.I)
_SIZE_RE2 = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*(?:ring\s*)?size\b", re.I)
_CARAT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:ct|cts|carat|carats)\b", re.I)
_LAB_WORDS = ("lab-grown", "lab grown", "labgrown", "lab created", "lab-created", "lab diamond", "moissanite", "created diamond")
_NATURAL_WORDS = ("natural diamond", "natural stone", "mined diamond", "earth-mined", "earth mined", "natural, not lab", "real diamond, not lab")
_OWN_STONE_WORDS = estimate_record.SUPPLIED_STONE_WORDS


_QUOTE_START_RE = re.compile(r"^\s*(on .{0,200}wrote:|-{2,}\s*original message\s*-{2,}|from:\s.*)$", re.I)


def own_words(body: str) -> str:
    """The customer's own lines: nothing quoted from an earlier email counts."""
    kept: list[str] = []
    for line in str(body or "").splitlines():
        if _QUOTE_START_RE.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _customer_text(digest: dict[str, Any]) -> str:
    parts = []
    for message in digest.get("messages") or []:
        if message.get("sent_by") == "shop":
            continue
        parts.append(own_words(message.get("body") or ""))
    return "\n".join(parts).lower()


def _number(value: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else None


def _sizes_in(text: str) -> set[str]:
    found = set()
    for match in _SIZE_RE.finditer(text):
        found.add(_normal_size(match.group(1)))
    for match in _SIZE_RE2.finditer(text):
        found.add(_normal_size(match.group(1)))
    return {s for s in found if s}


def _normal_size(raw: str) -> str:
    raw = raw.replace("½", ".5").replace("¼", ".25").replace("¾", ".75").replace("1/2", ".5").replace(" ", "")
    number = _number(raw)
    return f"{number:g}" if number is not None else ""


def compare(digest: dict[str, Any], specification: dict[str, Any]) -> list[dict[str, str]]:
    """Disagreements between the customer's words and the reading, each with its question."""
    text = _customer_text(digest)
    if not text.strip():
        return []
    pieces = estimate_record.pieces_of(specification or {})
    out: list[dict[str, str]] = []

    def add(topic: str, said: str, read: str) -> None:
        out.append({"name": PREFIX + topic, "topic": topic, "said": said, "read": read, "question": QUESTIONS[topic]})

    # Sizes: two distinct sizes in the text and one piece in the reading is
    # the ring-and-band case; one size in the text that differs from the
    # reading is a misread digit.
    sizes = _sizes_in(text)
    read_sizes = {f"{n:g}" for n in (_number(p.get("finger_size")) for p in pieces) if n is not None}
    if len(sizes) >= 2 and len(pieces) == 1:
        add("piece_count", "sizes " + ", ".join(sorted(sizes, key=float)), f"one piece, size {', '.join(read_sizes) or 'unknown'}")
    elif len(sizes) == 1 and read_sizes and not sizes & read_sizes:
        add("finger_size", "size " + next(iter(sizes)), "size " + ", ".join(read_sizes))

    # Carats: a figure in the text that no piece's center stone carries.
    carats = {f"{float(m.group(1)):g}" for m in _CARAT_RE.finditer(text)}
    read_carats = {f"{n:g}" for n in (_number(p.get("stone_carat")) for p in pieces) if n is not None}
    if carats and read_carats and not carats & read_carats:
        add("stone_carat", ", ".join(sorted(carats, key=float)) + " ct", ", ".join(sorted(read_carats, key=float)) + " ct")

    # Origin: a plain lab or natural word against the opposite reading.
    origins = {str(p.get("stone_origin") or "").lower() for p in pieces}
    says_lab = any(w in text for w in _LAB_WORDS)
    says_natural = any(w in text for w in _NATURAL_WORDS)
    if says_lab and not says_natural and origins and all(o and "lab" not in o and "moissanite" not in o for o in origins):
        add("stone_origin", "lab-grown", ", ".join(sorted(o for o in origins if o)))
    elif says_natural and not says_lab and origins and all(o and ("lab" in o or "moissanite" in o) for o in origins):
        add("stone_origin", "natural", ", ".join(sorted(o for o in origins if o)))

    # A stone the customer already owns, read as one the shop supplies.
    if any(w in text for w in _OWN_STONE_WORDS) and not estimate_record.customer_supplies_stone(specification or {}):
        add("customer_stone", "a stone of their own", "a stone the shop supplies")
    return out


def names(disagreements: list[dict[str, str]]) -> list[str]:
    return [d["name"] for d in disagreements]


def is_confirm(name: str) -> bool:
    return isinstance(name, str) and name.startswith(PREFIX)


def question_for(name: str) -> str | None:
    return QUESTIONS.get(name[len(PREFIX):]) if is_confirm(name) else None
