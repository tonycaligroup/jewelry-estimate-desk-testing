#!/usr/bin/env python3
"""Clear customer/job state while preserving shop and monitor configuration."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import activation_binding
import estimate_record
import inbox_monitor
import validate_profile


HASH_DIR_RE = re.compile(r"^[0-9a-f]{64}$")
RUN_DIR_RE = re.compile(r"^[0-9a-f]{24}$")


def _private_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"{label} is not a private directory")
    return path


def _validated_directories(root: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not root.exists():
        return []
    _private_directory(root, str(root))
    result: list[Path] = []
    for path in root.iterdir():
        if path.name.startswith(".") or path.is_file():
            continue
        if path.is_symlink() or not path.is_dir() or not pattern.fullmatch(path.name):
            raise ValueError(f"unexpected reset target: {path}")
        result.append(path)
    return sorted(result)


def _record_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    _private_directory(root, "record root")
    paths = sorted(root.glob("jed-*.json"))
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unexpected record target: {path}")
        estimate_record.read_object(path)
    return paths


def _queue_paths(root: Path) -> list[Path]:
    queue = root / "queue"
    if not queue.exists():
        return []
    _private_directory(queue, "monitor queue")
    paths = sorted(queue.glob("*.json"))
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unexpected queue target: {path}")
        inbox_monitor.validate_queue_item(inbox_monitor.read_json(path))
    return paths


def reset(workspace: Path, now_ms: int | None = None) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    desk = workspace / "estimate-desk"
    profile_path = desk / "shop-profile.json"
    monitor_root = desk / "inbox-monitor"
    claim_root = desk / "inbox-claims"
    record_root = desk / "records"
    work_root = desk / "work"
    run_root = desk / "run-work"

    profile = validate_profile.load_profile(profile_path)
    profile_status = validate_profile.validate_profile(profile)
    if not profile_status["ready"]:
        raise ValueError("shop profile is not valid; refusing customer-state reset")
    activation_binding.load(activation_binding.binding_path(monitor_root))
    state = inbox_monitor.load_monitor_state(monitor_root)
    if state["activation_state"] != "active":
        raise ValueError("inbox monitor must have active durable state")

    records = _record_paths(record_root)
    queue_items = _queue_paths(monitor_root)
    claims = _validated_directories(claim_root, HASH_DIR_RE)
    work_dirs = _validated_directories(work_root, HASH_DIR_RE)
    run_dirs = _validated_directories(run_root, RUN_DIR_RE)
    mirror_record_ids = [path.stem for path in records]
    effective_now = int(time.time() * 1000) if now_ms is None else now_ms
    inbox_monitor.require_epoch_ms(effective_now, "reset_at_ms")

    with inbox_monitor.setup_lock(monitor_root):
        with inbox_monitor.state_lock(monitor_root):
            state = inbox_monitor.load_monitor_state(monitor_root)
            state["discovery_watermark_ms"] = max(
                state["discovery_watermark_ms"], effective_now
            )
            inbox_monitor.atomic_write_json(monitor_root / "monitor-state.json", state)
            for path in queue_items:
                path.unlink()

        with estimate_record.record_lock(record_root):
            for path in records:
                path.unlink()

        for path in claims + work_dirs + run_dirs:
            shutil.rmtree(path)

    return {
        "customer_state_cleared": True,
        "discovery_watermark_ms": state["discovery_watermark_ms"],
        "removed": {
            "claims": len(claims),
            "queue_items": len(queue_items),
            "records": len(records),
            "run_work_directories": len(run_dirs),
            "work_directories": len(work_dirs),
        },
        "mirror_record_ids": mirror_record_ids,
        "preserved": [
            "shop-profile.json",
            "work/activation-binding.json",
            "monitor-state.json",
            "cron job and binding",
            "business and pricing configuration",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--confirmed-cron-disabled", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirmed_cron_disabled:
        print(
            json.dumps({"error": "--confirmed-cron-disabled is required"}),
            file=sys.stderr,
        )
        return 2
    try:
        print(json.dumps(reset(args.workspace), sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
