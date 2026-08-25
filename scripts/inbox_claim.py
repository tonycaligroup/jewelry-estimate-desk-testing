#!/usr/bin/env python3
"""Best-effort single-workspace claims for Gmail messages.

Kolo records do not provide compare-and-swap. This helper uses atomic directory
creation in the shared workspace to prevent overlapping runs on the same host.
It deliberately does not auto-steal stale claims: an uncertain prior send must
be reviewed instead of repeated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_claim_root() -> Path:
    configured_workspace = os.environ.get("OPENCLAW_WORKSPACE")
    workspace = (
        Path(configured_workspace).expanduser()
        if configured_workspace
        else Path.home() / ".openclaw" / "workspace-main"
    )
    return workspace.resolve() / "estimate-desk" / "inbox-claims"


def claim_key(message_id: str) -> str:
    if not message_id or len(message_id) > 512:
        raise ValueError("message ID must contain 1-512 characters")
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def claim_path(root: Path, message_id: str) -> Path:
    return root / claim_key(message_id)


def write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path / f"state.{secrets.token_hex(4)}.tmp"
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path / "state.json")


def read_state(path: Path, attempts: int = 20) -> dict[str, Any]:
    state_path = path / "state.json"
    for attempt in range(attempts):
        try:
            raw = state_path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.01)
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("claim state must be a JSON object")
    return state


def acquire(root: Path, message_id: str) -> tuple[bool, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = claim_path(root, message_id)
    token = secrets.token_hex(16)
    state = {
        "message_id_sha256": claim_key(message_id),
        "claim_token": token,
        "status": "processing",
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return False, read_state(path)
    write_state(path, state)
    return True, state


def finish(root: Path, message_id: str, token: str, status: str) -> dict[str, Any]:
    path = claim_path(root, message_id)
    state = read_state(path)
    if state.get("claim_token") != token:
        raise ValueError("claim token does not match")
    if state.get("status") != "processing":
        raise ValueError(f"claim is already {state.get('status')}")
    state["status"] = status
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_state(path, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=default_claim_root())
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("--message-id", required=True)
    for name in ("complete", "fail"):
        command = sub.add_parser(name)
        command.add_argument("--message-id", required=True)
        command.add_argument("--token", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "claim":
            acquired, state = acquire(args.root, args.message_id)
            print(json.dumps({"acquired": acquired, **state}, sort_keys=True))
            return 0 if acquired else 4
        status = "processed" if args.command == "complete" else "manual_review"
        state = finish(args.root, args.message_id, args.token, status)
        print(json.dumps(state, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
