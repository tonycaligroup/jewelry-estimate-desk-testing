#!/usr/bin/env python3
"""The desk's doctor: inconsistent state, each with the one line that repairs it.

RELIABILITY-PLAN.md 3.4. Read-only, seconds, no model, no customer contact.
Every finding comes from a real incident: a calendar hold nothing recorded,
a claim parked with no question anyone will answer, a question whose claim
is gone, a card the desk can no longer act on, a queue item with no claim.
The main Kolo session runs this instead of reading files and guessing.

`--requeue <gmail-id>` is the one repair that changes state: it hands the
desk a message again, through the normal fetch, and puts it in front of the
next tick. It refuses a message the desk already finished.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import brief_registry
import estimate_record
import gateway_token
import inbox_claim
import inbox_monitor
import owner_questions
import run_lease

PARKING_KINDS = {"missing_rate", "same_sender", "unclear_reply", "followup_stalled", "stuck_claim"}


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _lines(workspace: Path) -> dict[str, str]:
    base = _skill_dir()
    return {
        "requeue": f"python3 {base}/scripts/doctor.py --workspace {workspace} --requeue '<gmail-id>'",
        "answer": f"python3 {base}/scripts/workflow_safe.py answer-question --workspace {workspace} --base-dir {base} --question '<CODE>' --answer '<words>'",
    }


def scan(workspace: Path) -> list[dict[str, Any]]:
    """Every inconsistency the desk can see, newest incident kinds first. Never raises on state."""
    workspace = workspace.resolve()
    desk = workspace / "estimate-desk"
    monitor_root, claim_root, record_root = desk / "inbox-monitor", desk / "inbox-claims", desk / "records"
    lines = _lines(workspace)
    findings: list[dict[str, Any]] = []

    def add(code: str, subject: str, detail: str, repair: str, level: str = "repair") -> None:
        findings.append({"code": code, "level": level, "subject": subject, "detail": detail[:300], "repair": repair})

    try:
        items = inbox_monitor.all_queue_items(monitor_root)
    except (OSError, ValueError) as exc:
        add("monitor_unreadable", "inbox monitor", str(exc), "run the readiness check; the monitor state is not readable")
        return findings
    try:
        questions = owner_questions.list_questions(owner_questions.questions_root(monitor_root))
    except (OSError, ValueError):
        questions = []
    open_questions = [q for q in questions if q.get("status") == "open" and not q.get("dormant")]
    open_by_message: dict[str, list[dict[str, Any]]] = {}
    for q in open_questions:
        open_by_message.setdefault(str(q.get("gmail_message_id")), []).append(q)

    claims: dict[str, dict[str, Any] | None] = {}
    for item in items:
        message_id = item["gmail_message_id"]
        path = inbox_claim.claim_path(claim_root, message_id)
        try:
            claims[message_id] = inbox_claim.read_state(path) if path.exists() else None
        except (OSError, ValueError):
            claims[message_id] = None
        claim = claims[message_id]
        status = item.get("processing_status")
        if claim is None and status in {"processing", "awaiting_owner"}:
            add("queue_without_claim", f"message {message_id}",
                f"the queue says {status} but the claim folder is gone",
                lines["requeue"].replace("<gmail-id>", message_id))
            continue
        if claim is None:
            continue
        if claim.get("status") == "awaiting_owner" and not open_by_message.get(message_id):
            add("parked_without_question", f"message {message_id}",
                f"parked ({claim.get('reason_code') or 'no reason'}) but no open question will resume it",
                lines["requeue"].replace("<gmail-id>", message_id))
        if claim.get("status") == "processing" and not inbox_claim.recovery_lease_active(claim):
            if "inline_attempts" in claim:
                add("inline_retry_pending", f"message {message_id}",
                    f"the tick will retry it (attempt {int(claim.get('inline_attempts') or 0) + 1}); last error: {claim.get('last_error') or 'none'}",
                    "nothing; the next tick retries, then asks", level="info")
            else:
                add("worker_lease_lapsed", f"message {message_id}",
                    "a worker's lease has lapsed with no result",
                    "nothing; the next tick's reconciler resumes it once, then it becomes a review", level="info")
        for key, action in (claim.get("external_actions") or {}).items():
            if isinstance(action, dict) and action.get("status") in {"pending", "uncertain", "verified_unsent"}:
                who = "the next tick" if claim.get("status") in {"processing", "awaiting_owner"} else "the same execute line, run again,"
                add("action_unsettled", f"message {message_id}",
                    f"{key} is {action['status']}: the outcome of that call is not settled",
                    f"{who} checks the provider before doing anything; run it (or the execute line) once more", level="info")

    for q in open_questions:
        if q.get("kind") not in PARKING_KINDS:
            continue
        message_id = str(q.get("gmail_message_id"))
        claim = claims.get(message_id)
        path = inbox_claim.claim_path(claim_root, message_id)
        if claim is None and not path.exists():
            add("question_without_claim", f"question {owner_questions.reference(q['question_id'])}",
                f"{q['kind']} question is open but its claim is gone",
                "the question is stale: " + lines["answer"].replace("<CODE>", owner_questions.reference(q["question_id"]))
                .replace("<words>", "handle myself" if q["kind"] != "missing_rate" else "<the number, if the estimate is still open>"))
        elif claim is not None and claim.get("status") in {"processed", "manual_review"}:
            add("question_without_claim", f"question {owner_questions.reference(q['question_id'])}",
                f"{q['kind']} question is open but its claim already finished as {claim.get('status')}",
                "close it: " + lines["answer"].replace("<CODE>", owner_questions.reference(q["question_id"]))
                .replace("<words>", "handle myself" if q["kind"] != "missing_rate" else "skip"))

    try:
        entries = brief_registry.load_all(monitor_root)
    except (OSError, ValueError):
        entries = []
    for entry in entries:
        if entry.get("outcome") != "pending":
            continue
        message_id = str(entry.get("message_id"))
        if not inbox_claim.claim_path(claim_root, message_id).exists():
            add("card_without_claim", f"brief #{entry.get('brief_number') or entry.get('brief_id')}",
                f"{entry.get('kind')} card '{entry.get('action_title')}' is pending but its claim is gone",
                "reject this card in Kolo; the desk cannot withdraw a brief")

    work_root = desk / "work"
    for event_path in sorted(work_root.glob("booking-*/calendar-event.json")) if work_root.is_dir() else []:
        saved = _read(event_path)
        if not isinstance(saved, dict):
            continue
        key = event_path.parent.name[len("booking-"):]
        approval = None
        for store in sorted((desk / "approvals").glob(f"*-{key}.json")) if (desk / "approvals").is_dir() else []:
            approval = _read(store)
            break
        estimate_id = (approval or {}).get("estimate_id")
        record = None
        if estimate_id:
            try:
                record = estimate_record.read_object(estimate_record.record_path(record_root, estimate_id))
            except (OSError, ValueError):
                record = None
        booked = ((record or {}).get("appointment_booked") or {}).get("calendar_event_id")
        history = [h.get("calendar_event_id") for h in (record or {}).get("appointment_history") or [] if isinstance(h, dict)]
        if saved.get("id") and saved["id"] not in {booked, *history}:
            message_id = (approval or {}).get("source_message_id") or "?"
            question = next((q for q in open_questions if q.get("kind") == "command_failed"
                             and str(q.get("gmail_message_id")) == message_id), None)
            repair = (
                "answer the desk's question: " + lines["answer"].replace("<CODE>", owner_questions.reference(question["question_id"])).replace("<words>", "retry (or release)")
                if question else ((approval or {}).get("execute") or "run the booking's execute line again; it adopts this event")
            )
            add("calendar_hold_unrecorded", f"calendar event {saved['id']}",
                f"held for {saved.get('desk_slot_start')} but no booking on record {estimate_id or '?'}", repair)
        elif not saved.get("id") and saved.get("desk_slot_start"):
            add("calendar_step_started", f"booking {key}",
                f"a booking run was killed inside the calendar call for {saved.get('desk_slot_start')}",
                (approval or {}).get("execute") or "run the booking's execute line again; it checks the calendar first", level="info")

    locks = desk / "locks"
    now = datetime.now(timezone.utc)
    for lock in sorted(locks.glob("*.lock")) if locks.is_dir() else []:
        lease = _read(lock)
        if run_lease._expired(lease, now):
            add("stale_lock", lock.name, "a run died holding its lease", "nothing; the next run takes it over", level="info")
    return findings


def requeue(workspace: Path, message_id: str, token: str | None = None, opener: Any = None) -> dict[str, Any]:
    """Hand the desk a message again, through the normal path; refuse one it already finished."""
    workspace = workspace.resolve()
    desk = workspace / "estimate-desk"
    monitor_root, claim_root = desk / "inbox-monitor", desk / "inbox-claims"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id or ""):
        raise ValueError("a Gmail message id is letters, digits, dashes, and underscores")
    state = inbox_monitor.load_monitor_state(monitor_root)
    if state.get("activation_state") != "active":
        raise ValueError("the inbox monitor is not active")
    path = inbox_monitor.queue_path(monitor_root, message_id)
    if path.exists():
        item = inbox_monitor.load_queue_item(monitor_root, message_id)
        claim_file = inbox_claim.claim_path(claim_root, message_id)
        claim = inbox_claim.read_state(claim_file) if claim_file.exists() else None
        if claim is None:
            if item["processing_status"] == "unclaimed":
                return {"outcome": "already_queued", "message_id": message_id}
            item["processing_status"] = "unclaimed"
            item.pop("reason_code", None)
            item.pop("review_status", None)
            inbox_monitor.atomic_write_json(path, item)
            return {"outcome": "requeued", "message_id": message_id, "how": "queue item reset; its claim folder was gone"}
        if claim.get("status") == "awaiting_owner":
            reopened = inbox_monitor.reopen_item(monitor_root, message_id, claim_root, 1)
            token_claim = reopened["claim"]["claim_token"]
            inbox_claim.mark_inline(claim_root, message_id, token_claim, True)
            inbox_claim.release_lease(claim_root, message_id, token_claim)
            return {"outcome": "requeued", "message_id": message_id, "how": "parked claim reopened; the next tick retries it"}
        if claim.get("status") == "processing":
            if inbox_claim.recovery_lease_active(claim):
                raise ValueError("a run holds this claim right now; wait for it to finish")
            return {"outcome": "already_queued", "message_id": message_id, "how": "processing; the next tick retries or resumes it"}
        raise ValueError(f"this message already finished as {claim.get('status')}; nothing to requeue")
    import gmail_fetch  # local import: gmail_fetch imports inbox_monitor

    from urllib.parse import quote

    kwargs = {"opener": opener} if opener else {}
    message = gmail_fetch.fetch_json(f"messages/{quote(message_id, safe='')}", {"format": "metadata"},
                                     token or gateway_token.load_token(), **kwargs)
    thread_id = message.get("threadId")
    internal = message.get("internalDate")
    if not isinstance(thread_id, str) or not thread_id or not str(internal or "").isdigit():
        raise ValueError("Gmail returned no thread id or date for that message")
    internal_ms = int(internal)
    if internal_ms < int(state.get("activated_at_ms") or 0):
        raise ValueError("that message predates the desk's activation; it was never the desk's to answer")
    item = {
        "schema_version": inbox_monitor.QUEUE_SCHEMA_VERSION,
        "gmail_message_id": message_id,
        "gmail_message_id_sha256": inbox_monitor.message_key(message_id),
        "thread_id": thread_id,
        "internal_date_ms": internal_ms,
        "discovery_status": "complete",
        "processing_status": "unclaimed",
    }
    inbox_monitor.validate_queue_item(item)
    inbox_monitor.atomic_write_json(path, item)
    return {"outcome": "requeued", "message_id": message_id, "how": "fetched from Gmail and queued; the next tick takes it"}


def report(findings: list[dict[str, Any]]) -> str:
    repairs = [f for f in findings if f["level"] == "repair"]
    if not findings:
        return "state: clean"
    lines = [f"state: {len(repairs)} to repair, {len(findings) - len(repairs)} informational"]
    for f in findings:
        lines.append(f"{'REPAIR' if f['level'] == 'repair' else 'INFO  '} {f['code']} | {f['subject']} | {f['detail']}")
        lines.append(f"       -> {f['repair']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--requeue", default=None, help="a Gmail message id to hand the desk again")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.requeue:
            print(json.dumps(requeue(args.workspace, args.requeue), sort_keys=True))
            return 0
        findings = scan(args.workspace)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(findings, sort_keys=True))
    else:
        print(report(findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
