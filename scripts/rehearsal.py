#!/usr/bin/env python3
"""Rehearsal mode: prove an install end to end before a customer sees it.

ARCHITECTURE-OPTIONS.md F2 (built 6 September 2026). A profile switch
(`rehearsal.enabled`, `rehearsal.address`) that is impossible to mistake
for live: readiness prints it in capitals on its first line, every card
title, every customer subject, every owner notice, and every tick summary
carry `[REHEARSAL]`, and only mail from the named address is handled. Mail
from anyone else is discovered as usual and then held (a parked claim with
reason `held_for_live`), never read, never answered; switching rehearsal
off releases every held message through the doctor's requeue path, so real
mail is delayed, never lost. Off by default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import gmail_reply
import inbox_claim
import inbox_monitor
import kolo_safe
import owner_questions

PREFIX = "[REHEARSAL] "
HELD_REASON = "held_for_live"
ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def read(profile: dict[str, Any] | None) -> dict[str, Any]:
    """{"enabled": bool, "address": str|None} from the profile, tolerant of an absent block."""
    block = (profile or {}).get("rehearsal") if isinstance(profile, dict) else None
    if not isinstance(block, dict):
        return {"enabled": False, "address": None}
    address = str(block.get("address") or "").strip().lower() or None
    return {"enabled": bool(block.get("enabled")) and address is not None, "address": address}


def load(workspace: Path) -> dict[str, Any]:
    path = workspace / "estimate-desk" / "shop-profile.json"
    try:
        return read(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"enabled": False, "address": None}


def apply(workspace: Path) -> dict[str, Any]:
    """Set the process-wide prefixes from the profile; every entry point calls this once."""
    state = load(workspace)
    prefix = PREFIX if state["enabled"] else ""
    kolo_safe.TITLE_PREFIX = prefix
    gmail_reply.SUBJECT_PREFIX = prefix
    owner_questions.NOTICE_PREFIX = prefix
    return state


def banner(state: dict[str, Any]) -> str:
    return f"REHEARSAL MODE: only mail from {state['address']} is handled; everything else is held" if state["enabled"] else ""


def sender_address(message: dict[str, Any]) -> str:
    headers = ((message.get("payload") or {}).get("headers") or []) if isinstance(message, dict) else []
    for header in headers:
        if str(header.get("name", "")).lower() == "from":
            value = str(header.get("value") or "")
            match = re.search(r"<([^>]+)>", value)
            return (match.group(1) if match else value).strip().lower()
    return ""


def should_hold(state: dict[str, Any], message: dict[str, Any]) -> bool:
    return bool(state.get("enabled")) and sender_address(message) != state.get("address")


def hold(monitor_root: Path, claim_root: Path, message_id: str) -> dict[str, Any]:
    """Park the claim untouched until rehearsal is off (WORKFLOW.md 6.5: never a real customer)."""
    token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    return inbox_monitor.park_item(monitor_root, message_id, claim_root, token, HELD_REASON)


def held(monitor_root: Path, claim_root: Path) -> list[str]:
    out = []
    for item in inbox_monitor.all_queue_items(monitor_root):
        if item.get("processing_status") != "awaiting_owner":
            continue
        message_id = item.get("gmail_message_id")
        path = inbox_claim.claim_path(claim_root, str(message_id))
        try:
            state = inbox_claim.read_state(path)
        except (OSError, ValueError):
            continue
        if state.get("status") == "awaiting_owner" and state.get("reason_code") == HELD_REASON:
            out.append(str(message_id))
    return out


def release(workspace: Path) -> list[str]:
    """Every held message back to the queue; the next tick reads them in order."""
    import doctor  # local import: doctor imports the desk modules

    desk = workspace / "estimate-desk"
    released = []
    for message_id in held(desk / "inbox-monitor", desk / "inbox-claims"):
        doctor.requeue(workspace, message_id)
        released.append(message_id)
    return released


def switch(workspace: Path, enabled: bool, address: str | None) -> dict[str, Any]:
    path = workspace / "estimate-desk" / "shop-profile.json"
    profile = json.loads(path.read_text(encoding="utf-8"))
    if enabled:
        if not address or not ADDRESS_RE.match(address):
            raise ValueError("rehearsal needs the address the rehearsal inquiries come from")
        profile["rehearsal"] = {"enabled": True, "address": address.strip().lower()}
    else:
        profile["rehearsal"] = {"enabled": False, "address": (profile.get("rehearsal") or {}).get("address")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    result: dict[str, Any] = {"rehearsal": read(profile)}
    if not enabled:
        result["released"] = release(workspace)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Switch rehearsal mode on (one address) or off (held mail is released).")
    parser.add_argument("--workspace", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("--address", default=None)
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    try:
        if args.status:
            state = load(workspace)
            desk = workspace / "estimate-desk"
            result = {"rehearsal": state, "held": held(desk / "inbox-monitor", desk / "inbox-claims")}
        else:
            result = switch(workspace, args.on, args.address)
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
