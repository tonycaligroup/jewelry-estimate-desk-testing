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
import time
from pathlib import Path
from typing import Any, Callable

import cron_config
import gateway_token
import gmail_fetch
import inbox_claim
import inbox_monitor
import judge
import kolo_safe
import pipeline
import owner_questions
import validate_profile
import workflow_safe

STALE_AFTER_SECONDS = 600
# Inline judgment runs inside the tick's 300 s clock. Stop taking new claims
# once this much of it is spent so the tick never times out mid-claim.
INLINE_BUDGET_SECONDS = 170
# One-shot worker jobs delete themselves after a clean run; one that errored
# lingers disabled. Sweep those once they are this old.
SWEEP_AFTER_SECONDS = 3600
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
    branch: str = "intake",
) -> str:
    message = cron_config.render_worker_message(
        workspace, base_dir, message_id, estimate_id, work_dir, branch
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


def sweep_worker_jobs(openclaw: str, runner: Runner = subprocess.run, now_ms: int | None = None) -> int:
    """Remove disabled one-shot worker jobs that errored and never self-deleted."""
    try:
        listed = runner(
            [openclaw, "cron", "list", "--json", "--all"],
            check=True, capture_output=True, text=True, shell=False,
        )
        raw = listed.stdout or ""
        data = json.loads(raw[raw.find("{" if raw.lstrip().startswith("{") else "["):])
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError):
        return 0
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return 0
    now = int(time.time() * 1000) if now_ms is None else now_ms
    removed = 0
    for job in jobs:
        if not isinstance(job, dict) or not str(job.get("name", "")).startswith(cron_config.WORKER_NAME_PREFIX):
            continue
        if job.get("enabled"):
            continue
        state = job.get("state") or {}
        last = state.get("lastRunAtMs") or job.get("updatedAtMs") or job.get("createdAtMs") or 0
        try:
            age_ms = now - int(last)
        except (TypeError, ValueError):
            continue
        if age_ms < SWEEP_AFTER_SECONDS * 1000:
            continue
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        try:
            runner([openclaw, "cron", "rm", job_id], check=True, capture_output=True, text=True, shell=False)
            removed += 1
        except (OSError, subprocess.CalledProcessError):
            continue
    return removed


def tick(
    workspace: Path,
    base_dir: Path,
    owner_target: str,
    openclaw: str = "openclaw",
    max_workers: int = DEFAULT_MAX_WORKERS,
    runner: Runner = subprocess.run,
    token: str | None = None,
    judge_runner: Runner = subprocess.run,
) -> dict[str, Any]:
    p = paths_for(workspace)
    summary: dict[str, Any] = {
        "discovered": 0,
        "claimed": 0,
        "closed": 0,
        "manual_review": 0,
        "workers": [],
        "spawn_failures": 0,
        "reminders": 0,
        "inline": [],
        "inline_failures": 0,
        "swept_jobs": 0,
        "message": "NO_REPLY",
    }
    started = time.monotonic()
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
    # A question the owner has not answered for a working day gets one
    # reminder, then waits (WORKFLOW.md 6.10).
    summary["reminders"] = owner_questions.send_due_reminders(
        owner_questions.questions_root(p["monitor_root"]), runner=runner,
        extra_args=kolo_safe.owner_channel_args(p["monitor_root"]),
    )
    discovery = gmail_fetch.discover(p["monitor_root"], token)
    summary["discovered"] = discovery.get("discovered", 0)
    summary["swept_jobs"] = sweep_worker_jobs(openclaw, runner=runner)

    while len(summary["workers"]) + len(summary["inline"]) < max_workers:
        if summary["inline"] and time.monotonic() - started > INLINE_BUDGET_SECONDS:
            # Enough of the clock is gone; the rest of the queue waits a tick.
            break
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
        inline = pipeline.settings(workspace / "estimate-desk")
        if inline.get("inline"):
            # Finish the claim here with one-shot judgment calls; no worker.
            try:
                done = pipeline.process_claim(
                    workspace, base_dir, message_id, result,
                    model=inline.get("model"), judge_runner=judge_runner, command_runner=runner,
                    openclaw=openclaw,
                )
            except judge.JudgmentError as exc:
                summary["inline_failures"] += 1
                if exc.transient:
                    # Leave the claim processing and unleased; the stale
                    # reconciler resumes it once and the next tick retries.
                    summary["inline"].append({"message_id": message_id, "outcome": "deferred", "error": str(exc)[:160]})
                    continue
                kolo_safe.manual_review_claimed(
                    p["monitor_root"], p["claim_root"], message_id, None,
                    "classification_malformed", runner=runner,
                )
                summary["inline"].append({"message_id": message_id, "outcome": "manual_review", "error": str(exc)[:160]})
                continue
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                summary["inline_failures"] += 1
                summary["inline"].append({"message_id": message_id, "outcome": "deferred", "error": str(exc)[:160]})
                continue
            if done.get("outcome") != "needs_worker":
                summary["inline"].append({"message_id": message_id, "outcome": done.get("outcome")})
                continue
            summary["inline"].append({"message_id": message_id, "outcome": "needs_worker", "next_action": done.get("next_action")})
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
                branch=cron_config.worker_branch(result.get("record_status")),
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
    notes = []
    if summary["spawn_failures"]:
        notes.append(f"{summary['spawn_failures']} worker job(s) could not be started; will retry.")
    deferred = [item for item in summary["inline"] if item.get("outcome") == "deferred"]
    if deferred:
        notes.append(
            f"{len(deferred)} claim(s) could not be judged this tick ({deferred[0].get('error', '')[:120]}); will retry."
        )
    if notes:
        note = "\n".join(notes)
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
