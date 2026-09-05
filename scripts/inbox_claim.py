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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
# awaiting_owner: the desk asked the owner a question (WORKFLOW.md 6.10) and
# parked this claim; reopen() returns it to processing once the answer lands.
TERMINAL_STATUSES = {"processed", "manual_review", "awaiting_owner"}
REASONED_STATUSES = {"manual_review", "awaiting_owner"}
NOTIFICATION_STATUSES = {"pending", "sent", "failed_pre_delivery", "uncertain"}
NOTIFICATION_FIELDS = ("owner_notification", "manual_review_notification")
PROCESSING_PHASES = {
    "claimed": 0,
    "routed": 1,
    "ownership_confirmed": 2,
    "work_persisted": 3,
    "ready_to_finalize": 4,
}
EXTERNAL_ACTION_STATUSES = NOTIFICATION_STATUSES | {"verified_unsent"}
RECOVERY_LEASE_SECONDS = 360


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


def authoritative_claim_token(
    root: Path, message_id: str, *, allow_processed: bool = False, allow_parked: bool = False
) -> str:
    """Resolve a processing claim token from durable state, never model context."""
    state = read_state(claim_path(root, message_id))
    if state.get("message_id_sha256") != claim_key(message_id):
        raise ValueError("claim state does not match the Gmail message ID")
    allowed = {"processing", "processed"} if allow_processed else {"processing"}
    if allow_parked:
        # A claim parked behind one owner card (awaiting_owner) may still be
        # acted on by the executor of another card for the same message.
        allowed = allowed | {"awaiting_owner"}
    if state.get("status") not in allowed:
        raise ValueError("claim is not in an allowed state")
    return state["claim_token"]


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
        "manual_review_notification",
        "processing_phase",
        "phase_entered_at",
        "last_progress_at",
        "resume_count",
        "retry_count_at_phase",
        "recovery_lease_expires_at",
        "external_actions",
        "inline_attempts",
        "last_error",
        "last_error_kind",
        "last_error_at",
    }
    required = {
        "schema_version",
        "message_id_sha256",
        "claim_token",
        "status",
        "claimed_at",
    }
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
    for notification_field in NOTIFICATION_FIELDS:
        notification = state.get(notification_field)
        if notification is None:
            continue
        if not isinstance(notification, dict):
            raise ValueError(f"{notification_field} must be an object")
        if set(notification) != {"key", "status", "attempts", "updated_at"}:
            raise ValueError(
                f"{notification_field} contains missing or unsupported fields"
            )
        if notification.get("status") not in NOTIFICATION_STATUSES:
            raise ValueError(f"invalid {notification_field} status")
        if not isinstance(notification.get("key"), str):
            raise ValueError(f"invalid {notification_field} key")
        if (
            type(notification.get("attempts")) is not int
            or notification["attempts"] < 1
        ):
            raise ValueError(f"invalid {notification_field} attempts")
        if not isinstance(notification.get("updated_at"), str) or not notification[
            "updated_at"
        ]:
            raise ValueError(f"invalid {notification_field} updated_at")
    phase = state.get("processing_phase")
    if phase is not None and phase not in PROCESSING_PHASES:
        raise ValueError("invalid processing phase")
    if "last_progress_at" in state and (
        not isinstance(state["last_progress_at"], str) or not state["last_progress_at"]
    ):
        raise ValueError("invalid last_progress_at")
    if "phase_entered_at" in state and (
        not isinstance(state["phase_entered_at"], str) or not state["phase_entered_at"]
    ):
        raise ValueError("invalid phase_entered_at")
    if "resume_count" in state and (
        type(state["resume_count"]) is not int or state["resume_count"] < 0
    ):
        raise ValueError("invalid resume_count")
    if "retry_count_at_phase" in state and (
        type(state["retry_count_at_phase"]) is not int
        or state["retry_count_at_phase"] < 0
        or state["retry_count_at_phase"] > 1
    ):
        raise ValueError("invalid retry_count_at_phase")
    for timestamp_field in (
        "phase_entered_at",
        "last_progress_at",
        "recovery_lease_expires_at",
    ):
        raw_timestamp = state.get(timestamp_field)
        if raw_timestamp is None:
            continue
        try:
            parsed_timestamp = datetime.fromisoformat(raw_timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {timestamp_field}") from exc
        if parsed_timestamp.tzinfo is None:
            raise ValueError(f"{timestamp_field} must include timezone")
    actions = state.get("external_actions")
    if actions is not None:
        if not isinstance(actions, dict):
            raise ValueError("external_actions must be an object")
        for key, action in actions.items():
            if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", key):
                raise ValueError("invalid external action key")
            required_action_fields = {
                "category",
                "binding_sha256",
                "status",
                "attempts",
                "updated_at",
            }
            if (
                not isinstance(action, dict)
                or not required_action_fields.issubset(action)
                or not set(action).issubset(
                    required_action_fields
                    | {"provider_message_id", "provider_thread_id", "settled_by"}
                )
            ):
                raise ValueError(
                    "external action contains missing or unsupported fields"
                )
            if action.get("category") not in {
                "customer_delivery",
                "approval_request",
            }:
                raise ValueError("invalid external action category")
            if not isinstance(action.get("binding_sha256"), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", action["binding_sha256"]
            ):
                raise ValueError("invalid external action binding")
            if action.get("status") not in EXTERNAL_ACTION_STATUSES:
                raise ValueError("invalid external action status")
            if type(action.get("attempts")) is not int or action["attempts"] < 1:
                raise ValueError("invalid external action attempts")
            if (
                not isinstance(action.get("updated_at"), str)
                or not action["updated_at"]
            ):
                raise ValueError("invalid external action updated_at")
            for receipt_field in ("provider_message_id", "provider_thread_id"):
                if receipt_field in action and (
                    not isinstance(action[receipt_field], str)
                    or not action[receipt_field]
                    or len(action[receipt_field]) > 512
                ):
                    raise ValueError(f"invalid external action {receipt_field}")
    return state


def read_state(path: Path, attempts: int = 20) -> dict[str, Any]:
    state_path = path / "state.json"
    # A newly created claim directory may briefly precede its first state file.
    for attempt in range(attempts):
        try:
            raw = state_path.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.01)
    state = json.loads(raw)
    migrated = False
    if isinstance(state, dict) and "schema_version" not in state:
        legacy_required = {"message_id_sha256", "claim_token", "status", "claimed_at"}
        if not legacy_required.issubset(state) or state.get("status") not in {
            "processing",
            *TERMINAL_STATUSES,
        }:
            raise ValueError("unrecognized legacy claim state")
        state["schema_version"] = SCHEMA_VERSION
        migrated = True
    if (
        isinstance(state, dict)
        and state.get("processing_phase") in PROCESSING_PHASES
        and "retry_count_at_phase" not in state
    ):
        # Pre-bounded-recovery states cannot prove which phase an earlier
        # resume attempted. Treat any prior resume as the one allowed retry;
        # this is conservative and prevents an upgrade from retrying it again.
        resume_count = state.get("resume_count", 0)
        state["retry_count_at_phase"] = (
            1
            if (
                state.get("status") == "processing"
                and type(resume_count) is int
                and resume_count > 0
            )
            else 0
        )
        migrated = True
    if (
        isinstance(state, dict)
        and state.get("processing_phase") in PROCESSING_PHASES
        and "phase_entered_at" not in state
    ):
        state["phase_entered_at"] = state.get(
            "last_progress_at", state.get("claimed_at")
        )
        migrated = True
    if migrated:
        validated = validate_state(state)
        write_state(path, validated)
        return validated
    return validate_state(state)


def acquire(root: Path, message_id: str) -> tuple[bool, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = claim_path(root, message_id)
    token = secrets.token_hex(16)
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "schema_version": SCHEMA_VERSION,
        "message_id_sha256": claim_key(message_id),
        "claim_token": token,
        "status": "processing",
        "claimed_at": now,
        "processing_phase": "claimed",
        "phase_entered_at": now,
        "last_progress_at": now,
        "resume_count": 0,
        "retry_count_at_phase": 0,
    }
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return False, read_state(path)
    write_state(path, state)
    return True, state


def state_timestamp(state: dict[str, Any]) -> datetime:
    raw = state.get("last_progress_at", state.get("claimed_at"))
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("claim progress timestamp is invalid") from exc
    if value.tzinfo is None:
        raise ValueError("claim progress timestamp must include timezone")
    return value


def has_ambiguous_external_action(state: dict[str, Any]) -> bool:
    for notification_field in NOTIFICATION_FIELDS:
        notification = state.get(notification_field)
        if isinstance(notification, dict) and notification.get("status") in {
            "pending",
            "uncertain",
        }:
            return True
    return any(
        action.get("status") in {"pending", "uncertain"}
        for action in state.get("external_actions", {}).values()
    )


def recovery_lease_active(state: dict[str, Any], now: datetime | None = None) -> bool:
    raw = state.get("recovery_lease_expires_at")
    if raw is None:
        return False
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        expires = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid recovery_lease_expires_at") from exc
    if expires.tzinfo is None:
        raise ValueError("recovery_lease_expires_at must include timezone")
    return current < expires


def resume_stale(
    root: Path,
    message_id: str,
    minimum_age_seconds: int,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Resume one stale claim only when every external action is settled."""
    if type(minimum_age_seconds) is not int or minimum_age_seconds < 1:
        raise ValueError("minimum_age_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("status") != "processing":
            return False, state
        if (current - state_timestamp(state)).total_seconds() < minimum_age_seconds:
            return False, state
        # Claims created before the phase journal cannot prove that an
        # unrecorded external action did not happen.
        if state.get("processing_phase") not in PROCESSING_PHASES:
            return False, state
        if has_ambiguous_external_action(state):
            return False, state
        if recovery_lease_active(state, current):
            return False, state
        if state.get("retry_count_at_phase", 0) >= 1:
            return False, state
        state["resume_count"] = state.get("resume_count", 0) + 1
        state["retry_count_at_phase"] = 1
        state["recovery_lease_expires_at"] = (
            current + timedelta(seconds=RECOVERY_LEASE_SECONDS)
        ).isoformat()
        write_state(path, state)
        return True, state


def delegate(
    root: Path,
    message_id: str,
    claim_token: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Lease a processing claim to a worker job for a bounded time.

    The watcher hands judgment work to a separate job. While that job runs,
    the claim must look busy to every other tick, or the stale reconciler
    would resume or fail it under the worker. The existing recovery lease
    already means "someone owns this until then", so the same field carries
    the worker's deadline, and nothing else about the claim changes.
    """
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError("lease_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("status") != "processing":
            raise ValueError("only a processing claim can be delegated")
        if state.get("claim_token") != claim_token:
            raise ValueError("claim token does not match the authoritative claim")
        state["recovery_lease_expires_at"] = (
            current + timedelta(seconds=lease_seconds)
        ).isoformat()
        state["last_progress_at"] = current.isoformat()
        write_state(path, state)
        return state


def reopen(
    root: Path,
    message_id: str,
    lease_seconds: int,
    allow_manual_review: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a parked (awaiting_owner) claim to processing under a new token.

    The owner has answered, so the inquiry resumes from where it stopped: the
    phase journal is kept, the claim gets a fresh token and a worker lease, and
    the count of resumes goes up so a repeat is visible in the record.
    """
    if type(lease_seconds) is not int or lease_seconds < 1:
        raise ValueError("lease_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        allowed = {"awaiting_owner", "manual_review"} if allow_manual_review else {"awaiting_owner"}
        if state.get("status") not in allowed:
            raise ValueError(f"claim is {state.get('status')}, not awaiting_owner; nothing to reopen")
        state["status"] = "processing"
        state["claim_token"] = secrets.token_hex(16)
        state.pop("finished_at", None)
        state.pop("reason_code", None)
        state["resume_count"] = state.get("resume_count", 0) + 1
        state["retry_count_at_phase"] = 0
        state["last_progress_at"] = current.isoformat()
        state["recovery_lease_expires_at"] = (
            current + timedelta(seconds=lease_seconds)
        ).isoformat()
        write_state(path, state)
        return state


def authorize_legacy_resume(
    root: Path,
    message_id: str,
    token: str,
    minimum_age_seconds: int,
    confirmed_no_external_actions: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Journal one legacy claim after a human verifies that it caused no effects.

    This is intentionally separate from automatic stale recovery. The cron must
    never infer that a pre-journal claim is safe merely because its state file
    lacks action records.
    """
    if confirmed_no_external_actions is not True:
        raise ValueError("explicit no-external-actions confirmation is required")
    if type(minimum_age_seconds) is not int or minimum_age_seconds < 1:
        raise ValueError("minimum_age_seconds must be a positive integer")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if state.get("status") != "processing":
            raise ValueError("only a processing legacy claim can be authorized")
        if state.get("processing_phase") in PROCESSING_PHASES:
            raise ValueError("claim already has a phase journal")
        if (current - state_timestamp(state)).total_seconds() < minimum_age_seconds:
            raise ValueError(
                "legacy claim is not stale enough for manual authorization"
            )
        if (
            state.get("owner_notification") is not None
            or state.get("manual_review_notification") is not None
            or state.get("external_actions")
        ):
            raise ValueError(
                "legacy claim contains action evidence; manual review required"
            )
        state["processing_phase"] = "claimed"
        state["phase_entered_at"] = current.isoformat()
        state["last_progress_at"] = current.isoformat()
        state["resume_count"] = state.get("resume_count", 0) + 1
        state["retry_count_at_phase"] = 0
        state.pop("recovery_lease_expires_at", None)
        write_state(path, state)
        return state


def advance_phase(
    root: Path, message_id: str, token: str, phase: str
) -> dict[str, Any]:
    if phase not in PROCESSING_PHASES:
        raise ValueError("invalid processing phase")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if state.get("status") != "processing":
            raise ValueError("only processing claims can advance")
        current = state.get("processing_phase", "claimed")
        if current not in PROCESSING_PHASES:
            raise ValueError("legacy claim requires manual recovery")
        if PROCESSING_PHASES[phase] <= PROCESSING_PHASES[current]:
            return state
        now = datetime.now(timezone.utc).isoformat()
        state["processing_phase"] = phase
        state["phase_entered_at"] = now
        state["last_progress_at"] = now
        state["retry_count_at_phase"] = 0
        state.pop("recovery_lease_expires_at", None)
        write_state(path, state)
        return state


def acquire_external_action(
    root: Path,
    message_id: str,
    token: str,
    action_key: str,
    category: str,
    binding_sha256: str,
    allow_processed: bool = False,
    allow_parked: bool = False,
) -> tuple[bool, dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", action_key):
        raise ValueError("invalid external action key")
    if category not in {"customer_delivery", "approval_request"}:
        raise ValueError("invalid external action category")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", binding_sha256):
        raise ValueError("invalid external action binding")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        allowed = {"processing", "processed"} if allow_processed else {"processing"}
        if allow_parked:
            allowed = allowed | {"awaiting_owner"}
        if state.get("status") not in allowed:
            raise ValueError("external action requires an allowed claim state")
        actions = state.setdefault("external_actions", {})
        prior = actions.get(action_key)
        attempts = 1
        if prior is not None:
            if (
                prior.get("category") != category
                or prior.get("binding_sha256") != binding_sha256
            ):
                raise ValueError("external action binding changed")
            if (
                prior.get("status") == "failed_pre_delivery"
                and prior.get("attempts") == 1
            ):
                attempts = 2
            elif prior.get("status") == "verified_unsent" and int(prior.get("attempts") or 1) < 3:
                # The provider was asked and does not have the message: the
                # same payload may go again. Verified, never assumed.
                attempts = int(prior.get("attempts") or 1) + 1
            elif prior.get("status") in {"pending", "sent", "uncertain", "verified_unsent"}:
                # pending: the run died around the provider call; whether the
                # message went is unknown until the thread is read. The caller
                # verifies and settles; nothing is resent on a guess.
                return False, state
            else:
                raise ValueError(f"external action is already {prior.get('status')}")
        now = datetime.now(timezone.utc).isoformat()
        actions[action_key] = {
            "category": category,
            "binding_sha256": binding_sha256,
            "status": "pending",
            "attempts": attempts,
            "updated_at": now,
        }
        state["last_progress_at"] = now
        write_state(path, state)
        return True, state


INLINE_ERROR_KINDS = {"transient", "deterministic"}


def mark_inline(root: Path, message_id: str, token: str, inline: bool) -> dict[str, Any]:
    """Say whether the tick itself owns this processing claim (inline) or a worker does."""
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if inline:
            state.setdefault("inline_attempts", 0)
        else:
            for field in ("inline_attempts", "last_error", "last_error_kind", "last_error_at"):
                state.pop(field, None)
        write_state(path, state)
        return state


def release_lease(root: Path, message_id: str, token: str) -> dict[str, Any]:
    """End this run's lease now: the next tick may take the claim at once."""
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        state["recovery_lease_expires_at"] = datetime.now(timezone.utc).isoformat()
        write_state(path, state)
        return state


def note_inline_attempt(root: Path, message_id: str, token: str, error: str, kind: str) -> int:
    """Record one failed inline attempt; returns the count so far."""
    if kind not in INLINE_ERROR_KINDS:
        raise ValueError("error kind must be transient or deterministic")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        attempts = int(state.get("inline_attempts") or 0) + 1
        state["inline_attempts"] = attempts
        state["last_error"] = str(error)[:300]
        state["last_error_kind"] = kind
        state["last_error_at"] = datetime.now(timezone.utc).isoformat()
        state["last_progress_at"] = state["last_error_at"]
        write_state(path, state)
        return attempts


def settle_external_action(
    root: Path,
    message_id: str,
    token: str,
    action_key: str,
    status: str,
    provider_message_id: str | None = None,
    provider_thread_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a pending or uncertain action from evidence: the provider was read.

    `sent` records the provider ids the thread showed; `verified_unsent`
    records that the provider does not have the message, so the same payload
    may be sent once more.
    """
    if status not in {"sent", "verified_unsent"}:
        raise ValueError("a delivery settles as sent or verified_unsent")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        action = state.get("external_actions", {}).get(action_key)
        if not isinstance(action, dict) or action.get("status") not in {"pending", "uncertain"}:
            raise ValueError("only a pending or uncertain delivery can be settled")
        now = datetime.now(timezone.utc).isoformat()
        if status == "sent" and action.get("category") == "customer_delivery":
            for value, field in (
                (provider_message_id, "provider_message_id"),
                (provider_thread_id, "provider_thread_id"),
            ):
                if not isinstance(value, str) or not value or len(value) > 512:
                    raise ValueError(f"{field} is required for sent customer delivery")
            action["provider_message_id"] = provider_message_id
            action["provider_thread_id"] = provider_thread_id
        action["status"] = status
        action["settled_by"] = "provider_read"
        action["updated_at"] = now
        state["last_progress_at"] = now
        write_state(path, state)
        return state


def finish_external_action(
    root: Path,
    message_id: str,
    token: str,
    action_key: str,
    status: str,
    provider_message_id: str | None = None,
    provider_thread_id: str | None = None,
) -> dict[str, Any]:
    if status not in {"sent", "failed_pre_delivery", "uncertain"}:
        raise ValueError("invalid terminal external action status")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        action = state.get("external_actions", {}).get(action_key)
        if not isinstance(action, dict) or action.get("status") != "pending":
            raise ValueError("external action is not pending")
        now = datetime.now(timezone.utc).isoformat()
        if action.get("category") == "customer_delivery" and status == "sent":
            for value, field in (
                (provider_message_id, "provider_message_id"),
                (provider_thread_id, "provider_thread_id"),
            ):
                if not isinstance(value, str) or not value or len(value) > 512:
                    raise ValueError(f"{field} is required for sent customer delivery")
            action["provider_message_id"] = provider_message_id
            action["provider_thread_id"] = provider_thread_id
        action["status"] = status
        action["updated_at"] = now
        state["last_progress_at"] = now
        write_state(path, state)
        return state


def finish(
    root: Path,
    message_id: str,
    token: str,
    status: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    path = claim_path(root, message_id)
    if status not in TERMINAL_STATUSES:
        raise ValueError("invalid terminal claim status")
    if status == "processed" and reason_code is not None:
        raise ValueError("reason_code is allowed only for manual_review or awaiting_owner")
    if status in REASONED_STATUSES and (
        not reason_code
        or len(reason_code) > 80
        or not all(
            character.islower() or character.isdigit() or character == "_"
            for character in reason_code
        )
    ):
        raise ValueError(
            "reason_code must use 1-80 lowercase letters, digits, or underscores"
        )

    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if state.get("status") != "processing":
            same_outcome = state.get("status") == status and (
                status == "processed" or state.get("reason_code") == reason_code
            )
            if same_outcome:
                return state
            raise ValueError(
                f"claim already has conflicting terminal outcome {state.get('status')}"
            )
        if status == "processed":
            if state.get("processing_phase") != "ready_to_finalize":
                raise ValueError(
                    "processing claim is not ready_to_finalize; refusing processed outcome"
                )
            if has_ambiguous_external_action(state):
                raise ValueError(
                    "processing claim has an ambiguous external action; "
                    "refusing processed outcome"
                )
        state["status"] = status
        if reason_code is not None:
            state["reason_code"] = reason_code
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        state["last_progress_at"] = state["finished_at"]
        write_state(path, state)
        return state


def acquire_notification(
    root: Path,
    message_id: str,
    token: str,
    notification_key: str,
    *,
    notification_field: str = "owner_notification",
) -> tuple[bool, dict[str, Any]]:
    if notification_field not in NOTIFICATION_FIELDS:
        raise ValueError("invalid notification field")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        if (
            not notification_key
            or len(notification_key) > 200
            or not all(
                character.isalnum() or character in "_.:@-"
                for character in notification_key
            )
        ):
            raise ValueError("invalid notification key")
        prior = state.get(notification_field)
        attempts = 1
        if prior is not None:
            if prior.get("key") != notification_key:
                raise ValueError(
                    "a different notification is already bound to this message"
                )
            # Retained for explicit manual recovery; the normal Kolo wrapper
            # classifies every post-invocation failure as uncertain.
            if (
                prior.get("status") == "failed_pre_delivery"
                and prior.get("attempts") == 1
            ):
                attempts = 2
            elif prior.get("status") in {"pending", "sent", "uncertain"}:
                return False, state
            else:
                raise ValueError(f"notification is already {prior.get('status')}")
        now = datetime.now(timezone.utc).isoformat()
        state[notification_field] = {
            "key": notification_key,
            "status": "pending",
            "attempts": attempts,
            "updated_at": now,
        }
        state["last_progress_at"] = now
        write_state(path, state)
        return True, state


def begin_notification(
    root: Path,
    message_id: str,
    token: str,
    notification_key: str,
    *,
    notification_field: str = "owner_notification",
) -> dict[str, Any]:
    acquired, state = acquire_notification(
        root,
        message_id,
        token,
        notification_key,
        notification_field=notification_field,
    )
    if not acquired:
        raise ValueError(
            f"notification is already {state[notification_field]['status']}"
        )
    return state


def finish_notification(
    root: Path,
    message_id: str,
    token: str,
    status: str,
    *,
    notification_field: str = "owner_notification",
) -> dict[str, Any]:
    if notification_field not in NOTIFICATION_FIELDS:
        raise ValueError("invalid notification field")
    if status not in {"sent", "failed_pre_delivery", "uncertain"}:
        raise ValueError("invalid terminal notification status")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        if state.get("claim_token") != token:
            raise ValueError("claim token does not match")
        notification = state.get(notification_field)
        if (
            not isinstance(notification, dict)
            or notification.get("status") != "pending"
        ):
            raise ValueError("notification is not pending")
        now = datetime.now(timezone.utc).isoformat()
        notification["status"] = status
        notification["updated_at"] = now
        state["last_progress_at"] = now
        write_state(path, state)
        return state


def reconcile_notification(
    root: Path,
    message_id: str,
    *,
    notification_field: str = "owner_notification",
) -> dict[str, Any]:
    if notification_field not in NOTIFICATION_FIELDS:
        raise ValueError("invalid notification field")
    path = claim_path(root, message_id)
    with state_lock(path):
        state = read_state(path)
        notification = state.get(notification_field)
        if isinstance(notification, dict) and notification.get("status") == "pending":
            notification["status"] = "uncertain"
            notification["updated_at"] = datetime.now(timezone.utc).isoformat()
            state["last_progress_at"] = notification["updated_at"]
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
            changed = False
            for notification_field in NOTIFICATION_FIELDS:
                notification = state.get(notification_field)
                if (
                    not isinstance(notification, dict)
                    or notification.get("status") != "pending"
                ):
                    continue
                summary["pending"] += 1
                try:
                    updated_at = datetime.fromisoformat(notification["updated_at"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid {notification_field} updated_at"
                    ) from exc
                if updated_at.tzinfo is None:
                    raise ValueError(
                        f"{notification_field} updated_at must include timezone"
                    )
                if (current - updated_at).total_seconds() < minimum_age_seconds:
                    continue
                notification["status"] = "uncertain"
                notification["updated_at"] = current.isoformat()
                state["last_progress_at"] = notification["updated_at"]
                summary["reconciled"] += 1
                changed = True
            if changed:
                write_state(path, state)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=default_claim_root())
    sub = parser.add_subparsers(dest="command", required=True)
    claim = sub.add_parser("claim")
    claim.add_argument("--message-id", required=True)
    claim.add_argument("--resume-stale-after-seconds", type=int)
    phase = sub.add_parser("advance-phase")
    phase.add_argument("--message-id", required=True)
    phase.add_argument("--claim-token")
    phase.add_argument("--phase", choices=tuple(PROCESSING_PHASES), required=True)
    authorize = sub.add_parser("authorize-legacy-resume")
    authorize.add_argument("--message-id", required=True)
    authorize.add_argument("--claim-token", required=True)
    authorize.add_argument("--minimum-age-seconds", type=int, required=True)
    authorize.add_argument(
        "--confirmed-no-external-actions", action="store_true", required=True
    )
    for name in ("complete", "fail"):
        command = sub.add_parser(name)
        command.add_argument("--message-id", required=True)
        command.add_argument(
            "--claim-token", "--token", dest="claim_token", required=True
        )
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
            resumed = False
            if (
                not acquired
                and args.resume_stale_after_seconds is not None
                and state.get("status") == "processing"
            ):
                acquired, state = resume_stale(
                    args.root, args.message_id, args.resume_stale_after_seconds
                )
                resumed = acquired
            print(
                json.dumps(
                    {"acquired": acquired, "resumed": resumed, **state},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "advance-phase":
            token = args.claim_token or authoritative_claim_token(
                args.root, args.message_id
            )
            state = advance_phase(args.root, args.message_id, token, args.phase)
            print(json.dumps(state, sort_keys=True))
            return 0
        if args.command == "authorize-legacy-resume":
            state = authorize_legacy_resume(
                args.root,
                args.message_id,
                args.claim_token,
                args.minimum_age_seconds,
                args.confirmed_no_external_actions,
            )
            print(json.dumps(state, sort_keys=True))
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
            result = reconcile_stale_notifications(
                root=args.root, minimum_age_seconds=args.minimum_age_seconds
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        status = "processed" if args.command == "complete" else "manual_review"
        reason_code = args.reason_code if args.command == "fail" else None
        state = finish(
            args.root, args.message_id, args.claim_token, status, reason_code
        )
        print(json.dumps(state, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
