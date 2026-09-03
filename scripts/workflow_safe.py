#!/usr/bin/env python3
"""High-level fail-closed workflow actions that never copy claim tokens by hand."""

from __future__ import annotations

import argparse
import json
import re
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import approval_guard
import activation_binding
import customer_content_guard
import estimate_record
import gmail_reply
import gmail_safe
import gmail_text
import spot_price
import inbox_claim
import gateway_token
import gmail_classify
import gmail_route
import inbox_monitor
import kolo_safe
import owner_questions
import cost_components
import route_ownership


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_private(path: Path, value: Any) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def mirror_record(record: dict[str, Any], path: Path) -> None:
    write_private(path, record)
    subprocess.run(
        kolo_safe.build_record_upsert(
            "skill.jewelry_estimate", record["estimate_id"], path, record["status"]
        ),
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def finish_processed(
    monitor_root: Path,
    claim_root: Path,
    record_root: Path,
    message_id: str,
) -> None:
    token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    inbox_claim.advance_phase(claim_root, message_id, token, "work_persisted")
    inbox_claim.advance_phase(claim_root, message_id, token, "ready_to_finalize")
    inbox_monitor.finalize_item(
        monitor_root,
        message_id,
        claim_root,
        token,
        "processed",
        record_root=record_root,
    )


def send_spec_followup(args: argparse.Namespace) -> dict[str, Any]:
    route = read_object(args.route)
    body = args.body.read_text(encoding="utf-8")
    payload = gmail_reply.build_reply(route, body)
    write_private(args.gmail_payload, payload)
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"customer_reply:{args.estimate_id}:{args.message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
    )
    if args.initiating:
        record = estimate_record.record_spec_gate_sent(
            args.record_root, args.estimate_id, body, receipt
        )
    else:
        record = estimate_record.record_followup_sent(
            args.record_root, args.estimate_id, args.message_id, body, receipt
        )
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


def request_approval(args: argparse.Namespace) -> dict[str, Any]:
    candidate = read_object(args.current_state)
    current = estimate_record.prepare_approval_state(
        args.record_root,
        args.estimate_id,
        args.message_id,
        candidate,
        read_object(args.shop_profile),
    )
    approval_existed = args.approval_request.exists()
    if approval_existed:
        approval = read_object(args.approval_request)
    else:
        approval = approval_guard.build_request(current)
        estimate_record.validate_approval_request(
            args.record_root, args.estimate_id, args.message_id, approval
        )
        write_private(args.approval_request, approval)
    if approval_existed:
        estimate_record.validate_approval_request(
            args.record_root, args.estimate_id, args.message_id, approval
        )
    approver = activation_binding.load(
        activation_binding.binding_path(args.monitor_root)
    )
    kolo_safe.request_approval_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"approval_request:{args.estimate_id}:{args.message_id}",
        args.estimate_id,
        args.approval_request,
        approver["session_key"],
    )
    record = estimate_record.record_approval_requested(
        args.record_root, args.estimate_id, args.message_id, approval
    )
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


def _appointment_approval_details(
    record: dict[str, Any],
    message_id: str,
    intent: dict[str, Any],
) -> dict[str, Any]:
    if not {"requested_times", "calendar_availability"} <= set(intent) or not set(intent) <= {
        "requested_times", "calendar_availability", "availability_note"
    }:
        raise ValueError("appointment intent contains missing or unsupported fields")
    requested_times = intent["requested_times"]
    if (
        not isinstance(requested_times, list)
        or len(requested_times) > 5
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 160
            or any(character in value for character in "\r\n")
            for value in requested_times
        )
    ):
        raise ValueError("requested_times must contain at most five short strings")
    availability = intent["calendar_availability"]
    if not isinstance(availability, list) or len(availability) > 5:
        raise ValueError("calendar_availability must contain at most five slots")
    normalized_slots = []
    for index, slot in enumerate(availability):
        if not isinstance(slot, dict) or set(slot) != {"start", "end", "label"}:
            raise ValueError(
                f"calendar_availability[{index}] must contain start, end, and label"
            )
        if any(
            not isinstance(slot[field], str)
            or not slot[field]
            or len(slot[field]) > 160
            or any(character in slot[field] for character in "\r\n")
            for field in ("start", "end", "label")
        ):
            raise ValueError(f"calendar_availability[{index}] contains invalid text")
        normalized_slots.append(dict(slot))
    route_ownership.validate_record(record)
    if record["status"] not in {"estimate_sent", "appointment_booked", "approved"}:
        raise ValueError("appointment approval requires a sent estimate")
    route = record["route"]
    details = {
        "schema_version": 1,
        "action_type": "appointment_booking",
        "estimate_id": record["estimate_id"],
        "source_message_id": message_id,
        "customer_email": route["recipient"],
        "thread_id": route["thread_id"],
        "requested_times": [value.strip() for value in requested_times],
        "calendar_availability": normalized_slots,
        "piece": owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate",
    }
    if normalized_slots:
        details["proposed_time"] = dict(normalized_slots[0])
    note = intent.get("availability_note")
    if isinstance(note, str) and note.strip():
        details["availability_note"] = note.strip()[:160]
    return details


def request_appointment_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Create a durable appointment approval and optionally finalize the claim."""
    record, _decision = estimate_record.post_estimate_decision(
        args.record_root,
        args.estimate_id,
        args.message_id,
        "appointment_request",
    )
    intent = read_object(args.appointment_intent)
    if args.appointment_approval.exists():
        approval = read_object(args.appointment_approval)
        expected = _appointment_approval_details(record, args.message_id, intent)
        if approval != expected:
            raise ValueError("existing appointment approval binding changed")
    else:
        approval = _appointment_approval_details(record, args.message_id, intent)
        write_private(args.appointment_approval, approval)
    approver = activation_binding.load(
        activation_binding.binding_path(args.monitor_root)
    )
    kolo_safe.request_appointment_approval_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"appointment_approval:{args.estimate_id}:{args.message_id}",
        args.estimate_id,
        args.appointment_approval,
        approver["session_key"],
    )
    record = estimate_record.record_appointment_approval_requested(
        args.record_root, args.estimate_id, args.message_id, approval
    )
    mirror_record(record, args.record_output)
    if not args.defer_finalize_for_rendering:
        finish_processed(
            args.monitor_root, args.claim_root, args.record_root, args.message_id
        )
    return record


def send_approved_estimate(args: argparse.Namespace) -> dict[str, Any]:
    current = (
        read_object(args.current_state)
        if args.current_state is not None
        else estimate_record.current_approval_state(args.record_root, args.estimate_id)
    )
    approved = read_object(args.approved)
    message_id = args.message_id or estimate_record.approval_source_message_id(
        args.record_root, args.estimate_id
    )
    valid, errors = approval_guard.verify_execution(approved, current)
    if not valid:
        raise ValueError("approval verification failed: " + "; ".join(errors))
    route = current.get("route")
    if not isinstance(route, dict):
        raise ValueError("current state route must be an object")
    body = args.body.read_text(encoding="utf-8")
    customer_content_guard.validate_approved_price(
        body, approved["owner_approved_price"]
    )
    payload = gmail_reply.build_reply(route, body)
    write_private(args.gmail_payload, payload)
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        message_id,
        inbox_claim.authoritative_claim_token(
            args.claim_root, message_id, allow_processed=True
        ),
        f"approved_estimate:{args.estimate_id}:{message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
        allow_processed_claim=True,
    )
    record = estimate_record.record_estimate_sent(
        args.record_root,
        args.estimate_id,
        message_id,
        approved,
        current,
        receipt,
    )
    mirror_record(record, args.record_output)
    return record


def send_rendering(args: argparse.Namespace) -> dict[str, Any]:
    record, _decision = estimate_record.post_estimate_decision(
        args.record_root,
        args.estimate_id,
        args.message_id,
        "rendering_request",
    )
    route_ownership.validate_record(record)
    if record["status"] not in {"estimate_sent", "appointment_booked", "approved"}:
        raise ValueError("rendering delivery requires a sent estimate")
    route = record.get("route")
    if not isinstance(route, dict):
        raise ValueError("estimate record route must be an object")
    body = args.body.read_text(encoding="utf-8")
    customer_content_guard.validate_customer_text(body)
    payload = gmail_reply.build_reply(route, body, args.images)
    write_private(args.gmail_payload, payload)
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"customer_rendering:{args.estimate_id}:{args.message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
    )
    record = estimate_record.record_rendering_sent(
        args.record_root,
        args.estimate_id,
        args.message_id,
        body,
        args.images,
        receipt,
    )
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


NOT_CUSTOMER_MAIL = {
    "auto_reply",
    "calendar_event",
    "automated_notification",
    "bulk_mail",
    "internal_sender",
}
NOT_AN_INQUIRY_REASONS = {
    "not_a_quote_request",
    "vendor_or_marketing",
    "personal_or_internal",
    "unrelated",
}


def not_an_inquiry(args: argparse.Namespace) -> dict[str, Any]:
    """Close a claimed message that turned out not to be a quote request.

    The header classifier cannot read; it only removes machine mail. A human
    can still write to the shop about anything, and intake will have opened a
    record for it before anyone read it. Once the thread review shows the
    message asks for no estimate, this retires that record, mirrors it, and
    finalizes the claim, so the record never shadows later mail from the same
    sender. It refuses anything that has moved past the initial record.
    """
    if args.reason not in NOT_AN_INQUIRY_REASONS:
        raise ValueError("reason must be one of: " + ", ".join(sorted(NOT_AN_INQUIRY_REASONS)))
    record = read_object(estimate_record.record_path(args.record_root, args.estimate_id))
    route = record.get("route") or {}
    if route.get("gmail_message_id") != args.message_id:
        raise ValueError(
            "only the message that opened the record can close it as not an inquiry"
        )
    if record.get("status") != "awaiting_specs":
        raise ValueError(
            f"record is '{record.get('status')}', not a fresh awaiting_specs record"
        )
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    retired = estimate_record.retire(
        args.record_root, args.estimate_id, "not_an_inquiry", f"triage: {args.reason}"
    )
    mirror_record(retired, args.record_output)
    kolo_safe.complete_claimed(args.monitor_root, args.claim_root, args.message_id, token)
    return {
        "message_id": args.message_id,
        "estimate_id": args.estimate_id,
        "status": retired["status"],
        "reason": args.reason,
        "outcome": "not_an_inquiry_completed",
        "next_action": "done",
    }


def resolve_review_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Close a review the owner approved in the approval queue, then report it.

    This is the only thing the chat session may do with an approved
    manual-review brief: one command, no reading of customer mail, and the
    brief is marked executed so the queue reflects reality.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", args.review_key or ""):
        raise ValueError("review_key must be a lowercase SHA-256 value")
    open_keys = {r["review_key"] for r in inbox_monitor.list_manual_reviews(args.monitor_root)}
    item = None
    if args.review_key in open_keys:
        item = inbox_monitor.resolve_manual_review(args.monitor_root, args.review_key)
        outcome = "resolved"
    else:
        outcome = "already_resolved"
    result = {"action_type": "manual_review", "review_key": args.review_key, "outcome": outcome}
    kolo_safe.run_command(
        kolo_safe.build_update_brief(args.brief_id, "executed", result),
        runner=getattr(args, "runner", subprocess.run),
    )
    return {**result, "brief_id": args.brief_id, "review_status": (item or {}).get("review_status", "resolved")}


def worker_start(args: argparse.Namespace) -> dict[str, Any]:
    """Hand a worker job the intake result for the one claim leased to it.

    A worker is told which message it owns and nothing else. This is the only
    thing it may run first: it proves the claim is still processing and still
    leased, then returns the intake result the watcher wrote, with the exact
    work paths, so the worker never chooses paths or repeats intake.
    """
    state = inbox_claim.read_state(inbox_claim.claim_path(args.claim_root, args.message_id))
    if state.get("status") != "processing":
        raise ValueError(f"claim is {state.get('status')}, not processing; nothing to do")
    if not inbox_claim.recovery_lease_active(state):
        raise ValueError("claim lease has expired; the watcher will recover it")
    paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
    result = read_object(Path(paths["work_dir"]) / "intake-result.json")
    if result.get("message_id") != args.message_id or result.get("next_action") != "review_thread":
        raise ValueError("intake result does not describe a delegated review for this message")
    # The thread as plain text, so the worker never opens the Gmail JSON.
    try:
        thread = read_object(Path(paths["gmail_thread"]))
        profile = read_object(args.monitor_root.resolve().parent / "shop-profile.json")
        mailbox = (profile.get("shop") or {}).get("outbound_mailbox")
        result["thread"] = gmail_text.thread_digest(thread, args.message_id, mailbox)
    except (OSError, ValueError, json.JSONDecodeError):
        result["thread"] = None
    # Dead-spot guard. A previous worker may have reviewed the thread and then
    # died before, or just after, sending the specification follow-up. Tell
    # this worker exactly where to resume, or finish the claim when the send
    # already happened, so the customer is neither left unasked nor asked twice.
    record_root = getattr(args, "record_root", None) or args.monitor_root.resolve().parent / "records"
    estimate_id = result.get("estimate_id")
    record_file = estimate_record.record_path(record_root, estimate_id) if estimate_id else None
    if record_file is not None and record_file.exists():
        record = estimate_record.read_object(record_file)
        pending = estimate_record.pending_followup(record, args.message_id)
        if pending is not None:
            result["resume"] = pending
        elif record.get("status") == "awaiting_specs" and estimate_record.followup_sent(
            record, args.message_id
        ):
            review = next(
                (
                    item
                    for item in record.get("thread_reviews", [])
                    if isinstance(item, dict)
                    and item.get("source_message_id_sha256")
                    == estimate_record.sha256_text(args.message_id)
                ),
                None,
            )
            if review is not None and review.get("outcome") == "awaiting_specs":
                finish_processed(args.monitor_root, args.claim_root, record_root, args.message_id)
                result["outcome"] = "followup_already_sent"
                result["next_action"] = "done"
    return result


def intake(args: argparse.Namespace) -> dict[str, Any]:
    """Classify, route, decide ownership, and record one claimed message.

    These eight steps always run in the same order and never involve a
    judgment call, yet each one used to cost the run a model turn. Bundling
    them removes those turns. Every step is idempotent, so a resumed claim
    can run this again safely: the route is rebuilt, phases only advance,
    the initial record is retry-stable, and the owner alert deduplicates.
    """
    profile = read_object(args.shop_profile)
    mailbox = (profile.get("shop") or {}).get("outbound_mailbox")
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise ValueError("shop profile is missing shop.outbound_mailbox")
    paths = inbox_monitor.prepare_claim_work(
        args.monitor_root, args.claim_root, args.message_id
    )
    message = read_object(Path(paths["gmail_message"]))
    thread = read_object(Path(paths["gmail_thread"]))
    if message.get("id") != args.message_id:
        raise ValueError("fetched Gmail message does not match the claimed message")
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    classification = gmail_classify.classify(message, mailbox)
    result: dict[str, Any] = {
        "message_id": args.message_id,
        "classification": classification["classification"],
        "classification_reason": classification["reason_code"],
        "work_paths": paths,
    }
    if classification["classification"] in NOT_CUSTOMER_MAIL:
        # Nothing a customer wrote is in here: an auto-reply, a calendar
        # invitation, a machine notification, a newsletter, or a coworker.
        # It is closed without a record, an alert, or a reply, so it can never
        # become a phantom estimate that later mail gets matched against.
        kolo_safe.complete_claimed(args.monitor_root, args.claim_root, args.message_id, token)
        result.update(
            {"outcome": f"{classification['classification']}_completed", "next_action": "done"}
        )
        return result
    if classification["classification"] != "customer_or_uncertain":
        reason = "uncorrelated_dsn" if classification["classification"] == "dsn_candidate" else "uncertain_classification"
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, reason
        )
        result.update({"outcome": "manual_review", "reason_code": reason, "next_action": "done"})
        return result

    route = gmail_route.build_route(message, mailbox)
    write_private(Path(paths["route"]), route)
    inbox_claim.advance_phase(args.claim_root, args.message_id, token, "routed")
    candidates = estimate_record.lookup_thread(args.record_root, route)
    write_private(Path(paths["candidate_records"]), candidates)
    messages = thread.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("fetched Gmail thread has no messages")
    decision = route_ownership.decide(route, candidates, args.claim_root, len(messages))
    result.update({
        "decision": decision["decision"],
        "reason_code": decision.get("reason_code"),
        "thread_message_count": len(messages),
    })
    if (
        decision["decision"] == "manual_review"
        and decision.get("reason_code") == "identity_has_active_estimate_on_another_thread"
        and getattr(args, "force_new_inquiry", False)
    ):
        # The owner answered "new piece": treat this thread as a fresh inquiry.
        decision = {"decision": "new_inquiry", "reason_code": "owner_said_new_piece"}
        result["decision"] = "new_inquiry"
        result["reason_code"] = "owner_said_new_piece"
    if (
        decision["decision"] == "manual_review"
        and decision.get("reason_code") == "identity_has_active_estimate_on_another_thread"
    ):
        asked = ask_same_sender(args, token, route, decision.get("estimate_id"), message)
        result.update(asked)
        return result
    if decision["decision"] in {"manual_review", "owned_manual_review"}:
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, decision["reason_code"]
        )
        result.update({"outcome": "manual_review", "estimate_id": decision.get("estimate_id"), "next_action": "done"})
        return result
    if decision["decision"] == "new_inquiry":
        internal_date = message.get("internalDate")
        try:
            inbound_ms = int(internal_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("fetched Gmail message lacks a numeric internalDate") from exc
        record = estimate_record.create_initial_record(args.record_root, route, inbound_ms)
        mirror_record(record, Path(paths["inquiry_record"]))
    elif decision["decision"] == "owned":
        record = read_object(
            estimate_record.record_path(args.record_root, decision["estimate_id"])
        )
    else:
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, "missing_thread_ownership"
        )
        result.update({"outcome": "manual_review", "reason_code": "missing_thread_ownership", "next_action": "done"})
        return result
    inbox_claim.advance_phase(args.claim_root, args.message_id, token, "ownership_confirmed")
    estimate_id = record["estimate_id"]
    kolo_safe.notify_owner_claimed(
        args.claim_root,
        args.message_id,
        token,
        f"customer_replied:{estimate_id}:{args.message_id}",
        estimate_id,
        "customer-replied",
    )
    result.update({
        "outcome": "ownership_confirmed",
        "estimate_id": estimate_id,
        "record_status": record["status"],
        "next_action": "review_thread",
    })
    return result


SENT_STATUSES = {"estimate_sent", "appointment_booked", "approved"}


def _work_paths(args: argparse.Namespace) -> dict[str, str]:
    return inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)


def review_thread(args: argparse.Namespace) -> dict[str, Any]:
    """Record the worker's review and run every deterministic step after it.

    The worker supplies only its judgment: before an estimate, the merged
    specification and the missing required fields; after one, the
    post-estimate artifact. Everything else that used to cost a model round
    trip happens here: the thread ids come from the fetched thread, the
    review is persisted, and then either the follow-up is prepared, the
    post-estimate decision is finalized, the missing rate is asked, or the
    cost skeleton is built (including the spot price) ready for `price`.
    """
    review = read_object(args.review)
    paths = _work_paths(args)
    thread = read_object(Path(paths["gmail_thread"]))
    digest = gmail_text.thread_digest(thread, args.message_id)
    profile = read_object(args.shop_profile)
    record = estimate_record.read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    post_estimate = record.get("status") in SENT_STATUSES
    snapshot: dict[str, Any] = {
        "thread_id": digest["thread_id"],
        "source_message_id": args.message_id,
        "message_ids": digest["message_ids"],
        "missing_required_fields": [],
    }
    if post_estimate:
        if "post_estimate_artifact" not in review:
            raise ValueError("a post-estimate review needs post_estimate_artifact")
        snapshot["post_estimate_artifact"] = review["post_estimate_artifact"]
    else:
        if "specification" not in review or "missing_required_fields" not in review:
            raise ValueError("a pre-estimate review needs specification and missing_required_fields")
        snapshot["specification"] = review["specification"]
        snapshot["missing_required_fields"] = review["missing_required_fields"]
    record = estimate_record.record_thread_review(
        args.record_root, args.estimate_id, snapshot, profile
    )
    write_private(Path(paths["current_record"]), record)
    if post_estimate:
        decision = finalize_post_estimate(
            argparse.Namespace(
                monitor_root=args.monitor_root,
                claim_root=args.claim_root,
                record_root=args.record_root,
                message_id=args.message_id,
                estimate_id=args.estimate_id,
                record_output=Path(paths["current_record"]),
            )
        )
        return {"outcome": "post_estimate_reviewed", **decision, "next": decision["next_action"]}

    pending = estimate_record.pending_followup(record, args.message_id)
    if pending is not None:
        return {
            "outcome": "specification_incomplete",
            "next": "send_spec_followup",
            "missing_required_fields": pending["missing_required_fields"],
            "initiating": pending["initiating"],
            "customer_reply": paths["customer_reply"],
            "route": paths["route"],
        }

    pricing = profile.get("pricing") or {}
    spot_evidence = None
    spot = pricing.get("spot_metal") or {}
    if isinstance(spot, dict) and spot.get("enabled"):
        metal = cost_components.extract_metal(record.get("specification")).get("metal")
        if metal:
            try:
                spot_evidence = spot_price.get_prices(
                    args.monitor_root.resolve().parent / "spot-cache.json",
                    spot.get("provider"),
                    spot.get("refresh_frequency"),
                    [metal],
                    spot.get("currency", "USD"),
                    spot.get("unit", "gram"),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                kolo_safe.manual_review_claimed(
                    args.monitor_root, args.claim_root, args.message_id, None,
                    "spot_price_unavailable", runner=getattr(args, "runner", subprocess.run),
                )
                return {"outcome": "manual_review", "reason_code": "spot_price_unavailable",
                        "error": str(exc)[:200], "next": "done"}
            write_private(Path(paths["work_dir"]) / "spot-evidence.json", spot_evidence)
    skeleton = cost_components.prepare(record, profile, spot_evidence)
    if skeleton.get("unresolved"):
        try:
            asked = ask_missing_rate(args)
        except ValueError as exc:
            kolo_safe.manual_review_claimed(
                args.monitor_root, args.claim_root, args.message_id, None,
                "invalid_cost_components", runner=getattr(args, "runner", subprocess.run),
            )
            return {"outcome": "manual_review", "reason_code": "invalid_cost_components",
                    "error": str(exc)[:200], "next": "done"}
        return {**asked, "next": "done"}
    write_private(Path(paths["work_dir"]) / "cost-skeleton.json", skeleton)
    return {
        "outcome": "specification_complete",
        "next": "price",
        "fill": skeleton["fill"],
        "fee_catalog": [item["rate_key"] for item in skeleton.get("fee_catalog", [])],
        "stone_catalog": [item["rate_key"] for item in skeleton.get("stone_catalog", [])],
        "typical_finished_weights": pricing.get("typical_finished_weights") or {},
    }


def price(args: argparse.Namespace) -> dict[str, Any]:
    """Fill the skeleton with the worker's quantities, finalize, request approval.

    The worker's whole contribution is a few numbers: finished grams, bench
    hours, a missing center carat, and which fee and accent-stone catalog
    entries apply. Rates, unit costs, the price, the binding, the brief, the
    record, the mirror, and the claim finish are all deterministic.
    """
    paths = _work_paths(args)
    skeleton_path = Path(paths["work_dir"]) / "cost-skeleton.json"
    skeleton = read_object(skeleton_path)
    profile = read_object(args.shop_profile)
    lines = skeleton["cost_components"]
    for value, label in ((args.finished_grams, "finished grams"), (args.bench_hours, "bench hours")):
        if value is None or value <= 0:
            raise ValueError(f"{label} must be a positive number")
    lines["metal_lines"][0]["quantity_grams"] = float(args.finished_grams)
    lines["labor_lines"][0]["hours"] = float(args.bench_hours)
    if lines["stone_lines"]:
        if lines["stone_lines"][0].get("quantity") is None:
            if args.center_carat is None or args.center_carat <= 0:
                raise ValueError("center carat is required for this piece")
            lines["stone_lines"][0]["quantity"] = float(args.center_carat)
    fee_catalog = {item["rate_key"]: item for item in skeleton.get("fee_catalog", [])}
    for key in args.fees or []:
        if key not in fee_catalog:
            raise ValueError(f"unknown fee '{key}'; choose from the fee catalog")
        lines["other_hard_cost_lines"].append(dict(fee_catalog[key]))
    stone_catalog = {item["rate_key"]: item for item in skeleton.get("stone_catalog", [])}
    for spec in args.accents or []:
        key, _, quantity = spec.partition(":")
        if key not in stone_catalog:
            raise ValueError(f"unknown accent stone '{key}'; choose from the stone catalog")
        try:
            carats = float(quantity)
        except ValueError as exc:
            raise ValueError("accent stones are written as rate_key:total_carats") from exc
        if carats <= 0:
            raise ValueError("accent stone carats must be positive")
        lines["stone_lines"].append({
            "stone": key.replace("_", " "),
            "rate_key": key,
            "quantity": carats,
            "unit_cost": float(stone_catalog[key]["rate"]),
        })
    write_private(skeleton_path, skeleton)
    state = cost_components.finalize(skeleton, profile)
    write_private(Path(paths["current_state"]), state)
    record = request_approval(
        argparse.Namespace(
            monitor_root=args.monitor_root,
            claim_root=args.claim_root,
            record_root=args.record_root,
            message_id=args.message_id,
            estimate_id=args.estimate_id,
            current_state=Path(paths["current_state"]),
            approval_request=Path(paths["approval_request"]),
            shop_profile=args.shop_profile,
            record_output=Path(paths["current_record"]),
        )
    )
    return {
        "outcome": "approval_requested",
        "proposed_price": state.get("proposed_price"),
        "cost_components": state.get("cost_components"),
        "record_status": record.get("status"),
        "next": "done",
    }


def _customer_name(monitor_root: Path, claim_root: Path, message_id: str) -> str:
    headers = kolo_safe.claimed_message_headers(monitor_root, claim_root, message_id)
    sender = headers.get("From") or ""
    return kolo_safe._sender_display(sender) if sender else "A customer"


def ask_same_sender(
    args: argparse.Namespace,
    token: str,
    route: dict[str, Any],
    existing_estimate_id: str | None,
    message: dict[str, Any],
) -> dict[str, Any]:
    """Same customer, new thread: ask the owner, in words, same piece or new."""
    existing = {}
    if existing_estimate_id:
        try:
            existing = estimate_record.read_object(
                estimate_record.record_path(args.record_root, existing_estimate_id)
            )
        except (OSError, ValueError):
            existing = {}
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    old_subject = " ".join(((existing.get("route") or {}).get("original_subject") or "").split())[:80]
    old_words = owner_questions.summary_of_piece(existing.get("specification")) if existing.get("specification") else "a piece"
    new_subject = " ".join((route.get("original_subject") or "").split())[:80]
    text = (
        f"{who} wrote in a new email thread (\"{new_subject}\") but already has an open estimate "
        f"with us for {old_words}"
        + (f' ("{old_subject}", {existing.get("status", "open").replace("_", " ")})' if old_subject else "")
        + ". Is this the same piece, or a new one? Reply \"same\" and I will leave that thread to you, "
        "or \"new\" and I will quote it as a separate estimate."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "same_sender", existing_estimate_id or "jed-0000000000000000", args.message_id, text,
        {"existing_estimate_id": existing_estimate_id, "new_subject": new_subject},
    )
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "same_sender_question")
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "delivery": question["delivery"]["status"],
        "estimate_id": existing_estimate_id,
        "next_action": "done",
    }


def ask_unclear_reply(args: argparse.Namespace, record: dict[str, Any], outcome: str) -> dict[str, Any]:
    """A reply after an estimate the desk could not read: ask the owner what it meant."""
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    snippet = ""
    try:
        paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
        message = read_object(Path(paths["gmail_message"]))
        snippet = " ".join(gmail_text.body_text(message, limit=600).split())[:240]
    except (OSError, ValueError, json.JSONDecodeError):
        snippet = ""
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate"
    why = "it may change the design" if outcome == "design_change_detected" else "I could not tell what they mean"
    text = (
        f"{who} replied on the estimate for {piece}"
        + (f': "{snippet}"' if snippet else "")
        + f". I did not act because {why}. Is this a second piece to quote, a change to this one, "
        "are they accepting the estimate, or will you handle it? Reply \"second piece\", \"change\", "
        "\"accepts\", or \"I will handle it\"."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "unclear_reply", args.estimate_id, args.message_id, text, {"outcome": outcome},
    )
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "unclear_reply_question")
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "delivery": question["delivery"]["status"],
        "next_action": "done",
    }


def ask_missing_rate(args: argparse.Namespace) -> dict[str, Any]:
    """Ask the owner for the one rate pricing lacks, and park this claim.

    WORKFLOW.md 6.10: a missing rate is not an error and not a review. The
    question goes to the owner's channel in plain words, the claim waits as
    awaiting_owner with its work directory intact, and answer_question()
    resumes it once the owner replies. Everything here is idempotent: a
    repeat neither re-asks nor re-sends.
    """
    claim_token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    record = estimate_record.read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    queue_item = inbox_monitor.load_queue_item(args.monitor_root, args.message_id)
    if queue_item["thread_id"] != record["route"]["thread_id"]:
        raise ValueError("the claimed message is not on this estimate's thread")
    profile = read_object(args.shop_profile)
    missing = cost_components.missing_rates(record, profile)
    if not missing:
        raise ValueError("no rate is missing for this specification; price it instead")
    rate = missing[0]
    headers = kolo_safe.claimed_message_headers(args.monitor_root, args.claim_root, args.message_id)
    customer = kolo_safe._sender_display(headers.get("From", "")) if headers.get("From") else None
    root = owner_questions.questions_root(args.monitor_root)
    created, question = owner_questions.create_missing_rate(
        root,
        args.estimate_id,
        args.message_id,
        rate,
        customer,
        owner_questions.summary_of_piece(record.get("specification")),
    )
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(
        args.monitor_root, args.message_id, args.claim_root, claim_token, "missing_rate"
    )
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "created": created,
        "delivery": question["delivery"]["status"],
        "rate_kind": rate["rate_kind"],
        "rate_key": rate["suggested_key"],
        "estimate_id": args.estimate_id,
        "next_action": "done",
    }


def open_questions(args: argparse.Namespace) -> list[dict[str, Any]]:
    """What the desk is still waiting on from the owner, oldest first."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    root = owner_questions.questions_root(p["monitor_root"])
    return [
        {
            "question_id": q["question_id"],
            "reference": owner_questions.reference(q["question_id"]),
            "estimate_id": q["estimate_id"],
            "kind": q["kind"],
            "asked_at": q["asked_at"],
            "delivery": q["delivery"]["status"],
            "text": q["text"],
        }
        for q in owner_questions.list_questions(root, "open")
    ]


def answer_question(args: argparse.Namespace) -> dict[str, Any]:
    """Take the owner's reply, save the rate, and resume the parked inquiry.

    Runs in the main Kolo session, which is where the owner's answer arrives.
    The reply is read for exactly one number; the number goes on the rate
    card with provenance; the parked claim reopens under a worker lease and
    a one-shot worker is started to price the piece. The price still goes
    through the normal approval, so a misread number is caught there.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    workspace = args.workspace.resolve()
    p = inbox_watcher.paths_for(workspace)
    root = owner_questions.questions_root(p["monitor_root"])
    question = _question_to_answer(args, p, root)
    if question["status"] == "answered":
        if question["kind"] in owner_questions.DECISION_KINDS and _claim_still_waiting(p, question):
            # An earlier run recorded the answer and then failed before the
            # inquiry moved; replay the recorded answer rather than refusing.
            args.answer = question["answer"]["text"]
            replayed = answer_decision(args, workspace, p, root, question)
            replayed["replayed"] = True
            return replayed
        return {
            "outcome": "already_answered",
            "question_id": question["question_id"],
            "answer": question.get("answer"),
        }
    if question["kind"] in owner_questions.DECISION_KINDS:
        return answer_decision(args, workspace, p, root, question)
    if question["kind"] != "missing_rate":
        raise ValueError("unsupported question kind")
    # Refuse before writing anything if the estimate is not in the state the
    # question left it in. A hand-edited or already-priced record must be
    # repaired or handled deliberately, not turned into a misleading review.
    record = estimate_record.read_object(
        estimate_record.record_path(p["record_root"], question["estimate_id"])
    )
    try:
        route_ownership.validate_record(record)
    except ValueError as exc:
        raise ValueError(
            f"estimate record {question['estimate_id']} is invalid ({exc}); "
            "repair it before answering"
        ) from exc
    if record.get("status") != "awaiting_specs":
        raise ValueError(
            f"estimate record {question['estimate_id']} is {record.get('status')}, "
            "not awaiting_specs; nothing to price"
        )
    value = owner_questions.parse_amount(args.answer)
    question = owner_questions.record_answer(root, question, args.answer, value)
    owner_questions.save_rate(
        p["shop_profile"],
        question["rate"]["rate_kind"],
        question["rate"]["rate_key"],
        value,
        owner_questions.answer_provenance(question),
    )
    message_id = question["gmail_message_id"]
    estimate_id = question["estimate_id"]
    import cron_config  # local import keeps module import order unchanged

    reopened = inbox_monitor.reopen_item(
        p["monitor_root"], message_id, p["claim_root"], cron_config.WORKER_LEASE_SECONDS
    )
    work_dir = Path(reopened["work_paths"]["work_dir"])
    if not Path(reopened["work_paths"]["gmail_message"]).exists():
        import gmail_fetch  # local import; only needed when the work file was cleaned up

        gmail_fetch.fetch_claimed(
            p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token()
        )
    write_private(
        work_dir / "intake-result.json",
        {
            "message_id": message_id,
            "estimate_id": estimate_id,
            "outcome": "owner_answered",
            "question_id": question["question_id"],
            "next_action": "review_thread",
            "work_paths": reopened["work_paths"],
        },
    )
    result = {
        "outcome": "answered",
        "question_id": question["question_id"],
        "estimate_id": estimate_id,
        "rate_kind": question["rate"]["rate_kind"],
        "rate_key": question["rate"]["rate_key"],
        "value": value,
        "worker_job_id": None,
    }
    try:
        result["worker_job_id"] = inbox_watcher.spawn_worker(
            workspace,
            args.base_dir.resolve(),
            "",
            args.openclaw or inbox_watcher.default_openclaw(),
            message_id,
            estimate_id,
            str(work_dir),
            runner=getattr(args, "runner", subprocess.run),
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        # The claim is processing under a lease; when it lapses the watcher's
        # stale reconciler resumes it and the next tick starts a worker.
        result["worker_error"] = str(exc)[:200]
    return result


def _question_to_answer(args: argparse.Namespace, p: dict[str, Path], root: Path) -> dict[str, Any]:
    """The named question, the only open one, or an answered one whose inquiry is still waiting."""
    if args.question:
        return owner_questions.find(root, args.question)
    try:
        return owner_questions.only_open(root)
    except ValueError:
        answered = [
            q for q in owner_questions.list_questions(root, "answered")
            if q["kind"] in owner_questions.DECISION_KINDS and _claim_still_waiting(p, q)
        ]
        if len(answered) == 1:
            return answered[0]
        raise


def _claim_still_waiting(p: dict[str, Path], question: dict[str, Any]) -> bool:
    """True while the answered question's inquiry has not moved past the answer.

    Parked means the answer never took; processing without an intake result
    means an earlier attempt reopened the claim and failed before intake.
    Anything else has moved on, and replaying would double the work.
    """
    try:
        state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], question["gmail_message_id"]))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if state.get("status") == "awaiting_owner":
        return True
    if state.get("status") != "processing":
        return False
    work_dir = p["monitor_root"].resolve().parent / "work" / inbox_claim.claim_key(question["gmail_message_id"])
    return not (work_dir / "intake-result.json").exists()


def _resume_parked_claim(p: dict[str, Path], message_id: str) -> dict[str, Any]:
    """Lease the parked claim for the answer; carry on if an earlier attempt already reopened it."""
    import cron_config  # local import keeps module import order unchanged

    state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
    if state.get("status") == "awaiting_owner":
        return inbox_monitor.reopen_item(p["monitor_root"], message_id, p["claim_root"], cron_config.WORKER_LEASE_SECONDS)
    if state.get("status") == "processing":
        token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
        state = inbox_claim.delegate(p["claim_root"], message_id, token, cron_config.WORKER_LEASE_SECONDS)
        queue_item = inbox_monitor.sync_claim(p["monitor_root"], message_id, {"acquired": True, **state})
        return {
            "queue_item": queue_item,
            "claim": {"acquired": True, "resumed": True, **state},
            "work_paths": inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id),
        }
    raise ValueError(
        f"claim is {state.get('status')}; the inquiry is no longer waiting on this answer"
    )


def _close_parked_claim(p: dict[str, Path], message_id: str, reason: str) -> None:
    """Finish a parked claim on the owner's word: terminal, reviewed, no card."""
    _resume_parked_claim(p, message_id)
    token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    inbox_claim.finish(p["claim_root"], message_id, token, "manual_review", reason)
    inbox_monitor.reconcile_terminal(p["monitor_root"], message_id, p["claim_root"])
    inbox_monitor.cleanup_claim_work(p["monitor_root"], message_id)
    item = inbox_monitor.load_queue_item(p["monitor_root"], message_id)
    inbox_monitor.resolve_manual_review(p["monitor_root"], item["gmail_message_id_sha256"])


def answer_decision(
    args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path, question: dict[str, Any]
) -> dict[str, Any]:
    """Apply a fixed-outcome answer: same piece or new; what an unclear reply meant."""
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module

    outcome = owner_questions.match_option(question, args.answer)
    message_id = question["gmail_message_id"]
    result: dict[str, Any] = {
        "outcome": "answered", "question_id": question["question_id"], "kind": question["kind"], "decision": outcome,
    }
    if question["kind"] == "same_sender" and outcome == "new":
        # Lease the claim first: if that fails nothing is recorded, so the
        # same command can simply be run again.
        reopened = _resume_parked_claim(p, message_id)
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        if not Path(reopened["work_paths"]["gmail_message"]).exists():
            import gmail_fetch  # local import; only needed when the work file was cleaned up

            gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
        intake_result = intake(argparse.Namespace(
            monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
            message_id=message_id, shop_profile=p["shop_profile"], force_new_inquiry=True,
        ))
        result["intake"] = {k: intake_result.get(k) for k in ("decision", "estimate_id", "next_action", "outcome")}
        if intake_result.get("next_action") != "review_thread":
            return result
        work_dir = Path(reopened["work_paths"]["work_dir"])
        write_private(work_dir / "intake-result.json", intake_result)
        # Intake advances the phase journal, which drops the reopen lease; lease
        # the claim again before any worker or inline run touches it.
        inbox_claim.delegate(
            p["claim_root"], message_id,
            inbox_claim.authoritative_claim_token(p["claim_root"], message_id),
            cron_config.WORKER_LEASE_SECONDS,
        )
        import pipeline  # local import: pipeline imports this module

        switch = pipeline.settings(workspace / "estimate-desk")
        if switch.get("inline"):
            done = pipeline.process_claim(
                workspace, args.base_dir.resolve(), message_id, intake_result,
                model=switch.get("model"), judge_runner=getattr(args, "judge_runner", subprocess.run),
                command_runner=getattr(args, "runner", subprocess.run), openclaw=args.openclaw or inbox_watcher.default_openclaw(),
            )
            result["pipeline"] = done.get("outcome")
            if done.get("outcome") != "needs_worker":
                return result
        result["worker_job_id"] = inbox_watcher.spawn_worker(
            workspace, args.base_dir.resolve(), "", args.openclaw or inbox_watcher.default_openclaw(),
            message_id, intake_result["estimate_id"], str(work_dir), runner=getattr(args, "runner", subprocess.run),
            branch=cron_config.worker_branch(intake_result.get("record_status")),
        )
        return result
    # Every other outcome: the owner takes the conversation from here.
    reason = f"owner_decided_{outcome}"
    _close_parked_claim(p, message_id, reason)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    result["claim"] = reason
    return result


RENDERING_NOTE = (
    "Attached are visual illustrations of the design direction we discussed. The "
    "written specification and the final design you approve control the finished "
    "piece.\n\nIf you would like an adjustment to the look, reply here and tell me "
    "what you would like changed.\n"
)


def _rendering_images(paths: dict[str, str]) -> list[Path]:
    return [Path(paths[key]) for key in ("rendering_image_1", "rendering_image_2") if Path(paths[key]).exists()]


def _sha256_file(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def request_rendering_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Show the owner the views, file the approval, park the claim."""
    paths = _work_paths(args)
    images = _rendering_images(paths)
    if not images:
        raise ValueError("no materialized rendering images to approve")
    record = estimate_record.read_object(estimate_record.record_path(args.record_root, args.estimate_id))
    route_ownership.validate_record(record)
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate"
    details = {
        "schema_version": 1,
        "action_type": "send_rendering",
        "estimate_id": args.estimate_id,
        "gmail_message_id": args.message_id,
        "thread_id": record["route"]["thread_id"],
        "customer_email": record["route"]["recipient"],
        "piece": piece,
        "images": [{"slot": index, "sha256": _sha256_file(image)} for index, image in enumerate(images, start=1)],
    }
    approval_path = Path(paths["work_dir"]) / "rendering-approval.json"
    if approval_path.exists() and read_object(approval_path) != details:
        raise ValueError("existing rendering approval binding changed")
    write_private(approval_path, details)
    runner = getattr(args, "runner", subprocess.run)
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    for index, image in enumerate(images, start=1):
        kolo_safe.send_owner_preview(
            args.monitor_root,
            f"Rendering {index} of {len(images)} for {customer}'s {piece}. An approval card follows; "
            "approve it to email these to the customer.",
            image, runner=runner,
        )
    approver = activation_binding.load(activation_binding.binding_path(args.monitor_root))
    kolo_safe.request_rendering_approval_claimed(
        args.claim_root, args.message_id, token,
        f"rendering_approval:{args.estimate_id}:{args.message_id}",
        args.estimate_id, approval_path, approver["session_key"], runner=runner,
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "rendering_approval")
    return {"outcome": "rendering_approval_requested", "images": len(images), "next": "done"}


def send_approved_rendering(args: argparse.Namespace) -> dict[str, Any]:
    """The owner approved: verify the very images they saw, then send them."""
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    reopened = inbox_monitor.reopen_item(p["monitor_root"], args.message_id, p["claim_root"], cron_config.WORKER_LEASE_SECONDS)
    paths = reopened["work_paths"]
    approval = read_object(Path(paths["work_dir"]) / "rendering-approval.json")
    if approval.get("estimate_id") != args.estimate_id or approval.get("gmail_message_id") != args.message_id:
        raise ValueError("rendering approval does not match this estimate and message")
    images = _rendering_images(paths)
    expected = {item["slot"]: item["sha256"] for item in approval.get("images", [])}
    if len(images) != len(expected) or any(_sha256_file(img) != expected.get(i) for i, img in enumerate(images, start=1)):
        raise ValueError("rendering images changed since the owner approved them")
    body = Path(paths["customer_reply"])
    body.write_text(RENDERING_NOTE, encoding="utf-8")
    record = send_rendering(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        message_id=args.message_id, estimate_id=args.estimate_id, body=body, images=images,
        gmail_payload=Path(paths["gmail_payload"]), provider_response=Path(paths["gmail_provider_response"]),
        record_output=Path(paths["current_record"]),
    ))
    result = {"outcome": "rendering_sent", "images": len(images), "record_status": record.get("status")}
    if getattr(args, "brief_id", None):
        kolo_safe.run_command(
            kolo_safe.build_update_brief(args.brief_id, "executed", result),
            runner=getattr(args, "runner", subprocess.run),
        )
    return result


def reject_rendering(args: argparse.Namespace) -> dict[str, Any]:
    """The owner held the renderings back: close the claim, send nothing."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    _close_parked_claim(p, args.message_id, "owner_rejected_rendering")
    return {"outcome": "rendering_rejected", "message_id": args.message_id}


def finalize_post_estimate(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror and safely route one persisted post-estimate decision."""
    record, decision = estimate_record.post_estimate_decision(
        args.record_root, args.estimate_id, args.message_id
    )
    mirror_record(record, args.record_output)
    outcome = decision["outcome"]
    intents = decision["intents"]
    if outcome in {"design_change_detected", "classification_uncertain"}:
        asked = ask_unclear_reply(args, record, outcome)
        return {**asked, "outcome": outcome, "question_outcome": asked.get("outcome"),
                "should_finalize": True, "intents": intents, "next_action": "done"}
    if outcome != "post_estimate_continuation":
        reason_codes = {
            "design_change_detected": "design_change_detected",
            "classification_uncertain": "classification_uncertain",
            "classification_malformed": "classification_malformed",
        }
        kolo_safe.manual_review_claimed(
            args.monitor_root,
            args.claim_root,
            args.message_id,
            None,
            reason_codes[outcome],
        )
        return {
            "outcome": outcome,
            "should_finalize": True,
            "intents": intents,
            "next_action": "manual_review",
        }
    actionable = set(intents) & {"rendering_request", "appointment_request"}
    if not actionable:
        finish_processed(
            args.monitor_root, args.claim_root, args.record_root, args.message_id
        )
        return {
            "outcome": outcome,
            "should_finalize": True,
            "intents": intents,
            "next_action": "finalize",
        }
    next_action = (
        "request_appointment_approval_then_send_rendering"
        if actionable == {"rendering_request", "appointment_request"}
        else (
            "request_appointment_approval"
            if "appointment_request" in actionable
            else "send_rendering"
        )
    )
    return {
        "outcome": outcome,
        "should_finalize": False,
        "intents": intents,
        "next_action": next_action,
    }


def record_appointment_booked(args: argparse.Namespace) -> dict[str, Any]:
    """Persist and mirror an immutable successful appointment receipt."""
    before = read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    record = estimate_record.record_appointment_booked(
        args.record_root, args.estimate_id, read_object(args.receipt)
    )
    if before == record:
        write_private(args.record_output, record)
    else:
        mirror_record(record, args.record_output)
    return record


def add_common_paths(
    parser: argparse.ArgumentParser, *, message_required: bool = True
) -> None:
    parser.add_argument("--claim-root", type=Path, required=True)
    parser.add_argument("--record-root", type=Path, required=True)
    parser.add_argument("--message-id", required=message_required)
    parser.add_argument("--estimate-id", required=True)
    parser.add_argument("--record-output", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    spec = sub.add_parser("send-spec-followup")
    add_common_paths(spec)
    spec.add_argument("--monitor-root", type=Path, required=True)
    spec.add_argument("--route", type=Path, required=True)
    spec.add_argument("--body", type=Path, required=True)
    spec.add_argument("--gmail-payload", type=Path, required=True)
    spec.add_argument("--provider-response", type=Path, required=True)
    spec.add_argument("--initiating", action="store_true")
    approval = sub.add_parser("request-approval")
    add_common_paths(approval)
    approval.add_argument("--monitor-root", type=Path, required=True)
    approval.add_argument("--current-state", type=Path, required=True)
    approval.add_argument("--approval-request", type=Path, required=True)
    approval.add_argument("--shop-profile", type=Path, required=True)
    send = sub.add_parser("send-approved-estimate")
    add_common_paths(send, message_required=False)
    send.add_argument("--current-state", type=Path)
    send.add_argument("--approved", type=Path, required=True)
    send.add_argument("--body", type=Path, required=True)
    send.add_argument("--gmail-payload", type=Path, required=True)
    send.add_argument("--provider-response", type=Path, required=True)
    rendering = sub.add_parser("send-rendering")
    add_common_paths(rendering)
    rendering.add_argument("--monitor-root", type=Path, required=True)
    rendering.add_argument("--body", type=Path, required=True)
    rendering.add_argument(
        "--image", dest="images", type=Path, action="append", required=True
    )
    rendering.add_argument("--gmail-payload", type=Path, required=True)
    rendering.add_argument("--provider-response", type=Path, required=True)
    appointment = sub.add_parser("request-appointment-approval")
    add_common_paths(appointment)
    appointment.add_argument("--monitor-root", type=Path, required=True)
    appointment.add_argument("--appointment-intent", type=Path, required=True)
    appointment.add_argument("--appointment-approval", type=Path, required=True)
    appointment.add_argument("--defer-finalize-for-rendering", action="store_true")
    take = sub.add_parser("intake")
    take.add_argument("--monitor-root", type=Path, required=True)
    take.add_argument("--claim-root", type=Path, required=True)
    take.add_argument("--record-root", type=Path, required=True)
    take.add_argument("--message-id", required=True)
    take.add_argument("--shop-profile", type=Path, required=True)
    resolve = sub.add_parser("resolve-review-approval")
    resolve.add_argument("--monitor-root", type=Path, required=True)
    resolve.add_argument("--review-key", required=True)
    resolve.add_argument("--brief-id", required=True)
    start = sub.add_parser("worker-start")
    start.add_argument("--monitor-root", type=Path, required=True)
    start.add_argument("--claim-root", type=Path, required=True)
    start.add_argument("--message-id", required=True)
    start.add_argument("--record-root", type=Path, default=None)
    not_inquiry = sub.add_parser("not-an-inquiry")
    not_inquiry.add_argument("--monitor-root", type=Path, required=True)
    not_inquiry.add_argument("--claim-root", type=Path, required=True)
    not_inquiry.add_argument("--record-root", type=Path, required=True)
    not_inquiry.add_argument("--message-id", required=True)
    not_inquiry.add_argument("--estimate-id", required=True)
    not_inquiry.add_argument("--reason", required=True)
    not_inquiry.add_argument("--record-output", type=Path, required=True)
    review = sub.add_parser("review-thread")
    for name in ("--monitor-root", "--claim-root", "--record-root", "--shop-profile", "--review"):
        review.add_argument(name, type=Path, required=True)
    review.add_argument("--message-id", required=True)
    review.add_argument("--estimate-id", required=True)
    pricing = sub.add_parser("price")
    for name in ("--monitor-root", "--claim-root", "--record-root", "--shop-profile"):
        pricing.add_argument(name, type=Path, required=True)
    pricing.add_argument("--message-id", required=True)
    pricing.add_argument("--estimate-id", required=True)
    pricing.add_argument("--finished-grams", type=float, required=True)
    pricing.add_argument("--bench-hours", type=float, required=True)
    pricing.add_argument("--center-carat", type=float, default=None)
    pricing.add_argument("--fee", dest="fees", action="append", default=[])
    pricing.add_argument("--accent", dest="accents", action="append", default=[])
    render_ok = sub.add_parser("send-approved-rendering")
    render_ok.add_argument("--workspace", type=Path, required=True)
    render_ok.add_argument("--estimate-id", required=True)
    render_ok.add_argument("--message-id", required=True)
    render_ok.add_argument("--brief-id", default=None)
    render_no = sub.add_parser("reject-rendering")
    render_no.add_argument("--workspace", type=Path, required=True)
    render_no.add_argument("--message-id", required=True)
    ask_rate = sub.add_parser("ask-missing-rate")
    ask_rate.add_argument("--monitor-root", type=Path, required=True)
    ask_rate.add_argument("--claim-root", type=Path, required=True)
    ask_rate.add_argument("--record-root", type=Path, required=True)
    ask_rate.add_argument("--shop-profile", type=Path, required=True)
    ask_rate.add_argument("--message-id", required=True)
    ask_rate.add_argument("--estimate-id", required=True)
    questions = sub.add_parser("open-questions")
    questions.add_argument("--workspace", type=Path, required=True)
    answer = sub.add_parser("answer-question")
    answer.add_argument("--workspace", type=Path, required=True)
    answer.add_argument("--base-dir", type=Path, required=True)
    answer.add_argument("--question", default=None)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--openclaw", default=None)
    finalize = sub.add_parser("finalize-post-estimate")
    add_common_paths(finalize)
    finalize.add_argument("--monitor-root", type=Path, required=True)
    booked = sub.add_parser("record-appointment-booked")
    booked.add_argument("--record-root", type=Path, required=True)
    booked.add_argument("--estimate-id", required=True)
    booked.add_argument("--receipt", type=Path, required=True)
    booked.add_argument("--record-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "send-spec-followup":
            record = send_spec_followup(args)
        elif args.command == "request-approval":
            record = request_approval(args)
        elif args.command == "send-approved-estimate":
            record = send_approved_estimate(args)
        elif args.command == "request-appointment-approval":
            record = request_appointment_approval(args)
        elif args.command == "intake":
            record = intake(args)
        elif args.command == "not-an-inquiry":
            record = not_an_inquiry(args)
        elif args.command == "worker-start":
            record = worker_start(args)
        elif args.command == "resolve-review-approval":
            record = resolve_review_approval(args)
        elif args.command == "send-approved-rendering":
            record = send_approved_rendering(args)
        elif args.command == "reject-rendering":
            record = reject_rendering(args)
        elif args.command == "review-thread":
            record = review_thread(args)
        elif args.command == "price":
            record = price(args)
        elif args.command == "ask-missing-rate":
            record = ask_missing_rate(args)
        elif args.command == "open-questions":
            record = open_questions(args)
        elif args.command == "answer-question":
            record = answer_question(args)
        elif args.command == "finalize-post-estimate":
            record = finalize_post_estimate(args)
        elif args.command == "record-appointment-booked":
            record = record_appointment_booked(args)
        else:
            record = send_rendering(args)
        print(json.dumps(record, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
