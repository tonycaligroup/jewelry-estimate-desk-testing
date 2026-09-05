#!/usr/bin/env python3
"""One run of a command at a time (RELIABILITY-PLAN.md 3.1).

The same execute line pasted twice, or a retry from `answer-question`
while the first paste is still running, must not both reach the calendar
or the mailbox. A lease is an exclusive create of a small lock file with a
token and an expiry; a live lease refuses the second run, a lapsed one
(the first run died) is taken over.
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_SECONDS = 600


def lock_path(desk: Path, command: str, key: str) -> Path:
    safe_key = "".join(c for c in key if c.isalnum() or c in "-_")[:32] or "none"
    return desk / "locks" / f"{command}-{safe_key}.lock"


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _expired(lease: dict | None, now: datetime) -> bool:
    if not lease:
        return True
    try:
        return datetime.fromisoformat(str(lease.get("expires_at"))) <= now
    except (TypeError, ValueError):
        return True


@contextmanager
def hold(desk: Path, command: str, key: str, seconds: int = DEFAULT_SECONDS) -> Iterator[str]:
    """Hold the lease for the body; refuse while another live run holds it."""
    path = lock_path(desk, command, key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now(timezone.utc)
    token = secrets.token_hex(8)
    lease = {"token": token, "command": command, "key": key,
             "started_at": now.isoformat(), "expires_at": (now + timedelta(seconds=seconds)).isoformat()}
    body = json.dumps(lease, sort_keys=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read(path)
        if not _expired(existing, now):
            raise ValueError(f"another run of {command} is in progress; wait for it to finish, then run the line again")
        # The earlier run died; take the lease over.
        path.write_text(body, encoding="utf-8")
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
    try:
        yield token
    finally:
        current = _read(path)
        if current and current.get("token") == token:
            path.unlink(missing_ok=True)
