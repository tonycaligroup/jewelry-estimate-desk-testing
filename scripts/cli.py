#!/usr/bin/env python3
"""Run a platform command (openclaw or kolo) with a cutoff and a retry on the CLI's own lock.

Kolo's probe, 7 September 2026: the OpenClaw CLI keeps its state in SQLite
and two commands at once fail at once with "database is locked"; its
`--timeout-ms` is not a ceiling. Every desk command therefore runs through
here: cut off by the caller's deadline (the tick is never killed), and a
locked call tried again a few seconds later inside the same tick.
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Callable, Sequence

Runner = Callable[..., subprocess.CompletedProcess]

LOCK_TEXT = "database is locked"
LOCK_TRIES = 4
LOCK_PAUSE_SECONDS = 5
CUTOFF_MARGIN_SECONDS = 20
CUTOFF_MIN_SECONDS = 30
DEFAULT_TIMEOUT_SECONDS = 120  # a kolo card, notice, or audit query: seconds when the CLI is free


def remaining_seconds(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def run(argv: Sequence[str], runner: Runner = subprocess.run, deadline: float | None = None, what: str = "command",
        timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """One command: cut off at the deadline (or `timeout`), tried again on the CLI's database lock."""
    last_lock: Exception | None = None
    for attempt in range(LOCK_TRIES):
        seconds: float | None = timeout
        left = remaining_seconds(deadline)
        if left is not None:
            seconds = max(CUTOFF_MIN_SECONDS, left - CUTOFF_MARGIN_SECONDS)
        try:
            return runner(list(argv), check=check, capture_output=True, text=True, shell=False, timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"{what} cut off after {int(seconds or 0)} s" + (" at the tick's deadline" if left is not None else "")) from exc
        except subprocess.CalledProcessError as exc:
            text = ((exc.stderr or "") + (exc.stdout or "")).lower()
            if LOCK_TEXT not in text:
                raise
            last_lock = exc
            left = remaining_seconds(deadline)
            if attempt + 1 >= LOCK_TRIES or (left is not None and left < CUTOFF_MIN_SECONDS + LOCK_PAUSE_SECONDS):
                break
            time.sleep(LOCK_PAUSE_SECONDS)
    raise OSError(f"{what}: the platform CLI was busy ({LOCK_TEXT}) {LOCK_TRIES} times running") from last_lock
