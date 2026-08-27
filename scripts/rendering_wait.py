#!/usr/bin/env python3
"""Keep one rendering claim alive for a bounded asynchronous completion wait."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import inbox_monitor


WAIT_SECONDS = 30
MAX_WAITS = 8


def _validate_state(value: Any, message_id_sha256: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("message_id_sha256") != message_id_sha256
        or type(value.get("wait_count")) is not int
        or not 0 <= value["wait_count"] <= MAX_WAITS
    ):
        raise ValueError("invalid rendering wait state")
    return value


def wait_once(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Consume one fixed wait interval for an active claimed rendering."""
    work_paths = inbox_monitor.prepare_claim_work(
        monitor_root, claim_root, message_id
    )
    state_path = Path(work_paths["rendering_wait_state"])
    message_hash = inbox_monitor.message_key(message_id)
    with inbox_monitor.state_lock(monitor_root):
        if state_path.exists():
            state = _validate_state(inbox_monitor.read_json(state_path), message_hash)
        else:
            state = {
                "schema_version": 1,
                "message_id_sha256": message_hash,
                "wait_count": 0,
            }
        if state["wait_count"] >= MAX_WAITS:
            return {
                "waited": False,
                "wait_count": state["wait_count"],
                "remaining_waits": 0,
                "exhausted": True,
            }
        state["wait_count"] += 1
        inbox_monitor.atomic_write_json(state_path, state)
    sleeper(WAIT_SECONDS)
    return {
        "waited": True,
        "wait_count": state["wait_count"],
        "remaining_waits": MAX_WAITS - state["wait_count"],
        "exhausted": state["wait_count"] >= MAX_WAITS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    wait = sub.add_parser("wait")
    wait.add_argument("--monitor-root", type=Path, required=True)
    wait.add_argument("--claim-root", type=Path, required=True)
    wait.add_argument("--message-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = wait_once(args.monitor_root, args.claim_root, args.message_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
