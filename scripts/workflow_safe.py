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
    if set(intent) != {"requested_times", "calendar_availability"}:
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
    return {
        "schema_version": 1,
        "action_type": "appointment_booking",
        "estimate_id": record["estimate_id"],
        "source_message_id": message_id,
        "customer_email": route["recipient"],
        "thread_id": route["thread_id"],
        "requested_times": [value.strip() for value in requested_times],
        "calendar_availability": normalized_slots,
    }


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
        root, question, runner=getattr(args, "runner", subprocess.run)
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
    question = (
        owner_questions.find(root, args.question) if args.question else owner_questions.only_open(root)
    )
    if question["status"] == "answered":
        return {
            "outcome": "already_answered",
            "question_id": question["question_id"],
            "answer": question.get("answer"),
        }
    if question["kind"] != "missing_rate":
        raise ValueError("only missing-rate questions are answered this way")
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


def finalize_post_estimate(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror and safely route one persisted post-estimate decision."""
    record, decision = estimate_record.post_estimate_decision(
        args.record_root, args.estimate_id, args.message_id
    )
    mirror_record(record, args.record_output)
    outcome = decision["outcome"]
    intents = decision["intents"]
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
