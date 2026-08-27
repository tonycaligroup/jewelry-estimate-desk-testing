#!/usr/bin/env python3
"""Bind owner approvals to the Kolo user who activates the skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
APPROVER_SOURCE = "activating_kolo_user"
SESSION_KEY_RE = re.compile(r"^agent:[A-Za-z0-9_.:@/-]{1,255}$")


def binding_path(monitor_root: Path) -> Path:
    return monitor_root.parent / "work" / "activation-binding.json"


def validate_session_key(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_KEY_RE.fullmatch(value):
        raise ValueError("invalid Kolo activation session key")
    return value


def validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("activation binding must be a JSON object")
    if set(value) != {"schema_version", "approver_source", "session_key"}:
        raise ValueError("activation binding contains missing or unsupported fields")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported activation binding schema_version")
    if value["approver_source"] != APPROVER_SOURCE:
        raise ValueError("approver must be the activating Kolo user")
    validate_session_key(value["session_key"])
    return value


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("activating Kolo user is not bound for approvals") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("activation binding is not valid JSON") from exc
    return validate(value)


def create(path: Path, session_key: str) -> dict[str, Any]:
    value = validate(
        {
            "schema_version": SCHEMA_VERSION,
            "approver_source": APPROVER_SOURCE,
            "session_key": session_key,
        }
    )
    if path.exists():
        current = load(path)
        if current == value:
            return current
        raise ValueError(
            "a different activating Kolo user is already bound; refusing replacement"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind-activating-user")
    bind.add_argument("--monitor-root", type=Path, required=True)
    bind.add_argument("--session-key", required=True)
    status = sub.add_parser("status")
    status.add_argument("--monitor-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        path = binding_path(args.monitor_root)
        if args.command == "bind-activating-user":
            create(path, args.session_key)
        else:
            load(path)
        print(
            json.dumps(
                {
                    "approver_bound": True,
                    "approver_source": APPROVER_SOURCE,
                    "path": str(path),
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
