#!/usr/bin/env python3
"""Fault injection over the golden path: the three promises under every failure.

RELIABILITY-PLAN.md section 3.6. The golden path is replayed as a list of
actions (a customer email plus a tick, an execute line, an owner answer).
For every action, every external service it touches, and every fault mode
(fail once, fail twice, crash right after the effect), the scenario is run
fresh with the fault armed at that action, the action is retried the way
the main session would (the same line again, the next tick), and then the
promises are checked against the fakes:

1. never twice: no customer email, calendar event, or card duplicated;
2. never silent: the action reached its end state, or the owner has exactly
   one open question about it;
3. the owner heard nothing else.

Combinations that fail today are listed in KNOWN_GAPS with the plan step
that removes them. A gap that starts passing must be removed from the list
(the test fails on it), so the list stays honest as the plan lands.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import inbox_claim  # noqa: E402
import inbox_watcher  # noqa: E402
import judge  # noqa: E402
import workflow_safe  # noqa: E402
from test_golden_path import Crash, GoldenPathTests, World, local_key, next_weekday  # noqa: E402

from datetime import datetime as _datetime, timedelta as _timedelta  # noqa: E402


class _LaterClock(_datetime):
    """The claim journal's clock, one hour on: every lease has lapsed."""

    @classmethod
    def now(cls, tz=None):
        return _datetime.now(tz) + _timedelta(hours=1)


MODES = ("fail_once", "fail_twice", "crash_after")
RETRIES = 3


class Action:
    """One step of the path: what the world does, how the desk is driven, what must be true after."""

    def __init__(self, name: str, arrange, drive, expect) -> None:
        self.name, self.arrange, self.drive, self.expect = name, arrange, drive, expect


class Harness(GoldenPathTests):
    """Drives the desk without the golden path's own assertions, so faults can surface."""

    def raw_tick(self, ws: Path, world: World) -> dict:
        return inbox_watcher.tick(ws, ROOT, "kolo:test-owner", "openclaw", runner=world.run, token="t",
                                  judge_runner=world.run)

    def raw_execute(self, ws: Path, world: World, card: dict) -> tuple[int, str]:
        parts = shlex.split(card["payload"]["execute"].replace("<Brief ID>", card["brief_id"]))
        with patch("sys.stdout", io.StringIO()) as out, patch("sys.stderr", io.StringIO()) as err:
            code = workflow_safe.main(parts[2:])
        return code, out.getvalue() + err.getvalue()

    def raw_answer(self, ws: Path, text: str) -> tuple[int, str]:
        with patch("sys.stdout", io.StringIO()) as out, patch("sys.stderr", io.StringIO()) as err:
            code = workflow_safe.main(["answer-question", "--workspace", str(ws), "--base-dir", str(ROOT), "--answer", text])
        return code, out.getvalue() + err.getvalue()

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # the parent's test is not repeated here
        pass

    # ---- the golden path as actions -------------------------------------
    def actions(self) -> list[Action]:
        thread = "thread-fi"
        slot = next_weekday(2, 14, 0)

        def card_of(world: World, kind: str) -> dict:
            return next(c for c in reversed(world.cards) if c["kind"] == kind)

        def tick(ws, world):
            self.raw_tick(ws, world)

        return [
            Action(
                "inquiry -> follow-up",
                lambda ws, world: (
                    setattr(world, "spec", {
                        "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                        "engraving": "our company logo on the face",
                        "accent_stones": "a few small lab-grown diamonds along the shoulders",
                        "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
                    }),
                    world.customer_message("m1", thread, "A signet ring with our logo and small lab-grown diamonds please.\n\nPat"),
                ),
                tick,
                lambda ws, world: len(world.sent) == 1 and self.claim(ws, "m1")["status"] == "processed",
            ),
            Action(
                "reply -> rate question",
                lambda ws, world: (
                    world.spec.update({"finger_size": "10", "setting_style": "bead set"}),
                    world.customer_message("m2", thread, "Size 10, bead set.\n\nPat", attachments=("logo.png",)),
                ),
                tick,
                lambda ws, world: self.claim(ws, "m2")["status"] == "awaiting_owner"
                and len([q for q in self.questions(ws, "open") if q["kind"] == "missing_rate"]) == 1,
            ),
            Action(
                "owner answers the rate -> price card",
                lambda ws, world: None,
                lambda ws, world: self.raw_answer(ws, "600"),
                lambda ws, world: self.record(ws, self.only_estimate(ws))["status"] == "pending_approval"
                and any(c["kind"] == "price_approval" or "send-approved-estimate-brief" in c["payload"].get("execute", "") for c in world.cards),
            ),
            Action(
                "approve the price -> estimate sent",
                lambda ws, world: None,
                lambda ws, world: self.raw_execute(ws, world, card_of(world, world.cards[-1]["kind"])),
                lambda ws, world: self.record(ws, self.only_estimate(ws))["status"] == "estimate_sent" and len(world.sent) == 2,
            ),
            Action(
                "rendering request -> rendering card",
                lambda ws, world: (
                    setattr(world, "intents", ["rendering_request"]),
                    world.customer_message("m3", thread, "Could you send a rendering?\n\nPat"),
                ),
                tick,
                lambda ws, world: any(c["kind"] == "send_rendering" for c in world.cards)
                and self.claim(ws, "m3")["status"] == "awaiting_owner",
            ),
            Action(
                "approve the renderings -> sent",
                lambda ws, world: None,
                lambda ws, world: self.raw_execute(ws, world, card_of(world, "send_rendering")),
                lambda ws, world: len(world.sent) == 3 and len(world.sent[2]["attachments"]) == 2
                and self.claim(ws, "m3")["status"] == "processed",
            ),
            Action(
                "meeting request -> booking card",
                lambda ws, world: (
                    setattr(world, "intents", ["appointment_request"]),
                    setattr(world, "requested", ([f"{slot.strftime('%A')} at 2"], [local_key(slot)])),
                    world.customer_message("m4", thread, f"Can we meet {slot.strftime('%A')} at 2?\n\nPat"),
                ),
                tick,
                lambda ws, world: any(c["kind"] == "appointment_booking" for c in world.cards)
                and self.claim(ws, "m4")["status"] == "processed",
            ),
            Action(
                "approve the booking -> booked and confirmed",
                lambda ws, world: None,
                lambda ws, world: self.raw_execute(ws, world, card_of(world, "appointment_booking")),
                lambda ws, world: len(world.calendar_events) == 1
                and self.record(ws, self.only_estimate(ws))["status"] == "appointment_booked" and len(world.sent) == 4,
            ),
        ]

    # ---- running one combination ---------------------------------------
    def run_clean_until(self, ws: Path, world: World, actions: list[Action], stop: int) -> None:
        for action in actions[:stop]:
            action.arrange(ws, world)
            action.drive(ws, world)
            self.assertTrue(action.expect(ws, world), f"clean run broke at {action.name}")

    def services_touched(self, actions: list[Action], index: int) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            ws, world = self.workspace(directory)
            patches = self.patched(world)
            for p in patches:
                p.start()
            try:
                self.run_clean_until(ws, world, actions, index)
                world.calls = []
                actions[index].arrange(ws, world)
                actions[index].drive(ws, world)
                touched = list(dict.fromkeys(world.calls))
            finally:
                for p in patches:
                    p.stop()
        return touched

    def drive_with_faults_tolerated(self, action: Action, ws: Path, world: World) -> str | None:
        """Run the action the way the world would: exceptions are what the operator sees."""
        try:
            result = action.drive(ws, world)
        except Crash as crash:
            return f"crash: {crash}"
        except (OSError, ValueError, subprocess.CalledProcessError, judge.JudgmentError, json.JSONDecodeError) as exc:
            return f"error: {exc}"
        if isinstance(result, tuple) and result[0] != 0:
            return f"exit {result[0]}: {result[1].strip()[:160]}"
        return None

    def promises(self, world: World, questions_before: int, previews_before: int) -> list[str]:
        broken: list[str] = []
        bodies = Counter(m["body"] for m in world.sent)
        if any(n > 1 for n in bodies.values()):
            broken.append("a customer email was sent twice")
        starts = Counter(e["start"]["dateTime"] for e in world.created_events)
        if any(n > 1 for n in starts.values()):
            broken.append("a calendar event was created twice for the same time")
        keys = Counter(json.dumps(c["payload"], sort_keys=True) for c in world.cards)
        if any(n > 1 for n in keys.values()):
            broken.append("the same card was filed twice")
        questions = [n for n in world.notices if not n["file"] and "desk-answer" in (n["text"] or "")]
        others = [n for n in world.notices if not n["file"] and "desk-answer" not in (n["text"] or "")]
        if len(questions) - questions_before > 1:
            broken.append("the owner was asked more than one question")
        if others:
            broken.append("the owner heard something that is neither a question nor a preview: " + others[0]["text"][:80])
        return broken

    def run_combination(self, actions: list[Action], index: int, service: str, mode: str) -> str:
        """'ok', 'recovered', or a description of the gap."""
        with tempfile.TemporaryDirectory() as directory:
            ws, world = self.workspace(directory)
            patches = self.patched(world)
            for p in patches:
                p.start()
            try:
                self.run_clean_until(ws, world, actions, index)
                action = actions[index]
                questions_before = len([n for n in world.notices if not n["file"] and "desk-answer" in (n["text"] or "")])
                previews_before = len([n for n in world.notices if n["file"]])
                action.arrange(ws, world)
                if mode == "crash_after":
                    world.crash_after.add(service)
                else:
                    world.fail_next[service] = 1 if mode == "fail_once" else 2
                first = self.drive_with_faults_tolerated(action, ws, world)
                world.fail_next.clear()
                world.crash_after.clear()
                outcome = "ok" if first is None else None
                if not action.expect(ws, world):
                    # Recover the way the operator would: the same thing again.
                    last = first
                    # Time passes between retries on the pod: a crashed tick's
                    # lease lapses and the stale reconciler gets its one resume.
                    with patch.object(inbox_watcher, "STALE_AFTER_SECONDS", 1), patch.object(inbox_claim, "datetime", _LaterClock):
                        for _ in range(RETRIES):
                            last = self.drive_with_faults_tolerated(action, ws, world)
                            if action.expect(ws, world):
                                outcome = "recovered"
                                break
                    if outcome is None:
                        questions_now = len([n for n in world.notices if not n["file"] and "desk-answer" in (n["text"] or "")])
                        if questions_now == questions_before + 1:
                            outcome = "asked"
                        else:
                            outcome = f"stuck silently (first: {first}; last: {last})"
                elif outcome is None:
                    outcome = "recovered"
                broken = self.promises(world, questions_before, previews_before)
                if broken:
                    outcome = f"{outcome}; broken: " + "; ".join(broken)
            finally:
                for p in patches:
                    p.stop()
        return outcome


# Combinations that fail on today's code, with the plan step that removes them.
# Keyed by (action index, service, mode). The value is the plan step.
KNOWN_GAPS: dict[tuple[int, str, str], str] = {}


class FaultInjectionTests(Harness):
    def test_every_external_failure_keeps_the_three_promises(self) -> None:
        actions = self.actions()
        report: dict[tuple[int, str, str], str] = {}
        for index in range(len(actions)):
            for service in self.services_touched(actions, index):
                for mode in MODES:
                    report[(index, service, mode)] = self.run_combination(actions, index, service, mode)
        failures = {k: v for k, v in report.items() if v not in ("ok", "recovered", "asked")}
        unexpected = {k: v for k, v in failures.items() if k not in KNOWN_GAPS}
        healed = {k for k in KNOWN_GAPS if k not in failures}
        lines = [f"{actions[k[0]].name} | {k[1]} | {k[2]} -> {v}" for k, v in sorted(report.items())]
        (Path(tempfile.gettempdir()) / "jed-fault-injection-report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        message = "\n".join(f"{actions[k[0]].name} | {k[1]} | {k[2]} -> {v}" for k, v in sorted(unexpected.items()))
        self.assertEqual(unexpected, {}, "combinations not in KNOWN_GAPS failed:\n" + message)
        self.assertEqual(healed, set(), "known gaps now pass; remove them from KNOWN_GAPS: " + str(sorted(healed)))


if __name__ == "__main__":
    unittest.main()
