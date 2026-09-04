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
import estimate_record  # noqa: E402
import gateway_token  # noqa: E402
import gmail_safe  # noqa: E402
import inbox_claim  # noqa: E402
import inbox_monitor  # noqa: E402
import inbox_watcher  # noqa: E402
import judge  # noqa: E402
import kolo_safe  # noqa: E402
import owner_questions  # noqa: E402
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


class World:
    """Everything outside the skill: Gmail, Kolo, the calendar, the model, the image tool."""

    def __init__(self, ws: Path) -> None:
        self.ws = ws
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
        self.spawned: list[list[str]] = []
        self.other: list[list[str]] = []
        self.busy: list[dict[str, str]] = []
        self.calendar_events: dict[str, dict] = {}
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

    # ---- Gmail ---------------------------------------------------------
    def customer_message(self, message_id: str, thread_id: str, body: str, subject: str = "Custom signet ring",
                         attachments: tuple[str, ...] = ()) -> dict:
        self.clock_ms += 100
        parts = [{"mimeType": "text/plain", "body": {"data": b64url(body)}}]
        for name in attachments:
            parts.append({"mimeType": "image/png", "filename": name,
                          "body": {"attachmentId": f"att-{name}", "size": len(PNG)}})
        headers = {
            "From": CUSTOMER, "To": SHOP_MAILBOX, "Subject": subject,
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

    def _shop_message(self, thread_id: str, subject: str, body: str) -> dict:
        self.clock_ms += 100
        self.email_count += 1
        message_id = f"sent-{self.email_count}"
        message = {
            "id": message_id, "threadId": thread_id, "internalDate": str(self.clock_ms),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": "From", "value": f"Kolo Jewelers <{SHOP_MAILBOX}>"},
                                    {"name": "To", "value": CUSTOMER}, {"name": "Subject", "value": subject},
                                    {"name": "Message-ID", "value": f"<{message_id}@example.com>"}],
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
        paths = inbox_monitor.prepare_claim_work(monitor_root, claim_root, message_id)
        message = self.messages[message_id]
        Path(paths["gmail_message"]).write_text(json.dumps(message), encoding="utf-8")
        Path(paths["gmail_thread"]).write_text(json.dumps(self.thread(message["threadId"])), encoding="utf-8")
        return {"gmail_message": paths["gmail_message"], "gmail_thread": paths["gmail_thread"]}

    def fake_fetch_json(self, path: str, params, token: str, opener=None) -> dict:
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
        sent = self._shop_message(payload["threadId"], str(mime["Subject"]), body)
        self.sent.append({"id": sent["id"], "thread_id": payload["threadId"], "subject": str(mime["Subject"]),
                          "to": str(mime["To"]), "body": body, "attachments": attachments})
        return ok(argv, json.dumps({"id": sent["id"], "threadId": payload["threadId"]}))

    # ---- Calendar ------------------------------------------------------
    def query_freebusy(self, time_min, time_max, timezone_name, calendar_id, token, opener=None) -> dict:
        lo, hi = calendar_query.parse_timestamp(time_min, "a"), calendar_query.parse_timestamp(time_max, "b")
        busy = [b for b in self.busy
                if calendar_query.parse_timestamp(b["start"], "s") < hi and calendar_query.parse_timestamp(b["end"], "e") > lo]
        query = {"timeMin": time_min, "timeMax": time_max, "timeZone": timezone_name, "items": [{"id": calendar_id}]}
        body = {"kind": "calendar#freeBusy", "timeMin": time_min, "timeMax": time_max,
                "calendars": {calendar_id: {"busy": busy}}}
        return {"schema_version": 1, "provider": "google_calendar_freebusy",
                "provider_request_id": "request-0123456789abcdef",
                "response_date": format_datetime(datetime.now(timezone.utc)), "query": query,
                "response_body_sha256": calendar_query.canonical_hash(body), "response_body": body}

    def create_event(self, calendar_id, start, end, timezone_name, summary, description, attendee_email, token,
                     opener=None) -> dict:
        self.event_count += 1
        event = {"kind": "calendar#event", "id": f"evt-{self.event_count}", "summary": summary,
                 "start": {"dateTime": start, "timeZone": timezone_name}, "end": {"dateTime": end, "timeZone": timezone_name},
                 "attendees": [{"email": attendee_email}], "htmlLink": f"https://calendar.example/{self.event_count}",
                 "status": "confirmed"}
        self.calendar_events[event["id"]] = event
        self.busy.append({"start": start, "end": end, "event": event["id"]})
        return event

    def delete_event(self, calendar_id, event_id, token, opener=None) -> bool:
        self.deleted_events.append(event_id)
        self.busy = [b for b in self.busy if b.get("event") != event_id]
        return self.calendar_events.pop(event_id, None) is not None

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
            return ok(argv, json.dumps({"text": json.dumps(self.answer(prompt))}))
        if argv[1:3] == ["infer", "image"] and argv[3] in ("generate", "edit"):
            output = Path(flag(argv, "--output"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(PNG)
            self.renders.append(argv)
            return ok(argv, json.dumps({"ok": True, "outputs": [{"path": str(output)}]}))
        if argv[1:4] == ["infer", "image", "describe"]:
            ids = re.findall(r"^- (\w+):", flag(argv, "--prompt") or "", re.MULTILINE)
            text = json.dumps({"answers": {i: "yes" for i in ids}, "notes": {}})
            return ok(argv, json.dumps({"ok": True, "outputs": [{"text": text}]}))
        if argv[1:3] == ["cron", "list"]:
            return ok(argv, json.dumps({"jobs": []}))
        if argv[1:3] == ["cron", "create"]:
            self.spawned.append(argv)
            return ok(argv, json.dumps({"id": "job-unexpected", "name": flag(argv, "--name")}))
        self.other.append(argv)
        return ok(argv, "{}")

    def _kolo(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        command = argv[1]
        if command == "request-approval":
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
            return ok(argv, "")
        if command == "notify-owner":
            self.notices.append({"text": flag(argv, "-m"), "file": flag(argv, "--file"),
                                 "session_key": flag(argv, "--session-key")})
            return ok(argv, "")
        if command == "audit-query":
            wanted = flag(argv, "--event-type")
            events = [e for e in self.events if not wanted or e["event_type"] == wanted]
            return ok(argv, json.dumps({"status": "ok", "events": events}))
        if command == "update-brief":
            self.updates.append((flag(argv, "--brief-id"), flag(argv, "--status")))
            return ok(argv, "")
        self.other.append(argv)
        return ok(argv, "")

    def reject(self, card: dict, note: str) -> None:
        self.events.insert(0, {"event_type": "brief.rejected", "brief_id": card["brief_id"], "brief_number": card["number"],
                               "description": card["title"], "created_at": datetime.now(timezone.utc).isoformat(),
                               "details": {"note": note}})

    # ---- The model, by contract ---------------------------------------
    def answer(self, prompt: str) -> dict:
        if "decide what the CUSTOMER messages are" in prompt:
            return {"kind": self.triage_kind, "note": "read by the fake"}
        if "merge every fact the customer actually stated" in prompt:
            return {"specification": dict(self.spec)}
        if "asking the customer only for the missing details" in prompt:
            return {"body": (
                "Hi Pat,\n\nThanks for writing about the signet ring; happy to price it. Two quick questions "
                "so the number is right:\n\n- What finger size should the ring be?\n- How would you like the "
                "small diamonds set along the shoulders, bead set or channel set?\n\nBest,\nKolo Jewelers"
            )}
        if "Classify ONLY the newest customer message" in prompt:
            return {"post_estimate_artifact": {"design_change_assessment": "unchanged",
                                               "intents": list(self.intents), "changed_fields": []}}
        if "copy the customer's own words about timing" in prompt:
            return {"requested_times": list(self.requested[0]), "resolved_times": list(self.requested[1])}
        if "A jewelry shop owner wrote when they could meet" in prompt:
            return {"requested_times": ["the owner's times"], "resolved_times": list(self.owner_times)}
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
        )

    def tick(self, ws: Path, world: World) -> dict:
        summary = inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t",
                                     judge_runner=world.run)
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
        with patch("sys.stdout", io.StringIO()) as stdout:
            code = workflow_safe.main(parts[2:])
        printed = stdout.getvalue()
        self.assertEqual(code, 0, printed)
        return json.loads(printed)

    def answer(self, ws: Path, text: str) -> dict:
        with patch("sys.stdout", io.StringIO()) as stdout, patch("sys.stderr", io.StringIO()) as stderr:
            code = workflow_safe.main(["answer-question", "--workspace", str(ws), "--base-dir", str(ROOT), "--answer", text])
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

        # 3. The owner answers in words; the desk prices in place and files the brief.
        answered = self.answer(ws, "600")
        self.assertEqual(answered["value"], 600.0, answered)
        self.assertIsNone(answered.get("worker_job_id"), "pricing after a rate answer happens inline")
        self.assertEqual(answered.get("pipeline"), "approval_requested", answered)
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

        # 4. Approval: the one execute line sends the estimate.
        result = self.execute(ws, world, price_card["payload"]["execute"], price_card)
        self.assertEqual(result["outcome"], "estimate_sent", result)
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

    def _estimate_sent(self, ws: Path, world: World, spec: dict | None = None, rate: bool = True) -> tuple[str, str]:
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
        world.customer_message("s1", thread, "Please quote a 14k yellow gold signet ring, size 10, logo on the face, "
                               "small lab-grown diamonds G VS bead set on the shoulders.\n\nPat")
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
            self.assertIn(self.claim(ws, "s2")["status"], ("processed", "manual_review"))
            self.assertEqual(self.questions(ws, "open")[-1]["kind"], "appointment_next", "the new card's own question waits")
            self.assertEqual(self.tick(ws, world)["claimed"], 0, "nothing left in the queue")
        self.run_branch(branch)

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        def branch(ws: Path, world: World) -> None:
            _thread, estimate_id = self._estimate_sent(ws, world, rate=False, spec={
                "piece_type": "wedding band", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "7",
                "dimensions": "4mm wide", "finish": "brushed", "notes": "plain band, no stones",
            })
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
            self.assertEqual(world.sent, [])
            self.assertEqual(self.record(ws, self.only_estimate(ws))["status"], "pending_approval")
            self.tick(ws, world)
            self.assertEqual(len([n for n in world.notices if not n["file"]]), 1, "said once")
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

    def test_worker_prompt_files_a_card_and_never_sends(self) -> None:
        import cron_config

        post = cron_config.render_worker_message(Path("/ws"), ROOT, "abcdef0123456789", "jed-0123456789abcdef", "/ws/estimate-desk/work/x", branch="post_estimate") \
            if "branch" in cron_config.render_worker_message.__code__.co_varnames else (ROOT / "templates" / "worker-post-estimate.txt").read_text(encoding="utf-8")
        self.assertIn("request-rendering-approval", post)
        self.assertNotIn("needs no new approval", post)
        self.assertIn("never email the customer", post)
        self.assertIn("Never run `send-rendering`", post)
        # The card-filing command the prompt names parses and dispatches.
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as err:
            code = workflow_safe.main([
                "request-rendering-approval", "--monitor-root", "/nope/inbox-monitor", "--claim-root", "/nope/claims",
                "--record-root", "/nope/records", "--shop-profile", "/nope/profile.json", "--message-id", "m9",
                "--estimate-id", "jed-0123456789abcdef", "--checker", "worker",
            ])
        self.assertNotEqual(code, 0)
        self.assertNotIn("unknown command", err.getvalue())
        self.assertNotIn("invalid choice", err.getvalue())


class WindowGateTests(SideBranchTests):
    """A time outside the owner's declared windows never reaches a card or the calendar."""

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # inherited; runs once in the parent
        pass

    def test_requested_time_taken_offers_times_near_it(self) -> None:  # inherited; runs once in SideBranchTests
        pass

    def test_no_time_given_offers_a_tight_spread(self) -> None:
        pass

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
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

    def test_calendar_failure_asks_the_owner_instead_of_filing_an_empty_card(self) -> None:
        pass

    def test_plain_band_without_stones_is_priced_without_a_stone_question(self) -> None:
        pass

    def test_vendor_mail_closes_without_a_word_to_the_owner(self) -> None:
        pass

    def test_rejected_price_card_tells_the_owner_once_and_sends_nothing(self) -> None:
        pass

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
            self.assertEqual(answered.get("pipeline"), "approval_requested", answered)
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
            self.assertEqual(answered.get("pipeline"), "followup_sent", answered)
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
