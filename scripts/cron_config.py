#!/usr/bin/env python3
"""Render and validate the stable, behavior-bearing Kolo cron binding."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# Worker model. glm-5-3 ran about one tool call per minute with the full
# SKILL.md in context and timed out at 900 s on a two-review claim (3 Sep
# 2026). Qwen 3.7 Plus is the pod default and honors the explicit thinking
# setting below.
MODEL = "litellm-fireworks/qwen-3-7-plus"
JOB_NAME = "jed-inbox-monitor"
TIMEOUT_SECONDS = 900
TOOLS_ALLOW = ["exec", "read", "write", "image_generate"]
# The watcher is code, not a model: it polls, claims, classifies, routes,
# and hands judgment work to one worker job per claim. A tick with nothing
# to do finishes in seconds; the timeout only bounds a stuck Gmail call.
WATCHER_TIMEOUT_SECONDS = 300
# Each worker job gets its own clock, model, and tool allowlist. The lease on
# its claim outlives the timeout slightly so the watcher never resumes a
# claim while the worker's run is still being torn down.
WORKER_TIMEOUT_SECONDS = 900
WORKER_LEASE_SECONDS = 1020
WORKER_THINKING = "off"
WORKER_NAME_PREFIX = "jed-worker-"
WATCHER_COMMAND_TEMPLATE = (
    "python3 <BASE_DIR>/scripts/inbox_watcher.py "
    "--workspace <WORKSPACE> --base-dir <BASE_DIR> --owner-target <OWNER_TARGET>"
)
WATCHER_COMMAND_RE = re.compile(
    r"^python3 (/\S+)/scripts/inbox_watcher\.py "
    r"--workspace (/\S+) --base-dir (/\S+) --owner-target (\S+)$"
)


def template_path() -> Path:
    return Path(__file__).resolve().parents[1] / "templates" / "inbox-monitor-cron.txt"


WORKER_BRANCHES = {
    # record status at intake -> the one branch prompt the worker receives.
    "awaiting_specs": "intake",
    "estimate_sent": "post_estimate",
    "appointment_booked": "post_estimate",
    "approved": "post_estimate",
}


def worker_branch(record_status: str | None) -> str:
    """Pick the worker's branch from the record status the intake reported."""
    try:
        return WORKER_BRANCHES[record_status or ""]
    except KeyError as exc:
        raise ValueError(f"no worker branch for record status {record_status!r}") from exc


def worker_template_path(branch: str = "intake") -> Path:
    """The branch prompt; the shared preamble lives in worker-common.txt.

    A worker never reads SKILL.md. Its whole instruction set is the common
    preamble plus one branch (Stage B): a few kilobytes instead of the full
    runbook, so a claim fits comfortably inside the job's clock.
    """
    templates = Path(__file__).resolve().parents[1] / "templates"
    if branch == "common":
        return templates / "worker-common.txt"
    if branch not in set(WORKER_BRANCHES.values()):
        raise ValueError(f"unknown worker branch {branch!r}")
    return templates / f"worker-{branch.replace('_', '-')}.txt"


def watcher_command(workspace: Path, base_dir: Path, owner_target: str) -> str:
    """Render the exact shell line the watcher job runs every tick."""
    if not re.fullmatch(r"[A-Za-z0-9:_.@-]{3,200}", owner_target or ""):
        raise ValueError("owner target must be a plain delivery identifier")
    return (
        WATCHER_COMMAND_TEMPLATE.replace("<WORKSPACE>", str(workspace.resolve()))
        .replace("<BASE_DIR>", str(base_dir.resolve()))
        .replace("<OWNER_TARGET>", owner_target)
    )


def render_worker_message(
    workspace: Path,
    base_dir: Path,
    message_id: str,
    estimate_id: str,
    work_dir: str,
    branch: str = "intake",
) -> str:
    """Render one worker job's prompt: the common preamble plus one branch."""
    for value, label in ((message_id, "message_id"), (estimate_id, "estimate_id")):
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", value or ""):
            raise ValueError(f"{label} must be a plain provider identifier")
    if not work_dir.startswith("/") or any(ch.isspace() for ch in work_dir):
        raise ValueError("work_dir must be an absolute path without spaces")
    text = (
        worker_template_path("common").read_text(encoding="utf-8").rstrip("\n")
        + "\n\n"
        + worker_template_path(branch).read_text(encoding="utf-8").rstrip("\n")
    )
    return (
        text.replace("<WORKSPACE>", str(workspace.resolve()))
        .replace("<BASE_DIR>", str(base_dir.resolve()))
        .replace("<CLAIMED_GMAIL_ID>", message_id)
        .replace("<ESTIMATE_ID>", estimate_id)
        .replace("<WORK_DIR>", work_dir)
    )


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
        r"This is a fail-closed procedure,[\s\S]*?\n\n"
        r"1\. Run exactly `python3 (.+?)/scripts/validate_profile\.py "
        r"(.+?)/estimate-desk/shop-profile\.json`, then exactly `python3 "
        r"\1/scripts/inbox_monitor\.py status`\.",
        message,
    )
    if match is None:
        raise ValueError("cron binding message is not the canonical safe runbook")
    base_dir, workspace = match.groups()
    if not workspace.startswith("/") or not base_dir.startswith("/"):
        raise ValueError("cron binding message paths must be absolute")
    if message != render_message(Path(workspace), Path(base_dir)):
        raise ValueError("cron binding message differs from the canonical safe runbook")


def validate_binding(value: Any) -> dict[str, Any]:
    required_fields = {
        "id",
        "name",
        "schedule",
        "sessionTarget",
        "wakeMode",
        "payload",
        "delivery",
    }
    if (
        not isinstance(value, dict)
        or not required_fields.issubset(value)
        or not set(value).issubset(required_fields | {"agentId"})
    ):
        raise ValueError("cron binding contains missing or unsupported fields")
    if value.get("name") != JOB_NAME or value.get("sessionTarget") != "isolated":
        raise ValueError("cron binding identity or session target is invalid")
    job_id = require_string(value.get("id"), "id")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", job_id):
        raise ValueError("invalid cron id")
    if "agentId" in value:
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
    if set(delivery) != {"mode", "channel", "to"}:
        raise ValueError("cron binding delivery contains missing or unsupported fields")
    if delivery.get("mode") != "announce" or delivery.get("channel") != "kolo":
        raise ValueError("invalid cron binding delivery")
    owner_target = require_string(delivery.get("to"), "delivery.to")
    if payload.get("kind") == "command":
        validate_command_payload(payload, owner_target)
        return value
    required_payload = {
        "kind",
        "message",
        "model",
        "fallbacks",
        "timeoutSeconds",
        "lightContext",
        "toolsAllow",
    }
    if not required_payload.issubset(payload) or not set(payload).issubset(
        required_payload | {"thinking"}
    ):
        raise ValueError("cron binding payload contains missing or unsupported fields")
    if payload.get("kind") != "agentTurn" or payload.get("model") != MODEL:
        raise ValueError("invalid cron binding payload kind or model")
    if payload.get("fallbacks") != [] or payload.get("lightContext") is not True:
        raise ValueError("cron binding requires no fallbacks and lightContext true")
    if payload.get("toolsAllow") != TOOLS_ALLOW:
        raise ValueError(
            "cron binding toolsAllow must be exec, read, write, and image_generate"
        )
    if payload.get("timeoutSeconds") != TIMEOUT_SECONDS:
        raise ValueError(f"cron binding timeoutSeconds must be {TIMEOUT_SECONDS}")
    message = require_string(payload.get("message"), "payload.message")
    if "<WORKSPACE>" in message or "<BASE_DIR>" in message:
        raise ValueError("cron binding message contains unresolved path placeholders")
    validate_canonical_message(message)
    return value


def validate_command_payload(payload: dict[str, Any], owner_target: str) -> None:
    """Require the exact watcher command line with absolute, matching paths."""
    if set(payload) != {"kind", "argv", "cwd", "timeoutSeconds"}:
        raise ValueError("command binding payload contains missing or unsupported fields")
    argv = payload.get("argv")
    if (
        not isinstance(argv, list)
        or len(argv) != 3
        or argv[:2] != ["sh", "-lc"]
        or not isinstance(argv[2], str)
    ):
        raise ValueError("command binding argv must be sh -lc plus one command line")
    match = WATCHER_COMMAND_RE.match(argv[2])
    if match is None:
        raise ValueError("command binding is not the canonical watcher command")
    base_dir, workspace, base_dir_again, target = match.groups()
    if base_dir != base_dir_again:
        raise ValueError("command binding base directories disagree")
    if target != owner_target:
        raise ValueError("command binding owner target differs from delivery target")
    if argv[2] != watcher_command(Path(workspace), Path(base_dir), owner_target):
        raise ValueError("command binding differs from the canonical watcher command")
    if payload.get("cwd") != workspace:
        raise ValueError("command binding cwd must be the workspace")
    if payload.get("timeoutSeconds") != WATCHER_TIMEOUT_SECONDS:
        raise ValueError(f"command binding timeoutSeconds must be {WATCHER_TIMEOUT_SECONDS}")


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
    kind = payload.get("kind")
    if kind == "command":
        expected = watcher_command(workspace, base_dir, str(delivery.get("to")))
        argv = payload.get("argv")
        if not isinstance(argv, list) or argv != ["sh", "-lc", expected]:
            raise ValueError("live watcher command does not match the canonical command")
        if payload.get("cwd") != str(workspace.resolve()):
            raise ValueError("live watcher cwd must be the workspace")
        if payload.get("timeoutSeconds") != WATCHER_TIMEOUT_SECONDS:
            raise ValueError(f"watcher timeoutSeconds must be {WATCHER_TIMEOUT_SECONDS}")
    elif kind == "agentTurn":
        if payload.get("message") != render_message(workspace, base_dir):
            raise ValueError("live cron prompt does not match the canonical runbook")
        if payload.get("model") != MODEL or payload.get("fallbacks") != []:
            raise ValueError("cron model or fallbacks do not match the required runtime")
        if payload.get("timeoutSeconds") != TIMEOUT_SECONDS:
            raise ValueError(f"cron timeoutSeconds must be {TIMEOUT_SECONDS}")
        if payload.get("lightContext") is not True:
            raise ValueError("cron lightContext must be true")
        if payload.get("toolsAllow") != TOOLS_ALLOW:
            raise ValueError("cron toolsAllow must include the required safe tool set")
    else:
        raise ValueError("cron payload kind must be command or agentTurn")
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
        "schedule": {
            "kind": "cron",
            "expr": require_string(schedule.get("expr"), "schedule.expr"),
            "tz": require_string(schedule.get("tz"), "schedule.tz"),
        },
        "sessionTarget": "isolated",
        "wakeMode": require_string(job.get("wakeMode"), "wakeMode"),
        "payload": (
            {
                "kind": "command",
                "argv": list(payload["argv"]),
                "cwd": payload["cwd"],
                "timeoutSeconds": WATCHER_TIMEOUT_SECONDS,
            }
            if kind == "command"
            else {
                "kind": "agentTurn",
                "message": payload["message"],
                "model": MODEL,
                "fallbacks": [],
                "timeoutSeconds": TIMEOUT_SECONDS,
                "lightContext": True,
                "toolsAllow": TOOLS_ALLOW,
            }
        ),
        "delivery": {
            "mode": "announce",
            "channel": "kolo",
            "to": require_string(delivery.get("to"), "delivery.to"),
        },
    }
    # Kolo omits agentId when the cron uses the platform's default agent.
    # Preserve and validate an explicit agent when the native export includes
    # one, but do not invent an identity that cannot be verified from live state.
    if "agentId" in job:
        projection["agentId"] = require_string(job.get("agentId"), "agentId")
    if kind == "agentTurn":
        for optional in ("thinking",):
            if optional in payload:
                projection["payload"][optional] = payload[optional]
    return validate_binding(projection)


def build_target_binding(job: Any, workspace: Path, base_dir: Path) -> dict[str, Any]:
    """Project the intended safe config from an existing live job identity.

    The canonical job is now the model-free watcher command. The target keeps
    the live job's identity, schedule, and delivery and replaces only the
    payload, whatever kind the live job currently has.
    """
    if not isinstance(job, dict):
        raise ValueError("live cron job must be a JSON object")
    target = dict(job)
    delivery = job.get("delivery")
    if not isinstance(delivery, dict):
        raise ValueError("cron delivery must be an object")
    target["payload"] = {
        "kind": "command",
        "argv": [
            "sh",
            "-lc",
            watcher_command(workspace, base_dir, require_string(delivery.get("to"), "delivery.to")),
        ],
        "cwd": str(workspace.resolve()),
        "timeoutSeconds": WATCHER_TIMEOUT_SECONDS,
    }
    return build_binding(target, workspace, base_dir)


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


DAY_INDEX = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_hours(text: str) -> tuple[list[int], int, int]:
    """Owner words like "Mon-Fri 09:00-17:00", "Mon,Wed,Fri 10-16", or
    "daily 7am-11pm" as (cron weekdays, first hour, last hour inclusive)."""
    import re

    words = (text or "").strip().lower().replace("–", "-")
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", words)
    if not match:
        raise ValueError("monitoring hours need a start and an end, for example \"Mon-Fri 09:00-17:00\"")
    def hour(raw: str, suffix: str | None) -> int:
        value = int(raw)
        if suffix == "pm" and value < 12:
            value += 12
        if suffix == "am" and value == 12:
            value = 0
        if not 0 <= value <= 23:
            raise ValueError("hours must be between 0 and 23")
        return value
    start, end = hour(match.group(1), match.group(3)), hour(match.group(4), match.group(6))
    if end < start:
        raise ValueError("the end hour must come after the start hour")
    last = end if match.group(5) and match.group(5) != "00" else max(start, end - 1)
    day_text = words[: match.start()].strip(" ,")
    if not day_text or day_text in {"daily", "every day", "everyday", "all days", "7 days"}:
        days = list(range(0, 7))
    elif day_text in {"weekdays", "business days"}:
        days = [1, 2, 3, 4, 5]
    else:
        days = []
        for part in re.split(r"[,\s]+", day_text):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                a, b = a[:3], b[:3]
                if a not in DAY_INDEX or b not in DAY_INDEX:
                    raise ValueError(f"unknown day range {part!r}")
                ia, ib = DAY_ORDER.index(a), DAY_ORDER.index(b)
                span = DAY_ORDER[ia: ib + 1] if ia <= ib else DAY_ORDER[ia:] + DAY_ORDER[: ib + 1]
                days += [DAY_INDEX[d] for d in span]
            else:
                key = part[:3]
                if key not in DAY_INDEX:
                    raise ValueError(f"unknown day {part!r}")
                days.append(DAY_INDEX[key])
    days = sorted(set(days))
    return days, start, last


def schedule_expr(text: str, every_minutes: int = 2) -> str:
    """The watcher's cron expression for the owner's monitoring hours."""
    days, start, last = parse_hours(text)
    day_field = "*" if len(days) == 7 else ",".join(str(d) for d in days)
    hours = f"{start}-{last}" if last > start else str(start)
    return f"*/{every_minutes} {hours} * * {day_field}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render-message")
    bind = sub.add_parser("bind-live")
    target = sub.add_parser("target-binding")
    watcher = sub.add_parser("render-watcher-command")
    hours = sub.add_parser("schedule-from-hours")
    hours.add_argument("--hours", required=True, help='owner words, e.g. "Mon-Fri 09:00-17:00"')
    hours.add_argument("--every-minutes", type=int, default=2)
    for command in (render, bind, target, watcher):
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--base-dir", type=Path, required=True)
    for command in (render, bind, target):
        command.add_argument("--output", type=Path, required=True)
    watcher.add_argument("--owner-target", required=True)
    bind.add_argument("--job", type=Path, required=True)
    target.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "schedule-from-hours":
            print(schedule_expr(args.hours, args.every_minutes))
        elif args.command == "render-watcher-command":
            print(watcher_command(args.workspace, args.base_dir, args.owner_target))
        elif args.command == "render-message":
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
