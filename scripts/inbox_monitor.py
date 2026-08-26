#!/usr/bin/env python3
"""Durable activation and discovery queue state for inbox monitoring.

This helper stores provider identifiers and timestamps only. Gmail access and
customer-message interpretation remain outside this script.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import cron_config as cron_config_helper


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
QUEUE_SCHEMA_VERSION = 1
ACTIVATION_STATES = {"prepared", "active", "reconfiguring"}
PROCESSING_STATES = {"unclaimed", "processing", "processed", "manual_review"}
TERMINAL_STATES = {"processed", "manual_review"}
REQUIRED_CAPABILITIES = (
    "gmail_after_epoch",
    "gmail_internal_date_ms",
    "gmail_complete_pagination",
)


def default_root() -> Path:
    configured_workspace = os.environ.get("OPENCLAW_WORKSPACE")
    workspace = (
        Path(configured_workspace).expanduser()
        if configured_workspace
        else Path.home() / ".openclaw" / "workspace-main"
    )
    return workspace.resolve() / "estimate-desk" / "inbox-monitor"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def message_key(message_id: str) -> str:
    require_provider_id(message_id, "gmail_message_id")
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def require_provider_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field} must contain 1-512 characters")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} contains invalid characters")
    return value


def require_epoch_ms(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer epoch millisecond")
    return value


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"required state is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt JSON state at {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def setup_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock = root / "setup.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("inbox-monitor setup is already running or needs manual recovery") from exc
    try:
        yield
    finally:
        lock.rmdir()


@contextmanager
def state_lock(root: Path) -> Iterator[None]:
    """Serialize short local state commits; the OS releases this lock on crash."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / "state.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def validate_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ValueError("capabilities must be a JSON object")
    if set(value) != set(REQUIRED_CAPABILITIES):
        raise ValueError("capabilities contains missing or unsupported fields")
    missing = [name for name in REQUIRED_CAPABILITIES if value.get(name) is not True]
    if missing:
        raise ValueError("unsupported environment; missing capabilities: " + ", ".join(missing))
    return {name: True for name in REQUIRED_CAPABILITIES}


def normalize_legacy_monitor_state(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("schema_version") != LEGACY_SCHEMA_VERSION:
        return value
    expected = {
        "schema_version",
        "activation_state",
        "expected_cron_sha256",
        "capabilities",
        "activated_at_ms",
        "discovery_watermark_ms",
    }
    if set(value) != expected or value.get("activation_state") not in {"prepared", "active"}:
        raise ValueError("invalid legacy monitor state")
    return {
        "schema_version": SCHEMA_VERSION,
        "activation_state": value["activation_state"],
        "bound_cron_sha256": value["expected_cron_sha256"],
        "pending_cron_sha256": None,
        "capabilities": value["capabilities"],
        "activated_at_ms": value["activated_at_ms"],
        "discovery_watermark_ms": value["discovery_watermark_ms"],
    }


def validate_monitor_state(value: Any) -> dict[str, Any]:
    value = normalize_legacy_monitor_state(value)
    if not isinstance(value, dict):
        raise ValueError("monitor state must be a JSON object")
    if set(value) != {
        "schema_version",
        "activation_state",
        "bound_cron_sha256",
        "pending_cron_sha256",
        "capabilities",
        "activated_at_ms",
        "discovery_watermark_ms",
    }:
        raise ValueError("monitor state contains missing or unsupported fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported monitor state schema_version")
    activation_state = value.get("activation_state")
    if activation_state not in ACTIVATION_STATES:
        raise ValueError("invalid monitor activation_state")
    bound_hash = value.get("bound_cron_sha256")
    if not isinstance(bound_hash, str) or not bound_hash.startswith("sha256:"):
        raise ValueError("invalid bound_cron_sha256")
    pending_hash = value.get("pending_cron_sha256")
    if activation_state == "reconfiguring":
        if not isinstance(pending_hash, str) or not pending_hash.startswith("sha256:"):
            raise ValueError("reconfiguring state requires pending_cron_sha256")
        if pending_hash == bound_hash:
            raise ValueError("pending cron config must differ from the bound config")
    elif pending_hash is not None:
        raise ValueError("pending_cron_sha256 is allowed only while reconfiguring")
    activated_at = value.get("activated_at_ms")
    watermark = value.get("discovery_watermark_ms")
    if activation_state == "prepared":
        if activated_at is not None or watermark is not None:
            raise ValueError("prepared monitor state cannot have activation timestamps")
    else:
        require_epoch_ms(activated_at, "activated_at_ms")
        require_epoch_ms(watermark, "discovery_watermark_ms")
        if watermark < activated_at:
            raise ValueError("discovery watermark cannot precede activation")
    validate_capabilities(value.get("capabilities"))
    return value


def load_monitor_state(root: Path) -> dict[str, Any]:
    return validate_monitor_state(read_json(root / "monitor-state.json"))


def prepare(root: Path, capabilities: Any, cron_config: Any) -> dict[str, Any]:
    verified = validate_capabilities(capabilities)
    cron_config_helper.validate_binding(cron_config)
    cron_hash = sha256_json(cron_config)
    with setup_lock(root):
        state_path = root / "monitor-state.json"
        if state_path.exists():
            existing = load_monitor_state(root)
            if existing["bound_cron_sha256"] != cron_hash:
                raise ValueError("existing monitor cron identity/config does not match")
            return existing
        state = {
            "schema_version": SCHEMA_VERSION,
            "activation_state": "prepared",
            "bound_cron_sha256": cron_hash,
            "pending_cron_sha256": None,
            "capabilities": verified,
            "activated_at_ms": None,
            "discovery_watermark_ms": None,
        }
        atomic_write_json(state_path, state)
        return state


def activate(root: Path, cron_config: Any, activated_at_ms: int | None = None) -> dict[str, Any]:
    cron_config_helper.validate_binding(cron_config)
    cron_hash = sha256_json(cron_config)
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["bound_cron_sha256"] != cron_hash:
            raise ValueError("verified cron identity/config does not match prepared state")
        if state["activation_state"] == "active":
            if state.get("schema_version") != SCHEMA_VERSION:
                state["schema_version"] = SCHEMA_VERSION
                atomic_write_json(root / "monitor-state.json", state)
            return state
        if state["activation_state"] != "prepared":
            raise ValueError("monitor is not prepared for initial activation")
        activation = int(time.time() * 1000) if activated_at_ms is None else activated_at_ms
        require_epoch_ms(activation, "activated_at_ms")
        state["activation_state"] = "active"
        state["activated_at_ms"] = activation
        state["discovery_watermark_ms"] = activation
        atomic_write_json(root / "monitor-state.json", state)
        return state


def prepare_reconfiguration(
    root: Path, current_cron_config: Any, target_cron_config: Any
) -> dict[str, Any]:
    if not isinstance(current_cron_config, dict) or not current_cron_config:
        raise ValueError("current cron config must be a non-empty JSON object")
    cron_config_helper.validate_binding(target_cron_config)
    current_hash = sha256_json(current_cron_config)
    target_hash = sha256_json(target_cron_config)
    if current_hash == target_hash:
        raise ValueError("target cron config must differ from the current config")
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["bound_cron_sha256"] != current_hash:
            raise ValueError("current cron config does not match the bound config")
        if state["activation_state"] == "reconfiguring":
            if state["pending_cron_sha256"] != target_hash:
                raise ValueError("a different cron reconfiguration is already pending")
            return state
        if state["activation_state"] != "active":
            raise ValueError("only an active monitor can be reconfigured")
        state["schema_version"] = SCHEMA_VERSION
        state["activation_state"] = "reconfiguring"
        state["pending_cron_sha256"] = target_hash
        atomic_write_json(root / "monitor-state.json", state)
        return state


def activate_reconfiguration(root: Path, cron_config_value: Any) -> dict[str, Any]:
    cron_config_helper.validate_binding(cron_config_value)
    target_hash = sha256_json(cron_config_value)
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["activation_state"] != "reconfiguring":
            raise ValueError("monitor is not awaiting cron reconfiguration")
        if state["pending_cron_sha256"] != target_hash:
            raise ValueError("verified live cron does not match the pending config")
        state["activation_state"] = "active"
        state["bound_cron_sha256"] = target_hash
        state["pending_cron_sha256"] = None
        atomic_write_json(root / "monitor-state.json", state)
        return state


def cancel_reconfiguration(root: Path, current_cron_config: Any) -> dict[str, Any]:
    if not isinstance(current_cron_config, dict) or not current_cron_config:
        raise ValueError("current cron config must be a non-empty JSON object")
    current_hash = sha256_json(current_cron_config)
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["activation_state"] != "reconfiguring":
            raise ValueError("monitor is not awaiting cron reconfiguration")
        if state["bound_cron_sha256"] != current_hash:
            raise ValueError("rollback cron config does not match the bound config")
        state["activation_state"] = "active"
        state["pending_cron_sha256"] = None
        atomic_write_json(root / "monitor-state.json", state)
        return state


def validate_queue_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ValueError("invalid queue item schema_version")
    allowed = {
        "schema_version",
        "gmail_message_id",
        "gmail_message_id_sha256",
        "thread_id",
        "internal_date_ms",
        "discovery_status",
        "processing_status",
        "processing_started_at",
        "reason_code",
    }
    required = allowed - {"processing_started_at", "reason_code"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError("queue item contains missing or unsupported fields")
    message_id = require_provider_id(value.get("gmail_message_id"), "gmail_message_id")
    if value.get("gmail_message_id_sha256") != message_key(message_id):
        raise ValueError("queue item message hash mismatch")
    require_provider_id(value.get("thread_id"), "thread_id")
    require_epoch_ms(value.get("internal_date_ms"), "internal_date_ms")
    if value.get("processing_status") not in PROCESSING_STATES:
        raise ValueError("invalid queue processing_status")
    if value.get("discovery_status") not in {"pending", "complete"}:
        raise ValueError("invalid queue discovery_status")
    if "reason_code" in value and (
        not isinstance(value["reason_code"], str)
        or not value["reason_code"]
        or len(value["reason_code"]) > 80
        or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in value["reason_code"]
        )
    ):
        raise ValueError("invalid queue reason_code")
    if value["processing_status"] in TERMINAL_STATES and value["discovery_status"] != "complete":
        raise ValueError("terminal queue item must have complete discovery_status")
    return value


def queue_path(root: Path, message_id: str) -> Path:
    return root / "queue" / f"{message_key(message_id)}.json"


def load_queue_item(root: Path, message_id: str) -> dict[str, Any]:
    return validate_queue_item(read_json(queue_path(root, message_id)))


def discover_complete(
    root: Path,
    batch: Any,
    window_start_ms: int,
    window_end_ms: int,
) -> dict[str, Any]:
    with state_lock(root):
        state = load_monitor_state(root)
        if state["activation_state"] != "active":
            raise ValueError("monitor is not active")
        require_epoch_ms(window_start_ms, "window_start_ms")
        require_epoch_ms(window_end_ms, "window_end_ms")
        if window_start_ms != state["discovery_watermark_ms"]:
            raise ValueError("window_start_ms must equal the durable discovery watermark")
        if window_end_ms < window_start_ms:
            raise ValueError("window_end_ms cannot precede window_start_ms")
        if not isinstance(batch, list):
            raise ValueError("discovery batch must be a JSON array")

        inserted = 0
        existing = 0
        ignored_before_activation = 0
        ignored_after_window = 0
        for raw in batch:
            if not isinstance(raw, dict) or set(raw) != {
                "gmail_message_id",
                "thread_id",
                "internal_date_ms",
            }:
                raise ValueError("each discovery item must contain only Gmail ID, thread ID, and internalDate")
            message_id = require_provider_id(raw["gmail_message_id"], "gmail_message_id")
            thread_id = require_provider_id(raw["thread_id"], "thread_id")
            internal_date = require_epoch_ms(raw["internal_date_ms"], "internal_date_ms")
            if internal_date < state["activated_at_ms"]:
                ignored_before_activation += 1
                continue
            if internal_date > window_end_ms:
                ignored_after_window += 1
                continue
            path = queue_path(root, message_id)
            item = {
                "schema_version": QUEUE_SCHEMA_VERSION,
                "gmail_message_id": message_id,
                "gmail_message_id_sha256": message_key(message_id),
                "thread_id": thread_id,
                "internal_date_ms": internal_date,
                "discovery_status": "pending",
                "processing_status": "unclaimed",
            }
            if path.exists():
                prior = load_queue_item(root, message_id)
                for field in ("gmail_message_id", "thread_id", "internal_date_ms"):
                    if prior[field] != item[field]:
                        raise ValueError(f"immutable queue metadata changed for {message_id}")
                existing += 1
            else:
                atomic_write_json(path, item)
                inserted += 1

        state["discovery_watermark_ms"] = window_end_ms
        atomic_write_json(root / "monitor-state.json", state)
        return {
            "inserted": inserted,
            "existing": existing,
            "ignored_before_activation": ignored_before_activation,
            "ignored_after_window": ignored_after_window,
            "discovery_watermark_ms": window_end_ms,
        }


def all_queue_items(root: Path) -> list[dict[str, Any]]:
    queue = root / "queue"
    if not queue.exists():
        return []
    return [validate_queue_item(read_json(path)) for path in sorted(queue.glob("*.json"))]


def next_eligible(root: Path) -> dict[str, Any] | None:
    state = load_monitor_state(root)
    if state["activation_state"] != "active":
        raise ValueError("monitor is not active")
    items = all_queue_items(root)
    ordered = sorted(items, key=lambda item: (item["internal_date_ms"], item["gmail_message_id"]))
    for item in ordered:
        if item["processing_status"] != "unclaimed":
            continue
        older_in_thread = any(
            other["thread_id"] == item["thread_id"]
            and (other["internal_date_ms"], other["gmail_message_id"])
            < (item["internal_date_ms"], item["gmail_message_id"])
            and other["processing_status"] not in TERMINAL_STATES
            for other in ordered
        )
        if not older_in_thread:
            return item
    return None


def sync_claim(root: Path, message_id: str, claim: Any) -> dict[str, Any]:
    item = load_queue_item(root, message_id)
    if not isinstance(claim, dict) or type(claim.get("acquired")) is not bool:
        raise ValueError("claim result must include boolean acquired")
    if claim.get("message_id_sha256") != item["gmail_message_id_sha256"]:
        raise ValueError("queue/claim message hash mismatch")
    status = claim.get("status")
    if status not in {"processing", *TERMINAL_STATES}:
        raise ValueError("queue/claim status mismatch")
    if claim["acquired"] and status != "processing":
        raise ValueError("a newly acquired claim must be processing")
    item["processing_status"] = status
    if status in TERMINAL_STATES:
        item["discovery_status"] = "complete"
        if isinstance(claim.get("reason_code"), str):
            item["reason_code"] = claim["reason_code"]
    else:
        claimed_at = claim.get("claimed_at")
        if not isinstance(claimed_at, str) or not claimed_at:
            raise ValueError("processing claim must include claimed_at")
        item["processing_started_at"] = claimed_at
    atomic_write_json(queue_path(root, message_id), item)
    return item


def reconcile_terminal(root: Path, message_id: str, claim_root: Path) -> dict[str, Any]:
    item = load_queue_item(root, message_id)
    claim_path = claim_root / item["gmail_message_id_sha256"] / "state.json"
    claim = read_json(claim_path)
    status = claim.get("status") if isinstance(claim, dict) else None
    if status not in TERMINAL_STATES:
        raise ValueError("claim is not terminal; refusing to complete queue item")
    item["processing_status"] = status
    item["discovery_status"] = "complete"
    if isinstance(claim.get("reason_code"), str):
        item["reason_code"] = claim["reason_code"]
    atomic_write_json(queue_path(root, message_id), item)
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=default_root())
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--capabilities", type=Path, required=True)
    prepare_parser.add_argument("--cron-config", type=Path, required=True)
    activate_parser = sub.add_parser("activate")
    activate_parser.add_argument("--cron-config", type=Path, required=True)
    reconfigure_parser = sub.add_parser("reconfigure-prepare")
    reconfigure_parser.add_argument("--current-cron-config", type=Path, required=True)
    reconfigure_parser.add_argument("--target-cron-config", type=Path, required=True)
    reconfigure_activate_parser = sub.add_parser("reconfigure-activate")
    reconfigure_activate_parser.add_argument("--cron-config", type=Path, required=True)
    reconfigure_cancel_parser = sub.add_parser("reconfigure-cancel")
    reconfigure_cancel_parser.add_argument("--current-cron-config", type=Path, required=True)
    sub.add_parser("status")
    discover_parser = sub.add_parser("discover-complete")
    discover_parser.add_argument("--batch", type=Path, required=True)
    discover_parser.add_argument("--window-start-ms", type=int, required=True)
    discover_parser.add_argument("--window-end-ms", type=int, required=True)
    sub.add_parser("next")
    sync_parser = sub.add_parser("sync-claim")
    sync_parser.add_argument("--message-id", required=True)
    sync_parser.add_argument("--claim-result", type=Path, required=True)
    reconcile_parser = sub.add_parser("reconcile-terminal")
    reconcile_parser.add_argument("--message-id", required=True)
    reconcile_parser.add_argument("--claim-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.root, read_json(args.capabilities), read_json(args.cron_config))
        elif args.command == "activate":
            result = activate(args.root, read_json(args.cron_config))
        elif args.command == "reconfigure-prepare":
            result = prepare_reconfiguration(
                args.root,
                read_json(args.current_cron_config),
                read_json(args.target_cron_config),
            )
        elif args.command == "reconfigure-activate":
            result = activate_reconfiguration(args.root, read_json(args.cron_config))
        elif args.command == "reconfigure-cancel":
            result = cancel_reconfiguration(
                args.root, read_json(args.current_cron_config)
            )
        elif args.command == "status":
            result = load_monitor_state(args.root)
        elif args.command == "discover-complete":
            result = discover_complete(
                args.root,
                read_json(args.batch),
                args.window_start_ms,
                args.window_end_ms,
            )
        elif args.command == "next":
            result = next_eligible(args.root)
        elif args.command == "sync-claim":
            result = sync_claim(args.root, args.message_id, read_json(args.claim_result))
        else:
            result = reconcile_terminal(args.root, args.message_id, args.claim_root)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
