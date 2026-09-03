#!/usr/bin/env python3
"""Build a Gmail API payload that replies in the original customer thread."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Sequence

from customer_content_guard import plain_text, validate_customer_text
from gmail_route import email_identity_key


MESSAGE_ID_RE = re.compile(r"^<[^<>\s]+>$")


def require_text(route: dict[str, Any], field: str) -> str:
    value = route.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"route.{field} must be a non-empty string")
    if "\r" in value or "\n" in value:
        raise ValueError(f"route.{field} must not contain line breaks")
    return value.strip()


def require_email(route: dict[str, Any], field: str) -> str:
    value = require_text(route, field)
    _, address = parseaddr(value)
    if address != value or "@" not in address:
        raise ValueError(f"route.{field} must be one email address")
    return address


def require_message_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not MESSAGE_ID_RE.fullmatch(value):
        raise ValueError(f"route.{field} must be one RFC Message-ID in angle brackets")
    return value


def reply_subject(original_subject: str) -> str:
    return original_subject if re.match(r"^\s*re\s*:", original_subject, re.I) else f"Re: {original_subject}"


def build_reply(
    route: dict[str, Any],
    body: str,
    attachment: Path | Sequence[Path] | None = None,
) -> dict[str, str]:
    if route.get("channel") != "gmail":
        raise ValueError("route.channel must be gmail")
    thread_id = require_text(route, "thread_id")
    require_text(route, "gmail_message_id")
    original_message_id = require_message_id(
        route.get("original_message_id"), "original_message_id"
    )
    mailbox = require_email(route, "mailbox")
    recipient = require_email(route, "recipient")
    identity_key = require_text(route, "identity_key")
    if identity_key != email_identity_key(recipient):
        raise ValueError("route.identity_key does not match route.recipient")
    subject = require_text(route, "original_subject")
    body = validate_customer_text(plain_text(body))

    raw_references = route.get("references", [])
    if not isinstance(raw_references, list):
        raise ValueError("route.references must be an array of RFC Message-IDs")
    references = [
        require_message_id(value, f"references[{index}]")
        for index, value in enumerate(raw_references)
    ]
    if original_message_id not in references:
        references.append(original_message_id)

    message = EmailMessage()
    message["From"] = mailbox
    message["To"] = recipient
    message["Subject"] = reply_subject(subject)
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = make_msgid(domain=mailbox.rsplit("@", 1)[1])
    message["In-Reply-To"] = original_message_id
    message["References"] = " ".join(references)
    message.set_content(body)
    attachments = (
        []
        if attachment is None
        else [attachment]
        if isinstance(attachment, Path)
        else list(attachment)
    )
    if len(attachments) > 2:
        raise ValueError("a rendering reply may contain at most two images")
    for index, image in enumerate(attachments, start=1):
        content_type, encoding = mimetypes.guess_type(image.name)
        if encoding is not None or content_type not in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }:
            raise ValueError("rendering attachment must be JPEG, PNG, or WebP")
        data = image.read_bytes()
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError("rendering attachment must contain 1-20971520 bytes")
        maintype, subtype = content_type.split("/", 1)
        suffix = image.suffix.lower().lstrip(".")
        filename = (
            f"design-rendering.{suffix}"
            if len(attachments) == 1
            else f"design-rendering-{index}.{suffix}"
        )
        message.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    return {"threadId": thread_id, "raw": encoded}


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: dict[str, str]) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("route", type=Path)
    parser.add_argument("body", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--attachment", type=Path, action="append")
    args = parser.parse_args(argv)
    try:
        write_object(
            args.output,
            build_reply(
                read_object(args.route),
                args.body.read_text(encoding="utf-8"),
                args.attachment,
            ),
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
