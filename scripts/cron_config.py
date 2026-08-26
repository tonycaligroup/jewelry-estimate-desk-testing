#!/usr/bin/env python3
"""Render and validate the stable, behavior-bearing Kolo cron binding."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


MODEL = "litellm-fireworks/qwen-3-7-plus"
JOB_NAME = "jed-inbox-monitor"
TIMEOUT_SECONDS = 300


def template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "templates" / "inbox-monitor-cron.txt"


def render_message(workspace: Path, base_dir: Path) -> str:
    text = template_path().read_text(encoding="utf-8").rstrip("\n")
    return text.replace("<WORKSPACE>", str(workspace.resolve())).replace(
        "<BASE_DIR>", str(base_dir.resolve())
    )


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def validate_canonical_message(message: str) -> None:
    match = re.match(
        r"Run the Jewelry Estimate Desk inbox monitor exactly as follows\.\n\n"
        r"Never call create_goal or update_goal\.[\s\S]*?\n\n"
        r"1\. Validate (.+?)/estimate-desk/shop-profile\.json\. Run `python3 (.+?)/scripts/inbox_monitor\.py status`\.",
        message,
    )
    if match is None:
        raise ValueError("cron binding message is not the canonical safe runbook")
    workspace, base_dir = match.groups()
    if not workspace.startswith("/") or not base_dir.startswith("/"):
        raise ValueError("cron binding message paths must be absolute")
    if message != render_message(Path(workspace), Path(base_dir)):
        raise ValueError("cron binding message differs from the canonical safe runbook")


def validate_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "name",
        "agentId",
        "schedule",
        "sessionTarget",
        "wakeMode",
        "payload",
        "delivery",
    }:
        raise ValueError("cron binding contains missing or unsupported fields")
    if value.get("name") != JOB_NAME or value.get("sessionTarget") != "isolated":
        raise ValueError("cron binding identity or session target is invalid")
    job_id = require_string(value.get("id"), "id")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", job_id):
        raise ValueError("invalid cron id")
    require_string(value.get("agentId"), "agentId")
    require_string(value.get("wakeMode"), "wakeMode")
    schedule = value.get("schedule")
    payload = value.get("payload")
    delivery = value.get("delivery")
    if not all(isinstance(item, dict) for item in (schedule, payload, delivery)):
        raise ValueError("cron binding schedule, payload, and delivery must be objects")
    if set(schedule) != {"kind", "expr", "tz"} or schedule.get("kind") != "cron":
        raise ValueError("invalid cron binding schedule")
    require_string(schedule.get("expr"), "schedule.expr")
    require_string(schedule.get("tz"), "schedule.tz")
    required_payload = {
        "kind",
        "message",
        "model",
        "fallbacks",
        "timeoutSeconds",
        "lightContext",
    }
    if not required_payload.issubset(payload) or not set(payload).issubset(
        required_payload | {"thinking", "toolsAllow"}
    ):
        raise ValueError("cron binding payload contains missing or unsupported fields")
    if payload.get("kind") != "agentTurn" or payload.get("model") != MODEL:
        raise ValueError("invalid cron binding payload kind or model")
    if payload.get("fallbacks") != [] or payload.get("lightContext") is not True:
        raise ValueError("cron binding requires no fallbacks and lightContext true")
    if payload.get("timeoutSeconds") != TIMEOUT_SECONDS:
        raise ValueError("cron binding timeoutSeconds must be 300")
    message = require_string(payload.get("message"), "payload.message")
    if "<WORKSPACE>" in message or "<BASE_DIR>" in message:
        raise ValueError("cron binding message contains unresolved path placeholders")
    validate_canonical_message(message)
    if set(delivery) != {"mode", "channel", "to"}:
        raise ValueError("cron binding delivery contains missing or unsupported fields")
    if delivery.get("mode") != "announce" or delivery.get("channel") != "kolo":
        raise ValueError("invalid cron binding delivery")
    require_string(delivery.get("to"), "delivery.to")
    return value


def build_binding(job: Any, workspace: Path, base_dir: Path) -> dict[str, Any]:
    if not isinstance(job, dict):
        raise ValueError("live cron job must be a JSON object")
    if job.get("name") != JOB_NAME:
        raise ValueError("unexpected cron job name")
    if type(job.get("enabled")) is not bool:
        raise ValueError("cron enabled state must be boolean")
    schedule = job.get("schedule")
    payload = job.get("payload")
    delivery = job.get("delivery")
    if not all(isinstance(value, dict) for value in (schedule, payload, delivery)):
        raise ValueError("cron schedule, payload, and delivery must be objects")
    if schedule.get("kind") != "cron":
        raise ValueError("cron schedule kind must be cron")
    if payload.get("kind") != "agentTurn":
        raise ValueError("cron payload kind must be agentTurn")
    if payload.get("message") != render_message(workspace, base_dir):
        raise ValueError("live cron prompt does not match the canonical runbook")
    if payload.get("model") != MODEL or payload.get("fallbacks") != []:
        raise ValueError("cron model or fallbacks do not match the required runtime")
    if payload.get("timeoutSeconds") != TIMEOUT_SECONDS:
        raise ValueError("cron timeoutSeconds must be 300")
    if payload.get("lightContext") is not True:
        raise ValueError("cron lightContext must be true")
    if job.get("sessionTarget") != "isolated":
        raise ValueError("cron sessionTarget must be isolated")
    if delivery.get("mode") != "announce":
        raise ValueError("cron delivery mode must be announce")
    if delivery.get("channel") != "kolo":
        raise ValueError("cron delivery channel must be kolo")

    job_id = require_string(job.get("id"), "id")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", job_id):
        raise ValueError("invalid cron id")
    projection: dict[str, Any] = {
        "id": job_id,
        "name": JOB_NAME,
        "agentId": require_string(job.get("agentId"), "agentId"),
        "schedule": {
            "kind": "cron",
            "expr": require_string(schedule.get("expr"), "schedule.expr"),
            "tz": require_string(schedule.get("tz"), "schedule.tz"),
        },
        "sessionTarget": "isolated",
        "wakeMode": require_string(job.get("wakeMode"), "wakeMode"),
        "payload": {
            "kind": "agentTurn",
            "message": payload["message"],
            "model": MODEL,
            "fallbacks": [],
            "timeoutSeconds": TIMEOUT_SECONDS,
            "lightContext": True,
        },
        "delivery": {
            "mode": "announce",
            "channel": "kolo",
            "to": require_string(delivery.get("to"), "delivery.to"),
        },
    }
    for optional in ("thinking", "toolsAllow"):
        if optional in payload:
            projection["payload"][optional] = payload[optional]
    return validate_binding(projection)


def build_target_binding(job: Any, workspace: Path, base_dir: Path) -> dict[str, Any]:
    """Project the intended safe config from an existing live job identity."""
    if not isinstance(job, dict):
        raise ValueError("live cron job must be a JSON object")
    target = dict(job)
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("cron payload must be an object")
    target["payload"] = {
        "kind": "agentTurn",
        "message": render_message(workspace, base_dir),
        "model": MODEL,
        "fallbacks": [],
        "timeoutSeconds": TIMEOUT_SECONDS,
        "lightContext": True,
    }
    for optional in ("thinking", "toolsAllow"):
        if optional in payload:
            target["payload"][optional] = payload[optional]
    return build_binding(target, workspace, base_dir)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render-message")
    bind = sub.add_parser("bind-live")
    target = sub.add_parser("target-binding")
    for command in (render, bind, target):
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--base-dir", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    bind.add_argument("--job", type=Path, required=True)
    target.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "render-message":
            # This file is consumed verbatim as payload.message. A trailing
            # newline changes the binding hash and fails canonical validation.
            write_text(args.output, render_message(args.workspace, args.base_dir))
        elif args.command == "bind-live":
            job = json.loads(args.job.read_text(encoding="utf-8"))
            binding = build_binding(job, args.workspace, args.base_dir)
            write_text(args.output, json.dumps(binding, ensure_ascii=False, sort_keys=True) + "\n")
        else:
            job = json.loads(args.job.read_text(encoding="utf-8"))
            binding = build_target_binding(job, args.workspace, args.base_dir)
            write_text(args.output, json.dumps(binding, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
