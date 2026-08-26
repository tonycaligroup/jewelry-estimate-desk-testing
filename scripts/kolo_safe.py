#!/usr/bin/env python3
"""Invoke supported Kolo CLI operations without a shell."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import inbox_claim
import inbox_monitor


ESTIMATE_ID_RE = re.compile(r"^jed-[0-9a-f]{16}$")
SESSION_KEY_RE = re.compile(r"^agent:[A-Za-z0-9_.:@/-]{1,255}$")
OWNER_NOTIFICATION_MESSAGES = {
    "approval-ready": "Estimate {estimate_id} is ready for approval. Open the brief in Kolo.",
    "customer-replied": "Customer replied on estimate {estimate_id}. Open Kolo to review.",
}
MONITOR_NOTIFICATION_MESSAGES = {
    "manual-review": (
        "Jewelry Estimate Desk has an unresolved manual-review item. "
        "Ask Kolo to show unresolved Jewelry Estimate Desk reviews."
    ),
    "system-actionable": "Jewelry Estimate Desk inbox monitor needs attention. Open Kolo to review.",
    "state-error": "Jewelry Estimate Desk inbox monitor state needs attention. Open Kolo to review.",
}


def read_json_argument(path: Path) -> str:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_estimate_id(value: str) -> str:
    if not ESTIMATE_ID_RE.fullmatch(value):
        raise ValueError("estimate ID must match jed- followed by 16 lowercase hex characters")
    return value


def validate_session_key(value: str) -> str:
    if not SESSION_KEY_RE.fullmatch(value):
        raise ValueError("session key must be an agent: session key returned by sessions_list")
    return value


def build_request_approval(
    estimate_id: str, details: Path, session_key: str, agent_id: str = "main"
) -> list[str]:
    estimate_id = validate_estimate_id(estimate_id)
    session_key = validate_session_key(session_key)
    payload = read_json_argument(details)
    return [
        "kolo",
        "request-approval",
        "--agent-id",
        agent_id,
        "--action",
        f"Custom estimate — {estimate_id}",
        "--reasoning",
        "Structured custom-jewelry estimate ready for owner review",
        "--risk-level",
        "medium",
        "--details",
        payload,
        "--execution-payload",
        payload,
        "--session-key",
        session_key,
    ]


def build_notify_owner(estimate_id: str, event: str = "approval-ready") -> list[str]:
    estimate_id = validate_estimate_id(estimate_id)
    try:
        message = OWNER_NOTIFICATION_MESSAGES[event].format(
            estimate_id=estimate_id.upper()
        )
    except KeyError as exc:
        raise ValueError("invalid owner notification event") from exc
    return [
        "kolo",
        "notify-owner",
        "-m",
        message,
    ]


def build_notify_monitor(event: str) -> list[str]:
    try:
        message = MONITOR_NOTIFICATION_MESSAGES[event]
    except KeyError as exc:
        raise ValueError("invalid monitor notification event") from exc
    return ["kolo", "notify-owner", "-m", message]


def build_record_upsert(record_type: str, external_id: str, payload: Path, status: str) -> list[str]:
    if not re.fullmatch(r"skill\.[a-z0-9_.-]+", record_type):
        raise ValueError("invalid record type")
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]+", external_id):
        raise ValueError("invalid external ID")
    if not re.fullmatch(r"[a-z0-9_-]+", status):
        raise ValueError("invalid status")
    return [
        "kolo",
        "record-upsert",
        "--record-type",
        record_type,
        "--external-id",
        external_id,
        "--payload",
        read_json_argument(payload),
        "--status",
        status,
    ]


def build_log_action(
    title: str,
    description: str,
    event_type: str,
    idempotency_key: str,
    details: Path,
    agent_id: str = "main",
) -> list[str]:
    if not title or len(title) > 160 or any(character in title for character in "\r\n"):
        raise ValueError("title must contain 1-160 characters on one line")
    if not description or len(description) > 2000:
        raise ValueError("description must contain 1-2000 characters")
    if not re.fullmatch(r"[a-z0-9_-]+", event_type):
        raise ValueError("invalid event type")
    if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", idempotency_key):
        raise ValueError("invalid idempotency key")
    return [
        "kolo",
        "log-action",
        "--agent-id",
        agent_id,
        "--title",
        title,
        "--description",
        description,
        "--event-type",
        event_type,
        "--idempotency-key",
        idempotency_key,
        "--details",
        read_json_argument(details),
    ]


def run_command(
    argv: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(list(argv), check=True, capture_output=True, text=True, shell=False)


def request_approval_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str,
    action_key: str,
    estimate_id: str,
    details: Path,
    session_key: str,
    agent_id: str = "main",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Create one approval request with durable ambiguity tracking."""
    command = build_request_approval(estimate_id, details, session_key, agent_id)
    binding_material = json.dumps(
        {
            "agent_id": agent_id,
            "details": json.loads(read_json_argument(details)),
            "estimate_id": estimate_id,
            "session_key": session_key,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    binding = "sha256:" + hashlib.sha256(binding_material).hexdigest()
    acquired, state = inbox_claim.acquire_external_action(
        claim_root,
        message_id,
        claim_token,
        action_key,
        "approval_request",
        binding,
    )
    if not acquired:
        status = state["external_actions"][action_key]["status"]
        if status == "sent":
            return subprocess.CompletedProcess(
                command, 0, "approval request already sent\n", ""
            )
        raise ValueError(f"approval request is already {status}; refusing retry")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_external_action(
            claim_root, message_id, claim_token, action_key, "uncertain"
        )
        raise
    inbox_claim.finish_external_action(
        claim_root, message_id, claim_token, action_key, "sent"
    )
    return result


def notify_owner_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str,
    notification_key: str,
    estimate_id: str,
    event: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Send one claimed-message notification with durable ambiguity tracking."""
    command = build_notify_owner(estimate_id, event)
    acquired, state = inbox_claim.acquire_notification(
        claim_root, message_id, claim_token, notification_key
    )
    if not acquired:
        status = state["owner_notification"]["status"]
        return subprocess.CompletedProcess(command, 0, f"notification already {status}\n", "")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        # Kolo provides no delivery receipt lookup. Once invocation begins, a
        # failure is ambiguous and must never be retried automatically.
        inbox_claim.finish_notification(
            claim_root, message_id, claim_token, "uncertain"
        )
        raise
    inbox_claim.finish_notification(claim_root, message_id, claim_token, "sent")
    return result


def notify_monitor_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str,
    notification_key: str,
    event: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Send one generic monitor notification with durable ambiguity tracking."""
    command = build_notify_monitor(event)
    acquired, state = inbox_claim.acquire_notification(
        claim_root, message_id, claim_token, notification_key
    )
    if not acquired:
        status = state["owner_notification"]["status"]
        return subprocess.CompletedProcess(command, 0, f"notification already {status}\n", "")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_notification(
            claim_root, message_id, claim_token, "uncertain"
        )
        raise
    # `sent` records successful CLI acceptance. Kolo exposes no independent
    # user-visible delivery receipt for this command.
    inbox_claim.finish_notification(claim_root, message_id, claim_token, "sent")
    return result


def manual_review_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    claim_token: str,
    reason_code: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    """Persist manual review before attempting its one owner notification."""
    queue_item = inbox_monitor.finalize_item(
        monitor_root,
        message_id,
        claim_root,
        claim_token,
        "manual_review",
        reason_code,
    )
    result = notify_monitor_claimed(
        claim_root,
        message_id,
        claim_token,
        f"manual_review:{reason_code}:{message_id}",
        "manual-review",
        runner=runner,
    )
    return queue_item, result


def reconcile_stale_claims(
    monitor_root: Path,
    claim_root: Path,
    minimum_age_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, int]:
    """Terminalize unsafe stale claims and leave safely resumable claims queued."""
    stale = inbox_monitor.stale_processing_items(
        monitor_root, claim_root, minimum_age_seconds
    )
    summary = {
        "stale": len(stale),
        "resumable": 0,
        "manual_review": 0,
        "notification_uncertain": 0,
    }
    for item in stale:
        if item["recovery_action"] == "resume":
            summary["resumable"] += 1
            continue
        inbox_monitor.finalize_item(
            monitor_root,
            item["gmail_message_id"],
            claim_root,
            item["claim_token"],
            "manual_review",
            item["reason_code"],
        )
        summary["manual_review"] += 1
        try:
            notify_monitor_claimed(
                claim_root,
                item["gmail_message_id"],
                item["claim_token"],
                f"manual_review:{item['reason_code']}:{item['gmail_message_id']}",
                "manual-review",
                runner=runner,
            )
        except (OSError, subprocess.CalledProcessError):
            # Terminal state is already committed. An ambiguous notification
            # is preserved and never retried automatically.
            summary["notification_uncertain"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    approval = sub.add_parser("request-approval")
    approval.add_argument("--estimate-id", required=True)
    approval.add_argument("--details", type=Path, required=True)
    approval.add_argument("--session-key", required=True)
    approval.add_argument("--agent-id", default="main")
    approval_claimed = sub.add_parser("request-approval-claimed")
    approval_claimed.add_argument("--claim-root", type=Path, required=True)
    approval_claimed.add_argument("--message-id", required=True)
    approval_claimed.add_argument("--claim-token", required=True)
    approval_claimed.add_argument("--action-key", required=True)
    approval_claimed.add_argument("--estimate-id", required=True)
    approval_claimed.add_argument("--details", type=Path, required=True)
    approval_claimed.add_argument("--session-key", required=True)
    approval_claimed.add_argument("--agent-id", default="main")

    notify = sub.add_parser("notify-owner")
    notify.add_argument("--estimate-id", required=True)
    notify.add_argument(
        "--event", choices=sorted(OWNER_NOTIFICATION_MESSAGES), default="approval-ready"
    )
    notify_monitor = sub.add_parser("notify-monitor")
    notify_monitor.add_argument(
        "--event", choices=sorted(MONITOR_NOTIFICATION_MESSAGES), required=True
    )
    notify_claimed = sub.add_parser("notify-owner-claimed")
    notify_claimed.add_argument("--claim-root", type=Path, required=True)
    notify_claimed.add_argument("--message-id", required=True)
    notify_claimed.add_argument("--claim-token", required=True)
    notify_claimed.add_argument("--notification-key", required=True)
    notify_claimed.add_argument("--estimate-id", required=True)
    notify_claimed.add_argument(
        "--event", choices=sorted(OWNER_NOTIFICATION_MESSAGES), required=True
    )
    notify_monitor_claimed_parser = sub.add_parser("notify-monitor-claimed")
    notify_monitor_claimed_parser.add_argument("--claim-root", type=Path, required=True)
    notify_monitor_claimed_parser.add_argument("--message-id", required=True)
    notify_monitor_claimed_parser.add_argument("--claim-token", required=True)
    notify_monitor_claimed_parser.add_argument("--notification-key", required=True)
    notify_monitor_claimed_parser.add_argument(
        "--event", choices=sorted(MONITOR_NOTIFICATION_MESSAGES), required=True
    )
    manual_review_parser = sub.add_parser("manual-review-claimed")
    manual_review_parser.add_argument("--monitor-root", type=Path, required=True)
    manual_review_parser.add_argument("--claim-root", type=Path, required=True)
    manual_review_parser.add_argument("--message-id", required=True)
    manual_review_parser.add_argument("--claim-token", required=True)
    manual_review_parser.add_argument("--reason-code", required=True)
    stale_parser = sub.add_parser("reconcile-stale-claims")
    stale_parser.add_argument("--monitor-root", type=Path, required=True)
    stale_parser.add_argument("--claim-root", type=Path, required=True)
    stale_parser.add_argument("--minimum-age-seconds", type=int, required=True)

    upsert = sub.add_parser("record-upsert")
    upsert.add_argument("--record-type", required=True)
    upsert.add_argument("--external-id", required=True)
    upsert.add_argument("--payload", type=Path, required=True)
    upsert.add_argument("--status", required=True)

    log = sub.add_parser("log-action")
    log.add_argument("--title", required=True)
    log.add_argument("--description", required=True)
    log.add_argument("--event-type", required=True)
    log.add_argument("--idempotency-key", required=True)
    log.add_argument("--details", type=Path, required=True)
    log.add_argument("--agent-id", default="main")

    args = parser.parse_args(argv)
    try:
        if args.command == "request-approval":
            command = build_request_approval(
                args.estimate_id, args.details, args.session_key, args.agent_id
            )
        elif args.command == "request-approval-claimed":
            result = request_approval_claimed(
                args.claim_root,
                args.message_id,
                args.claim_token,
                args.action_key,
                args.estimate_id,
                args.details,
                args.session_key,
                args.agent_id,
            )
            if result.stdout:
                print(result.stdout, end="")
            return 0
        elif args.command == "notify-owner":
            command = build_notify_owner(args.estimate_id, args.event)
        elif args.command == "notify-monitor":
            command = build_notify_monitor(args.event)
        elif args.command == "notify-owner-claimed":
            result = notify_owner_claimed(
                args.claim_root,
                args.message_id,
                args.claim_token,
                args.notification_key,
                args.estimate_id,
                args.event,
            )
            if result.stdout:
                print(result.stdout, end="")
            return 0
        elif args.command == "notify-monitor-claimed":
            result = notify_monitor_claimed(
                args.claim_root,
                args.message_id,
                args.claim_token,
                args.notification_key,
                args.event,
            )
            if result.stdout:
                print(result.stdout, end="")
            return 0
        elif args.command == "manual-review-claimed":
            queue_item, _result = manual_review_claimed(
                args.monitor_root,
                args.claim_root,
                args.message_id,
                args.claim_token,
                args.reason_code,
            )
            notification = inbox_claim.read_state(
                inbox_claim.claim_path(args.claim_root, args.message_id)
            )["owner_notification"]
            print(
                json.dumps(
                    {
                        "processing_status": queue_item["processing_status"],
                        "reason_code": queue_item.get("reason_code"),
                        "notification_status": notification["status"],
                        "delivery_receipt_available": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        elif args.command == "reconcile-stale-claims":
            print(
                json.dumps(
                    reconcile_stale_claims(
                        args.monitor_root,
                        args.claim_root,
                        args.minimum_age_seconds,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        elif args.command == "record-upsert":
            command = build_record_upsert(
                args.record_type, args.external_id, args.payload, args.status
            )
        else:
            command = build_log_action(
                args.title,
                args.description,
                args.event_type,
                args.idempotency_key,
                args.details,
                args.agent_id,
            )
        result = run_command(command)
        if result.stdout:
            print(result.stdout, end="")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
