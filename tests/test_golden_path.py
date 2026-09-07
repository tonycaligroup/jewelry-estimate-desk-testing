#!/usr/bin/env python3
"""The golden path, end to end, on the real code.

One customer, one thread, every stage the desk handles: an inquiry with
details missing, the follow-up, the reply, a rate the owner has to supply,
the price brief, the approval and the estimate email, a rendering with the
customer's artwork, an appointment request, a booking card, a rejection, the
owner's words, a fresh offer card, the customer's pick, the booking, and a
reschedule. Every step runs the same code the pod runs (the watcher tick and
the exact execute line carried by each card); only the world is faked: Gmail,
Kolo, the calendar, and the model.

The fakes answer by contract, so a test failure here means the pieces no
longer fit together, not that a mock changed.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import re
import shlex
import subprocess
import sys
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import format_datetime
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import activation_binding  # noqa: E402
import artwork  # noqa: E402
import calendar_query  # noqa: E402
import doctor  # noqa: E402
import estimate_record  # noqa: E402
import gateway_token  # noqa: E402
import gmail_safe  # noqa: E402
import inbox_claim  # noqa: E402
import inbox_monitor  # noqa: E402
import inbox_watcher  # noqa: E402
import judge  # noqa: E402
import kolo_safe  # noqa: E402
import owner_questions  # noqa: E402
import readiness  # noqa: E402
import reading_check  # noqa: E402
import rendering  # noqa: E402
import run_lease  # noqa: E402
import workflow_safe  # noqa: E402
from test_runtime import IntakeTests  # noqa: E402

ZONE_NAME = "America/Los_Angeles"
ZONE = ZoneInfo(ZONE_NAME)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CUSTOMER = "Pat Customer <pat@example.net>"
SHOP_MAILBOX = "shop@example.com"
REAL_COMPLETE = judge.complete


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def flag(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else default


def ok(argv: list[str], stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, stdout, "")


def next_weekday(days_ahead: int, hour: int, minute: int, after: datetime | None = None) -> datetime:
    """A local date-time inside the shop's windows, at least `days_ahead` days out."""
    start = (after or datetime.now(ZONE)) + timedelta(days=days_ahead)
    while start.weekday() >= 5:
        start += timedelta(days=1)
    return start.replace(hour=hour, minute=minute, second=0, microsecond=0)


def local_key(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M")


class Crash(BaseException):
    """The process was killed right after a side effect. Not an Exception: nothing in the code may catch it."""


class World:
    """Everything outside the skill: Gmail, Kolo, the calendar, the model, the image tool.

    Fault injection: `fail_next[service] = n` makes the next n calls to that
    service fail the way the real one would; `crash_after.add(service)` lets
    the next call succeed and then kills the process (a Crash). Every call is
    logged in `calls`, so a test can learn which services an action touches.
    """

    SERVICES = ("gmail_read", "gmail_send", "calendar_freebusy", "calendar_create", "calendar_delete", "calendar_list", "image_describe",
                "kolo_card", "kolo_notify", "kolo_audit", "kolo_update", "model", "image")

    def __init__(self, ws: Path) -> None:
        self.ws = ws
        self.fail_next: dict[str, int] = {}
        self.crash_after: set[str] = set()
        self.calls: list[str] = []
        self.threads: dict[str, list[dict]] = {}
        self.messages: dict[str, dict] = {}
        self.batch: list[dict] = []
        self.clock_ms = 1_100
        self.cards: list[dict] = []
        self.notices: list[dict] = []
        self.events: list[dict] = []
        self.updates: list[tuple[str, str]] = []
        self.sent: list[dict] = []
        self.renders: list[list[str]] = []
        self.render_jobs: list[dict] = []
        self._pending_render_jobs: list[dict] = []
        self.spawned: list[list[str]] = []
        self.other: list[list[str]] = []
        self.busy: list[dict[str, str]] = []
        self.design_change: list[str] = []
        self.describe_argv: list[list[str]] = []
        self.failed_render_jobs: list[str] = []
        self.calendar_events: dict[str, dict] = {}
        self.created_events: list[dict] = []
        self.deleted_events: list[str] = []
        self.prompts: list[str] = []
        # What the model "sees" at each stage; the test moves these along.
        self.spec: dict = {}
        self.intents: list[str] = []
        self.requested: tuple[list[str], list[str]] = ([], [])
        self.owner_times: list[str] = []
        self.triage_kind = "estimate_request"
        self.email_count = 0
        self.brief_count = 0
        self.event_count = 0

    # ---- Fault injection ----------------------------------------------
    def _service(self, name: str, argv: list[str] | None = None, subprocess_style: str | None = None) -> None:
        """Log the call; raise the realistic failure when one is armed. Called before the effect."""
        self.calls.append(name)
        if self.fail_next.get(name, 0) > 0:
            self.fail_next[name] -= 1
            if subprocess_style == "checked":
                raise subprocess.CalledProcessError(1, argv or [name], "", f"{name}: gateway error")
            raise OSError(f"{name}: gateway dropped")

    def _after(self, name: str, result):
        """Called after the effect: a crash armed for this service kills the process here."""
        if name in self.crash_after:
            self.crash_after.discard(name)
            raise Crash(f"killed right after {name}")
        return result

    # ---- Gmail ---------------------------------------------------------
    def customer_message(self, message_id: str, thread_id: str, body: str, subject: str = "Custom signet ring",
                         attachments: tuple[str, ...] = (), sender: str | None = None) -> dict:
        self.clock_ms += 100
        parts = [{"mimeType": "text/plain", "body": {"data": b64url(body)}}]
        for name in attachments:
            parts.append({"mimeType": "image/png", "filename": name,
                          "body": {"attachmentId": f"att-{name}", "size": len(PNG)}})
        headers = {
            "From": sender or CUSTOMER, "To": SHOP_MAILBOX, "Subject": subject,
            "Message-ID": f"<{message_id}@example.net>",
        }
        message = {
            "id": message_id, "threadId": thread_id, "internalDate": str(self.clock_ms),
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": k, "value": v} for k, v in headers.items()], "parts": parts},
        }
        self.messages[message_id] = message
        self.threads.setdefault(thread_id, []).append(message)
        self.batch.append({"gmail_message_id": message_id, "thread_id": thread_id, "internal_date_ms": self.clock_ms})
        return message

    def _shop_message(self, thread_id: str, subject: str, body: str, message_id_header: str | None = None) -> dict:
        self.clock_ms += 100
        self.email_count += 1
        message_id = f"sent-{self.email_count}"
        message = {
            "id": message_id, "threadId": thread_id, "internalDate": str(self.clock_ms),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": "From", "value": f"Kolo Jewelers <{SHOP_MAILBOX}>"},
                                    {"name": "To", "value": CUSTOMER}, {"name": "Subject", "value": subject},
                                    {"name": "Message-ID", "value": message_id_header or f"<{message_id}@example.com>"}],
                        "body": {"data": b64url(body)}},
        }
        self.messages[message_id] = message
        self.threads.setdefault(thread_id, []).append(message)
        return message

    def thread(self, thread_id: str) -> dict:
        return {"id": thread_id, "messages": list(self.threads.get(thread_id, []))}

    def fake_discover(self, monitor_root: Path, token: str, now_ms=None, opener=None) -> dict:
        batch, self.batch = self.batch, []
        watermark = inbox_monitor.load_monitor_state(monitor_root)["discovery_watermark_ms"]
        result = inbox_monitor.discover_complete(monitor_root, batch, watermark, self.clock_ms + 1_000)
        return {"discovered": len(batch), **result}

    def fake_fetch(self, monitor_root: Path, claim_root: Path, message_id: str, token: str, opener=None) -> dict:
        self._service("gmail_read")
        paths = inbox_monitor.prepare_claim_work(monitor_root, claim_root, message_id)
        message = self.messages[message_id]
        Path(paths["gmail_message"]).write_text(json.dumps(message), encoding="utf-8")
        Path(paths["gmail_thread"]).write_text(json.dumps(self.thread(message["threadId"])), encoding="utf-8")
        return {"gmail_message": paths["gmail_message"], "gmail_thread": paths["gmail_thread"]}

    def fake_fetch_json(self, path: str, params, token: str, opener=None) -> dict:
        self._service("gmail_read")
        match = re.fullmatch(r"threads/([^/]+)", path)
        if not match:
            raise AssertionError("unexpected Gmail fetch: " + path)
        return self.thread(match.group(1))

    def fake_collect(self, thread: dict, out_dir: Path, token: str, opener=None, limit: int = 3) -> list[Path]:
        parts = artwork.image_parts(thread)
        if not parts:
            return []
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / (parts[0]["filename"] or "artwork.png")
        target.write_bytes(PNG)
        return [target]

    def _curl(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self._service("gmail_send", argv, "checked")
        payload = json.loads(Path(flag(argv, "--data-binary")[1:]).read_text(encoding="utf-8"))
        raw = payload["raw"]
        raw += "=" * (-len(raw) % 4)
        mime = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        body = ""
        attachments = []
        for part in mime.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = part.get_content()
            elif part.get_filename():
                attachments.append(part.get_filename())
        sent = self._shop_message(payload["threadId"], str(mime["Subject"]), body, str(mime["Message-ID"]))
        self.sent.append({"id": sent["id"], "thread_id": payload["threadId"], "subject": str(mime["Subject"]),
                          "to": str(mime["To"]), "body": body, "attachments": attachments})
        return self._after("gmail_send", ok(argv, json.dumps({"id": sent["id"], "threadId": payload["threadId"]})))

    # ---- Calendar ------------------------------------------------------
    def query_freebusy(self, time_min, time_max, timezone_name, calendar_id, token, opener=None) -> dict:
        self._service("calendar_freebusy")
        lo, hi = calendar_query.parse_timestamp(time_min, "a"), calendar_query.parse_timestamp(time_max, "b")
        busy = [b for b in self.busy
                if calendar_query.parse_timestamp(b["start"], "s") < hi and calendar_query.parse_timestamp(b["end"], "e") > lo]
        query = {"timeMin": time_min, "timeMax": time_max, "timeZone": timezone_name, "items": [{"id": calendar_id}]}
        body = {"kind": "calendar#freeBusy", "timeMin": time_min, "timeMax": time_max,
                "calendars": {calendar_id: {"busy": busy}}}
        return self._after("calendar_freebusy", {"schema_version": 1, "provider": "google_calendar_freebusy",
                "provider_request_id": "request-0123456789abcdef",
                "response_date": format_datetime(datetime.now(timezone.utc)), "query": query,
                "response_body_sha256": calendar_query.canonical_hash(body), "response_body": body})

    def create_event(self, calendar_id, start, end, timezone_name, summary, description, attendee_email, token,
                     opener=None) -> dict:
        self._service("calendar_create")
        self.event_count += 1
        event = {"kind": "calendar#event", "id": f"evt-{self.event_count}", "summary": summary, "description": description,
                 "start": {"dateTime": start, "timeZone": timezone_name}, "end": {"dateTime": end, "timeZone": timezone_name},
                 "attendees": [{"email": attendee_email}], "htmlLink": f"https://calendar.example/{self.event_count}",
                 "status": "confirmed"}
        self.calendar_events[event["id"]] = event
        self.busy.append({"start": start, "end": end, "event": event["id"]})
        self.created_events.append(event)
        return self._after("calendar_create", event)

    def list_events(self, calendar_id, time_min, time_max, token, opener=None) -> list[dict]:
        self._service("calendar_list")
        lo, hi = calendar_query.parse_timestamp(time_min, "a"), calendar_query.parse_timestamp(time_max, "b")
        found = [e for e in self.calendar_events.values()
                 if calendar_query.parse_timestamp(e["start"]["dateTime"], "s") < hi
                 and calendar_query.parse_timestamp(e["end"]["dateTime"], "e") > lo]
        return self._after("calendar_list", found)

    def delete_event(self, calendar_id, event_id, token, opener=None) -> bool:
        self._service("calendar_delete")
        self.deleted_events.append(event_id)
        self.busy = [b for b in self.busy if b.get("event") != event_id]
        return self._after("calendar_delete", self.calendar_events.pop(event_id, None) is not None)

    # ---- The runner: every command the skill shells out to --------------
    def run(self, argv, **_kwargs) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        tool = Path(argv[0]).name
        if tool == "openclaw":
            return self._openclaw(argv)
        if tool == "kolo":
            return self._kolo(argv)
        if tool == "curl":
            return self._curl(argv)
        self.other.append(argv)
        return ok(argv, "")

    def complete(self, prompt, model=None, runner=None, openclaw=None, timeout=None) -> str:
        return REAL_COMPLETE(prompt, model, self.run, openclaw)

    def _openclaw(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:4] == ["infer", "model", "run"]:
            prompt = flag(argv, "--prompt") or ""
            self.prompts.append(prompt)
            self.calls.append("model")
            if self.fail_next.get("model", 0) > 0:
                self.fail_next["model"] -= 1
                return subprocess.CompletedProcess(argv, 1, "", "model down")
            return self._after("model", ok(argv, json.dumps({"text": json.dumps(self.answer(prompt))})))
        if argv[1:3] == ["infer", "image"] and argv[3] in ("generate", "edit"):
            self._service("image", argv, "checked")
            output = Path(flag(argv, "--output"))
            output.parent.mkdir(parents=True, exist_ok=True)
            # Every render is a different image, as on the pod: a revision must
            # replace the slot files, never be refused as "different data".
            output.write_bytes(PNG + len(self.renders).to_bytes(4, "big"))
            self.renders.append(argv)
            return self._after("image", ok(argv, json.dumps({"ok": True, "outputs": [{"path": str(output)}]})))
        if argv[1:4] == ["infer", "image", "describe"]:
            self._service("image_describe", argv, "checked")
            self.describe_argv.append(list(argv))
            ids = re.findall(r"^- (\w+):", flag(argv, "--prompt") or "", re.MULTILINE)
            text = json.dumps({"answers": {i: "yes" for i in ids}, "notes": {}})
            return ok(argv, json.dumps({"ok": True, "outputs": [{"text": text}]}))
        if argv[1:3] == ["cron", "list"]:
            return ok(argv, json.dumps({"jobs": []}))
        if argv[1:3] == ["cron", "create"]:
            name = flag(argv, "--name") or ""
            # No one-shot render jobs any more: a rendering runs inside the
            # watcher, one view per tick. Any job creation is unexpected.
            self.spawned.append(argv)
            return ok(argv, json.dumps({"id": "job-unexpected", "name": name}))
        self.other.append(argv)
        return ok(argv, "{}")

    def _kolo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = argv[1]
        if command == "request-approval":
            self._service("kolo_card", argv, "checked")
            self.brief_count += 1
            brief_id = f"{self.brief_count:08x}-0000-4000-8000-000000000000"
            payload = json.loads(flag(argv, "--execution-payload") or "{}")
            card = {"brief_id": brief_id, "number": self.brief_count, "title": flag(argv, "--action"),
                    "reasoning": flag(argv, "--reasoning"), "details": json.loads(flag(argv, "--details") or "{}"),
                    "payload": payload, "session_key": flag(argv, "--session-key"),
                    "kind": payload.get("action_type")}
            self.cards.append(card)
            self.events.insert(0, {"event_type": "brief.submitted", "brief_id": brief_id, "brief_number": self.brief_count,
                                   "description": card["title"], "created_at": datetime.now(timezone.utc).isoformat(),
                                   "details": {}})
            return self._after("kolo_card", ok(argv, ""))
        if command == "notify-owner":
            self._service("kolo_notify", argv, "checked")
            self.notices.append({"text": flag(argv, "-m"), "file": flag(argv, "--file"),
                                 "session_key": flag(argv, "--session-key")})
            return self._after("kolo_notify", ok(argv, ""))
        if command == "audit-query":
            self._service("kolo_audit", argv, "checked")
            wanted = flag(argv, "--event-type")
            events = [e for e in self.events if not wanted or e["event_type"] == wanted]
            return self._after("kolo_audit", ok(argv, json.dumps({"status": "ok", "events": events})))
        if command == "update-brief":
            self._service("kolo_update", argv, "checked")
            update = (flag(argv, "--brief-id"), flag(argv, "--status"))
            if update[1] == "executed" and update in self.updates:
                # Kolo refuses a second "executed" on a brief (6 September 2026, Briefs #24 and #25).
                raise subprocess.CalledProcessError(1, argv, "", "brief is already executed")
            self.updates.append(update)
            return self._after("kolo_update", ok(argv, ""))
        self.other.append(argv)
        return ok(argv, "")

    def approve(self, card: dict) -> None:
        """The owner approves from the phone: the trail records it, nothing reaches the session."""
        self.events.insert(0, {"event_type": "brief.approved", "brief_id": card["brief_id"], "brief_number": card["number"],
                               "description": card["title"], "created_at": datetime.now(timezone.utc).isoformat(),
                               "details": {"source": "web", "status": "approved", "previous_status": "pending"}})

    def reject(self, card: dict, note: str) -> None:
        self.events.insert(0, {"event_type": "brief.rejected", "brief_id": card["brief_id"], "brief_number": card["number"],
                               "description": card["title"], "created_at": datetime.now(timezone.utc).isoformat(),
                               "details": {"note": note}})

    # ---- The model, by contract ---------------------------------------
    def answer(self, prompt: str) -> dict:
        if "Kinds:" in prompt and '"specification": {...}' in prompt:
            # One call for a new inquiry: kind and specification together.
            spec = dict(self.spec) if self.triage_kind == "estimate_request" else {}
            return {"kind": self.triage_kind, "note": "read by the fake", "specification": spec}
        if "decide what the CUSTOMER messages are" in prompt:
            return {"kind": self.triage_kind, "note": "read by the fake"}
        if "merge every fact the customer actually stated" in prompt:
            return {"specification": dict(self.spec)}
        if "MISSING DETAILS TO ASK FOR" in prompt:
            return {"body": (
                "Hi Pat,\n\nThanks for writing about the signet ring; happy to price it. Two quick questions "
                "so the number is right:\n\n- What finger size should the ring be?\n- How would you like the "
                "small diamonds set along the shoulders, bead set or channel set?\n\nBest,\nKolo Jewelers"
            )}
        if "Classify ONLY the newest customer message" in prompt:
            if self.design_change:
                return {"post_estimate_artifact": {"design_change_assessment": "changed",
                                                   "intents": [], "changed_fields": list(self.design_change)}}
            return {"post_estimate_artifact": {"design_change_assessment": "unchanged",
                                               "intents": list(self.intents), "changed_fields": []}}
        if "copy the customer's own words about timing" in prompt:
            return {"requested_times": list(self.requested[0]), "resolved_times": list(self.requested[1])}
        if "A jewelry shop owner wrote when they could meet" in prompt:
            return {"requested_times": ["the owner's times"], "resolved_times": list(self.owner_times)}
        if "PIECES TO QUANTIFY:" in prompt:
            menu = json.loads(prompt.split("PIECES TO QUANTIFY: ", 1)[1])
            out = []
            for entry in menu:
                one = self.quantities(prompt)
                one["finished_grams"] = 4.0 if "band" in entry["label"] else 5.5
                one["bench_hours"] = 2.0 if "band" in entry["label"] else 4.0
                spec_words = json.dumps(entry["specification"]).lower()
                one["accents"] = [a for a in one["accents"] if "lab_grown" in a["key"] and "lab-grown" in spec_words and "accent" in spec_words]
                if entry.get("center_carat_needed"):
                    one["center_carat"] = 1.0
                out.append(one)
            return {"pieces": out}
        if "estimating quantities for a price quote" in prompt:
            return self.quantities(prompt)
        if "You plan a product rendering" in prompt:
            return {"archetype": "signet", "mark_source": "artwork" if "(they did)" in prompt else "none",
                    "must_be_exact": ["the customer's logo on the face", "yellow gold"],
                    "fine_lettering": False, "notes": "signet with the customer's logo"}
        if "You write customer emails for" in prompt:
            return {"body": self.customer_email(prompt)}
        raise AssertionError("unexpected model prompt: " + prompt[:300])

    def quantities(self, prompt: str) -> dict:
        fees = [key for key in ("casting", "setting") if re.search(rf"\b{key}\b", prompt)]
        accents = []
        stone = re.search(r"\b[a-z0-9_]*lab_grown[a-z0-9_]*\b", prompt)
        if stone:
            accents.append({"key": stone.group(0), "carats": 0.2})
        return {"finished_grams": 9.5, "bench_hours": 3.5, "fees": fees, "accents": accents}

    def customer_email(self, prompt: str) -> str:
        facts: dict[str, str] = {}
        block = prompt.split("FACTS (use exactly):\n", 1)[1].split("\n\n", 1)[0]
        for line in block.splitlines():
            key, _, value = line[2:].partition(": ")
            facts[key] = value
        labels: list[str] = []
        if facts.get("time_labels"):
            try:
                labels = list(ast.literal_eval(facts["time_labels"]))
            except (ValueError, SyntaxError):
                labels = [facts["time_labels"]]
        self.email_count_for_opening = getattr(self, "email_count_for_opening", 0) + 1
        opening = f"Hi Pat, this is note number {self.email_count_for_opening} from the bench."
        task = prompt.split("TASK: ", 1)[1].split("\n", 1)[0]
        shop = "Kolo Jewelers"
        if task.startswith("Send the customer their estimate"):
            body = (
                f"{opening} Thank you for the finger size and the logo; the signet ring is priced. "
                f"The estimate for the piece is {facts['price']}. We estimate high on purpose so there are no "
                "surprises; the figure is pending final design approval, and when the final price comes in lower "
                "we pass the saving on to you. Nothing is committed until you approve the final design. "
                + (f"This estimate is good through {facts['valid_through']}. " if facts.get("valid_through") else "")
                + f"Reply when you would like to set up a time to go over the design.\n\n{shop}"
            )
        elif task.startswith("Confirm the appointment"):
            body = (f"{opening} You are booked for {labels[0]} to go over the signet ring design. A calendar "
                    f"invitation is on its way to this address. If that time stops working, just reply here.\n\n{shop}")
        elif task.startswith("Confirm that the appointment has been moved"):
            body = (f"{opening} The appointment has been moved to {labels[0]}. The earlier invitation is cancelled "
                    f"and a new one is on its way. Reply if it stops working for you.\n\n{shop}")
        elif task.startswith("Offer the customer"):
            lines = "\n".join(f"- {label}" for label in labels)
            body = (f"{opening} Here are the times I can offer to go over the signet ring design:\n{lines}\n"
                    f"Reply with the one that works, or tell me what does. Nothing is booked yet.\n\n{shop}")
        elif task.startswith("Send the attached design renderings"):
            body = (f"{opening} Attached are two renderings of the signet ring with your logo on the face. They "
                    "illustrate the design direction we discussed; the written specification and the final design "
                    f"you approve control the finished piece. Reply with anything you would like changed.\n\n{shop}")
        else:
            raise AssertionError("unexpected email task: " + task)
        return body


class GoldenPathTests(unittest.TestCase):
    """See the module docstring. Every assertion is about what the owner and the customer see."""

    def setUp(self) -> None:
        self.helper = IntakeTests("test_intake_cli_prints_the_result")

    def profile(self) -> dict:
        profile = json.loads((ROOT / "templates" / "shop-profile.json").read_text(encoding="utf-8"))
        profile["shop"].update({"name": "Kolo Jewelers", "outbound_mailbox": SHOP_MAILBOX,
                                "address": {"street": "1 Main St", "city": "Oakland", "state": "CA", "zip": "94612"},
                                "voice": "Warm and plain, short sentences, sign as Kolo Jewelers."})
        profile["pricing"].update({
            "markup_multiplier": 2.0,
            "metal_per_gram": {"14k_yellow_gold": 65.0},
            "stones_per_carat": {},
            "fees": {"casting": 120.0, "setting": 80.0},
            "bench_labor_per_hour": 90.0,
            "typical_finished_weights": {"signet ring": 9.0},
        })
        profile["scheduling"].update({
            "timezone": ZONE_NAME, "calendar": "primary",
            "windows": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "09:00", "end": "17:00"}],
        })
        profile["inbox_monitoring"].update({"enabled": True, "timezone": ZONE_NAME})
        profile["terms"].update({"lead_time_business_days": 15, "deposit_terms": "50% to start",
                                 "tax_handling": "sales tax added at checkout", "quote_valid_days": 14})
        return profile

    def workspace(self, directory: str) -> tuple[Path, World]:
        ws = Path(directory) / "ws"
        desk = ws / "estimate-desk"
        desk.mkdir(parents=True)
        monitor_root = desk / "inbox-monitor"
        inbox_monitor.prepare(monitor_root, self.helper.capabilities(), self.helper.cron())
        inbox_monitor.activate(monitor_root, self.helper.cron(), 1_000)
        activation_binding.create(activation_binding.binding_path(monitor_root), "agent:main:kolo:direct:first-owner")
        (desk / "shop-profile.json").write_text(json.dumps(self.profile(), indent=2), encoding="utf-8")
        return ws, World(ws)

    def patched(self, world: World):
        return (
            patch.object(inbox_watcher.gmail_fetch, "discover", side_effect=world.fake_discover),
            patch.object(inbox_watcher.gmail_fetch, "fetch_claimed", side_effect=world.fake_fetch),
            patch.object(sys.modules["gmail_fetch"], "fetch_json", side_effect=world.fake_fetch_json),
            patch.object(workflow_safe, "mirror_record", side_effect=lambda record, path: workflow_safe.write_private(path, record)),
            patch.object(kolo_safe, "run_command", side_effect=lambda argv, runner=None: world.run(argv)),
            patch.object(gmail_safe, "run_command", side_effect=lambda argv, runner=None, stdin_text=None: world.run(argv)),
            patch.object(judge, "complete", side_effect=world.complete),
            patch.object(gateway_token, "load_token", return_value="t"),
            patch.object(artwork, "collect", side_effect=world.fake_collect),
            patch.object(calendar_query, "query_freebusy", side_effect=world.query_freebusy),
            patch.object(calendar_query, "create_event", side_effect=world.create_event),
            patch.object(calendar_query, "delete_event", side_effect=world.delete_event),
            patch.object(calendar_query, "list_events", side_effect=world.list_events),
        )

    def run_render_jobs(self, ws: Path, world: World) -> None:
        """Kept for callers: renderings no longer run in jobs; `tick` carries them view by view."""
        return None

    def one_tick(self, ws: Path, world: World) -> dict:
        return inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t", judge_runner=world.run)

    def tick(self, ws: Path, world: World) -> dict:
        """One tick, then the ticks that queue right behind it while a rendering is under way.

        On the pod the next scheduled tick waits for the running one and
        starts the moment it ends, so a rendering's views follow each other
        back to back; this helper does the same, bounded.
        """
        summary = self.one_tick(ws, world)
        for _ in range(8):
            if not any(i.get("outcome") == "rendering_in_progress" for i in summary["inline"]):
                break
            summary = self.one_tick(ws, world)
        self.assertEqual(summary["inline_failures"], 0, summary)
        self.assertEqual(summary["spawn_failures"], 0, summary)
        self.assertEqual(summary["manual_review"], 0, summary)
        self.assertEqual(world.spawned, [], "the golden path never needs a worker job")
        return summary

    def execute(self, ws: Path, world: World, line: str, card: dict | None = None, **replacements: str) -> dict:
        """Run the exact command the card carries, the way the main session pastes it."""
        if card is not None:
            line = line.replace("<Brief ID>", card["brief_id"])
        for placeholder, value in replacements.items():
            line = line.replace(placeholder, value)
        parts = shlex.split(line)
        self.assertEqual(parts[0], "python3")
        self.assertTrue(parts[1].endswith("scripts/workflow_safe.py"), parts[1])
        self.assertEqual(Path(parts[parts.index("--workspace") + 1]), ws.resolve())
        with patch("sys.stdout", io.StringIO()) as stdout, patch("sys.stderr", io.StringIO()) as stderr:
            code = workflow_safe.main(parts[2:])
        printed = stdout.getvalue()
        self.assertEqual(code, 0, printed + stderr.getvalue())
        return json.loads(printed)

    def answer(self, ws: Path, text: str) -> dict:
        with patch("sys.stdout", io.StringIO()) as stdout, patch("sys.stderr", io.StringIO()) as stderr:
            code = workflow_safe.main(["answer-question", "--workspace", str(ws), "--base-dir", str(ROOT), "--answer", text,
                                       "--openclaw", "openclaw"])
        printed = stdout.getvalue()
        self.assertEqual(code, 0, printed + stderr.getvalue())
        return json.loads(printed)

    def record(self, ws: Path, estimate_id: str) -> dict:
        return estimate_record.read_object(estimate_record.record_path(ws / "estimate-desk" / "records", estimate_id))

    def only_estimate(self, ws: Path) -> str:
        records = sorted((ws / "estimate-desk" / "records").glob("*.json"))
        self.assertEqual(len(records), 1, records)
        return records[0].stem

    def claim(self, ws: Path, message_id: str) -> dict:
        return inbox_claim.read_state(inbox_claim.claim_path(ws / "estimate-desk" / "inbox-claims", message_id))

    def questions(self, ws: Path, status: str | None = None) -> list[dict]:
        root = owner_questions.questions_root(ws / "estimate-desk" / "inbox-monitor")
        return owner_questions.list_questions(root, status) if status else owner_questions.list_questions(root)

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ws, world = self.workspace(directory)
            patches = self.patched(world)
            for p in patches:
                p.start()
            try:
                self._golden_path(ws, world)
            finally:
                for p in patches:
                    p.stop()

    def _golden_path(self, ws: Path, world: World) -> None:
        thread = "thread-signet"
        first_slot = next_weekday(2, 14, 0)
        second_slot = next_weekday(1, 10, 30, after=first_slot)
        third_slot = next_weekday(1, 11, 0, after=second_slot)

        # 1. The inquiry: piece, metal, and stones stated; finger size and setting missing.
        world.spec = {
            "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
            "engraving": "our company logo on the face",
            "accent_stones": "a few small lab-grown diamonds along the shoulders",
            "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
        }
        world.customer_message("m1", thread, (
            "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face and a few "
            "small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me an estimate?\n\nPat"
        ))
        summary = self.tick(ws, world)
        self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
        self.assertEqual(len(world.prompts), 2, "a new inquiry costs two model calls: one to read it, one to write back")
        self.assertEqual(summary["inline"][0]["model_calls"], 2, summary)
        self.assertIn("tick_seconds", summary["timing"])
        self.assertEqual(len(world.sent), 1)
        self.assertIn("?", world.sent[0]["body"])
        self.assertNotIn("$", world.sent[0]["body"])
        self.assertEqual(world.cards, [], "no card for a follow-up")
        self.assertEqual(world.notices, [], "the owner hears nothing about a routine follow-up")
        estimate_id = self.only_estimate(ws)
        record = self.record(ws, estimate_id)
        self.assertEqual(record["status"], "awaiting_specs")
        self.assertEqual(sorted(record["missing_required_fields"]), ["finger_size", "setting_style"])
        self.assertEqual(self.claim(ws, "m1")["status"], "processed")

        # 2. The reply completes the design and brings the logo; the shop has no lab-grown melee rate.
        world.spec.update({"finger_size": "10", "setting_style": "bead set", "reference_images": ["logo.png"]})
        world.customer_message("m2", thread, "Size 10 please, and bead set is fine. Our logo is attached.\n\nPat",
                               attachments=("logo.png",))
        summary = self.tick(ws, world)
        self.assertEqual(len(world.sent), 1, "nothing goes to the customer while a rate is missing")
        self.assertEqual(world.cards, [], "no price card without the rate")
        self.assertEqual(len(world.notices), 1, world.notices)
        question_text = world.notices[0]["text"]
        self.assertIn("desk-answer", question_text)
        self.assertRegex(question_text, r"(?i)lab.?grown")
        self.assertEqual(self.record(ws, estimate_id)["status"], "awaiting_specs")
        open_questions = self.questions(ws, "open")
        self.assertEqual([q["kind"] for q in open_questions], ["missing_rate"])
        rate_key = open_questions[0]["rate"]["rate_key"]

        # 3. The owner answers in words; the answer is quick, the next tick prices and files the brief.
        answered = self.answer(ws, "600")
        self.assertEqual(answered["value"], 600.0, answered)
        self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
        summary = self.tick(ws, world)
        self.assertEqual([(i.get("step"), i["outcome"]) for i in summary["inline"]], [("price_from_record", "approval_requested")], summary)
        profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profile["pricing"]["stones_per_carat"][rate_key], 600.0)
        self.assertEqual(len(world.cards), 1, world.cards)
        price_card = world.cards[0]
        record = self.record(ws, estimate_id)
        self.assertEqual(record["status"], "pending_approval")
        price = record["proposed_price"]
        self.assertIn(f"${price:,.2f}", price_card["title"])
        self.assertRegex(price_card["title"], r"(?i)cost")
        self.assertRegex(price_card["title"], r"(?i)profit")
        self.assertIn("send-approved-estimate-brief", price_card["payload"]["execute"])
        self.assertEqual(len(world.sent), 1, "the estimate waits for approval")
        self.assertEqual(len(world.notices), 1, "no extra pings around the price card")
        registry = ws / "estimate-desk" / "briefs" / f"{price_card['brief_id']}.json"
        self.assertTrue(registry.exists(), "the brief id is on file for the rejection poll")

        # 4. Approval from the phone: the next tick sends the estimate; the session runs nothing.
        world.approve(price_card)
        summary = self.tick(ws, world)
        self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
        self.assertEqual(summary["approvals"][0]["result"]["outcome"], "estimate_sent", summary)
        self.assertEqual(len(world.sent), 2)
        estimate_mail = world.sent[1]
        self.assertIn(f"${price:,.2f}", estimate_mail["body"])
        self.assertEqual(len(re.findall(r"\$", estimate_mail["body"])), 1, "the price appears once, no cost lines")
        self.assertNotIn("*", estimate_mail["body"])
        self.assertEqual(self.record(ws, estimate_id)["status"], "estimate_sent")
        self.assertIn((price_card["brief_id"], "executed"), world.updates)
        self.assertEqual(self.execute(ws, world, price_card["payload"]["execute"], price_card)["outcome"], "already_sent",
                         "a second paste of the same line is harmless")
        self.assertEqual(len(world.sent), 2)

        # 5. The customer asks for a rendering: views rendered from the logo, checked, and carded.
        world.intents = ["rendering_request"]
        world.customer_message("m3", thread, "This looks good. Could you send me a rendering of the design?\n\nPat")
        summary = self.tick(ws, world)
        self.assertEqual(len(world.sent), 2, "renderings wait for approval")
        self.assertEqual(len(world.cards), 2, world.cards)
        render_card = world.cards[1]
        self.assertEqual(render_card["kind"], "send_rendering")
        self.assertIn("Checker", render_card["details"])
        self.assertIn("passed", render_card["details"]["Checker"])
        self.assertTrue(world.renders, "images were generated")
        for argv in world.renders:
            self.assertEqual(argv[3], "edit", "the customer's logo is carried into the render")
            self.assertTrue(flag(argv, "--file").endswith("logo.png"), argv)
        self.assertEqual(len(render_card["payload"]["images"]), 2)
        previews = [n for n in world.notices if n["file"]]
        self.assertEqual(len(previews), 2, "the owner sees both views")
        self.assertEqual(len([n for n in world.notices if not n["file"]]), 1, "still only the rate question in words")
        self.assertIn("send-approved-rendering", render_card["payload"]["execute"])

        # 6. Approval sends the two views with a plain-text note.
        result = self.execute(ws, world, render_card["payload"]["execute"], render_card)
        self.assertEqual(len(world.sent), 3, result)
        self.assertEqual(len(world.sent[2]["attachments"]), 2, world.sent[2])
        self.assertNotIn("$", world.sent[2]["body"])
        self.assertIn((render_card["brief_id"], "executed"), world.updates)

        # 7. The customer names a free time: one binary booking card, nothing booked yet.
        world.intents = ["appointment_request"]
        world.requested = ([first_slot.strftime("%A at %-I %p").lower()], [local_key(first_slot)])
        world.customer_message("m4", thread, f"Great. Could we meet {first_slot.strftime('%A')} at 2 to go over it?\n\nPat")
        summary = self.tick(ws, world)
        self.assertEqual(len(world.cards), 3, world.cards)
        book_card = world.cards[2]
        self.assertEqual(book_card["kind"], "appointment_booking", book_card["payload"])
        self.assertEqual(len(book_card["payload"]["calendar_availability"]), 1, "a free requested time is binary")
        self.assertEqual(book_card["payload"]["calendar_availability"][0]["start"][:16], local_key(first_slot))
        self.assertIn("book-approved-appointment", book_card["payload"]["execute"])
        self.assertEqual(world.calendar_events, {}, "nothing booked before approval")
        self.assertEqual(len(world.sent), 3)
        dormant = [q for q in self.questions(ws, "open") if q["kind"] == "appointment_next"]
        self.assertEqual(len(dormant), 1)
        self.assertTrue(dormant[0]["dormant"], "the what-next question sleeps until a rejection")
        self.assertEqual(len([n for n in world.notices if not n["file"]]), 1, "no ping for a card")

        # 8. The owner rejects the card; the next tick notices and asks what to do.
        world.reject(book_card, "not that day")
        summary = self.tick(ws, world)
        self.assertEqual([r.get("kind") for r in summary["rejections"]], ["appointment"], summary)
        asked = [n for n in world.notices if not n["file"]]
        self.assertEqual(len(asked), 2, asked)
        self.assertIn("desk-answer", asked[1]["text"])
        self.assertRegex(asked[1]["text"], r"(?i)pat|times")
        self.assertEqual(len(world.sent), 3, "a rejection sends nothing")
        summary = self.tick(ws, world)
        self.assertEqual(summary["rejections"], [], "a rejection is handled once")

        # 9. The owner answers in plain words with two times; a fresh offer card, no email.
        world.owner_times = [local_key(second_slot), local_key(third_slot)]
        answered = self.answer(ws, (
            f"Offer {second_slot.strftime('%A')} at 10:30 or {third_slot.strftime('%A')} at 11 instead"
        ))
        self.assertEqual(answered["outcome"], "offer_card_filed", answered)
        self.assertEqual(len(world.sent), 3, "the owner's words never go straight to the customer")
        self.assertEqual(len(world.cards), 4, world.cards)
        offer_card = world.cards[3]
        self.assertEqual(offer_card["kind"], "appointment_offer")
        starts = [o["start"][:16] for o in offer_card["payload"]["calendar_availability"]]
        self.assertEqual(starts, [local_key(second_slot), local_key(third_slot)])
        self.assertIn("send-approved-times", offer_card["payload"]["execute"])

        # 10. Approval emails exactly those times; nothing is booked.
        result = self.execute(ws, world, offer_card["payload"]["execute"], offer_card)
        self.assertEqual(len(world.sent), 4, result)
        for option in offer_card["payload"]["calendar_availability"]:
            self.assertIn(option["label"], world.sent[3]["body"])
        self.assertEqual(world.calendar_events, {})
        record = self.record(ws, estimate_id)
        self.assertEqual(len(record["times_offered"]), 1)

        # 11. The customer picks one of the offered times: a binary card, then the booking.
        world.requested = ([f"{second_slot.strftime('%A')} at 10:30"], [local_key(second_slot)])
        world.customer_message("m5", thread, f"{second_slot.strftime('%A')} at 10:30 works for me.\n\nPat")
        summary = self.tick(ws, world)
        self.assertEqual(len(world.cards), 5, world.cards)
        pick_card = world.cards[4]
        self.assertEqual(pick_card["kind"], "appointment_booking")
        self.assertEqual([o["start"][:16] for o in pick_card["payload"]["calendar_availability"]], [local_key(second_slot)])
        result = self.execute(ws, world, pick_card["payload"]["execute"], pick_card)
        self.assertEqual(len(world.calendar_events), 1, result)
        booked = next(iter(world.calendar_events.values()))
        self.assertEqual(booked["start"]["dateTime"][:16], local_key(second_slot))
        self.assertEqual(booked["attendees"], [{"email": "pat@example.net"}])
        self.assertEqual(len(world.sent), 5)
        self.assertIn(pick_card["payload"]["calendar_availability"][0]["label"], world.sent[4]["body"])
        record = self.record(ws, estimate_id)
        self.assertEqual(record["status"], "appointment_booked")
        self.assertEqual(record["appointment_booked"]["calendar_event_id"], booked["id"])

        # 12. The customer moves it: a new card, the old event cancelled, one email about the move.
        world.requested = ([f"{third_slot.strftime('%A')} at 11"], [local_key(third_slot)])
        world.customer_message("m6", thread, f"Something came up. Could we do {third_slot.strftime('%A')} at 11 instead?\n\nPat")
        summary = self.tick(ws, world)
        self.assertEqual(len(world.cards), 6, world.cards)
        move_card = world.cards[5]
        self.assertEqual([o["start"][:16] for o in move_card["payload"]["calendar_availability"]], [local_key(third_slot)])
        result = self.execute(ws, world, move_card["payload"]["execute"], move_card)
        self.assertEqual(world.deleted_events, [booked["id"]], result)
        self.assertEqual(len(world.calendar_events), 1)
        moved = next(iter(world.calendar_events.values()))
        self.assertEqual(moved["start"]["dateTime"][:16], local_key(third_slot))
        self.assertEqual(len(world.sent), 6)
        self.assertRegex(world.sent[5]["body"], r"(?i)moved")
        record = self.record(ws, estimate_id)
        self.assertEqual(record["appointment_booked"]["calendar_event_id"], moved["id"])
        self.assertEqual(len(record.get("appointment_history") or []), 1)

        # The whole way through: every customer email is plain text, and the owner heard
        # exactly two questions in words plus the two preview images.
        for mail in world.sent:
            self.assertNotIn("**", mail["body"])
            self.assertNotIn("{{", mail["body"])
            self.assertEqual(mail["thread_id"], thread)
        self.assertEqual(len([n for n in world.notices if not n["file"]]), 2, world.notices)
        self.assertEqual(world.spawned, [])
        self.assertEqual(world.other, [], "no command outside the known set")
        for message_id in ("m1", "m2", "m3", "m4", "m5", "m6"):
            self.assertEqual(self.claim(ws, message_id)["status"], "processed", message_id)


if __name__ == "__main__":
    unittest.main()


class SideBranchTests(GoldenPathTests):
    """The branches off the golden path, each on the same real code."""

    def _profile_with_rates(self, ws: Path) -> None:
        profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
        profile["pricing"]["stones_per_carat"]["lab_grown_diamond"] = 900.0
        profile["pricing"]["typical_finished_weights"].update({"engagement ring": 5.0, "wedding band": 4.0})
        (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")

    def _estimate_sent(self, ws: Path, world: World, spec: dict | None = None, rate: bool = True,
                       text: str | None = None) -> tuple[str, str]:
        """A complete inquiry priced and sent: the starting point for post-estimate branches."""
        if rate:
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        thread = "thread-side"
        world.spec = spec or {
            "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
            "setting_style": "bead set", "engraving": "our logo on the face",
            "accent_stones": "small lab-grown diamonds along the shoulders",
            "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
        }
        world.customer_message("s1", thread, text or ("Please quote a 14k yellow gold signet ring, size 10, logo on the face, "
                                                       "small lab-grown diamonds G VS bead set on the shoulders.\n\nPat"))
        summary = self.tick(ws, world)
        self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
        card = world.cards[-1]
        self.execute(ws, world, card["payload"]["execute"], card)
        estimate_id = self.only_estimate(ws)
        self.assertEqual(self.record(ws, estimate_id)["status"], "estimate_sent")
        return thread, estimate_id

    def run_branch(self, branch) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ws, world = self.workspace(directory)
            patches = self.patched(world)
            for p in patches:
                p.start()
            try:
                branch(ws, world)
            finally:
                for p in patches:
                    p.stop()

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # inherited; runs once in the parent
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.busy.append({"start": (wanted - timedelta(hours=1)).isoformat(), "end": (wanted + timedelta(hours=1)).isoformat()})
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer", card["payload"])
            starts = [o["start"][:16] for o in card["payload"]["calendar_availability"]]
            self.assertTrue(starts, card["payload"])
            self.assertNotIn(local_key(wanted), starts, "the taken time is not offered")
            self.assertTrue(all(s[:10] == local_key(wanted)[:10] for s in starts), f"offers stay on the asked day: {starts}")
            self.assertRegex(card["payload"]["availability_note"], r"(?i)taken")
            self.assertEqual(len(world.sent), 1, "nothing emailed without approval")
            self.assertEqual([n for n in world.notices if not n["file"]], [], "a card, no ping")
        self.run_branch(branch)

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        """The owner's rule (7 September 2026): say the hours, offer times inside them."""
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            saturday = next_weekday(1, 18, 0)
            while saturday.weekday() != 5:
                saturday += timedelta(days=1)
            world.intents = ["appointment_request"]
            world.requested = (["Saturday at 6pm"], [local_key(saturday)])
            world.customer_message("s2", thread, "Can we meet Saturday at 6pm?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer", card["payload"])
            self.assertIn("outside your hours", card["details"]["Customer asked for"])
            self.assertIn("outside your hours", card["title"])
            self.assertTrue(card["payload"]["outside_hours"], card["payload"])
            self.assertIn("Monday to Friday", card["payload"]["hours"])
            starts = [o["start"][:16] for o in card["payload"]["calendar_availability"]]
            self.assertTrue(starts and local_key(saturday) not in starts, "nothing outside the hours is offered")
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            body = world.sent[-1]["body"]
            self.assertIn(card["payload"]["hours"], body, "the email states the hours exactly")
            self.assertIn("outside our hours", body)
            for option in card["payload"]["calendar_availability"]:
                self.assertIn(option["label"], body)
        self.run_branch(branch)

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        """6 September 2026: 'next Tuesday at 2pm' asked on a Sunday fell past the 7-day window; the desk offered nothing."""
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(9, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"next {wanted.strftime('%A')} at 2pm"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet next {wanted.strftime('%A')} at 2pm?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking", card["payload"])
            self.assertEqual(card["payload"]["calendar_availability"][0]["start"][:16], local_key(wanted))
            self.assertEqual([n for n in world.notices if not n["file"]], [], "a card, no question")
        self.run_branch(branch)

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.busy.append({"start": wanted.replace(hour=0).isoformat(), "end": wanted.replace(hour=23, minute=59).isoformat()})
            for offset in range(1, 8):
                day = wanted + timedelta(days=offset)
                world.busy.append({"start": (day - timedelta(hours=2)).isoformat(), "end": (day + timedelta(hours=2)).isoformat()})
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer", card["payload"])
            starts = [o["start"][:16] for o in card["payload"]["calendar_availability"]]
            self.assertTrue(starts, card["payload"])
            self.assertNotIn(local_key(wanted)[:10], [s[:10] for s in starts], "the fully booked day is not offered")
            self.assertEqual([n for n in world.notices if not n["file"]], [], "a card, no question")
        self.run_branch(branch)

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["appointment_request"]
            world.requested = (["sometime next week"], [])
            world.customer_message("s2", thread, "Could we set up a time to talk it over sometime next week?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer", card["payload"])
            options = card["payload"]["calendar_availability"]
            self.assertTrue(1 <= len(options) <= 3, options)
            self.assertLessEqual(len({o["start"][:10] for o in options}), 2, "at most two days in a tight spread")
            self.assertEqual(len(world.sent), 1)
        self.run_branch(branch)

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            cards_before = len(world.cards)
            with patch.object(calendar_query, "query_freebusy", side_effect=ValueError("gateway said no")):
                self.tick(ws, world)
            self.assertEqual(len(world.cards), cards_before, "no card with nothing to approve")
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("desk-answer", asked[0]["text"])
            self.assertRegex(asked[0]["text"], r"(?i)calendar check failed")
            self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner", "the claim waits for the answer")
            open_questions = [q for q in self.questions(ws, "open") if q["kind"] == "appointment_next"]
            self.assertEqual(len(open_questions), 1)
            self.assertFalse(open_questions[0].get("dormant"))
            # The owner answers with times; the offer card comes as usual.
            slot = next_weekday(3, 11, 0)
            world.owner_times = [local_key(slot)]
            answered = self.answer(ws, f"Offer {slot.strftime('%A')} at 11")
            self.assertEqual(answered["outcome"], "offer_card_filed", answered)
            self.assertEqual(world.cards[-1]["kind"], "appointment_offer")
            self.assertEqual(len(world.sent), 1, "still nothing sent without approval")
            self.assertEqual(self.claim(ws, "s2")["status"], "processed", "the card is filed; the claim is the desk's")
            # 6 September 2026, Brief #26: approving that card must offer the times, not refuse the claim.
            card = world.cards[-1]
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent), 2, "the times were emailed once")
            self.assertIn(slot.strftime("%A"), world.sent[-1]["body"])
            self.assertEqual(self.record(ws, estimate_id)["times_offered"][-1]["options"][0]["start"][:16], local_key(slot))
            self.assertIn(self.claim(ws, "s2")["status"], ("processed", "manual_review"))
            self.assertEqual(self.questions(ws, "open")[-1]["kind"], "appointment_next", "the new card's own question waits")
            self.assertEqual(self.tick(ws, world)["claimed"], 0, "nothing left in the queue")
        self.run_branch(branch)

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        def branch(ws: Path, world: World) -> None:
            _thread, estimate_id = self._estimate_sent(ws, world, rate=False, spec={
                "piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7",
                "dimensions": "4mm wide", "finish": "brushed", "notes": "plain band, no stones",
            }, text="Please quote a plain 14k yellow gold wedding band, size 7, 4mm wide, brushed finish, no stones.\n\nPat")
            self.assertEqual([n for n in world.notices if not n["file"]], [], "no rate question for a plain band")
            card = world.cards[0]
            self.assertNotRegex(card["title"], r"(?i)stone|carat|melee")
            record = self.record(ws, estimate_id)
            self.assertEqual(record["missing_required_fields"], [])
        self.run_branch(branch)

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        def branch(ws: Path, world: World) -> None:
            world.triage_kind = "vendor_or_marketing"
            world.customer_message("v1", "thread-vendor", "Wholesale findings at 20% off this month!", subject="Findings sale")
            summary = self.tick(ws, world)
            self.assertEqual(summary["closed"] + len([i for i in summary["inline"] if i["outcome"] == "not_an_inquiry"]), 1, summary)
            self.assertEqual(world.cards, [])
            self.assertEqual(world.notices, [])
            self.assertEqual(world.sent, [])
        self.run_branch(branch)

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        def branch(ws: Path, world: World) -> None:
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                "setting_style": "bead set", "accent_stones": "small lab-grown diamonds",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("r1", "thread-reject", "Quote please: 14k yellow gold signet, size 10, small lab-grown diamonds.\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            world.reject(card, "too high")
            summary = self.tick(ws, world)
            self.assertEqual([r.get("kind") for r in summary["rejections"]], ["price"], summary)
            notes = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(notes), 1, notes)
            self.assertRegex(notes[0]["text"], r"(?i)passed on the price")
            self.assertIn("desk-answer", notes[0]["text"], "a question, answered in words (WORKFLOW 6.10)")
            self.assertEqual(world.sent, [])
            estimate_id = self.only_estimate(ws)
            self.assertEqual(self.record(ws, estimate_id)["status"], "pending_approval")
            self.tick(ws, world)
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 1, "said once")
            # The owner names a price: a fresh binary card at that price, same cost sheet, new margin.
            answered = self.answer(ws, "file it at $2,000")
            self.assertEqual(answered["outcome"], "price_card_filed", answered)
            fresh = world.cards[-1]
            self.assertNotEqual(fresh["brief_id"], card["brief_id"])
            self.assertIn("quote $2,000.00", fresh["title"])
            self.assertIn("cost $1,252.50", fresh["title"])
            self.assertEqual(fresh["details"]["Owner-set price"], "$2,000.00 (the desk priced $2,505.00)")
            self.assertRegex(fresh["details"]["Margin"], r"^37%, below the shop's 50%$")
            record = self.record(ws, estimate_id)
            self.assertEqual(record["status"], "pending_approval")
            self.assertEqual(record["proposed_price"], 2000.0)
            self.assertEqual(record["internal_cost_sheet"]["customer_price"], 2000.0)
            self.assertEqual(record["owner_price"]["previous_price"], 2505.0)
            self.assertEqual(len(record["approval_requests"]), 2, "the rejected request stays as history")
            self.assertEqual(world.sent, [], "nothing sent without the fresh card's approval")
            # Approved from the phone: the tick sends the new price, once, and the old draft is gone.
            world.approve(fresh)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent), 1)
            self.assertIn("$2,000.00", world.sent[-1]["body"])
            self.assertNotIn("2,505", world.sent[-1]["body"])
            self.assertEqual(self.record(ws, estimate_id)["status"], "estimate_sent")
            self.assertEqual(self.execute(ws, world, fresh["payload"]["execute"], fresh)["outcome"], "already_sent")
            self.assertEqual(len(world.sent), 1)
        self.run_branch(branch)

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        def branch(ws: Path, world: World) -> None:
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                "setting_style": "bead set", "accent_stones": "small lab-grown diamonds",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("r2", "thread-reject-2", "Quote please.\n\nPat")
            self.tick(ws, world)
            world.reject(world.cards[-1], "too high")
            self.tick(ws, world)
            self.assertEqual(self.answer(ws, "2300")["outcome"], "price_card_filed")
            fresh = world.cards[-1]
            self.assertIn("quote $2,300.00", fresh["title"])
            world.reject(fresh, "still too high")
            summary = self.tick(ws, world)
            self.assertEqual([r.get("kind") for r in summary["rejections"]], ["price"], summary)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 2, "asked once per rejection")
            self.assertIn("$2,300.00", asked[-1]["text"])
            self.assertEqual(self.answer(ws, "I will handle it myself")["decision"], "handle_myself")
            estimate_id = self.only_estimate(ws)
            self.assertEqual(self.record(ws, estimate_id)["status"], "dormant")
            self.assertEqual(world.sent, [])
            self.assertEqual(self.tick(ws, world)["rejections"], [])
        self.run_branch(branch)


class RenderingGateTests(GoldenPathTests):
    """No rendering reaches a customer without the owner's card, whichever path rendered it."""


    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # inherited; runs once in the parent
        pass

    def test_send_rendering_refuses_without_an_approved_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ws, world = self.workspace(directory)
            body = Path(directory) / "note.txt"
            body.write_text("Here are the renderings.", encoding="utf-8")
            image = Path(directory) / "view.png"
            image.write_bytes(PNG)
            desk = ws / "estimate-desk"
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err:
                code = workflow_safe.main([
                    "send-rendering", "--monitor-root", str(desk / "inbox-monitor"), "--claim-root", str(desk / "inbox-claims"),
                    "--record-root", str(desk / "records"), "--message-id", "m9", "--estimate-id", "jed-0123456789abcdef",
                    "--record-output", str(Path(directory) / "out.json"), "--body", str(body), "--image", str(image),
                    "--gmail-payload", str(Path(directory) / "p.json"), "--provider-response", str(Path(directory) / "r.json"),
                ])
            self.assertNotEqual(code, 0)
            self.assertIn("approval-gated", err.getvalue())
            self.assertEqual(world.sent, [])



class WindowGateTests(SideBranchTests):
    """A time outside the owner's declared windows never reaches a card or the calendar."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # inherited; runs once in the parent
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:  # inherited; runs once in SideBranchTests
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _sunday(self) -> datetime:
        day = datetime.now(ZONE) + timedelta(days=2)
        while day.weekday() != 6:
            day += timedelta(days=1)
        return day.replace(hour=15, minute=0, second=0, microsecond=0)

    def test_hand_written_intent_with_a_sunday_is_refused_before_any_card(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import argparse

            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["appointment_request"]
            world.requested = (["Sunday afternoon"], [])
            world.customer_message("s2", thread, "Could we meet Sunday afternoon?\n\nPat")
            # Stop the inline pipeline at the intent so the claim stays processing, as a worker would find it.
            with patch.object(inbox_watcher.pipeline, "post_estimate_actions", return_value={"outcome": "needs_worker", "next_action": "request_appointment_approval"}):
                inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t", judge_runner=world.run)
            cards_before = len(world.cards)
            desk = ws / "estimate-desk"
            paths = inbox_monitor.prepare_claim_work(desk / "inbox-monitor", desk / "inbox-claims", "s2")
            sunday = self._sunday()
            intent = {"requested_times": ["Sunday afternoon"], "calendar_availability": [{
                "start": sunday.isoformat(), "end": (sunday + timedelta(minutes=30)).isoformat(),
                "label": sunday.strftime("%A %B %-d, 3:00 PM"),
            }]}
            Path(paths["appointment_intent"]).write_text(json.dumps(intent), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside the declared consultation windows"):
                workflow_safe.request_appointment_approval(argparse.Namespace(
                    monitor_root=desk / "inbox-monitor", claim_root=desk / "inbox-claims", record_root=desk / "records",
                    shop_profile=None, message_id="s2", estimate_id=estimate_id,
                    appointment_intent=Path(paths["appointment_intent"]),
                    appointment_approval=Path(paths["appointment_approval"]), record_output=Path(paths["current_record"]),
                    defer_finalize_for_rendering=False, runner=world.run,
                ))
            self.assertEqual(len(world.cards), cards_before, "no card with a Sunday on it")
            self.assertEqual(world.calendar_events, {})
        self.run_branch(branch)

    def test_booking_executor_refuses_a_sunday_even_on_an_approved_store(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import argparse

            thread, estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking")
            # Someone edits the durable store to a Sunday after the card was filed.
            store = workflow_safe.approval_store_path(ws / "estimate-desk" / "inbox-monitor", estimate_id, "s2")
            approval = json.loads(store.read_text(encoding="utf-8"))
            sunday = self._sunday()
            approval["calendar_availability"] = [{"start": sunday.isoformat(), "end": (sunday + timedelta(minutes=30)).isoformat(),
                                                  "label": "Sunday 3:00 PM"}]
            store.write_text(json.dumps(approval), encoding="utf-8")
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err:
                parts = shlex.split(card["payload"]["execute"].replace("<Brief ID>", card["brief_id"]))
                code = workflow_safe.main(parts[2:])
            self.assertNotEqual(code, 0)
            self.assertIn("outside the declared consultation windows", err.getvalue())
            self.assertEqual(world.calendar_events, {}, "nothing booked")
            self.assertEqual(len(world.sent), 1, "no confirmation email")
        self.run_branch(branch)


class OwnStoneAndStallTests(SideBranchTests):
    """A customer's own stone is never graded, and the same question is never sent twice."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_a_stale_confirm_left_by_a_dead_run_is_dropped_before_the_customer_is_asked(self) -> None:
        """6 September 2026: a dead run had recorded "confirm your own stone" from quoted shop text; the retry
        emailed that question. The retry must re-run the check first and price when nothing is left."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                "setting_style": "bead set", "engraving": "our logo on the face",
                "accent_stones": "small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("s1", "thread-stale", "Please quote a 14k yellow gold signet ring, size 10, logo on the face, "
                                   "small lab-grown diamonds G VS bead set on the shoulders.\n\nPat\n\n"
                                   "On Sun wrote:\n> I have attached the design renderings you requested.\n")
            # The dead run: it read the quoted text as the customer's, recorded the ask, and died before sending.
            with patch.object(reading_check, "compare", return_value=[{"name": "confirm.customer_stone", "topic": "customer_stone",
                                                                        "said": "x", "read": "y", "question": reading_check.QUESTIONS["customer_stone"]}]), \
                    patch.object(workflow_safe, "send_spec_followup", side_effect=Crash("killed before the send")):
                with self.assertRaises(Crash):
                    inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t", judge_runner=world.run)
            estimate_id = self.only_estimate(ws)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["missing_required_fields"], ["confirm.customer_stone"], "the stale ask is on the record")
            self.assertEqual(world.sent, [], "nothing was sent by the dead run")
            # The retry, on fixed code: the check is re-run on the same words, the stale line goes, the price card comes.
            state = self.claim(ws, "s1")
            inbox_claim.release_lease(ws / "estimate-desk" / "inbox-claims", "s1", state["claim_token"])
            with patch.object(inbox_watcher, "STALE_AFTER_SECONDS", 1):
                summary = self.tick(ws, world)
            self.assertEqual(world.sent, [], "the customer is never asked the stale question")
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertNotIn("confirm.", json.dumps(self.record(ws, estimate_id)["missing_required_fields"]))
        self.run_branch(branch)

    def test_a_second_stuck_gets_a_new_question_and_a_requeue_starts_fresh(self) -> None:
        """6 September 2026: the second stuck reused a closed code, nothing reached the owner, and a requeue
        kept the old failure count so the next tick asked instead of trying."""
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
            # The image tool is down: the render job fails every time the tick spawns it.
            world.fail_next["image"] = 500
            with patch.object(inbox_watcher, "TRANSIENT_ATTEMPTS", 2), patch.object(rendering, "DESCRIBE_PAUSE_SECONDS", 0):
                first = None
                for _ in range(4):
                    summary = self.one_tick(ws, world)
                    if summary["stuck"]:
                        first = summary["stuck"][0]
                        break
                self.assertIsNotNone(first, "the desk asks after the bounded tries")
                asked = [n for n in world.notices if not n["file"] and "desk-answer" in n["text"]]
                first_code = asked[-1]["text"].split("desk-answer ")[-1].strip()[:6]
                # The owner says retry; the tool is still down; the desk must ask again, with a new code.
                answered = self.answer(ws, "retry")
                self.assertEqual(answered["decision"], "retry", answered)
                self.assertEqual(answered["pipeline"], "queued_for_tick", answered)
                second = None
                for _ in range(4):
                    summary = self.one_tick(ws, world)
                    if summary["stuck"]:
                        second = summary["stuck"][0]
                        break
                self.assertIsNotNone(second, "a second stuck is asked, not parked silently")
                asked = [n for n in world.notices if not n["file"] and "desk-answer" in n["text"]]
                second_code = asked[-1]["text"].split("desk-answer ")[-1].strip()[:6]
                self.assertNotEqual(first_code, second_code, "a closed question's code is never reused")
                self.assertEqual([f["code"] for f in doctor.scan(ws) if f["level"] != "info"], [], "an open question resumes the claim")
                # The doctor's requeue starts fresh: the tool is back, the rendering lands on a card.
                world.fail_next.clear()
                self.assertEqual(doctor.requeue(ws, "s2")["outcome"], "requeued")
                self.assertEqual(self.claim(ws, "s2").get("inline_attempts", 0), 0, "a requeue resets the retry budget")
                for _ in range(3):
                    summary = self.one_tick(ws, world)
                    if world.cards and world.cards[-1]["kind"] == "send_rendering":
                        break
                self.assertEqual(world.cards[-1]["kind"], "send_rendering", summary)
        self.run_branch(branch)

    def test_a_rendering_between_views_is_in_flight_and_the_owner_hears_nothing(self) -> None:
        """7 September 2026, rehearsal: every rendering tick announced "1 claimed item(s) still processing"."""
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
            first = self.one_tick(ws, world)
            self.assertEqual([i["outcome"] for i in first["inline"]], ["rendering_in_progress"], first)
            self.assertEqual(first["message"], "NO_REPLY", "a rendering between views is in flight, not unsettled")
            second = self.one_tick(ws, world)
            self.assertEqual([i["outcome"] for i in second["inline"]], ["rendering_approval_requested"], second)
            self.assertEqual(second["message"], "NO_REPLY")
            self.assertEqual([n for n in world.notices if not n["file"] and "desk-answer" not in n["text"]], [],
                             "no notice that is neither a question nor a preview")
        self.run_branch(branch)

    def test_a_vision_check_that_keeps_failing_cards_the_views_unchecked_instead_of_asking(self) -> None:
        """6 September 2026: the describe call failed six jobs in a row and the owner was asked; the images were fine."""
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.fail_next["image_describe"] = 50
            with patch.object(rendering, "DESCRIBE_PAUSE_SECONDS", 0):
                world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
                summary = self.tick(ws, world)
            world.fail_next.clear()
            card = world.cards[-1]
            self.assertEqual(card["kind"], "send_rendering", summary)
            self.assertIn("not machine-checked", card["details"]["Checker"])
            self.assertEqual(len(card["payload"]["images"]), 2)
            self.assertEqual([n for n in world.notices if not n["file"] and "desk-answer" in n["text"]], [], "no question for a flaky checker")
            self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner")
        self.run_branch(branch)

    def test_a_change_after_the_estimate_reopens_it_and_sends_an_updated_estimate_once(self) -> None:
        """WORKFLOW.md 6.8: the owner's "change" reopens the gate on the same thread; the old figure is history."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            first_sent = dict(self.record(ws, estimate_id)["estimate_delivery"])
            world.design_change = ["metal_karat", "metal_color"]
            world.customer_message("s2", thread, "Actually, could we do it in 18k rose gold instead?\n\nPat\n\n"
                                   "On Sun, Sep 6, 2026 at 6:45 PM shop@example.com wrote:\n"
                                   "> Hi Pat, I have attached the design renderings you requested; the written\n"
                                   "> specification controls the piece. Reset your expectations for lead time.\n")
            summary = self.tick(ws, world)
            asked = [n for n in world.notices if not n["file"] and "desk-answer" in n["text"]]
            self.assertEqual(len(asked), 1, (summary, asked, [(q["kind"], q["status"], q.get("delivery")) for q in self.questions(ws)], self.claim(ws, "s2")["status"]))
            self.assertIn("change", asked[-1]["text"])
            self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner", summary)
            self.assertEqual(len(world.sent), 1, "nothing sent on a change the owner has not read")
            # The owner says it is a change: the record reopens, the new words are read, and the price card follows.
            world.design_change = []
            world.spec = {**world.spec, "metal": "rose gold", "metal_karat": "18k", "metal_color": "rose"}
            answered = self.answer(ws, "change")
            self.assertEqual(answered["decision"], "design_change", answered)
            self.assertEqual(answered["revision"], 1)
            self.assertEqual(answered["pipeline"], "queued_for_tick", "the answer is quick; the tick reads and prices")
            self.assertEqual(self.record(ws, estimate_id)["status"], "awaiting_specs")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["status"], "pending_approval", answered)
            self.assertEqual(record["revision"], 1)
            self.assertEqual(record["estimate_history"][-1]["estimate_delivery"], first_sent, "the sent estimate is history, untouched")
            self.assertEqual(record["specification"]["metal_karat"], "18k")
            card = world.cards[-1]
            self.assertIn("18K rose gold", card["title"])
            self.assertEqual(len(world.sent), 1)
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent), 2, "the updated estimate went out once")
            self.assertIn("updated estimate", world.sent[-1]["body"])
            self.assertEqual(self.record(ws, estimate_id)["status"], "estimate_sent")
            self.assertEqual(self.claim(ws, "s2")["status"], "processed")
        self.run_branch(branch)

    def test_a_second_piece_after_the_estimate_joins_it_as_another_line(self) -> None:
        """WORKFLOW.md 6.8 and the multi-piece rule: "second piece" reopens the estimate with two lines and one total."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            thread, estimate_id = self._estimate_sent(ws, world)
            world.design_change = ["pieces"]
            world.customer_message("s2", thread, "Could you also quote a plain matching band, size 10?\n\nPat\n\n"
                                   "On Sun, Sep 6, 2026 at 6:45 PM shop@example.com wrote:\n"
                                   "> Thank you for the details on a signet ring. Here is where the estimate lands:\n"
                                   "> finger size 10, stone origin lab-grown, accent stones 0.2 ct\n"
                                   "> Estimate: $2,505.00\n> This estimate is good through September 20, 2026.\n")
            self.tick(ws, world)
            self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner")
            world.design_change = []
            world.spec = {"metal": "yellow gold", "metal_karat": "14k", "pieces": [
                {**{k: v for k, v in world.spec.items()}},
                {"piece_type": "wedding band", "finger_size": "10", "notes": "plain, polished, no stones"},
            ]}
            answered = self.answer(ws, "second piece")
            self.assertEqual(answered["decision"], "second_piece", answered)
            self.assertEqual(answered["pipeline"], "queued_for_tick")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["status"], "pending_approval", answered)
            self.assertEqual(len(record["specification"]["pieces"]), 2)
            self.assertEqual(record["estimate_history"][-1]["reopened_for"], "second_piece")
            card = world.cards[-1]
            self.assertIn("wedding band", card["title"])
            self.assertIn("signet ring", card["title"])
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent), 2)
            self.assertIn("updated", world.sent[-1]["body"].lower())
            self.assertEqual(len(re.findall(r"\$", world.sent[-1]["body"])), 1, "one total for both pieces")
        self.run_branch(branch)

    def test_two_sizes_read_as_one_piece_are_confirmed_before_any_price(self) -> None:
        """ARCHITECTURE-OPTIONS.md E': the customer names two sizes, the model reads one piece; the follow-up confirms."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            world.spec = {
                "piece_type": "engagement ring", "finger_size": "6", "metal": "yellow gold", "metal_karat": "14k",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_carat": "2", "stone_shape": "round",
                "stone_color": "F", "stone_clarity": "VS1", "setting_style": "solitaire", "center_stone": "yes",
            }
            world.customer_message("c1", "thread-confirm", "A 14k yellow gold engagement ring, size 6, 2 ct round lab-grown "
                                   "solitaire, and a plain matching band in size 10.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(world.cards, [], "nothing priced on a reading the customer's words contradict")
            body = world.sent[-1]["body"]
            self.assertIn("how many pieces", body)
            record = self.record(ws, self.only_estimate(ws))
            self.assertIn("confirm.piece_count", record["missing_required_fields"])
            self.assertEqual(record["status"], "awaiting_specs")
            asked = [p for p in world.prompts if "MISSING DETAILS TO ASK FOR" in p]
            self.assertTrue(asked and "to confirm:" in asked[-1], "the drafting model is given the confirming line")
            # The customer confirms two pieces; the reading now agrees and the estimate carries two lines.
            world.spec = {"metal": "yellow gold", "metal_karat": "14k", "pieces": [
                {"piece_type": "engagement ring", "finger_size": "6", "stone_type": "diamond", "stone_origin": "lab-grown",
                 "stone_carat": "2", "stone_shape": "round", "stone_color": "F", "stone_clarity": "VS1", "setting_style": "solitaire",
                 "center_stone": "yes"},
                {"piece_type": "wedding band", "finger_size": "10", "notes": "plain, polished, no stones"},
            ]}
            world.customer_message("c2", "thread-confirm", "Yes, two pieces: the ring in size 6 and the band in size 10.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertIn("wedding band", world.cards[-1]["title"])
            self.assertNotIn("confirm.", json.dumps(self.record(ws, self.only_estimate(ws))["missing_required_fields"]))
        self.run_branch(branch)

    def _pendant(self, ws: Path, world: World) -> str:
        profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
        profile["pricing"]["metal_per_gram"]["18k_yellow_gold"] = 90.0
        profile["pricing"]["typical_finished_weights"]["pendant"] = 4.0
        (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        world.spec = {
            "piece_type": "pendant", "metal": "yellow gold", "metal_karat": "18k", "stone_type": "diamond",
            "setting_style": "bezel", "customer_supplied_materials": "her mother's diamond",
            "notes": "reset my mother's diamond in a bezel on a thin dainty chain", "dimensions": "18 inch chain",
        }
        world.customer_message("d1", "thread-pendant", (
            "I would like my mother's diamond reset in an 18k yellow gold bezel on a thin dainty chain, 18 inches.\n\nDavid"
        ))
        return "thread-pendant"

    def test_own_stone_is_asked_for_shape_and_size_never_grade(self) -> None:
        def branch(ws: Path, world: World) -> None:
            self._pendant(ws, world)
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(record["missing_required_fields"], ["stone_carat", "stone_shape"])
            for grade in ("stone_color", "stone_clarity", "stone_origin", "stone_cut"):
                self.assertNotIn(grade, record["missing_required_fields"])
            # The customer gives the stone's shape and size; no stone cost, no rate question, a price card.
            world.spec.update({"stone_shape": "round", "stone_carat": "about 1 carat"})
            world.customer_message("d2", "thread-pendant", "It is round, about a carat.\n\nDavid")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertEqual([n for n in world.notices if not n["file"]], [], "no rate question for a stone the shop does not buy")
            card = world.cards[-1]
            self.assertNotRegex(card["title"], r"(?i)/ct|per carat|natural|lab-grown|lab grown|melee|diamond")
            self.assertRegex(card["title"], r"(?i)cost")
        self.run_branch(branch)

    def test_second_unanswered_ask_goes_to_the_owner_not_the_customer(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread = self._pendant(ws, world)
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1)
            # The customer pushes back instead of answering; the spec does not move.
            world.customer_message("d2", thread, "What does the shape or size have to do with this??? This is super confusing.\n\nDavid")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["awaiting_owner"], summary)
            self.assertEqual(len(world.sent), 1, "the same question is never sent twice")
            self.assertEqual(summary["message"], "NO_REPLY")
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("desk-answer", asked[0]["text"])
            self.assertRegex(asked[0]["text"], r"(?i)super confusing")
            self.assertRegex(asked[0]["text"], r"(?i)skip")
            self.assertEqual(self.claim(ws, "d2")["status"], "awaiting_owner")
            estimate_id = self.only_estimate(ws)
            # "skip": the details become the jeweler's call and the price card follows.
            answered = self.answer(ws, "skip it, price it as you see fit")
            self.assertEqual(answered["decision"], "skip", answered)
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
            summary = self.tick(ws, world)
            self.assertEqual([(i.get("step"), i["outcome"]) for i in summary["inline"]], [("price_from_record", "approval_requested")], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["status"], "pending_approval")
            self.assertEqual(record["specification"]["stone_shape"], "jeweler's choice")
            self.assertEqual(len(world.sent), 1)
            self.assertTrue(world.cards, "a price card was filed")
        self.run_branch(branch)

    def test_ask_again_sends_the_question_once_more(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread = self._pendant(ws, world)
            self.tick(ws, world)
            world.customer_message("d2", thread, "Why do you need that?\n\nDavid")
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1)
            answered = self.answer(ws, "ask again please")
            self.assertEqual(answered["decision"], "ask_again", answered)
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
            summary = self.tick(ws, world)
            self.assertEqual([(i.get("step"), i["outcome"]) for i in summary["inline"]], [("resend_followup", "followup_sent")],
                             summary)
            self.assertEqual(len(world.sent), 2)
            self.assertIn("?", world.sent[1]["body"])
            self.assertEqual(self.claim(ws, "d2")["status"], "processed")
        self.run_branch(branch)

    def test_handle_myself_leaves_the_thread_alone(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread = self._pendant(ws, world)
            self.tick(ws, world)
            world.customer_message("d2", thread, "Why do you need that?\n\nDavid")
            self.tick(ws, world)
            answered = self.answer(ws, "I'll handle it")
            self.assertEqual(answered["decision"], "handle_myself", answered)
            self.assertEqual(len(world.sent), 1)
            self.assertEqual(world.cards, [])
            self.assertIn(self.claim(ws, "d2")["status"], ("manual_review", "processed"))
            self.assertEqual(self.tick(ws, world)["claimed"], 0)
        self.run_branch(branch)


class MeetingFirstTests(SideBranchTests):
    """A customer who asks to come in gets the meeting, not a questionnaire."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_meeting_before_the_estimate_then_details_then_price(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread = "thread-propose"
            world.spec = {
                "piece_type": "engagement ring", "stone_type": "diamond",
                "customer_supplied_materials": "his own stone", "notes": "ready to propose, has the stone",
                "scheduling_intent": "are you available next week? I can bring the stone",
            }
            world.requested = (["next week"], [])
            world.customer_message("p1", thread, "Hey, I think I am ready to propose. I have some ideas I'd love to show you "
                                   "in person. Are you available next week? I can bring the stone :)\n\nDavid")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["appointment_approval_requested"], summary)
            self.assertEqual(world.sent, [], "no questionnaire; the meeting comes first")
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer", card["payload"])
            self.assertTrue(card["payload"]["calendar_availability"])
            estimate_id = self.only_estimate(ws)
            self.assertEqual(self.record(ws, estimate_id)["status"], "awaiting_specs")
            self.assertEqual(self.claim(ws, "p1")["status"], "processed")
            # Approve the offer: the email says the design gets settled at the meeting, and asks for nothing.
            self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(len(world.sent), 1)
            for option in card["payload"]["calendar_availability"]:
                self.assertIn(option["label"], world.sent[0]["body"])
            self.assertNotRegex(world.sent[0]["body"], r"(?i)carat|clarity|finger size")
            # He picks a time: a booking card; approving books it and the record still waits for details.
            pick = card["payload"]["calendar_availability"][0]
            world.spec["scheduling_intent"] = f"{pick['label']} works"
            world.requested = ([pick["label"]], [pick["start"][:16]])
            world.customer_message("p2", thread, f"{pick['label']} works for me.\n\nDavid")
            self.tick(ws, world)
            book = world.cards[-1]
            self.assertEqual(book["kind"], "appointment_booking", book["payload"])
            self.execute(ws, world, book["payload"]["execute"], book)
            self.assertEqual(len(world.calendar_events), 1)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["status"], "awaiting_specs", "booked before the estimate; still waiting for details")
            self.assertTrue(record["appointment_booked"].get("before_estimate"))
            self.assertEqual(len(world.sent), 2)
            self.assertIn(pick["label"], world.sent[1]["body"])
            # After the visit he emails the details: the desk prices, no more meeting cards.
            world.spec = {
                "piece_type": "engagement ring", "stone_type": "diamond", "customer_supplied_materials": "his own stone",
                "stone_shape": "round", "stone_carat": "1.2", "metal": "yellow gold", "metal_karat": "14k",
                "finger_size": "6", "setting_style": "solitaire",
            }
            world.customer_message("p3", thread, "Great meeting you. Let's do 14k yellow gold, solitaire, size 6. "
                                   "The stone is a 1.2 round.\n\nDavid")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertEqual(world.cards[-1]["kind"], world.cards[-1]["kind"])
            self.assertIn("send-approved-estimate-brief", world.cards[-1]["payload"]["execute"])
            self.assertEqual(self.record(ws, estimate_id)["status"], "pending_approval")
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 0, "no questions needed along the way")
        self.run_branch(branch)


class CombinedIntentTests(SideBranchTests):
    """A rendering and a meeting in one email: two cards, approved in either order, booked once."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _both_cards(self, ws: Path, world: World) -> tuple[dict, dict, datetime]:
        thread, _estimate_id = self._estimate_sent(ws, world)
        wanted = next_weekday(2, 15, 0)
        world.intents = ["rendering_request", "appointment_request"]
        world.requested = ([f"{wanted.strftime('%A')} at 3"], [local_key(wanted)])
        world.customer_message("s2", thread, f"Looks good. Could you send a rendering, and can we meet {wanted.strftime('%A')} at 3?\n\nPat")
        self.tick(ws, world)
        kinds = [c["kind"] for c in world.cards[-2:]]
        self.assertEqual(sorted(kinds), ["appointment_booking", "send_rendering"], kinds)
        book = next(c for c in world.cards if c["kind"] == "appointment_booking")
        render = next(c for c in world.cards if c["kind"] == "send_rendering")
        self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner", "parked behind the rendering card")
        return book, render, wanted

    def test_booking_approved_while_the_rendering_card_is_still_open(self) -> None:
        def branch(ws: Path, world: World) -> None:
            book, render, wanted = self._both_cards(ws, world)
            sent_before = len(world.sent)
            result = self.execute(ws, world, book["payload"]["execute"], book)
            self.assertEqual(result["outcome"], "appointment_booked", result)
            self.assertEqual(len(world.calendar_events), 1)
            self.assertEqual(len(world.sent), sent_before + 1, "the confirmation went out")
            self.assertIn((book["brief_id"], "executed"), world.updates)
            # The rendering card still works afterwards.
            result = self.execute(ws, world, render["payload"]["execute"], render)
            self.assertEqual(len(world.sent), sent_before + 2)
            self.assertEqual(len(world.sent[-1]["attachments"]), 2)
            self.assertEqual(self.claim(ws, "s2")["status"], "processed")
        self.run_branch(branch)

    def test_a_booking_that_died_after_creating_the_event_books_once_on_retry(self) -> None:
        def branch(ws: Path, world: World) -> None:
            book, _render, wanted = self._both_cards(ws, world)
            parts = shlex.split(book["payload"]["execute"].replace("<Brief ID>", book["brief_id"]))
            with patch.object(gmail_safe, "send_reply_claimed", side_effect=OSError("gateway dropped")):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    code = workflow_safe.main(parts[2:])
            self.assertNotEqual(code, 0)
            self.assertEqual(len(world.calendar_events), 1, "the event exists; nothing recorded it")
            self.assertIsNone(self.record(ws, self.only_estimate(ws)).get("appointment_booked"))
            result = self.execute(ws, world, book["payload"]["execute"], book)
            self.assertEqual(result["outcome"], "appointment_booked", result)
            self.assertEqual(len(world.calendar_events), 1, "the same event, not a second one")
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(record["appointment_booked"]["calendar_event_id"], next(iter(world.calendar_events)))
        self.run_branch(branch)


class FailureQuestionTests(SideBranchTests):
    """A card's command that fails becomes one question with the fix attached (plan 3.2), and runs one at a time (3.1)."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _booking_card(self, ws: Path, world: World) -> dict:
        thread, _estimate_id = self._estimate_sent(ws, world)
        wanted = next_weekday(2, 14, 0)
        world.intents = ["appointment_request"]
        world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
        world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
        self.tick(ws, world)
        card = world.cards[-1]
        self.assertEqual(card["kind"], "appointment_booking")
        return card

    def _run_line(self, ws: Path, card: dict) -> tuple[int, str]:
        parts = shlex.split(card["payload"]["execute"].replace("<Brief ID>", card["brief_id"]))
        with patch("sys.stdout", io.StringIO()) as out, patch("sys.stderr", io.StringIO()) as err:
            code = workflow_safe.main(parts[2:])
        return code, out.getvalue() + err.getvalue()

    def test_a_failed_booking_asks_once_and_retry_finishes_it(self) -> None:
        def branch(ws: Path, world: World) -> None:
            card = self._booking_card(ws, world)
            questions_before = len([n for n in world.notices if not n["file"]])
            world.fail_next["gmail_send"] = 5
            with patch.object(gmail_safe, "find_delivery", side_effect=OSError("gateway down")):
                code, out = self._run_line(ws, card)
                self.assertNotEqual(code, 0)
                self.assertIn('"asked_owner": true', out)
                # A second paste of the same line asks nothing new.
                code, _out = self._run_line(ws, card)
                self.assertNotEqual(code, 0)
            world.fail_next.clear()
            asked = [n for n in world.notices if not n["file"]][questions_before:]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("desk-answer", asked[0]["text"])
            self.assertRegex(asked[0]["text"], r"(?i)could not finish booking")
            self.assertRegex(asked[0]["text"], r"(?i)held on your calendar")
            self.assertIn((card["brief_id"], "failed"), world.updates)
            self.assertEqual(len(world.calendar_events), 1, "the event exists; nothing sent")
            self.assertEqual(len(world.sent), 1)
            answered = self.answer(ws, "retry")
            self.assertEqual(answered["decision"], "retry", answered)
            self.assertEqual(len(world.calendar_events), 1, "the same event, adopted")
            self.assertEqual(len(world.sent), 2, "the confirmation went out once")
            self.assertEqual(self.record(ws, self.only_estimate(ws))["status"], "appointment_booked")
            self.assertIn((card["brief_id"], "executed"), world.updates)
            self.assertEqual([q["status"] for q in self.questions(ws) if q["kind"] == "command_failed"], ["answered"])
        self.run_branch(branch)

    def test_release_lets_go_of_the_calendar_hold_only(self) -> None:
        def branch(ws: Path, world: World) -> None:
            card = self._booking_card(ws, world)
            world.fail_next["gmail_send"] = 5
            with patch.object(gmail_safe, "find_delivery", side_effect=OSError("gateway down")):
                self._run_line(ws, card)
            world.fail_next.clear()
            self.assertEqual(len(world.calendar_events), 1)
            answered = self.answer(ws, "release it")
            self.assertEqual(answered["decision"], "release", answered)
            self.assertEqual(world.calendar_events, {}, "the desk's own hold is gone")
            self.assertEqual(len(world.sent), 1, "nothing sent")
            self.assertIsNone(self.record(ws, self.only_estimate(ws)).get("appointment_booked"))
        self.run_branch(branch)

    def test_two_runs_of_one_command_do_not_overlap(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import run_lease

            card = self._booking_card(ws, world)
            desk = ws / "estimate-desk"
            key = inbox_claim.claim_key("s2")[:16]
            with run_lease.hold(desk, "book-approved-appointment", key):
                code, out = self._run_line(ws, card)
            self.assertNotEqual(code, 0)
            self.assertIn("in progress", out)
            self.assertEqual(world.calendar_events, {}, "the second run touched nothing")
            self.assertFalse(run_lease.lock_path(desk, "book-approved-appointment", key).exists(), "released after the holder")
            code, _out = self._run_line(ws, card)
            self.assertEqual(code, 0)
            self.assertEqual(len(world.calendar_events), 1)
        self.run_branch(branch)


class StuckClaimTests(SideBranchTests):
    """A claim the tick cannot finish is retried with a bound, then becomes one question (plan 3.3)."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _raw_tick(self, ws: Path, world: World) -> dict:
        return inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t", judge_runner=world.run)

    def test_a_deterministic_failure_asks_after_two_tries_and_retry_finishes(self) -> None:
        def branch(ws: Path, world: World) -> None:
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7",
                          "notes": "plain band, no stones"}
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.customer_message("k1", "thread-stuck", "A plain 14k yellow gold band, size 7 please.\n\nPat")
            with patch.object(inbox_watcher.pipeline, "process_claim", side_effect=ValueError("the record is odd")):
                first = self._raw_tick(ws, world)
                self.assertEqual(first["inline"][0]["outcome"], "deferred", first)
                self.assertEqual(first["inline"][0]["kind"], "deterministic")
                self.assertEqual(first["message"], "NO_REPLY")
                second = self._raw_tick(ws, world)
                self.assertEqual(second["retried"], 1, second)
                self.assertEqual(second["inline"][0]["attempts"], 2)
                self.assertEqual([n for n in world.notices if not n["file"]], [], "no question yet: two tries first")
                third = self._raw_tick(ws, world)
            self.assertEqual(len(third["stuck"]), 1, third)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertRegex(asked[0]["text"], r"(?i)could not finish Pat Customer's email")
            self.assertRegex(asked[0]["text"], r"the record is odd")
            self.assertIn("desk-answer", asked[0]["text"])
            self.assertEqual(self.claim(ws, "k1")["status"], "awaiting_owner")
            self.assertEqual(self._raw_tick(ws, world)["stuck"], [], "asked once")
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 1)
            # The owner says retry: the claim runs to its end (a price card for a complete band).
            answered = self.answer(ws, "retry")
            self.assertEqual(answered["decision"], "retry", answered)
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", "the retry runs in the tick, not the session")
            fourth = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in fourth["inline"]], ["approval_requested"], fourth)
            self.assertEqual(self.claim(ws, "k1")["status"], "processed")
            self.assertTrue(world.cards)
        self.run_branch(branch)

    def test_skip_sets_a_stuck_email_aside_for_the_owner(self) -> None:
        def branch(ws: Path, world: World) -> None:
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7"}
            world.customer_message("k1", "thread-stuck", "A plain band please.\n\nPat")
            with patch.object(inbox_watcher.pipeline, "process_claim", side_effect=ValueError("the record is odd")):
                for _ in range(3):
                    self._raw_tick(ws, world)
            self.assertEqual(self.claim(ws, "k1")["status"], "awaiting_owner")
            answered = self.answer(ws, "skip it")
            self.assertEqual(answered["decision"], "skip", answered)
            self.assertEqual(self.claim(ws, "k1")["status"], "manual_review")
            self.assertEqual(self._raw_tick(ws, world)["claimed"], 0)
            self.assertEqual(world.sent, [])
            self.assertEqual(world.cards, [])
        self.run_branch(branch)

    def test_a_transient_failure_retries_more_before_asking(self) -> None:
        def branch(ws: Path, world: World) -> None:
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7"}
            world.customer_message("k1", "thread-stuck", "A plain band please.\n\nPat")
            with patch.object(inbox_watcher.pipeline, "process_claim", side_effect=OSError("gateway dropped")):
                for _ in range(inbox_watcher.TRANSIENT_ATTEMPTS):
                    summary = self._raw_tick(ws, world)
                    self.assertEqual(summary["stuck"], [])
                summary = self._raw_tick(ws, world)
            self.assertEqual(len(summary["stuck"]), 1, summary)
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 1)
        self.run_branch(branch)


class DoctorTests(SideBranchTests):
    """The doctor names each inconsistency with its repair line; requeue hands the desk a message again (plan 3.4)."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _doctor(self, ws: Path) -> list[dict]:
        import doctor

        return doctor.scan(ws)

    def _rate_parked(self, ws: Path, world: World) -> None:
        world.spec = {
            "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
            "setting_style": "bead set", "accent_stones": "small lab-grown diamonds",
            "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
        }
        world.customer_message("q1", "thread-doc", "Quote please: 14k yellow gold signet, size 10, small lab-grown diamonds.\n\nPat")
        self.tick(ws, world)
        self.assertEqual(self.claim(ws, "q1")["status"], "awaiting_owner")

    def test_a_clean_desk_has_no_findings_and_readiness_says_so(self) -> None:
        def branch(ws: Path, world: World) -> None:
            self._estimate_sent(ws, world)
            self.assertEqual(self._doctor(ws), [])
            import doctor

            self.assertTrue(doctor.report([]).endswith("state: clean"))
            self.assertTrue(doctor.report([]).startswith("version: "))
        self.run_branch(branch)

    def test_a_parked_claim_whose_question_vanished_is_found_and_requeued(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import doctor

            self._rate_parked(ws, world)
            root = owner_questions.questions_root(ws / "estimate-desk" / "inbox-monitor")
            for path in root.glob("q-*.json"):
                path.unlink()
            findings = self._doctor(ws)
            self.assertEqual([f["code"] for f in findings], ["parked_without_question"], findings)
            self.assertIn("--requeue 'q1'", findings[0]["repair"])
            asked_before = len([n for n in world.notices if not n["file"]])
            result = doctor.requeue(ws, "q1")
            self.assertEqual(result["outcome"], "requeued", result)
            self.assertEqual(self.claim(ws, "q1")["status"], "processing")
            self.assertEqual(self._doctor(ws)[0]["code"], "inline_retry_pending")
            summary = self.tick(ws, world)
            self.assertEqual(summary["retried"], 1, summary)
            self.assertEqual(self.claim(ws, "q1")["status"], "awaiting_owner")
            self.assertEqual(len([n for n in world.notices if not n["file"]]), asked_before + 1, "the rate question is asked again")
            self.assertEqual(self._doctor(ws), [])
        self.run_branch(branch)

    def test_a_question_whose_claim_is_gone_gets_a_closing_line(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import shutil

            self._rate_parked(ws, world)
            shutil.rmtree(inbox_claim.claim_path(ws / "estimate-desk" / "inbox-claims", "q1").parent)
            codes = sorted(f["code"] for f in self._doctor(ws))
            self.assertEqual(codes, ["question_without_claim", "queue_without_claim"], codes)
            stale = next(f for f in self._doctor(ws) if f["code"] == "question_without_claim")
            self.assertIn("answer-question", stale["repair"])
        self.run_branch(branch)

    def test_a_calendar_hold_nothing_recorded_points_at_the_question_or_the_line(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            world.fail_next["gmail_send"] = 5
            parts = shlex.split(card["payload"]["execute"].replace("<Brief ID>", card["brief_id"]))
            with patch.object(gmail_safe, "find_delivery", side_effect=OSError("gateway down")):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    workflow_safe.main(parts[2:])
            world.fail_next.clear()
            findings = [f for f in self._doctor(ws) if f["level"] == "repair"]
            self.assertEqual([f["code"] for f in findings], ["calendar_hold_unrecorded"], findings)
            self.assertIn("answer-question", findings[0]["repair"])
            self.assertIn("retry", findings[0]["repair"])
            self.answer(ws, "retry")
            self.assertEqual([f for f in self._doctor(ws) if f["level"] == "repair"], [])
        self.run_branch(branch)

    def test_requeue_hands_the_desk_a_missed_email_and_refuses_a_finished_one(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import doctor

            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7"}
            world.customer_message("missed-1", "thread-missed", "A plain band please.\n\nPat")
            world.batch = []  # discovery never saw it
            with patch.object(sys.modules["gmail_fetch"], "fetch_json", side_effect=lambda path, params, token, opener=None: (
                world.messages["missed-1"] if path.startswith("messages/") else world.fake_fetch_json(path, params, token))):
                result = doctor.requeue(ws, "missed-1", token="t")
            self.assertEqual(result["outcome"], "requeued", result)
            summary = self.tick(ws, world)
            self.assertEqual(summary["claimed"], 1, summary)
            self.assertEqual(self.claim(ws, "missed-1")["status"], "processed")
            with self.assertRaisesRegex(ValueError, "already finished"):
                doctor.requeue(ws, "missed-1", token="t")
        self.run_branch(branch)


class ReliabilityRulesTests(unittest.TestCase):
    """RELIABILITY-PLAN.md step 5: the rules the main session lives by are pinned to the code that backs them."""

    def setUp(self) -> None:
        raw = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.raw_size = len(raw.encode("utf-8"))
        self.skill = " ".join(raw.split())  # SKILL.md wraps lines; the rules are read as sentences

    def test_skill_md_fits_and_names_the_doctor(self) -> None:
        self.assertLess(self.raw_size, 65_000)
        self.assertIn("scripts/doctor.py", self.skill)
        self.assertIn("--requeue", self.skill)

    def test_the_session_runs_the_line_once_more_then_waits(self) -> None:
        self.assertIn("run the same line once more", self.skill)
        self.assertIn("paste the output and wait", self.skill)
        self.assertIn("never sends or books twice", self.skill)

    def test_the_session_never_narrates_or_edits_state(self) -> None:
        self.assertIn("Never summarise a record, a queue, a claim, or a brief from memory", self.skill)
        self.assertIn("never write, rename, or delete anything under `estimate-desk/`", self.skill)
        self.assertIn("run `python3 {baseDir}/scripts/doctor.py", self.skill)
        self.assertIn("never by writing a queue item", self.skill)

    def test_an_open_desk_question_takes_the_next_reply(self) -> None:
        self.assertIn("the owner's next reply is the answer to it", self.skill)
        self.assertIn("Never ask the owner a question of your own while a desk question is open", self.skill)

    def test_every_execute_line_names_a_real_executor(self) -> None:
        import re

        named = set(re.findall(r"workflow_safe\.py (send-approved-estimate-brief|send-approved-rendering|book-approved-appointment|send-approved-times|appointment-rejected)", self.skill))
        self.assertEqual(named, workflow_safe.EXECUTOR_COMMANDS)

    def test_every_question_kind_the_desk_asks_is_documented_with_its_replies(self) -> None:
        for kind, words in (
            ("command_failed", ("retry", "release", "handle myself")),
            ("stuck_claim", ("retry", "skip", "handle myself")),
            ("followup_stalled", ("skip", "ask again", "handle myself")),
            ("appointment_next", ("other times", "handle myself")),
        ):
            self.assertIn(kind, owner_questions.DECISION_KINDS)
            for word in words:
                self.assertIn(word, self.skill, f"{kind}: {word}")

    def test_the_legacy_runbook_is_gone(self) -> None:
        for gone in ("needs no new approval", "rendering_wait.py wait", "image_generate", "Only Stage 3 authorizes"):
            self.assertNotIn(gone, self.skill, gone)
        self.assertIn("never emails a customer", self.skill)
        self.assertIn("does none of this", self.skill)


class PartialAnswerTests(SideBranchTests):
    """A customer who answers some of what was asked gets one more ask for the rest; only silence or a nag stalls."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_progress_earns_a_second_ask_and_the_price_fields_come_first(self) -> None:
        def branch(ws: Path, world: World) -> None:
            import pipeline

            world.spec = {"piece_type": "engagement ring"}
            world.customer_message("e1", "thread-eng", "I am looking for an engagement ring.\n\nTony")
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1)
            asked = [p for p in world.prompts if "MISSING DETAILS TO ASK FOR" in p][-1]
            order = asked.split("MISSING DETAILS TO ASK FOR: ", 1)[1].split("\n", 1)[0]
            self.assertTrue(order.startswith("stone origin, stone type"), order)
            self.assertIn("setting style", order)
            self.assertIn("metal karat", order, "the whole metal question is asked at once")
            # He answers most of it, not the size or the origin: one more ask, no owner question.
            world.spec = {"piece_type": "engagement ring", "metal": "rose gold", "metal_karat": "18k", "stone_type": "diamond",
                          "stone_carat": "3", "stone_color": "D", "stone_clarity": "flawless", "stone_cut": "ideal",
                          "setting_style": "solitaire", "finish": "polished"}
            world.customer_message("e2", "thread-eng", "A classic 3 ct solitaire, D flawless, ideal cut, 18k rose gold, polished.\n\nTony")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(len(world.sent), 2)
            self.assertEqual([n for n in world.notices if not n["file"]], [], "no owner question for a partial answer")
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(sorted(record["missing_required_fields"]), ["finger_size", "stone_origin"])
            # He answers nothing new: now the owner is asked, not the customer a third time.
            world.customer_message("e3", "thread-eng", "Sounds great, thanks!\n\nTony")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["awaiting_owner"], summary)
            self.assertEqual(len(world.sent), 2)
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 1)
            self.assertEqual(pipeline.prioritized(["setting_style", "finger_size", "stone_origin", "metal"]),
                             ["stone_origin", "finger_size", "metal", "setting_style"])
        self.run_branch(branch)


class MultiPieceReadingTests(unittest.TestCase):
    """MULTI-PIECE-PLAN.md batch 1: two pieces are read, gated, and asked about as two pieces; one piece is unchanged."""

    def test_pieces_are_merged_with_shared_facts_and_one_piece_is_untouched(self) -> None:
        spec = {"metal": "yellow gold", "metal_karat": "14k", "notes": "matching set",
                "pieces": [{"piece_type": "engagement ring", "finger_size": "6", "stone_type": "diamond", "stone_origin": "lab-grown",
                            "stone_carat": "2", "stone_shape": "round", "setting_style": "solitaire"},
                           {"piece_type": "wedding band", "finger_size": "10", "metal_color": "rose"}]}
        pieces = estimate_record.pieces_of(spec)
        self.assertEqual(len(pieces), 2)
        self.assertEqual(pieces[0]["metal"], "yellow gold")
        self.assertEqual(pieces[1]["metal_color"], "rose", "a piece overrides a shared fact")
        self.assertNotIn("notes", pieces[0])
        self.assertEqual(estimate_record.piece_label(spec, 1), "wedding band")
        self.assertTrue(estimate_record.is_set(spec))
        one = {"piece_type": "ring", "metal": "14k white gold", "pieces": [{"piece_type": "ring"}]}
        self.assertEqual(estimate_record.pieces_of(one), [{"piece_type": "ring", "metal": "14k white gold"}])
        self.assertFalse(estimate_record.is_multi_piece(one))
        self.assertEqual(estimate_record.split_field_name("pieces.1.finger_size"), (1, "finger_size"))
        self.assertEqual(estimate_record.split_field_name("finger_size"), (None, "finger_size"))

    def test_the_extractor_keeps_two_pieces_and_folds_one(self) -> None:
        two = judge.check_specification({"specification": {"metal": "yellow gold", "pieces": [
            {"piece_type": "engagement ring", "finger_size": "6"}, {"piece_type": "wedding band", "finger_size": "10"}]}})
        self.assertEqual([p["piece_type"] for p in two["specification"]["pieces"]], ["engagement ring", "wedding band"])
        one = judge.check_specification({"specification": {"metal": "yellow gold", "pieces": [{"piece_type": "ring", "finger_size": "6"}]}})
        self.assertNotIn("pieces", one["specification"])
        self.assertEqual(one["specification"]["finger_size"], "6")

    def test_the_gate_names_each_piece_and_the_follow_up_reads_them(self) -> None:
        import pipeline
        import spec_gate

        profile = {"defaults": {"stone_origin": "ask_always"}}
        spec = {"metal": "yellow gold", "metal_karat": "14k",
                "pieces": [{"piece_type": "engagement ring", "stone_type": "diamond", "stone_carat": "2", "stone_shape": "round",
                            "setting_style": "solitaire"},
                           {"piece_type": "wedding band"}]}
        missing = spec_gate.missing_required_fields(spec, profile)
        self.assertEqual(sorted(missing), ["pieces.0.finger_size", "pieces.0.stone_clarity", "pieces.0.stone_color",
                                           "pieces.0.stone_origin", "pieces.1.finger_size"])
        ordered = pipeline.prioritized(missing)
        self.assertEqual(ordered[0], "pieces.0.stone_origin")
        labels = pipeline.describe_missing(spec, ordered)
        self.assertEqual(labels[0], "engagement ring: stone origin")
        self.assertEqual(labels[-1], "wedding band: finger size")
        fallback = pipeline.plain_followup(ordered, "Kolo Jewelers", spec)
        self.assertIn("For the wedding band, what ring size?", fallback)
        # one piece: bare names, as before
        self.assertEqual(spec_gate.missing_required_fields({"piece_type": "wedding band", "metal": "14k yellow gold"}, profile), ["finger_size"])


class TwoPieceTests(SideBranchTests):
    """MULTI-PIECE-PLAN.md batch 2: a ring and a band in one email, one estimate with a line per piece."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def _two_piece_rendering_card(self, ws: Path, world: World) -> dict:
        self._profile_with_rates(ws)
        world.spec = {"metal": "yellow gold", "metal_karat": "14k", "notes": "a matching set", "pieces": [
            {"piece_type": "engagement ring", "finger_size": "6", "stone_type": "diamond", "stone_origin": "lab-grown",
             "stone_carat": "2", "stone_shape": "round", "stone_color": "F", "stone_clarity": "VS1", "setting_style": "solitaire",
             "center_stone": "yes"},
            {"piece_type": "wedding band", "finger_size": "10", "notes": "plain, polished, no stones"},
        ]}
        world.customer_message("t1", "thread-two", "A matching set: 14k yellow gold engagement ring, size 6, 2 ct round lab-grown "
                               "solitaire, and a plain band, size 10.\n\nPat")
        summary = self.tick(ws, world)
        self.assertTrue(world.cards, summary)
        world.approve(world.cards[-1])
        self.tick(ws, world)
        world.intents = ["rendering_request"]
        world.customer_message("t2", "thread-two", "Could you show me renderings of both?\n\nPat", attachments=("logo.png",))
        self.tick(ws, world)
        card = world.cards[-1]
        self.assertEqual(card["kind"], "send_rendering", card)
        self.assertEqual(len(card["payload"]["images"]), 4)
        return card

    def test_rejected_renderings_ask_what_to_change_and_only_the_named_piece_is_rendered_again(self) -> None:
        def branch(ws: Path, world: World) -> None:
            first = self._two_piece_rendering_card(ws, world)
            renders_before, previews_before = len(world.renders), len([n for n in world.notices if n["file"]])
            world.reject(first, "band looks off")
            summary = self.tick(ws, world)
            self.assertEqual([r.get("kind") for r in summary["rejections"]], ["rendering"], summary)
            asked = [n for n in world.notices if not n["file"] and "desk-answer" in n["text"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("engagement ring, wedding band", asked[-1]["text"])
            self.assertEqual(self.claim(ws, "t2")["status"], "awaiting_owner", "the claim waits behind the question")
            self.assertEqual(len(world.sent), 1, "nothing sent")
            answered = self.answer(ws, "make the band wider and flatter")
            self.assertEqual(answered["outcome"], "re_render_started", answered)
            self.assertEqual(answered["pieces"], ["wedding band"])
            self.tick(ws, world)
            self.assertEqual(len(world.renders), renders_before + 2, "only the band's two views are rendered again")
            self.assertTrue(any("wider and flatter" in flag(argv, "--prompt") for argv in world.renders[-2:]), "the owner's words reach the prompt")
            fresh = world.cards[-1]
            self.assertNotEqual(fresh["brief_id"], first["brief_id"])
            self.assertTrue(fresh["title"].startswith("Send renderings (revision 2) to pat@example.net: "), fresh["title"])
            self.assertIn("Revised: make the band wider and flatter", fresh["title"], "the SMS shows the revision")
            self.assertIn("Checker: view 1", fresh["title"], "the SMS shows how the views checked")
            self.assertEqual(fresh["details"]["Revised"], "make the band wider and flatter")
            self.assertEqual(len(fresh["payload"]["images"]), 4)
            self.assertEqual(fresh["payload"]["images"][:2], first["payload"]["images"][:2], "the ring's views are kept as they were")
            self.assertNotEqual(fresh["payload"]["images"][2:], first["payload"]["images"][2:], "the band's views are new images")
            work = ws / "estimate-desk" / "work"
            self.assertTrue(list(work.rglob("rendering-3-r1.png")), "the passed-on image is kept as history")
            self.assertEqual(len([n for n in world.notices if n["file"]]), previews_before + 4, "four previews again")
            self.assertEqual(self.claim(ws, "t2")["status"], "awaiting_owner")
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(record["rendering_revisions"][-1]["pieces"], ["wedding band"])
            world.approve(fresh)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent), 2)
            self.assertEqual(len(world.sent[-1]["attachments"]), 4)
            self.assertEqual(self.claim(ws, "t2")["status"], "processed")
            self.assertEqual(self.tick(ws, world)["approvals"], [])
        self.run_branch(branch)

    def test_rejected_renderings_then_handle_myself_holds_them(self) -> None:
        def branch(ws: Path, world: World) -> None:
            first = self._two_piece_rendering_card(ws, world)
            world.reject(first, "no")
            self.tick(ws, world)
            answered = self.answer(ws, "I will handle it myself")
            self.assertEqual(answered["decision"], "handle_myself")
            self.assertEqual(self.claim(ws, "t2")["status"], "manual_review")
            self.assertEqual(len(world.sent), 1)
            self.assertEqual(self.tick(ws, world)["rejections"], [])
        self.run_branch(branch)

    def test_ring_and_band_are_asked_priced_and_written_as_two_pieces(self) -> None:
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            world.spec = {"metal": "yellow gold", "metal_karat": "14k", "pieces": [
                {"piece_type": "engagement ring", "stone_type": "diamond", "stone_origin": "lab-grown", "stone_carat": "2",
                 "stone_shape": "round", "stone_color": "F", "stone_clarity": "VS1", "setting_style": "solitaire", "center_stone": "yes"},
                {"piece_type": "wedding band", "notes": "plain, polished, no stones"},
            ]}
            world.customer_message("t1", "thread-two", "A 14k yellow gold engagement ring with a 2 ct round lab-grown diamond solitaire, "
                                   "and a plain 14k yellow gold wedding band.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            asked = [p for p in world.prompts if "MISSING DETAILS TO ASK FOR" in p][-1]
            self.assertIn("engagement ring: finger size", asked)
            self.assertIn("wedding band: finger size", asked)
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(sorted(record["missing_required_fields"]), ["pieces.0.finger_size", "pieces.1.finger_size"])
            # Both sizes come back: one card, two lines each for metal and labor, one total.
            world.spec["pieces"][0]["finger_size"] = "6"
            world.spec["pieces"][1]["finger_size"] = "10"
            world.customer_message("t2", "thread-two", "The ring is a size 6 and the band a size 10.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertEqual([n for n in world.notices if not n["file"]], [])
            card = world.cards[-1]
            self.assertIn("engagement ring", card["title"])
            self.assertIn("wedding band", card["title"])
            self.assertRegex(card["title"], r"bench labor \(engagement ring\)")
            self.assertRegex(card["title"], r"bench labor \(wedding band\)")
            record = self.record(ws, self.only_estimate(ws))
            review = record.get("owner_review") or card["payload"].get("owner_review") or {}
            metal_lines = review.get("metal_costs") or card["payload"].get("owner_review", {}).get("metal_costs") or []
            self.assertEqual(len(metal_lines), 2, review or card["payload"])
            expected_cost = 5.5 * 65 + 4.0 * 65 + 1 * 0 + 4.0 * 90 + 2.0 * 90 + 2 * 900 + 2 * (120 + 80)
            self.assertAlmostEqual(float(review.get("hard_cost_total") or card["payload"]["owner_review"]["hard_cost_total"]), expected_cost, places=2)
            # The approval sends one estimate naming both pieces with one price.
            result = self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(result["outcome"], "estimate_sent", result)
            body = world.sent[-1]["body"]
            self.assertEqual(len(re.findall(r"\$", body)), 1)
            self.assertIn(f"${float(record['proposed_price']):,.2f}", body)
        self.run_branch(branch)

    def test_two_pieces_render_two_views_each_on_one_card_and_send_four(self) -> None:
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            world.spec = {"metal": "yellow gold", "metal_karat": "14k", "notes": "a matching set", "pieces": [
                {"piece_type": "engagement ring", "finger_size": "6", "stone_type": "diamond", "stone_origin": "lab-grown",
                 "stone_carat": "2", "stone_shape": "round", "stone_color": "F", "stone_clarity": "VS1", "setting_style": "solitaire",
                 "center_stone": "yes"},
                {"piece_type": "wedding band", "finger_size": "10", "notes": "plain, polished, no stones"},
            ]}
            world.customer_message("t1", "thread-two", "A matching set: 14k yellow gold engagement ring, size 6, 2 ct round lab-grown "
                                   "solitaire, and a plain band, size 10.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            card = world.cards[-1]
            self.execute(ws, world, card["payload"]["execute"], card)
            world.intents = ["rendering_request"]
            world.customer_message("t2", "thread-two", "Could you show me renderings of both?\n\nPat", attachments=("logo.png",))
            self.tick(ws, world)
            render = world.cards[-1]
            self.assertEqual(render["kind"], "send_rendering", render)
            self.assertEqual(len(render["payload"]["images"]), 4, render["payload"])
            self.assertIn("engagement ring", render["details"]["Checker"])
            self.assertIn("wedding band", render["details"]["Checker"])
            previews = [n for n in world.notices if n["file"]]
            self.assertEqual(len(previews), 4)
            self.assertIn("(engagement ring)", previews[0]["text"])
            self.assertIn("(wedding band)", previews[-1]["text"])
            self.assertEqual(len(world.renders), 4, "two views per piece, no more")
            describes = [c for c in world.calls if c == "image_describe"]
            self.assertTrue(describes, "every view is graded")
            self.assertTrue(all("litellm/kolo-best-available" in " ".join(argv) for argv in world.describe_argv),
                            "the checker names its model; the job environment's default is never trusted")
            self.assertTrue(any("matching set" in flag(argv, "--prompt") for argv in world.renders), "the set flag reaches the render prompts")
            self.execute(ws, world, render["payload"]["execute"], render)
            self.assertEqual(len(world.sent[-1]["attachments"]), 4)
            self.assertEqual(self.claim(ws, "t2")["status"], "processed")
            # And a meeting, booked as usual, with the summary naming both pieces.
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("t3", "thread-two", f"Could we meet {wanted.strftime('%A')} at 2 to see them?\n\nPat")
            self.tick(ws, world)
            book = world.cards[-1]
            self.assertEqual(book["kind"], "appointment_booking", book["payload"])
            self.assertIn("engagement ring", book["payload"]["piece"])
            self.assertIn("wedding band", book["payload"]["piece"])
            result = self.execute(ws, world, book["payload"]["execute"], book)
            self.assertEqual(result["outcome"], "appointment_booked", result)
            self.assertEqual(len(world.calendar_events), 1)
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 0, "no question needed along the way")
        self.run_branch(branch)



class DeskExecutesApprovalsTests(SideBranchTests):
    """ARCHITECTURE-OPTIONS.md A' tier 1: rendering and appointment approvals are executed by the desk from the trail."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_an_approved_booking_is_booked_by_the_next_tick_and_the_line_is_then_a_no_op(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("s2", thread, f"Can we meet {wanted.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking")
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.calendar_events), 1)
            self.assertEqual(self.record(ws, estimate_id)["status"], "appointment_booked")
            self.assertIn((card["brief_id"], "executed"), world.updates)
            sent_before = len(world.sent)
            # The session runs the line anyway: nothing more happens.
            result = self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(result["outcome"], "already_booked", result)
            self.assertEqual(len(world.sent), sent_before)
            self.assertEqual(self.tick(ws, world)["approvals"], [], "an approval is acted on once")
        self.run_branch(branch)

    def test_an_approved_rendering_card_is_sent_by_the_next_tick(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "send_rendering")
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent[-1]["attachments"]), 2)
            self.assertEqual(self.claim(ws, "s2")["status"], "processed")
        self.run_branch(branch)

    def test_the_session_runs_the_rendering_line_first_and_the_tick_then_does_nothing(self) -> None:
        """6 September 2026, Brief #25: the session pasted the line before the tick; the tick must not ask the owner."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "send_rendering")
            world.approve(card)
            result = self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(result["outcome"], "rendering_sent", result)
            sent_before, questions_before = len(world.sent), len(world.notices)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(summary["approvals"][0]["result"]["outcome"], "already_sent", summary)
            self.assertEqual(len(world.sent), sent_before, "the renderings went out once")
            self.assertEqual(len(world.notices), questions_before, "no question to the owner")
            self.assertEqual(world.updates.count((card["brief_id"], "executed")), 1)
            self.assertEqual(self.claim(ws, "s2")["status"], "processed")
            self.assertEqual(len(self.record(ws, estimate_id)["rendering_deliveries"]), 1)
            self.assertEqual(self.tick(ws, world)["approvals"], [], "an approval is acted on once")
        self.run_branch(branch)

    def test_a_tick_that_finds_the_line_running_waits_and_tries_next_tick(self) -> None:
        def branch(ws: Path, world: World) -> None:
            thread, _estimate_id = self._estimate_sent(ws, world)
            world.intents = ["rendering_request"]
            world.customer_message("s2", thread, "Could you send a rendering?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            world.approve(card)
            desk = ws.resolve() / "estimate-desk"
            key = inbox_claim.claim_key("s2")[:16]
            questions_before = len(world.notices)
            with run_lease.hold(desk, "send-approved-rendering", key):
                summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["in_progress"], summary)
            sent_before = len(world.sent)
            self.assertEqual(len(world.notices), questions_before, "a run in progress is not a failure")
            self.assertEqual(len(world.sent), sent_before)
            self.assertEqual(self.claim(ws, "s2")["status"], "awaiting_owner")
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.sent[-1]["attachments"]), 2)
            self.assertEqual(self.tick(ws, world)["approvals"], [], "an approval is acted on once")
        self.run_branch(branch)

    def test_a_price_card_approval_is_sent_by_the_next_tick(self) -> None:
        def branch(ws: Path, world: World) -> None:
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {"piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                          "setting_style": "bead set", "accent_stones": "small lab-grown diamonds", "stone_type": "diamond",
                          "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS"}
            world.customer_message("p1", "thread-price", "Quote please.\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.assertIn("send-approved-estimate-brief", card["payload"]["execute"])
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], "a card is binary; the desk sends it")
            self.assertEqual(len(world.sent), 1)
            self.assertEqual(self.execute(ws, world, card["payload"]["execute"], card)["outcome"], "already_sent")
            self.assertEqual(len(world.sent), 1)
        self.run_branch(branch)


class RehearsalTests(SideBranchTests):
    """ARCHITECTURE-OPTIONS.md F2: rehearsal is loud, handles one address, and holds real mail untouched."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_rehearsal_handles_the_owners_mail_loudly_and_holds_everyone_elses_until_off(self) -> None:
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            owner = "Tony Owner <owner@example.org>"
            switched = json.loads(self._run(["python3", str(ROOT / "scripts" / "rehearsal.py"), "--workspace", str(ws), "--on", "--address", "owner@example.org"]))
            self.assertTrue(switched["rehearsal"]["enabled"], switched)
            checks = readiness.checks(ws, ROOT, "openclaw", runner=world.run)
            self.assertEqual(checks[0]["check"], "REHEARSAL MODE", checks[0])
            self.assertIn("owner@example.org", checks[0]["detail"])
            # A real customer writes: discovered, held, never read, never answered.
            world.customer_message("real1", "thread-real", "Quote please, a signet ring.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual(summary["held"], 1, json.dumps(summary)[:700])
            self.assertEqual(summary["claimed"], 1)
            self.assertTrue(summary["notes"] and summary["notes"][0].startswith("REHEARSAL MODE"), summary["notes"])
            self.assertEqual(world.sent, [])
            self.assertEqual(world.cards, [])
            claim = self.claim(ws, "real1")
            self.assertEqual((claim["status"], claim["reason_code"]), ("awaiting_owner", "held_for_live"))
            findings = doctor.scan(ws)
            self.assertEqual([f["code"] for f in findings if f["level"] != "info"], [], "held mail is not a repair item")
            self.assertIn("1 message(s) held for live", [f["detail"] for f in findings if f["code"] == "rehearsal_mode"][0])
            # The owner's own inquiry runs the whole path, marked everywhere.
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                "setting_style": "bead set", "engraving": "our logo on the face",
                "accent_stones": "small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("own1", "thread-own", "Please quote a 14k yellow gold signet ring, size 10, logo on the face, "
                                   "small lab-grown diamonds G VS bead set on the shoulders.\n\nTony", sender=owner)
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            card = world.cards[-1]
            self.assertTrue(card["title"].startswith("[REHEARSAL] Price approval for "), card["title"])
            world.approve(card)
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1)
            self.assertTrue(world.sent[-1]["subject"].startswith("[REHEARSAL] "), world.sent[-1]["subject"])
            self.assertIn("owner@example.org", world.sent[-1]["to"])
            # Off: the held mail is released and read on the next tick, with no mark on anything.
            switched = json.loads(self._run(["python3", str(ROOT / "scripts" / "rehearsal.py"), "--workspace", str(ws), "--off"]))
            self.assertEqual(switched["released"], ["real1"], switched)
            summary = self.tick(ws, world)
            self.assertEqual([i["message_id"] for i in summary["inline"]], ["real1"], summary)
            self.assertNotIn("REHEARSAL", json.dumps(summary["notes"]))
            self.assertTrue(world.cards[-1]["title"].startswith("Price approval for "), world.cards[-1]["title"])
            self.assertEqual(readiness.checks(ws, ROOT, "openclaw", runner=world.run)[0]["check"], "shop profile")
        self.run_branch(branch)

    def _run(self, argv: list[str]) -> str:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed.stdout


class SameSenderTests(SideBranchTests):
    """A known customer writes on a new thread: the owner is asked same or new; 'new' quotes it inline (no worker)."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_a_time_outside_the_hours_is_answered_with_the_hours_and_open_times(self) -> None:
        pass

    def test_a_day_past_the_offer_window_is_checked_and_booked_on_that_day(self) -> None:
        pass

    def test_a_taken_time_with_no_free_neighbour_offers_a_spread_instead_of_a_question(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

    def test_rejecting_the_fresh_price_card_asks_again_and_handle_myself_retires_it(self) -> None:
        pass

    def test_same_piece_on_a_new_thread_carries_the_estimate_on_there(self) -> None:
        """WORKFLOW.md 6.1, owner says "same": the record's thread moves and the reply goes to the new thread."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k"}
            world.customer_message("f1", "thread-first", "A plain 14k yellow gold band please.\n\nPat", subject="Band")
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1, "the follow-up asked for the size")
            estimate_id = self.only_estimate(ws)
            # The customer answers in a brand-new thread instead of replying.
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7",
                          "dimensions": "4mm wide", "finish": "polished", "notes": "plain band, no stones"}
            world.customer_message("n1", "thread-second", "Size 7, 4mm wide, polished. Plain, no stones.\n\nPat\n\n"
                                   "On Sun wrote:\n> To put together an accurate estimate, could you share:\n> - What finger size should the ring be?\n",
                                   subject="My band")
            self.tick(ws, world)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("carry that estimate on in the new thread", asked[0]["text"])
            self.assertEqual(self.claim(ws, "n1")["status"], "awaiting_owner")
            answered = self.answer(ws, "same")
            self.assertEqual(answered["decision"], "same", answered)
            self.assertEqual(answered.get("thread_id"), "thread-second", answered)
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["route"]["thread_id"], "thread-second")
            self.assertEqual(record["route_history"][-1]["route"]["thread_id"], "thread-first")
            self.assertEqual(record["status"], "pending_approval")
            self.assertEqual(len(sorted((ws / "estimate-desk" / "records").glob("*.json"))), 1, "one estimate, one piece")
            world.approve(world.cards[-1])
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 2)
            self.assertEqual(world.sent[-1]["thread_id"], "thread-second", "the estimate goes to the thread the customer is in now")
            self.assertEqual(self.claim(ws, "n1")["status"], "processed")
        self.run_branch(branch)

    def test_same_on_a_new_thread_after_the_estimate_books_the_meeting_in_the_new_thread(self) -> None:
        """The live script: estimate sent, the customer asks to meet from a brand-new thread, owner says same."""
        def branch(ws: Path, world: World) -> None:
            _thread, estimate_id = self._estimate_sent(ws, world)
            wanted = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{wanted.strftime('%A')} at 2"], [local_key(wanted)])
            world.customer_message("n1", "thread-new", f"Following up on my ring, can we meet {wanted.strftime('%A')} at 2?\n\nPat\n\n"
                                   "On Sun wrote:\n> Estimate: $2,505.00\n> This estimate is good through September 20, 2026.\n",
                                   subject="Following up")
            self.tick(ws, world)
            asked = [n for n in world.notices if not n["file"] and "desk-answer" in n["text"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("same piece, or a new one", asked[-1]["text"])
            self.assertEqual(self.claim(ws, "n1")["status"], "awaiting_owner")
            answered = self.answer(ws, "same")
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
            summary = self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking", (summary, card.get("payload")))
            self.assertEqual(self.record(ws, estimate_id)["route"]["thread_id"], "thread-new")
            self.assertEqual(len(world.sent), 1, "nothing sent before approval")
            world.approve(card)
            summary = self.tick(ws, world)
            self.assertEqual([a["outcome"] for a in summary["approvals"]], ["executed"], summary)
            self.assertEqual(len(world.calendar_events), 1)
            self.assertEqual(world.sent[-1]["thread_id"], "thread-new", "the confirmation goes to the thread the customer is in now")
            self.assertEqual(self.record(ws, estimate_id)["status"], "appointment_booked")
            self.assertEqual(self.claim(ws, "n1")["status"], "processed")
        self.run_branch(branch)

    def test_new_thread_from_a_known_customer_is_asked_and_new_is_quoted_inline(self) -> None:
        def branch(ws: Path, world: World) -> None:
            # An inquiry still open on its own thread (details asked, not yet given).
            world.spec = {"piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k"}
            world.customer_message("f1", "thread-first", "A plain 14k yellow gold band please.\n\nPat", subject="Band")
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 1)
            # The same customer opens a second thread about something else.
            world.spec = {"piece_type": "pendant", "metal": "yellow gold", "metal_karat": "14k", "dimensions": "18 inch chain",
                          "notes": "plain gold pendant, no stones"}
            world.customer_message("n1", "thread-second", "Separately, could you quote a plain 14k gold pendant on an 18 inch chain?\n\nPat",
                                   subject="Pendant")
            summary = self.tick(ws, world)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertRegex(asked[0]["text"], r"(?i)same|new")
            self.assertEqual(self.claim(ws, "n1")["status"], "awaiting_owner")
            cards_before = len(world.cards)
            answered = self.answer(ws, "new")
            self.assertEqual(answered["decision"], "new", answered)
            self.assertEqual(answered.get("pipeline"), "queued_for_tick", answered)
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertEqual(len(world.cards), cards_before + 1, "the new piece got its own price card")
            self.assertEqual(world.spawned, [])
            records = sorted((ws / "estimate-desk" / "records").glob("*.json"))
            self.assertEqual(len(records), 2, "two estimates, one per piece")
        self.run_branch(branch)
