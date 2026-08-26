#!/usr/bin/env python3
"""Best-effort single-workspace claims for Gmail messages.

Kolo records do not provide compare-and-swap. This helper uses atomic directory
creation in the shared workspace to prevent overlapping runs on the same host.
It deliberately does not auto-steal stale claims: an uncertain prior send must
be reviewed instead of repeated.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
TERMINAL_STATUSES = {"processed", "manual_review"}
NOTIFICATION_STATUSES = {"pending", "sent", "failed_pre_delivery", "uncertain"}


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


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    lock_path = path / "state.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("claim state must be a JSON object")
    allowed = {
        "schema_version",
        "message_id_sha256",
        "claim_token",
        "status",
        "claimed_at",
        "finished_at",
        "reason_code",
        "owner_notification",
    }
    required = {"schema_version", "message_id_sha256", "claim_token", "status", "claimed_at"}
    if not required.issubset(state) or not set(state).issubset(allowed):
        raise ValueError("claim state contains missing or unsupported fields")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported claim schema_version")
    if state.get("status") not in {"processing", *TERMINAL_STATUSES}:
        raise ValueError("invalid claim status")
    if not isinstance(state.get("message_id_sha256"), str):
        raise ValueError("invalid claim message hash")
    if not isinstance(state.get("claim_token"), str):
        raise ValueError("invalid claim token")
    if not isinstance(state.get("claimed_at"), str) or not state["claimed_at"]:
        raise ValueError("invalid claim timestamp")
    if "reason_code" in state and (
        not isinstance(state["reason_code"], str)
        or not state["reason_code"]
        or len(state["reason_code"]) > 80
        or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in state["reason_code"]
        )
    ):
        raise ValueError("invalid claim reason_code")
    notification = state.get("owner_notification")
    if notification is not None:
        if not isinstance(notification, dict):
            raise ValueError("owner_notification must be an object")
        if set(notification) != {"key", "status", "attempts", "updated_at"}:
            raise ValueError("owner_notification contains missing or unsupported fields")
        if notification.get("status") not in NOTIFICATION_STATUSES:
            raise ValueError("invalid owner notification status")
        if not isinstance(notification.get("key"), str):
            raise ValueError("invalid owner notification key")
        if type(notification.get("attempts")) is not int or notification["attempts"] < 1:
            raise ValueError("invalid owner notification attempts")
    return state


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
    if isinstance(state, dict) and "schema_version" not in state:
        legacy_required = {"message_id_sha256", "claim_token", "status", "claimed_at"}
        if not legacy_required.issubset(state) or state.get("status") not in {
            "processing",
            *TERMINAL_STATUSES,
        }:
            raise ValueError("unrecognized legacy claim state")
        state["schema_version"] = SCHEMA_VERSION
        write_state(path, state)
    return validate_state(state)


def acquire(root: Path, message_id: str) -> tuple[bool, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = claim_path(root, message_id)
    token = secrets.token_hex(16)
    state = {
        "schema_version": SCHEMA_VERSION,
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


def finish(
    root: Path,
    message_id: str,
    token: str,
    status: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    path = claim_path(root, message_id)
    state = read_state(path)
    if state.get("claim_token") != token:
        raise ValueError("claim token does not match")
    if state.get("status") != "processing":
        raise ValueError(f"claim is already {state.get('status')}")
    state["status"] = status
    if reason_code is not None:
        if status != "manual_review":
            raise ValueError("reason_code is allowed only for manual_review")
        if not reason_code or len(reason_code) > 80 or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in reason_code
        ):
            raise ValueError("reason_code must use 1-80 lowercase letters, digits, or underscores")
        state["reason_code"] = reason_code
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_state(path, state)
    return state


def begin_notification(
    root: Path, message_id: str, token: str, notification_key: str
) -> dict[str, Any]:
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if not notification_key or len(notification_key) > 200 or not all(
            character.isalnum() or character in "_.:@-" for character in notification_key
        ):
            raise ValueError("invalid notification key")
        prior = state.get("owner_notification")
        attempts = 1
        if prior is not None:
            if prior.get("key") != notification_key:
                raise ValueError("a different notification is already bound to this message")
            if prior.get("status") == "failed_pre_delivery" and prior.get("attempts") == 1:
                attempts = 2
            else:
                raise ValueError(f"notification is already {prior.get('status')}")
        state["owner_notification"] = {
            "key": notification_key,
            "status": "pending",
            "attempts": attempts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_state(path, state)
        return state


def finish_notification(
    root: Path, message_id: str, token: str, status: str
) -> dict[str, Any]:
    if status not in {"sent", "failed_pre_delivery", "uncertain"}:
        raise ValueError("invalid terminal notification status")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        notification = state.get("owner_notification")
        if not isinstance(notification, dict) or notification.get("status") != "pending":
            raise ValueError("notification is not pending")
        notification["status"] = status
        notification["updated_at"] = datetime.now(timezone.utc).isoformat()
        write_state(path, state)
        return state


def reconcile_notification(root: Path, message_id: str) -> dict[str, Any]:
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        notification = state.get("owner_notification")
        if isinstance(notification, dict) and notification.get("status") == "pending":
            notification["status"] = "uncertain"
            notification["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_state(path, state)
        return state


def reconcile_stale_notifications(
    root: Path,
    minimum_age_seconds: int,
    now: datetime | None = None,
) -> dict[str, int]:
    if type(minimum_age_seconds) is not int or minimum_age_seconds < 1:
        raise ValueError("minimum_age_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    summary = {"claims_scanned": 0, "pending": 0, "reconciled": 0}
    if not root.exists():
        return summary
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", path.name):
            continue
        with state_lock(path):
            state = read_state(path)
            summary["claims_scanned"] += 1
            notification = state.get("owner_notification")
            if not isinstance(notification, dict) or notification.get("status") != "pending":
                continue
            summary["pending"] += 1
            try:
                updated_at = datetime.fromisoformat(notification["updated_at"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid owner notification updated_at") from exc
            if updated_at.tzinfo is None:
                raise ValueError("owner notification updated_at must include timezone")
            if (current - updated_at).total_seconds() < minimum_age_seconds:
                continue
            notification["status"] = "uncertain"
            notification["updated_at"] = current.isoformat()
            write_state(path, state)
            summary["reconciled"] += 1
    return summary


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
        if name == "fail":
            command.add_argument("--reason-code", required=True)
    begin_notify = sub.add_parser("notification-begin")
    begin_notify.add_argument("--message-id", required=True)
    begin_notify.add_argument("--token", required=True)
    begin_notify.add_argument("--notification-key", required=True)
    finish_notify = sub.add_parser("notification-finish")
    finish_notify.add_argument("--message-id", required=True)
    finish_notify.add_argument("--token", required=True)
    finish_notify.add_argument(
        "--status", choices=("sent", "failed_pre_delivery", "uncertain"), required=True
    )
    reconcile_notify = sub.add_parser("notification-reconcile")
    reconcile_notify.add_argument("--message-id", required=True)
    reconcile_stale = sub.add_parser("notification-reconcile-stale")
    reconcile_stale.add_argument("--minimum-age-seconds", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "claim":
            acquired, state = acquire(args.root, args.message_id)
            print(json.dumps({"acquired": acquired, **state}, sort_keys=True))
            return 0
        if args.command == "notification-begin":
            state = begin_notification(
                args.root, args.message_id, args.token, args.notification_key
            )
            print(json.dumps(state, sort_keys=True))
            return 0
        if args.command == "notification-finish":
            state = finish_notification(
                args.root, args.message_id, args.token, args.status
            )
            print(json.dumps(state, sort_keys=True))
            return 0
        if args.command == "notification-reconcile":
            state = reconcile_notification(args.root, args.message_id)
            print(json.dumps(state, sort_keys=True))
            return 0
        if args.command == "notification-reconcile-stale":
            result = reconcile_stale_notifications(root=args.root, minimum_age_seconds=args.minimum_age_seconds)
            print(json.dumps(result, sort_keys=True))
            return 0
        status = "processed" if args.command == "complete" else "manual_review"
        reason_code = args.reason_code if args.command == "fail" else None
        state = finish(args.root, args.message_id, args.token, status, reason_code)
        print(json.dumps(state, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
