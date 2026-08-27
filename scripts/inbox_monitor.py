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
import re
import secrets
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import cron_config as cron_config_helper
import estimate_record
import inbox_claim


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
WORK_ARTIFACTS = {
    "gmail_message": "gmail-message.json",
    "gmail_thread": "gmail-thread.json",
    "route": "route.json",
    "candidate_records": "candidate-records.json",
    "inquiry_record": "inquiry-record.json",
    "thread_review": "thread-review.json",
    "current_record": "current-record.json",
    "customer_reply": "customer-reply.txt",
    "rendering_image_1": "rendering-1.png",
    "rendering_image_2": "rendering-2.png",
    "rendering_wait_state": "rendering-wait.json",
    "gmail_payload": "gmail-payload.json",
    "gmail_provider_response": "gmail-provider-response.json",
    "current_state": "current-state.json",
    "approval_request": "approval-request.json",
    "appointment_intent": "appointment-intent.json",
    "appointment_approval": "appointment-approval.json",
}


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
        raise ValueError(
            "inbox-monitor setup is already running or needs manual recovery"
        ) from exc
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
        raise ValueError(
            "unsupported environment; missing capabilities: " + ", ".join(missing)
        )
    return {name: True for name in REQUIRED_CAPABILITIES}


def normalize_legacy_monitor_state(value: Any) -> Any:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != LEGACY_SCHEMA_VERSION
    ):
        return value
    expected = {
        "schema_version",
        "activation_state",
        "expected_cron_sha256",
        "capabilities",
        "activated_at_ms",
        "discovery_watermark_ms",
    }
    if set(value) != expected or value.get("activation_state") not in {
        "prepared",
        "active",
    }:
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


def verify_legacy_binding(root: Path, live_job: Any) -> dict[str, Any]:
    """Reconstruct and prove the schema-1 five-field binding without mutation."""
    raw_state = read_json(root / "monitor-state.json")
    if not isinstance(raw_state, dict) or raw_state.get("schema_version") != 1:
        raise ValueError("monitor state is not a legacy schema-1 binding")
    state = validate_monitor_state(raw_state)
    if not isinstance(live_job, dict):
        raise ValueError("live cron job must be a JSON object")
    schedule = live_job.get("schedule")
    payload = live_job.get("payload")
    if not isinstance(schedule, dict) or not isinstance(payload, dict):
        raise ValueError("live cron schedule and payload must be objects")
    if schedule.get("kind") != "cron":
        raise ValueError("live cron schedule kind must be cron")
    if payload.get("fallbacks") != []:
        raise ValueError("legacy monitor requires no live cron fallbacks")
    legacy = {
        "name": cron_config_helper.require_string(live_job.get("name"), "name"),
        "schedule": cron_config_helper.require_string(
            schedule.get("expr"), "schedule.expr"
        ),
        "timezone": cron_config_helper.require_string(
            schedule.get("tz"), "schedule.tz"
        ),
        "model": cron_config_helper.require_string(
            payload.get("model"), "payload.model"
        ),
        # The schema-1 setup serialized the CLI's empty fallback argument.
        "fallbacks": "",
    }
    if legacy["name"] != cron_config_helper.JOB_NAME:
        raise ValueError("legacy cron job name does not match")
    if legacy["model"] != cron_config_helper.MODEL:
        raise ValueError("legacy cron model does not match")
    if sha256_json(legacy) != state["bound_cron_sha256"]:
        raise ValueError("reconstructed legacy cron config does not match bound hash")
    return legacy


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


def activate(
    root: Path, cron_config: Any, activated_at_ms: int | None = None
) -> dict[str, Any]:
    cron_config_helper.validate_binding(cron_config)
    cron_hash = sha256_json(cron_config)
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["bound_cron_sha256"] != cron_hash:
            raise ValueError(
                "verified cron identity/config does not match prepared state"
            )
        if state["activation_state"] == "active":
            if state.get("schema_version") != SCHEMA_VERSION:
                state["schema_version"] = SCHEMA_VERSION
                atomic_write_json(root / "monitor-state.json", state)
            return state
        if state["activation_state"] != "prepared":
            raise ValueError("monitor is not prepared for initial activation")
        activation = (
            int(time.time() * 1000) if activated_at_ms is None else activated_at_ms
        )
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


def adopt_disabled_live_reconfiguration(
    root: Path,
    current_cron_config: Any,
    live_job: Any,
    workspace: Path,
    base_dir: Path,
) -> dict[str, Any]:
    """Recover when a verified cron edit happened before reconfigure-prepare.

    This is intentionally narrower than the normal two-phase path: the live job
    must already be disabled, canonical, and have the same stable job ID as the
    previously bound config. The old config must still match the durable bound
    hash, so this cannot be used to adopt an unrelated or unproven edit.
    """
    if not isinstance(current_cron_config, dict) or not current_cron_config:
        raise ValueError("current cron config must be a non-empty JSON object")
    if not isinstance(live_job, dict) or live_job.get("enabled") is not False:
        raise ValueError("live cron must be disabled before recovery adoption")
    old_id = cron_config_helper.require_string(
        current_cron_config.get("id"), "current_cron_config.id"
    )
    if live_job.get("id") != old_id:
        raise ValueError("live cron id does not match the bound cron id")
    target_config = cron_config_helper.build_binding(live_job, workspace, base_dir)
    current_hash = sha256_json(current_cron_config)
    target_hash = sha256_json(target_config)
    if current_hash == target_hash:
        raise ValueError("live cron config is already bound")
    with setup_lock(root):
        state = load_monitor_state(root)
        if state["activation_state"] != "active":
            raise ValueError("only an active monitor can adopt a recovered live config")
        if state["pending_cron_sha256"] is not None:
            raise ValueError("cannot adopt while a cron reconfiguration is pending")
        if state["bound_cron_sha256"] != current_hash:
            raise ValueError("current cron config does not match the bound config")
        state["bound_cron_sha256"] = target_hash
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
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != QUEUE_SCHEMA_VERSION
    ):
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
        "review_status",
        "review_resolved_at",
    }
    required = allowed - {
        "processing_started_at",
        "reason_code",
        "review_status",
        "review_resolved_at",
    }
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
    if "review_status" in value:
        if value["processing_status"] != "manual_review" or value[
            "review_status"
        ] not in {
            "open",
            "resolved",
        }:
            raise ValueError("invalid manual-review status")
    if "review_resolved_at" in value and (
        value.get("review_status") != "resolved"
        or not isinstance(value["review_resolved_at"], str)
        or not value["review_resolved_at"]
    ):
        raise ValueError("review_resolved_at requires a resolved manual review")
    if (
        value["processing_status"] in TERMINAL_STATES
        and value["discovery_status"] != "complete"
    ):
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
            raise ValueError(
                "window_start_ms must equal the durable discovery watermark"
            )
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
                raise ValueError(
                    "each discovery item must contain only Gmail ID, thread ID, and internalDate"
                )
            message_id = require_provider_id(
                raw["gmail_message_id"], "gmail_message_id"
            )
            thread_id = require_provider_id(raw["thread_id"], "thread_id")
            internal_date = require_epoch_ms(
                raw["internal_date_ms"], "internal_date_ms"
            )
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
                        raise ValueError(
                            f"immutable queue metadata changed for {message_id}"
                        )
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
    return [
        validate_queue_item(read_json(path)) for path in sorted(queue.glob("*.json"))
    ]


def prepare_run_work(root: Path) -> dict[str, str]:
    """Create a private run-scoped path without relying on platform scratch space."""
    load_monitor_state(root)
    run_root = root.resolve().parent / "run-work"
    if run_root.is_symlink() or (run_root.exists() and not run_root.is_dir()):
        raise ValueError("run work root is not a private directory")
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_root, 0o700)
    run_dir = run_root / secrets.token_hex(12)
    run_dir.mkdir(mode=0o700)
    return {
        "run_dir": str(run_dir),
        "discovery_batch": str(run_dir / "discovery-batch.json"),
    }


def cleanup_run_work(root: Path, discovery_batch: Path) -> None:
    """Best-effort cleanup of a run path created by prepare_run_work."""
    run_root = root.resolve().parent / "run-work"
    if run_root.is_symlink() or not run_root.is_dir():
        return
    parent = discovery_batch.parent
    if (
        discovery_batch.name != "discovery-batch.json"
        or parent.parent != run_root
        or not re.fullmatch(r"[0-9a-f]{24}", parent.name)
        or parent.is_symlink()
    ):
        return
    try:
        discovery_batch.unlink(missing_ok=True)
        parent.rmdir()
    except OSError:
        # Cleanup is privacy hygiene after a durable commit, not workflow state.
        pass


def prepare_claim_work(root: Path, claim_root: Path, message_id: str) -> dict[str, str]:
    """Create and return the only supported persistent artifact paths for a claim."""
    item = load_queue_item(root, message_id)
    claim = inbox_claim.read_state(inbox_claim.claim_path(claim_root, message_id))
    if claim.get("message_id_sha256") != item["gmail_message_id_sha256"]:
        raise ValueError("queue/claim message hash mismatch")
    if claim.get("status") != "processing":
        raise ValueError("claim work requires a processing claim")
    work_root = root.resolve().parent / "work"
    if work_root.is_symlink() or (work_root.exists() and not work_root.is_dir()):
        raise ValueError("claim work root is not a private directory")
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work_root, 0o700)
    work_dir = work_root / item["gmail_message_id_sha256"]
    if work_dir.is_symlink() or (work_dir.exists() and not work_dir.is_dir()):
        raise ValueError("claim work path is not a private directory")
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work_dir, 0o700)
    return {
        "work_dir": str(work_dir),
        **{name: str(work_dir / filename) for name, filename in WORK_ARTIFACTS.items()},
    }


def cleanup_claim_work(root: Path, message_id: str) -> None:
    """Best-effort removal of customer-bearing artifacts after terminal state."""
    work_root = root.resolve().parent / "work"
    if work_root.is_symlink() or not work_root.is_dir():
        return
    work_dir = work_root / message_key(message_id)
    if work_dir.is_symlink() or not work_dir.is_dir():
        return
    try:
        shutil.rmtree(work_dir)
    except OSError:
        # The claim and queue are already terminal; cleanup cannot undo them.
        pass


def stale_processing_items(
    root: Path,
    claim_root: Path,
    minimum_age_seconds: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    if type(minimum_age_seconds) is not int or minimum_age_seconds < 1:
        raise ValueError("minimum_age_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    stale: list[dict[str, Any]] = []
    for item in all_queue_items(root):
        if item["processing_status"] != "processing":
            continue
        claim = inbox_claim.read_state(
            inbox_claim.claim_path(claim_root, item["gmail_message_id"])
        )
        if claim.get("status") != "processing":
            continue
        if (
            current - inbox_claim.state_timestamp(claim)
        ).total_seconds() < minimum_age_seconds:
            continue
        lease_active = inbox_claim.recovery_lease_active(claim, current)
        if lease_active:
            continue
        resumable = (
            claim.get("processing_phase") in inbox_claim.PROCESSING_PHASES
            and not inbox_claim.has_ambiguous_external_action(claim)
            and claim.get("retry_count_at_phase", 0) < 1
        )
        reason_code = (
            "stale_processing_retry_exhausted"
            if (
                claim.get("processing_phase") in inbox_claim.PROCESSING_PHASES
                and not inbox_claim.has_ambiguous_external_action(claim)
                and claim.get("retry_count_at_phase", 0) >= 1
            )
            else "stale_processing_ambiguous"
        )
        stale.append(
            {
                "gmail_message_id": item["gmail_message_id"],
                "gmail_message_id_sha256": item["gmail_message_id_sha256"],
                "claim_token": claim["claim_token"],
                "recovery_action": "resume" if resumable else "manual_review",
                "reason_code": reason_code,
            }
        )
    return sorted(stale, key=lambda value: value["gmail_message_id_sha256"])


def assert_settled(root: Path) -> dict[str, int]:
    processing = [
        item
        for item in all_queue_items(root)
        if item["processing_status"] == "processing"
    ]
    if processing:
        raise ValueError(
            f"{len(processing)} claimed queue item(s) remain processing; "
            "refusing successful run completion"
        )
    return {
        "processing": 0,
        "unclaimed": sum(
            item["processing_status"] == "unclaimed" for item in all_queue_items(root)
        ),
    }


def next_eligible(
    root: Path,
    claim_root: Path | None = None,
    stale_after_seconds: int = 600,
) -> dict[str, Any] | None:
    state = load_monitor_state(root)
    if state["activation_state"] != "active":
        raise ValueError("monitor is not active")
    items = all_queue_items(root)
    ordered = sorted(
        items, key=lambda item: (item["internal_date_ms"], item["gmail_message_id"])
    )
    if claim_root is not None:
        stale_by_hash = {
            item["gmail_message_id_sha256"]: item
            for item in stale_processing_items(root, claim_root, stale_after_seconds)
            if item["recovery_action"] == "resume"
        }
        for item in ordered:
            stale = stale_by_hash.get(item["gmail_message_id_sha256"])
            if stale is not None:
                return {**item, "recovery_action": "resume"}
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
        if status == "manual_review":
            item.setdefault("review_status", "open")
    else:
        claimed_at = claim.get("claimed_at")
        if not isinstance(claimed_at, str) or not claimed_at:
            raise ValueError("processing claim must include claimed_at")
        item["processing_started_at"] = claimed_at
    atomic_write_json(queue_path(root, message_id), item)
    return item


def claim_next(
    root: Path,
    claim_root: Path,
    stale_after_seconds: int,
) -> dict[str, Any] | None:
    """Select, claim, synchronize, and prepare paths in one deterministic call."""
    item = next_eligible(root, claim_root, stale_after_seconds)
    if item is None:
        return None
    message_id = item["gmail_message_id"]
    acquired, claim = inbox_claim.acquire(claim_root, message_id)
    resumed = False
    if not acquired and claim.get("status") == "processing":
        acquired, claim = inbox_claim.resume_stale(
            claim_root, message_id, stale_after_seconds
        )
        resumed = acquired
    claim_result = {"acquired": acquired, "resumed": resumed, **claim}
    queue_item = sync_claim(root, message_id, claim_result)
    result: dict[str, Any] = {
        "queue_item": queue_item,
        "claim": claim_result,
    }
    if acquired:
        result["work_paths"] = prepare_claim_work(root, claim_root, message_id)
    return result


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
    if status == "manual_review":
        item.setdefault("review_status", "open")
    atomic_write_json(queue_path(root, message_id), item)
    return item


def list_manual_reviews(root: Path) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    for item in all_queue_items(root):
        if item["processing_status"] != "manual_review":
            continue
        if item.get("review_status", "open") != "open":
            continue
        reviews.append(
            {
                "review_key": item["gmail_message_id_sha256"],
                "reason_code": item.get("reason_code", "unspecified"),
                "internal_date_ms": item["internal_date_ms"],
                "review_status": item.get("review_status", "open"),
            }
        )
    return sorted(
        reviews, key=lambda item: (item["internal_date_ms"], item["review_key"])
    )


def resolve_manual_review(root: Path, review_key: str) -> dict[str, Any]:
    if not isinstance(review_key, str) or not re.fullmatch(r"[0-9a-f]{64}", review_key):
        raise ValueError("review_key must be a lowercase SHA-256 value")
    path = root / "queue" / f"{review_key}.json"
    with state_lock(root):
        item = validate_queue_item(read_json(path))
        if item["processing_status"] != "manual_review":
            raise ValueError("review item is not manual_review")
        item["review_status"] = "resolved"
        item["review_resolved_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, item)
        return item


def finalize_item(
    root: Path,
    message_id: str,
    claim_root: Path,
    claim_token: str,
    outcome: str,
    reason_code: str | None = None,
    record_root: Path | None = None,
) -> dict[str, Any]:
    """Idempotently finish one claim and reconcile its durable queue item."""
    if outcome == "processed":
        if reason_code is not None:
            raise ValueError("processed outcome must not include reason_code")
        status = "processed"
    elif outcome == "manual_review":
        if reason_code is None:
            raise ValueError("manual_review outcome requires reason_code")
        status = "manual_review"
    else:
        raise ValueError("outcome must be processed or manual_review")
    if outcome == "processed" and record_root is not None:
        item = validate_queue_item(read_json(queue_path(root, message_id)))
        claim_state = inbox_claim.read_state(
            inbox_claim.claim_path(claim_root, message_id)
        )
        if claim_state.get("claim_token") != claim_token:
            raise ValueError("claim token does not match")
        estimate_record.require_processed_evidence(
            record_root,
            message_id,
            item["thread_id"],
            claim_state,
        )
    inbox_claim.finish(
        claim_root,
        message_id,
        claim_token,
        status,
        reason_code,
    )
    result = reconcile_terminal(root, message_id, claim_root)
    cleanup_claim_work(root, message_id)
    return result


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
    reconfigure_adopt_parser = sub.add_parser("reconfigure-adopt-disabled-live")
    reconfigure_adopt_parser.add_argument(
        "--current-cron-config", type=Path, required=True
    )
    reconfigure_adopt_parser.add_argument("--live-job", type=Path, required=True)
    reconfigure_adopt_parser.add_argument("--workspace", type=Path, required=True)
    reconfigure_adopt_parser.add_argument("--base-dir", type=Path, required=True)
    reconfigure_cancel_parser = sub.add_parser("reconfigure-cancel")
    reconfigure_cancel_parser.add_argument(
        "--current-cron-config", type=Path, required=True
    )
    legacy_parser = sub.add_parser("verify-legacy-binding")
    legacy_parser.add_argument("--live-job", type=Path, required=True)
    legacy_parser.add_argument("--output", type=Path, required=True)
    sub.add_parser("status")
    sub.add_parser("prepare-run")
    discover_parser = sub.add_parser("discover-complete")
    discover_parser.add_argument("--batch", type=Path, required=True)
    discover_parser.add_argument("--window-start-ms", type=int, required=True)
    discover_parser.add_argument("--window-end-ms", type=int, required=True)
    next_parser = sub.add_parser("next")
    next_parser.add_argument(
        "--claim-root", type=Path, default=inbox_claim.default_claim_root()
    )
    next_parser.add_argument("--stale-after-seconds", type=int, default=600)
    claim_next_parser = sub.add_parser("claim-next")
    claim_next_parser.add_argument(
        "--claim-root", type=Path, default=inbox_claim.default_claim_root()
    )
    claim_next_parser.add_argument("--stale-after-seconds", type=int, default=600)
    sub.add_parser("assert-settled")
    sub.add_parser("manual-reviews")
    resolve_review_parser = sub.add_parser("resolve-manual-review")
    resolve_review_parser.add_argument("--review-key", required=True)
    sync_parser = sub.add_parser("sync-claim")
    sync_parser.add_argument("--message-id", required=True)
    sync_parser.add_argument("--claim-result", type=Path, required=True)
    reconcile_parser = sub.add_parser("reconcile-terminal")
    reconcile_parser.add_argument("--message-id", required=True)
    reconcile_parser.add_argument("--claim-root", type=Path, required=True)
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--message-id", required=True)
    finalize_parser.add_argument("--claim-root", type=Path, required=True)
    finalize_parser.add_argument("--claim-token")
    finalize_parser.add_argument(
        "--outcome", choices=("processed", "manual_review"), required=True
    )
    finalize_parser.add_argument("--reason-code")
    finalize_parser.add_argument("--record-root", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(
                args.root, read_json(args.capabilities), read_json(args.cron_config)
            )
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
        elif args.command == "reconfigure-adopt-disabled-live":
            result = adopt_disabled_live_reconfiguration(
                args.root,
                read_json(args.current_cron_config),
                read_json(args.live_job),
                args.workspace,
                args.base_dir,
            )
        elif args.command == "reconfigure-cancel":
            result = cancel_reconfiguration(
                args.root, read_json(args.current_cron_config)
            )
        elif args.command == "verify-legacy-binding":
            result = verify_legacy_binding(args.root, read_json(args.live_job))
            atomic_write_json(args.output, result)
        elif args.command == "status":
            result = load_monitor_state(args.root)
        elif args.command == "prepare-run":
            result = prepare_run_work(args.root)
        elif args.command == "discover-complete":
            batch = read_json(args.batch)
            result = discover_complete(
                args.root,
                batch,
                args.window_start_ms,
                args.window_end_ms,
            )
            cleanup_run_work(args.root, args.batch)
        elif args.command == "next":
            result = next_eligible(args.root, args.claim_root, args.stale_after_seconds)
        elif args.command == "claim-next":
            result = claim_next(args.root, args.claim_root, args.stale_after_seconds)
        elif args.command == "assert-settled":
            result = assert_settled(args.root)
        elif args.command == "manual-reviews":
            result = list_manual_reviews(args.root)
        elif args.command == "resolve-manual-review":
            result = resolve_manual_review(args.root, args.review_key)
        elif args.command == "sync-claim":
            result = sync_claim(
                args.root, args.message_id, read_json(args.claim_result)
            )
        elif args.command == "reconcile-terminal":
            result = reconcile_terminal(args.root, args.message_id, args.claim_root)
        else:
            token = args.claim_token or inbox_claim.authoritative_claim_token(
                args.claim_root, args.message_id
            )
            result = finalize_item(
                args.root,
                args.message_id,
                args.claim_root,
                token,
                args.outcome,
                args.reason_code,
                args.record_root,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
