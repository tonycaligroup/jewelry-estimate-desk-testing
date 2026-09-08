"""Every live defect under tests/fixtures/live replayed on the real code (see the README there)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reading_check  # noqa: E402
import test_golden_path  # noqa: E402

FIXTURES = sorted((Path(__file__).resolve().parent / "fixtures" / "live").glob("*.json"))


def _digest(messages: list[str], shop: str = "") -> dict:
    out = [{"body": body, "sent_by": "customer"} for body in messages]
    if shop:
        out.append({"body": shop, "sent_by": "shop"})
    return {"messages": out}


class LiveFixtureTests(unittest.TestCase):
    def test_fixtures_exist_and_are_well_formed(self) -> None:
        self.assertTrue(FIXTURES, "no live fixtures found")
        for path in FIXTURES:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("defect", "seen_on", "fixed_in", "text_is", "kind"):
                self.assertIn(key, data, f"{path.name} lacks {key}")
            self.assertIn(data["kind"], ("reading_check", "golden_path"), path.name)
            self.assertIn(data["text_is"], ("verbatim", "paraphrase"), path.name)

    def test_reading_check_fixtures(self) -> None:
        for path in FIXTURES:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["kind"] != "reading_check":
                continue
            with self.subTest(fixture=path.name):
                found = reading_check.compare(_digest(data["messages"], data.get("shop", "")), data["specification"])
                self.assertEqual([d["topic"] for d in found], data["expect"]["topics"], data["defect"])

    def test_golden_path_fixtures(self) -> None:
        for path in FIXTURES:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["kind"] != "golden_path":
                continue
            with self.subTest(fixture=path.name):
                self._replay(data)

    def _replay(self, data: dict) -> None:
        # The golden-path helpers (workspace, fakes, ticks, the exact execute
        # lines) are borrowed from the suite's own test class by composition,
        # so no inherited test runs twice.
        helper = test_golden_path.SideBranchTests("test_one_customer_from_inquiry_to_reschedule")
        outer = self

        def branch(ws: Path, world) -> None:
            helper._profile_with_rates(ws)
            overrides = data.get("profile") or {}
            if overrides:
                profile_path = ws / "estimate-desk" / "shop-profile.json"
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                for key, value in overrides.items():
                    profile["pricing"].setdefault(key, {}).update(value) if isinstance(value, dict) else profile["pricing"].__setitem__(key, value)
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
            thread = estimate_id = None
            for number, step in enumerate(data["steps"], 1):
                summary = None
                prompts_before = len(world.prompts)
                if "estimate_sent" in step:
                    thread, estimate_id = helper._estimate_sent(ws, world, spec=step["estimate_sent"]["spec"],
                                                                text=step["estimate_sent"].get("text"))
                elif "customer" in step:
                    if "spec" in step:
                        world.spec = step["spec"]
                    world.design_change = list(step["customer"].get("design_change") or [])
                    cards_before = len(world.cards)
                    if step["customer"].get("thread") == "new":
                        thread = f"thread-{step['customer']['id']}"
                    world.customer_message(step["customer"]["id"], thread or "thread-fixture", step["customer"]["text"],
                                           subject=step["customer"].get("subject") or "Custom signet ring")
                    summary = helper.tick(ws, world)
                    world.design_change = []
                    if step.get("expect", {}).get("no_new_card"):
                        outer.assertEqual(len(world.cards), cards_before, f"step {number}: a card was filed")
                elif "answer" in step:
                    answered = helper.answer(ws, step["answer"])
                    if "decision" in step.get("expect", {}):
                        outer.assertEqual(answered["decision"], step["expect"]["decision"], answered)
                elif step.get("tick"):
                    if "spec" in step:
                        world.spec = step["spec"]
                    world.design_change = list(step.get("design_change") or [])
                    summary = helper.tick(ws, world)
                    world.design_change = []
                expect = step.get("expect") or {}
                if estimate_id is None and (ws / "estimate-desk" / "records").exists():
                    records = sorted((ws / "estimate-desk" / "records").glob("*.json"))
                    estimate_id = records[0].stem if len(records) == 1 else None
                if "outcomes" in expect:
                    outer.assertEqual([i["outcome"] for i in summary["inline"]], expect["outcomes"], f"step {number}: {summary}")
                if "sent" in expect:
                    outer.assertEqual(len(world.sent), expect["sent"], f"step {number}: emails sent")
                record = helper.record(ws, estimate_id) if estimate_id else {}
                if "claim_status" in expect:
                    latest = step.get("customer", {}).get("id") or next(k for k in reversed(list(world.messages)) if not k.startswith("sent-"))
                    outer.assertEqual(helper.claim(ws, latest)["status"], expect["claim_status"], f"step {number}")
                if "record_thread" in expect:
                    outer.assertEqual(record["route"]["thread_id"], expect["record_thread"], f"step {number}")
                if "missing_required_fields" in expect:
                    outer.assertEqual(record.get("missing_required_fields"), expect["missing_required_fields"], f"step {number}")
                if "pieces" in expect:
                    outer.assertEqual(len(record["specification"]["pieces"]), expect["pieces"], f"step {number}")
                if "piece" in expect:
                    piece = record["specification"]["pieces"][expect["piece"]["index"]]
                    for key, value in expect["piece"]["has"].items():
                        outer.assertEqual(piece.get(key), value, f"step {number}: piece {expect['piece']['index']} lacks {key}")
                title = world.cards[-1]["title"] if world.cards else ""
                for words in expect.get("card_title_contains", []):
                    outer.assertIn(words, title, f"step {number}")
                assumptions = title.split("Assumptions: ")[1] if "Assumptions: " in title else ""
                for words in expect.get("assumptions_contain", []):
                    outer.assertIn(words, assumptions, f"step {number}: {assumptions}")
                if "model_quantity_prompts" in expect:
                    asked = [pr for pr in world.prompts[prompts_before:] if "PIECES TO QUANTIFY:" in pr]
                    outer.assertEqual(len(asked), expect["model_quantity_prompts"], f"step {number}: the model was asked about pieces")
                for words, count in (expect.get("assumptions_count") or {}).items():
                    outer.assertEqual(assumptions.count(words), count, f"step {number}: {assumptions}")

        helper.setUp()
        try:
            helper.run_branch(branch)
        finally:
            helper.tearDown()


if __name__ == "__main__":
    unittest.main()
