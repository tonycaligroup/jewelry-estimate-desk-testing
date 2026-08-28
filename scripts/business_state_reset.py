#!/usr/bin/env python3
"""Reset business activation state after customer state has been cleared."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import activation_binding
import customer_state_reset
import inbox_monitor
import validate_profile


SPOT_CACHE_RE = re.compile(r"^spot-cache(?:-[a-z0-9-]+)?\.json$")


def _customer_state_counts(desk: Path) -> dict[str, int]:
    monitor_root = desk / "inbox-monitor"
    return {
        "claims": len(
            customer_state_reset._validated_directories(  # noqa: SLF001
                desk / "inbox-claims", customer_state_reset.HASH_DIR_RE
            )
        ),
        "queue_items": len(customer_state_reset._queue_paths(monitor_root)),  # noqa: SLF001
        "records": len(customer_state_reset._record_paths(desk / "records")),  # noqa: SLF001
        "run_work_directories": len(
            customer_state_reset._validated_directories(  # noqa: SLF001
                desk / "run-work", customer_state_reset.RUN_DIR_RE
            )
        ),
        "work_directories": len(
            customer_state_reset._validated_directories(  # noqa: SLF001
                desk / "work", customer_state_reset.HASH_DIR_RE
            )
        ),
    }


def _spot_cache_paths(desk: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(desk.glob("spot-cache*.json")):
        if path.is_symlink() or not path.is_file() or not SPOT_CACHE_RE.fullmatch(
            path.name
        ):
            raise ValueError(f"unexpected spot-cache reset target: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"spot cache is not valid JSON: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"spot cache must be a JSON object: {path}")
        result.append(path)
    return result


def reset(workspace: Path, template: Path | None = None) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    desk = workspace / "estimate-desk"
    monitor_root = desk / "inbox-monitor"
    profile_path = desk / "shop-profile.json"
    binding_path = activation_binding.binding_path(monitor_root)
    cron_binding_path = desk / "work" / "cron-binding.json"
    template_path = (
        Path(__file__).resolve().parent.parent / "templates" / "shop-profile.json"
        if template is None
        else template.expanduser().resolve()
    )

    profile = validate_profile.load_profile(profile_path)
    if not validate_profile.validate_profile(profile)["ready"]:
        raise ValueError("current shop profile is not valid; refusing business reset")
    activation_binding.load(binding_path)
    state = inbox_monitor.load_monitor_state(monitor_root)
    if state["activation_state"] != "active":
        raise ValueError("inbox monitor must have active durable state")
    if cron_binding_path.is_symlink() or not cron_binding_path.is_file():
        raise ValueError("durable cron binding is missing or unsafe")
    cron_binding = inbox_monitor.read_json(cron_binding_path)
    if not isinstance(cron_binding, dict) or not cron_binding:
        raise ValueError("durable cron binding must be a non-empty JSON object")

    template_value = inbox_monitor.read_json(template_path)
    template_status = validate_profile.validate_profile(template_value)
    if template_status["ready"]:
        raise ValueError("business reset template must require fresh setup")

    counts = _customer_state_counts(desk)
    if any(counts.values()):
        raise ValueError("customer state must be cleared before business reset")
    spot_caches = _spot_cache_paths(desk)

    with inbox_monitor.setup_lock(monitor_root):
        with inbox_monitor.state_lock(monitor_root):
            state = inbox_monitor.load_monitor_state(monitor_root)
            if state["activation_state"] != "active":
                raise ValueError("inbox monitor activation changed during reset")
            if any(_customer_state_counts(desk).values()):
                raise ValueError("customer state appeared during business reset")
            prepared_state = {
                "schema_version": inbox_monitor.SCHEMA_VERSION,
                "activation_state": "prepared",
                "bound_cron_sha256": state["bound_cron_sha256"],
                "pending_cron_sha256": None,
                "capabilities": state["capabilities"],
                "activated_at_ms": None,
                "discovery_watermark_ms": None,
            }
            inbox_monitor.atomic_write_json(profile_path, template_value)
            binding_path.unlink()
            for path in spot_caches:
                path.unlink()
            inbox_monitor.atomic_write_json(
                monitor_root / "monitor-state.json", prepared_state
            )

    return {
        "business_state_reset": True,
        "activation_state": "prepared",
        "customer_state_counts": counts,
        "removed": {
            "activation_binding": True,
            "spot_caches": len(spot_caches),
        },
        "preserved": [
            "installed skill",
            "work/cron-binding.json",
            "inbox monitor job identity and disabled live configuration",
            "Gmail account authorization",
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
