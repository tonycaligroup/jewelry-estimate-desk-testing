#!/usr/bin/env python3
"""One command that says whether a pod is ready to run the desk.

Run it during setup, before the cron is enabled, and after any platform
change. Every line is PASS, FAIL, or SKIP with the reason; exit status 1
when anything failed. It writes nothing and sends nothing to a customer.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import activation_binding
import inbox_monitor
import judge
import kolo_safe
import pipeline
import slots
import rehearsal
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

    # Rehearsal first, in capitals: it must be impossible to mistake for live.
    state = rehearsal.apply(workspace)
    if state["enabled"]:
        add("REHEARSAL MODE", "WARN", rehearsal.banner(state))
    # Profile
    try:
        profile = validate_profile.load_profile(desk / "shop-profile.json")
        result = validate_profile.validate_profile(profile, require_setup=True)
        add("shop profile", "PASS" if result.get("ready") else "FAIL", "; ".join(result.get("errors", []))[:200])
    except (OSError, ValueError) as exc:
        profile = {}
        add("shop profile", "FAIL", str(exc))
    # The installed scripts, file by file: a version number names a folder; the manifest proves the files.
    try:
        import manifest  # local import: keeps the readiness checks importable on their own

        verified = manifest.verify(base_dir)
        add("installed scripts", "PASS" if verified["ok"] else "FAIL", manifest.describe(verified))
    except (OSError, ValueError) as exc:
        add("installed scripts", "FAIL", f"manifest could not be checked: {exc}")
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
            import image_provider
            import inbox_watcher

            profile_now = validate_profile.load_profile(desk / "shop-profile.json")
            mode = inbox_watcher.desk_settings(profile_now if isinstance(profile_now, dict) else None)["model_provider"]
            judge.MODEL_PROVIDER_MODE = mode
            direct = image_provider.available(mode)
            transport = "direct (proxy reachable)" if direct else "cli"
            if direct:
                image_provider.reset_model_resolution()
                resolution = image_provider.resolve_model(switch.get("model"))
                model = resolution["model"]
                skipped = "; ".join(f"{item['model']} skipped: {item['reason']}" for item in resolution["skipped"])
            else:
                skipped = "preference probing skipped: direct provider unavailable"
            text = judge.complete('Reply with exactly {"ok":true}', model, runner, openclaw, timeout=90)
            detail = f"resolved model {model} via {transport}" + (f"; {skipped}" if skipped else "") + f": {text[:80]}"
            add("inline judgment", "PASS" if '"ok"' in text else "FAIL", detail)
        except Exception as exc:  # noqa: BLE001 - a readiness check reports, never crashes
            add("inline judgment", "FAIL", f"model {model}: {exc}")

    # Gmail gateway (the watcher's first call every tick), then the same
    # send-as preflight the watcher runs before any approval, read-only here.
    gmail_live = False
    try:
        import auth_health
        import gateway_token
        import gmail_fetch

        token, source = gateway_token.load_token_with_source()
        listing = gmail_fetch.fetch_json("messages", {"maxResults": 1}, token)
        add("gmail gateway", "PASS",
            f"inbox reachable, {listing.get('resultSizeEstimate', '?')} message(s) visible; credential from {source}")
        try:
            profile = json.loads((workspace / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            mailbox = str(((profile.get("shop") or {}) if isinstance(profile, dict) else {}).get("outbound_mailbox") or "")
        except (OSError, ValueError):
            mailbox = ""
        probe = auth_health.probe(token, mailbox)
        gmail_live = probe["status"] == auth_health.PASSED
        add("gmail send-as", "PASS" if gmail_live else "FAIL",
            f"{mailbox} authorized" if gmail_live else f"{probe['status']} (HTTP {probe['http_status']}): {auth_health.REPAIRS.get(probe['status'], '')}")
    except Exception as exc:  # noqa: BLE001 - a readiness check reports, never crashes
        add("gmail gateway", "FAIL", str(exc))

    # The watcher's authentication health record: reported, never written here.
    try:
        import auth_health

        health = auth_health.load_record(workspace)
        active = health.get("active_failure")
        if not isinstance(active, dict):
            add("gmail auth record", "PASS", auth_health.describe(health))
        elif gmail_live:
            add("gmail auth record", "WARN",
                f"recorded {active.get('class')} since {active.get('at')}; recovery is visible now, "
                "the next successful watcher tick clears the recorded incident")
        else:
            add("gmail auth record", "FAIL", auth_health.describe(health))
    except Exception as exc:  # noqa: BLE001
        add("gmail auth record", "WARN", f"could not read: {exc}")

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

    # The optional spreadsheet mirror: not configured, reachable (formatting re-applied), or unreachable.
    try:
        import sheet_mirror  # local import: keeps the readiness checks importable on their own

        status, detail = sheet_mirror.check(workspace)
        add("spreadsheet mirror", status, detail)
    except Exception as exc:  # noqa: BLE001
        add("spreadsheet mirror", "WARN", f"could not check: {exc}")
    # Desk state: what the doctor sees. A finding is not a readiness failure,
    # but the owner should know before the cron runs on top of it.
    try:
        import doctor  # local import: keeps the readiness checks importable on their own

        findings = doctor.scan(workspace)
        repairs = [f for f in findings if f["level"] == "repair"]
        add("desk state", "PASS" if not repairs else "WARN",
            "clean" if not findings else f"{len(repairs)} to repair, {len(findings) - len(repairs)} informational; run doctor.py")
    except (OSError, ValueError) as exc:
        add("desk state", "WARN", f"doctor could not scan: {exc}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument("--expect", default=None, help="the version the owner published; a mismatch is a FAIL")
    parser.add_argument("--cron-context", action="store_true",
                        help="run these checks under the watcher job's own shell line (sh -lc with the same env import)")
    parser.add_argument("--in-cron-context", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    import skill_version

    workspace, base_dir = args.workspace.resolve(), args.base_dir.resolve()
    if args.cron_context:
        return run_in_cron_context(workspace, base_dir, args.openclaw, args.expect)
    version = skill_version.installed(base_dir)
    print(f"version: {version}")
    results = checks(workspace, base_dir, args.openclaw)
    if args.expect:
        results.insert(0, {"check": "installed version", "status": "PASS" if version == args.expect else "FAIL",
                           "detail": f"installed {version}, expected {args.expect}"})
    if args.in_cron_context:
        results.insert(0, cron_context_row(workspace, version, results))
    for row in results:
        print(f"{row['status']:4} {row['check']}: {row['detail']}")
    failed = [r for r in results if r["status"] == "FAIL"]
    print("READY" if not failed else f"NOT READY ({len(failed)} failed)")
    return 0 if not failed else 1


STAMP_FILE = "readiness-cron-context.json"


def stamp_path(workspace: Path) -> Path:
    """Where a passing cron-context run leaves its proof; activation requires it (14 September 2026)."""
    return workspace / "estimate-desk" / "work" / STAMP_FILE


def cron_context_row(workspace: Path, version: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """The first line of a cron-context run: the shell the watcher gets, and the stamp activation reads."""
    import gateway_token
    import workflow_safe

    try:
        source = gateway_token.load_token_with_source()[1]
    except (OSError, ValueError):
        source = "none"
    ready = not any(r["status"] == "FAIL" for r in results)
    facts = {
        "at": datetime.now(timezone.utc).isoformat(),
        "version": version,
        "ready": ready,
        "home": os.environ.get("HOME", ""),
        "uid": os.getuid(),
        "credential_source": source,
        "litellm_base_url_set": bool(os.environ.get("LITELLM_BASE_URL")),
    }
    try:
        workflow_safe.write_private(stamp_path(workspace), facts)
        stamped = "stamped"
    except OSError as exc:
        stamped = f"stamp not written: {exc}"
    return {"check": "cron context", "status": "PASS",
            "detail": f"HOME={facts['home']} uid={facts['uid']} credential from {source}; {stamped}"}


def run_in_cron_context(workspace: Path, base_dir: Path, openclaw: str, expect: str | None) -> int:
    """Re-run this script the way the watcher job runs: `sh -lc` with the same environment import line."""
    import cron_config

    inner = (
        f"{cron_config.LITELLM_ENV_IMPORT} python3 {base_dir}/scripts/readiness.py "
        f"--workspace {workspace} --base-dir {base_dir} --openclaw {openclaw} --in-cron-context"
        + (f" --expect {expect}" if expect else "")
    )
    print("cron context: sh -lc, the watcher's own environment import")
    proc = subprocess.run(["sh", "-lc", inner], capture_output=True, text=True, timeout=600)
    sys.stdout.write(proc.stdout)
    if proc.stderr.strip():
        sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
