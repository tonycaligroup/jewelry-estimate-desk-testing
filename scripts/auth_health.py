#!/usr/bin/env python3
"""The desk's Gmail authentication health: one read-only preflight, one durable record, one notice a day.

Before the watcher executes an approval, reads a rejection, or touches a
customer, it asks the gateway for the account's send-as list and requires the
configured outbound mailbox to appear exactly once. That proves the credential
reaches an account authorized to send from that mailbox. A 200 without the
alias is not proof of a wrong account (the right account may have lost the
alias), so it is classified as the mailbox not being authorized.

The record at `estimate-desk/run-work/gmail-auth-health.json` keeps the last
success, the active failure, and the last failure for evidence. Only a
successful watcher preflight clears the active failure; readiness and the
doctor report it and never write here. The owner hears once a day per class,
through the bound owner channel, never through cron stdout, and the notice
carries no response body, credential, or header. Every read-modify-write of
the record holds a file lock, so two overlapping ticks cannot double-send a
notice or clear a failure the other is recording (14 September 2026).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import gmail_fetch
import kolo_safe
import workflow_safe


RECORD_FILE = "gmail-auth-health.json"
LOCK_FILE = "gmail-auth-health.lock"
CHECK = "settings/sendAs"

PASSED = "passed"
GATEWAY_KEY_REJECTED = "gateway_key_rejected"
INTEGRATION_DISCONNECTED = "integration_disconnected"
GATEWAY_FORBIDDEN = "gateway_forbidden"
OUTBOUND_MAILBOX_NOT_AUTHORIZED = "outbound_mailbox_not_authorized"
INVALID_GATEWAY_RESPONSE = "invalid_gateway_response"

FAILURE_CLASSES = (
    GATEWAY_KEY_REJECTED,
    INTEGRATION_DISCONNECTED,
    GATEWAY_FORBIDDEN,
    OUTBOUND_MAILBOX_NOT_AUTHORIZED,
    INVALID_GATEWAY_RESPONSE,
)

# What the owner is told to do, per class. Plain words, one action each.
REPAIRS = {
    GATEWAY_KEY_REJECTED: "run `kolo gateway restart`, then the readiness check in cron context",
    INTEGRATION_DISCONNECTED: "reconnect Gmail in Settings > Integrations, then run the readiness check",
    GATEWAY_FORBIDDEN: "run the readiness check and report its Gmail lines; the gateway refused for a reason other than a disconnected integration",
    OUTBOUND_MAILBOX_NOT_AUTHORIZED: "verify the connected Gmail account and its \"Send mail as\" settings, then run the readiness check",
    INVALID_GATEWAY_RESPONSE: "run the readiness check and report its Gmail lines; the gateway's send-as answer was malformed",
}


def record_path(workspace: Path) -> Path:
    return Path(workspace) / "estimate-desk" / "run-work" / RECORD_FILE


def _empty_record() -> dict[str, Any]:
    return {"schema": 1, "last_success_at": None, "active_failure": None, "last_failure": None, "notice": {}}


def load_record(workspace: Path) -> dict[str, Any]:
    """The record; a missing file reads as empty, an unreadable one reads as empty and says so in `unreadable`."""
    path = record_path(workspace)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_record()
    except OSError as exc:
        return {**_empty_record(), "unreadable": f"cannot read: {exc}"[:200]}
    try:
        value = json.loads(text)
    except ValueError as exc:
        return {**_empty_record(), "unreadable": f"not JSON: {exc}"[:200]}
    if not isinstance(value, dict):
        return {**_empty_record(), "unreadable": "not a JSON object"}
    problem = _shape_problem(value)
    if problem:
        return {**_empty_record(), "unreadable": problem}
    record = _empty_record()
    record.update({k: value.get(k) for k in ("last_success_at", "active_failure", "last_failure") if k in value})
    record["notice"] = value.get("notice") if isinstance(value.get("notice"), dict) else {}
    return record


def _shape_problem(value: dict[str, Any]) -> str | None:
    """Structural corruption reads as unreadable, never as healthy: a failure entry that is not one is not 'no failure'."""
    if value.get("schema", 1) != 1:
        return f"unsupported schema {value.get('schema')!r}"
    if value.get("last_success_at") is not None and not isinstance(value.get("last_success_at"), str):
        return "last_success_at is not a timestamp"
    for key in ("active_failure", "last_failure"):
        entry = value.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            return f"{key} is not a failure entry"
        if entry.get("class") not in FAILURE_CLASSES:
            return f"{key} has an unknown class {entry.get('class')!r}"
        if not isinstance(entry.get("at"), str):
            return f"{key} has no timestamp"
    if "notice" in value and value.get("notice") is not None and not isinstance(value.get("notice"), dict):
        return "notice is not an object"
    return None


def save_record(workspace: Path, record: dict[str, Any]) -> None:
    """Write the record; an unreadable predecessor is moved aside first, and if it cannot be, nothing is written over it."""
    path = record_path(workspace)
    clean = {k: v for k, v in record.items() if k != "unreadable"}
    if record.get("unreadable") and path.exists():
        moment = datetime.now(timezone.utc)
        aside = path.with_name(f"{path.stem}.corrupt-{moment.strftime('%Y%m%dT%H%M%S')}-{moment.microsecond:06d}{path.suffix}")
        try:
            path.replace(aside)
        except OSError as exc:
            raise OSError(f"cannot move the unreadable authentication record aside, so it is left untouched: {exc}") from exc
    workflow_safe.write_private(path, clean)


@contextmanager
def _locked(workspace: Path) -> Iterator[None]:
    """One writer at a time for the record: two ticks overlapping never both send, never race a clear."""
    lock = record_path(workspace).with_name(LOCK_FILE)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock.open("a", encoding="utf-8") as handle:
        try:
            lock.chmod(0o600)
        except OSError:
            pass
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def classify_error(error: BaseException) -> str | None:
    """A failure class for a gateway HTTP error, by status, never by message text; None when it is not an auth failure."""
    status = getattr(error, "status", None)
    if not isinstance(status, int):
        return None
    if status == 401:
        return GATEWAY_KEY_REJECTED
    if status == 403:
        detail = str(getattr(error, "detail", "") or "").lower()
        if "no active connection" in detail:
            return INTEGRATION_DISCONNECTED
        return GATEWAY_FORBIDDEN
    return None


def match_send_as(response: Any, mailbox: str) -> str:
    """passed when exactly one alias matches the mailbox case-insensitively; otherwise the class that says why not."""
    wanted = (mailbox or "").strip().casefold()
    if not wanted or "@" not in wanted:
        raise ValueError("outbound mailbox is missing or invalid")
    aliases = response.get("sendAs") if isinstance(response, dict) else None
    if not isinstance(aliases, list):
        return INVALID_GATEWAY_RESPONSE
    matches = 0
    for alias in aliases:
        if not isinstance(alias, dict):
            return INVALID_GATEWAY_RESPONSE
        address = alias.get("sendAsEmail")
        if not isinstance(address, str):
            return INVALID_GATEWAY_RESPONSE
        if address.strip().casefold() == wanted:
            matches += 1
    if matches == 1:
        return PASSED
    if matches == 0:
        return OUTBOUND_MAILBOX_NOT_AUTHORIZED
    return INVALID_GATEWAY_RESPONSE


def probe(token: str, mailbox: str, opener: Callable[..., Any] = gmail_fetch.urlopen) -> dict[str, Any]:
    """One send-as request, classified. Raises only what is not an authentication failure (transport, 5xx, 429)."""
    try:
        response = gmail_fetch.fetch_json(CHECK, None, token, opener)
    except gmail_fetch.GatewayHTTPError as exc:
        classified = classify_error(exc)
        if classified is None:
            raise
        return {"status": classified, "http_status": exc.status, "check": CHECK}
    return {"status": match_send_as(response, mailbox), "http_status": 200, "check": CHECK}


def preflight(
    workspace: Path,
    token: str,
    credential_source: str,
    mailbox: str,
    version: str,
    opener: Callable[..., Any] = gmail_fetch.urlopen,
    cron_context: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Probe, then update the record under the lock: a pass clears only the active failure; a failure becomes active and last."""
    result = probe(token, mailbox, opener)
    at = _now(now).isoformat()
    with _locked(workspace):
        record = load_record(workspace)
        if result["status"] == PASSED:
            record["last_success_at"] = at
            record["active_failure"] = None
        else:
            entry = {
                "at": at,
                "version": version,
                "http_status": result["http_status"],
                "class": result["status"],
                "credential_source": credential_source,
                "cron_context": bool(cron_context),
                "check": CHECK,
                "repair": REPAIRS.get(result["status"], "run the readiness check"),
                "last_success_at": record.get("last_success_at"),
            }
            record["active_failure"] = entry
            record["last_failure"] = entry
        save_record(workspace, record)
    return {**result, "record": {k: v for k, v in record.items() if k != "unreadable"}}


def notice_text(status: str, mailbox: str, workspace: Path, base_dir: Path) -> str:
    """The owner's sentence: what is wrong in plain words and the one line to run. No gateway words, no credential."""
    readiness = (
        f"python3 {base_dir}/scripts/readiness.py --workspace {workspace} --base-dir {base_dir} --cron-context"
    )
    head = "The Jewelry Estimate Desk cannot use Gmail and has sent nothing. "
    if status == GATEWAY_KEY_REJECTED:
        body = "The platform gateway rejected the desk's credential (HTTP 401). Run `kolo gateway restart`, then: "
    elif status == INTEGRATION_DISCONNECTED:
        body = "The Gmail integration is disconnected (HTTP 403). Reconnect Gmail in Settings > Integrations, then run: "
    elif status == GATEWAY_FORBIDDEN:
        body = "The gateway refused the desk's request (HTTP 403) for a reason other than a disconnected integration. Run and report the Gmail lines of: "
    elif status == OUTBOUND_MAILBOX_NOT_AUTHORIZED:
        body = (
            f"The connected Gmail account is not authorized to send as {mailbox}. "
            "Verify the Gmail account and its \"Send mail as\" settings, then run: "
        )
    elif status == INVALID_GATEWAY_RESPONSE:
        body = f"The gateway's send-as answer was malformed or listed {mailbox} more than once. Run and report the Gmail lines of: "
    else:
        body = "Run: "
    return head + body + readiness


def notice_due(record: dict[str, Any], status: str, now: datetime | None = None) -> bool:
    """Once per UTC day per class, each class with its own day; alternating classes never re-notify within a day."""
    notice = record.get("notice") if isinstance(record.get("notice"), dict) else {}
    sent = notice.get("sent") if isinstance(notice.get("sent"), dict) else {}
    today = _now(now).strftime("%Y-%m-%d")
    return sent.get(status) != today


def notify_if_due(
    workspace: Path,
    monitor_root: Path,
    status: str,
    text: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Send through the bound owner channel; mark the day sent only when the notify command succeeds.

    Delivery is recorded apart from the failure itself. A failed send keeps the
    failure recorded and leaves the day unsent, so a later tick tries again.
    `kolo notify-owner` gives no delivery receipt; the command's success is the
    strongest confirmation the platform offers. The lock is held across the
    send so an overlapping tick cannot send the same notice.
    """
    with _locked(workspace):
        record = load_record(workspace)
        if not notice_due(record, status, now):
            return {"sent": False, "reason": "already sent today"}
        # Bound to the failure that is active now: an overlapping tick may have
        # passed its preflight and cleared this one since it was recorded, in
        # which case saying "the desk cannot use Gmail" would be false.
        active = record.get("active_failure")
        if not isinstance(active, dict) or active.get("class") != status:
            return {"sent": False, "reason": "failure no longer active"}
        moment = _now(now)
        notice = dict(record.get("notice") or {})
        notice["attempts"] = int(notice.get("attempts") or 0) + 1
        notice["last_attempt_at"] = moment.isoformat()
        try:
            kolo_safe.tell_owner(monitor_root, text, runner=runner)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            words = str(exc)
            for extra in (getattr(exc, "stderr", None), getattr(exc, "stdout", None)):
                if isinstance(extra, str) and extra.strip():
                    words += ": " + extra.strip()
            notice["last_error"] = words[:200]
            record["notice"] = notice
            save_record(workspace, record)
            return {"sent": False, "reason": "notify failed; will retry on a later tick"}
        sent = dict(notice.get("sent") or {}) if isinstance(notice.get("sent"), dict) else {}
        sent[status] = moment.strftime("%Y-%m-%d")
        notice.update({"sent": sent, "sent_on": sent[status], "sent_at": moment.isoformat(), "class": status})
        notice.pop("last_error", None)
        record["notice"] = notice
        save_record(workspace, record)
        return {"sent": True, "reason": "sent"}


def describe(record: dict[str, Any]) -> str:
    """One line for readiness and the doctor."""
    if record.get("unreadable"):
        return f"record unreadable ({record['unreadable']}); the next watcher preflight moves it aside and starts a new one"
    active = record.get("active_failure")
    if not isinstance(active, dict):
        last = record.get("last_success_at")
        return "no active failure" + (f"; last success {last}" if last else "")
    return (
        f"{active.get('class')} since {active.get('at')} (HTTP {active.get('http_status')}, "
        f"credential from {active.get('credential_source')}): {active.get('repair')}"
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="print the desk's Gmail authentication health record")
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(load_record(args.workspace.resolve()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
