#!/usr/bin/env python3
"""Fetch Gmail discovery and claimed-thread data through the Maton gateway.

This helper owns HTTP construction, pagination, provider-response validation,
and private artifact writes so the cron model never constructs Gmail commands
or discovery JSON itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import gateway_token
import inbox_monitor


API_ROOT = "https://gateway.maton.ai/google-mail/gmail/v1/users/me"
PAGE_SIZE = 100
Opener = Callable[..., Any]


def require_token(value: str) -> str:
    if not value or any(character in value for character in "\r\n"):
        raise ValueError("MATON_API_KEY is missing or invalid")
    return value


def fetch_json(
    path: str,
    params: dict[str, str | int] | None,
    token: str,
    opener: Opener = urlopen,
) -> dict[str, Any]:
    token = require_token(token)
    url = f"{API_ROOT}/{path.lstrip('/')}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener(request, timeout=30) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise ValueError(f"Gmail gateway returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError("Gmail gateway request failed") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Gmail gateway returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Gmail gateway response must be a JSON object")
    return value


def provider_id(value: Any, field: str) -> str:
    return inbox_monitor.require_provider_id(value, field)


def list_discovery(
    watermark_ms: int,
    token: str,
    opener: Opener = urlopen,
) -> list[dict[str, Any]]:
    inbox_monitor.require_epoch_ms(watermark_ms, "discovery_watermark_ms")
    after_seconds = max(0, watermark_ms // 1000 - 1)
    page_token: str | None = None
    seen_page_tokens: set[str] = set()
    discovered: dict[str, dict[str, Any]] = {}
    while True:
        params: dict[str, str | int] = {
            "q": f"in:inbox after:{after_seconds}",
            "maxResults": PAGE_SIZE,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        page = fetch_json("messages", params, token, opener)
        messages = page.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Gmail messages page must contain an array")
        for summary in messages:
            if not isinstance(summary, dict):
                raise ValueError("Gmail message summary must be an object")
            message_id = provider_id(summary.get("id"), "gmail_message_id")
            summary_thread_id = provider_id(summary.get("threadId"), "thread_id")
            detail = fetch_json(
                f"messages/{quote(message_id, safe='')}",
                {"format": "metadata"},
                token,
                opener,
            )
            if provider_id(detail.get("id"), "gmail_message_id") != message_id:
                raise ValueError("Gmail detail ID does not match its list result")
            thread_id = provider_id(detail.get("threadId"), "thread_id")
            if thread_id != summary_thread_id:
                raise ValueError("Gmail thread ID changed between list and detail")
            raw_internal_date = detail.get("internalDate")
            if isinstance(raw_internal_date, str) and raw_internal_date.isdigit():
                internal_date_ms = int(raw_internal_date)
            elif type(raw_internal_date) is int:
                internal_date_ms = raw_internal_date
            else:
                raise ValueError("Gmail internalDate must be integer milliseconds")
            inbox_monitor.require_epoch_ms(internal_date_ms, "internal_date_ms")
            item = {
                "gmail_message_id": message_id,
                "thread_id": thread_id,
                "internal_date_ms": internal_date_ms,
            }
            prior = discovered.get(message_id)
            if prior is not None and prior != item:
                raise ValueError("duplicate Gmail ID has inconsistent metadata")
            discovered[message_id] = item
        raw_next = page.get("nextPageToken")
        if raw_next is None:
            break
        page_token = provider_id(raw_next, "nextPageToken")
        if page_token in seen_page_tokens:
            raise ValueError("Gmail pagination repeated a page token")
        seen_page_tokens.add(page_token)
    return sorted(
        discovered.values(),
        key=lambda item: (item["internal_date_ms"], item["gmail_message_id"]),
    )


def discover(
    monitor_root: Path,
    token: str,
    now_ms: int | None = None,
    opener: Opener = urlopen,
) -> dict[str, Any]:
    state = inbox_monitor.load_monitor_state(monitor_root)
    if state["activation_state"] != "active":
        raise ValueError("monitor is not active")
    window_start_ms = state["discovery_watermark_ms"]
    window_end_ms = int(time.time() * 1000) if now_ms is None else now_ms
    inbox_monitor.require_epoch_ms(window_end_ms, "window_end_ms")
    if window_end_ms < window_start_ms:
        raise ValueError("window_end_ms cannot precede the durable watermark")
    work = inbox_monitor.prepare_run_work(monitor_root)
    batch_path = Path(work["discovery_batch"])
    try:
        batch = list_discovery(window_start_ms, token, opener)
        inbox_monitor.atomic_write_json(batch_path, batch)
        result = inbox_monitor.discover_complete(
            monitor_root, batch, window_start_ms, window_end_ms
        )
        return {"discovered": len(batch), **result}
    finally:
        inbox_monitor.cleanup_run_work(monitor_root, batch_path)


def fetch_claimed(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    token: str,
    opener: Opener = urlopen,
) -> dict[str, str]:
    item = inbox_monitor.load_queue_item(monitor_root, message_id)
    paths = inbox_monitor.prepare_claim_work(monitor_root, claim_root, message_id)
    message = fetch_json(
        f"messages/{quote(message_id, safe='')}",
        {"format": "full"},
        token,
        opener,
    )
    if provider_id(message.get("id"), "gmail_message_id") != message_id:
        raise ValueError("Gmail message response ID does not match the claim")
    thread_id = provider_id(message.get("threadId"), "thread_id")
    if thread_id != item["thread_id"]:
        raise ValueError("Gmail message response thread does not match the queue")
    thread = fetch_json(
        f"threads/{quote(thread_id, safe='')}",
        {"format": "full"},
        token,
        opener,
    )
    if provider_id(thread.get("id"), "thread_id") != thread_id:
        raise ValueError("Gmail thread response ID does not match the queue")
    messages = thread.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(entry, dict) and entry.get("id") == message_id for entry in messages
    ):
        raise ValueError("Gmail thread does not contain the claimed message")
    inbox_monitor.atomic_write_json(Path(paths["gmail_message"]), message)
    inbox_monitor.atomic_write_json(Path(paths["gmail_thread"]), thread)
    return {
        "gmail_message": paths["gmail_message"],
        "gmail_thread": paths["gmail_thread"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    discover_parser = sub.add_parser("discover")
    discover_parser.add_argument(
        "--monitor-root", type=Path, default=inbox_monitor.default_root()
    )
    fetch_parser = sub.add_parser("fetch-claimed")
    fetch_parser.add_argument(
        "--monitor-root", type=Path, default=inbox_monitor.default_root()
    )
    fetch_parser.add_argument("--claim-root", type=Path, required=True)
    fetch_parser.add_argument("--message-id", required=True)
    args = parser.parse_args(argv)
    try:
        token = require_token(gateway_token.load_token())
        if args.command == "discover":
            result = discover(args.monitor_root, token)
        else:
            result = fetch_claimed(
                args.monitor_root, args.claim_root, args.message_id, token
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
