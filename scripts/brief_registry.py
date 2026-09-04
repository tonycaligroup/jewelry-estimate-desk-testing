#!/usr/bin/env python3
"""Which Kolo briefs the desk filed, and what became of them.

Kolo hands an approved brief to the main session but says nothing about a
rejected one. The audit trail does record both (`brief.submitted` when the
desk files a card, `brief.rejected` when the owner rejects it), so the desk
notes each card's brief id at filing time and the watcher polls the trail
every tick for rejections of cards it filed (WORKFLOW.md 6.10: a rejected
appointment card asks the owner what to do).
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import kolo_safe

KINDS = {"price", "rendering", "appointment"}


def root_for(monitor_root: Path) -> Path:
    return monitor_root.resolve().parent / "briefs"


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def register(
    monitor_root: Path, kind: str, action_title: str, estimate_id: str, message_id: str,
    runner: Callable[..., Any] | None = None, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Find the brief Kolo just created for this card and remember it.

    `kolo request-approval` prints nothing useful, so the id comes from the
    newest `brief.submitted` audit event whose description is this card's
    title. Returns None when the trail has not caught up yet; the next tick's
    poll cannot match such a card, so the owner's words remain the fallback.
    """
    if kind not in KINDS:
        raise ValueError("unsupported brief kind")
    current = now or datetime.now(timezone.utc)
    since = (current - timedelta(minutes=10)).isoformat()
    events = kolo_safe.audit_events(event_type="brief.submitted", from_date=since, runner=runner)
    match = next(
        (e for e in events if str(e.get("description") or "")[:120] == action_title[:120]), None,
    )
    if match is None or not match.get("brief_id"):
        return None
    entry = {
        "brief_id": match["brief_id"],
        "brief_number": match.get("brief_number"),
        "kind": kind,
        "estimate_id": estimate_id,
        "message_id": message_id,
        "action_title": action_title[:120],
        "filed_at": match.get("created_at") or current.isoformat(),
        "outcome": "pending",
    }
    _write(root_for(monitor_root) / f"{match['brief_id']}.json", entry)
    return entry


def load_all(monitor_root: Path) -> list[dict[str, Any]]:
    root = root_for(monitor_root)
    if not root.is_dir():
        return []
    entries = []
    for path in sorted(root.glob("*.json")):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and entry.get("brief_id"):
            entries.append(entry)
    return entries


def mark(monitor_root: Path, brief_id: str, outcome: str, note: str | None = None) -> None:
    path = root_for(monitor_root) / f"{brief_id}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    entry["outcome"] = outcome
    entry["decided_at"] = datetime.now(timezone.utc).isoformat()
    if note:
        entry["note"] = note[:400]
    _write(path, entry)


def watermark_path(monitor_root: Path) -> Path:
    return root_for(monitor_root) / "rejections-watermark.json"


def rejected_since_last_poll(
    monitor_root: Path, runner: Callable[..., Any] | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Rejections of cards the desk filed, new since the previous poll."""
    current = now or datetime.now(timezone.utc)
    path = watermark_path(monitor_root)
    try:
        since = json.loads(path.read_text(encoding="utf-8"))["since"]
    except (OSError, ValueError, KeyError):
        since = (current - timedelta(hours=6)).isoformat()
    pending = {e["brief_id"]: e for e in load_all(monitor_root) if e.get("outcome") == "pending"}
    if not pending:
        _write(path, {"since": current.isoformat()})
        return []
    events = kolo_safe.audit_events(event_type="brief.rejected", from_date=since, runner=runner)
    found = []
    for event in events:
        entry = pending.get(event.get("brief_id"))
        if entry is None:
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        found.append({**entry, "note": str(details.get("note") or "")[:400], "rejected_at": event.get("created_at")})
    # Poll overlap of one minute so a slow trail is not missed; marks keep repeats harmless.
    _write(path, {"since": (current - timedelta(minutes=1)).isoformat()})
    return found
