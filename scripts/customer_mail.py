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
    "Warm and personal, like a note from the jeweler who will make the piece: react to what the customer "
    "shared before asking anything, short sentences, first names when the customer used theirs, generous "
    "with help, no sales language, no exclamation marks. Sign with the shop name."
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


GREETING_RE = re.compile(r"^(hi|hello|hey|dear|good (morning|afternoon|evening))\b", re.IGNORECASE)


_CLOSING_RE = re.compile(r"^(warmly|best|best regards|kind regards|regards|thanks|thank you|cheers|sincerely|talk soon|see you (then|soon))[,!.]?$", re.IGNORECASE)


def _parts(body: str, shop: str) -> tuple[str | None, list[str], str | None]:
    """(greeting, middle paragraphs, sign-off) of one email body."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(body or "").strip()) if p.strip()]
    greeting = None
    if paragraphs and GREETING_RE.match(paragraphs[0]) and len(paragraphs[0]) < 60 and "\n" not in paragraphs[0]:
        greeting = paragraphs.pop(0)
    signoff = None
    if paragraphs:
        last = paragraphs[-1]
        lines = last.split("\n")
        if len(lines) <= 3 and len(last) < 120 and (shop.lower() in last.lower() or _CLOSING_RE.match(lines[0].strip())):
            signoff = paragraphs.pop()
    return greeting, paragraphs, signoff


def merge_bodies(first: str, second: str, shop: str) -> str:
    """One email from two drafts written for the same customer message: one greeting, both middles, one sign-off.

    The owner, 9 September 2026: a booking and a rendering approved from one
    reply went out as two emails. The confirmation comes first, the pictures
    or the price after it.
    """
    g1, m1, s1 = _parts(first, shop)
    g2, m2, s2 = _parts(second, shop)
    pieces = [g1 or g2] + m1 + m2 + [s2 or s1]
    return "\n\n".join(p for p in pieces if p) + "\n"


def _opening(text: str) -> str:
    """The first real sentence after the greeting line, lowercased; empty when short."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if lines and GREETING_RE.match(lines[0]) and len(lines[0]) < 40:
        lines = lines[1:]
    if not lines:
        return ""
    sentence = re.split(r"(?<=[.!?])\s", lines[0], maxsplit=1)[0].strip().lower()
    return sentence if len(sentence) > 20 else ""


# A rendering email says the pictures guide, they do not promise (the owner, 9 September 2026).
SAYS_WILL_CALL_RE = re.compile(r"(?i)\b(?:I(?:'|’)ll|I will|we(?:'|’)ll|we will|I can|we can) (?:call|phone|ring) you\b|\bcall you at\b")
GUIDANCE_RE = re.compile(r"(?i)\b(?:for guidance|guidance only|as a guide|guide only|for reference only|for illustration)\b")
CLAIMS_A_MEETING_RE = re.compile(
    r"(?i)\b(?:reserved|booked|i have (?:you|us|it) down for|(?:confirm(?:ed|ing)|locked in|set aside) (?:our|your|the|that) "
    r"(?:meeting|appointment|time|slot)|(?:meeting|appointment) is (?:set|confirmed|booked)|see you (?:on|at) )"
)
REPAIR_AS_DESIGN_RE = re.compile(r"(?i)\b(?:design(?:ing)?|custom(?:ize|ized|isation|ization)?|perfect piece)\b")


def _check(kind: str, facts: dict[str, Any], previous: str, sender: str = "") -> Callable[[dict[str, Any]], dict[str, Any]]:
    def check(value: dict[str, Any]) -> dict[str, Any]:
        body = value.get("body")
        if not isinstance(body, str) or len(body.strip()) < 120:
            raise ValueError("body must be the full email text, at least a few sentences")
        body = customer_content_guard.plain_text(body.strip())
        other = gmail_text.greets_someone_else(body, sender)
        if other:
            raise ValueError(f"the email is to {sender}, who wrote it; do not greet {other}")
        if len(body) > 3000:
            raise ValueError("body is too long; keep it under 3000 characters")
        if "{{" in body or "}}" in body or "<" in body and ">" in body:
            raise ValueError("body must not contain placeholders or markup")
        customer_content_guard.validate_customer_text(body)
        if kind in ("estimate", "followup", "offer") and not facts.get("meeting booked") and CLAIMS_A_MEETING_RE.search(body):
            # Live, 9 September 2026: an estimate email said "Thursday at 11am, which I have reserved" with nothing booked.
            raise ValueError("never say a meeting time is reserved, booked, or confirmed: no meeting is booked; "
                             "if they named a time, say only that you will confirm it separately")
        if facts.get("this is a repair visit, not a new-piece design consultation") and REPAIR_AS_DESIGN_RE.search(body):
            raise ValueError("this is a repair visit: never describe it as designing or customizing a new piece")
        if kind in ("confirmation", "reschedule") and "this is a phone call, not a visit" not in facts and SAYS_WILL_CALL_RE.search(body):
            # Live, 9 September 2026: a visit's confirmation said "I'll call you at (310) 810-3004" because the
            # signature had a number. A visit is a visit.
            raise ValueError("this is a visit to the shop, not a phone call: never say you will call or phone them")
        if kind == "rendering" and not GUIDANCE_RE.search(body):
            raise ValueError("say the renderings are for guidance only: they show the direction of the design and "
                             "a close rendering is still not the finished piece")
        if kind == "estimate":
            approved = float(str(facts["price"]).replace("$", "").replace(",", ""))
            customer_content_guard.validate_approved_price(body, approved)
            missing = [idea for idea, pattern in HIGH_SIDE_IDEAS.items() if not re.search(pattern, body, re.IGNORECASE)]
            if len(missing) > 1:
                raise ValueError("the estimate must say, in your own words: " + "; ".join(missing))
            if facts.get("valid_through") and facts["valid_through"] not in body:
                raise ValueError(f"the estimate must say it is good through {facts['valid_through']}")
            if facts.get("updated") and "updated" not in body.lower():
                raise ValueError("this is an updated estimate after the customer's change; the email must say so")
        else:
            if customer_content_guard.DOLLAR_AMOUNT_RE.search(body):
                raise ValueError("this email must not mention any dollar amount")
        for label in facts.get("time_labels") or []:
            if label not in body:
                raise ValueError(f"the email must state this time exactly as written: {label}")
        hours = facts.get("consultation hours")
        if hours and str(hours) not in body:
            raise ValueError(f"the email must state the consultation hours exactly as written: {hours}")
        if previous and _opening(body) and _opening(body) == _opening(previous):
            raise ValueError("do not open with the same sentence as the last email on this thread")
        return {"body": body}
    return check


KIND_BRIEFS = {
    "estimate": (
        "Send the customer their estimate. State the price exactly as given, once. Say, in your own words, "
        "that the figure is estimated on the high side on purpose, that it is pending final design approval, "
        "that the final price often comes in lower and any saving is passed to them, and that nothing is "
        "committed until they approve the final design. Mention the lead time if one is given, and the date "
        "the estimate is good through. Invite them to reply to set up a time to go over the design. Do not "
        "list the specification back to them line by line; refer to the piece naturally. If the facts name details "
        "chosen by the jeweler, say in one sentence that you priced it with your own choice of those (name them "
        "plainly, for example stone color and clarity) and that they can tell you if they have a preference. Never "
        "say a meeting time is reserved, booked, or confirmed unless the facts name a booked meeting; if they "
        "named a time, say only that you will confirm it separately. If the facts say their visit is being "
        "confirmed separately, say only that and do not invite them to set up a time. If the facts say renderings are "
        "attached, say in one sentence that the attached renderings are for guidance only and show the direction of the "
        "design. When "
        "the facts say there is more than one piece, name each piece in a sentence and give the one total for all of them."
    ),
    "confirmation": (
        "Confirm the appointment at exactly the time given (write the time exactly as provided). If the facts say it "
        "is a phone call, confirm the call rather than a visit: say you will call them at the number given, or, when "
        "the facts say you do not have it, ask in one sentence for the best number to reach them. Say a calendar "
        "invitation is on its way to this address. Say what the meeting is for in a personal way: mention what "
        "they are bringing or planning if they said, and that you are looking forward to it. If the facts say "
        "the visit is to design the piece, say you look forward to designing their perfect piece together at the "
        "meeting; never mention an estimate, a quote, or that there is none yet, and do not ask for any detail "
        "now. Say to reply if the time stops working. No prices."
    ),
    "reschedule": (
        "Confirm that the appointment has been moved to exactly the time given (write it exactly as provided), "
        "that the earlier invitation is cancelled and a new one is on its way, and to reply if it stops working. "
        "No prices."
    ),
    "offer": (
        "Say you would be glad to meet, in a personal way that reacts to what they said, then offer exactly these "
        "meeting times, each written exactly as provided, one per line, and ask them to reply with the one that "
        "works or say what does. If the facts give consultation hours, say the time they asked for falls outside "
        "those hours and state the hours exactly as written before offering the times. If the facts say the visit "
        "is to design the piece, say you look forward to designing their perfect piece together when they come in; "
        "never mention an estimate, a quote, or that there is none yet, and do not ask for any detail now, "
        "unless the facts list details to ask: then, after the times, say that since they also asked about the "
        "price, a few details from them would get the estimate started (you are asking; never write that they "
        "need anything), ask for each listed detail as a short dash list of plain questions in the customer's "
        "words, one question per detail exactly as listed, and say it is fine not to know; never a technical question. "
        "If the facts say how to introduce the questions, use that introduction instead of the price wording. "
        "If the facts give their vision, confirm it before those questions in one sentence the way a jeweler speaks to "
        "a client (\"Just so I have your vision right: you are after ...\"), naming the piece, the stones, and the "
        "setting as given, and ask them to say if anything is off. "
        "Nothing is booked yet. No prices."
    ),
    "acknowledge": (
        "The customer asked for a price rather than a visit. Thank them warmly for the details, say you will work up "
        "the estimate yourself and get back to them shortly, and that you are glad to talk it through by phone or in "
        "person if they would like. Do not ask any design question. No prices, no dates, nothing promised beyond getting back to them."
    ),
    "rendering": (
        "Send the attached design renderings. Say warmly that the renderings are for guidance only: they show "
        "the direction of the design, a rendering that comes close is still not the finished piece, and small "
        "details may differ; the written specification and the final design they approve are what the shop "
        "makes. Invite them to reply with anything they would like changed. When the facts name more than one "
        "piece, say which views show which piece. No prices."
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
    customer_name: str = "",
) -> tuple[str, str]:
    """(body, source): source is "model" or "fallback". Never raises. `customer_name` is the owner's correction, when they made one."""
    if kind not in KIND_BRIEFS:
        raise ValueError("unsupported email kind")
    shop = profile.get("shop") or {}
    voice = str(shop.get("voice") or DEFAULT_VOICE)
    shop_name = str(shop.get("name") or "the shop")
    previous = _last_desk_email(digest)
    sender = (str(customer_name or "").strip().split() or [""])[0].strip(",.") or gmail_text.sender_first_name(digest)
    if sender:
        facts = {**facts, "the customer's name, from their address line": sender + " (address them by it, never by someone "
                 "else they mention, such as the person the piece is for)"}
    fact_lines = "\n".join(f"- {key}: {value}" for key, value in facts.items() if value not in (None, "", []))
    prompt = (
        f"You write customer emails for {shop_name}, a retail custom-jewelry shop. Voice: {voice}\n\n"
        f"TASK: {KIND_BRIEFS[kind]}\n\n"
        "Write the reply body only: no subject, no headers, plain text, no markdown, no bullet symbols other "
        "than a dash, no prices other than the one given (if any). Do not wrap lines: each paragraph is one "
        "line, with a blank line between paragraphs. Read the whole thread and answer as the "
        "next message in it: use the customer's name if they gave one, refer to what they said, and do not "
        "repeat the wording of the shop's earlier emails.\n\n"
        f"FACTS (use exactly):\n{fact_lines}\n\n"
        + (f"THE SHOP'S LAST EMAIL (do not reuse its opening or closing):\n{previous}\n\n" if previous else "")
        + f"THREAD:\n{_thread_block(digest)}\n\n"
        'Answer with one JSON object only: {"body": "..."}'
    )
    try:
        out = judge.ask_json(prompt, _check(kind, facts, previous, sender), model, runner, openclaw, temperature=judge.DRAFT_TEMPERATURE)
        return out["body"], "model"
    except (judge.JudgmentError, ValueError, KeyError):
        return fallback, "fallback"
