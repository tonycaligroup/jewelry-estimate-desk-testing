#!/usr/bin/env python3
"""Conservatively classify deterministic Gmail system messages from headers."""

from __future__ import annotations

import argparse
import json
import re
import sys
from email.utils import getaddresses
from pathlib import Path
from typing import Any


EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+$")
AUTO_REPLY_SUBJECT_RE = re.compile(
    r"^\s*(automatic reply|auto reply|out of office|away from the office)\s*:?",
    re.IGNORECASE,
)
# Google Calendar and other calendar clients prefix every event mail with one
# of these words. The colon is required so a customer subject such as
# "Invitation ring for my sister" is not swept up.
CALENDAR_SUBJECT_RE = re.compile(
    r"^\s*(?:re:\s*|fwd?:\s*)*"
    r"(invitation|updated invitation|cancell?ed event|canceled|accepted|declined|"
    r"tentatively accepted|new event|event reminder|reminder)\s*:",
    re.IGNORECASE,
)
CALENDAR_MIME_TYPES = {"text/calendar", "application/ics"}
CALENDAR_SENDERS = {"calendar-notification@google.com"}
# Machine senders at Google that never carry a customer's words. Google Forms
# receipts are deliberately absent: a form-built contact page relays real
# inquiries through forms-receipts-noreply@google.com.
AUTOMATED_GOOGLE_SENDERS = {
    "gemini-notes@google.com",
    "meet-recordings-noreply@google.com",
    "drive-shares-noreply@google.com",
    "drive-shares-dm-noreply@google.com",
    "comments-noreply@docs.google.com",
    "calendar-notification@google.com",
}
# Relays that carry a customer's own words and must stay routable.
FORM_RELAY_SENDERS = {"forms-receipts-noreply@google.com"}
# Mailbox providers whose domain says nothing about who the sender works for.
# A shop that sends from one of these gets no same-domain filtering at all.
PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
    "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "pm.me", "comcast.net",
    "att.net", "verizon.net", "sbcglobal.net", "mail.com", "zoho.com",
}


def header_values(message: dict[str, Any], name: str) -> list[str]:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Gmail message payload must be an object")
    headers = payload.get("headers")
    if not isinstance(headers, list):
        raise ValueError("Gmail message payload.headers must be an array")
    values: list[str] = []
    for header in headers:
        if not isinstance(header, dict):
            raise ValueError("each Gmail header must be an object")
        if str(header.get("name", "")).lower() == name.lower():
            value = header.get("value")
            if not isinstance(value, str):
                raise ValueError(f"Gmail {name} header must be text")
            values.append(value.strip())
    return values


def one_header(message: dict[str, Any], name: str, required: bool = False) -> str:
    values = header_values(message, name)
    if len(values) > 1:
        raise ValueError(f"Gmail message contains multiple {name} headers")
    if not values:
        if required:
            raise ValueError(f"Gmail message is missing {name} header")
        return ""
    return values[0]


def sender_address(message: dict[str, Any]) -> str:
    value = one_header(message, "From", required=True)
    parsed = [(name, address) for name, address in getaddresses([value]) if address]
    if len(parsed) != 1 or not EMAIL_RE.fullmatch(parsed[0][1]):
        raise ValueError("Gmail From header must contain exactly one email address")
    return parsed[0][1].lower()


def auto_submitted_keyword(value: str) -> str:
    """Return the RFC 3834 keyword without its optional parameters."""
    return value.split(";", 1)[0].strip().lower()


def mime_types(message: dict[str, Any]) -> set[str]:
    """Collect every part's declared MIME type, however deeply nested."""
    found: set[str] = set()
    stack: list[Any] = [message.get("payload")]
    while stack:
        part = stack.pop()
        if not isinstance(part, dict):
            continue
        mime = part.get("mimeType")
        if isinstance(mime, str) and mime:
            found.add(mime.split(";", 1)[0].strip().lower())
        parts = part.get("parts")
        if isinstance(parts, list):
            stack.extend(parts)
    return found


def shop_domain(outbound_mailbox: str | None) -> str | None:
    """Return the shop's own domain when it identifies the business.

    Coworkers at the shop's domain are not customers. A shop on a public
    mailbox provider shares its domain with the whole world, so the filter is
    switched off rather than applied.
    """
    if not isinstance(outbound_mailbox, str) or "@" not in outbound_mailbox:
        return None
    domain = outbound_mailbox.rsplit("@", 1)[1].strip().lower()
    if not domain or domain in PUBLIC_MAIL_DOMAINS:
        return None
    return domain


def classify(
    message: dict[str, Any], outbound_mailbox: str | None = None
) -> dict[str, str]:
    """Sort one Gmail message into a fixed class using only its headers.

    Every class except `customer_or_uncertain` is decided from mail-system
    headers that a customer never writes: calendar invitations, machine
    notifications, mailing-list mail, bounces, automatic replies, and mail
    from the shop's own staff. Anything a human customer might have typed
    stays `customer_or_uncertain` for the workflow to read.
    """
    sender = sender_address(message)
    subject = one_header(message, "Subject")
    auto_submitted = auto_submitted_keyword(one_header(message, "Auto-Submitted"))
    content_type = one_header(message, "Content-Type").lower()
    sender_local, sender_domain = sender.split("@", 1)
    dsn_sender = sender_local in {"mailer-daemon", "postmaster"}
    dsn_content = "report-type=delivery-status" in content_type
    dsn_subject = any(
        phrase in subject.lower()
        for phrase in ("delivery status notification", "undeliverable", "delivery failure")
    )
    # An RFC 6522 delivery-status report is authoritative on its own. Bounces are
    # not always sent from mailer-daemon or postmaster, and a bounce that is not
    # recognized here would otherwise be swallowed as an automatic reply, hiding
    # a failed estimate delivery from the owner.
    if dsn_content or (
        dsn_sender and (dsn_subject or auto_submitted == "auto-generated")
    ):
        return {"classification": "dsn_candidate", "reason_code": "delivery_status_headers"}

    # Only `auto-replied` marks this message as an automatic reply. Contact
    # forms, marketplace notifications, and other application senders stamp
    # `auto-generated` on genuine customer inquiries, so those must stay
    # routable rather than being completed with no reply and no owner alert.
    #
    # X-Auto-Response-Suppress is deliberately not consulted: it is a
    # sender-set directive asking recipients not to auto-reply, routinely
    # present on Exchange-originated and application-generated mail. It is not
    # evidence that this message is itself an automatic reply.
    auto_header = auto_submitted == "auto-replied"
    vendor_auto_header = bool(
        header_values(message, "X-Autoreply")
        or header_values(message, "X-Autorespond")
    )
    if auto_header or vendor_auto_header or AUTO_REPLY_SUBJECT_RE.match(subject):
        return {"classification": "auto_reply", "reason_code": "automatic_reply_headers"}

    # Calendar traffic: invitations, updates, cancellations, and RSVPs carry a
    # text/calendar part or a fixed subject prefix. A coworker's meeting is not
    # an estimate request, and neither is a customer accepting a consultation
    # slot; the booking flow records that from the calendar itself.
    calendar_part = bool(
        (mime_types(message) & CALENDAR_MIME_TYPES)
        or "text/calendar" in content_type
    )
    if calendar_part or sender in CALENDAR_SENDERS or CALENDAR_SUBJECT_RE.match(subject):
        return {"classification": "calendar_event", "reason_code": "calendar_headers"}

    # Google's own machine senders: meeting notes, recordings, document shares.
    google_machine = sender not in FORM_RELAY_SENDERS and sender_domain in {
        "google.com",
        "docs.google.com",
    } and (
        sender in AUTOMATED_GOOGLE_SENDERS
        or "noreply" in sender_local
        or "no-reply" in sender_local
        or "notification" in sender_local
    )
    if google_machine:
        return {
            "classification": "automated_notification",
            "reason_code": "automated_sender",
        }

    # Mailing lists, newsletters, and marketing carry list headers or a bulk
    # precedence that no personal message from a customer has.
    precedence = one_header(message, "Precedence").lower()
    if (
        header_values(message, "List-Unsubscribe")
        or header_values(message, "List-Id")
        or precedence in {"bulk", "list", "junk"}
    ):
        return {"classification": "bulk_mail", "reason_code": "list_headers"}

    # Staff at the shop's own domain write internal mail, not inquiries.
    own_domain = shop_domain(outbound_mailbox)
    if own_domain and sender_domain == own_domain:
        return {"classification": "internal_sender", "reason_code": "same_domain_sender"}

    return {"classification": "customer_or_uncertain", "reason_code": "requires_routing"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("message", type=Path)
    parser.add_argument("--shop-mailbox", default=None)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.message.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Gmail message must be a JSON object")
        print(json.dumps(classify(value, args.shop_mailbox), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
