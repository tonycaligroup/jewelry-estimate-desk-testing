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

import activation_binding
import estimate_record
import inbox_claim
import inbox_monitor


ESTIMATE_ID_RE = re.compile(r"^jed-[0-9a-f]{16}$")
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
        raise ValueError(
            "estimate ID must match jed- followed by 16 lowercase hex characters"
        )
    return value


def validate_session_key(value: str) -> str:
    return activation_binding.validate_session_key(value)


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


def approval_reasoning(details: dict[str, Any]) -> str:
    """Render the bound cost sheet in the visible owner-approval summary."""
    review = details.get("owner_review")
    if not isinstance(review, dict):
        return "Structured custom-jewelry estimate ready for owner review"
    required = {
        "customer_price",
        "hard_cost_total",
        "estimated_gross_profit",
        "metal_costs",
        "stone_costs",
        "labor_costs",
        "other_hard_costs",
    }
    missing = sorted(required - set(review))
    if missing:
        raise ValueError(
            "owner approval display is missing fields: " + ", ".join(missing)
        )
    lines = [
        "JEWELER-ONLY COST SHEET — never customer-facing",
        f"Customer price: {_money(review['customer_price'])}",
        f"Estimated hard costs: {_money(review['hard_cost_total'])}",
        f"Estimated gross profit: {_money(review['estimated_gross_profit'])}",
        "",
        "Metal assumptions:",
    ]
    for item in review.get("metal_costs", []):
        lines.append(
            f"- {item['metal']}: {item['quantity_grams']:g} g × "
            f"{_money(item['unit_cost'])}/g = {_money(item['total_cost'])}"
        )
    lines.append("Stone assumptions:")
    for item in review.get("stone_costs", []):
        lines.append(
            f"- {item['stone']}: {item['quantity']:g} × "
            f"{_money(item['unit_cost'])} = {_money(item['total_cost'])}"
        )
    lines.append("Labor assumptions:")
    for item in review.get("labor_costs", []):
        lines.append(
            f"- {item['task']}: {item['hours']:g} hr × "
            f"{_money(item['rate'])}/hr = {_money(item['total_cost'])}"
        )
    if review.get("other_hard_costs"):
        lines.append("Other hard costs:")
        for item in review["other_hard_costs"]:
            lines.append(f"- {item['label']}: {_money(item['total_cost'])}")
    reasoning = "\n".join(lines)
    if len(reasoning) > 4000:
        raise ValueError("owner approval cost summary exceeds 4000 characters")
    return reasoning


def _piece_words(specification: Any) -> str:
    try:
        import owner_questions  # local import: owner_questions depends on cost_components only

        return owner_questions.summary_of_piece(specification)
    except Exception:  # noqa: BLE001 - a summary is a courtesy, never a failure
        return "a piece"


def approval_title(details: dict[str, Any], estimate_id: str) -> str:
    """'Price approval: a pendant in 14K yellow gold with a natural ruby 1 ct, $3,802.76'."""
    review = details.get("owner_review") if isinstance(details.get("owner_review"), dict) else {}
    price = review.get("customer_price", details.get("proposed_price"))
    piece = _piece_words(details.get("specification"))
    money = _money(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else "price pending"
    return f"Price approval: {piece}, {money}"[:120]


def approval_details(details: dict[str, Any], estimate_id: str) -> dict[str, str]:
    """Flat text rows for the card; nested objects render as noise there."""
    route = details.get("route") if isinstance(details.get("route"), dict) else {}
    review = details.get("owner_review") if isinstance(details.get("owner_review"), dict) else {}
    price = review.get("customer_price", details.get("proposed_price"))
    rows = {
        "Customer": str(route.get("recipient") or "unknown")[:120],
        "Subject": str(route.get("original_subject") or "unknown")[:160],
        "Piece": _piece_words(details.get("specification"))[:160],
        "Proposed price": _money(price) if isinstance(price, (int, float)) and not isinstance(price, bool) else "unknown",
        "Approve means": "Send this price to the customer in their email thread.",
        "Reject means": "Nothing is sent; the desk steps back from this thread.",
        "Estimate": estimate_id,
    }
    hard = review.get("hard_cost_total")
    if isinstance(hard, (int, float)) and not isinstance(hard, bool):
        rows["Hard cost (owner only)"] = _money(hard)
    return rows


def build_request_approval(
    estimate_id: str, details: Path, session_key: str, agent_id: str = "main"
) -> list[str]:
    estimate_id = validate_estimate_id(estimate_id)
    session_key = validate_session_key(session_key)
    details_object = json.loads(read_json_argument(details))
    payload = json.dumps(
        details_object, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return [
        "kolo",
        "request-approval",
        "--agent-id",
        agent_id,
        "--action",
        approval_title(details_object, estimate_id),
        "--reasoning",
        approval_reasoning(details_object),
        "--risk-level",
        "medium",
        "--details",
        json.dumps(
            approval_details(details_object, estimate_id),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ),
        "--execution-payload",
        payload,
        "--session-key",
        session_key,
    ]


def build_request_appointment_approval(
    estimate_id: str, details: Path, session_key: str, agent_id: str = "main"
) -> list[str]:
    """Build one durable owner approval for a customer appointment request."""
    estimate_id = validate_estimate_id(estimate_id)
    session_key = validate_session_key(session_key)
    details_object = json.loads(read_json_argument(details))
    if details_object.get("action_type") not in {"appointment_booking", "appointment_offer"}:
        raise ValueError("appointment approval action_type must be appointment_booking or appointment_offer")
    if details_object.get("estimate_id") != estimate_id:
        raise ValueError("appointment approval estimate_id does not match")
    payload = json.dumps(
        details_object, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    rows, reasoning, title = appointment_card(details_object, estimate_id)
    return [
        "kolo",
        "request-approval",
        "--agent-id",
        agent_id,
        "--action",
        title,
        "--reasoning",
        reasoning,
        "--risk-level",
        "medium",
        "--details",
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--execution-payload",
        payload,
        "--session-key",
        session_key,
    ]


def appointment_card(details: dict[str, Any], estimate_id: str) -> tuple[dict[str, str], str, str]:
    """Flat rows the owner can check against their own calendar.

    Booking cards are yes-or-no on one time. Offer cards list the times the
    desk would email the customer. Either way, reject makes the desk ask the
    owner what to do next.
    """
    customer = str(details.get("customer_email") or "unknown")[:120]
    piece = str(details.get("piece") or "their estimate")[:120]
    asked = details.get("requested_times") or []
    asked_text = "; ".join(str(t) for t in asked)[:200] if asked else "no time given"
    options = details.get("calendar_availability") or []
    booking = details.get("action_type") == "appointment_booking"
    rows: dict[str, str] = {
        "Customer": customer,
        "Piece": piece,
        "Customer asked for": asked_text,
        "Estimate": estimate_id,
    }
    code = str(details.get("reject_code") or "").strip()
    reject = (
        f"Nothing happens for the customer. Then reply here with \"{code}\" and what you want: times to offer, "
        "\"other times\", or \"handle myself\"."
        if code else "Nothing happens for the customer yet; the desk asks you what to do next."
    )
    if booking and options:
        when = str(options[0].get("label") or options[0].get("start"))[:120]
        rows["Time"] = when
        rows["Approve means"] = f"Book {when} on your calendar, invite the customer, and confirm in their email thread."
        rows["Reject means"] = reject
        title = f"Book appointment: {piece}, {when}"[:120]
        reasoning = (
            f"{customer} asked to meet ({asked_text}). That time is free on your calendar inside your "
            "declared windows. Approve to book it; reject and tell the desk what to do with the code below."
        )
    elif options:
        for index, slot in enumerate(options[:3], start=1):
            rows[f"Option {index}"] = str(slot.get("label") or slot.get("start") or "")[:120]
        rows["Approve means"] = "Email these times to the customer and let them pick one. Nothing is booked yet."
        rows["Reject means"] = reject
        title = f"Offer meeting times: {piece}"[:120]
        why = str(details.get("availability_note") or "").strip()
        reasoning = (
            f"{customer} asked to meet ({asked_text}). "
            + (f"{why[0].upper() + why[1:]}. " if why else "")
            + "These times are free on your calendar inside your declared windows. Approve to offer them; "
            "reject and tell the desk what to do with the code below."
        )
    else:
        reason = str(details.get("availability_note") or "no calendar-checked times were available")[:160]
        rows["Times I can offer"] = f"none ({reason})"
        rows["Approve means"] = "Nothing; the desk needs times from you. Reject, and it will ask."
        rows["Reject means"] = reject
        title = f"Appointment request: {piece}"[:120]
        reasoning = f"{customer} asked to meet ({asked_text}). {reason}."
    return rows, reasoning, title


def build_request_rendering_approval(
    estimate_id: str, details: Path, session_key: str, agent_id: str = "main"
) -> list[str]:
    """One owner approval before renderings reach a customer (WORKFLOW.md 6.6)."""
    estimate_id = validate_estimate_id(estimate_id)
    session_key = validate_session_key(session_key)
    details_object = json.loads(read_json_argument(details))
    if details_object.get("action_type") != "send_rendering":
        raise ValueError("rendering approval action_type must be send_rendering")
    if details_object.get("estimate_id") != estimate_id:
        raise ValueError("rendering approval estimate_id does not match")
    images = details_object.get("images") or []
    if not images:
        raise ValueError("rendering approval needs at least one image")
    payload = json.dumps(details_object, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    customer = str(details_object.get("customer_email") or "unknown")[:120]
    piece = str(details_object.get("piece") or "their estimate")[:120]
    rows = {
        "Customer": customer,
        "Piece": piece,
        "Images": f"{len(images)} view(s), sent to you in chat just before this card",
        "Approve means": "Email these renderings to the customer in their thread, with the note that the written specification controls the piece.",
        "Reject means": "Nothing is sent; tell the desk what to change if you want new views.",
        "Estimate": estimate_id,
    }
    return [
        "kolo", "request-approval", "--agent-id", agent_id,
        "--action", f"Send renderings: {piece}"[:120],
        "--reasoning", (
            f"{customer} asked to see the design. The desk generated {len(images)} view(s) of the approved "
            "specification and sent them to you in chat. Approve to email them; reject to hold them."
        ),
        "--risk-level", "medium",
        "--details", json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--execution-payload", payload,
        "--session-key", session_key,
    ]


def request_rendering_approval_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str | None,
    action_key: str,
    estimate_id: str,
    details: Path,
    session_key: str,
    agent_id: str = "main",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """File the rendering approval once, journaled like every external action."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    command = build_request_rendering_approval(estimate_id, details, session_key, agent_id)
    binding_material = json.dumps(
        {"agent_id": agent_id, "details": json.loads(read_json_argument(details)),
         "estimate_id": estimate_id, "session_key": session_key},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    binding = "sha256:" + hashlib.sha256(binding_material).hexdigest()
    acquired, state = inbox_claim.acquire_external_action(
        claim_root, message_id, claim_token, action_key, "approval_request", binding,
    )
    if not acquired:
        status = state["external_actions"][action_key]["status"]
        if status == "sent":
            return subprocess.CompletedProcess(command, 0, "rendering approval already sent\n", "")
        raise ValueError(f"rendering approval is already {status}; refusing retry")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_external_action(claim_root, message_id, claim_token, action_key, "uncertain")
        raise
    inbox_claim.finish_external_action(claim_root, message_id, claim_token, action_key, "sent")
    return result


def send_owner_preview(monitor_root: Path, text: str, image: Path,
                       runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    """Show the owner one rendering in their channel (PNG inline)."""
    run_command(["kolo", "notify-owner", "-m", text, "--file", str(image), *owner_channel_args(monitor_root)], runner=runner)


REVIEW_REASON_TEXT = {
    "identity_has_active_estimate_on_another_thread": (
        "This sender already has an open estimate on a different email thread. "
        "Decide whether this is a new piece, a duplicate, or a reply that belongs "
        "with the existing estimate."
    ),
    "uncertain_classification": "The desk could not tell what this message asks for.",
    "classification_uncertain": (
        "A reply arrived after an estimate was sent, and the desk could not tell "
        "whether it accepts, asks for a meeting or a picture, changes the design, "
        "or asks for something new."
    ),
    "invalid_cost_components": (
        "Pricing stopped because a rate is missing from the rate card. Add the "
        "rate in the shop settings and answer the customer yourself; the desk "
        "does not retry on its own."
    ),
    "uncorrelated_dsn": "A delivery failure bounced back that does not match a known estimate.",
    "not_an_estimate_request": (
        "The customer asked for something the desk does not do: an appraisal, "
        "an inventory price, or a job status. Answer it yourself."
    ),
    "specification_incomplete_after_followup": (
        "Two rounds of questions did not complete the specification. Decide "
        "whether to ask again yourself or close it."
    ),
    "spot_price_unavailable": "The live metal price could not be fetched, so pricing stopped.",
    "same_sender_question": "The desk asked you whether this is the same piece or a new one.",
    "rendering_approval": "The desk is waiting for your approval to send renderings.",
    "owner_rejected_rendering": "You held the renderings back.",
    "unclear_reply_question": "The desk asked you what this reply meant.",
    "customer_escalation": (
        "The customer is upset, disputing, or asking for something the desk must "
        "not answer on its own. Please take this conversation over."
    ),
    "classification_malformed": (
        "The desk could not produce a usable reading of this message after two "
        "tries. Please read it yourself."
    ),
    "missing_thread_ownership": "The desk could not prove which estimate this thread belongs to.",
    "intake_failed": "A system error stopped intake before any customer contact.",
    "system_actionable": "A mailbox, authentication, or system failure needs attention.",
    "rendering_generation_timeout": "Rendering images did not finish in time.",
    "rendering_validation_failed": "The generated renderings did not match the approved design.",
    "stale_processing_retry_exhausted": "Processing stopped twice before finishing; check the thread.",
    "stale_processing_ambiguous": "Processing stopped mid-action and cannot be resumed safely.",
}


def review_reason_text(reason_code: str) -> str:
    return REVIEW_REASON_TEXT.get(reason_code, reason_code.replace("_", " "))


def _sender_display(value: str) -> str:
    """'Pat Doe <pat@example.net>' -> 'Pat Doe'; bare addresses pass through."""
    value = " ".join((value or "").split())
    match = re.fullmatch(r'"?([^"<]*?)"?\s*<([^>]+)>', value)
    if match:
        name, address = match.group(1).strip(), match.group(2).strip()
        return name or address
    return value


def build_request_review_approval(
    review_key: str,
    reason_code: str,
    message_id: str,
    session_key: str,
    headers: dict[str, str] | None = None,
    agent_id: str = "main",
) -> list[str]:
    """File one manual-review item as a Kolo approval brief.

    The approval card is a yes/no control, so the brief asks one yes/no
    question: did you handle this email? The title names the sender and
    subject so the owner can find the email in the shop inbox without
    opening anything else; the details repeat them as flat text fields
    (nested objects render as noise in the card), say what to do, and spell
    out what approve and reject mean. The execution payload lets the
    approval executor close the review when the owner approves.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", review_key or ""):
        raise ValueError("review_key must be a lowercase SHA-256 value")
    if not re.fullmatch(r"[a-z0-9_]{3,64}", reason_code or ""):
        raise ValueError("invalid review reason code")
    session_key = validate_session_key(session_key)
    headers = headers or {}
    sender = (headers.get("From") or "").strip()
    subject = " ".join((headers.get("Subject") or "").split())
    received = (headers.get("Date") or "").strip()
    who = _sender_display(sender)[:60] if sender else "an unknown sender"
    title = f"Check email from {who}"
    if subject:
        title += f": {subject[:90]}"
    why = review_reason_text(reason_code)
    details = {
        "What to do": (
            "Open this email in the shop inbox and handle it yourself, "
            "then answer here."
        ),
        "From": sender[:120] or "unknown",
        "Subject": subject[:160] or "unknown",
        "Received": received[:60] or "unknown",
        "Why it needs you": why,
        "Approve": "Yes, I handled this email. The desk closes the review.",
        "Reject": "Not yet. The review stays on the list and nothing changes.",
        "Review key": review_key[:12],
    }
    payload = {
        "action_type": "manual_review",
        "review_key": review_key,
        "reason_code": reason_code,
        "gmail_message_id": message_id,
    }
    return [
        "kolo",
        "request-approval",
        "--agent-id",
        agent_id,
        "--action",
        title,
        "--reasoning",
        f"{why} Open the email in the shop inbox and handle it yourself. "
        "Approve = yes, I handled it (the desk closes this review). "
        "Reject = not yet (it stays on the review list).",
        "--risk-level",
        "low",
        "--details",
        json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--execution-payload",
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "--session-key",
        session_key,
    ]


def build_update_brief(brief_id: str, status: str, result: dict[str, Any]) -> list[str]:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", brief_id or ""):
        raise ValueError("invalid brief id")
    if status not in {"executed", "failed"}:
        raise ValueError("brief status must be executed or failed")
    return [
        "kolo",
        "update-brief",
        "--brief-id",
        brief_id,
        "--status",
        status,
        "--execution-result",
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ]


def owner_channel_args(monitor_root: Path | None) -> list[str]:
    """Extra notify-owner arguments for the channel the owner chose at setup.

    The shop profile may carry `owner_channel.session_key`; when it does, every
    owner-facing message is addressed there instead of the default main chat.
    """
    if monitor_root is None:
        return []
    try:
        profile = json.loads((monitor_root.resolve().parent / "shop-profile.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    channel = profile.get("owner_channel") if isinstance(profile, dict) else None
    key = channel.get("session_key") if isinstance(channel, dict) else None
    if not (isinstance(key, str) and key.strip()):
        # No channel chosen at setup: use the thread that activated the desk,
        # which is where the approval cards appear, so everything the owner
        # sees is in one place.
        key = _activation_session_key(monitor_root)
    if isinstance(key, str) and key.strip():
        try:
            return ["--session-key", validate_session_key(key.strip())]
        except ValueError:
            return []
    return []


def _activation_session_key(monitor_root: Path) -> str | None:
    try:
        import activation_binding

        return activation_binding.load(activation_binding.binding_path(monitor_root))["session_key"]
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ImportError):
        return None


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


def build_record_upsert(
    record_type: str, external_id: str, payload: Path, status: str
) -> list[str]:
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
    claim_token: str | None,
    action_key: str,
    estimate_id: str,
    details: Path,
    session_key: str,
    agent_id: str = "main",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Create one approval request with durable ambiguity tracking."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
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


def request_appointment_approval_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str | None,
    action_key: str,
    estimate_id: str,
    details: Path,
    session_key: str,
    agent_id: str = "main",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Create one durable appointment approval with ambiguity tracking."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    command = build_request_appointment_approval(
        estimate_id, details, session_key, agent_id
    )
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
                command, 0, "appointment approval already sent\n", ""
            )
        raise ValueError(f"appointment approval is already {status}; refusing retry")
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
    claim_token: str | None,
    notification_key: str,
    estimate_id: str,
    event: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Send one claimed-message notification with durable ambiguity tracking."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    command = build_notify_owner(estimate_id, event)
    acquired, state = inbox_claim.acquire_notification(
        claim_root, message_id, claim_token, notification_key
    )
    if not acquired:
        status = state["owner_notification"]["status"]
        return subprocess.CompletedProcess(
            command, 0, f"notification already {status}\n", ""
        )
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
    claim_token: str | None,
    notification_key: str,
    event: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Send one generic monitor notification with durable ambiguity tracking."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    command = build_notify_monitor(event)
    acquired, state = inbox_claim.acquire_notification(
        claim_root,
        message_id,
        claim_token,
        notification_key,
        notification_field="manual_review_notification",
    )
    if not acquired:
        status = state["manual_review_notification"]["status"]
        return subprocess.CompletedProcess(
            command, 0, f"notification already {status}\n", ""
        )
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_notification(
            claim_root,
            message_id,
            claim_token,
            "uncertain",
            notification_field="manual_review_notification",
        )
        raise
    # `sent` records successful CLI acceptance. Kolo exposes no independent
    # user-visible delivery receipt for this command.
    inbox_claim.finish_notification(
        claim_root,
        message_id,
        claim_token,
        "sent",
        notification_field="manual_review_notification",
    )
    return result


def _headers_from_gmail_message(message: dict[str, Any]) -> dict[str, str]:
    headers = (message.get("payload") or {}).get("headers") or []
    wanted: dict[str, str] = {}
    for header in headers:
        if isinstance(header, dict) and header.get("name") in ("From", "Subject", "Date"):
            value = header.get("value")
            if isinstance(value, str) and header["name"] not in wanted:
                wanted[header["name"]] = value.strip()
    return wanted


def claimed_message_headers(
    monitor_root: Path, claim_root: Path, message_id: str
) -> dict[str, str]:
    """Best-effort From/Subject/Date of the claimed message, for the brief only.

    Reads the fetched message in the claim work directory while the claim is
    processing; after that (a backfilled brief, a re-filed one) it falls back
    to the work file if it still exists, then to the estimate record that the
    message opened, so the owner always sees who wrote and about what.
    """
    try:
        paths = inbox_monitor.prepare_claim_work(monitor_root, claim_root, message_id)
        message_path = Path(paths["gmail_message"])
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        message_path = (
            monitor_root.resolve().parent
            / "work"
            / inbox_monitor.message_key(message_id)
            / inbox_monitor.WORK_ARTIFACTS["gmail_message"]
        )
    try:
        message = json.loads(message_path.read_text(encoding="utf-8"))
        found = _headers_from_gmail_message(message)
        if found:
            return found
    except (OSError, ValueError, json.JSONDecodeError, KeyError):
        pass
    try:
        record_root = monitor_root.resolve().parent / "records"
        matches = estimate_record.lookup_by_initiating_message(record_root, message_id)
    except (OSError, ValueError, KeyError):
        return {}
    if not matches:
        return {}
    route = matches[0].get("route") or {}
    found = {}
    if isinstance(route.get("recipient"), str) and route["recipient"]:
        found["From"] = route["recipient"]
    if isinstance(route.get("original_subject"), str) and route["original_subject"]:
        found["Subject"] = route["original_subject"]
    return found


def review_brief_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    claim_token: str,
    review_key: str,
    reason_code: str,
    headers: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """File the review brief once, journaled like any other owner notice."""
    approver = activation_binding.load(activation_binding.binding_path(monitor_root))
    command = build_request_review_approval(
        review_key, reason_code, message_id, approver["session_key"], headers
    )
    acquired, state = inbox_claim.acquire_notification(
        claim_root,
        message_id,
        claim_token,
        f"manual_review_brief:{reason_code}:{message_id}",
        notification_field="manual_review_notification",
    )
    if not acquired:
        status = state["manual_review_notification"]["status"]
        return subprocess.CompletedProcess(command, 0, f"notification already {status}\n", "")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_notification(
            claim_root, message_id, claim_token, "uncertain",
            notification_field="manual_review_notification",
        )
        raise
    inbox_claim.finish_notification(
        claim_root, message_id, claim_token, "sent",
        notification_field="manual_review_notification",
    )
    return result


def review_notice_text(reason_code: str, headers: dict[str, str]) -> str:
    who = _sender_display(headers.get("From", "")) if headers.get("From") else "a customer"
    subject = " ".join((headers.get("Subject") or "").split())[:90]
    about = f' about "{subject}"' if subject else ""
    return (
        f"I could not finish an email from {who}{about}. {review_reason_text(reason_code)} "
        "I have stepped back from that thread; please handle it yourself."
    )


def review_notice_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    claim_token: str,
    reason_code: str,
    headers: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Tell the owner once, in their channel, that the desk stepped back."""
    command = ["kolo", "notify-owner", "-m", review_notice_text(reason_code, headers), *owner_channel_args(monitor_root)]
    acquired, state = inbox_claim.acquire_notification(
        claim_root, message_id, claim_token,
        f"manual_review_notice:{reason_code}:{message_id}",
        notification_field="manual_review_notification",
    )
    if not acquired:
        status = state["manual_review_notification"]["status"]
        return subprocess.CompletedProcess(command, 0, f"notification already {status}\n", "")
    try:
        result = run_command(command, runner=runner)
    except (OSError, subprocess.CalledProcessError):
        inbox_claim.finish_notification(
            claim_root, message_id, claim_token, "uncertain",
            notification_field="manual_review_notification",
        )
        raise
    inbox_claim.finish_notification(
        claim_root, message_id, claim_token, "sent",
        notification_field="manual_review_notification",
    )
    return result


def manual_review_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    claim_token: str | None,
    reason_code: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    """Persist manual review, then put it in front of the owner as a brief.

    The brief goes to the approval queue, the one place the owner already
    checks. When no activation binding exists (an older installation), the
    generic chat alert is used instead so nothing is lost silently.
    """
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    headers = claimed_message_headers(monitor_root, claim_root, message_id)
    queue_item = inbox_monitor.finalize_item(
        monitor_root,
        message_id,
        claim_root,
        claim_token,
        "manual_review",
        reason_code,
    )
    binding = activation_binding.binding_path(monitor_root)
    if binding.exists():
        # A desk failure is a plain notice in the owner's channel, not a
        # card to click: who wrote, what stopped, and that the desk stepped
        # back from the thread. Reviews that need a decision are questions.
        result = review_notice_claimed(
            monitor_root, claim_root, message_id, claim_token, reason_code, headers, runner=runner,
        )
    else:
        result = notify_monitor_claimed(
            claim_root,
            message_id,
            claim_token,
            f"manual_review:{reason_code}:{message_id}",
            "manual-review",
            runner=runner,
        )
    return queue_item, result


def complete_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    claim_token: str | None,
) -> dict[str, Any]:
    """Terminalize a deterministically filtered message without side effects."""
    if claim_token is None:
        claim_token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    inbox_claim.advance_phase(
        claim_root, message_id, claim_token, "ready_to_finalize"
    )
    return inbox_monitor.finalize_item(
        monitor_root,
        message_id,
        claim_root,
        claim_token,
        "processed",
    )


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
    approval_claimed.add_argument("--claim-token")
    approval_claimed.add_argument("--action-key", required=True)
    approval_claimed.add_argument("--estimate-id", required=True)
    approval_claimed.add_argument("--details", type=Path, required=True)
    approval_claimed.add_argument("--session-key", required=True)
    approval_claimed.add_argument("--agent-id", default="main")
    appointment_approval_claimed = sub.add_parser(
        "request-appointment-approval-claimed"
    )
    appointment_approval_claimed.add_argument("--claim-root", type=Path, required=True)
    appointment_approval_claimed.add_argument("--message-id", required=True)
    appointment_approval_claimed.add_argument("--claim-token")
    appointment_approval_claimed.add_argument("--action-key", required=True)
    appointment_approval_claimed.add_argument("--estimate-id", required=True)
    appointment_approval_claimed.add_argument("--details", type=Path, required=True)
    appointment_approval_claimed.add_argument("--session-key", required=True)
    appointment_approval_claimed.add_argument("--agent-id", default="main")

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
    notify_claimed.add_argument("--claim-token")
    notify_claimed.add_argument("--notification-key", required=True)
    notify_claimed.add_argument("--estimate-id", required=True)
    notify_claimed.add_argument(
        "--event", choices=sorted(OWNER_NOTIFICATION_MESSAGES), required=True
    )
    notify_monitor_claimed_parser = sub.add_parser("notify-monitor-claimed")
    notify_monitor_claimed_parser.add_argument("--claim-root", type=Path, required=True)
    notify_monitor_claimed_parser.add_argument("--message-id", required=True)
    notify_monitor_claimed_parser.add_argument("--claim-token")
    notify_monitor_claimed_parser.add_argument("--notification-key", required=True)
    notify_monitor_claimed_parser.add_argument(
        "--event", choices=sorted(MONITOR_NOTIFICATION_MESSAGES), required=True
    )
    manual_review_parser = sub.add_parser("manual-review-claimed")
    manual_review_parser.add_argument("--monitor-root", type=Path, required=True)
    manual_review_parser.add_argument("--claim-root", type=Path, required=True)
    manual_review_parser.add_argument("--message-id", required=True)
    manual_review_parser.add_argument("--claim-token")
    manual_review_parser.add_argument("--reason-code", required=True)
    complete_parser = sub.add_parser("complete-claimed")
    complete_parser.add_argument("--monitor-root", type=Path, required=True)
    complete_parser.add_argument("--claim-root", type=Path, required=True)
    complete_parser.add_argument("--message-id", required=True)
    complete_parser.add_argument("--claim-token")
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
        elif args.command == "request-appointment-approval-claimed":
            result = request_appointment_approval_claimed(
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
            )["manual_review_notification"]
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
        elif args.command == "complete-claimed":
            print(
                json.dumps(
                    complete_claimed(
                        args.monitor_root,
                        args.claim_root,
                        args.message_id,
                        args.claim_token,
                    ),
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
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
