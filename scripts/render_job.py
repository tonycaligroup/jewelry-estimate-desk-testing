#!/usr/bin/env python3
"""Render one claim's views in a job of its own (ARCHITECTURE-OPTIONS.md C').

The watcher has a five-minute clock; a rendering for several pieces does
not fit. The tick spawns this script as a one-shot command job with the
watcher's own job shape and a longer clock. It renders, checks,
materializes, files the rendering card, and parks the claim, exactly as
the inline path did; a failure is counted on the claim and the lease
released, so the tick retries with its bound and then asks the owner.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import estimate_record
import inbox_claim
import inbox_monitor
import inbox_watcher
import rehearsal
import judge
import pipeline
import run_lease
import workflow_safe

Runner = Callable[..., subprocess.CompletedProcess[str]]
LEASE_SECONDS = 1020


def run(
    workspace: Path, base_dir: Path, message_id: str, estimate_id: str, openclaw: str,
    runner: Runner = subprocess.run, judge_runner: Runner = subprocess.run,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    p = inbox_watcher.paths_for(workspace)
    rehearsal.apply(workspace)
    desk = workspace / "estimate-desk"
    key = inbox_claim.claim_key(message_id)[:16]
    with run_lease.hold(desk, "render-job", key, seconds=LEASE_SECONDS):
        claim_token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
        inbox_claim.delegate(p["claim_root"], message_id, claim_token, LEASE_SECONDS)
        paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        settings = pipeline.settings(desk)
        try:
            done = pipeline.render_and_send(
                p, message_id, estimate_id, record, paths, openclaw, runner,
                model=settings.get("model"), judge_runner=judge_runner,
            )
        except (judge.JudgmentError, OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
            inbox_claim.note_inline_attempt(p["claim_root"], message_id, claim_token, str(exc), inbox_watcher._error_kind(exc))
            inbox_claim.release_lease(p["claim_root"], message_id, claim_token)
            raise
        return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--estimate-id", required=True)
    parser.add_argument("--openclaw", default=None)
    args = parser.parse_args(argv)
    try:
        done = run(args.workspace, args.base_dir.resolve(), args.message_id, args.estimate_id,
                   args.openclaw or inbox_watcher.default_openclaw())
    except (judge.JudgmentError, OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(done, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
