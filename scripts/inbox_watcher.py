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
from datetime import datetime, timezone
import json
import re
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
import rehearsal
import pipeline
import skill_version
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


RENDER_JOB_PREFIX = "jed-render-"
RENDER_JOB_TIMEOUT_SECONDS = 900
RENDER_JOB_LEASE_SECONDS = 1020


def render_job_command(base_dir: Path, workspace: Path, message_id: str, estimate_id: str) -> str:
    return (f"python3 {base_dir.resolve()}/scripts/render_job.py --workspace {workspace.resolve()} "
            f"--base-dir {base_dir.resolve()} --message-id {message_id} --estimate-id {estimate_id}")


def render_job_create_argv(openclaw: str, workspace: Path, message_id: str, command: str) -> list[str]:
    """One-shot command job in the watcher's own shape: isolated, no announce, its own clock."""
    # --best-effort-deliver: the job's own delivery step (it has no chat
    # target) must never mark a finished job errored, or --delete-after-run
    # does not fire and the job sits in the owner's routines list.
    return [
        openclaw, "cron", "create", "--at", "+5s", "--delete-after-run", "--best-effort-deliver", "--session", "isolated",
        "--name", f"{RENDER_JOB_PREFIX}{message_id[:12]}", "--command", command,
        "--command-cwd", str(workspace.resolve()), "--timeout-seconds", str(RENDER_JOB_TIMEOUT_SECONDS), "--json",
    ]


def spawn_render_job(workspace: Path, base_dir: Path, openclaw: str, message_id: str, estimate_id: str,
                     runner: Runner = subprocess.run) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id or "") or not re.fullmatch(r"jed-[0-9a-f]{16}", estimate_id or ""):
        raise ValueError("render job needs a plain message id and an estimate id")
    command = render_job_command(base_dir, workspace, message_id, estimate_id)
    # One seam for every desk subprocess (the tests fake it there too).
    completed = kolo_safe.run_command(render_job_create_argv(openclaw, workspace, message_id, command), runner=runner)
    raw = completed.stdout or ""
    try:
        job = json.loads(raw[raw.find("{"):])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("render job creation returned no job JSON") from exc
    job = job.get("job", job)
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("render job creation returned no job id")
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
        name = str(job.get("name", "")) if isinstance(job, dict) else ""
        if not (name.startswith(cron_config.WORKER_NAME_PREFIX) or name.startswith(RENDER_JOB_PREFIX)):
            continue
        if job.get("enabled"):
            continue
        state = job.get("state") or {}
        last = state.get("lastRunAtMs") or job.get("updatedAtMs") or job.get("createdAtMs") or 0
        try:
            age_ms = now - int(last)
        except (TypeError, ValueError):
            continue
        # A finished one-shot job (it ran and errored, or ran and could not
        # deliver) is removed on the very next tick; the owner's routines
        # list never shows the desk's leftovers. Anything else waits an hour.
        finished = str(state.get("lastRunStatus") or job.get("lastRunStatus") or "").lower() in {"error", "ok", "success", "done"}
        if not finished and age_ms < SWEEP_AFTER_SECONDS * 1000:
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

INLINE_LEASE_SECONDS = 300
TRANSIENT_ATTEMPTS = 6
DETERMINISTIC_ATTEMPTS = 2


TICK_LOG_KEEP = 40


def keep_summary(workspace: Path, summary: dict[str, Any]) -> None:
    """The last ticks' summaries, on disk, so a handoff or a deferral is never lost with the process."""
    try:
        root = workspace / "estimate-desk" / "run-work"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "tick-log.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except (OSError, ValueError):
            entries = []
        entries.append({"at": datetime.now(timezone.utc).isoformat(), **{k: v for k, v in summary.items() if k != "message"}})
        workflow_safe.write_private(path, entries[-TICK_LOG_KEEP:])
    except (OSError, ValueError):
        pass


def _inline_retry_candidates(p: dict[str, Path]) -> list[str]:
    """Processing claims the tick owns whose run ended without finishing: lapsed lease, no worker."""
    found: list[str] = []
    for item in inbox_monitor.all_queue_items(p["monitor_root"]):
        if item["processing_status"] != "processing":
            continue
        try:
            claim = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], item["gmail_message_id"]))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if claim.get("status") != "processing" or "inline_attempts" not in claim:
            continue
        if inbox_claim.recovery_lease_active(claim):
            continue
        found.append(item["gmail_message_id"])
    return sorted(found)


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, judge.JudgmentError):
        return "transient" if exc.transient else "deterministic"
    if isinstance(exc, (OSError, subprocess.CalledProcessError)):
        return "transient"
    return "deterministic"


def run_inline_claim(
    workspace: Path, base_dir: Path, p: dict[str, Path], message_id: str, owner_target: str, openclaw: str,
    runner: Runner, judge_runner: Runner, token: str | None, summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch, intake (once), and judge one claim in this process. Exceptions are the caller's.

    Safe to run again after a failure or a crash: the thread is fetched only
    when its file is missing, the intake result is reused when it exists,
    and every external effect is journaled by the code it calls.
    """
    summary = summary if summary is not None else {"workers": [], "render_jobs": [], "spawn_failures": 0, "inline": [], "closed": 0, "manual_review": 0}
    started = time.monotonic()
    calls_before = len(judge.CALL_LOG)
    claim_token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    inbox_claim.mark_inline(p["claim_root"], message_id, claim_token, True)
    inbox_claim.delegate(p["claim_root"], message_id, claim_token, INLINE_LEASE_SECONDS)
    paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
    if not Path(paths["gmail_thread"]).exists() or not Path(paths["gmail_message"]).exists():
        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, token or gateway_token.load_token())
    rehearsal_state = rehearsal.load(workspace)
    if rehearsal_state["enabled"] and rehearsal.should_hold(rehearsal_state, workflow_safe.read_object(Path(paths["gmail_message"]))):
        # Rehearsal (ARCHITECTURE-OPTIONS.md F2): not the rehearsal address,
        # so this is real mail; held untouched until rehearsal is off.
        rehearsal.hold(p["monitor_root"], p["claim_root"], message_id)
        summary["held"] = summary.get("held", 0) + 1
        held = {"message_id": message_id, "outcome": "held_for_live", "seconds": round(time.monotonic() - started, 2)}
        summary["inline"].append(held)
        return held
    step_path = Path(paths["work_dir"]) / workflow_safe.NEXT_STEP_FILE
    if step_path.exists():
        # An owner answer left one step for the tick (a price from the
        # record, a follow-up sent again): run that, not a full read.
        note = workflow_safe.read_object(step_path) or {}
        step = str(note.get("action") or "")
        estimate_id = str(note.get("estimate_id") or "")
        if not estimate_id:
            raise ValueError("the next step names no estimate")
        switch = pipeline.settings(workspace / "estimate-desk")
        if step == "price_from_record":
            done = pipeline.price_from_record(workspace, message_id, estimate_id, model=switch.get("model"),
                                              judge_runner=judge_runner, command_runner=runner, openclaw=openclaw)
        elif step == "resend_followup":
            done = pipeline.resend_followup(workspace, base_dir, message_id, estimate_id, model=switch.get("model"),
                                            judge_runner=judge_runner, command_runner=runner, openclaw=openclaw)
        else:
            raise ValueError(f"unknown next step {step!r}")
        step_path.unlink(missing_ok=True)
        stepped = {"message_id": message_id, "outcome": done.get("outcome"), "step": step,
                   "seconds": round(time.monotonic() - started, 2), "model_calls": len(judge.CALL_LOG) - calls_before}
        summary["inline"].append(stepped)
        return stepped
    intake_path = Path(paths["work_dir"]) / "intake-result.json"
    if intake_path.exists():
        result = workflow_safe.read_object(intake_path)
    else:
        result = workflow_safe.intake(
            argparse.Namespace(
                monitor_root=p["monitor_root"],
                claim_root=p["claim_root"],
                record_root=p["record_root"],
                message_id=message_id,
                shop_profile=p["shop_profile"],
                runner=runner,
            )
        )
        if result.get("next_action") == "done":
            if result.get("outcome") == "manual_review":
                summary["manual_review"] += 1
            else:
                summary["closed"] += 1
            return {"outcome": result.get("outcome", "done")}
        workflow_safe.write_private(intake_path, result)
    work_dir = result["work_paths"]["work_dir"]
    inline = pipeline.settings(workspace / "estimate-desk")
    if not inline.get("inline"):
        raise ValueError("inline judgment is switched off in pipeline.json; the desk has no other way to judge a claim")
    done = pipeline.process_claim(
        workspace, base_dir, message_id, result,
        model=inline.get("model"), judge_runner=judge_runner, command_runner=runner, openclaw=openclaw,
    )
    if done.get("outcome") == "render_job_requested":
        # The rendering runs in its own job. Count the spawn as an attempt:
        # a job that never finishes lapses its lease, the tick retries, and
        # after the bound the owner is asked.
        inbox_claim.note_inline_attempt(p["claim_root"], message_id, claim_token,
                                        "the rendering job started but did not finish", "transient")
        inbox_claim.delegate(p["claim_root"], message_id, claim_token, RENDER_JOB_LEASE_SECONDS)
        job_id = spawn_render_job(workspace, base_dir, openclaw, message_id, done.get("estimate_id") or result["estimate_id"], runner=runner)
        summary["render_jobs"].append({"message_id": message_id, "job_id": job_id})
        calls = judge.CALL_LOG[calls_before:]
        summary["inline"].append({"message_id": message_id, "outcome": "render_job_spawned", "job_id": job_id,
                                  "seconds": round(time.monotonic() - started, 2), "model_calls": len(calls)})
        return {"outcome": "render_job_spawned", "job_id": job_id}
    if done.get("outcome") == "needs_worker":
        # No worker agent exists any more (ARCHITECTURE-OPTIONS.md D): this
        # is a failure like any other, retried with the bound, then asked.
        raise judge.JudgmentError(done.get("error") or "the claim could not be finished inline", transient=True)
    calls = judge.CALL_LOG[calls_before:]
    summary["inline"].append({"message_id": message_id, "outcome": done.get("outcome"),
                              "seconds": round(time.monotonic() - started, 2), "model_calls": len(calls),
                              "model_seconds": round(sum(c["seconds"] for c in calls), 2)})
    return done


def _attempt_inline(
    workspace: Path, base_dir: Path, p: dict[str, Path], message_id: str, owner_target: str, openclaw: str,
    runner: Runner, judge_runner: Runner, token: str | None, summary: dict[str, Any],
) -> None:
    """One attempt at a claim; a failure is counted on the claim and the lease released for the next tick."""
    try:
        run_inline_claim(workspace, base_dir, p, message_id, owner_target, openclaw, runner, judge_runner, token, summary)
    except (judge.JudgmentError, OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        summary["inline_failures"] += 1
        kind = _error_kind(exc)
        try:
            claim_token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
            attempts = inbox_claim.note_inline_attempt(p["claim_root"], message_id, claim_token, str(exc), kind)
            inbox_claim.release_lease(p["claim_root"], message_id, claim_token)
        except (OSError, ValueError, json.JSONDecodeError):
            attempts = None
        summary["inline"].append({"message_id": message_id, "outcome": "deferred", "error": str(exc)[:160],
                                  "kind": kind, "attempts": attempts})


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
    rehearsal_state = rehearsal.apply(workspace)
    summary: dict[str, Any] = {
        "version": skill_version.installed(base_dir),
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
        "retried": 0,
        "stuck": [],
        "render_jobs": [],
        "message": "NO_REPLY",
    }
    started = time.monotonic()
    judge.reset_stats()
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
    # Kolo says nothing when a card is rejected; the audit trail does.
    summary["rejections"] = workflow_safe.handle_rejected_briefs(workspace, runner=runner)
    # Approvals of rendering and appointment cards are executed here, not
    # by the main session (ARCHITECTURE-OPTIONS.md A' tier 1).
    summary["approvals"] = workflow_safe.handle_approved_briefs(workspace, runner=runner)
    summary["reminders"] = owner_questions.send_due_reminders(
        owner_questions.questions_root(p["monitor_root"]), runner=runner,
        extra_args=kolo_safe.owner_channel_args(p["monitor_root"]),
    )
    discovery = gmail_fetch.discover(p["monitor_root"], token)
    summary["discovered"] = discovery.get("discovered", 0)
    summary["swept_jobs"] = sweep_worker_jobs(openclaw, runner=runner)

    # Claims the tick itself owns whose last run ended without finishing (a
    # deferral or a crash): retry them first, with a bound, then ask.
    for message_id in _inline_retry_candidates(p):
        if len(summary["workers"]) + len(summary["inline"]) >= max_workers:
            break
        if summary["inline"] and time.monotonic() - started > INLINE_BUDGET_SECONDS:
            break
        claim = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
        attempts = int(claim.get("inline_attempts") or 0)
        limit = DETERMINISTIC_ATTEMPTS if claim.get("last_error_kind") == "deterministic" else TRANSIENT_ATTEMPTS
        if attempts >= limit:
            asked = workflow_safe.ask_stuck_claim(p, message_id, str(claim.get("last_error") or "no error recorded"),
                                                 attempts, runner=runner)
            summary["stuck"].append({"message_id": message_id, "attempts": attempts, "question_id": asked.get("question_id")})
            continue
        summary["retried"] += 1
        _attempt_inline(workspace, base_dir, p, message_id, owner_target, openclaw, runner, judge_runner, token, summary)

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
        _attempt_inline(workspace, base_dir, p, message_id, owner_target, openclaw, runner, judge_runner, token, summary)

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
    summary["timing"] = {"tick_seconds": round(time.monotonic() - started, 2), **judge.stats()}
    notes = []
    if summary["spawn_failures"]:
        notes.append(f"{summary['spawn_failures']} worker job(s) could not be started; will retry.")
    deferred = [item for item in summary["inline"] if item.get("outcome") == "deferred"]
    if deferred:
        notes.append(
            f"{len(deferred)} claim(s) could not be judged this tick ({deferred[0].get('error', '')[:120]}); will retry."
        )
    # Claims this tick deferred are in flight, not stuck: the reconciler and
    # the next tick own them. The owner hears about a claim only when it
    # becomes a card or a question; the notes stay in the run log.
    if rehearsal_state.get("enabled"):
        notes.insert(0, rehearsal.banner(rehearsal_state) + (f"; {summary.get('held', 0)} held this tick" if summary.get("held") else ""))
    summary["notes"] = notes
    if report["message"] != "NO_REPLY" and not report.get("settled"):
        unleased = report["counts"]["processing"] - report.get("delegated", 0)
        if unleased <= len(deferred) and len(report["message"].splitlines()) == 1:
            summary["message"] = "NO_REPLY"
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
    keep_summary(args.workspace.resolve(), summary)
    print(summary["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
