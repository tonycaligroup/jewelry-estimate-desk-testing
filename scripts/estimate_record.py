#!/usr/bin/env python3
"""Maintain the private local estimate records used for inbox routing."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import route_ownership
import approval_guard


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def default_record_root() -> Path:
    configured_workspace = os.environ.get("OPENCLAW_WORKSPACE")
    workspace = (
        Path(configured_workspace).expanduser()
        if configured_workspace
        else Path.home() / ".openclaw" / "workspace-main"
    )
    return workspace.resolve() / "estimate-desk" / "records"


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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
def record_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / ".records.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def estimate_id_for_route(route: dict[str, Any]) -> str:
    message_id = route.get("gmail_message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("route.gmail_message_id must be non-empty text")
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
    return f"jed-{digest}"


def build_initial_record(
    route: dict[str, Any], inbound_timestamp_ms: int
) -> dict[str, Any]:
    if type(inbound_timestamp_ms) is not int or inbound_timestamp_ms < 0:
        raise ValueError("inbound_timestamp_ms must be a non-negative integer")
    if route.get("channel") != "gmail":
        raise ValueError("route.channel must be gmail")
    record = {
        "schema_version": 1,
        "estimate_id": estimate_id_for_route(route),
        "status": "awaiting_specs",
        "route": route,
        "inbound_timestamp_ms": inbound_timestamp_ms,
    }
    route_ownership.validate_record(record)
    return record


def record_path(root: Path, estimate_id: str) -> Path:
    if not route_ownership.ESTIMATE_ID_RE.fullmatch(estimate_id):
        raise ValueError("invalid estimate_id")
    return root / f"{estimate_id}.json"


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_provider_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{field} must contain 1-512 characters")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} contains invalid characters")
    return value


def preserve_append_only(
    existing: dict[str, Any], proposed: dict[str, Any], field: str
) -> dict[str, Any]:
    existing_values = existing.get(field, [])
    proposed_values = proposed.get(field, [])
    if not isinstance(existing_values, list) or not isinstance(proposed_values, list):
        raise ValueError(f"{field} must be an array")
    if existing_values and not proposed_values:
        proposed = dict(proposed)
        proposed[field] = existing_values
        proposed_values = existing_values
    if proposed_values[: len(existing_values)] != existing_values:
        raise ValueError(f"{field} is immutable and append-only")
    if len(proposed_values) < len(existing_values):
        raise ValueError(f"{field} cannot be removed")
    return proposed


def reject_duplicate_thread_record(root: Path, record: dict[str, Any]) -> None:
    thread_id = record["route"]["thread_id"]
    estimate_id = record["estimate_id"]
    for path in root.glob("jed-*.json"):
        if path.name == f"{estimate_id}.json":
            continue
        other = read_object(path)
        other_route = other.get("route")
        if isinstance(other_route, dict) and other_route.get("thread_id") == thread_id:
            raise ValueError("a different estimate record already owns this thread")


def persist_record(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    route_ownership.validate_record(record)
    path = record_path(root, record["estimate_id"])
    with record_lock(root):
        reject_duplicate_thread_record(root, record)
        if path.exists():
            existing = read_object(path)
            route_ownership.validate_record(existing)
            if existing["route"] != record["route"]:
                raise ValueError("estimate route is immutable")
            existing_reply = existing.get("spec_gate_reply")
            proposed_reply = record.get("spec_gate_reply")
            if existing_reply is not None:
                if proposed_reply is not None and proposed_reply != existing_reply:
                    raise ValueError("spec-gate send evidence is immutable")
                if proposed_reply is None:
                    record = dict(record)
                    record["spec_gate_reply"] = existing_reply
            for field in (
                "followup_replies",
                "thread_reviews",
                "approval_requests",
                "rendering_deliveries",
            ):
                record = preserve_append_only(existing, record, field)
            existing_delivery = existing.get("estimate_delivery")
            proposed_delivery = record.get("estimate_delivery")
            if existing_delivery is not None:
                if (
                    proposed_delivery is not None
                    and proposed_delivery != existing_delivery
                ):
                    raise ValueError("estimate delivery evidence is immutable")
                if proposed_delivery is None:
                    record = dict(record)
                    record["estimate_delivery"] = existing_delivery
            existing_binding = existing.get("approval_binding_hash")
            if existing_binding is not None:
                existing_source = existing.get("approval_source_message_id")
                proposed_source = record.get("approval_source_message_id")
                if proposed_source is None:
                    record = dict(record)
                    record["approval_source_message_id"] = existing_source
                elif proposed_source != existing_source:
                    raise ValueError("approval source message ID is immutable")
                bound_state = {
                    "estimate_id": record.get("estimate_id"),
                    "route": record.get("route"),
                    "specification": record.get("specification"),
                    "proposed_price": record.get("proposed_price"),
                    "internal_cost_sheet": record.get("internal_cost_sheet"),
                }
                try:
                    proposed_binding = approval_guard.binding_hash(bound_state)
                except ValueError as exc:
                    raise ValueError("approval-bound estimate state is invalid") from exc
                if proposed_binding != existing_binding:
                    raise ValueError("approval-bound estimate state is immutable")
        write_object(path, record)
    return record


def create_initial_record(
    root: Path, route: dict[str, Any], inbound_timestamp_ms: int
) -> dict[str, Any]:
    proposed = build_initial_record(route, inbound_timestamp_ms)
    path = record_path(root, proposed["estimate_id"])
    with record_lock(root):
        reject_duplicate_thread_record(root, proposed)
        if path.exists():
            existing = read_object(path)
            route_ownership.validate_record(existing)
            if existing["route"] != proposed["route"]:
                raise ValueError("estimate route is immutable")
            return existing
        write_object(path, proposed)
    return proposed


def lookup_thread(root: Path, route: dict[str, Any]) -> list[dict[str, Any]]:
    thread_id = route.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("route.thread_id must be non-empty text")
    if not root.exists():
        return []
    candidates: list[dict[str, Any]] = []
    with record_lock(root):
        for path in sorted(root.glob("jed-*.json")):
            record = read_object(path)
            record_route = record.get("route")
            if (
                isinstance(record_route, dict)
                and record_route.get("thread_id") == thread_id
            ):
                candidates.append(record)
    return candidates


def record_spec_gate_sent(
    root: Path,
    estimate_id: str,
    reply_body: str,
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Persist privacy-minimal Gmail evidence for the initial specification reply."""
    if not reply_body.strip():
        raise ValueError("spec-gate reply body must not be empty")
    provider_message_id = provider_response.get("id")
    provider_thread_id = provider_response.get("threadId")
    for value, field in (
        (provider_message_id, "provider response id"),
        (provider_thread_id, "provider response threadId"),
    ):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError(f"{field} must contain 1-512 characters")
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError(f"{field} contains invalid characters")

    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] != "awaiting_specs":
            raise ValueError("spec-gate evidence requires awaiting_specs status")
        if provider_thread_id != record["route"]["thread_id"]:
            raise ValueError(
                "provider response threadId does not match the owned thread"
            )
        evidence = {
            "status": "sent",
            "provider_message_id": provider_message_id,
            "thread_id": provider_thread_id,
            "body_sha256": "sha256:"
            + hashlib.sha256(reply_body.encode("utf-8")).hexdigest(),
        }
        existing = record.get("spec_gate_reply")
        if existing is not None:
            comparable = dict(existing) if isinstance(existing, dict) else existing
            if isinstance(comparable, dict):
                comparable.pop("sent_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting spec-gate send evidence already exists")
        evidence["sent_at"] = datetime.now(timezone.utc).isoformat()
        record["spec_gate_reply"] = evidence
        write_object(path, record)
        return record


def record_followup_sent(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    reply_body: str,
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Append immutable evidence for a later same-thread specification reply."""
    if not source_message_id or len(source_message_id) > 512:
        raise ValueError("source message ID must contain 1-512 characters")
    if not reply_body.strip():
        raise ValueError("follow-up reply body must not be empty")
    provider_message_id = provider_response.get("id")
    provider_thread_id = provider_response.get("threadId")
    for value, field in (
        (provider_message_id, "provider response id"),
        (provider_thread_id, "provider response threadId"),
    ):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError(f"{field} must contain 1-512 characters")
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError(f"{field} contains invalid characters")

    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] != "awaiting_specs":
            raise ValueError("specification follow-up requires awaiting_specs status")
        if provider_thread_id != record["route"]["thread_id"]:
            raise ValueError(
                "provider response threadId does not match the owned thread"
            )
        evidence = {
            "status": "sent",
            "source_message_id_sha256": "sha256:"
            + hashlib.sha256(source_message_id.encode("utf-8")).hexdigest(),
            "provider_message_id": provider_message_id,
            "thread_id": provider_thread_id,
            "body_sha256": "sha256:"
            + hashlib.sha256(reply_body.encode("utf-8")).hexdigest(),
        }
        followups = record.setdefault("followup_replies", [])
        if not isinstance(followups, list):
            raise ValueError("followup_replies must be an array")
        for existing in followups:
            if not isinstance(existing, dict):
                raise ValueError("followup_replies contains invalid evidence")
            if (
                existing.get("source_message_id_sha256")
                != evidence["source_message_id_sha256"]
            ):
                continue
            comparable = dict(existing)
            comparable.pop("sent_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting follow-up evidence for source message")
        evidence["sent_at"] = datetime.now(timezone.utc).isoformat()
        followups.append(evidence)
        write_object(path, record)
        return record


def record_thread_review(
    root: Path,
    estimate_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Persist a privacy-minimal full-thread specification review."""
    thread_id = validate_provider_id(snapshot.get("thread_id"), "thread_id")
    source_message_id = validate_provider_id(
        snapshot.get("source_message_id"), "source_message_id"
    )
    message_ids = snapshot.get("message_ids")
    if not isinstance(message_ids, list) or not message_ids:
        raise ValueError("message_ids must be a non-empty array")
    validated_ids = [
        validate_provider_id(value, "message_ids entry") for value in message_ids
    ]
    if len(set(validated_ids)) != len(validated_ids):
        raise ValueError("message_ids must not contain duplicates")
    if source_message_id not in validated_ids:
        raise ValueError("source_message_id must be present in message_ids")
    specification = snapshot.get("specification")
    if not isinstance(specification, dict) or not specification:
        raise ValueError("specification must be a non-empty object")
    missing = snapshot.get("missing_required_fields")
    if not isinstance(missing, list) or any(
        not isinstance(field, str)
        or not field
        or len(field) > 80
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in field
        )
        for field in missing
    ):
        raise ValueError("missing_required_fields must contain lowercase field keys")
    if len(set(missing)) != len(missing):
        raise ValueError("missing_required_fields must not contain duplicates")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if thread_id != record["route"]["thread_id"]:
            raise ValueError("thread review does not match the owned thread")
        if record["route"]["gmail_message_id"] not in validated_ids:
            raise ValueError("thread review must include the initiating Gmail message")
        post_estimate = record["status"] in {
            "estimate_sent",
            "appointment_booked",
            "approved",
        }
        if post_estimate:
            if missing:
                raise ValueError(
                    "post-estimate continuation cannot reopen the specification gate"
                )
            if specification != record.get("specification"):
                raise ValueError(
                    "post-estimate specification changed; new owner approval required"
                )
            outcome = "post_estimate_continuation"
        else:
            outcome = "awaiting_specs" if missing else "specs_complete"
        evidence = {
            "source_message_id_sha256": sha256_text(source_message_id),
            "thread_id": thread_id,
            "thread_message_count": len(validated_ids),
            "thread_context_sha256": canonical_sha256(validated_ids),
            "specification_sha256": canonical_sha256(specification),
            "missing_required_fields": sorted(missing),
            "outcome": outcome,
        }
        reviews = record.setdefault("thread_reviews", [])
        if not isinstance(reviews, list):
            raise ValueError("thread_reviews must be an array")
        for existing in reviews:
            if not isinstance(existing, dict):
                raise ValueError("thread_reviews contains invalid evidence")
            if (
                existing.get("source_message_id_sha256")
                != evidence["source_message_id_sha256"]
            ):
                continue
            comparable = dict(existing)
            comparable.pop("recorded_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting thread review for source message")
        if record["status"] != "awaiting_specs" and not post_estimate:
            raise ValueError(
                "thread specification review requires an active estimate status"
            )
        evidence["recorded_at"] = datetime.now(timezone.utc).isoformat()
        reviews.append(evidence)
        if not post_estimate:
            record["specification"] = specification
            record["missing_required_fields"] = sorted(missing)
            record["status"] = "awaiting_specs"
        write_object(path, record)
        return record


def _validate_approval_request(
    record: dict[str, Any],
    estimate_id: str,
    source_message_id: str,
    approval_request: dict[str, Any],
) -> dict[str, Any]:
    """Validate an approval against authoritative record state without writing."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if approval_request.get("estimate_id") != estimate_id:
        raise ValueError("approval request estimate_id does not match")
    binding_hash = approval_request.get("binding_hash")
    if not isinstance(binding_hash, str) or not HASH_RE.fullmatch(binding_hash):
        raise ValueError("approval request binding_hash is invalid")
    proposed_price = approval_request.get("proposed_price")
    if isinstance(proposed_price, bool) or not isinstance(proposed_price, (int, float)):
        raise ValueError("approval request proposed_price must be numeric")
    if approval_guard.binding_hash(approval_request) != binding_hash:
        raise ValueError("approval request binding_hash does not match its contents")
    route_ownership.validate_record(record)
    if approval_request.get("route") != record["route"]:
        raise ValueError("approval request route does not match the record")
    if approval_request.get("specification") != record.get("specification"):
        raise ValueError("approval request specification does not match the record")
    source_hash = sha256_text(source_message_id)
    review = next(
        (
            item
            for item in record.get("thread_reviews", [])
            if isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
            and item.get("outcome") == "specs_complete"
        ),
        None,
    )
    if review is None:
        raise ValueError("approval request lacks a matching complete thread review")
    return {
        "source_message_id_sha256": source_hash,
        "binding_hash": binding_hash,
        "proposed_price": proposed_price,
    }


def prepare_approval_state(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Bind model-produced pricing to the record's immutable route and specs."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if candidate.get("estimate_id") != estimate_id:
        raise ValueError("current state estimate_id does not match the command")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        source_hash = sha256_text(source_message_id)
        if not any(
            isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
            and item.get("outcome") == "specs_complete"
            for item in record.get("thread_reviews", [])
        ):
            raise ValueError("approval request lacks a matching complete thread review")
        if record.get("status") == "pending_approval":
            if record.get("approval_source_message_id") != source_message_id:
                raise ValueError("pending approval belongs to a different source message")
            state = {
                "estimate_id": record["estimate_id"],
                "route": record["route"],
                "specification": record.get("specification"),
                "proposed_price": record.get("proposed_price"),
                "internal_cost_sheet": record.get("internal_cost_sheet"),
            }
            if approval_guard.binding_hash(state) != record.get(
                "approval_binding_hash"
            ):
                raise ValueError(
                    "authoritative approval state does not match its binding"
                )
            return state
        if record.get("status") != "awaiting_specs":
            raise ValueError("approval preparation requires awaiting_specs status")
        state = dict(candidate)
        state.update(
            {
                "estimate_id": record["estimate_id"],
                "route": record["route"],
                "specification": record.get("specification"),
                "proposed_price": candidate.get("proposed_price"),
                "internal_cost_sheet": candidate.get("internal_cost_sheet"),
            }
        )
        approval_guard.binding_payload(state)
        return state


def validate_approval_request(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    approval_request: dict[str, Any],
) -> dict[str, Any]:
    """Preflight an approval before any external request is attempted."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        evidence = _validate_approval_request(
            record, estimate_id, source_message_id, approval_request
        )
        matching = next(
            (
                item
                for item in record.get("approval_requests", [])
                if isinstance(item, dict)
                and item.get("source_message_id_sha256")
                == evidence["source_message_id_sha256"]
            ),
            None,
        )
        if matching is not None:
            comparable = dict(matching)
            comparable.pop("requested_at", None)
            if comparable != evidence:
                raise ValueError("conflicting approval request for source message")
        elif record.get("status") != "awaiting_specs":
            raise ValueError("approval evidence requires awaiting_specs status")
        return approval_request


def record_approval_requested(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    approval_request: dict[str, Any],
) -> dict[str, Any]:
    """Append owner-approval evidence after Kolo accepts the claimed request."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        evidence = _validate_approval_request(
            record, estimate_id, source_message_id, approval_request
        )
        requests = record.setdefault("approval_requests", [])
        if not isinstance(requests, list):
            raise ValueError("approval_requests must be an array")
        for existing in requests:
            if not isinstance(existing, dict):
                raise ValueError("approval_requests contains invalid evidence")
            if (
                existing.get("source_message_id_sha256")
                != evidence["source_message_id_sha256"]
            ):
                continue
            comparable = dict(existing)
            comparable.pop("requested_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting approval request for source message")
        if record["status"] != "awaiting_specs":
            raise ValueError("approval evidence requires awaiting_specs status")
        evidence["requested_at"] = datetime.now(timezone.utc).isoformat()
        requests.append(evidence)
        record["approval_binding_hash"] = evidence["binding_hash"]
        record["approval_source_message_id"] = source_message_id
        record["proposed_price"] = evidence["proposed_price"]
        record["internal_cost_sheet"] = approval_request["internal_cost_sheet"]
        record["missing_required_fields"] = []
        record["status"] = "pending_approval"
        write_object(path, record)
        return record


def current_approval_state(root: Path, estimate_id: str) -> dict[str, Any]:
    """Reconstruct the exact approval-bound state after claim work cleanup."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") not in {"pending_approval", "estimate_sent"}:
            raise ValueError("estimate does not have an approval-bound state")
        state = {
            "estimate_id": record["estimate_id"],
            "route": record["route"],
            "specification": record.get("specification"),
            "proposed_price": record.get("proposed_price"),
            "internal_cost_sheet": record.get("internal_cost_sheet"),
        }
        expected = record.get("approval_binding_hash")
        if approval_guard.binding_hash(state) != expected:
            raise ValueError("authoritative approval state does not match its binding")
        return state


def approval_source_message_id(root: Path, estimate_id: str) -> str:
    """Return the provider ID whose durable claim owns the approval request."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        source_message_id = validate_provider_id(
            record.get("approval_source_message_id"), "approval_source_message_id"
        )
        source_hash = sha256_text(source_message_id)
        if not any(
            isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
            and item.get("binding_hash") == record.get("approval_binding_hash")
            for item in record.get("approval_requests", [])
        ):
            raise ValueError("approval source message lacks matching durable evidence")
        return source_message_id


def record_estimate_sent(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    approved: dict[str, Any],
    current_state: dict[str, Any],
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Move a durably approved estimate to estimate_sent after provider acceptance."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    valid, errors = approval_guard.verify_execution(approved, current_state)
    if not valid:
        raise ValueError("approval verification failed: " + "; ".join(errors))
    if current_state.get("estimate_id") != estimate_id:
        raise ValueError("current state estimate_id does not match")
    provider_message_id = validate_provider_id(
        provider_response.get("id"), "provider response id"
    )
    provider_thread_id = validate_provider_id(
        provider_response.get("threadId"), "provider response threadId"
    )
    binding_hash = approved.get("binding_hash")
    if not isinstance(binding_hash, str) or not HASH_RE.fullmatch(binding_hash):
        raise ValueError("approved binding_hash is invalid")
    approved_price = approved.get("owner_approved_price")

    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["route"] != current_state.get("route"):
            raise ValueError("current route does not match the record")
        if record.get("specification") != current_state.get("specification"):
            raise ValueError("current specification does not match the record")
        if provider_thread_id != record["route"]["thread_id"]:
            raise ValueError(
                "provider response threadId does not match the owned thread"
            )
        approval = next(
            (
                item
                for item in record.get("approval_requests", [])
                if isinstance(item, dict)
                and item.get("binding_hash") == binding_hash
                and item.get("source_message_id_sha256")
                == sha256_text(source_message_id)
            ),
            None,
        )
        if approval is None:
            raise ValueError(
                "estimate send lacks matching durable approval-request evidence"
            )
        if record.get("status") == "estimate_sent":
            existing = record.get("estimate_delivery")
            if isinstance(existing, dict) and all(
                existing.get(key) == value
                for key, value in {
                    "approval_binding_hash": binding_hash,
                    "approved_price": approved_price,
                    "provider_message_id": provider_message_id,
                    "thread_id": provider_thread_id,
                }.items()
            ):
                return record
            raise ValueError("conflicting estimate delivery evidence already exists")
        if record.get("status") != "pending_approval":
            raise ValueError("estimate delivery requires pending_approval status")
        record["estimate_delivery"] = {
            "status": "sent",
            "source_message_id_sha256": sha256_text(source_message_id),
            "approval_binding_hash": binding_hash,
            "approved_price": approved_price,
            "provider_message_id": provider_message_id,
            "thread_id": provider_thread_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        record["approved_price"] = approved_price
        record["outbound_provider_message_id"] = provider_message_id
        record["status"] = "estimate_sent"
        write_object(path, record)
        return record


def record_rendering_sent(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    reply_body: str,
    image_paths: list[Path],
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Append one same-thread rendering delivery for one customer request."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if not reply_body.strip():
        raise ValueError("rendering reply body must not be empty")
    if not image_paths or len(image_paths) > 2:
        raise ValueError("rendering delivery requires one or two images")
    image_bytes = [path.read_bytes() for path in image_paths]
    if any(not value for value in image_bytes):
        raise ValueError("rendering images must not be empty")
    provider_message_id = validate_provider_id(
        provider_response.get("id"), "provider response id"
    )
    provider_thread_id = validate_provider_id(
        provider_response.get("threadId"), "provider response threadId"
    )
    source_hash = sha256_text(source_message_id)
    evidence = {
        "status": "sent",
        "source_message_id_sha256": source_hash,
        "provider_message_id": provider_message_id,
        "thread_id": provider_thread_id,
        "body_sha256": sha256_text(reply_body),
        "image_sha256": [
            "sha256:" + hashlib.sha256(value).hexdigest() for value in image_bytes
        ],
    }

    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] not in {"estimate_sent", "appointment_booked", "approved"}:
            raise ValueError("rendering delivery requires a sent estimate")
        if provider_thread_id != record["route"]["thread_id"]:
            raise ValueError(
                "provider response threadId does not match the owned thread"
            )
        deliveries = record.setdefault("rendering_deliveries", [])
        if not isinstance(deliveries, list):
            raise ValueError("rendering_deliveries must be an array")
        for existing in deliveries:
            if not isinstance(existing, dict):
                raise ValueError("rendering_deliveries contains invalid evidence")
            if existing.get("source_message_id_sha256") != source_hash:
                continue
            comparable = dict(existing)
            comparable.pop("sent_at", None)
            comparable.pop("iteration", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting rendering delivery for source message")
        evidence["iteration"] = len(deliveries) + 1
        evidence["sent_at"] = datetime.now(timezone.utc).isoformat()
        deliveries.append(evidence)
        write_object(path, record)
        return record


def require_processed_evidence(
    root: Path,
    message_id: str,
    thread_id: str,
    claim_state: dict[str, Any],
) -> None:
    """Refuse completion when an estimate-thread message lacks its durable outcome."""
    matches: list[dict[str, Any]] = []
    if root.exists():
        with record_lock(root):
            for path in sorted(root.glob("jed-*.json")):
                record = read_object(path)
                route_ownership.validate_record(record)
                if record["route"]["thread_id"] == thread_id:
                    matches.append(record)
    if len(matches) > 1:
        raise ValueError("multiple estimate records match the Gmail thread")
    if not matches:
        return
    record = matches[0]
    source_hash = sha256_text(message_id)
    initiating = record["route"]["gmail_message_id"] == message_id
    reviews = record.get("thread_reviews", [])
    matching_review = next(
        (
            item
            for item in reviews
            if isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
        ),
        None,
    )

    rendering = next(
        (
            item
            for item in record.get("rendering_deliveries", [])
            if isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
            and item.get("status") == "sent"
            and item.get("thread_id") == thread_id
        ),
        None,
    )
    if rendering is not None:
        return

    if initiating and record["status"] == "awaiting_specs":
        if matching_review is not None:
            if matching_review.get("thread_id") != thread_id:
                raise ValueError("thread review is bound to the wrong Gmail thread")
            if matching_review.get("outcome") != "awaiting_specs":
                raise ValueError("thread review outcome does not match estimate status")
            if matching_review.get("specification_sha256") != canonical_sha256(
                record.get("specification")
            ):
                raise ValueError(
                    "record specification changed after full-thread review"
                )
        evidence = record.get("spec_gate_reply")
        if not isinstance(evidence, dict) or evidence.get("status") != "sent":
            raise ValueError(
                "awaiting_specs inquiry lacks durable spec-gate send evidence; "
                "refusing processed outcome"
            )
        if evidence.get("thread_id") != record["route"]["thread_id"]:
            raise ValueError(
                "spec-gate send evidence is bound to the wrong Gmail thread"
            )
        if (
            not isinstance(evidence.get("provider_message_id"), str)
            or not evidence["provider_message_id"]
        ):
            raise ValueError("spec-gate send evidence lacks a provider message ID")
        return

    if matching_review is None:
        raise ValueError("estimate-thread message lacks a durable full-thread review")
    if matching_review.get("thread_id") != thread_id:
        raise ValueError("thread review is bound to the wrong Gmail thread")
    if matching_review.get("specification_sha256") != canonical_sha256(
        record.get("specification")
    ):
        raise ValueError("record specification changed after full-thread review")
    outcome = matching_review.get("outcome")
    if outcome == "awaiting_specs":
        if record["status"] != "awaiting_specs":
            raise ValueError("thread review outcome does not match estimate status")
        followup = next(
            (
                item
                for item in record.get("followup_replies", [])
                if isinstance(item, dict)
                and item.get("source_message_id_sha256") == source_hash
                and item.get("status") == "sent"
                and item.get("thread_id") == thread_id
            ),
            None,
        )
        if followup is None:
            raise ValueError(
                "incomplete customer reply lacks durable follow-up send evidence"
            )
        return
    if outcome == "specs_complete":
        if record["status"] != "pending_approval":
            raise ValueError("complete thread review is not pending approval")
        approval = next(
            (
                item
                for item in record.get("approval_requests", [])
                if isinstance(item, dict)
                and item.get("source_message_id_sha256") == source_hash
            ),
            None,
        )
        if approval is None:
            raise ValueError("complete customer reply lacks durable approval evidence")
        action_key = f"approval_request:{record['estimate_id']}:{message_id}"
        action = claim_state.get("external_actions", {}).get(action_key)
        if (
            not isinstance(action, dict)
            or action.get("category") != "approval_request"
            or action.get("status") != "sent"
        ):
            raise ValueError(
                "complete customer reply lacks a sent claimed approval request"
            )
        return
    raise ValueError("thread review has an invalid completion outcome")


def require_initial_reply_evidence(root: Path, message_id: str) -> None:
    """Backward-compatible initial-inquiry evidence check."""
    matches = lookup_by_initiating_message(root, message_id)
    if not matches or matches[0]["status"] != "awaiting_specs":
        return
    require_processed_evidence(
        root,
        message_id,
        matches[0]["route"]["thread_id"],
        {},
    )


def lookup_by_initiating_message(root: Path, message_id: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if root.exists():
        with record_lock(root):
            for path in sorted(root.glob("jed-*.json")):
                record = read_object(path)
                route_ownership.validate_record(record)
                if record["route"]["gmail_message_id"] == message_id:
                    matches.append(record)
    if len(matches) > 1:
        raise ValueError("multiple estimate records match the initiating Gmail message")
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-inquiry")
    create.add_argument("route", type=Path)
    create.add_argument("--inbound-timestamp-ms", type=int, required=True)
    create.add_argument("--record-root", type=Path, default=default_record_root())
    create.add_argument("--output", type=Path, required=True)
    upsert = sub.add_parser("upsert")
    upsert.add_argument("record", type=Path)
    upsert.add_argument("--record-root", type=Path, default=default_record_root())
    lookup = sub.add_parser("lookup-thread")
    lookup.add_argument("route", type=Path)
    lookup.add_argument("--record-root", type=Path, default=default_record_root())
    lookup.add_argument("--output", type=Path, required=True)
    spec_gate = sub.add_parser("record-spec-gate-sent")
    spec_gate.add_argument("--estimate-id", required=True)
    spec_gate.add_argument("--reply-body", type=Path, required=True)
    spec_gate.add_argument("--provider-response", type=Path, required=True)
    spec_gate.add_argument("--record-root", type=Path, default=default_record_root())
    spec_gate.add_argument("--output", type=Path)
    followup = sub.add_parser("record-followup-sent")
    followup.add_argument("--estimate-id", required=True)
    followup.add_argument("--source-message-id", required=True)
    followup.add_argument("--reply-body", type=Path, required=True)
    followup.add_argument("--provider-response", type=Path, required=True)
    followup.add_argument("--record-root", type=Path, default=default_record_root())
    followup.add_argument("--output", type=Path)
    thread_review = sub.add_parser("record-thread-review")
    thread_review.add_argument("--estimate-id", required=True)
    thread_review.add_argument("--snapshot", type=Path, required=True)
    thread_review.add_argument(
        "--record-root", type=Path, default=default_record_root()
    )
    thread_review.add_argument("--output", type=Path)
    approval = sub.add_parser("record-approval-requested")
    approval.add_argument("--estimate-id", required=True)
    approval.add_argument("--source-message-id", required=True)
    approval.add_argument("--approval-request", type=Path, required=True)
    approval.add_argument("--record-root", type=Path, default=default_record_root())
    approval.add_argument("--output", type=Path)
    sent = sub.add_parser("record-estimate-sent")
    sent.add_argument("--estimate-id", required=True)
    sent.add_argument("--source-message-id", required=True)
    sent.add_argument("--approved", type=Path, required=True)
    sent.add_argument("--current-state", type=Path, required=True)
    sent.add_argument("--provider-response", type=Path, required=True)
    sent.add_argument("--record-root", type=Path, default=default_record_root())
    sent.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create-inquiry":
            record = create_initial_record(
                args.record_root,
                read_object(args.route),
                args.inbound_timestamp_ms,
            )
            write_object(args.output, record)
        elif args.command == "upsert":
            record = persist_record(args.record_root, read_object(args.record))
        elif args.command == "lookup-thread":
            records = lookup_thread(args.record_root, read_object(args.route))
            write_object(args.output, records)
            record = {"candidates": len(records)}
        elif args.command == "record-spec-gate-sent":
            record = record_spec_gate_sent(
                args.record_root,
                args.estimate_id,
                args.reply_body.read_text(encoding="utf-8"),
                read_object(args.provider_response),
            )
            if args.output is not None:
                write_object(args.output, record)
        elif args.command == "record-followup-sent":
            record = record_followup_sent(
                args.record_root,
                args.estimate_id,
                args.source_message_id,
                args.reply_body.read_text(encoding="utf-8"),
                read_object(args.provider_response),
            )
            if args.output is not None:
                write_object(args.output, record)
        elif args.command == "record-thread-review":
            record = record_thread_review(
                args.record_root, args.estimate_id, read_object(args.snapshot)
            )
            if args.output is not None:
                write_object(args.output, record)
        elif args.command == "record-approval-requested":
            record = record_approval_requested(
                args.record_root,
                args.estimate_id,
                args.source_message_id,
                read_object(args.approval_request),
            )
            if args.output is not None:
                write_object(args.output, record)
        else:
            record = record_estimate_sent(
                args.record_root,
                args.estimate_id,
                args.source_message_id,
                read_object(args.approved),
                read_object(args.current_state),
                read_object(args.provider_response),
            )
            if args.output is not None:
                write_object(args.output, record)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
