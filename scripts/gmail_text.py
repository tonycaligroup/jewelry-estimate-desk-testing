#!/usr/bin/env python3
"""Plain-text view of a fetched Gmail message or thread, for the worker.

A worker used to open the raw Gmail JSON (nested MIME parts, base64url
bodies) with its read tool and decode it in its head: two model round trips
per claim and a good way to misread a thread. The watcher already fetched
the thread; this module turns it into what the model actually needs, a
chronological list of short plain-text messages, so `worker-start` can hand
it over inline and the worker never reads the Gmail files at all.
"""

from __future__ import annotations

import base64
import html
import re
from typing import Any

BODY_LIMIT = 6_000
DIGEST_LIMIT = 40_000

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


def header(message: dict[str, Any], name: str) -> str:
    payload = message.get("payload") or {}
    for item in payload.get("headers") or []:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            value = item.get("value")
            return value.strip() if isinstance(value, str) else ""
    return ""


def _decode(data: Any) -> str:
    if not isinstance(data, str) or not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    found = [part]
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            found.extend(_parts(child))
    return found


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    return html.unescape(_TAG_RE.sub("", markup))


def body_text(message: dict[str, Any], limit: int = BODY_LIMIT) -> str:
    """The message body as plain text: text/plain first, else stripped HTML."""
    payload = message.get("payload") or {}
    plain: list[str] = []
    rich: list[str] = []
    for part in _parts(payload):
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(_html_to_text(_decode(data)))
    text = _clean("\n".join(t for t in plain if t)) or _clean("\n".join(t for t in rich if t))
    if not text:
        snippet = message.get("snippet")
        text = html.unescape(snippet.strip()) if isinstance(snippet, str) else ""
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n[truncated]"
    return text


def _internal_date(message: dict[str, Any]) -> int:
    try:
        return int(message.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def _address(value: str) -> str:
    match = re.search(r"<([^>]+)>", value)
    return (match.group(1) if match else value).strip().lower()


def thread_digest(
    thread: dict[str, Any],
    claimed_id: str,
    mailbox: str | None = None,
    limit: int = DIGEST_LIMIT,
) -> dict[str, Any]:
    """Chronological plain-text messages plus the ids a review needs."""
    messages = [m for m in thread.get("messages") or [] if isinstance(m, dict)]
    messages.sort(key=lambda m: (_internal_date(m), str(m.get("id") or "")))
    shop = (mailbox or "").strip().lower()
    digest: list[dict[str, Any]] = []
    used = 0
    for message in messages:
        sender = header(message, "From")
        entry = {
            "gmail_message_id": message.get("id"),
            "from": sender,
            "sent_by": "shop" if shop and _address(sender) == shop else "customer",
            "date": header(message, "Date"),
            "internal_ms": _internal_date(message),
            "subject": header(message, "Subject"),
            "claimed": message.get("id") == claimed_id,
            "body": body_text(message),
        }
        used += len(entry["body"])
        if used > limit:
            entry["body"] = "[omitted: thread digest limit reached]"
        digest.append(entry)
    return {
        "thread_id": thread.get("id"),
        "message_ids": [m.get("id") for m in messages],
        "messages": digest,
    }


def sender_first_name(digest: dict[str, Any]) -> str:
    """The first name in the From display name of the customer message being handled ("David Trujillo <..>" -> "David"), else ''.

    A gift-giver names the person the piece is for, and a draft once opened
    "Hi Verónica" to David (live, 9 September 2026). The address line, not
    the body, says who is writing.
    """
    messages = digest.get("messages") or []
    claimed = next((m for m in messages if m.get("claimed")), None) or next((m for m in reversed(messages) if m.get("sent_by") == "customer"), None)
    if not claimed:
        return ""
    sender = str(claimed.get("from") or "")
    match = re.match(r'\s*"?([^"<]+?)"?\s*<', sender)
    display = (match.group(1) if match else "").strip()
    if not display or "@" in display:
        return ""
    if "," in display:  # "Trujillo, David"
        display = display.split(",", 1)[1].strip() or display
    first = display.split()[0].strip(",.")
    return first if re.fullmatch(r"[^\W\d_][\w'’.-]*", first) else ""


GREETING_NAME_RE = re.compile(r"^(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\s+([^\W\d_][\w'’.-]*)\s*[,!.:]?\s*$", re.IGNORECASE)


def greets_someone_else(body: str, sender_name: str) -> str | None:
    """The name a greeting line addresses when it is not the sender's, else None."""
    if not sender_name:
        return None
    first = next((line.strip() for line in str(body or "").splitlines() if line.strip()), "")
    match = GREETING_NAME_RE.match(first)
    if not match:
        return None
    named = match.group(1)
    if named.lower().rstrip(".,") in _GENERIC_GREETINGS:
        return None
    return named if named.lower().rstrip(".,") != sender_name.lower() else None


_GENERIC_GREETINGS = {"there", "all", "everyone", "team", "friend", "friends", "folks", "sir", "madam", "again", "both"}
