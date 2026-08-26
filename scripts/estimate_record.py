#!/usr/bin/env python3
"""Maintain the private local estimate records used for inbox routing."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import route_ownership


def default_record_root() -> Path:
    configured_workspace = os.environ.get("OPENCLAW_WORKSPACE")
    workspace = (
        Path(configured_workspace).expanduser()
        if configured_workspace
        else Path.home() / ".openclaw" / "workspace-main"
    )
    return workspace.resolve() / "estimate-desk" / "records"


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_object(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def record_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_path = root / ".records.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def estimate_id_for_route(route: dict[str, Any]) -> str:
    message_id = route.get("gmail_message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("route.gmail_message_id must be non-empty text")
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]
    return f"jed-{digest}"


def build_initial_record(
    route: dict[str, Any], inbound_timestamp_ms: int
) -> dict[str, Any]:
    if type(inbound_timestamp_ms) is not int or inbound_timestamp_ms < 0:
        raise ValueError("inbound_timestamp_ms must be a non-negative integer")
    if route.get("channel") != "gmail":
        raise ValueError("route.channel must be gmail")
    record = {
        "schema_version": 1,
        "estimate_id": estimate_id_for_route(route),
        "status": "awaiting_specs",
        "route": route,
        "inbound_timestamp_ms": inbound_timestamp_ms,
    }
    route_ownership.validate_record(record)
    return record


def record_path(root: Path, estimate_id: str) -> Path:
    if not route_ownership.ESTIMATE_ID_RE.fullmatch(estimate_id):
        raise ValueError("invalid estimate_id")
    return root / f"{estimate_id}.json"


def reject_duplicate_thread_record(root: Path, record: dict[str, Any]) -> None:
    thread_id = record["route"]["thread_id"]
    estimate_id = record["estimate_id"]
    for path in root.glob("jed-*.json"):
        if path.name == f"{estimate_id}.json":
            continue
        other = read_object(path)
        other_route = other.get("route")
        if isinstance(other_route, dict) and other_route.get("thread_id") == thread_id:
            raise ValueError("a different estimate record already owns this thread")


def persist_record(root: Path, record: dict[str, Any]) -> dict[str, Any]:
    route_ownership.validate_record(record)
    path = record_path(root, record["estimate_id"])
    with record_lock(root):
        reject_duplicate_thread_record(root, record)
        if path.exists():
            existing = read_object(path)
            route_ownership.validate_record(existing)
            if existing["route"] != record["route"]:
                raise ValueError("estimate route is immutable")
            existing_reply = existing.get("spec_gate_reply")
            proposed_reply = record.get("spec_gate_reply")
            if existing_reply is not None:
                if proposed_reply is not None and proposed_reply != existing_reply:
                    raise ValueError("spec-gate send evidence is immutable")
                if proposed_reply is None:
                    record = dict(record)
                    record["spec_gate_reply"] = existing_reply
        write_object(path, record)
    return record


def create_initial_record(
    root: Path, route: dict[str, Any], inbound_timestamp_ms: int
) -> dict[str, Any]:
    proposed = build_initial_record(route, inbound_timestamp_ms)
    path = record_path(root, proposed["estimate_id"])
    with record_lock(root):
        reject_duplicate_thread_record(root, proposed)
        if path.exists():
            existing = read_object(path)
            route_ownership.validate_record(existing)
            if existing["route"] != proposed["route"]:
                raise ValueError("estimate route is immutable")
            return existing
        write_object(path, proposed)
    return proposed


def lookup_thread(root: Path, route: dict[str, Any]) -> list[dict[str, Any]]:
    thread_id = route.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("route.thread_id must be non-empty text")
    if not root.exists():
        return []
    candidates: list[dict[str, Any]] = []
    with record_lock(root):
        for path in sorted(root.glob("jed-*.json")):
            record = read_object(path)
            record_route = record.get("route")
            if isinstance(record_route, dict) and record_route.get("thread_id") == thread_id:
                candidates.append(record)
    return candidates


def record_spec_gate_sent(
    root: Path,
    estimate_id: str,
    reply_body: str,
    provider_response: dict[str, Any],
) -> dict[str, Any]:
    """Persist privacy-minimal Gmail evidence for the initial specification reply."""
    if not reply_body.strip():
        raise ValueError("spec-gate reply body must not be empty")
    provider_message_id = provider_response.get("id")
    provider_thread_id = provider_response.get("threadId")
    for value, field in (
        (provider_message_id, "provider response id"),
        (provider_thread_id, "provider response threadId"),
    ):
        if not isinstance(value, str) or not value or len(value) > 512:
            raise ValueError(f"{field} must contain 1-512 characters")
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError(f"{field} contains invalid characters")

    path = record_path(root, estimate_id)
    with record_lock(root):
        record = read_object(path)
        route_ownership.validate_record(record)
        if record["status"] != "awaiting_specs":
            raise ValueError("spec-gate evidence requires awaiting_specs status")
        if provider_thread_id != record["route"]["thread_id"]:
            raise ValueError("provider response threadId does not match the owned thread")
        evidence = {
            "status": "sent",
            "provider_message_id": provider_message_id,
            "thread_id": provider_thread_id,
            "body_sha256": "sha256:"
            + hashlib.sha256(reply_body.encode("utf-8")).hexdigest(),
        }
        existing = record.get("spec_gate_reply")
        if existing is not None:
            comparable = dict(existing) if isinstance(existing, dict) else existing
            if isinstance(comparable, dict):
                comparable.pop("sent_at", None)
            if comparable == evidence:
                return record
            raise ValueError("conflicting spec-gate send evidence already exists")
        evidence["sent_at"] = datetime.now(timezone.utc).isoformat()
        record["spec_gate_reply"] = evidence
        write_object(path, record)
        return record


def require_initial_reply_evidence(root: Path, message_id: str) -> None:
    """Refuse completion of an awaiting-specs inquiry without same-thread send proof."""
    matches: list[dict[str, Any]] = []
    if root.exists():
        with record_lock(root):
            for path in sorted(root.glob("jed-*.json")):
                record = read_object(path)
                route_ownership.validate_record(record)
                if record["route"]["gmail_message_id"] == message_id:
                    matches.append(record)
    if len(matches) > 1:
        raise ValueError("multiple estimate records match the initiating Gmail message")
    if not matches or matches[0]["status"] != "awaiting_specs":
        return
    record = matches[0]
    evidence = record.get("spec_gate_reply")
    if not isinstance(evidence, dict) or evidence.get("status") != "sent":
        raise ValueError(
            "awaiting_specs inquiry lacks durable spec-gate send evidence; "
            "refusing processed outcome"
        )
    if evidence.get("thread_id") != record["route"]["thread_id"]:
        raise ValueError("spec-gate send evidence is bound to the wrong Gmail thread")
    if not isinstance(evidence.get("provider_message_id"), str) or not evidence[
        "provider_message_id"
    ]:
        raise ValueError("spec-gate send evidence lacks a provider message ID")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-inquiry")
    create.add_argument("route", type=Path)
    create.add_argument("--inbound-timestamp-ms", type=int, required=True)
    create.add_argument("--record-root", type=Path, default=default_record_root())
    create.add_argument("--output", type=Path, required=True)
    upsert = sub.add_parser("upsert")
    upsert.add_argument("record", type=Path)
    upsert.add_argument("--record-root", type=Path, default=default_record_root())
    lookup = sub.add_parser("lookup-thread")
    lookup.add_argument("route", type=Path)
    lookup.add_argument("--record-root", type=Path, default=default_record_root())
    lookup.add_argument("--output", type=Path, required=True)
    spec_gate = sub.add_parser("record-spec-gate-sent")
    spec_gate.add_argument("--estimate-id", required=True)
    spec_gate.add_argument("--reply-body", type=Path, required=True)
    spec_gate.add_argument("--provider-response", type=Path, required=True)
    spec_gate.add_argument("--record-root", type=Path, default=default_record_root())
    spec_gate.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create-inquiry":
            record = create_initial_record(
                args.record_root,
                read_object(args.route),
                args.inbound_timestamp_ms,
            )
            write_object(args.output, record)
        elif args.command == "upsert":
            record = persist_record(args.record_root, read_object(args.record))
        elif args.command == "lookup-thread":
            records = lookup_thread(args.record_root, read_object(args.route))
            write_object(args.output, records)
            record = {"candidates": len(records)}
        else:
            record = record_spec_gate_sent(
                args.record_root,
                args.estimate_id,
                args.reply_body.read_text(encoding="utf-8"),
                read_object(args.provider_response),
            )
            if args.output is not None:
                write_object(args.output, record)
        print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
