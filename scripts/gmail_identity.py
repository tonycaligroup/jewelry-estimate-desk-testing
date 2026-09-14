#!/usr/bin/env python3
"""Read the exact Gmail send-as identity used by the desk, without writing Gmail."""

from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from typing import Any, Callable

import gateway_token
import gmail_fetch


class _SignatureText(HTMLParser):
    BLOCKS = {"address", "blockquote", "br", "div", "li", "p", "table", "tr"}
    IGNORED = {"iframe", "object", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in self.IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def plain_signature(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _SignatureText()
    parser.feed(value)
    parser.close()
    text = "".join(parser.parts).replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def retrieve(
    mailbox: str,
    token: str,
    opener: Callable[..., Any] = gmail_fetch.urlopen,
) -> dict[str, Any]:
    wanted = mailbox.strip().casefold()
    if not wanted or "@" not in wanted or any(character in wanted for character in "\r\n"):
        raise ValueError("outbound mailbox is missing or invalid")
    response = gmail_fetch.fetch_json("settings/sendAs", None, token, opener)
    aliases = response.get("sendAs", [])
    if not isinstance(aliases, list):
        raise ValueError("Gmail send-as response must contain an array")
    matches = [
        alias for alias in aliases
        if isinstance(alias, dict) and str(alias.get("sendAsEmail") or "").strip().casefold() == wanted
    ]
    if len(matches) != 1:
        raise ValueError("no unique send-as identity matches the outbound mailbox")
    alias = matches[0]
    display_name = str(alias.get("displayName") or "").strip()
    if any(character in display_name for character in "\r\n"):
        raise ValueError("Gmail send-as display name must be one line")
    return {
        "send_as_email": wanted,
        "display_name": display_name,
        "signature_block": plain_signature(alias.get("signature")),
        "is_primary": alias.get("isPrimary") is True,
        "is_default": alias.get("isDefault") is True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mailbox", required=True)
    args = parser.parse_args()
    print(json.dumps(retrieve(args.mailbox, gateway_token.load_token()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
