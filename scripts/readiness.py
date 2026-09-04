#!/usr/bin/env python3
"""One command that says whether a pod is ready to run the desk.

Run it during setup, before the cron is enabled, and after any platform
change. Every line is PASS, FAIL, or SKIP with the reason; exit status 1
when anything failed. It writes nothing and sends nothing to a customer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import activation_binding
import inbox_monitor
import judge
import kolo_safe
import pipeline
import slots
import validate_profile

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run(argv: list[str], runner: Runner, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return runner(argv, capture_output=True, text=True, timeout=timeout, check=False)


def checks(workspace: Path, base_dir: Path, openclaw: str, runner: Runner = subprocess.run) -> list[dict[str, Any]]:
    desk = workspace / "estimate-desk"
    monitor_root = desk / "inbox-monitor"
    out: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str = "") -> None:
        out.append({"check": name, "status": status, "detail": detail[:200]})

    # Profile
    try:
        profile = validate_profile.load_profile(desk / "shop-profile.json")
        result = validate_profile.validate_profile(profile)
        add("shop profile", "PASS" if result.get("ready") else "FAIL", "; ".join(result.get("errors", []))[:200])
    except (OSError, ValueError) as exc:
        profile = {}
        add("shop profile", "FAIL", str(exc))
    scheduling = profile.get("scheduling") or {}
    if scheduling.get("calendar") and slots.parse_windows(scheduling):
        add("calendar and windows", "PASS", f"calendar {scheduling['calendar']}, {len(slots.parse_windows(scheduling))} window(s)")
    else:
        add("calendar and windows", "FAIL", "set scheduling.calendar and scheduling.windows or appointments cannot be offered")

    # Activation binding (approver + owner channel default)
    try:
        binding = activation_binding.load(activation_binding.binding_path(monitor_root))
        add("activation binding", "PASS", "owner messages default to the activation thread")
    except (OSError, ValueError) as exc:
        binding = None
        add("activation binding", "FAIL", str(exc))

    # Monitor state
    try:
        state = inbox_monitor.load_monitor_state(monitor_root)
        add("monitor state", "PASS" if state.get("activation_state") == "active" else "FAIL", str(state.get("activation_state")))
    except (OSError, ValueError) as exc:
        add("monitor state", "FAIL", str(exc))

    # Inline judgment model
    switch = pipeline.settings(desk)
    model = switch.get("model") or judge.DEFAULT_MODEL
    if not switch.get("inline"):
        add("inline judgment", "SKIP", "pipeline.json turns it off; worker jobs will be used")
    else:
        try:
            proc = _run(judge.infer_argv('Reply with exactly {"ok":true}', model, openclaw), runner, timeout=90)
            text = judge._unwrap(proc.stdout) if proc.returncode == 0 else ""
            add("inline judgment", "PASS" if '"ok"' in text else "FAIL", f"model {model}: " + (text[:80] or proc.stderr[:120]))
        except Exception as exc:  # noqa: BLE001 - a readiness check reports, never crashes
            add("inline judgment", "FAIL", f"model {model}: {exc}")

    # Gmail gateway (the watcher's first call every tick)
    try:
        import gateway_token
        import gmail_fetch

        listing = gmail_fetch.fetch_json("messages", {"maxResults": 1}, gateway_token.load_token())
        add("gmail gateway", "PASS", f"inbox reachable, {listing.get('resultSizeEstimate', '?')} message(s) visible")
    except Exception as exc:  # noqa: BLE001 - a readiness check reports, never crashes
        add("gmail gateway", "FAIL", str(exc))

    # Audit trail (rejections are read from it)
    proc = _run(["kolo", "audit-query", "--page-size", "1"], runner)
    ok = proc.returncode == 0 and '"status": "ok"' in proc.stdout.replace("\n", "")
    add("audit trail access", "PASS" if ok else "FAIL", "kolo audit-query works" if ok else (proc.stderr or proc.stdout)[:120])

    # Kolo backend
    proc = _run(["kolo", "ping"], runner)
    add("kolo backend", "PASS" if proc.returncode == 0 else "FAIL", (proc.stdout or proc.stderr).strip()[:80])

    # Watcher job
    proc = _run([openclaw, "cron", "list", "--json", "--all"], runner)
    try:
        jobs = json.loads(proc.stdout)
        jobs = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
        watcher = next((j for j in jobs if str(j.get("name")) == "jed-inbox-monitor"), None)
    except (ValueError, TypeError):
        watcher = None
    if watcher is None:
        add("watcher cron", "FAIL", "no job named jed-inbox-monitor")
    else:
        add("watcher cron", "PASS", f"enabled={watcher.get('enabled')} schedule={watcher.get('schedule') or watcher.get('cron')}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--openclaw", default="openclaw")
    args = parser.parse_args(argv)
    results = checks(args.workspace.resolve(), args.base_dir.resolve(), args.openclaw)
    for row in results:
        print(f"{row['status']:4} {row['check']}: {row['detail']}")
    failed = [r for r in results if r["status"] == "FAIL"]
    print("READY" if not failed else f"NOT READY ({len(failed)} failed)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
