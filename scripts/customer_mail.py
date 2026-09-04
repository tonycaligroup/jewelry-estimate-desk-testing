#!/usr/bin/env python3
"""Customer emails written for the thread, checked before they go.

Batch 3 replaced the main session's drafting with fixed paragraphs, which
made every email read the same. Here a cheap stateless model call writes
each outbound email from the whole thread, the approved facts, and the
shop's voice; deterministic checks then require the exact figures and the
substance that must survive, and the fixed text is the fallback when the
draft fails twice. Nothing here changes what was approved.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Callable
from urllib.parse import quote

import customer_content_guard
import gmail_fetch
import gmail_text
import judge

Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_VOICE = (
    "Warm and plain, like a note from the person at the bench. Short sentences, first names when the "
    "customer used theirs, no sales language, no exclamation marks. Sign with the shop name."
)
THREAD_CHAR_LIMIT = 12_000

# Ideas the estimate email must carry in its own words (templates/approved-estimate-note.md).
HIGH_SIDE_IDEAS = {
    "estimated high on purpose": r"high (?:end|side)|on the (?:high|generous) side|estimate(?:d)? high",
    "pending final design approval": r"pending|once (?:the |your )?design|final design|until (?:the |your )?design",
    "final price can come in lower and savings are passed on": r"lower|come(?:s)? in under|pass(?:ed)? (?:that |it |the difference )?(?:along|on|straight)",
    "nothing is committed until the customer approves the final design": r"nothing (?:is )?(?:locked|committed|final)|no commitment|until you(?:'ve| have)? (?:seen|approved)",
}


def fetch_thread_digest(record: dict[str, Any], message_id: str, mailbox: str | None, token: str,
                        opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """The customer's whole thread as the model sees it, oldest first."""
    kwargs = {"opener": opener} if opener else {}
    thread = gmail_fetch.fetch_json(
        f"threads/{quote(record['route']['thread_id'], safe='')}", {"format": "full"}, token, **kwargs
    )
    return gmail_text.thread_digest(thread, message_id, mailbox)


def _thread_block(digest: dict[str, Any]) -> str:
    text = judge.thread_text(digest)
    if len(text) > THREAD_CHAR_LIMIT:
        text = "[earlier messages trimmed]\n" + text[-THREAD_CHAR_LIMIT:]
    return text


def _last_desk_email(digest: dict[str, Any]) -> str:
    for message in reversed(digest.get("messages") or []):
        if message.get("sent_by") == "shop":
            return str(message.get("body") or "")[:1500]
    return ""


def _check(kind: str, facts: dict[str, Any], previous: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def check(value: dict[str, Any]) -> dict[str, Any]:
        body = value.get("body")
        if not isinstance(body, str) or len(body.strip()) < 120:
            raise ValueError("body must be the full email text, at least a few sentences")
        body = customer_content_guard.plain_text(body.strip())
        if len(body) > 3000:
            raise ValueError("body is too long; keep it under 3000 characters")
        if "{{" in body or "}}" in body or "<" in body and ">" in body:
            raise ValueError("body must not contain placeholders or markup")
        customer_content_guard.validate_customer_text(body)
        if kind == "estimate":
            approved = float(str(facts["price"]).replace("$", "").replace(",", ""))
            customer_content_guard.validate_approved_price(body, approved)
            missing = [idea for idea, pattern in HIGH_SIDE_IDEAS.items() if not re.search(pattern, body, re.IGNORECASE)]
            if len(missing) > 1:
                raise ValueError("the estimate must say, in your own words: " + "; ".join(missing))
            if facts.get("valid_through") and facts["valid_through"] not in body:
                raise ValueError(f"the estimate must say it is good through {facts['valid_through']}")
        else:
            if customer_content_guard.DOLLAR_AMOUNT_RE.search(body):
                raise ValueError("this email must not mention any dollar amount")
        for label in facts.get("time_labels") or []:
            if label not in body:
                raise ValueError(f"the email must state this time exactly as written: {label}")
        first = body.strip().splitlines()[0].strip().lower()
        if previous and first and first == previous.strip().splitlines()[0].strip().lower() and len(first) > 12:
            raise ValueError("do not open with the same line as the last email on this thread")
        return {"body": body}
    return check


KIND_BRIEFS = {
    "estimate": (
        "Send the customer their estimate. State the price exactly as given, once. Say, in your own words, "
        "that the figure is estimated on the high side on purpose, that it is pending final design approval, "
        "that the final price often comes in lower and any saving is passed to them, and that nothing is "
        "committed until they approve the final design. Mention the lead time if one is given, and the date "
        "the estimate is good through. Invite them to reply to set up a time to go over the design. Do not "
        "list the specification back to them line by line; refer to the piece naturally."
    ),
    "confirmation": (
        "Confirm the appointment at exactly the time given (write the time exactly as provided). Say a calendar "
        "invitation is on its way to this address. Say what the meeting is for. Say to reply if the time stops "
        "working. No prices."
    ),
    "reschedule": (
        "Confirm that the appointment has been moved to exactly the time given (write it exactly as provided), "
        "that the earlier invitation is cancelled and a new one is on its way, and to reply if it stops working. "
        "No prices."
    ),
    "offer": (
        "Offer the customer exactly these meeting times, each written exactly as provided, one per line, and "
        "ask them to reply with the one that works or say what does. Nothing is booked yet. No prices."
    ),
    "rendering": (
        "Send the attached design renderings. Say they illustrate the design direction discussed, that the "
        "written specification and the final design they approve control the finished piece, and that they can "
        "reply with anything they would like changed. No prices."
    ),
}


def draft(
    kind: str,
    facts: dict[str, Any],
    digest: dict[str, Any],
    profile: dict[str, Any],
    fallback: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> tuple[str, str]:
    """(body, source): source is "model" or "fallback". Never raises."""
    if kind not in KIND_BRIEFS:
        raise ValueError("unsupported email kind")
    shop = profile.get("shop") or {}
    voice = str(shop.get("voice") or DEFAULT_VOICE)
    shop_name = str(shop.get("name") or "the shop")
    previous = _last_desk_email(digest)
    fact_lines = "\n".join(f"- {key}: {value}" for key, value in facts.items() if value not in (None, "", []))
    prompt = (
        f"You write customer emails for {shop_name}, a retail custom-jewelry shop. Voice: {voice}\n\n"
        f"TASK: {KIND_BRIEFS[kind]}\n\n"
        "Write the reply body only: no subject, no headers, plain text, no markdown, no bullet symbols other "
        "than a dash, no prices other than the one given (if any). Read the whole thread and answer as the "
        "next message in it: use the customer's name if they gave one, refer to what they said, and do not "
        "repeat the wording of the shop's earlier emails.\n\n"
        f"FACTS (use exactly):\n{fact_lines}\n\n"
        + (f"THE SHOP'S LAST EMAIL (do not reuse its opening or closing):\n{previous}\n\n" if previous else "")
        + f"THREAD:\n{_thread_block(digest)}\n\n"
        'Answer with one JSON object only: {"body": "..."}'
    )
    try:
        out = judge.ask_json(prompt, _check(kind, facts, previous), model, runner, openclaw)
        return out["body"], "model"
    except (judge.JudgmentError, ValueError, KeyError):
        return fallback, "fallback"
