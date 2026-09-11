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
from concurrent.futures import ThreadPoolExecutor
import threading
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
import estimate_record
import inbox_monitor
import image_provider
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


RENDER_JOB_PREFIX = "jed-render-"  # one-shot render jobs of 4.10 to 4.12; the sweep still removes leftovers


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


TICK_LOG_KEEP = 720  # a day of two-minute ticks: enough to answer "did we miss ticks?" the next morning


TICK_MARK_FILE = "tick-started.json"
_MARK_LOCK = threading.Lock()
DEFAULT_CLAIMS_PER_TICK = 16
DEFAULT_PARALLEL_CLAIMS = 1  # RELEASE-PLAN-4.14.md 2.5: shipped sequential; raised in the profile after the burst test
MAX_CLAIMS_PER_TICK = 64
MAX_PARALLEL_CLAIMS = 16


def desk_settings(profile: dict[str, Any] | None) -> dict[str, Any]:
    """The profile's desk block: claims_per_tick, parallel_claims; and model.provider."""
    block = (profile or {}).get("desk") if isinstance(profile, dict) else None
    block = block if isinstance(block, dict) else {}
    model_block = (profile or {}).get("model") if isinstance(profile, dict) else None
    model_block = model_block if isinstance(model_block, dict) else {}

    def whole(value: Any, default: int, ceiling: int) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= ceiling else default

    provider = str(model_block.get("provider") or "auto").strip().lower()
    return {
        "claims_per_tick": whole(block.get("claims_per_tick"), DEFAULT_CLAIMS_PER_TICK, MAX_CLAIMS_PER_TICK),
        "parallel_claims": whole(block.get("parallel_claims"), DEFAULT_PARALLEL_CLAIMS, MAX_PARALLEL_CLAIMS),
        "model_provider": provider if provider in ("auto", "direct", "cli") else "auto",
    }


def mark_tick(workspace: Path, **fields: Any) -> None:
    """A tick writes its start (and each claim it takes) here; a tick that dies leaves this behind, unmatched by a log entry.

    Claims in flight are a list (several at once under RELEASE-PLAN-4.14.md 2.2); the newest is also `message_id`.
    """
    with _MARK_LOCK:
        try:
            root = workspace / "estimate-desk" / "run-work"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / TICK_MARK_FILE
            current: dict[str, Any] = {}
            if fields.get("started") is None:
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    current = {}
            current = current if isinstance(current, dict) else {}
            claims = list(current.get("claims") or []) if fields.get("started") is None else []
            if fields.get("message_id") and fields["message_id"] not in claims:
                claims.append(fields["message_id"])
            workflow_safe.write_private(path, {**current, **fields, "claims": claims})
        except (OSError, ValueError):
            pass


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


def _run_claims_in_parallel(
    workspace: Path, base_dir: Path, p: dict[str, Path], owner_target: str, openclaw: str, runner: Runner,
    judge_runner: Runner, token: str | None, summary: dict[str, Any], candidates: list[str], rendering_later: list[str],
    retry_or_ask: Callable[[str], None], max_workers: int, parallel: int, started: float,
) -> None:
    """Claims in threads (RELEASE-PLAN-4.14.md 2.2, 2.4): one customer's claims in order, customers side by side.

    Retries of ordinary claims are listed first, then new mail is claimed
    (up to the tick's cap), then renderings under way. The list is grouped
    by thread; each group runs in order on one worker; groups run
    `parallel` at a time. Each task writes its own summary, merged under a
    lock, so the tick's log is whole whatever the interleaving.
    """
    order: list[tuple[str, str, str]] = []  # (thread, message_id, kind)
    for message_id in [m for m in candidates if m not in rendering_later]:
        order.append((_thread_of(p, message_id), message_id, "retry"))
    while len(order) < max_workers:
        if order and time.monotonic() - started > INLINE_BUDGET_SECONDS:
            break
        claimed = inbox_monitor.claim_next(p["monitor_root"], p["claim_root"], STALE_AFTER_SECONDS)
        if claimed is None:
            break
        if not claimed["claim"].get("acquired"):
            continue
        summary["claimed"] += 1
        order.append((str(claimed["queue_item"].get("thread_id") or claimed["queue_item"]["gmail_message_id"]),
                      claimed["queue_item"]["gmail_message_id"], "new"))
    for message_id in rendering_later[: max(0, max_workers - len(order))]:
        order.append((_thread_of(p, message_id), message_id, "retry"))

    groups: dict[str, list[tuple[str, str]]] = {}
    for thread, message_id, kind in order:
        groups.setdefault(thread, []).append((message_id, kind))
    lock = threading.Lock()

    def run_group(items: list[tuple[str, str]]) -> None:
        for message_id, kind in items:
            part = _empty_summary()
            if kind == "retry":
                # retry_or_ask writes into the shared summary; it is small and locked here.
                with lock:
                    retry_or_ask(message_id)
                continue
            _attempt_inline(workspace, base_dir, p, message_id, owner_target, openclaw, runner, judge_runner, token, part)
            with lock:
                _merge_summary(summary, part)

    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(groups) or 1))) as pool:
        list(pool.map(run_group, groups.values()))


def _rendering_under_way(p: dict[str, Path], message_id: str) -> bool:
    """A claim whose work folder holds rendering progress: its next step is a view, not mail."""
    try:
        paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
    except (OSError, ValueError):
        return False
    return (Path(paths["work_dir"]) / pipeline.PROGRESS_FILE).exists()


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, judge.JudgmentError):
        return "transient" if exc.transient else "deterministic"
    if isinstance(exc, (OSError, subprocess.CalledProcessError)):
        return "transient"
    return "deterministic"


TICK_MARGIN_SECONDS = 30  # the view step plans to be done this long before the watcher's timeout


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
    mark_tick(workspace, message_id=message_id, step="claim")
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
    progress_path = Path(paths["work_dir"]) / pipeline.PROGRESS_FILE
    if progress_path.exists() and not (Path(paths["work_dir"]) / workflow_safe.NEXT_STEP_FILE).exists():
        # A rendering under way (RELEASE-PLAN-4.12.md follow-up): one more
        # view this tick, no re-reading of the customer, the card when the
        # last view is done. (A rendering inside an owner's step, concierge
        # mode, continues through that step below.)
        intake_path = Path(paths["work_dir"]) / "intake-result.json"
        estimate_id = str((workflow_safe.read_object(intake_path) if intake_path.exists() else {}).get("estimate_id") or "")
        if not estimate_id:
            raise ValueError("a rendering is under way but its estimate is unknown")
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        switch = pipeline.settings(workspace / "estimate-desk")
        tick_started = summary.get("tick_started")
        deadline = (tick_started + cron_config.WATCHER_TIMEOUT_SECONDS - TICK_MARGIN_SECONDS) if tick_started else None
        mark_tick(workspace, message_id=message_id, step="rendering view")
        done = pipeline.render_step(p, message_id, estimate_id, record, paths, openclaw, runner,
                                    model=switch.get("model"), judge_runner=judge_runner, deadline=deadline)
        if done.get("outcome") == "rendering_in_progress":
            return _rendering_continues(p, message_id, claim_token, done, summary, started, calls_before)
        calls = judge.CALL_LOG[calls_before:]
        summary["inline"].append({"message_id": message_id, "outcome": done.get("outcome"),
                                  "seconds": round(time.monotonic() - started, 2), "model_calls": len(calls)})
        return done
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
                judge_runner=judge_runner,
                openclaw=openclaw,
                model=pipeline.settings(workspace / "estimate-desk").get("model"),
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
    if done.get("outcome") == "rendering_in_progress":
        return _rendering_continues(p, message_id, claim_token, done, summary, started, calls_before)
    if done.get("outcome") == "needs_worker":
        # No worker agent exists any more (ARCHITECTURE-OPTIONS.md D): this
        # is a failure like any other, retried with the bound, then asked.
        raise judge.JudgmentError(done.get("error") or "the claim could not be finished inline", transient=True)
    calls = judge.CALL_LOG[calls_before:]
    summary["inline"].append({"message_id": message_id, "outcome": done.get("outcome"),
                              "seconds": round(time.monotonic() - started, 2), "model_calls": len(calls),
                              "model_seconds": round(sum(c["seconds"] for c in calls), 2)})
    return done


def _rendering_continues(p: dict[str, Path], message_id: str, claim_token: str, done: dict[str, Any],
                         summary: dict[str, Any], started: float, calls_before: int) -> dict[str, Any]:
    """A view is done and more remain: release the claim for the next tick; not a failure, not an attempt."""
    inbox_claim.release_lease(p["claim_root"], message_id, claim_token)
    calls = judge.CALL_LOG[calls_before:]
    entry = {"message_id": message_id, "outcome": "rendering_in_progress", "done": done.get("done"), "of": done.get("of"),
             "seconds": round(time.monotonic() - started, 2), "model_calls": len(calls)}
    summary["inline"].append(entry)
    summary["render_jobs"].append({"message_id": message_id, "done": done.get("done"), "of": done.get("of")})
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


def _empty_summary() -> dict[str, Any]:
    return {"workers": [], "render_jobs": [], "spawn_failures": 0, "inline": [], "closed": 0, "manual_review": 0,
            "held": 0, "stuck": [], "inline_failures": 0, "retried": 0}


def _merge_summary(into: dict[str, Any], part: dict[str, Any]) -> None:
    for key, value in part.items():
        if isinstance(value, list):
            into.setdefault(key, []).extend(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            into[key] = into.get(key, 0) + value


def _thread_of(p: dict[str, Path], message_id: str) -> str:
    try:
        return str(inbox_monitor.load_queue_item(p["monitor_root"], message_id).get("thread_id") or message_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return message_id


def tick(
    workspace: Path,
    base_dir: Path,
    owner_target: str,
    openclaw: str = "openclaw",
    max_workers: int | None = None,
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
    summary["tick_started"] = started
    mark_tick(workspace, started=datetime.now(timezone.utc).isoformat(), message_id=None, step=None)
    judge.reset_stats()
    image_provider.reset_model_resolution()
    profile_loaded = validate_profile.load_profile(p["shop_profile"])
    profile_result = validate_profile.validate_profile(profile_loaded)
    if not profile_result.get("ready"):
        raise ValueError("shop profile is not ready: " + "; ".join(profile_result.get("errors", [])))
    desk = desk_settings(profile_loaded if isinstance(profile_loaded, dict) else None)
    judge.MODEL_PROVIDER_MODE = desk["model_provider"]
    switch = pipeline.settings(workspace / "estimate-desk")
    if max_workers is None:
        max_workers = desk["claims_per_tick"]
    parallel = desk["parallel_claims"]
    direct = image_provider.available(desk["model_provider"])
    summary["transport"] = "direct" if direct else "cli"
    if direct:
        resolution = image_provider.resolve_model(switch.get("model"))
        summary["model"] = resolution["model"]
        summary["model_resolution"] = resolution
    else:
        summary["model"] = switch.get("model") or judge.DEFAULT_MODEL
        summary["model_resolution"] = {
            "model": summary["model"], "pinned": bool(switch.get("model")),
            "skipped": [{"model": name, "reason": "direct provider unavailable; CLI identity was not probed"}
                        for name in image_provider.MODEL_PREFERENCE],
        }
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
    # The sweep of one-shot jobs is gone with the jobs (4.13.0); one fewer CLI call per tick (RELEASE-PLAN-4.14.md 2.2).

    # Claims the tick itself owns whose last run ended without finishing (a
    # deferral or a crash): retry them first, with a bound, then ask. A
    # rendering under way goes last, after new mail: views take minutes
    # through the CLI and must never delay a customer's message (8
    # September 2026).
    candidates = _inline_retry_candidates(p)
    rendering_later = [m for m in candidates if _rendering_under_way(p, m)]

    def retry_or_ask(message_id: str) -> None:
        claim = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
        attempts = int(claim.get("inline_attempts") or 0)
        limit = DETERMINISTIC_ATTEMPTS if claim.get("last_error_kind") == "deterministic" else TRANSIENT_ATTEMPTS
        if attempts >= limit:
            asked = workflow_safe.ask_stuck_claim(p, message_id, str(claim.get("last_error") or "no error recorded"),
                                                 attempts, runner=runner)
            summary["stuck"].append({"message_id": message_id, "attempts": attempts, "question_id": asked.get("question_id")})
            return
        summary["retried"] += 1
        _attempt_inline(workspace, base_dir, p, message_id, owner_target, openclaw, runner, judge_runner, token, summary)

    if parallel <= 1:
        # Sequential, exactly as before 4.14: claim, run, claim, run.
        for message_id in [m for m in candidates if m not in rendering_later]:
            if len(summary["workers"]) + len(summary["inline"]) >= max_workers:
                break
            if summary["inline"] and time.monotonic() - started > INLINE_BUDGET_SECONDS:
                break
            retry_or_ask(message_id)

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

        # Now the renderings, with whatever clock is left.
        for message_id in rendering_later:
            if len(summary["workers"]) + len(summary["inline"]) >= max_workers:
                break
            retry_or_ask(message_id)
    else:
        _run_claims_in_parallel(workspace, base_dir, p, owner_target, openclaw, runner, judge_runner, token, summary,
                                candidates, rendering_later, retry_or_ask, max_workers, parallel, started)

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
    if image_provider.MODEL_EVENTS:
        summary["model_mismatches"] = list(image_provider.MODEL_EVENTS)
    # The optional spreadsheet mirror (RELEASE-PLAN-4.15.md 2.8): rewritten when the desk's state changed, after
    # the customers' work, best effort; a Google failure is journaled and never reaches the owner or a customer.
    # The owner's cost sheet is read first (9 September 2026): drafts are kept, blocks marked ready are priced.
    try:
        import sheet_mirror  # local import: keeps the watcher importable without the mirror's dependencies

        if time.monotonic() - started < max(60.0, cron_config.WATCHER_TIMEOUT_SECONDS - 60):
            try:
                summary["sheet"] = sheet_mirror.pull(workspace)
            except Exception as exc:  # noqa: BLE001 - a failing read never stops the write (live, 10 September 2026)
                summary["sheet"] = {"pulled": False, "reason": str(exc)[:120]}
            summary["mirror"] = sheet_mirror.push(workspace)
    except Exception as exc:  # noqa: BLE001
        summary["mirror"] = {"pushed": False, "reason": str(exc)[:120]}
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
