#!/usr/bin/env python3
"""Plain-English questions to the owner, and what to do with the answers.

WORKFLOW.md 6.10: when the desk needs a fact only the owner has, it asks in
the owner's channel in plain words and the owner answers in plain words. A
question is never an approval and never a review item. Each question is one
private JSON file: what was asked, why, how it was delivered, and, once the
owner replies, the answer with its provenance. The inquiry that raised it is
parked until the answer arrives, then resumed.

The first kind is a missing rate: pricing found no rate on the card for a
metal or stone in the specification. The owner replies with a number, the
number is saved to the rate card so the question is never asked twice, and
the worker prices the piece.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
QUESTION_KINDS = {"missing_rate", "same_sender", "unclear_reply", "appointment_next", "followup_stalled", "command_failed"}
DECISION_KINDS = {"same_sender", "unclear_reply", "appointment_next", "followup_stalled", "command_failed"}
# Fixed outcomes per decision kind, with the words an owner is likely to use.
DECISION_OPTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "same_sender": {
        "same": ("same", "same piece", "same one", "existing", "that one", "yes", "it is the same"),
        "new": ("new", "new piece", "different", "separate", "another", "second", "no"),
    },
    "unclear_reply": {
        "second_piece": ("second piece", "another piece", "new piece", "additional", "second one", "separate piece"),
        "design_change": ("change", "changed", "changes", "modify", "modification", "different design", "update the design", "revise"),
        "accepts": ("accept", "accepts", "accepted", "go ahead", "approved", "wants it", "yes to the estimate", "take it", "they want it"),
        "handle_myself": ("handle", "i will", "i'll", "mine", "leave it", "myself", "i got it", "i have it", "skip"),
    },
    "followup_stalled": {
        "skip": ("skip", "price it", "go ahead", "without", "don't need", "do not need", "not needed", "jeweler's choice", "your call", "proceed"),
        "ask_again": ("ask again", "ask them again", "try again", "resend", "send it again", "ask once more"),
        "handle_myself": ("handle", "i will", "i'll", "mine", "leave it", "myself", "i got it", "i have it"),
    },
    "command_failed": {
        "retry": ("retry", "try again", "again", "run it again", "go", "go ahead", "yes"),
        "release": ("release", "undo", "cancel", "let it go", "drop it", "free the time", "remove the hold"),
        "handle_myself": ("handle", "i will", "i'll", "mine", "leave it", "myself", "i got it", "i have it", "skip"),
    },
    "appointment_next": {
        "times_given": ("offer", "try", "how about", "suggest", "propose", "these", "give them"),
        "offer_other_times": ("other times", "different times", "new times", "pick again", "something else", "other options"),
        "handle_myself": ("handle", "i will", "i'll", "mine", "leave it", "myself", "i got it", "i have it", "skip"),
    },
}
RATE_KINDS = {"metal_per_gram": "per gram", "stones_per_carat": "per carat"}
REMINDER_AFTER_SECONDS = 24 * 60 * 60
DELIVERY_STATUSES = {"pending", "sent", "uncertain"}
Runner = Callable[..., subprocess.CompletedProcess[str]]

_AMOUNT_RE = re.compile(r"(?<![A-Za-z0-9])\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![A-Za-z0-9])")


def questions_root(monitor_root: Path) -> Path:
    """Questions live beside the monitor, claims, and records."""
    return monitor_root.resolve().parent / "questions"


def question_id(estimate_id: str, kind: str, subject: str) -> str:
    digest = hashlib.sha256(f"{estimate_id}:{kind}:{subject}".encode("utf-8")).hexdigest()
    return f"q-{digest[:12]}"


def reference(qid: str) -> str:
    """The short code the owner can quote back: six characters, upper case."""
    return qid[2:8].upper()


def question_path(root: Path, qid: str) -> Path:
    if not re.fullmatch(r"q-[0-9a-f]{12}", qid or ""):
        raise ValueError("invalid question id")
    return root / f"{qid}.json"


def _now(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current


def _write(path: Path, value: Any) -> None:
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
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate(question: Any) -> dict[str, Any]:
    if not isinstance(question, dict) or question.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid question schema_version")
    required = {
        "question_id", "kind", "estimate_id", "gmail_message_id", "asked_at",
        "status", "delivery", "text",
    }
    if not required.issubset(question):
        raise ValueError("question is missing required fields")
    if question["kind"] not in QUESTION_KINDS:
        raise ValueError("unsupported question kind")
    if question["status"] not in {"open", "answered"}:
        raise ValueError("invalid question status")
    delivery = question["delivery"]
    if not isinstance(delivery, dict) or delivery.get("status") not in DELIVERY_STATUSES:
        raise ValueError("invalid question delivery state")
    if question["kind"] in DECISION_KINDS:
        options = question.get("options")
        if not isinstance(options, dict) or not options or set(options) != set(DECISION_OPTIONS[question["kind"]]):
            raise ValueError("decision question options do not match its kind")
    if question["kind"] == "missing_rate":
        rate = question.get("rate")
        if (
            not isinstance(rate, dict)
            or rate.get("rate_kind") not in RATE_KINDS
            or not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){0,5}", str(rate.get("rate_key") or ""))
        ):
            raise ValueError("missing_rate question needs a valid rate_kind and rate_key")
    return question


def load(root: Path, qid: str) -> dict[str, Any]:
    path = question_path(root, qid)
    return validate(json.loads(path.read_text(encoding="utf-8")))


def save(root: Path, question: dict[str, Any]) -> dict[str, Any]:
    validate(question)
    _write(question_path(root, question["question_id"]), question)
    return question


def list_questions(root: Path, status: str | None = None) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(root.glob("q-*.json")):
        try:
            question = validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if status is None or question["status"] == status:
            found.append(question)
    return sorted(found, key=lambda q: (q["asked_at"], q["question_id"]))


def find(root: Path, ref_or_id: str) -> dict[str, Any]:
    """Resolve a question id or the six-character reference the owner quoted."""
    value = (ref_or_id or "").strip()
    if re.fullmatch(r"q-[0-9a-f]{12}", value.lower()):
        return load(root, value.lower())
    code = re.sub(r"[^0-9A-Fa-f]", "", value).upper()
    matches = [q for q in list_questions(root) if reference(q["question_id"]) == code]
    if len(matches) != 1:
        raise ValueError(f"no single question matches '{ref_or_id}'")
    return matches[0]


def only_open(root: Path) -> dict[str, Any]:
    """The one open question, when there is exactly one; otherwise refuse.

    A dormant question (filed with an approval card, answered only if the
    owner rejects and replies with its code) does not count as open here.
    """
    open_questions = [q for q in list_questions(root, "open") if not q.get("dormant")]
    if len(open_questions) != 1:
        raise ValueError(
            f"{len(open_questions)} question(s) are open; name the one being answered"
        )
    return open_questions[0]


def summary_of_piece(specification: Any) -> str:
    """'a pendant in 14K white gold with a lab-grown sapphire 0.75 ct'."""
    import cost_components  # local import; cost_components does not depend on this module

    spec = specification if isinstance(specification, dict) else {}
    piece = str(spec.get("piece_type") or "").strip().lower()
    metal = cost_components.extract_metal(spec)
    stone = cost_components.extract_center_stone(spec)
    parts = [f"{'an' if piece[:1] in tuple('aeiou') else 'a'} {piece}" if piece else "a piece"]
    if metal.get("description"):
        parts.append(f"in {metal['description']}")
    if stone.get("description"):
        parts.append(f"with a {stone['description']}")
    return " ".join(parts)


def missing_rate_text(question: dict[str, Any], reminder: bool = False) -> str:
    rate = question["rate"]
    unit = RATE_KINDS[rate["rate_kind"]]
    who = question.get("customer_name") or "A customer"
    piece = question.get("piece_summary") or "a piece"
    lines = [
        f"{who} asked for a quote on {piece}. I do not have a {unit} price for "
        f"{rate['description']} on your rate card. What price {unit} should I use?",
        f'Reply with just the number, for example "use 450". '
        f"(Question {reference(question['question_id'])}, estimate "
        f"{question['estimate_id'].upper()})",
    ]
    if rate.get("candidates"):
        lines.insert(
            1,
            "Your card has these related rates, but none is a clear match: "
            + ", ".join(rate["candidates"]) + ".",
        )
    text = " ".join(lines)
    return f"Reminder, still waiting on this: {text}" if reminder else text


def create_missing_rate(
    root: Path,
    estimate_id: str,
    message_id: str,
    rate: dict[str, Any],
    customer_name: str | None,
    piece_summary: str,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """File the question once; a repeat returns the existing one unchanged."""
    if rate.get("rate_kind") not in RATE_KINDS:
        raise ValueError("unsupported rate kind")
    qid = question_id(estimate_id, "missing_rate", f"{rate['rate_kind']}:{rate['suggested_key']}")
    path = question_path(root, qid)
    if path.exists():
        return False, load(root, qid)
    question = {
        "schema_version": SCHEMA_VERSION,
        "question_id": qid,
        "kind": "missing_rate",
        "estimate_id": estimate_id,
        "gmail_message_id": message_id,
        "asked_at": _now(now).isoformat(),
        "status": "open",
        "customer_name": customer_name or None,
        "piece_summary": piece_summary,
        "rate": {
            "rate_kind": rate["rate_kind"],
            "rate_key": rate["suggested_key"],
            "description": rate["description"],
            "candidates": list(rate.get("candidates") or []),
        },
        "delivery": {"status": "pending"},
        "reminder": None,
        "text": "",
    }
    question["text"] = missing_rate_text(question)
    return True, save(root, question)


def create_decision(
    root: Path,
    kind: str,
    estimate_id: str,
    message_id: str,
    text: str,
    context: dict[str, Any] | None = None,
    now: datetime | None = None,
    dormant: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """File a fixed-outcome question once; a repeat returns the existing one.

    Dormant: filed alongside an approval card, never delivered on its own; the
    owner reaches it by replying with its code after rejecting the card.
    """
    if kind not in DECISION_KINDS:
        raise ValueError("unsupported decision kind")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("decision text must be non-empty")
    qid = question_id(estimate_id, kind, message_id)
    path = question_path(root, qid)
    if path.exists():
        return False, load(root, qid)
    question = {
        "schema_version": SCHEMA_VERSION,
        "question_id": qid,
        "kind": kind,
        "estimate_id": estimate_id,
        "gmail_message_id": message_id,
        "asked_at": _now(now).isoformat(),
        "status": "open",
        "options": {key: " / ".join(words[:2]) for key, words in DECISION_OPTIONS[kind].items()},
        "context": dict(context or {}),
        "delivery": {"status": "pending"},
        "reminder": None,
        "text": text.strip() + f" (Question {reference(qid)})",
        "dormant": bool(dormant),
    }
    return True, save(root, question)


TIME_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "mon", "tue",
              "wed", "thu", "fri", "sat", "sun", "tomorrow", "today", "morning", "afternoon", "noon", "am", "pm")


def _mentions_a_time(words: str) -> bool:
    return any(character.isdigit() for character in words) or any(f" {w} " in words for w in TIME_WORDS)


def match_option(question: dict[str, Any], answer: str) -> str:
    """The one fixed outcome the owner's words point to; refuse when unclear."""
    if question.get("kind") not in DECISION_KINDS:
        raise ValueError("not a decision question")
    words = " " + re.sub(r"[^a-z0-9' ]+", " ", (answer or "").lower()) + " "
    words = re.sub(r"\s+", " ", words)
    hits: dict[str, int] = {}
    for key, phrases in DECISION_OPTIONS[question["kind"]].items():
        if f" {key.replace('_', ' ')} " in words or f" {key} " in words:
            hits[key] = hits.get(key, 0) + 2
        for phrase in phrases:
            if f" {phrase} " in words:
                hits[key] = hits.get(key, 0) + 1
    if not hits and question.get("kind") == "appointment_next" and _mentions_a_time(words):
        # The owner typed times: "Tuesday 2pm or Wednesday at 11".
        return "times_given"
    if not hits:
        raise ValueError(
            "could not tell which answer was meant; reply with one of: "
            + ", ".join(DECISION_OPTIONS[question["kind"]])
        )
    ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        raise ValueError(
            "the answer matched more than one option; reply with one of: "
            + ", ".join(DECISION_OPTIONS[question["kind"]])
        )
    return ranked[0][0]


def record_decision(
    root: Path, question: dict[str, Any], text: str, outcome: str, now: datetime | None = None
) -> dict[str, Any]:
    if question["status"] != "open":
        raise ValueError("question is already answered")
    if outcome not in DECISION_OPTIONS[question["kind"]]:
        raise ValueError("outcome is not one of the question's options")
    question["status"] = "answered"
    question["answer"] = {
        "text": text.strip()[:400],
        "outcome": outcome,
        "answered_at": _now(now).isoformat(),
        "answered_by": "owner",
    }
    return save(root, question)


def question_text(question: dict[str, Any], reminder: bool = False) -> str:
    if question["kind"] == "missing_rate":
        text = missing_rate_text(question, reminder)
    else:
        text = question["text"]
        text = f"Reminder, still waiting on this: {text}" if reminder else text
    return with_answer_command(question, text)


def with_answer_command(question: dict[str, Any], text: str) -> str:
    """The exact command the desk session runs with the owner's words.

    The main Kolo session reads the owner's reply in the same thread as this
    message, so the command travels with the question and nothing is left to
    guess (WORKFLOW.md 6.10).
    """
    if not question.get("answer_command"):
        return text
    # One short tag; SKILL.md maps it to the answer-question command.
    return f"{text}\n\ndesk-answer {reference(question['question_id'])}"


def answer_command(base_dir: Path, workspace: Path, question_id: str) -> str:
    return (
        f"python3 {base_dir}/scripts/workflow_safe.py answer-question --workspace {workspace} "
        f"--base-dir {base_dir} --question {reference(question_id)} --answer '<owner reply>'"
    )


def notify_command(text: str, extra: list[str] | None = None) -> list[str]:
    return ["kolo", "notify-owner", "-m", text, *(extra or [])]


def deliver(
    root: Path,
    question: dict[str, Any],
    runner: Runner = subprocess.run,
    reminder: bool = False,
    now: datetime | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Send the question (or its one reminder) with write-ahead journaling.

    Kolo gives no delivery receipt. Once the command starts, a failure is
    ambiguous and is recorded as uncertain, never retried automatically; the
    owner can still ask for open questions in chat.
    """
    current = _now(now).isoformat()
    if reminder:
        if question["status"] != "open" or question.get("reminder") or question.get("dormant"):
            return question
        text = question_text(question, reminder=True)
        question["reminder"] = {"status": "pending", "attempted_at": current}
        field = "reminder"
    else:
        if question["delivery"]["status"] != "pending":
            return question
        text = question_text(question)
        question["delivery"] = {"status": "pending", "attempted_at": current}
        field = "delivery"
    save(root, question)
    try:
        runner(notify_command(text, extra_args), check=True, capture_output=True, text=True, shell=False)
    except (OSError, subprocess.CalledProcessError):
        question[field]["status"] = "uncertain"
        save(root, question)
        return question
    question[field]["status"] = "sent"
    question[field]["sent_at"] = _now(now).isoformat()
    save(root, question)
    return question


def due_reminders(
    root: Path, now: datetime | None = None, after_seconds: int = REMINDER_AFTER_SECONDS
) -> list[dict[str, Any]]:
    """Open questions old enough for their single reminder."""
    current = _now(now)
    due: list[dict[str, Any]] = []
    for question in list_questions(root, "open"):
        if question.get("reminder"):
            continue
        if question["delivery"]["status"] != "sent":
            continue
        asked = datetime.fromisoformat(question["asked_at"])
        if (current - asked).total_seconds() >= after_seconds:
            due.append(question)
    return due


def send_due_reminders(
    root: Path, runner: Runner = subprocess.run, now: datetime | None = None,
    extra_args: list[str] | None = None,
) -> int:
    sent = 0
    for question in due_reminders(root, now):
        deliver(root, question, runner=runner, reminder=True, now=now, extra_args=extra_args)
        sent += 1
    return sent


def parse_amount(text: str) -> float:
    """Read the one number in the owner's reply; refuse when there is not exactly one."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the answer is empty")
    cleaned = text.replace("’", "'")
    found: list[float] = []
    # The pattern accepts only numbers that stand alone, so a reference code
    # such as "3F9A2C" is never read as an amount.
    for match in _AMOUNT_RE.finditer(cleaned):
        whole = match.group(1).replace(",", "")
        fraction = match.group(2)
        found.append(float(f"{whole}.{fraction}" if fraction else whole))
    distinct = sorted(set(found))
    if not distinct:
        raise ValueError("no number found in the answer")
    if len(distinct) > 1:
        raise ValueError("more than one number in the answer; reply with one number")
    value = distinct[0]
    if value <= 0:
        raise ValueError("the rate must be greater than zero")
    return value


def save_rate(
    profile_path: Path,
    rate_kind: str,
    rate_key: str,
    value: float,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Put the answered rate on the card, with who answered and when."""
    if rate_kind not in RATE_KINDS:
        raise ValueError("unsupported rate kind")
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+){0,5}", rate_key or ""):
        raise ValueError("invalid rate key")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    pricing = profile.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("shop profile is missing its pricing block")
    card = pricing.get(rate_kind)
    if not isinstance(card, dict):
        card = {}
        pricing[rate_kind] = card
    card[rate_key] = round(float(value), 2)
    ledger = pricing.get("rate_provenance")
    if not isinstance(ledger, dict):
        ledger = {}
        pricing["rate_provenance"] = ledger
    ledger[f"{rate_kind}.{rate_key}"] = provenance
    _write(profile_path, profile)
    return profile


def record_answer(
    root: Path,
    question: dict[str, Any],
    text: str,
    value: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    if question["status"] != "open":
        raise ValueError("question is already answered")
    question["status"] = "answered"
    question["answer"] = {
        "text": text.strip()[:400],
        "value": round(float(value), 2),
        "answered_at": _now(now).isoformat(),
        "answered_by": "owner",
    }
    return save(root, question)


def answer_provenance(question: dict[str, Any]) -> dict[str, Any]:
    answer = question.get("answer") or {}
    return {
        "source": "owner_answer",
        "question_id": question["question_id"],
        "estimate_id": question["estimate_id"],
        "answered_at": answer.get("answered_at"),
        "answer_text": answer.get("text"),
    }


def supersede(root: Path, question: dict[str, Any], why: str) -> dict[str, Any]:
    """Close a dormant question that the owner never needed (the card was approved)."""
    if question["status"] != "open":
        return question
    question["status"] = "answered"
    question["answer"] = {"text": why[:400], "outcome": "superseded", "answered_at": _now(None).isoformat(),
                          "answered_by": "desk"}
    return save(root, question)
