#!/usr/bin/env python3
"""One model-free tick of the inbox monitor.

The watcher is the scheduled job. It does everything that never needs
judgment: validate the shop profile, reconcile stale work, discover new
Gmail, claim each message, fetch it, and run the deterministic intake. Mail
no customer wrote is closed on the spot. Every claim that still needs a
human-style reading is handed to one short-lived worker job with its own
clock, and the claim is leased to that worker so the next tick leaves it
alone. The tick then prints the owner-facing report, or NO_REPLY.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import cron_config
import gateway_token
import gmail_fetch
import inbox_claim
import inbox_monitor
import kolo_safe
import validate_profile
import workflow_safe

STALE_AFTER_SECONDS = 600
# The agent lane on this pod runs two jobs at once. Spawning more than that
# per tick would queue workers behind each other with their timeouts running;
# the rest of the queue simply waits for the next tick, unclaimed.
DEFAULT_MAX_WORKERS = 2
Runner = Callable[..., subprocess.CompletedProcess[str]]


def default_openclaw() -> str:
    """Find the OpenClaw CLI the way the gateway shell would."""
    return shutil.which("openclaw") or "/usr/local/bin/openclaw"


def paths_for(workspace: Path) -> dict[str, Path]:
    desk = workspace / "estimate-desk"
    return {
        "monitor_root": desk / "inbox-monitor",
        "claim_root": desk / "inbox-claims",
        "record_root": desk / "records",
        "shop_profile": desk / "shop-profile.json",
    }


def worker_create_argv(
    openclaw: str,
    message_id: str,
    message: str,
    owner_target: str,
) -> list[str]:
    """Argument array for one worker job; never a shell string."""
    return [
        openclaw,
        "cron",
        "create",
        "--at",
        "+5s",
        "--delete-after-run",
        "--session",
        "isolated",
        "--name",
        f"{cron_config.WORKER_NAME_PREFIX}{message_id[:12]}",
        "--message",
        message,
        "--model",
        cron_config.MODEL,
        "--thinking",
        cron_config.WORKER_THINKING,
        "--tools",
        ",".join(cron_config.TOOLS_ALLOW),
        "--timeout-seconds",
        str(cron_config.WORKER_TIMEOUT_SECONDS),
        "--light-context",
        # A worker has no owner-facing output of its own: approvals, alerts,
        # and briefs all go through bundled commands. Delivery stays off so
        # narration or a stray final line can never reach the owner's phone.
        "--no-deliver",
        "--json",
    ]


def spawn_worker(
    workspace: Path,
    base_dir: Path,
    owner_target: str,
    openclaw: str,
    message_id: str,
    estimate_id: str,
    work_dir: str,
    runner: Runner = subprocess.run,
) -> str:
    message = cron_config.render_worker_message(
        workspace, base_dir, message_id, estimate_id, work_dir
    )
    completed = runner(
        worker_create_argv(openclaw, message_id, message, owner_target),
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    raw = completed.stdout or ""
    try:
        job = json.loads(raw[raw.find("{"):])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("worker job creation returned no job JSON") from exc
    job = job.get("job", job)
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("worker job creation returned no job id")
    return job_id


def tick(
    workspace: Path,
    base_dir: Path,
    owner_target: str,
    openclaw: str = "openclaw",
    max_workers: int = DEFAULT_MAX_WORKERS,
    runner: Runner = subprocess.run,
    token: str | None = None,
) -> dict[str, Any]:
    p = paths_for(workspace)
    summary: dict[str, Any] = {
        "discovered": 0,
        "claimed": 0,
        "closed": 0,
        "manual_review": 0,
        "workers": [],
        "spawn_failures": 0,
        "message": "NO_REPLY",
    }
    profile_result = validate_profile.validate_profile(
        validate_profile.load_profile(p["shop_profile"])
    )
    if not profile_result.get("ready"):
        raise ValueError("shop profile is not ready: " + "; ".join(profile_result.get("errors", [])))
    state = inbox_monitor.load_monitor_state(p["monitor_root"])
    if state["activation_state"] != "active":
        summary["skipped"] = state["activation_state"]
        return summary
    if token is None:
        token = gateway_token.load_token()

    inbox_claim.reconcile_stale_notifications(p["claim_root"], STALE_AFTER_SECONDS)
    kolo_safe.reconcile_stale_claims(
        p["monitor_root"], p["claim_root"], STALE_AFTER_SECONDS, runner=runner
    )
    discovery = gmail_fetch.discover(p["monitor_root"], token)
    summary["discovered"] = discovery.get("discovered", 0)

    while len(summary["workers"]) < max_workers:
        claimed = inbox_monitor.claim_next(
            p["monitor_root"], p["claim_root"], STALE_AFTER_SECONDS
        )
        if claimed is None:
            break
        if not claimed["claim"].get("acquired"):
            continue
        message_id = claimed["queue_item"]["gmail_message_id"]
        summary["claimed"] += 1
        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, token)
        result = workflow_safe.intake(
            argparse.Namespace(
                monitor_root=p["monitor_root"],
                claim_root=p["claim_root"],
                record_root=p["record_root"],
                message_id=message_id,
                shop_profile=p["shop_profile"],
            )
        )
        if result.get("next_action") == "done":
            if result.get("outcome") == "manual_review":
                summary["manual_review"] += 1
            else:
                summary["closed"] += 1
            continue
        work_dir = result["work_paths"]["work_dir"]
        workflow_safe.write_private(Path(work_dir) / "intake-result.json", result)
        claim_token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
        inbox_claim.delegate(
            p["claim_root"], message_id, claim_token, cron_config.WORKER_LEASE_SECONDS
        )
        try:
            job_id = spawn_worker(
                workspace,
                base_dir,
                owner_target,
                openclaw,
                message_id,
                result["estimate_id"],
                work_dir,
                runner=runner,
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            # Leave the claim processing with its lease. Once the lease ends
            # the stale reconciler resumes it exactly once, and the next tick
            # spawns again; a second failure becomes manual review. Nothing
            # is dropped silently.
            summary["spawn_failures"] += 1
            continue
        summary["workers"].append({"message_id": message_id, "job_id": job_id})

    # The owner's channel may be a phone. Reviews reach the owner as approval
    # briefs, so the tick itself speaks only when something is wrong: an
    # uncertain alert or action, or a worker that could not be started.
    report = inbox_monitor.run_report(
        p["monitor_root"],
        p["claim_root"],
        announce=True,
        in_flight_ok=True,
        review_lines=False,
    )
    summary["message"] = report["message"]
    if summary["spawn_failures"]:
        note = f"{summary['spawn_failures']} worker job(s) could not be started; will retry."
        summary["message"] = note if report["message"] == "NO_REPLY" else report["message"] + "\n" + note
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--owner-target", required=True)
    parser.add_argument("--openclaw", default=None)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        summary = tick(
            args.workspace.resolve(),
            args.base_dir.resolve(),
            args.owner_target,
            args.openclaw or default_openclaw(),
            args.max_workers,
        )
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        # Stdout is what the owner sees; stderr is what the run log keeps.
        print(f"Inbox monitor tick failed: {exc}")
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    if args.summary is not None:
        workflow_safe.write_private(args.summary, summary)
    print(summary["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
