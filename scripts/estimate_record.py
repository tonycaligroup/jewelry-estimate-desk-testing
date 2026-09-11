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
import pricing_model


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
POST_ESTIMATE_ASSESSMENTS = {"unchanged", "changed", "uncertain"}
POST_ESTIMATE_INTENTS = {
    "estimate_acceptance",
    "rendering_request",
    "appointment_request",
    "cancellation",
}


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
                history = record.get("route_history") or []
                moved = history and isinstance(history[-1], dict) and history[-1].get("route") == existing["route"]
                if not moved:
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
                "appointment_approval_requests",
                "rendering_deliveries",
            ):
                record = preserve_append_only(existing, record, field)
            existing_delivery = existing.get("estimate_delivery")
            proposed_delivery = record.get("estimate_delivery")
            revised = int(record.get("revision") or 0) > int(existing.get("revision") or 0)
            if revised:
                # A reopened estimate (WORKFLOW.md 6.8): the sent estimate and its
                # binding moved into estimate_history; check they are all there.
                history = record.get("estimate_history") or []
                archived = history[-1] if history and isinstance(history[-1], dict) else {}
                if existing_delivery is not None and archived.get("estimate_delivery") != existing_delivery:
                    raise ValueError("a revision must archive the sent estimate unchanged")
                if existing.get("approval_binding_hash") and archived.get("approval_binding_hash") != existing.get("approval_binding_hash"):
                    raise ValueError("a revision must archive the approval binding unchanged")
                existing_delivery = None
            if existing_delivery is not None:
                if (
                    proposed_delivery is not None
                    and proposed_delivery != existing_delivery
                ):
                    raise ValueError("estimate delivery evidence is immutable")
                if proposed_delivery is None:
                    record = dict(record)
                    record["estimate_delivery"] = existing_delivery
            existing_binding = None if revised else existing.get("approval_binding_hash")
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
                    raise ValueError(
                        "approval-bound estimate state is invalid"
                    ) from exc
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
    identity_key = route.get("identity_key")
    if not root.exists():
        return []
    candidates: list[dict[str, Any]] = []
    with record_lock(root):
        for path in sorted(root.glob("jed-*.json")):
            record = read_object(path)
            record_route = record.get("route")
            if not isinstance(record_route, dict):
                continue
            if record_route.get("thread_id") == thread_id:
                candidates.append(record)
            elif (
                # Also surface in-flight work for the same customer on another
                # thread, so route_ownership can refuse to fork the estimate.
                isinstance(identity_key, str)
                and identity_key
                and record_route.get("identity_key") == identity_key
                and record.get("status") in route_ownership.ACTIVE_STATUSES
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


def last_ask(record: dict[str, Any]) -> tuple[set[str], str]:
    """The fields the desk last asked the customer for, and when that email went (ISO), or (set(), "")."""
    revision = int(record.get("revision") or 0)
    reviews = [r for r in record.get("thread_reviews", []) if isinstance(r, dict) and r.get("outcome") == "awaiting_specs"
               and isinstance(r.get("missing_required_fields"), list) and int(r.get("revision") or 0) >= revision]
    sent = [record.get("spec_gate_reply")] + [item for item in record.get("followup_replies") or [] if isinstance(item, dict)]
    stamps = [str(item.get("sent_at") or "") for item in sent if isinstance(item, dict) and item.get("status") == "sent"]
    if not reviews or not stamps:
        return set(), ""
    return {str(f) for f in reviews[-1]["missing_required_fields"]}, max(stamps)


def predates_last_ask(record: dict[str, Any], message_date: str | int | None) -> bool:
    """True when the message was sent before the desk's last question went out (two emails before the first tick).

    `message_date` is Gmail's internalDate in milliseconds when known, else the Date header.
    """
    _fields, sent_at = last_ask(record)
    if not sent_at or not message_date:
        return False
    try:
        from email.utils import parsedate_to_datetime

        if isinstance(message_date, (int, float)) or str(message_date).isdigit():
            when = datetime.fromtimestamp(int(message_date) / 1000.0, tz=timezone.utc)
        elif str(message_date)[:4].isdigit():
            when = datetime.fromisoformat(str(message_date).replace("Z", "+00:00"))
        else:
            when = parsedate_to_datetime(str(message_date))
        sent = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return when < sent


def followup_stalled(record: dict[str, Any], source_message_id: str, missing: list[str], predates_ask: bool = False) -> list[str]:
    """The fields an earlier follow-up already asked for that this reply still leaves open.

    Empty when this is the first ask or the customer gave something new. The
    desk never sends the same question twice; the owner decides instead.
    """
    if not missing or record.get("status") != "awaiting_specs":
        return []
    if predates_ask:
        return []  # written before the question went out: not a non-answer
    source_hash = sha256_text(source_message_id)
    revision = int(record.get("revision") or 0)
    earlier_reviews = [
        review for review in record.get("thread_reviews", [])
        if isinstance(review, dict) and review.get("source_message_id_sha256") != source_hash
        and review.get("outcome") == "awaiting_specs" and isinstance(review.get("missing_required_fields"), list)
        and int(review.get("revision") or 0) >= revision  # a reopened estimate starts its asks afresh
    ]
    if not earlier_reviews or not (record.get("spec_gate_reply") or record.get("followup_replies")):
        return []
    last_missing = {str(field) for field in earlier_reviews[-1]["missing_required_fields"]}
    sent = (1 if record.get("spec_gate_reply") else 0) + len([
        item for item in record.get("followup_replies") or [] if isinstance(item, dict) and item.get("status") == "sent"
    ])
    still_open = sorted(set(missing) & last_missing)
    if set(missing) == last_missing:
        # The reply answered none of it: the same question is not sent again.
        return still_open
    if sent >= 2:
        # Two asks already; a third email is a nag. The owner decides.
        return still_open or sorted(missing)
    # Progress: the customer answered some of it. One more ask for the rest.
    return []


def mark_jewelers_choice(root: Path, estimate_id: str, source_message_id: str, fields: list[str]) -> dict[str, Any]:
    """The owner chose to price without these details: they become the jeweler's call.

    The review that decided to ask the customer for them is rewritten to say
    the specification is complete, so the held-back follow-up is withdrawn
    and pricing proceeds from the same message.
    """
    path = record_path(root, estimate_id)
    source_hash = sha256_text(source_message_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") != "awaiting_specs":
            raise ValueError("only an estimate still awaiting specifications can skip details")
        specification = dict(record.get("specification") or {})
        for field in fields:
            if str(field).startswith("confirm."):
                continue  # a reading check is confirmed by the customer, never chosen by the jeweler
            index, bare = split_field_name(str(field))
            if index is None:
                specification[bare] = "jeweler's choice"
            else:
                pieces = [dict(p) if isinstance(p, dict) else {} for p in specification.get("pieces") or []]
                if 0 <= index < len(pieces):
                    pieces[index][bare] = "jeweler's choice"
                    specification["pieces"] = pieces
        record["specification"] = specification
        record["missing_required_fields"] = sorted(
            f for f in (record.get("missing_required_fields") or []) if f not in set(fields)
        )
        for review in record.get("thread_reviews", []):
            if isinstance(review, dict) and review.get("source_message_id_sha256") == source_hash \
                    and review.get("outcome") == "awaiting_specs":
                review["missing_required_fields"] = [f for f in review.get("missing_required_fields", []) if f not in set(fields)]
                if not review["missing_required_fields"]:
                    review["outcome"] = "specs_complete"
                review["specification_sha256"] = canonical_sha256(specification)
                review["owner_skipped_fields"] = sorted(str(f) for f in fields)
        write_object(path, record)
        return record


def followup_sent(record: dict[str, Any], source_message_id: str) -> bool:
    """True when the specification question for this message reached the customer."""
    if record["route"]["gmail_message_id"] == source_message_id:
        evidence = record.get("spec_gate_reply")
        return isinstance(evidence, dict) and evidence.get("status") == "sent"
    source_hash = sha256_text(source_message_id)
    return any(
        isinstance(item, dict)
        and item.get("source_message_id_sha256") == source_hash
        and item.get("status") == "sent"
        for item in record.get("followup_replies", [])
    )


def pending_followup(record: dict[str, Any], source_message_id: str) -> dict[str, Any] | None:
    """The review that said "ask the customer" when nothing was sent yet.

    A worker can die between recording a thread review and sending the
    follow-up. The review alone is not a finished job: until the send is
    recorded the customer has not been asked, and a resumed worker must send
    rather than review again.
    """
    if record.get("status") != "awaiting_specs":
        return None
    source_hash = sha256_text(source_message_id)
    # The latest review of this message: a reopened estimate reads the same
    # message again, and the earlier (post-estimate) review is history.
    review = next(
        (
            item
            for item in reversed(record.get("thread_reviews", []))
            if isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
        ),
        None,
    )
    if review is None or review.get("outcome") != "awaiting_specs":
        return None
    if followup_sent(record, source_message_id):
        return None
    return {
        "action": "send_spec_followup",
        "missing_required_fields": list(review.get("missing_required_fields") or []),
        "initiating": record["route"]["gmail_message_id"] == source_message_id,
        "review_recorded_at": review.get("recorded_at"),
    }


def record_thread_review(
    root: Path,
    estimate_id: str,
    snapshot: dict[str, Any],
    shop_profile: dict[str, Any] | None = None,
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
        if record["route"]["gmail_message_id"] not in validated_ids and not record.get("route_history"):
            # After the owner's "same" moved the estimate to a new thread, the
            # message that opened it lives in the old one (route_history).
            raise ValueError("thread review must include the initiating Gmail message")
        post_estimate = record["status"] in {
            "estimate_sent",
            "appointment_booked",
            "approved",
        }
        if post_estimate:
            specification = record.get("specification")
            if not isinstance(specification, dict) or not specification:
                raise ValueError("sent estimate is missing its approved specification")
            classification_error_codes = post_estimate_artifact_error_codes(
                snapshot.get("post_estimate_artifact")
            )
            assessment, intents, changed_fields, malformed = (
                classify_post_estimate_artifact(snapshot.get("post_estimate_artifact"))
            )
            if missing:
                malformed = True
                classification_error_codes.append("unexpected_missing_fields")
            if malformed:
                outcome = "classification_malformed"
                assessment = "uncertain"
                intents = []
                changed_fields = []
            elif assessment == "changed":
                outcome = "design_change_detected"
            elif assessment == "uncertain":
                outcome = "classification_uncertain"
            else:
                outcome = "post_estimate_continuation"
        else:
            specification = snapshot.get("specification")
            if not isinstance(specification, dict) or not specification:
                raise ValueError("specification must be a non-empty object")
            if not is_multi_piece(specification):
                missing = enforce_specification_policies(
                    specification, missing, shop_profile
                )
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
        if post_estimate:
            evidence.update(
                {
                    "approved_specification_sha256": canonical_sha256(specification),
                    "design_change_assessment": assessment,
                    "intents": intents,
                    "changed_fields": changed_fields,
                }
            )
            if malformed:
                evidence["classification_error_codes"] = sorted(
                    set(classification_error_codes)
                )
        reviews = record.setdefault("thread_reviews", [])
        if not isinstance(reviews, list):
            raise ValueError("thread_reviews must be an array")
        revision = int(record.get("revision") or 0)
        if revision:
            evidence["revision"] = revision
        for existing in reviews:
            if not isinstance(existing, dict):
                raise ValueError("thread_reviews contains invalid evidence")
            if (
                existing.get("source_message_id_sha256")
                != evidence["source_message_id_sha256"]
            ):
                continue
            if int(existing.get("revision") or 0) < revision:
                # A review from before the estimate was reopened (WORKFLOW.md
                # 6.8) is history; the same message is read again now.
                continue
            comparable = dict(existing)
            comparable.pop("recorded_at", None)
            if comparable == evidence:
                return record
            if (
                not post_estimate
                and existing.get("outcome") == "awaiting_specs"
                and not followup_sent(record, source_message_id)
            ):
                # The earlier review already decided to ask the customer and
                # nothing was sent yet. That review stands; a resumed worker
                # must send its follow-up, not replace it with a new opinion.
                return record
            legacy_evidence = dict(evidence)
            legacy_evidence.pop("classification_error_codes", None)
            if (
                evidence.get("outcome") == "classification_malformed"
                and "classification_error_codes" not in comparable
                and comparable == legacy_evidence
            ):
                return record
            if (
                not post_estimate
                and record["status"] == "awaiting_specs"
                and (
                    (existing.get("outcome") == "specs_complete" and not followup_sent(record, source_message_id))
                    # The owner's numbers from the cost sheet review the same email again; the earlier review
                    # (a question sent, nothing priced) is replaced (9 and 10 September 2026).
                    or bool((record.get("concierge") or {}).get("details"))
                    or bool((record.get("sheet_draft") or {}).get("acted_hash"))
                )
            ):
                # Nothing was sent or priced on the earlier reading; a fresh
                # reading of the same email (after an owner answer, or a
                # resumed run) replaces it instead of stranding the claim.
                existing.clear()
                existing.update(evidence)
                existing["recorded_at"] = datetime.now(timezone.utc).isoformat()
                existing["superseded_earlier_review"] = True
                record["specification"] = specification
                record["missing_required_fields"] = sorted(missing)
                write_object(path, record)
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


STONE_WORDS_IN_TEXT = (
    "diamond", "sapphire", "ruby", "emerald", "moissanite", "aquamarine", "morganite", "tanzanite", "amethyst",
    "topaz", "garnet", "opal", "pearl", "tourmaline", "spinel", "peridot", "citrine", "pave", "pavé", "melee",
    "tennis", "eternity", "halo", "gemstone", "stones",
)

FANCY_DIAMOND_COLORS = (
    "salt-and-pepper", "champagne", "cognac", "yellow", "pink", "blue",
    "black", "brown", "green", "orange", "red", "grey",
)


def _fancy_diamond_color(stone_type: Any, grade: Any, evidence: str) -> str | None:
    """Return a named fancy color only when it belongs to a diamond."""
    kind = re.sub(r"[^a-z]+", " ", str(stone_type or "").lower()).strip()
    if "diamond" not in kind:
        return None
    grade_words = re.sub(r"[^a-z]+", "-", str(grade or "").lower()).strip("-")
    words = re.sub(r"[^a-z]+", " ", str(evidence or "").lower()).strip()
    for color in FANCY_DIAMOND_COLORS:
        aliases = {color, color.replace("-and-", " and "), color.replace("-", " ")}
        normalized = color.replace("-", " ")
        if normalized in kind or grade_words in aliases:
            return color
        for alias in aliases:
            phrase = re.sub(r"[^a-z]+", " ", alias).strip()
            if re.search(rf"\b{re.escape(phrase)}\b(?:\s+\w+){{0,2}}\s+diamonds?\b", words):
                return color
            if re.search(rf"\bdiamonds?\b(?:\s+\w+){{0,2}}\s+\b{re.escape(phrase)}\b", words):
                return color
    return None


def settle_fancy_diamonds(specification: Any, customer_words: str = "", photo_words: str = "") -> Any:
    """Make a fancy diamond color part of the stone name, never its grade.

    The operation is deterministic and recursive for multi-piece requests. A
    grade letter and ``colorless`` are deliberately not fancy colors.
    """
    if not isinstance(specification, dict):
        return specification
    settled = dict(specification)
    evidence = " ".join((customer_words, photo_words, str(settled.get("reference_images") or "")))

    def settle_stone(type_key: str, color_key: str, description_key: str | None = None) -> None:
        description = str(settled.get(description_key) or "") if description_key else ""
        stone_type = settled.get(type_key)
        if not stone_type and "diamond" in description.lower():
            stone_type = "diamond"
        color = _fancy_diamond_color(stone_type, settled.get(color_key), " ".join((evidence, description)))
        if not color:
            return
        name = f"{color} diamond"
        settled[type_key] = name
        settled.pop(color_key, None)
        if description_key:
            if description:
                settled[description_key] = re.sub(r"(?<![a-z])diamonds?(?![a-z])", name, description,
                                                   count=1, flags=re.IGNORECASE)
            else:
                settled[description_key] = f"{name} accents"

    settle_stone("stone_type", "stone_color")
    settle_stone("accent_stone_type", "accent_stone_color", "accent_stones")
    if isinstance(settled.get("pieces"), list):
        settled["pieces"] = [settle_fancy_diamonds(piece, customer_words, photo_words)
                             if isinstance(piece, dict) else piece for piece in settled["pieces"]]
    return settled


def stones_in_words(specification: Any) -> bool:
    """Stones named anywhere the customer's words were kept, not only in stone_type.

    A tennis bracelet, a pave signet, or "small white diamonds" in the notes
    are stones the shop must price and, when the profile says ask-always,
    ask the origin of.
    """
    if not isinstance(specification, dict):
        return False
    text = " ".join(
        str(specification.get(key) or "").lower()
        for key in ("piece_type", "accent_stones", "setting_style", "notes", "stone_color", "stone_shape")
    )
    if any(word in text for word in ("no stones", "without stones", "no diamonds", "no gems", "plain band")):
        return False
    return any(word in text for word in STONE_WORDS_IN_TEXT)


# Facts stated once for the whole order apply to every piece unless a piece says otherwise.
SHARED_KEYS = (
    "metal", "metal_karat", "metal_color", "finish", "budget", "event_date", "scheduling_intent",
    "customer_supplied_materials", "reference_images", "certificate", "quantity",
)
PIECE_PREFIX = "pieces."


NEVER_SHARED = ("pieces", "notes", "piece_type", "engraving")


def pieces_of(specification: Any) -> list[dict[str, Any]]:
    """The pieces in a specification: [spec] for one piece, one merged dict per entry otherwise.

    MULTI-PIECE-PLAN.md 2. A specification without `pieces` (or with fewer
    than two) is one piece and is returned untouched, so every one-piece
    path sees exactly what it saw before. Otherwise each piece is the
    shared top-level facts overlaid by the piece's own.
    """
    if not isinstance(specification, dict):
        return [{}]
    raw = specification.get("pieces")
    if not isinstance(raw, list) or len([p for p in raw if isinstance(p, dict)]) < 2:
        return [{k: v for k, v in specification.items() if k != "pieces"}]
    # A fact written at the top level of a multi-piece specification is
    # shared by definition (the model put it there for both): metal, and
    # the stone facts too ("diamond, lab-grown, D" at the top with two
    # pieces below, 7 September 2026). Each piece's own facts win.
    # What a piece *is* is never shared: a top-level piece_type beside a
    # pieces list is the model's slip, and handing it to a piece without one
    # rendered an engagement ring as a second men's band (live, 8 September
    # 2026). An engraving is the one piece's too.
    shared = {k: v for k, v in specification.items()
              if k not in NEVER_SHARED and not isinstance(v, (list, dict)) and v not in (None, "", [])}
    merged = []
    for piece in raw:
        if isinstance(piece, dict):
            merged.append({**shared, **{k: v for k, v in piece.items() if v not in (None, "", [])}})
    return merged


def is_multi_piece(specification: Any) -> bool:
    return isinstance(specification, dict) and len(pieces_of(specification)) > 1


def piece_label(specification: Any, index: int) -> str:
    """'engagement ring', 'wedding band', or 'piece 2'; two pieces of one kind are told apart by what differs.

    Live, 7 September 2026: a yellow band and a rose band were both labelled
    "(men's wedding band)" on the card, so the assumption lines could not be
    told apart. The first differing fact joins the label ("men's wedding
    band, rose gold"); a still-identical pair is numbered.
    """
    pieces = pieces_of(specification)
    if not (0 <= index < len(pieces)):
        return f"piece {index + 1}"
    kind = str(pieces[index].get("piece_type") or "").strip().lower()
    if not kind:
        return f"piece {index + 1}"
    twins = [i for i, piece in enumerate(pieces) if str(piece.get("piece_type") or "").strip().lower() == kind]
    if len(twins) == 1:
        return kind
    for key in ("metal_color", "metal", "finger_size", "stone_type", "stone_carat", "setting_style"):
        values = [str(pieces[i].get(key) or "").strip().lower() for i in twins]
        mine = values[twins.index(index)]
        if mine and values.count(mine) == 1:
            words = mine if key != "finger_size" else f"size {mine}"
            if key in ("metal_color",) and "gold" not in mine and str(pieces[index].get("metal") or "").lower().find("gold") >= 0:
                words = f"{mine} gold"
            return f"{kind}, {words}"
    return f"{kind} {twins.index(index) + 1}"


def is_set(specification: Any) -> bool:
    """The customer asked for matching pieces: one design language across them."""
    if not isinstance(specification, dict):
        return False
    text = " ".join(str(specification.get(k) or "").lower() for k in ("notes", "setting_style", "piece_type"))
    for piece in specification.get("pieces") or []:
        if isinstance(piece, dict):
            text += " " + " ".join(str(piece.get(k) or "").lower() for k in ("notes", "setting_style", "piece_type"))
    return any(word in text for word in ("matching", "a set", "bridal set", "to match", "same design", "coordinating"))


def split_field_name(name: str) -> tuple[int | None, str]:
    """'pieces.1.finger_size' -> (1, 'finger_size'); 'finger_size' -> (None, 'finger_size')."""
    if isinstance(name, str) and name.startswith(PIECE_PREFIX):
        rest = name[len(PIECE_PREFIX):]
        index, _, field = rest.partition(".")
        if index.isdigit() and field:
            return int(index), field
    return None, str(name)


SUPPLIED_STONE_WORDS = (
    "my stone", "my diamond", "my own", "our own", "her stone", "her diamond", "his stone", "his diamond",
    "mother's", "mothers", "father's", "fathers", "grandmother's", "grandmothers", "grandma's", "grandpa's",
    "family stone", "family diamond", "heirloom", "existing stone", "existing diamond", "the stone i have",
    "i have a diamond", "i have a stone", "i have the diamond", "i have the stone", "i already have",
    "reset my", "reset her", "reset his", "reset the stone", "reset the diamond", "re-set", "remount", "re-mount", "reuse", "re-use",
    "customer supplied", "customer-supplied", "supplied by the customer", "their own stone", "own stone",
)


def customer_supplies_stone(specification: Any) -> bool:
    """The stone is the customer's own: nothing to grade, nothing to buy.

    A mother's diamond going into a new bezel needs its shape and size so the
    setting fits; its color and clarity change nothing the shop does or
    charges, and asking for them reads as nonsense to the customer.
    """
    if not isinstance(specification, dict):
        return False
    supplied = specification.get("customer_supplied_materials")
    supplied_text = " ".join(str(s) for s in supplied).lower() if isinstance(supplied, list) else str(supplied or "").lower()
    if supplied_text and supplied_text not in {"none", "no", "n/a", "false"}:
        if any(word in supplied_text for word in STONE_WORDS_IN_TEXT + ("stone", "gem")):
            return True
    text = " ".join(
        str(specification.get(key) or "").lower()
        for key in ("stone_type", "notes", "setting_style", "piece_type", "accent_stones")
    )
    return any(word in text for word in SUPPLIED_STONE_WORDS) and any(
        word in text for word in STONE_WORDS_IN_TEXT + ("stone", "gem")
    )


def enforce_specification_policies(
    specification: dict[str, Any],
    missing: list[str],
    shop_profile: dict[str, Any] | None,
) -> list[str]:
    """Apply profile fields that the model may not treat as delegatable."""
    result = set(missing)
    placeholder_values = {
        "",
        "n/a",
        "not applicable",
        "not specified",
        "tbd",
        "to be determined",
        "unknown",
        "unspecified",
    }
    no_stone_values = {
        "",
        "0",
        "false",
        "n/a",
        "no",
        "no-stones",
        "none",
        "not-applicable",
    }

    def indicates_stones(value: Any) -> bool:
        if isinstance(value, str):
            normalized_value = (
                value.strip().lower().replace("_", "-").replace(" ", "-")
            )
            return normalized_value not in no_stone_values
        if isinstance(value, bool):
            return value
        if isinstance(value, (list, dict)):
            return bool(value)
        if isinstance(value, (int, float)):
            return value > 0
        return False

    stone_type = specification.get("stone_type")
    stones = specification.get("stones")
    has_stones = (
        indicates_stones(stone_type)
        or indicates_stones(stones)
        or (
            isinstance(specification.get("stone_count"), (int, float))
            and not isinstance(specification.get("stone_count"), bool)
            and specification["stone_count"] > 0
        )
        or stones_in_words(specification)
    )
    style_values = [
        specification.get(key)
        for key in ("setting_style", "setting", "style", "design_style")
    ]
    has_setting_style = any(
        isinstance(value, str)
        and value.strip().lower().replace("_", " ") not in placeholder_values
        for value in style_values
    ) or any(isinstance(value, dict) and bool(value) for value in style_values)
    if not has_setting_style and has_stones:
        result.add("setting_style")
    if shop_profile is None:
        return sorted(result)
    defaults = shop_profile.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError("shop profile defaults must be an object")
    if defaults.get("stone_origin") == "ask_always":
        stone_origin = specification.get("stone_origin")
        normalized = (
            stone_origin.strip().lower().replace("_", "-").replace(" ", "-")
            if isinstance(stone_origin, str)
            else ""
        )
        explicit_origins = {
            "natural",
            "lab",
            "lab-grown",
            "lab-created",
            "laboratory-grown",
            "laboratory-created",
        }
        if has_stones and normalized not in explicit_origins and not customer_supplies_stone(specification):
            result.add("stone_origin")
    return sorted(result)


def post_estimate_artifact_error_codes(value: Any) -> list[str]:
    """Return privacy-safe structural reasons for a malformed classification."""
    if not isinstance(value, dict):
        return ["not_object"]
    expected_keys = {
        "design_change_assessment",
        "intents",
        "changed_fields",
    }
    errors: list[str] = []
    if set(value) != expected_keys:
        errors.append("unexpected_keys")
    assessment = value.get("design_change_assessment")
    intents = value.get("intents")
    changed_fields = value.get("changed_fields")
    if assessment not in POST_ESTIMATE_ASSESSMENTS:
        errors.append("invalid_assessment")
    if not isinstance(intents, list):
        errors.append("intents_not_array")
    else:
        if any(not isinstance(intent, str) for intent in intents):
            errors.append("intent_not_string")
        string_intents = [intent for intent in intents if isinstance(intent, str)]
        if len(set(string_intents)) != len(string_intents):
            errors.append("duplicate_intents")
        if any(intent not in POST_ESTIMATE_INTENTS for intent in string_intents):
            errors.append("unsupported_intent")
    if not isinstance(changed_fields, list):
        errors.append("changed_fields_not_array")
    else:
        if any(not isinstance(field, str) for field in changed_fields):
            errors.append("changed_field_not_string")
        string_fields = [field for field in changed_fields if isinstance(field, str)]
        if len(set(string_fields)) != len(string_fields):
            errors.append("duplicate_changed_fields")
        if any(
            not field
            or len(field) > 80
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in field
            )
            for field in string_fields
        ):
            errors.append("invalid_changed_field")
        if assessment == "changed" and not changed_fields:
            errors.append("changed_without_fields")
        if assessment in {"unchanged", "uncertain"} and changed_fields:
            errors.append("fields_without_changed")
    return errors


def classify_post_estimate_artifact(
    value: Any,
) -> tuple[str, list[str], list[str], bool]:
    """Return a normalized fail-closed post-estimate intent classification."""
    if post_estimate_artifact_error_codes(value):
        return "uncertain", [], [], True
    assert isinstance(value, dict)
    assessment = value.get("design_change_assessment")
    intents = value.get("intents")
    changed_fields = value.get("changed_fields")
    assert isinstance(assessment, str)
    assert isinstance(intents, list)
    assert isinstance(changed_fields, list)
    return assessment, sorted(intents), sorted(changed_fields), False


def post_estimate_decision(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    required_intent: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the claim-bound post-estimate decision for one inbound message."""
    source_hash = sha256_text(
        validate_provider_id(source_message_id, "source_message_id")
    )
    record = read_object(record_path(root, estimate_id))
    route_ownership.validate_record(record)
    reviews = record.get("thread_reviews")
    if not isinstance(reviews, list) or not reviews:
        raise ValueError("post-estimate decision is missing")
    decision = reviews[-1]
    if not isinstance(decision, dict):
        raise ValueError("post-estimate decision is invalid")
    if decision.get("source_message_id_sha256") != source_hash:
        raise ValueError("post-estimate decision does not match the claimed message")
    if decision.get("thread_id") != record["route"]["thread_id"]:
        raise ValueError("post-estimate decision does not match the owned thread")
    specification = record.get("specification")
    if not isinstance(specification, dict) or not specification:
        raise ValueError("sent estimate is missing its approved specification")
    approved_hash = canonical_sha256(specification)
    if decision.get("approved_specification_sha256") != approved_hash:
        raise ValueError("post-estimate decision does not match the approved specification")
    outcome = decision.get("outcome")
    allowed_outcomes = {
        "post_estimate_continuation",
        "design_change_detected",
        "classification_uncertain",
        "classification_malformed",
    }
    if outcome not in allowed_outcomes:
        raise ValueError("post-estimate decision has an invalid outcome")
    intents = decision.get("intents")
    if (
        not isinstance(intents, list)
        or any(not isinstance(intent, str) for intent in intents)
        or len(set(intents)) != len(intents)
        or any(intent not in POST_ESTIMATE_INTENTS for intent in intents)
    ):
        raise ValueError("post-estimate decision has invalid intents")
    if required_intent is not None and (
        outcome != "post_estimate_continuation" or required_intent not in intents
    ):
        raise ValueError(
            f"post-estimate decision does not authorize {required_intent}"
        )
    return record, decision


def _require_aware_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError(f"{field} must be a short ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def record_appointment_cancelled(root: Path, estimate_id: str, source_message_id: str, note: str = "") -> dict[str, Any]:
    """The customer cancelled the booked meeting: the booking moves to history and the status steps back to estimate_sent."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        booked = record.get("appointment_booked")
        if not isinstance(booked, dict):
            return record
        cancelled = record.setdefault("appointments_cancelled", [])
        if isinstance(cancelled, list):
            cancelled.append({**booked, "cancelled_by_sha256": sha256_text(source_message_id), "note": str(note or "")[:200],
                              "cancelled_at": datetime.now(timezone.utc).isoformat()})
        record.pop("appointment_booked", None)
        if record.get("status") == "appointment_booked":
            record["status"] = "estimate_sent"
        write_object(path, record)
        return record


def record_appointment_booked(
    root: Path,
    estimate_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    """Persist one immutable booking receipt after provider actions succeed."""
    required = {
        "estimate_id",
        "source_message_id",
        "calendar_event_id",
        "confirmed_start",
        "confirmed_end",
        "confirmation_message_id",
        "confirmation_thread_id",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("appointment booking receipt has missing or unsupported fields")
    if receipt.get("estimate_id") != estimate_id:
        raise ValueError("appointment booking receipt estimate_id does not match")
    source_message_id = validate_provider_id(
        receipt.get("source_message_id"), "source_message_id"
    )
    calendar_event_id = validate_provider_id(
        receipt.get("calendar_event_id"), "calendar_event_id"
    )
    confirmation_message_id = validate_provider_id(
        receipt.get("confirmation_message_id"), "confirmation_message_id"
    )
    confirmation_thread_id = validate_provider_id(
        receipt.get("confirmation_thread_id"), "confirmation_thread_id"
    )
    start = _require_aware_timestamp(
        receipt.get("confirmed_start"), "confirmed_start"
    )
    end = _require_aware_timestamp(receipt.get("confirmed_end"), "confirmed_end")
    if end <= start:
        raise ValueError("confirmed_end must be after confirmed_start")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] not in {
            "estimate_sent",
            "appointment_booked",
            "approved",
            "awaiting_specs",
        }:
            raise ValueError("appointment booking requires an open estimate")
        if confirmation_thread_id != record["route"]["thread_id"]:
            raise ValueError("appointment confirmation thread does not match route")
        source_hash = sha256_text(source_message_id)
        approvals = record.get("appointment_approval_requests")
        if not isinstance(approvals, list) or not any(
            isinstance(value, dict)
            and value.get("source_message_id_sha256") == source_hash
            for value in approvals
        ):
            raise ValueError("appointment booking has no matching approved request")
        evidence = {
            "source_message_id_sha256": source_hash,
            "calendar_event_id": calendar_event_id,
            "confirmed_start": receipt["confirmed_start"],
            "confirmed_end": receipt["confirmed_end"],
            "confirmation_message_id": confirmation_message_id,
            "confirmation_thread_id": confirmation_thread_id,
        }
        existing = record.get("appointment_booked")
        if existing is not None:
            if not isinstance(existing, dict):
                raise ValueError("appointment_booked receipt is invalid")
            comparable = dict(existing)
            comparable.pop("booked_at", None)
            comparable.pop("replaced_by", None)
            if comparable == evidence:
                return record
            if existing.get("source_message_id_sha256") == source_hash:
                raise ValueError("conflicting_appointment_receipt")
            # A later approved time replaces the booking; the old one is kept.
            history = record.setdefault("appointment_history", [])
            if not isinstance(history, list):
                raise ValueError("appointment_history must be an array")
            history.append({**existing, "replaced_at": datetime.now(timezone.utc).isoformat()})
        evidence["booked_at"] = datetime.now(timezone.utc).isoformat()
        if record["status"] == "awaiting_specs":
            # Booked before the estimate: the meeting is where the details
            # get settled, so the record keeps waiting for them.
            evidence["before_estimate"] = True
        else:
            record["status"] = "appointment_booked"
        record["appointment_booked"] = evidence
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


def enforce_configured_price(
    internal_cost_sheet: dict[str, Any],
    proposed_price: Any,
    shop_profile: dict[str, Any] | None,
) -> None:
    """Require the bound price to be the configured pricing model's output.

    Without this the customer price is whatever arithmetic the model performed,
    and the binding hash then locks that number in as if it were authoritative.
    """
    if shop_profile is None:
        raise ValueError("approval preparation requires the shop profile")
    expected = pricing_model.quote_price(
        internal_cost_sheet["hard_cost_total"], shop_profile.get("pricing")
    )
    if abs(float(proposed_price) - expected) > 0.01:
        raise ValueError(
            "proposed_price does not match the configured pricing model "
            f"(expected {expected:.2f})"
        )


SPOT_METALS = {"gold", "silver", "platinum", "palladium"}


def _card_rate(card: Any, rate_key: Any, label: str) -> float:
    """Resolve one rate from the shop's configured card, or refuse."""
    if not isinstance(rate_key, str) or not rate_key.strip():
        raise ValueError(f"{label} must name the rate_key it priced from")
    if not isinstance(card, dict) or rate_key not in card:
        raise ValueError(
            f"{label} rate_key '{rate_key}' is not in the shop's configured rates; "
            "escalate for a rate rather than pricing without one"
        )
    rate = card[rate_key]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
        raise ValueError(f"configured rate for '{rate_key}' is not a usable number")
    return float(rate)


def _check_allowance(line: dict[str, Any], pricing: dict[str, Any], label: str) -> None:
    """An allowance line (waste, contingency, setting labor, the minimum job) must redo its arithmetic from the card."""
    try:
        _prefix, section, key = str(line.get("rate_key")).split(":", 2)
    except ValueError:
        raise ValueError(f"{label} names a malformed allowance") from None
    card = pricing if section == "pricing" else pricing.get(section)
    rate = card.get(key) if isinstance(card, dict) else None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ValueError(f"{label} allowance '{section}.{key}' is not on the rate card")
    if abs(float(line.get("rate") or 0) - float(rate)) > 0.01:
        raise ValueError(f"{label}.rate does not equal the configured {section}.{key}")
    kind, basis, total = str(line.get("kind") or ""), float(line.get("basis") or 0), float(line.get("total_cost") or 0)
    expected = {"pct": basis * float(rate) / 100, "setting": float(rate) * (1 + basis / 100), "per_stone": float(rate) * basis,
                "minimum": float(rate) - basis}.get(kind)
    if expected is None or abs(round(expected, 2) - total) > 0.01 or total <= 0:
        raise ValueError(f"{label}.total_cost does not follow from its basis and the configured {section}.{key}")


def enforce_rate_provenance(
    internal_cost_sheet: dict[str, Any],
    pricing: Any,
    spot_evidence: Any = None,
) -> None:
    """Require every unit cost to come from the shop's rates, not from the model.

    Arithmetic and the pricing model are already enforced, so a fabricated rate
    otherwise yields a perfectly consistent and perfectly fictional estimate.
    """
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    spot = pricing.get("spot_metal")
    spot_enabled = isinstance(spot, dict) and spot.get("enabled") is True

    for index, line in enumerate(internal_cost_sheet["stone_lines"]):
        label = f"internal_cost_sheet.stone_lines[{index}]"
        rate = _card_rate(pricing.get("stones_per_carat"), line.get("rate_key"), label)
        if abs(float(line["unit_cost"]) - rate) > 0.01:
            raise ValueError(f"{label}.unit_cost does not equal its configured rate")

    for index, line in enumerate(internal_cost_sheet["other_hard_cost_lines"]):
        label = f"internal_cost_sheet.other_hard_cost_lines[{index}]"
        rate_key = str(line.get("rate_key") or "")
        if rate_key.startswith("allowance:"):
            _check_allowance(line, pricing, label)
            continue
        rate = _card_rate(pricing.get("fees"), line.get("rate_key"), label)
        if abs(float(line["total_cost"]) - rate) > 0.01:
            raise ValueError(f"{label}.total_cost does not equal its configured fee")

    bench = pricing.get("bench_labor_per_hour")
    for index, line in enumerate(internal_cost_sheet["labor_lines"]):
        label = f"internal_cost_sheet.labor_lines[{index}]"
        if isinstance(bench, bool) or not isinstance(bench, (int, float)):
            raise ValueError(
                f"{label} cannot be priced: bench_labor_per_hour is not configured"
            )
        if abs(float(line["rate"]) - float(bench)) > 0.01:
            raise ValueError(f"{label}.rate does not equal bench_labor_per_hour")

    for index, line in enumerate(internal_cost_sheet["metal_lines"]):
        label = f"internal_cost_sheet.metal_lines[{index}]"
        if not spot_enabled:
            rate = _card_rate(
                pricing.get("metal_per_gram"), line.get("rate_key"), label
            )
            if abs(float(line["unit_cost"]) - rate) > 0.01:
                raise ValueError(
                    f"{label}.unit_cost does not equal its configured rate"
                )
            continue
        # Spot pricing is enabled, so the line prices from live metal rather
        # than the card and must show the inputs it used.
        rate_key = line.get("rate_key")
        if rate_key not in SPOT_METALS:
            raise ValueError(
                f"{label}.rate_key must name a spot metal while spot pricing is enabled"
            )
        for field in ("spot_price_per_gram", "purity"):
            if field not in line:
                raise ValueError(f"{label} must include {field} when priced from spot")
        purity = float(line["purity"])
        if not 0 < purity <= 1:
            raise ValueError(f"{label}.purity must be greater than 0 and at most 1")
        quoted = float(line["spot_price_per_gram"])
        prices = spot_evidence.get("prices") if isinstance(spot_evidence, dict) else None
        if not isinstance(prices, dict) or rate_key not in prices:
            raise ValueError(
                f"{label} priced from spot without spot price evidence for {rate_key}"
            )
        if abs(quoted - float(prices[rate_key])) > 0.01:
            raise ValueError(
                f"{label}.spot_price_per_gram does not match the recorded spot evidence"
            )
        if abs(float(line["unit_cost"]) - round(quoted * purity, 2)) > 0.01:
            raise ValueError(
                f"{label}.unit_cost does not equal spot_price_per_gram times purity"
            )


def prepare_approval_state(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    candidate: dict[str, Any],
    shop_profile: dict[str, Any] | None = None,
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
                raise ValueError(
                    "pending approval belongs to a different source message"
                )
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
        cost_components = candidate.get("cost_components")
        internal_cost_sheet = candidate.get("internal_cost_sheet")
        if cost_components is not None:
            if internal_cost_sheet is not None:
                raise ValueError(
                    "current state must contain cost_components or internal_cost_sheet, not both"
                )
            internal_cost_sheet = approval_guard.build_internal_cost_sheet(
                cost_components, candidate.get("proposed_price")
            )
            state.pop("cost_components", None)
        state.update(
            {
                "estimate_id": record["estimate_id"],
                "route": record["route"],
                "specification": record.get("specification"),
                "proposed_price": candidate.get("proposed_price"),
                "internal_cost_sheet": internal_cost_sheet,
            }
        )
        approval_guard.binding_payload(state)
        enforce_configured_price(
            state["internal_cost_sheet"], state["proposed_price"], shop_profile
        )
        import cost_components  # local import: cost_components imports this module

        enforce_rate_provenance(
            state["internal_cost_sheet"],
            cost_components.with_one_time_rates((shop_profile or {}).get("pricing"), record),  # "use 450 once" (9 September 2026)
            candidate.get("spot_price_evidence"),
        )
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
                and item.get("binding_hash") not in rejected_bindings(record)
            ),
            None,
        )
        owner_price = record.get("owner_price") if isinstance(record.get("owner_price"), dict) else None
        if matching is not None:
            comparable = {k: v for k, v in matching.items() if k not in {"requested_at", "owner_set"}}
            if comparable != evidence:
                raise ValueError("conflicting approval request for source message")
        elif record.get("status") == "pending_approval" and owner_price and \
                abs(float(owner_price.get("price", -1)) - float(evidence["proposed_price"])) <= 0.005:
            pass  # the fresh card at the owner's price
        elif record.get("status") != "awaiting_specs":
            raise ValueError("approval evidence requires awaiting_specs status")
        return approval_request


RETIREMENT_REASONS = {
    "duplicate_of_another_thread",
    "created_in_error",
    "superseded_by_another_estimate",
    "customer_withdrew",
    "test_artifact",
    "not_an_inquiry",
    "owner_handles_thread",
}


def record_rendering_revision(root: Path, estimate_id: str, source_message_id: str, note: str, pieces: list[str]) -> dict[str, Any]:
    """The owner passed on the renderings and said what should change (WORKFLOW.md 6.6)."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if not isinstance(note, str) or not note.strip() or len(note) > 400:
        raise ValueError("a revision note must be text of at most 400 characters")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        revisions = record.setdefault("rendering_revisions", [])
        if not isinstance(revisions, list):
            raise ValueError("rendering_revisions must be an array")
        revisions.append({
            "source_message_id_sha256": sha256_text(source_message_id),
            "note": note.strip(),
            "pieces": [str(p) for p in pieces],
            "round": len([r for r in revisions if isinstance(r, dict)
                          and r.get("source_message_id_sha256") == sha256_text(source_message_id)]) + 2,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        write_object(path, record)
        return record


MOVABLE_STATUSES = {"awaiting_specs", "estimate_sent", "appointment_booked", "approved"}


def move_route(root: Path, estimate_id: str, new_route: dict[str, Any], source_message_id: str) -> dict[str, Any]:
    """The same customer continued the same piece in a new email thread (WORKFLOW.md 6.1, owner said "same").

    Every reply from now on goes to the new thread; the old route is kept in
    `route_history`. A record whose price card is pending keeps its thread
    (the card's binding holds the route), so that case still hands over.
    """
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if not isinstance(new_route, dict):
        raise ValueError("new_route must be an object")
    for field in ("thread_id", "gmail_message_id", "recipient"):
        route_ownership.require_text(new_route.get(field), f"new_route.{field}")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") not in MOVABLE_STATUSES:
            raise ValueError(f"estimate is {record.get('status')}; its thread cannot move while a card is pending")
        if new_route.get("recipient", "").lower() != str(record["route"].get("recipient", "")).lower():
            raise ValueError("the new thread is from a different address; not the same customer")
        if new_route.get("thread_id") == record["route"].get("thread_id"):
            return record
        for other in root.glob("jed-*.json"):
            if other.name == f"{estimate_id}.json":
                continue
            try:
                other_record = read_object(other)
            except (OSError, ValueError):
                continue
            if (other_record.get("route") or {}).get("thread_id") == new_route.get("thread_id") \
                    and other_record.get("status") in route_ownership.ACTIVE_STATUSES:
                raise ValueError("another open estimate already owns that thread")
        history = record.setdefault("route_history", [])
        if not isinstance(history, list):
            raise ValueError("route_history must be an array")
        history.append({
            "route": record["route"],
            "moved_by_sha256": sha256_text(source_message_id),
            "moved_at": datetime.now(timezone.utc).isoformat(),
        })
        # Only the thread and the reply headers move. The initiating message
        # stays the one that opened the estimate, so ownership and the
        # evidence rules keep reading the new message as a reply.
        moved = dict(record["route"])
        for field in ("thread_id", "original_message_id", "references", "original_subject"):
            if field in new_route:
                moved[field] = new_route[field]
        record["route"] = moved
        write_object(path, record)
        return record


REOPEN_KINDS = {"design_change", "second_piece"}
ARCHIVED_FIELDS = (
    "estimate_delivery", "approval_binding_hash", "approval_source_message_id", "proposed_price",
    "internal_cost_sheet", "approved_price", "outbound_provider_message_id", "owner_price",
    "rejected_approval_bindings", "rendering_revisions",
)


def reopen_for_change(root: Path, estimate_id: str, source_message_id: str, kind: str, note: str = "") -> dict[str, Any]:
    """After a sent estimate the customer changed the design or added a piece (WORKFLOW.md 6.8).

    The sent estimate is history, never edited: everything bound to it moves
    into `estimate_history` and the record goes back to awaiting_specs on the
    same thread, so the gate, the price, and the card run again from the
    customer's new words. The specification stays as the starting point; the
    next reading merges the change into it (or adds the piece).
    """
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    if kind not in REOPEN_KINDS:
        raise ValueError("kind must be design_change or second_piece")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") not in {"estimate_sent", "appointment_booked", "approved"}:
            raise ValueError(f"estimate is {record.get('status')}; only a sent estimate can be reopened")
        archived = {field: record[field] for field in ARCHIVED_FIELDS if field in record}
        archived.update({
            "revision": int(record.get("revision") or 0),
            "specification": record.get("specification"),
            "reopened_by_sha256": sha256_text(source_message_id),
            "reopened_for": kind,
            "note": str(note or "")[:400],
            "reopened_at": datetime.now(timezone.utc).isoformat(),
        })
        history = record.setdefault("estimate_history", [])
        if not isinstance(history, list):
            raise ValueError("estimate_history must be an array")
        history.append(archived)
        for field in ARCHIVED_FIELDS:
            record.pop(field, None)
        record["revision"] = int(record.get("revision") or 0) + 1
        record["reopened_for"] = kind
        record["missing_required_fields"] = []
        record["status"] = "awaiting_specs"
        write_object(path, record)
        return record


def drop_stale_confirms(root: Path, estimate_id: str, source_message_id: str, keep: list[str]) -> dict[str, Any]:
    """A recorded "ask" carried reading checks the current check no longer raises: drop them.

    The review was written by a run that died (or by an older version of the
    check); the retry re-runs the check on the same words before it honours
    the ask. Only `confirm.*` names not in `keep` go; a real missing field
    stays. An emptied review becomes specs_complete, and the retry prices.
    """
    source_hash = sha256_text(validate_provider_id(source_message_id, "source_message_id"))
    keep_set = {str(k) for k in keep}
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        changed = False
        for review in record.get("thread_reviews", []):
            if not isinstance(review, dict) or review.get("source_message_id_sha256") != source_hash \
                    or review.get("outcome") != "awaiting_specs":
                continue
            before = list(review.get("missing_required_fields") or [])
            after = [f for f in before if not (str(f).startswith("confirm.") and f not in keep_set)]
            if after != before:
                review["missing_required_fields"] = after
                review["stale_confirms_dropped"] = [f for f in before if f not in after]
                if not after:
                    review["outcome"] = "specs_complete"
                changed = True
        if changed:
            record["missing_required_fields"] = sorted(
                f for f in (record.get("missing_required_fields") or [])
                if not (str(f).startswith("confirm.") and f not in keep_set)
            )
            write_object(path, record)
        return record


def carry_prior_facts(record: dict[str, Any], specification: dict[str, Any]) -> dict[str, Any]:
    """After a reopen for a second piece, the pieces already quoted keep every fact they were quoted with.

    The model's re-read may return the prior pieces thin, or one flat piece
    (the new one). The prior specification (estimate_history[-1]) is
    authoritative for the first pieces, in order: every key it had and the
    re-read lacks is restored; nothing on the record is ever asked again.
    """
    if not isinstance(specification, dict) or record.get("reopened_for") != "second_piece":
        return specification
    history = record.get("estimate_history") or []
    prior = history[-1].get("specification") if history and isinstance(history[-1], dict) else None
    if not isinstance(prior, dict) or not prior:
        return specification
    prior_pieces = [{k: v for k, v in piece.items() if k != "pieces"} for piece in pieces_of(prior)]
    raw = specification.get("pieces")
    pieces = [dict(p) for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    if len(pieces) <= len(prior_pieces):
        if len(prior_pieces) != 1 or pieces:
            return specification
        # One flat piece came back: it is the new one; the first is the prior.
        new_piece = {k: v for k, v in specification.items() if k != "pieces"}
        if not new_piece.get("piece_type") or new_piece.get("piece_type") == prior_pieces[0].get("piece_type"):
            return specification
        return {"pieces": [prior_pieces[0], new_piece]}
    for piece, before in zip(pieces, prior_pieces):
        for key, value in before.items():
            if piece.get(key) in (None, "", []) and value not in (None, "", []):
                piece[key] = value
    return {**specification, "pieces": pieces}


CHANGE_MATCH_KEYS = ("metal_color", "finger_size", "metal", "metal_karat")


BAND_STONE_WORDS = ("eternity", "channel set", "channel-set", "channel", "pave", "pavé", "melee", "all the way around",
                    "all around", "around the band", "in the middle of the band", "bead set", "bead-set", "micro pave")
CENTER_STONE_WORDS = ("center stone", "centre stone", "main stone", "solitaire", "halo", "feature stone")


_PIECE_NOUN_RE = re.compile(r"(?i)\b(?:rings?|bands?|bracelets?|necklaces?|chains?|pendants?|earrings?|studs|hoops|anklets?|bangles?|cuffs?|brooch(?:es)?|cuff ?links|charms?|lockets?|solitaires?)\b")
_ORDER_FACT_RES = (
    re.compile(r"(?i)\b\d{1,2}\s*(?:k|kt|karat)\b"),                                             # a karat
    re.compile(r"(?i)\b(?:gold|platinum|silver|palladium|(?:white|yellow|rose)\s*gold|wg|yg|rg)\b"),  # a metal
    re.compile(r"(?i)\b(?:diamonds?|sapphires?|rub(?:y|ies)|emeralds?|moissanite|lab[- ]?(?:grown|created)?|stones?|gems?|cts?|carats?)\b"),  # a stone
    re.compile(r"(?i)\b(?:size\s*\d|\d+(?:\.\d+)?\s*[-\s]?(?:inch(?:es)?|in|mm|cm)\b|wrist|finger|length)\b"),  # a size
    re.compile(r"(?i)(?:\$\s*\d|\bbudget\b|\b\d{1,3},\d{3}\b)"),                              # a budget
)


# A ready-made piece: the shop shows what is in stock at a visit (WORKFLOW.md triage table).
INVENTORY_RE = re.compile(
    r"(?i)\b(?:in stock|ready[- ]to[- ]ship|ready[- ]made|pre[- ]?made|off the shelf|already made|ready to go|"
    r"(?:do|did) you (?:have|carry|sell|stock) (?:any|some|a|an|the)\b|what do you have\b|something (?:ready|available|in stock)|"
    r"available (?:now|today|right away|to buy|for purchase)|(?:have|got) anything)\b"
)


def asks_for_inventory(own_words: str) -> bool:
    """The customer asks for something the shop already has, not something made for them."""
    return bool(INVENTORY_RE.search(str(own_words or "")))


def mark_inventory_inquiry(root: Path, estimate_id: str, source_message_id: str, note: str = "") -> dict[str, Any]:
    """Remember that this record is a ready-made inquiry: the desk offers a visit, never a questionnaire."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if not record.get("inventory_inquiry"):
            record["inventory_inquiry"] = {
                "since_gmail_message_id": source_message_id,
                "note": str(note or "")[:200],
                "marked_at": datetime.now(timezone.utc).isoformat(),
            }
            write_object(path, record)
        return record


_NOT_AN_ORDER_RE = re.compile(r"(?i)\b(?:apprais\w*|insurance|valuation|valued?|worth|authentic\w*|status of|my order|order status|tracking)\b")


def reads_like_an_order(own_words: str) -> bool:
    """A piece named with at least two facts an estimate needs (karat, metal, stone, size, budget).

    Live (8 September 2026): "any lab tennis bracelets available in the $2,000
    to $3,000 range? 14k WG lab diamonds, 7-inch wrist... something ready to
    ship?" was read as an inventory question and filed silently. A shop that
    makes to order quotes such a message; the reading decides the kind, the
    words decide whether it is an order.
    """
    text = str(own_words or "")
    if not _PIECE_NOUN_RE.search(text) or _NOT_AN_ORDER_RE.search(text):
        return False
    return sum(1 for pattern in _ORDER_FACT_RES if pattern.search(text)) >= 2


_DAY = r"(?:mon|tues?|wed(?:nes)?|thurs?|fri|sat(?:ur)?|sun)(?:day)?|tomorrow|tonight|today|this (?:afternoon|evening|week|weekend)|next week|the weekend"
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:am|pm|o'?clock)|noon|(?:in the )?(?:morning|afternoon|evening)"
# A meeting named outright: an appointment, coming by the shop, meeting in person.
MEETING_RE = re.compile(
    r"(?i)\b(?:reschedul\w*|appointment|(?:our|the|a|that|my) meeting|in person|stop by|drop by|swing by|"
    r"come (?:in|by|over)(?: to)? (?:the|your) (?:shop|store)|come (?:in|by|over)\b[^.?!\n]{0,20}\b(?:" + _DAY + r")|"
    r"meet (?:you|up|with you|in person)|(?:can|could|shall|should) we meet\b|meet\b[^.?!\n]{0,25}\b(?:" + _DAY + r")|"
    r"(?:can|could|may) (?:i|we) (?:come|stop|drop|swing) (?:in|by|over)|(?:would|'d|i'd|we'd) (?:like|love) to (?:come|stop|drop|swing) (?:in|by|over)|"
    r"(?:want|happy|glad) to (?:come|stop|drop|swing) (?:in|by|over))\b"
)
# A day and a time proposed as a question: "any chance we can do Friday at 4pm?".
PROPOSAL_RE = re.compile(
    r"(?i)\b(?:can|could|would|does|do|how about|what about|any chance)\b[^.?!\n]{0,40}\b(?:" + _DAY + r")\b"
    r"[^.?!\n]{0,30}\b(?:" + _TIME + r")\b"
)
# A deadline or a delivery, not a visit: "can you have it ready by Friday at 5pm?".
NOT_A_VISIT_RE = re.compile(
    r"(?i)\b(?:(?<!come )(?<!stop )(?<!drop )(?<!swing )by|before|until|ready|done|finished|deliver\w*|ship\w*|"
    r"pick(?: it)? up|arrive\w*|mail\w*)\b"
)
RESCHEDULE_RE = re.compile(
    r"(?i)\b(?:reschedul\w*|something came up|can(?:no|')t make|(?:move|push|change) (?:it|our|the|my|that)\b|"
    r"(?:a )?different (?:day|time)|another (?:day|time)|instead|my mistake|wrong (?:day|time|date)|meant to say|mixed (?:up|that up)|"
    r"works better|would be better)\b"
)
# Picking or accepting a time the shop offered: "the second one works", "Wednesday is fine", "2pm works".
ACCEPTS_TIME_RE = re.compile(
    r"(?i)\b(?:the (?:first|second|third|last|earlier|later) (?:one|time|slot)|either (?:one|works|is fine)|any of (?:those|them)|"
    r"that (?:time|one|slot) (?:works|is fine|is good)|(?:works|is fine|is good|is perfect|sounds good|sounds great|sounds perfect) for (?:me|us)|"
    r"see you (?:then|there|on|at)|book (?:it|me|that)|(?:let'?s|lets) do (?:it|that|the)|i'?ll (?:take|be there|come (?:then|at|on))|"
    r"(?:" + _DAY + r")\b[^.?!\n]{0,30}\b(?:works|is fine|is good|is perfect|would be (?:great|fine|good|perfect))|"
    r"(?:" + _TIME + r")\b[^.?!\n]{0,20}\b(?:works|is fine|is good|is perfect))\b"
)


ASKS_FOR_ESTIMATE_RE = re.compile(
    r"(?i)\b(?:estimates?|ballpark|quotes?|quotation|pricing|prices?|priced|costs?|how much|"
    r"what (?:would|does|do|will) (?:it|that|they|this|these|one|something like (?:this|that)) (?:run|cost|come to|be))\b"
)


def asks_for_estimate(own_words: str) -> bool:
    """The customer mentions an estimate, a price, or a cost: the desk pursues it (the owner's rule, 8 September 2026)."""
    return bool(ASKS_FOR_ESTIMATE_RE.search(str(own_words or "")))


REPAIR_WORK_RE = re.compile(
    r"(?i)\b(?:resiz\w*|repair\w*|fix(?:ed|ing)?|restor\w*|solder\w*|refinish\w*|"
    r"repolish\w*|polish\w*|re-?plate\w*|re-?set\w*|remount\w*|replace\w*\s+(?:a\s+|the\s+)?"
    r"(?:stone|clasp|prong|shank|head)|size\s+\d+(?:\.\d+)?\s+(?:down|up|to))\b"
)
EXPLICIT_NEW_PIECE_RE = re.compile(
    r"(?i)\b(?:new|another|matching|custom)\s+(?:piece|ring|band|earrings?|pendant|bracelet|necklace)|"
    r"\b(?:make|create|design|build|commission)\s+(?:me\s+)?(?:a|an|new|another|matching)\b"
)
REPAIR_TIME_ESTIMATE_RE = re.compile(
    r"(?i)\b(?:estimate|idea|sense)\s+(?:of\s+)?how\s+long\b|\btime\s+estimate\b|"
    r"\bestimated?\s+(?:time|turnaround)\b|\bhow\s+long\b"
)
REPAIR_PRICE_WORD_RE = re.compile(r"(?i)\b(?:ballpark|quotes?|quotation|pricing|prices?|costs?|how much)\b")


def is_repair_request(customer_words: str) -> bool:
    """Whether the customer's own conversation explicitly concerns work on an existing item."""
    return bool(REPAIR_WORK_RE.search(str(customer_words or "")))


def explicitly_requests_new_piece(customer_words: str) -> bool:
    """A repair photo or jewelry noun is not a new commission; the customer's words must introduce one."""
    return bool(EXPLICIT_NEW_PIECE_RE.search(str(customer_words or "")))


def asks_for_repair_price(newest_own_words: str) -> bool:
    """A direct repair price request, excluding questions about turnaround time."""
    words = str(newest_own_words or "")
    if REPAIR_TIME_ESTIMATE_RE.search(words) and not REPAIR_PRICE_WORD_RE.search(words):
        return False
    return asks_for_estimate(words)


LEAVES_TO_JEWELER_RE = re.compile(
    r"(?i)\b(?:i (?:don'?t|do not) know|not sure|no idea|no clue|not really|haven'?t (?:decided|thought about it)|no preference|up to you|"
    r"you (?:decide|choose|pick)|your call|whatever you (?:think|suggest|recommend)|what(?:ever)? (?:looks|works) best|"
    r"i'?ll leave (?:it|that) to you|leave (?:it|that) to you|just a reference|use your judgment|surprise me|"
    r"no,? i (?:don'?t|do not)(?![^.?!\n]{0,20}\b(?:want|like|need)\b))\b"  # "No, I don't." answers the ask; "No, I don't want a halo" does not
)


GRADE_KEYS = ("stone_color", "stone_clarity")

# A setting the customer names outright; the words are theirs, so the fact is theirs (9 September 2026).
SETTING_WORDS_RE = re.compile(
    r"(?i)\b(halo|bezel(?:[- ]set)?|solitaire|pav[eé](?:[- ]set)?|channel(?:[- ]set)?|prong(?:[- ]set)?|"
    r"three[- ]stone|tension(?:[- ]set)?|cluster|cathedral|flush(?:[- ]set)?|bead(?:[- ]set)?)\b"
)
# "no halo", "not a halo", "don't want a halo": the word right before the setting (an article allowed) says no.
_NOT_THAT_SETTING_RE = re.compile(r"(?i)\b(?:no|not|without|nor|don'?t want|rather than|instead of)\s+(?:(?:a|an|the|any)\s+)?$")


def setting_in_words(own_words: str) -> str | None:
    """The setting style the customer's own words name ("earrings like these... not sure the halo size"), else None."""
    text = str(own_words or "")
    for match in SETTING_WORDS_RE.finditer(text):
        if _NOT_THAT_SETTING_RE.search(text[max(0, match.start() - 24):match.start()]):
            continue
        return match.group(1).lower().replace("é", "e")
    return None


EARRING_STYLE_WORDS = (("stud", "stud"), ("hoop", "hoop"), ("huggie", "hoop"), ("drop", "drop"), ("dangl", "drop"),
                       ("chandelier", "drop"), ("threader", "drop"))
_EARRING_STYLE_RE = re.compile(r"(?i)\b(stud|hoop|huggie|drop|dangl|chandelier|threader)\w*")


def earring_style_in_words(words: str) -> str | None:
    """'stud', 'hoop', or 'drop' when the words name a kind of earring, else None."""
    match = _EARRING_STYLE_RE.search(str(words or ""))
    if not match:
        return None
    stem = match.group(1).lower()
    return next((style for word, style in EARRING_STYLE_WORDS if stem.startswith(word)), None)


# "the pendant you made for me", "the ring I bought from you", "same as the one you made": a piece the shop made before.
PRIOR_PIECE_RE = re.compile(
    r"(?i)\b(?:(?:you|you guys|you all|your shop|your team)\s+(?:made|created|built|designed|did|crafted)\s+(?:for\s+)?(?:me|us|my|our|her|him)\b|"
    r"(?:made|created|built|designed|crafted)\s+by\s+you\b|(?:bought|purchased|ordered|got|had made)\s+(?:it\s+|this\s+|that\s+|these\s+)?"
    r"(?:from|through|with|at)\s+(?:you|your shop|your store)\b|(?:the|this|that)\s+(?:one|piece|ring|pendant|necklace|bracelet|earrings)\s+you\s+made\b|"
    r"you\s+made\s+(?:this|that|the|these|those|it)\b)"
)
PRIOR_PIECE_STATUSES = ("estimate_sent", "appointment_booked", "approved")


# "the emerald earrings we talked about earlier", "the ring you quoted", "from my earlier inquiry": an estimate on file.
EARLIER_CONVERSATION_RE = re.compile(
    r"(?i)\b(?:(?:we|you)\s+(?:talked|spoke|discussed|chatted|went over)\s+(?:about\s+)?(?:[^.?!\n]{0,40}?\b)?(?:earlier|before|previously|last (?:week|month|time|year)|the other day|a while (?:ago|back))\b|"
    r"you\s+(?:quoted|estimated|priced)\s+(?:me|us|for me|for us)?\b|(?:the|your)\s+(?:estimate|quote|price|number)\s+you\s+(?:sent|gave|emailed)\b|"
    r"(?:from|in)\s+(?:my|our|the|your)\s+(?:earlier|previous|last|other)\s+(?:email|emails|inquiry|estimate|quote|conversation|thread|message)\b|"
    r"(?:we|you)\s+(?:talked|spoke|discussed|went over)\s+(?:about\s+)?(?:the|this|that|these|those|my|our)\b|"
    r"(?:do you |you )?remember\s+(?:the|those|these|that|my|our|when|how)\b|"
    r"(?:the same|just like|like|same as|as)\s+(?:last time|before|the last (?:one|ones|pair|time)|the ones? (?:you|we))\b)"
)


_PAST_CUE_RE = re.compile(
    r"(?i)\b(?:remember|recall|again|last time|before|previous(?:ly)?|earlier|prior|original(?:ly)?|the other day|"
    r"a while (?:ago|back)|(?:years?|months?|weeks?) ago|last (?:year|month|week|spring|summer|fall|winter|christmas)|"
    r"you (?:did|made|built|sent|quoted|priced|designed|sold|showed)|from you|another (?:one|pair|set|ring|band|pendant|necklace|bracelet)|"
    r"one more|a second (?:one|pair|ring)|matching|same as|(?:just )?like the (?:ones?|pair|last)|the usual|the ones? (?:I|we) (?:got|bought|ordered|had))\b"
)
_PIECE_WORD_RE = re.compile(
    r"(?i)\b(?:rings?|bands?|pendants?|necklaces?|bracelets?|earrings?|studs|hoops|drops|piece|pieces|pair|chain|set|order|estimate|quote|design)\b"
)
_NOT_THE_PAST_RE = re.compile(
    r"(?i)\b(?:website|site|photo|picture|pictured|attached|image|link|instagram|pinterest|catalog|online|below|above|here|"
    r"(?:another|a different|other|some other) (?:jeweler|jeweller|shop|store)|by a jewel+er|elsewhere|somewhere else|inherited|heirloom|"
    r"passed down|grandmother|grandfather|grandma|grandpa|great-)\b"
)
_STANDALONE_PAST_RE = re.compile(r"(?i)\b(?:same as (?:last time|before)|like (?:last time|before)|as before|the usual)\b")


def refers_to_an_earlier_conversation(own_words: str) -> bool:
    """The customer points at something the desk already has: an estimate, a conversation, a piece from before.

    "The earrings we talked about earlier", "remember those sapphire earrings?",
    "another pair like the ones I got", "same as last time but in rose gold".
    A sentence that names a piece and a cue to the past counts; a sentence that
    points at a photo, a website or an attachment does not (that is a new
    idea, not our history). The owner, 10 September 2026: the whole family,
    not one phrase.
    """
    text = str(own_words or "")
    if EARLIER_CONVERSATION_RE.search(text):
        return True
    for sentence in _SENTENCE_RE.split(text):
        if _NOT_THE_PAST_RE.search(sentence):
            continue
        if _STANDALONE_PAST_RE.search(sentence):
            return True
        if _PAST_CUE_RE.search(sentence) and _PIECE_WORD_RE.search(sentence):
            return True
    return False


def refers_to_a_prior_piece(own_words: str) -> bool:
    """The customer says the shop made (or sold them) the piece they are describing."""
    return bool(PRIOR_PIECE_RE.search(str(own_words or "")))


_PRIOR_PIECE_WORDS = {
    "earring": "earring", "earrings": "earring",
    "stud": "stud", "studs": "stud",
    "hoop": "hoop", "hoops": "hoop",
    "ring": "ring", "rings": "ring",
    "band": "band", "bands": "band",
}
_PRIOR_PIECE_STOP_WORDS = {"a", "an", "the", "of", "pair", "custom", "piece"}


def _prior_piece_words(value: str | None) -> set[str]:
    """Comparable jewelry words; explicit forms avoid unsafe generic stemming."""
    return {
        _PRIOR_PIECE_WORDS.get(word, word)
        for word in re.findall(r"[a-z]+", str(value or "").lower())
        if word not in _PRIOR_PIECE_STOP_WORDS
    }


def find_prior_piece(root: Path, recipient: str, piece_type: str | None, exclude: str | None = None) -> dict[str, Any] | None:
    """The desk's most recent quoted estimate for this customer that names the same kind of piece, else None.

    "On file" means the desk's own records (the jeweler, 9 September 2026:
    when the piece is on file and can be found, the owner is not asked).
    The quoted specification (the archived binding if there is one, else the
    record's) is what the customer had; a piece of another kind is not it.
    """
    email = str(recipient or "").strip().lower()
    if not email or not root.exists():
        return None
    wanted = _prior_piece_words(piece_type)
    found: list[tuple[int, str, dict[str, Any]]] = []
    for path in sorted(root.glob("jed-*.json")):
        try:
            record = read_object(path)
        except (OSError, ValueError):
            continue
        if record.get("estimate_id") == exclude:
            continue
        route = record.get("route") if isinstance(record.get("route"), dict) else {}
        if str(route.get("recipient") or "").strip().lower() != email:
            continue
        history = record.get("estimate_history") or []
        quoted = history[-1].get("specification") if history and isinstance(history[-1], dict) else None
        rank = 2
        if not isinstance(quoted, dict) or not quoted:
            quoted = record.get("specification") if record.get("status") in PRIOR_PIECE_STATUSES else None
        if not isinstance(quoted, dict) or not quoted:
            # An estimate that never reached a price still holds what the customer said ("the earrings we talked
            # about earlier", 9 September 2026); a quoted one is preferred when both exist.
            quoted = record.get("specification") if isinstance(record.get("specification"), dict) else None
            rank = 1
            if (record.get("retirement") or {}).get("reason") in ("not_an_inquiry", "test_artifact", "created_in_error"):
                continue
        if not isinstance(quoted, dict) or not quoted or not _present(quoted.get("piece_type")):
            continue
        had = _prior_piece_words(quoted.get("piece_type"))
        if wanted and not (wanted & had):
            continue
        found.append((rank, str(record.get("created_at") or record.get("estimate_id") or ""),
                      {"estimate_id": record.get("estimate_id"), "specification": quoted}))
    if not found:
        return None
    found.sort(key=lambda item: (item[0], item[1]))
    return found[-1][2]


PRIOR_LOOSE_KEYS = ("notes", "reference_images", "scheduling_intent", "pieces", "quantity", "event_date", "budget", "engraving")


def prior_piece_facts(prior_spec: dict[str, Any], current: dict[str, Any], own_words: str = "") -> dict[str, Any]:
    """The facts of the piece on file the new words do not replace (a smaller stone replaces the carat and the size).

    A value the new reading holds stands only when the customer's new words
    say it; a guess of the reading ("prong" for studs) gives way to the
    piece on file (live, 9 September 2026: "the exact same thing" lost its
    diamond halo in the rendering).
    """
    if not isinstance(prior_spec, dict):
        return {}
    import ledger  # local import: the ledger imports this module

    carried: dict[str, Any] = {}
    for key, value in prior_spec.items():
        if key in PRIOR_LOOSE_KEYS or not _present(value):
            continue
        held = (current or {}).get(key)
        if _present(held) and (not own_words or ledger.source_of(held, own_words, "")[0] == "customer"):
            continue  # the new words say so
        carried[key] = value
    new_size = any(_present((current or {}).get(k)) and (not own_words or ledger.source_of((current or {}).get(k), own_words, "")[0] == "customer")
                   for k in ("stone_dimensions", "stone_carat"))
    new_metal = _present((current or {}).get("metal")) and (not own_words or ledger.source_of((current or {}).get("metal"), own_words, "")[0] == "customer") \
        and str((current or {}).get("metal") or "").strip().lower() != str((prior_spec or {}).get("metal") or "").strip().lower()
    if new_metal:
        # "Everything the same but in platinum": the old metal's colour and karat do not ride along (live, 9 September
        # 2026: "white platinum").
        for key in ("metal_color", "metal_karat"):
            carried.pop(key, None)
    if new_size:
        # A new size in their words: the old carat and size do not ride along.
        carried.pop("stone_carat", None)
        carried.pop("stone_dimensions", None)
        carried.pop("stone_carat_basis", None)
    piece = str(prior_spec.get("piece_type") or (current or {}).get("piece_type") or "").lower()
    if "earring" in piece and not _present((current or {}).get("earring_style")) and not _present(carried.get("earring_style")):
        # An earlier estimate from before the style was a field: its own words or photo say studs, hoops, or drops
        # (live, 9 September 2026: the carried pair was still asked "studs, hoops, or drops?").
        style = earring_style_in_words(piece) or earring_style_in_words(str(prior_spec.get("reference_images") or "")) \
            or earring_style_in_words(str(prior_spec.get("setting_style") or "")) or earring_style_in_words(str(prior_spec.get("notes") or ""))
        if style:
            carried["earring_style"] = style
    return carried


def prior_basis(record: dict[str, Any] | None) -> bool | str:
    """False, "estimate" (an earlier estimate on file), or "made" (the owner's details of a piece the shop made)."""
    prior = (record or {}).get("prior_piece") if isinstance((record or {}).get("prior_piece"), dict) else {}
    if not prior.get("on_file"):
        return False
    if prior.get("basis") in ("made", "estimate"):
        return str(prior["basis"])
    return "made" if prior.get("owner") == "details" else "estimate"


def mark_prior_piece(root: Path, estimate_id: str, info: dict[str, Any]) -> dict[str, Any]:
    """Remember what the desk found (or the owner said) about the piece the customer says the shop made."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        existing = record.get("prior_piece") if isinstance(record.get("prior_piece"), dict) else {}
        on_file = bool(info.get("on_file", existing.get("on_file")))
        record["prior_piece"] = {**existing, "on_file": on_file, **{k: v for k, v in info.items() if k != "on_file"}}
        write_object(path, record)
        return record


def set_one_time_rate(root: Path, estimate_id: str, rate_kind: str, rate_key: str, value: float) -> dict[str, Any]:
    """A rate for this estimate only ("use 450 once"): never on the card, used by this record's pricing alone."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        rates = record.setdefault("one_time_rates", {})
        rates.setdefault(rate_kind, {})[rate_key] = float(value)
        write_object(path, record)
        return record


CUSTOMER_FIELDS = ("name", "phone", "notes")
PHONE_RE = re.compile(r"(?<!\d)(?<!\d\.)(?:\+?1[ .-]?)?\(?(\d{3})\)?[ .-]?(\d{3})[ .-]?(\d{4})(?!\d)(?!\.\d)")
_CALL_RE = re.compile(r"(?i)\b(?:phone|call|zoom|video|facetime|google meet|teams|ring me|give me a ring)\b")
_VISIT_RE = re.compile(r"(?i)\b(?:in person|come (?:in|by|over)|stop by|drop by|swing by|visit|at the shop|at your shop|your store|the store"
                       r"|bring(?:ing)? (?:it|them|these|those|everything|the \w+) (?:in|by|along|with me)|show you)\b")
_COURTESY_RE = re.compile(
    r"(?i)^(?:[\s!.,\-]*(?:see you (?:then|there|soon|tomorrow|today|tonight|on \w+|next \w+|at \d[\d:]*\s*(?:am|pm)?|\w+day)"
    r"|thanks?(?: you)?(?: so much| very much| a lot| again)?|thank you(?: so much| very much| again)?|perfect|great|sounds (?:good|great|perfect)"
    r"|looking forward(?: to it| to that| to meeting you)?|will do|got it|ok(?:ay)?|awesome|wonderful|cheers|talk (?:soon|then|tomorrow)"
    r"|until (?:then|tomorrow)|excellent|noted|confirmed|that works|works for me)[\s!.,\-]*)+$"
)


def courtesy_only(own_words: str) -> bool:
    """A note with nothing to act on: 'See you tomorrow!', 'Thanks, looking forward to it.'

    The first paragraph must be nothing but courtesy, and no question may follow it anywhere (a signature
    with a number or an address is fine). Live, 9 September 2026: 'See you tomorrow!' after a booking
    got a questionnaire.
    """
    text = str(own_words or "").strip()
    if not text:
        return False
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    first, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if "?" in first or not _COURTESY_RE.fullmatch(first.replace("\n", " ")):
        return False
    return "?" not in rest


def phone_in_words(text: str) -> str | None:
    """The first phone number in the text (a signature counts), as digits with dots: 213.431.9336."""
    match = PHONE_RE.search(str(text or ""))
    return ".".join(match.groups()) if match else None


def meeting_kind_in_words(own_words: str) -> str | None:
    """'call' when they ask for a phone or video call, 'visit' when they ask to come in, else None."""
    text = str(own_words or "")
    if _CALL_RE.search(text) and not _VISIT_RE.search(text):
        return "call"
    if _VISIT_RE.search(text):
        return "visit"
    return None


def note_meeting(root: Path, estimate_id: str, kind: str | None, phone: str | None) -> dict[str, Any]:
    """What the booked meeting is (a call, a visit) and the number to call, kept beside the booking."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        meeting = dict(record.get("meeting") or {})
        if kind:
            meeting["kind"] = kind
        if phone:
            meeting["phone"] = phone
        meeting["at"] = datetime.now(timezone.utc).isoformat()
        record["meeting"] = meeting
        if phone:
            overrides = dict(record.get("customer_overrides") or {})
            if not overrides.get("phone"):
                overrides["phone"] = phone
                record["customer_overrides"] = {**overrides, "at": meeting["at"]}
        write_object(path, record)
        return record


def customer_phone(record: dict[str, Any] | None) -> str:
    override = (record or {}).get("customer_overrides") if isinstance((record or {}).get("customer_overrides"), dict) else {}
    meeting = (record or {}).get("meeting") if isinstance((record or {}).get("meeting"), dict) else {}
    return str(override.get("phone") or meeting.get("phone") or "")


def save_customer_overrides(root: Path, estimate_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """What the owner typed about the person on the Customers tab: a corrected name, a phone, notes (9 September 2026)."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        block = {k: str(values.get(k) or "").strip()[:200] for k in CUSTOMER_FIELDS if str(values.get(k) or "").strip()}
        if block:
            record["customer_overrides"] = {**block, "at": datetime.now(timezone.utc).isoformat()}
        else:
            record.pop("customer_overrides", None)
        write_object(path, record)
        return record


def customer_name(record: dict[str, Any] | None) -> str:
    """The owner's corrected name when they gave one, else the address line's display name."""
    override = (record or {}).get("customer_overrides") if isinstance((record or {}).get("customer_overrides"), dict) else {}
    if override.get("name"):
        return str(override["name"])
    import kolo_safe  # local import: kolo_safe imports this module

    return kolo_safe._sender_display(str(((record or {}).get("route") or {}).get("recipient") or ""))


def customer_first_name(record: dict[str, Any] | None) -> str:
    override = (record or {}).get("customer_overrides") if isinstance((record or {}).get("customer_overrides"), dict) else {}
    name = str(override.get("name") or "").strip()
    return name.split()[0].strip(",.") if name else ""


def save_sheet_draft(root: Path, estimate_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """What the owner typed on the cost sheet, kept on the record so nothing typed is lost (9 September 2026)."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        previous = record.get("sheet_draft") if isinstance(record.get("sheet_draft"), dict) else {}
        record["sheet_draft"] = {**{k: v for k, v in draft.items() if k in ("status", "details", "lines", "hash")},
                                 "at": datetime.now(timezone.utc).isoformat(), **({"acted_hash": previous["acted_hash"]} if previous.get("acted_hash") else {})}
        write_object(path, record)
        return record


def mark_sheet_draft_acted(root: Path, estimate_id: str, draft_hash: str, quantities: dict[str, Any]) -> dict[str, Any]:
    """The draft was used: its quantities and unit costs become the owner's numbers for the price."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        draft = dict(record.get("sheet_draft") or {})
        draft["acted_hash"] = draft_hash
        record["sheet_draft"] = draft
        record["owner_quantities"] = quantities
        write_object(path, record)
        return record


def owner_supplies_facts(root: Path, estimate_id: str, facts: dict[str, Any]) -> dict[str, Any]:
    """The owner's details of a piece on file (in their books, not the desk's) join the specification, as owner facts."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") != "awaiting_specs":
            raise ValueError("only an estimate still awaiting specifications can take the owner's details")
        specification = dict(record.get("specification") or {})
        given = {k: v for k, v in facts.items() if _present(v)}
        specification.update(given)
        record["specification"] = specification
        # What the owner supplied is no longer missing (the cost sheet's Details cell, 10 September 2026).
        record["missing_required_fields"] = [f for f in (record.get("missing_required_fields") or []) if f not in given]
        write_object(path, record)
        return record


DESK_MODES = ("concierge", "auto")


def desk_mode(profile: dict[str, Any] | None) -> str:
    """concierge (the default): the desk books the call and the owner gathers the details; auto: the desk asks by email."""
    mode = str(((profile or {}).get("desk") or {}).get("mode") or "concierge").strip().lower()
    return mode if mode in DESK_MODES else "concierge"


CONCIERGE_QUESTIONS = (
    ("budget", "do you have a budget in mind, even a rough range?"),
    ("event_date", "is there a date you would like it by?"),
)


def concierge_questions(specification: dict[str, Any] | None) -> list[str]:
    """Budget and timeframe, the two things the desk asks before the call (the jeweler, 9 September 2026), unless already given."""
    spec = specification if isinstance(specification, dict) else {}
    return [q[0].upper() + q[1:] for key, q in CONCIERGE_QUESTIONS if not _present(spec.get(key))]


def mark_concierge(root: Path, estimate_id: str, **info: Any) -> dict[str, Any]:
    """Remember the concierge steps taken on this estimate (the offer, the standing details question, the owner's details)."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        block = dict(record.get("concierge") or {})
        block.update(info)
        record["concierge"] = block
        write_object(path, record)
        return record


def photo_reading(specification: dict[str, Any] | None) -> str:
    """The desk's reading of the customer's example photo ("from the photo: ..."), else ''."""
    text = str((specification or {}).get("reference_images") or "").strip()
    return text if text.lower().startswith("from the photo") else ""


def vision_in_words(specification: dict[str, Any] | None, on_file: bool | str = False) -> str | None:
    """The customer's vision as a jeweler would say it back, when a photo came with the words (the owner, 9 September 2026).

    "I want the attached but with sapphires" plus a photo of halo studs is
    confirmed, not questioned: "sapphire stud earrings with a diamond halo,
    round lab-grown sapphires at 2.5 ct each, in 14K white gold". Built from
    the facts on the record (their words, then the photo), for one piece;
    None when there was no photo or the reading has nothing to say.
    """
    spec = specification if isinstance(specification, dict) else {}
    if not photo_reading(spec) and not on_file:
        return None
    raw = spec.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        return None
    import cost_components  # local import: cost_components imports this module

    def clean(value: Any) -> str:
        text = str(value or "").strip()
        return "" if not text or text.lower() in ("jeweler's choice", "n/a", "unknown", "none", "unspecified") else text

    piece = clean(spec.get("piece_type")).lower()
    stone = clean(spec.get("stone_type")).lower()
    style = clean(spec.get("earring_style")).lower()
    setting = clean(spec.get("setting_style")).lower()
    if not piece and not stone:
        return None
    pair = is_pair(spec)
    # The piece, named the way a jeweler says it: "sapphire stud earrings", "a sapphire halo ring".
    if "earring" in piece:
        head = " ".join(w for w in (stone, style, "earrings") if w)
    else:
        core = piece if not stone or stone in piece else f"{stone} {piece}"
        head = core if core.startswith(("a ", "an ")) else ("an " if core[:1] in "aeiou" else "a ") + core
    parts = [head]
    accents = clean(spec.get("accent_stones")).lower() + " " + clean(spec.get("accent_stone_type")).lower()
    if "halo" in setting:
        accent = "diamond" if "diamond" in accents or "melee" in accents else clean(spec.get("accent_stone_type")).lower()
        parts[0] += f" with a {accent} halo" if accent else " with a halo"
    elif setting:
        parts[0] += f", {setting}" if setting.endswith(("set", "setting")) else f" in a {setting} setting"
    if stone:
        shape = clean(spec.get("stone_shape") or spec.get("stone_cut")).lower()
        origin = clean(spec.get("stone_origin")).lower()
        carat = spec.get("stone_carat")
        basis = clean(spec.get("stone_carat_basis")).lower()
        noun = stone + ("s" if pair and not stone.endswith("s") else "")
        words = " ".join(w for w in (shape, origin, noun) if w)
        detail = bool(shape or origin or carat not in (None, "", 0))
        if carat not in (None, "", 0):
            try:
                number = f"{float(carat):g}"
            except (TypeError, ValueError):
                number = str(carat)
            words += f" at {number} ct" + (" each" if basis == "each" else " total for the pair" if basis == "total" else "")
        if detail:
            parts.append(words)  # the bare stone is already in the piece's name
    metal = cost_components.extract_metal(spec).get("description")
    if metal:
        parts.append(f"in {metal}")
    size = clean(spec.get("stone_dimensions"))
    if size and not spec.get("stone_carat"):
        parts.append(f"the stone {size}")
    if on_file:
        made = isinstance(on_file, str) and on_file == "made"
        parts.append("after the piece we made for you, with the changes you named" if made else "after the design we discussed before, with the changes you named")
    return ", ".join(parts)


_MM_SIZE_RE = re.compile(r"(?i)\b(\d{1,2}(?:\.\d)?)\s*(?:mm)?\s*(?:x|×|by)\s*(\d{1,2}(?:\.\d)?)\s*mm\b")


def stone_size_in_words(own_words: str) -> str | None:
    """The stone size in millimetres the customer wants ('15mm x 12mm'); the last one named when they compare sizes."""
    found = [f"{a}mm x {b}mm" for a, b in _MM_SIZE_RE.findall(str(own_words or ""))]
    return found[-1] if found else None


_CARAT_RANGE_WORDS_RE = re.compile(r"(?i)\b(\d+(?:\.\d+)?)\s*(?:ct|carats?)?\s*(?:to|-|–|or)\s*(\d+(?:\.\d+)?)\s*(?:ct|carats?)\b")


def settle_carat_range(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """"Maybe in the 2 to 3 ct range": the top of the range is priced (estimated high), the range shown as the assumption.

    Live (9 September 2026): the reading took the middle, the check saw a
    figure the reading lacked, and the customer was asked to confirm a carat
    they had given as a range. A range is an answer.
    """
    if not isinstance(specification, dict):
        return specification
    raw = specification.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        return specification
    match = _CARAT_RANGE_WORDS_RE.search(str(own_words or ""))
    if not match:
        return specification
    low, high = float(match.group(1)), float(match.group(2))
    if not 0 < low < high < 100:
        return specification
    current = specification.get("stone_carat")
    try:
        number = float(current) if current not in (None, "") else None
    except (TypeError, ValueError):
        number = None
    if number is not None and not (low <= number <= high):
        return specification  # a carat outside the range came from somewhere else; leave it
    return {**specification, "stone_carat": high, "stone_carat_range": f"{low:g} to {high:g} ct"}


def settle_stone_dimensions(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """A stone sized in millimetres by the customer is sized; the carat follows from it and is never asked.

    Live (9 September 2026): "she would like 15mm x 12mm oval blue topaz"
    was asked what carat weight, then "I'm not sure". The jeweler works the
    weight out from the millimetres; the cost sheet estimates it and the
    card shows the assumption.
    """
    if not isinstance(specification, dict):
        return specification
    raw = specification.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        return specification
    if _present(specification.get("stone_dimensions")):
        return specification
    size = stone_size_in_words(own_words)
    return {**specification, "stone_dimensions": size} if size else specification


ATTACHMENT_VISUAL_FIELDS = {"earring_style", "stone_shape", "stone_cut", "setting_style"}
ATTACHMENT_REFERENCE = "customer attached image; match its visual design"


def settle_attachment_visuals(
    specification: dict[str, Any], missing: list[str], image_attached: bool
) -> dict[str, Any]:
    """An attached image settles visual choices even when its machine reading fails.

    The machine does not invent what it could not see. The bench follows the
    customer's image, while origin, carat, metal, and every nonvisual fact
    remain questions unless the words or file answer them.
    """
    if not isinstance(specification, dict) or not image_attached:
        return specification
    result = dict(specification)
    pieces = [dict(piece) if isinstance(piece, dict) else {} for piece in result.get("pieces") or []]
    settled = False
    for name in missing:
        index, field = split_field_name(name)
        if field not in ATTACHMENT_VISUAL_FIELDS:
            continue
        if index is None:
            if not _present(result.get(field)):
                result[field] = "jeweler's choice"
                settled = True
        elif 0 <= index < len(pieces) and not _present(pieces[index].get(field)):
            pieces[index][field] = "jeweler's choice"
            settled = True
    if pieces:
        result["pieces"] = pieces
    if settled and not _present(result.get("reference_images")):
        result["reference_images"] = ATTACHMENT_REFERENCE
    return result


def settle_earring_style(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """Earrings are studs, hoops, or drops: the piece's own name or the customer's words say which (the owner, 9 September 2026).

    Live: "a pair of earrings" with a halo and no photo rendered as leverback
    drops, a fair guess from those words and a wrong one for the halo studs
    the customer had in mind. The kind is a preference a customer can give,
    so it is settled from their words here and asked, plainly, when they
    have not said (spec_gate).
    """
    if not isinstance(specification, dict):
        return specification
    raw = specification.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        return specification
    piece = str(specification.get("piece_type") or "").lower()
    if "earring" not in piece or _present(specification.get("earring_style")):
        return specification
    style = earring_style_in_words(piece) or earring_style_in_words(own_words) or earring_style_in_words(photo_reading(specification))
    return {**specification, "earring_style": style} if style else specification


def settle_setting_style(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """A setting the customer names in their own words is theirs, never the jeweler's choice.

    Live (9 September 2026): "earrings like these... not sure the halo size"
    priced with "jeweler's choice: setting style" on the card and "my own
    choice of setting style" in the estimate, while the customer had written
    "halo". The reading is the model's; a setting word in the customer's own
    sentence is a rule, and it fills the setting when the reading left it
    empty or handed it to the jeweler. One piece only: a set's pieces keep
    their own words.
    """
    if not isinstance(specification, dict):
        return specification
    raw = specification.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        return specification
    current = str(specification.get("setting_style") or "").strip().lower()
    if current and current != "jeweler's choice":
        return specification
    import spec_gate  # local import: spec_gate imports this module

    if not spec_gate.has_stones(specification):
        return specification
    # The photo's reading stands in when the words say nothing: the email states it back and the customer corrects.
    named = setting_in_words(own_words) or (setting_in_words(photo_reading(specification)) if not current else None)
    if not named:
        return specification
    return {**specification, "setting_style": named}


PAIR_WORDS = ("earring", "cufflink", "cuff link", "stud", "hoop", "huggie")
_BASIS_EACH_RE = re.compile(r"(?i)\b(?:each|apiece|a ?piece|per (?:earring|ear|stone|side|piece)|every (?:earring|stone))\b")
_BASIS_TOTAL_RE = re.compile(r"(?i)\b(?:total|tcw|ctw|t\.?c\.?w\.?|combined|for (?:the|both) (?:pair|earrings)|the pair|pair total|between (?:the )?two|altogether|in all)\b")


def is_pair(specification: dict[str, Any]) -> bool:
    """Earrings, cufflinks, studs, hoops: two of the piece, so a carat may be each or the pair's total."""
    piece = str((specification or {}).get("piece_type") or "").lower()
    return any(w in piece for w in PAIR_WORDS)


def carat_basis_in_words(own_words: str) -> str | None:
    """'each' or 'total' when the customer's words say which, else None."""
    text = str(own_words or "")
    each, total = bool(_BASIS_EACH_RE.search(text)), bool(_BASIS_TOTAL_RE.search(text))
    if each and not total:
        return "each"
    if total and not each:
        return "total"
    return None


def settle_carat_basis(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """For a pair with a stated carat, the customer's words decide whether it is each stone or the pair's total.

    The owner's rule (9 September 2026): "2.5 ct rounds" for earrings is
    either 2.5 ct per stone or 2.5 ct for the pair, and the price differs
    by half, so the desk settles it from the words or asks; it applies to
    pairs only.
    """
    if not isinstance(specification, dict) or not is_pair(specification):
        return specification
    if specification.get("stone_carat") in (None, "", 0) or str(specification.get("stone_carat")).lower() == "jeweler's choice":
        return specification
    current = str(specification.get("stone_carat_basis") or "").lower()
    if current in ("each", "total"):
        return specification
    found = carat_basis_in_words(own_words)
    if found:
        return {**specification, "stone_carat_basis": found}
    return specification


_OWNER_CARAT_RE = re.compile(r"(?i)(?<![\d.])(\d+(?:\.\d+)?)\s*(?:ct|cts|carat|carats)\b")
_OWNER_KARAT_RE = re.compile(r"(?i)\b(\d{1,2})\s*(?:k|kt|karat)\b")
_OWNER_SIZE_RE = re.compile(r"(?i)\bsize\s*(\d{1,2}(?:\.\d)?)\b")
_OWNER_STONES = ("emerald", "sapphire", "ruby", "diamond", "moissanite", "aquamarine", "morganite", "tanzanite", "amethyst", "opal", "pearl", "garnet", "topaz")


def owner_facts_in_words(text: str, specification: dict[str, Any] | None = None) -> dict[str, Any]:
    """The facts an owner states in a reply ("5 ct total", "18k rose gold", "size 7", "natural"), as specification keys.

    Deterministic and narrow: the owner's word is the last word on a fact, so
    only what is plainly written is taken. For a pair, a carat with no
    "each" is the pair's total (owners price in totals).
    """
    words = str(text or "")
    found: dict[str, Any] = {}
    m = _OWNER_CARAT_RE.search(words)
    if m:
        found["stone_carat"] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
        basis = carat_basis_in_words(words)
        if is_pair(specification or {}):
            found["stone_carat_basis"] = basis or "total"
        elif basis:
            found["stone_carat_basis"] = basis
    m = _OWNER_KARAT_RE.search(words)
    if m:
        found["metal_karat"] = int(m.group(1))
    lowered = words.lower()
    for colour in ("yellow", "white", "rose"):
        if re.search(r"\b" + colour + r"\b", lowered):
            found["metal_color"] = colour
            if "gold" in lowered:
                found["metal"] = f"{colour} gold"
            break
    if "platinum" in lowered:
        found["metal"] = "platinum"
    if re.search(r"\blab[- ]?grown\b|\blab\b", lowered):
        found["stone_origin"] = "lab-grown"
    elif re.search(r"\bnatural\b", lowered):
        found["stone_origin"] = "natural"
    shape = re.search(r"\b(oval|round|cushion|pear|emerald[- ]cut|princess|marquise|radiant|asscher|heart|baguette|trillion)\b", lowered)
    if shape:
        found["stone_shape"] = shape.group(1)
    size = stone_size_in_words(words)
    if size:
        found["stone_dimensions"] = size
    setting = setting_in_words(words)
    if setting:
        found["setting_style"] = setting
    m = _OWNER_SIZE_RE.search(words)
    if m:
        found["finger_size"] = m.group(1)
    for stone in _OWNER_STONES:
        if re.search(r"\b" + stone + r"s?\b", lowered):
            found["stone_type"] = stone
            break
    return found


def revive(root: Path, estimate_id: str, note: str = "") -> dict[str, Any]:
    """A dormant record comes back to awaiting_specs so the desk can read and price it again (the owner asked)."""
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") != "dormant":
            raise ValueError(f"estimate is {record.get('status')}, not dormant; nothing to revive")
        rejected = record.setdefault("rejected_approval_bindings", [])
        old_binding = record.get("approval_binding_hash")
        if isinstance(old_binding, str) and old_binding not in rejected:
            rejected.append(old_binding)
        record["revived"] = [*(record.get("revived") or []), {**(record.get("retirement") or {}), "note": str(note or "")[:200],
                                                               "revived_at": datetime.now(timezone.utc).isoformat()}]
        record.pop("retirement", None)
        record.pop("approval_binding_hash", None)
        record["status"] = "awaiting_specs"
        record["missing_required_fields"] = []
        record["revision"] = int(record.get("revision") or 0) + 1
        write_object(path, record)
        return record


def owner_changes_specification(root: Path, estimate_id: str, changes: dict[str, Any], question_id: str) -> dict[str, Any]:
    """The owner changed a fact after passing on the price: the record reopens for pricing with the owner's facts.

    The rejected binding becomes history, the specification takes the owner's
    words, and the record goes back to awaiting_specs so the tick re-prices
    it from the record (the 9 September 2026 rule: an owner's "5 ct" re-prices,
    it never stands the desk down).
    """
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") not in ("pending_approval", "awaiting_specs"):
            raise ValueError(f"estimate is {record.get('status')}; a rejected price card is needed to change the facts")
        rejected = record.setdefault("rejected_approval_bindings", [])
        old_binding = record.get("approval_binding_hash")
        if isinstance(old_binding, str) and old_binding not in rejected:
            rejected.append(old_binding)
        specification = dict(record.get("specification") or {})
        specification.update(changes)
        record["specification"] = specification
        record["owner_spec_changes"] = [*(record.get("owner_spec_changes") or []),
                                         {"changes": changes, "question_id": question_id, "at": datetime.now(timezone.utc).isoformat()}]
        record["status"] = "awaiting_specs"
        record["missing_required_fields"] = []
        record["revision"] = int(record.get("revision") or 0) + 1
        record.pop("approval_binding_hash", None)
        write_object(path, record)
        return record


def settle_grades(specification: dict[str, Any]) -> dict[str, Any]:
    """A stone's color and clarity are the jeweler's choice unless the customer stated them (the owner, 9 September 2026).

    Never asked: the price card and the estimate email say what the jeweler
    chose, and the customer's own words replace it the moment they give one.
    """
    if not isinstance(specification, dict):
        return specification
    import spec_gate  # local import: spec_gate imports this module

    def fill(piece: dict[str, Any]) -> dict[str, Any]:
        if not spec_gate.has_stones(piece) or customer_supplies_stone(piece):
            return piece
        out = dict(piece)
        for key in GRADE_KEYS:
            if not spec_gate.present(out.get(key)):
                out[key] = "jeweler's choice"
        return out

    raw = specification.get("pieces")
    if isinstance(raw, list) and len(raw) > 1:
        pieces = [fill(dict(p)) if isinstance(p, dict) and spec_gate.has_stones({**{k: v for k, v in specification.items() if k != "pieces"}, **p}) else p
                  for p in raw]
        return {**specification, "pieces": pieces}
    return fill(specification)


def leaves_to_jeweler(own_words: str) -> bool:
    """The customer leaves an asked detail to the jeweler ("I don't know", "you decide", "just a reference")."""
    return bool(LEAVES_TO_JEWELER_RE.search(str(own_words or "")))


def settle_left_to_jeweler(specification: dict[str, Any], record: dict[str, Any], own_words: str) -> dict[str, Any]:
    """A reply that leaves the last ask to the jeweler fills those details as the jeweler's choice, in code.

    Live (8 September 2026): the desk asked stud earrings' "dimensions", the
    customer wrote "I don't know. This is just a reference." and the owner
    was asked whether to skip. The follow-up itself promised "say so and I
    will suggest what usually looks best", so the desk keeps that promise:
    every detail of the last ask the reply still leaves open becomes the
    jeweler's choice; reading checks and the piece itself are never chosen.
    """
    if not isinstance(specification, dict) or not leaves_to_jeweler(own_words):
        return specification
    asked = [str(f) for f in ((record or {}).get("missing_required_fields") or [])]
    if not asked:
        return specification
    settled = dict(specification)
    for field in asked:
        if field.startswith("confirm."):
            continue
        index, bare = split_field_name(field)
        if bare == "piece_type":
            continue
        if index is None:
            if not _present(settled.get(bare)):
                settled[bare] = "jeweler's choice"
        else:
            pieces = [dict(p) if isinstance(p, dict) else {} for p in settled.get("pieces") or []]
            if index < len(pieces) and not _present(pieces[index].get(bare)):
                pieces[index][bare] = "jeweler's choice"
                settled["pieces"] = pieces
    return settled


def _present(value: Any) -> bool:
    return value not in (None, "", [], {}) and not (isinstance(value, str) and value.strip().lower() in
                                                     ("", "n/a", "unknown", "unspecified", "tbd", "none"))


def accepts_a_time(own_words: str) -> bool:
    """The customer picks or accepts an offered time, in their own words."""
    return bool(ACCEPTS_TIME_RE.search(str(own_words or "")))


_SENTENCE_RE = re.compile(r"(?<=[.?!])\s+|\n+")


# A day and a clock time stated flat: "Monday the 21st at 11am please", "Monday at 11am."
_DAY_AND_TIME_RE = re.compile(r"(?i)\b(?:" + _DAY + r"|the \d{1,2}(?:st|nd|rd|th)|(?:january|february|march|april|may|june|july|august|"
                              r"september|october|november|december) \d{1,2})\b[^.?!\n]{0,30}\b(?:at|@|around)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b")


def scheduling_sentences(own_words: str) -> list[str]:
    """The customer's sentences that ask for a meeting, propose a day and time, or state one; deadlines are not visits.

    Live (9 September 2026): "Monday the 21st at 11am please." with the
    details was priced with no meeting card, because the day and time were
    stated, not asked. A day with a clock time in one sentence is a time to
    meet unless the sentence is about a deadline or a delivery.
    """
    found: list[str] = []
    for sentence in _SENTENCE_RE.split(str(own_words or "")):
        sentence = sentence.strip()
        if not sentence or NOT_A_VISIT_RE.search(sentence):
            continue
        if MEETING_RE.search(sentence) or PROPOSAL_RE.search(sentence) or _DAY_AND_TIME_RE.search(sentence):
            found.append(sentence)
    return found


_ASKS_FOR_TIME_RE = re.compile(r"(?i)^\W*(?:can|could|would|will|is it possible|any chance|how about|what about|are you (?:free|available|open))\b")


def asks_to_reschedule(own_words: str, booked: bool = False) -> bool:
    """The customer is moving a meeting they already have ("something came up... can we do Friday at 4pm?").

    With a meeting booked, a question that names a time is the meeting
    moving, whatever the words around it (live, 9 September 2026: "My
    mistake, I have the wrong day. Can we do Monday at 3pm?" got the
    questionnaire). A statement of the booked time ("See you Friday at 3pm")
    is not.
    """
    text = str(own_words or "")
    sentences = scheduling_sentences(text)
    if not sentences:
        # "Sorry, I meant to say Tuesday": a bare day with reschedule words moves the booked meeting (live, 9 Sep).
        return booked and bool(RESCHEDULE_RE.search(text)) and bool(day_sentences(text))
    if RESCHEDULE_RE.search(text):
        return True
    return booked and any(s.rstrip().endswith("?") or _ASKS_FOR_TIME_RE.match(s) for s in sentences)


def moved_to_same_time(own_words: str, record: dict[str, Any] | None) -> str | None:
    """'Sorry, I meant to say Tuesday' with a meeting booked: that day, at the booked time, as a phrase the resolver reads."""
    booked = (record or {}).get("appointment_booked") if isinstance((record or {}).get("appointment_booked"), dict) else None
    text = str(own_words or "")
    if not booked or not booked.get("confirmed_start") or scheduling_sentences(text) or not RESCHEDULE_RE.search(text):
        return None
    days = day_sentences(text)
    match = _DAY_NAMED_RE.search(days[0]) if days else None
    if not match:
        return None
    try:
        start = datetime.fromisoformat(str(booked["confirmed_start"]))
    except ValueError:
        return None
    clock = start.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")
    return f"{match.group(0)} at {clock}"


# A day named without a clock time: "Monday", "next Tuesday", "the 14th", "next week", "tomorrow morning".
_DAY_NAMED_RE = re.compile(
    r"(?i)\b(?:(?:next|this) (?:week|month)|" + _DAY + r"|(?:the )?\d{1,2}(?:st|nd|rd|th)\b|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2})\b"
)


def day_sentences(own_words: str) -> list[str]:
    """The customer's sentences that name a day or a stretch of days; deadlines and deliveries are not visits."""
    found: list[str] = []
    for sentence in _SENTENCE_RE.split(str(own_words or "")):
        sentence = sentence.strip()
        if not sentence or NOT_A_VISIT_RE.search(sentence):
            continue
        if _DAY_NAMED_RE.search(sentence):
            found.append(sentence)
    return found


def times_were_offered(record: dict[str, Any] | None) -> bool:
    """The shop has offered this customer times: a day named in their reply answers that offer."""
    offers = (record or {}).get("times_offered")
    return isinstance(offers, list) and bool(offers)


def settle_scheduling_intent(specification: dict[str, Any], own_words: str, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """The customer's own words decide a meeting request when the reading missed it.

    Live (8 September 2026): "Something came up for Saturday... Any chance
    we can do Friday at 4pm?" was read as an estimate request and answered
    with the questionnaire. The reading is the model's; whether the message
    asks to meet is a rule, so the sentences that ask are the intent. After
    the shop has offered times, a reply that names a day without a clock
    time ("Monday the 14th would be best") answers that offer (live, 9
    September 2026: such a reply was priced with no meeting card at all).
    """
    if not isinstance(specification, dict) or present_value(specification.get("scheduling_intent")):
        return specification
    sentences = scheduling_sentences(own_words)
    booked = isinstance((record or {}).get("appointment_booked"), dict)
    if not sentences and (times_were_offered(record) or (booked and RESCHEDULE_RE.search(str(own_words or "")))):
        sentences = day_sentences(own_words)
    if not sentences:
        return specification
    return {**specification, "scheduling_intent": " ".join(sentences)[:300]}


def drop_carried_scheduling_intent(specification: dict[str, Any], record: dict[str, Any], own_words: str) -> dict[str, Any]:
    """A meeting asked for in an earlier email is not asked for again by a reply about something else.

    Live (8 September 2026, twice): the desk offered times; the customer
    replied "Before I come in, is there any way I can get a ballpark
    estimate?" and the desk offered times again. The reading merges the
    thread, so the first email's request rode along, re-worded. A reply keeps
    a scheduling intent only when its own words ask for a meeting, propose a
    day and time, pick or accept an offered time, or, once times were
    offered, name a day.
    """
    if not isinstance(specification, dict) or not present_value(specification.get("scheduling_intent")):
        return specification
    if scheduling_sentences(own_words) or accepts_a_time(own_words):
        return specification
    if times_were_offered(record) and day_sentences(own_words):
        return specification
    if asks_to_reschedule(own_words, booked=isinstance((record or {}).get("appointment_booked"), dict)):
        return specification  # "Sorry, I meant to say Tuesday": the booked meeting moving (live, 9 September 2026)
    # The reading of the thread can re-word the earlier request, so its text is
    # never compared: on a reply, only the reply's own words carry a meeting.
    return {k: v for k, v in specification.items() if k != "scheduling_intent"}


def present_value(value: Any) -> bool:
    return bool(value) and not (isinstance(value, str) and not value.strip())


def settle_center_stone(specification: dict[str, Any], own_words: str) -> dict[str, Any]:
    """The customer's own words decide whether there is a center stone; the reading does not get to guess.

    "0.2 ct diamond eternity band in the middle, channel set" is a band of
    small stones totalling 0.2 ct. A reading that calls that a center stone
    makes the desk ask for its carat and cut (live, 8 September 2026). When
    the words name an eternity, channel-set, pave, or all-around design and
    never a center, main, or feature stone, every piece's `center_stone` is
    "no" and the stated carat stays as the total.
    """
    text = " " + " ".join(str(own_words or "").lower().split()) + " "
    if not any(w in text for w in BAND_STONE_WORDS) or any(w in text for w in CENTER_STONE_WORDS):
        return specification
    if not isinstance(specification, dict):
        return specification

    def settle(piece: dict[str, Any]) -> dict[str, Any]:
        if not piece.get("stone_type") and not piece.get("stone_carat") and not piece.get("accent_stones"):
            return piece
        return {**piece, "center_stone": "no"}

    if is_multi_piece(specification):
        return {**specification, "pieces": [settle(p) if isinstance(p, dict) else p for p in specification["pieces"]]}
    return settle(specification)


def known_specification(record: dict[str, Any]) -> dict[str, Any] | None:
    """Everything the desk knows about this customer's piece: the current specification, the quoted one beneath it.

    After a reopen the current specification may be a thin re-read that a
    review already recorded (live, 8 September 2026: the change read on
    4.13.6 became the record's specification, the quoted facts lived only
    in the archived estimate, and 4.13.7's merge read against the thin
    one). The quoted specification is what the customer confirmed, so it
    wins where the two differ (a thin re-read that called the rose band
    yellow does not stand); the current one fills what the estimate never
    had (the engraving the change asked for). The newest reading, merged
    on top of this by `merge_known_facts`, wins over both.
    """
    current = record.get("specification") if isinstance(record.get("specification"), dict) else None
    history = record.get("estimate_history") or []
    quoted = history[-1].get("specification") if history and isinstance(history[-1], dict) else None
    if not isinstance(quoted, dict) or not quoted or record.get("reopened_for") == "second_piece":
        return current or None
    if not current:
        return quoted
    merged = merge_known_facts({"specification": current}, quoted)
    return merged or current


def merge_known_facts(record: dict[str, Any], specification: dict[str, Any]) -> dict[str, Any]:
    """The reading of a new message keeps every fact the record already holds; the new words win.

    Used for a continuing record that is not a second-piece reopen: a
    follow-up answered from a new thread ("same"), or a change to a quoted
    piece ("change"). Flat against flat fills the gaps; pieces against
    pieces fill by position; one flat piece against several known pieces is
    matched by piece type and then colour, size, metal, karat, and merged
    into that piece with the others unchanged; a flat reading with no piece
    type against several pieces is a change to all of them.
    """
    if not isinstance(specification, dict) or record.get("reopened_for") == "second_piece":
        return specification
    known = known_specification(record) if "estimate_history" in record else record.get("specification")
    if not isinstance(known, dict) or not known:
        return specification

    def fill(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
        merged = dict(new)
        for key, value in old.items():
            if key != "pieces" and merged.get(key) in (None, "", []) and value not in (None, "", []):
                merged[key] = value
        return merged

    known_pieces = [{k: v for k, v in piece.items() if k != "pieces"} for piece in pieces_of(known)]
    raw = specification.get("pieces")
    new_pieces = [dict(p) for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    if len(known_pieces) <= 1 and not new_pieces:
        return fill(specification, known_pieces[0] if known_pieces else known)
    if new_pieces and len(new_pieces) >= len(known_pieces):
        merged = [fill(piece, old) for piece, old in zip(new_pieces, known_pieces)] + new_pieces[len(known_pieces):]
        return {**fill({k: v for k, v in specification.items() if k != "pieces"}, {k: v for k, v in known.items() if k != "pieces"}),
                "pieces": merged}
    if new_pieces:
        return specification  # fewer pieces than known and more than one: the gate decides
    flat = {k: v for k, v in specification.items() if k != "pieces"}
    kind = str(flat.get("piece_type") or "").strip().lower()
    if not kind:
        # No piece named: the change applies to every piece ("make both 14k").
        return {**known, "pieces": [{**piece, **{k: v for k, v in flat.items() if v not in (None, "", [])}} for piece in known_pieces]}
    candidates = [i for i, piece in enumerate(known_pieces) if str(piece.get("piece_type") or "").strip().lower() == kind]
    for key in CHANGE_MATCH_KEYS:
        if len(candidates) <= 1:
            break
        wanted = str(flat.get(key) or "").strip().lower()
        if wanted:
            narrowed = [i for i in candidates if str(known_pieces[i].get(key) or "").strip().lower() == wanted]
            candidates = narrowed or candidates
    if len(candidates) != 1:
        return specification  # ambiguous: the gate asks
    index = candidates[0]
    pieces = [fill(flat, piece) if i == index else piece for i, piece in enumerate(known_pieces)]
    return {**{k: v for k, v in known.items() if k != "pieces"}, "pieces": pieces}


def rejected_bindings(record: dict[str, Any]) -> set[str]:
    """Binding hashes of price cards the owner rejected before naming a price."""
    value = record.get("rejected_approval_bindings")
    return {v for v in value if isinstance(v, str)} if isinstance(value, list) else set()


def record_owner_price(
    root: Path, estimate_id: str, price: Any, question_id: str, shop_profile: dict[str, Any] | None
) -> dict[str, Any]:
    """The owner rejected the price card and named the price to file (WORKFLOW.md 6.4).

    The cost sheet stays as priced; the customer price, the profit, and the
    binding change. The rejected request becomes history, the price carries
    its provenance, and the record stays pending_approval for the fresh card.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not 1 <= float(price) <= 10_000_000:
        raise ValueError("the owner's price must be a number between 1 and 10,000,000")
    price = round(float(price), 2)
    if not isinstance(question_id, str) or not question_id:
        raise ValueError("question_id is required")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record.get("status") != "pending_approval":
            raise ValueError(f"estimate is {record.get('status')}, not pending_approval; no price to re-file")
        sheet = record.get("internal_cost_sheet")
        if not isinstance(sheet, dict):
            raise ValueError("record has no cost sheet to re-file from")
        hard = float(sheet["hard_cost_total"])
        if price <= hard:
            raise ValueError(f"the price must be above the hard cost of ${hard:,.2f}")
        previous = float(record["proposed_price"])
        expected = None
        if isinstance(shop_profile, dict):
            try:
                expected = pricing_model.quote_price(hard, shop_profile.get("pricing"))
            except (TypeError, ValueError):
                expected = None
        sheet = dict(sheet)
        sheet["customer_price"] = price
        state = {
            "estimate_id": record["estimate_id"],
            "route": record["route"],
            "specification": record.get("specification"),
            "proposed_price": price,
            "internal_cost_sheet": sheet,
        }
        binding = approval_guard.binding_hash(state)
        # The rejected request stays untouched (approval_requests is append-only);
        # its binding is listed as rejected so every reader passes over it.
        rejected = record.setdefault("rejected_approval_bindings", [])
        if not isinstance(rejected, list):
            raise ValueError("rejected_approval_bindings must be an array")
        old_binding = record.get("approval_binding_hash")
        if isinstance(old_binding, str) and old_binding not in rejected:
            rejected.append(old_binding)
        record["owner_price"] = {
            "price": price,
            "previous_price": previous,
            "desk_price": expected,
            "hard_cost_total": hard,
            "margin": round((price - hard) / price, 4),
            "expected_margin": round((expected - hard) / expected, 4) if expected else None,
            "question_id": question_id,
            "set_at": datetime.now(timezone.utc).isoformat(),
        }
        record["proposed_price"] = price
        record["internal_cost_sheet"] = sheet
        record["approval_binding_hash"] = binding
        write_object(path, record)
        return record


def retire(
    root: Path, estimate_id: str, reason: str, note: str | None = None
) -> dict[str, Any]:
    """Retire one estimate the shop will not pursue, without touching anything else.

    A record created in error otherwise has nowhere to go: the only path that
    removed one was a full customer-state reset, which also deletes good work
    and rewinds the discovery watermark. This is deliberately narrow. It moves a
    single non-terminal record to `dormant`, records why, and changes no claim,
    queue item, watermark, or other record.
    """
    if reason not in RETIREMENT_REASONS:
        raise ValueError(
            "reason must be one of: " + ", ".join(sorted(RETIREMENT_REASONS))
        )
    if note is not None and (not isinstance(note, str) or len(note) > 400):
        raise ValueError("note must be text of at most 400 characters")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        status = record["status"]
        if status not in route_ownership.ACTIVE_STATUSES:
            raise ValueError(
                f"estimate is already terminal with status '{status}'; "
                "nothing to retire"
            )
        # Retiring a sent estimate would leave the customer holding a price the
        # shop has quietly abandoned. That needs a customer message, not a
        # status change, so it is out of scope here.
        if status in {"estimate_sent", "appointment_booked", "approved"}:
            raise ValueError(
                f"estimate is '{status}' and the customer has already been told; "
                "resolve it with the customer rather than retiring the record"
            )
        entry: dict[str, Any] = {
            "reason": reason,
            "retired_at": datetime.now(timezone.utc).isoformat(),
            "previous_status": status,
        }
        if note:
            entry["note"] = note
        record["retirement"] = entry
        record["status"] = "dormant"
        write_object(path, record)
        return record


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
                or existing.get("binding_hash") in rejected_bindings(record)
            ):
                continue
            comparable = {k: v for k, v in existing.items() if k not in {"requested_at", "owner_set"}}
            if comparable == evidence:
                return record
            raise ValueError("conflicting approval request for source message")
        owner_price = record.get("owner_price") if isinstance(record.get("owner_price"), dict) else None
        if record["status"] == "pending_approval" and owner_price and \
                abs(float(owner_price.get("price", -1)) - float(evidence["proposed_price"])) <= 0.005 and \
                evidence["binding_hash"] == record.get("approval_binding_hash"):
            # A fresh card at the price the owner named (WORKFLOW.md 6.4): the
            # rejected request stays as history, this one is the live one.
            evidence["owner_set"] = True
        elif record["status"] != "awaiting_specs":
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
        try:
            import ledger  # local import: the ledger never imports the record helpers

            ledger.quote(Path(root).parent, estimate_id, record.get("specification") or {}, record.get("route", {}).get("gmail_message_id"))
        except Exception:  # noqa: BLE001 - the ledger is derived state; the send stands
            pass
        return record


def record_times_offered(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    options: list[dict[str, Any]],
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Append one same-thread offer of meeting times to the customer."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    provider_message_id = validate_provider_id(provider_response.get("id"), "provider response id")
    if not options:
        raise ValueError("an offer needs at least one time")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] not in {"estimate_sent", "appointment_booked", "approved", "awaiting_specs"}:
            raise ValueError("offering times requires an open estimate")
        offers = record.setdefault("times_offered", [])
        if not isinstance(offers, list):
            raise ValueError("times_offered must be an array")
        evidence = {
            "source_message_id_sha256": sha256_text(source_message_id),
            "provider_message_id": provider_message_id,
            "options": [{"start": o["start"], "end": o["end"], "label": o.get("label", "")} for o in options],
        }
        for existing in offers:
            if isinstance(existing, dict) and existing.get("provider_message_id") == provider_message_id:
                return record
        evidence["offered_at"] = datetime.now(timezone.utc).isoformat()
        offers.append(evidence)
        write_object(path, record)
        return record


def record_appointment_approval_requested(
    root: Path,
    estimate_id: str,
    source_message_id: str,
    approval: dict[str, Any],
) -> dict[str, Any]:
    """Append durable evidence for one post-estimate appointment approval."""
    source_message_id = validate_provider_id(source_message_id, "source_message_id")
    required = {
        "schema_version",
        "action_type",
        "estimate_id",
        "source_message_id",
        "customer_email",
        "thread_id",
        "requested_times",
        "calendar_availability",
    }
    # The owner's card also carries the piece, the proposed time, and a note
    # about availability; they are display fields, not binding ones.
    optional = {"piece", "proposed_time", "availability_note", "execute", "execute_on_reject", "reject_code", "outside_hours", "hours",
                "ask_for", "ask_intro", "meeting_kind", "phone", "behavior_check", "filed_title"}  # behavior_check is shadow evidence shown on the card
    if (
        not isinstance(approval, dict)
        or not required <= set(approval)
        or not set(approval) <= required | optional
    ):
        raise ValueError("appointment approval contains missing or unsupported fields")
    if approval.get("schema_version") != 1:
        raise ValueError("unsupported appointment approval schema_version")
    if approval.get("action_type") not in {"appointment_booking", "appointment_offer"}:
        raise ValueError("appointment approval action_type must be appointment_booking or appointment_offer")
    if approval.get("estimate_id") != estimate_id:
        raise ValueError("appointment approval estimate_id does not match")
    if approval.get("source_message_id") != source_message_id:
        raise ValueError("appointment approval source_message_id does not match")
    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] not in {"estimate_sent", "appointment_booked", "approved", "awaiting_specs"}:
            raise ValueError("appointment approval requires an open estimate")
        route = record["route"]
        if approval.get("customer_email") != route["recipient"]:
            raise ValueError("appointment approval customer email does not match route")
        if approval.get("thread_id") != route["thread_id"]:
            raise ValueError("appointment approval thread_id does not match route")
        evidence = {
            "status": "pending_approval",
            "source_message_id_sha256": sha256_text(source_message_id),
            "approval_sha256": canonical_sha256(approval),
        }
        requests = record.setdefault("appointment_approval_requests", [])
        if not isinstance(requests, list):
            raise ValueError("appointment_approval_requests must be an array")
        for existing in requests:
            if not isinstance(existing, dict):
                raise ValueError(
                    "appointment_approval_requests contains invalid evidence"
                )
            if (
                existing.get("source_message_id_sha256")
                != evidence["source_message_id_sha256"]
            ):
                continue
            comparable = dict(existing)
            comparable.pop("requested_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting appointment approval for source message")
        evidence["requested_at"] = datetime.now(timezone.utc).isoformat()
        requests.append(evidence)
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
    if not image_paths or len(image_paths) > 4:
        raise ValueError("rendering delivery requires one to four images")
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
            for item in reversed(reviews)
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

    appointment_approval = next(
        (
            item
            for item in record.get("appointment_approval_requests", [])
            if isinstance(item, dict)
            and item.get("source_message_id_sha256") == source_hash
            and item.get("status") == "pending_approval"
        ),
        None,
    )
    if appointment_approval is not None:
        action_key = f"appointment_approval:{record['estimate_id']}:{message_id}"
        action = claim_state.get("external_actions", {}).get(action_key)
        if (
            not isinstance(action, dict)
            or action.get("category") != "approval_request"
            or action.get("status") != "sent"
        ):
            raise ValueError(
                "appointment request lacks a sent claimed approval request"
            )
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
    if outcome == "post_estimate_continuation":
        # A reply after the estimate that needs no card (thanks, an acceptance, a cancellation): the
        # review recorded the decision and its intents, and that is the durable outcome (simulation,
        # 9 September 2026: such claims failed here and retried until they stuck).
        return
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
        actions = claim_state.get("external_actions", {}) or {}
        # A re-priced record (a design change, an owner's changed fact) journals its card under ":rev<n>".
        candidates = [v for k, v in actions.items() if k == action_key or k.startswith(action_key + ":rev")]
        if not any(
            isinstance(action, dict) and action.get("category") == "approval_request" and action.get("status") == "sent"
            for action in candidates
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
    thread_review.add_argument("--shop-profile", type=Path, required=True)
    thread_review.add_argument(
        "--record-root", type=Path, default=default_record_root()
    )
    thread_review.add_argument("--output", type=Path)
    retire_parser = sub.add_parser("retire")
    retire_parser.add_argument("--estimate-id", required=True)
    retire_parser.add_argument("--reason", required=True)
    retire_parser.add_argument("--note")
    retire_parser.add_argument(
        "--record-root", type=Path, default=default_record_root()
    )
    retire_parser.add_argument("--output", type=Path)
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
                args.record_root,
                args.estimate_id,
                read_object(args.snapshot),
                read_object(args.shop_profile),
            )
            if args.output is not None:
                write_object(args.output, record)
        elif args.command == "retire":
            record = retire(
                args.record_root, args.estimate_id, args.reason, args.note
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
