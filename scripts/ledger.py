#!/usr/bin/env python3
"""The estimate ledger: every fact with its source, in SQLite (RELEASE-PLAN-4.15.md).

A record used to hold one specification that the model rewrote on every
message, with no memory of where a fact came from; a stated fact could be
re-read into something else, a photo's reading vanished with the claim's
scratch folder, and "asked once" was an inference. The ledger is append-only
rows in `estimate-desk/ledger.sqlite`: field, piece, stone, value, source
(customer, photo, jeweler, owner, reading), the message and the words that
support it, and which message asked and which answered. The record's
`specification` is derived from the winning rows, so every consumer keeps
its shape.

Precedence: the owner outranks everyone; the customer's written word
outranks a photo, a jeweler's choice, or a reading; among rows of one rank
the newest wins. A photo or a reading never replaces a customer row.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_NAME = "ledger.sqlite"
SOURCES = ("owner", "quoted", "customer", "jeweler", "photo", "reading")
# quoted: a fact the customer saw on a sent estimate; only a change they name (the classifier's changed
# fields, or the owner's "change" answer) may move it, never a re-read that happens to find the word.
# quoted and customer share a rank: absorb() only writes a customer row over a quoted one for a change the
# customer named, and then the newer row wins.
RANK = {"owner": 5, "quoted": 4, "customer": 4, "jeweler": 2, "photo": 1, "reading": 0}
METAL_FAMILY = {"metal", "metal_karat", "metal_color"}
JEWELERS_CHOICE = "jeweler's choice"
# Keys that describe the piece, not a fact about it: kept as readings, never protected or asked.
LOOSE_KEYS = {"notes", "reference_images", "scheduling_intent", "pieces"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    estimate_id TEXT NOT NULL,
    field TEXT NOT NULL,
    piece INTEGER,
    stone TEXT,
    value TEXT,
    source TEXT NOT NULL,
    gmail_message_id TEXT,
    span TEXT,
    asked_in TEXT,
    answered_in TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_estimate ON facts(estimate_id);
"""


def path(desk: Path) -> Path:
    return Path(desk) / DB_NAME


def connect(desk: Path) -> sqlite3.Connection:
    Path(desk).mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(str(path(desk)), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript(SCHEMA)
    try:
        path(desk).chmod(0o600)
    except OSError:
        pass
    return connection


def stone_of(field: str) -> str | None:
    """Which stone a flat key describes: stone_* is the center stone, accent_* the accents, else none."""
    if field.startswith("stone_") or field == "center_stone":
        return "center"
    if field.startswith("accent_"):
        return "accent"
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _decode(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _present(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if value in (None, "", [], {}):
        return False
    return not (isinstance(value, str) and value.strip().lower() in ("", "n/a", "unknown", "unspecified", "tbd", "none", "null"))


def add_facts(desk: Path, estimate_id: str, rows: Iterable[dict[str, Any]]) -> int:
    """Append rows; each needs field and value, the rest defaults (source reading, now)."""
    count = 0
    with connect(desk) as connection:
        for row in rows:
            source = str(row.get("source") or "reading")
            if source not in SOURCES:
                raise ValueError(f"unknown source {source!r}")
            connection.execute(
                "INSERT INTO facts (estimate_id, field, piece, stone, value, source, gmail_message_id, span, asked_in, answered_in, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (estimate_id, str(row["field"]), row.get("piece"), row.get("stone", stone_of(str(row["field"]))),
                 _encode(row.get("value")) if "value" in row else None, source, row.get("gmail_message_id"),
                 row.get("span"), row.get("asked_in"), row.get("answered_in"), row.get("at") or _now()),
            )
            count += 1
    return count


def rows(desk: Path, estimate_id: str) -> list[dict[str, Any]]:
    if not path(desk).exists():
        return []
    with connect(desk) as connection:
        found = connection.execute("SELECT * FROM facts WHERE estimate_id = ? ORDER BY id", (estimate_id,)).fetchall()
    return [{**dict(r), "value": _decode(r["value"])} for r in found]


def has_rows(desk: Path, estimate_id: str) -> bool:
    if not path(desk).exists():
        return False
    with connect(desk) as connection:
        return connection.execute("SELECT 1 FROM facts WHERE estimate_id = ? AND value IS NOT NULL LIMIT 1", (estimate_id,)).fetchone() is not None


def winning(desk: Path, estimate_id: str) -> dict[tuple[str, int | None], dict[str, Any]]:
    """The row that stands for each (field, piece): the highest rank, then the newest."""
    best: dict[tuple[str, int | None], dict[str, Any]] = {}
    for row in rows(desk, estimate_id):
        if row.get("value") is None and row.get("asked_in") and not row.get("answered_in"):
            continue  # an open ask is not a value
        if row.get("value") is None:
            continue
        key = (row["field"], row.get("piece"))
        current = best.get(key)
        if current is None or RANK[row["source"]] >= RANK[current["source"]]:
            best[key] = row
    return best


def specification(desk: Path, estimate_id: str, current: dict[str, Any] | None = None) -> dict[str, Any]:
    """The derived specification: winning rows flattened into the shape every consumer expects.

    `current` is the reading of the message being handled; its loose keys
    (notes, references, a meeting request) ride along, since they belong to
    the message and are never ledger rows.
    """
    spec: dict[str, Any] = {}
    pieces: dict[int, dict[str, Any]] = {}
    for (field, piece), row in winning(desk, estimate_id).items():
        if piece is None:
            spec[field] = row["value"]
        else:
            pieces.setdefault(int(piece), {})[field] = row["value"]
    if pieces:
        spec["pieces"] = [pieces.get(i, {}) for i in range(max(pieces) + 1)]
    for key in LOOSE_KEYS - {"pieces"}:
        if isinstance(current, dict) and _present(current.get(key)):
            spec[key] = current[key]
    return spec


def source_of(value: Any, own_words: str, photo_text: str) -> tuple[str, str | None]:
    """Where a value comes from: the customer's own words (with the span), the photo's reading, the jeweler, or a reading."""
    if isinstance(value, str) and value.strip().lower() == JEWELERS_CHOICE:
        return "jeweler", None
    text = _value_text(value)
    if not text:
        return "reading", None
    span = _find(text, own_words)
    if span:
        return "customer", span
    if _find(text, photo_text):
        return "photo", None
    return "reading", None


def _value_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


_STOP = {"the", "a", "an", "and", "or", "of", "with", "in", "on", "for", "to", "gold", "stone", "stones", "diamond", "diamonds"}


def _find(text: str, words: str) -> str | None:
    """The value, or its distinctive word, as it appears in the words; None when it does not."""
    haystack = " " + re.sub(r"\s+", " ", str(words or "").lower()) + " "
    needle = re.sub(r"\s+", " ", text.lower()).strip()
    if not needle or len(haystack) < 3:
        return None
    numeric = re.fullmatch(r"[0-9.]+", needle) is not None
    boundary = (r"(?<![0-9])", r"(?![0-9])") if numeric else (r"(?<![a-z0-9])", r"(?:s|es)?(?![a-z])")  # "emerald" in "emeralds"
    if re.search(boundary[0] + re.escape(needle) + boundary[1], haystack):
        return text
    # "18" in "18k", "white gold" in "18k WG": the distinctive tokens of the value.
    tokens = [t for t in re.findall(r"[a-z0-9.]+", needle) if t not in _STOP and len(t) > 1]
    if not tokens:
        return None
    hits = [t for t in tokens if re.search((r"(?<![0-9])" if re.fullmatch(r"[0-9.]+", t) else r"(?<![a-z0-9])") + re.escape(t)
                                           + (r"(?![0-9])" if re.fullmatch(r"[0-9.]+", t) else r"(?:s|es)?(?![a-z])"), haystack)]
    if len(hits) == len(tokens):
        return " ".join(hits)
    return None


def absorb(desk: Path, estimate_id: str, spec: dict[str, Any], message_id: str | None, own_words: str,
           photo_text: str = "", default_source: str = "reading", changeable: set[str] | None = None) -> list[dict[str, Any]]:
    """Record a reading as rows, each with the source its value supports; a customer row is never overwritten by less.

    Returns the rows added. A value equal to the winning one adds nothing.
    A value that differs from a standing customer row is recorded only when
    the new words say it (the customer's written word takes precedence).
    """
    if not isinstance(spec, dict):
        return []
    standing = winning(desk, estimate_id)
    added: list[dict[str, Any]] = []
    now = _now()

    def consider(field: str, value: Any, piece: int | None) -> None:
        if field in LOOSE_KEYS or not _present(value):
            return  # notes, references, and a meeting request belong to the message, not the ledger
        current = standing.get((field, piece))
        if current is not None and current["value"] == value:
            return
        source, span = source_of(value, own_words, photo_text)
        if source == "reading":
            source = default_source
        if current is not None and current["source"] == "quoted" and source == "customer":
            if not changeable or (ANY_FIELD not in changeable and field not in changeable):
                return  # a quoted fact moves only for a change the customer named
            source = "customer"
        elif current is not None and RANK[current["source"]] > RANK[source]:
            return  # a lesser source never replaces what stands
        if current is not None and current["source"] == "customer" and source == "customer" and span is None:
            return
        row = {"field": field, "piece": piece, "stone": stone_of(field), "value": value, "source": source,
               "gmail_message_id": message_id, "span": span, "at": now}
        added.append(row)

    for field, value in spec.items():
        if field == "pieces":
            continue
        consider(field, value, None)
    raw = spec.get("pieces")
    if isinstance(raw, list):
        for index, piece in enumerate(raw):
            if isinstance(piece, dict):
                for field, value in piece.items():
                    if field != "pieces":
                        consider(field, value, index)
    if added:
        add_facts(desk, estimate_id, added)
    return added


def quote(desk: Path, estimate_id: str, spec: dict[str, Any], message_id: str | None) -> int:
    """The estimate went out: every fact on it is quoted, and stands until the customer names a change.

    Written outright, whatever stood before: a fact the customer confirmed
    by receiving the estimate outranks the reading that first found it.
    """
    if not isinstance(spec, dict):
        return 0
    now = _now()
    rows_to_add: list[dict[str, Any]] = []

    def take(field: str, value: Any, piece: int | None) -> None:
        if field in LOOSE_KEYS or not _present(value):
            return
        rows_to_add.append({"field": field, "piece": piece, "stone": stone_of(field), "value": value, "source": "quoted",
                            "gmail_message_id": message_id, "span": None, "at": now})

    for field, value in spec.items():
        if field != "pieces":
            take(field, value, None)
    raw = spec.get("pieces")
    if isinstance(raw, list):
        for index, piece in enumerate(raw):
            if isinstance(piece, dict):
                for field, value in piece.items():
                    if field != "pieces":
                        take(field, value, index)
    return add_facts(desk, estimate_id, rows_to_add)


def migrate(desk: Path, record: dict[str, Any]) -> int:
    """A record from before the ledger, once: the last sent estimate's facts as quoted rows, the current ones as readings."""
    estimate_id = str(record.get("estimate_id") or "")
    if not estimate_id or has_rows(desk, estimate_id):
        return 0
    message_id = (record.get("route") or {}).get("gmail_message_id")
    count = 0
    history = record.get("estimate_history") or []
    archived = history[-1].get("specification") if history and isinstance(history[-1], dict) else None
    if isinstance(archived, dict) and archived:
        count += quote(desk, estimate_id, archived, message_id)
    spec = record.get("specification") if isinstance(record.get("specification"), dict) else {}
    if spec:
        count += len(absorb(desk, estimate_id, spec, message_id, "", "", default_source="reading"))
    return count


ANY_FIELD = "*"


def changeable_fields(record: dict[str, Any]) -> set[str]:
    """The quoted fields the customer may move: what the newest post-estimate review says changed.

    A reopen archives the reviews with the sent estimate, so the last
    archived reviews count too. A record reopened for a change with no
    fields named (the owner said "change" on an unclear reply) lets any
    field the customer's words state move.
    """
    reviews = [r for r in (record.get("thread_reviews") or []) if isinstance(r, dict)]
    history = record.get("estimate_history") or []
    if record.get("reopened_for") == "design_change" and history and isinstance(history[-1], dict):
        reviews = [r for r in (history[-1].get("thread_reviews") or []) if isinstance(r, dict)] + reviews
    named = [r for r in reviews if r.get("changed_fields")]
    if named:
        fields = {str(name).split(".")[-1] for name in named[-1]["changed_fields"]}
        if fields & METAL_FAMILY:
            fields |= METAL_FAMILY  # "18k rose gold" moves the metal, its karat, and its colour together
        return fields
    if record.get("reopened_for") == "design_change":
        return {ANY_FIELD}
    return set()


def mark_asked(desk: Path, estimate_id: str, fields: Iterable[str], message_id: str) -> int:
    """The desk asked the customer for these fields in this email."""
    return add_facts(desk, estimate_id, [
        {"field": f, "piece": None, "stone": stone_of(f), "asked_in": message_id, "source": "reading", "at": _now()} for f in fields
    ])


def open_asks(desk: Path, estimate_id: str) -> list[str]:
    """Fields the desk asked for that no later row answered."""
    asked: dict[str, str] = {}
    answered: set[str] = set()
    for row in rows(desk, estimate_id):
        if row.get("asked_in") and row.get("value") is None:
            asked[row["field"]] = row["asked_in"]
        elif row.get("value") is not None and row["field"] in asked and row.get("at", "") >= "":
            answered.add(row["field"])
    return sorted(f for f in asked if f not in answered)


def delete_estimate(desk: Path, estimate_id: str) -> int:
    if not path(desk).exists():
        return 0
    with connect(desk) as connection:
        return connection.execute("DELETE FROM facts WHERE estimate_id = ?", (estimate_id,)).rowcount


def delete_all(desk: Path) -> int:
    if not path(desk).exists():
        return 0
    with connect(desk) as connection:
        return connection.execute("DELETE FROM facts").rowcount


def describe_source(row: dict[str, Any]) -> str:
    """The source in the owner's words, for a card or the sheet."""
    source = row.get("source")
    if source == "customer":
        return f"customer wrote: {row.get('span')}" if row.get("span") else "customer"
    return {"photo": "from the photo", "jeweler": "jeweler's choice", "owner": "owner's decision",
            "quoted": "quoted in the estimate"}.get(str(source), "read from the thread")
