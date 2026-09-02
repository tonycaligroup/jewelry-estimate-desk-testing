#!/usr/bin/env python3
"""High-level fail-closed workflow actions that never copy claim tokens by hand."""

from __future__ import annotations

import argparse
import json
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
import inbox_claim
import gateway_token
import gmail_classify
import gmail_route
import inbox_monitor
import kolo_safe
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
    not_inquiry = sub.add_parser("not-an-inquiry")
    not_inquiry.add_argument("--monitor-root", type=Path, required=True)
    not_inquiry.add_argument("--claim-root", type=Path, required=True)
    not_inquiry.add_argument("--record-root", type=Path, required=True)
    not_inquiry.add_argument("--message-id", required=True)
    not_inquiry.add_argument("--estimate-id", required=True)
    not_inquiry.add_argument("--reason", required=True)
    not_inquiry.add_argument("--record-output", type=Path, required=True)
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
