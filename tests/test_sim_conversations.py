#!/usr/bin/env python3
"""Stress-test scenarios for the Jewelry Estimate Desk, run on the real code.

Each test class is a theme (first emails, partial replies, post-estimate
changes, scheduling picks, non-inquiry mail, renderings/photos, quoted
text, and concurrency). Every scenario asserts what WORKFLOW.md says
should happen, not what the code currently does; failing scenarios are
where the fix belongs, so this file passes as a whole.

Built on the same fakes as tests/test_golden_path.py: only Gmail, Kolo,
the calendar, and the model are faked; every other line is the real
skill. See that file's World class and SideBranchTests helpers for the
contract.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import test_golden_path
from test_golden_path import World, local_key, next_weekday

import estimate_record

# Subclassed via the module attribute (test_golden_path.GoldenPathTests), not
# a bare imported name: unittest's loader picks up every TestCase subclass
# bound as a top-level name in this module, and a plain
# `from test_golden_path import GoldenPathTests` (or even aliasing it to a
# module-level name here) would make it re-discover and re-run the golden
# path itself under this module's name.


class SimBase(test_golden_path.GoldenPathTests):
    """Shared scaffolding for every scenario class in this file.

    Copies the small helpers SideBranchTests defines (rather than
    inheriting its test_ methods, which would re-run the whole golden
    path and every side branch once per theme class in this file).
    """

    def test_one_customer_from_inquiry_to_reschedule(self) -> None:  # inherited; must not re-run here
        pass

    def _profile_with_rates(self, ws: Path) -> None:
        profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
        profile["pricing"]["stones_per_carat"]["lab_grown_diamond"] = 900.0
        profile["pricing"]["typical_finished_weights"].update({"engagement ring": 5.0, "wedding band": 4.0})
        (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")

    def _estimate_sent(self, ws: Path, world: World, spec: dict | None = None, rate: bool = True,
                       text: str | None = None, thread: str = "thread-side") -> tuple[str, str]:
        """A complete inquiry priced and sent: the starting point for post-estimate branches."""
        if rate:
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
        world.spec = spec or {
            "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
            "setting_style": "bead set", "engraving": "our logo on the face",
            "accent_stones": "small lab-grown diamonds along the shoulders",
            "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
        }
        world.customer_message("s1", thread, text or (
            "Please quote a 14k yellow gold signet ring, size 10, logo on the face, "
            "small lab-grown diamonds G VS bead set on the shoulders.\n\nPat"
        ))
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


# ---------------------------------------------------------------------------
# 1. First emails: typos, mixed facts, budget with no piece, multiple pieces
# ---------------------------------------------------------------------------

class FirstMessageReadingTests(SimBase):

    def test_typo_laden_mixed_facts_first_email_still_prices(self) -> None:
        """WORKFLOW.md 6.1/6.3: the reading (faked here) is what matters; typos in the
        customer's prose must not stop a complete specification from pricing."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k", "finger_size": "10",
                "setting_style": "bead set", "engraving": "our logo on the face",
                "accent_stones": "small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("t1", "thread-typo", (
                "hii i want a custom singet ring 14k yelow gold sz 10 wit are compny logo on teh face and "
                "smal lab grown diamonds G vs along teh shouldres, bead set. can u qoute pls\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            self.assertEqual(self.record(ws, self.only_estimate(ws))["missing_required_fields"], [])
        self.run_branch(branch)

    def test_budget_with_no_piece_named_asks_for_the_piece_not_a_price(self) -> None:
        """WORKFLOW.md 6.2: budget is never a prerequisite and never a substitute for the
        piece; the gate must ask for piece_type, and no price-free ballpark may leak."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {"budget": "$3,000 to $5,000"}
            world.customer_message("b1", "thread-budget", (
                "Hi, my budget is somewhere between $3,000 and $5,000. What can you make me for that?\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertIn("piece_type", self.record(ws, self.only_estimate(ws))["missing_required_fields"])
            self.assertEqual(len(world.sent), 1)
            self.assertNotIn("$", world.sent[0]["body"], "no ballpark price for a budget alone (WORKFLOW 6.2)")
        self.run_branch(branch)

    def test_two_pieces_named_in_one_first_email(self) -> None:
        """WORKFLOW.md 6.1: 'piece type and quantity' is read per object; two pieces in one
        message price as two pieces on one card, not one merged or one dropped."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {
                "metal": "yellow gold", "metal_karat": "14k", "stone_type": "diamond", "stone_origin": "lab-grown",
                "pieces": [
                    {"piece_type": "engagement ring", "finger_size": "6", "setting_style": "solitaire",
                     "stone_carat": "1.0", "stone_cut": "round", "center_stone": "yes"},
                    {"piece_type": "wedding band", "finger_size": "6", "setting_style": "channel set",
                     "accent_stones": "small lab-grown diamonds all around", "center_stone": "no"},
                ],
            }
            world.customer_message("p1", "thread-two-pieces", (
                "Hi, I'd like a quote for a matching set: a 14k yellow gold engagement ring, size 6, solitaire, "
                "1 carat lab-grown diamond, and a wedding band, size 6, channel set with small lab-grown diamonds "
                "all around.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(record["missing_required_fields"], [])
            pieces = estimate_record.pieces_of(record["specification"])
            self.assertEqual(len(pieces), 2, pieces)
            card = world.cards[-1]
            self.assertIn("engagement ring", card["title"].lower() + json.dumps(card["details"]).lower())
            self.assertIn("wedding band", card["title"].lower() + json.dumps(card["details"]).lower())
        self.run_branch(branch)

    def test_three_pieces_named_in_one_first_email(self) -> None:
        """Same rule as the two-piece case, extended to three: WORKFLOW.md 6.1 sets no cap
        on how many objects one message can name."""
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec = {
                "metal": "yellow gold", "metal_karat": "14k", "stone_type": "diamond", "stone_origin": "lab-grown",
                "pieces": [
                    {"piece_type": "wedding band", "finger_size": "6", "setting_style": "channel set",
                     "accent_stones": "small lab-grown diamonds all around", "center_stone": "no"},
                    {"piece_type": "wedding band", "finger_size": "9", "setting_style": "plain",
                     "center_stone": "no"},
                    {"piece_type": "pendant", "setting_style": "bezel",
                     "stone_carat": "0.5", "stone_cut": "round", "center_stone": "yes"},
                ],
            }
            world.customer_message("p2", "thread-three-pieces", (
                "Hi, could you quote his and hers wedding bands (sizes 6 and 9, both 14k yellow gold, hers "
                "channel set with small lab-grown diamonds all around, his plain) and a bezel pendant with a "
                "0.5 carat lab-grown diamond, also 14k yellow gold.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, self.only_estimate(ws))
            pieces = estimate_record.pieces_of(record["specification"])
            self.assertEqual(len(pieces), 3, pieces)
            self.assertEqual(record["missing_required_fields"], [])
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 2. Partial replies: some questions answered, "I don't know" / "you decide"
# ---------------------------------------------------------------------------

class PartialReplyTests(SimBase):

    def test_reply_answers_only_some_of_the_questions(self) -> None:
        """WORKFLOW.md 6.2: 'After one partial reply we may ask once more, only for
        load-bearing gaps.' The second ask must not repeat the field already answered."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("q1", "thread-partial", (
                "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face "
                "and a few small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me "
                "an estimate?\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(sorted(self.record(ws, self.only_estimate(ws))["missing_required_fields"]),
                             ["finger_size", "setting_style"])

            world.spec.update({"finger_size": "10"})  # setting_style still unanswered
            world.customer_message("q2", "thread-partial", "Size 10 please.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(len(world.sent), 2, "a second, narrower ask goes out")
            self.assertIn("?", world.sent[1]["body"])
            # The email text itself is the fake model's canned copy (see World.answer),
            # so the real, code-level guarantee this checks is the record's own
            # narrowed list of what is still missing, not the drafted wording.
            self.assertEqual(self.record(ws, self.only_estimate(ws))["missing_required_fields"], ["setting_style"])
        self.run_branch(branch)

    def test_second_partial_reply_on_the_same_field_goes_to_the_owner(self) -> None:
        """WORKFLOW.md 6.2: 'After that, the decision goes to the owner.' A third message
        that still leaves the same field open must stop sending the customer anything and
        ask the owner instead."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("s1", "thread-stall", (
                "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face "
                "and a few small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me "
                "an estimate?\n\nPat"
            ))
            self.tick(ws, world)
            world.spec.update({"finger_size": "10"})
            world.customer_message("s2", "thread-stall", "Size 10 please.\n\nPat")
            self.tick(ws, world)
            self.assertEqual(len(world.sent), 2)
            # Still no setting_style: the customer does not answer it a second time either.
            world.customer_message("s3", "thread-stall", "Hmm, tricky one. Let me think it over and get back to you.\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["awaiting_owner"], summary)
            self.assertEqual(len(world.sent), 2, "nothing more goes to the customer once it stalls")
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertRegex(asked[0]["text"], r"(?i)setting")
        self.run_branch(branch)

    def test_i_dont_know_you_decide_becomes_the_jewelers_choice(self) -> None:
        """RELEASE-PLAN-4.15.md section 4: 'the customer's own words decide ... "I don't
        know"'. A customer who leaves the last ask to the jeweler must not be asked again,
        and the price card must show it as an assumption, not a stall."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("d1", "thread-idk", (
                "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face "
                "and a few small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me "
                "an estimate?\n\nPat"
            ))
            self.tick(ws, world)
            self.assertEqual(sorted(self.record(ws, self.only_estimate(ws))["missing_required_fields"]),
                             ["finger_size", "setting_style"])
            profile = json.loads((ws / "estimate-desk" / "shop-profile.json").read_text(encoding="utf-8"))
            profile["pricing"]["stones_per_carat"]["lab_grown_diamond_melee"] = 600.0
            (ws / "estimate-desk" / "shop-profile.json").write_text(json.dumps(profile), encoding="utf-8")
            world.spec.update({"finger_size": "10"})  # setting_style deliberately left off the reading
            world.customer_message("d2", "thread-idk", (
                "Size 10. I don't know about the setting style, you decide whatever looks best.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, self.only_estimate(ws))
            self.assertEqual(record["missing_required_fields"], [])
            self.assertEqual(record["specification"].get("setting_style"), "jeweler's choice")
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 3. After the estimate: design changes, second pieces, thanks/acceptance
# ---------------------------------------------------------------------------

class PostEstimateTests(SimBase):

    def test_make_it_18k_instead_returns_to_the_gate_and_pricing(self) -> None:
        """WORKFLOW.md 6.6: 'A design change | Treat it as a changed specification: it
        returns to the gate and pricing, and the owner reviews it.'"""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.design_change = ["metal_karat"]
            world.customer_message("c1", thread, "Actually, could you make it 18k instead of 14k?\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["post_estimate_finished"], summary)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertIn("desk-answer", asked[0]["text"])
            answered = self.answer(ws, "change")
            self.assertEqual(answered["decision"], "design_change", answered)
            world.spec["metal_karat"] = "18k"
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["specification"]["metal_karat"], "18k")
            self.assertEqual(record["status"], "pending_approval")
        self.run_branch(branch)

    def test_second_piece_after_the_estimate_keeps_the_firsts_facts(self) -> None:
        """WORKFLOW.md 6.6 plus tests/fixtures/live/2026-09-07-second-piece-shared-facts.json:
        a matching second piece must not lose the first piece's already-priced facts."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.design_change = ["pieces"]
            world.customer_message("c2", thread, "Could you also quote a plain matching band, size 10, same stones?\n\nPat")
            self.tick(ws, world)
            answered = self.answer(ws, "second piece")
            self.assertEqual(answered["decision"], "second_piece", answered)
            world.spec = {
                "metal": "yellow gold", "metal_karat": "14k", "stone_type": "diamond", "stone_origin": "lab-grown",
                "stone_color": "G", "stone_clarity": "VS",
                "pieces": [
                    {"piece_type": "signet ring", "finger_size": "10"},
                    {"piece_type": "wedding band", "finger_size": "10", "setting_style": "channel set",
                     "accent_stones": "small lab-grown diamonds all around", "center_stone": "no"},
                ],
            }
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            pieces = estimate_record.pieces_of(record["specification"])
            self.assertEqual(len(pieces), 2, pieces)
            self.assertEqual(pieces[0].get("setting_style"), "bead set", "the first piece keeps its priced facts")
            self.assertEqual(pieces[0].get("engraving"), "our logo on the face")
        self.run_branch(branch)

    @unittest.expectedFailure  # defect: no owner alert for a thanks-only post-estimate reply
    def test_thanks_only_reply_after_the_estimate_alerts_the_owner(self) -> None:
        """WORKFLOW.md 6.6, closing line: 'Every customer reply also raises a "customer
        replied" alert to the owner so nothing sits unseen.' A plain thank-you after the
        estimate must still surface to the owner in some form.

        Actual: scripts/workflow_safe.py:finalize_post_estimate treats an
        assessment of "unchanged" with no actionable intents as fully done —
        it calls finish_processed() and returns with no card and no notice
        (confirmed by tests/test_runtime.py
        test_finalize_post_estimate_settles_acknowledgement, which asserts
        exactly this and is not touched by this scenario). The reply is
        filed silently; the owner never hears the thread moved.

        Fix belongs in scripts/workflow_safe.py:finalize_post_estimate
        (around the `if not actionable:` branch, ~line 3074): when the
        classification is "post_estimate_continuation" with no actionable
        intents, call kolo_safe.notify_owner with a one-line "customer
        replied" note (piece, a snippet of the reply) before finish_processed,
        the same way ownership_confirmed already declines to ping but this
        path is the terminal one for the thread and must not go silent.
        """
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.customer_message("th1", thread, "Thank you so much, this all looks wonderful!\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["post_estimate_finished"], summary)
            asked = [n for n in world.notices if not n["file"]]
            self.assertGreaterEqual(len(asked), 1, "the owner must hear that the customer replied")
        self.run_branch(branch)

    def test_estimate_acceptance_alerts_the_owner(self) -> None:
        """WORKFLOW.md 6.6: 'Acceptance, or "let's do it" | Alert the owner. No further
        price step is needed.'

        Actual: judge.classify_reply can return intents=["estimate_acceptance"],
        but scripts/workflow_safe.py:finalize_post_estimate only treats
        {"rendering_request", "appointment_request"} as actionable
        (`actionable = set(intents) & {"rendering_request", "appointment_request"}`,
        ~line 3074). estimate_acceptance is silently dropped from
        `actionable`, so an explicit "yes, let's move forward!" reply is
        filed with finish_processed() and no owner notice at all — the exact
        case WORKFLOW.md names by name.

        Fix belongs in scripts/workflow_safe.py:finalize_post_estimate: when
        "estimate_acceptance" is in intents, send a short notify-owner alert
        ("<customer> accepted the estimate for <piece>: '<snippet>'") before
        finish_processed, whether or not rendering/appointment intents are
        also present.
        """
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["estimate_acceptance"]
            world.customer_message("a1", thread, "This looks great, let's move forward!\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["post_estimate_finished"], summary)
            asked = [n for n in world.notices if not n["file"]]
            self.assertGreaterEqual(len(asked), 1, "WORKFLOW.md 6.6: acceptance must alert the owner")
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 4. Scheduling: picking an offered time, reschedules, cancellations
# ---------------------------------------------------------------------------

class SchedulingPickTests(SimBase):

    def _offer_two_times(self, ws: Path, world: World, thread: str, estimate_id: str) -> tuple:
        first_slot = next_weekday(2, 14, 0)
        second_slot = next_weekday(1, 10, 30, after=first_slot)
        world.owner_times = [local_key(first_slot), local_key(second_slot)]
        answered = self.answer(ws, f"Offer {first_slot.strftime('%A')} at 2 or {second_slot.strftime('%A')} at 10:30")
        self.assertEqual(answered["outcome"], "offer_card_filed", answered)
        card = world.cards[-1]
        self.execute(ws, world, card["payload"]["execute"], card)
        return first_slot, second_slot

    def test_pick_the_second_offered_time_by_ordinal(self) -> None:
        """WORKFLOW.md 6.7: the customer's own pick, whatever words they use, becomes a
        binary booking card for that time."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["appointment_request"]
            world.requested = (["sometime next week"], [])
            world.customer_message("o0", thread, "Could we set up a time to talk it over sometime next week?\n\nPat")
            self.tick(ws, world)
            first_slot, second_slot = self._offer_two_times(ws, world, thread, estimate_id)
            world.requested = (["the second one"], [local_key(second_slot)])
            world.customer_message("o1", thread, "The second one works for me.\n\nPat")
            summary = self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking", card["payload"])
            self.assertEqual(card["payload"]["calendar_availability"][0]["start"][:16], local_key(second_slot))
        self.run_branch(branch)

    def test_pick_by_day_name(self) -> None:
        """Same rule, picking by naming the day rather than an ordinal or a clock time."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["appointment_request"]
            world.requested = (["sometime next week"], [])
            world.customer_message("d0", thread, "Could we set up a time to talk it over sometime next week?\n\nPat")
            self.tick(ws, world)
            first_slot, second_slot = self._offer_two_times(ws, world, thread, estimate_id)
            day_name = second_slot.strftime("%A")
            world.requested = ([f"{day_name} works"], [local_key(second_slot)])
            world.customer_message("d1", thread, f"{day_name} works for me.\n\nPat")
            summary = self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking", card["payload"])
            self.assertEqual(card["payload"]["calendar_availability"][0]["start"][:16], local_key(second_slot))
        self.run_branch(branch)

    def test_pick_by_clock_time(self) -> None:
        """Same rule, picking by naming the clock time rather than the day."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.intents = ["appointment_request"]
            world.requested = (["sometime next week"], [])
            world.customer_message("k0", thread, "Could we set up a time to talk it over sometime next week?\n\nPat")
            self.tick(ws, world)
            first_slot, second_slot = self._offer_two_times(ws, world, thread, estimate_id)
            world.requested = (["2pm"], [local_key(first_slot)])
            world.customer_message("k1", thread, "2pm works for me.\n\nPat")
            summary = self.tick(ws, world)
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_booking", card["payload"])
            self.assertEqual(card["payload"]["calendar_availability"][0]["start"][:16], local_key(first_slot))
        self.run_branch(branch)

    def test_reschedule_with_an_existing_booking_moves_it(self) -> None:
        """WORKFLOW.md 6.7: moving an existing booking cancels the old event and books the new one."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            first_slot = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{first_slot.strftime('%A')} at 2"], [local_key(first_slot)])
            world.customer_message("r0", thread, f"Can we meet {first_slot.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(self.record(ws, estimate_id)["status"], "appointment_booked")

            second_slot = next_weekday(1, 11, 0, after=first_slot)
            world.requested = ([f"{second_slot.strftime('%A')} at 11"], [local_key(second_slot)])
            world.customer_message("r1", thread, f"Something came up. Could we do {second_slot.strftime('%A')} at 11 instead?\n\nPat")
            summary = self.tick(ws, world)
            move_card = world.cards[-1]
            self.execute(ws, world, move_card["payload"]["execute"], move_card)
            self.assertEqual(len(world.deleted_events), 1)
            self.assertEqual(self.record(ws, estimate_id)["appointment_booked"]["confirmed_start"][:16], local_key(second_slot))
        self.run_branch(branch)

    def test_reschedule_with_no_existing_booking_is_a_meeting_not_a_questionnaire(self) -> None:
        """tests/fixtures/live/2026-09-08-reschedule-is-a-meeting-not-a-questionnaire.json:
        reschedule wording on a thread with nothing booked yet must still be read as a
        meeting request, never as a fresh estimate questionnaire."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {"piece_type": "engagement ring"}
            world.customer_message("re1", "thread-no-booking-reschedule", (
                "Hello,\n\nSomething came up for Saturday, sorry about that.\n\n"
                "Any chance we could do Friday at 4pm instead?\n\nPat"
            ), subject="Re: Looking for an Engagement Ring")
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["appointment_approval_requested"], summary)
            self.assertEqual(len(world.sent), 0)
        self.run_branch(branch)

    def test_customer_cancels_the_appointment(self) -> None:
        """A customer who clearly cancels a booked meeting ("please cancel, I don't need
        it anymore") must have the calendar event released and the owner told, per
        WORKFLOW.md's general rule that the desk never leaves something it can read with
        confidence unhandled (6.6, 6.10) and non-negotiable 6 (meeting state must reflect
        reality, never a phantom booking left on the calendar).

        Actual: judge.INTENTS (scripts/judge.py) only has estimate_acceptance,
        rendering_request, appointment_request — there is no cancel/decline
        intent at all, and the classify_reply prompt's appointment_request
        description only covers "move or reschedule", not "cancel". A clean
        cancellation message reads as design_change_assessment "unchanged"
        with intents=[], which (as in the acceptance/thanks-only defects
        above) is treated as fully done by
        workflow_safe.finalize_post_estimate with no card, no notice, and
        the calendar_query event left standing — the customer is told
        nothing and the shop can still show up expecting them.

        Fix belongs in scripts/judge.py (add a "cancellation" intent to
        INTENTS and the classify_reply prompt) and
        scripts/workflow_safe.py:finalize_post_estimate (a new branch that,
        on a cancellation intent with record["appointment_booked"] present,
        deletes the calendar event via calendar_query.delete_event,
        clears/records the cancellation on the record, and notifies the
        owner) plus a customer acknowledgement email confirming the meeting
        was cancelled.
        """
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            first_slot = next_weekday(2, 14, 0)
            world.intents = ["appointment_request"]
            world.requested = ([f"{first_slot.strftime('%A')} at 2"], [local_key(first_slot)])
            world.customer_message("cn0", thread, f"Can we meet {first_slot.strftime('%A')} at 2?\n\nPat")
            self.tick(ws, world)
            card = world.cards[-1]
            self.execute(ws, world, card["payload"]["execute"], card)
            self.assertEqual(self.record(ws, estimate_id)["status"], "appointment_booked")

            world.intents = ["cancellation"]  # the reading's word for it (judge.INTENTS)
            world.customer_message("cn1", thread, (
                "Hi, please cancel our meeting -- I don't need it anymore, something came up "
                "and I won't be able to make it or reschedule for now. Thanks.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertNotEqual(world.deleted_events, [], "the booked event must be released on a clear cancellation")
            self.assertEqual(self.record(ws, estimate_id)["status"], "estimate_sent", "the booking is history")
            asked = [n for n in world.notices if not n["file"]]
            self.assertGreaterEqual(len(asked), 1, "the owner must be told the meeting was cancelled")
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 5. Mail that is not a plain custom-piece conversation
# ---------------------------------------------------------------------------

class NonInquiryMailTests(SimBase):

    def test_out_of_office_autoreply_closes_without_action(self) -> None:
        """WORKFLOW.md 6.9: 'Automatic replies (out of office): closed without action.'"""
        def branch(ws: Path, world: World) -> None:
            world.customer_message("oo1", "thread-ooo", (
                "I am currently out of the office with limited access to email and will respond "
                "when I return."
            ), subject="Automatic reply: Out of Office")
            summary = self.tick(ws, world)
            self.assertEqual(world.cards, [])
            self.assertEqual(world.notices, [])
            self.assertEqual(world.sent, [])
        self.run_branch(branch)

    def test_inventory_question_offers_a_visit_not_a_quote(self) -> None:
        """WORKFLOW.md 6.1: a ready-made/in-stock question is offered a visit through the
        appointment card, never a quote or a questionnaire."""
        def branch(ws: Path, world: World) -> None:
            world.triage_kind = "inventory_request"
            world.spec = {}
            world.customer_message("iv1", "thread-inventory", (
                "Hi, do you have anything in stock right now in tennis bracelets? Just curious what's available.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["appointment_approval_requested"], summary)
            self.assertEqual(len(world.sent), 0, "nothing is emailed before the owner approves the visit offer")
            card = world.cards[-1]
            self.assertEqual(card["kind"], "appointment_offer")
        self.run_branch(branch)

    def test_appraisal_request_goes_to_the_owner_never_priced(self) -> None:
        """WORKFLOW.md 6.1: 'An appraisal or insurance value | Stop; tell the owner; never
        value property.'"""
        def branch(ws: Path, world: World) -> None:
            world.triage_kind = "not_an_estimate_request"
            world.spec = {}
            world.customer_message("ap1", "thread-appraisal", (
                "Hi, I have a ring I inherited from my grandmother and I need to know what it's worth "
                "for insurance purposes. Can you appraise it?\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["awaiting_owner"], summary)
            self.assertEqual(world.sent, [], "never a price or a value to the customer")
            self.assertEqual(world.cards, [])
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            self.assertRegex(asked[0]["text"], r"(?i)appraisal|quote it|handle myself")
        self.run_branch(branch)

    def test_custom_order_facts_misclassified_as_inventory_still_prices(self) -> None:
        """WORKFLOW.md 6.1: 'A message naming a piece with two of its facts (karat, metal,
        stone, size, budget) is an estimate request whatever the reading called it,
        appraisals excepted.' The message below has no ready-made/in-stock language at
        all (no "do you have", "in stock", "ready to ship" -- see
        estimate_record.INVENTORY_RE) and names a piece plus three facts (karat, metal,
        stone), so per WORKFLOW it must be quoted as a custom estimate even though the
        reading mislabelled it inventory_request.

        Actual: scripts/pipeline.py's rescue for a misread reading
        (`judged["kind"] in ("not_an_estimate_request", "not_a_quote_request") and not
        estimate_record.asks_for_inventory(...) and estimate_record.reads_like_an_order(...)`,
        around line 767) only fires for the two "not_a..." triage kinds. It is never
        checked when the reading is "inventory_request" directly: pipeline.py line 773's
        `inventory = ... or triage["kind"] == "inventory_request" or (...)` treats that
        kind as inventory unconditionally, with no reads_like_an_order/asks_for_inventory
        rescue at all. A reading that says inventory_request for a message with no
        inventory wording and two or more order facts is never corrected, so the desk
        offers a visit instead of pricing the custom piece the customer actually asked
        for.

        Fix belongs in scripts/pipeline.py around lines 761-774: extend the rescue
        condition (or the `inventory` boolean itself) to also demote a bare
        "inventory_request" reading back to estimate_request when
        estimate_record.asks_for_inventory(handled_words) is False and
        estimate_record.reads_like_an_order(handled_words) is True, exactly as already
        done for the "not_an_estimate_request"/"not_a_quote_request" kinds.
        """
        def branch(ws: Path, world: World) -> None:
            self._profile_with_rates(ws)
            world.triage_kind = "inventory_request"  # the reading itself, mislabeled
            world.spec = {
                "piece_type": "engagement ring", "metal": "white gold", "metal_karat": "14k",
                "finger_size": "6", "setting_style": "solitaire",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_carat": "1.0", "center_stone": "yes",
            }
            world.customer_message("mc1", "thread-misclassified", (
                "Hi, I'd like a quote for a 14k white gold engagement ring, size 6, solitaire setting, with a "
                "1 carat lab-grown diamond. My budget is around $5,000. Let me know what you'd charge.\n\nPat"
            ))
            summary = self.tick(ws, world)
            # Rescued as an estimate request: the gate asks the one preference still open (the cut), never a visit card.
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(self.record(ws, self.only_estimate(ws))["missing_required_fields"], ["stone_cut"])
            self.assertFalse(any(c["kind"] == "appointment_offer" for c in world.cards), "no visit card for an order")
        self.run_branch(branch)

    def test_plain_thanks_before_any_estimate_is_not_treated_as_an_inquiry(self) -> None:
        """WORKFLOW.md 6.1: mail that is not an estimate request closes without a price,
        a card, or an owner ping (not_a_quote_request is in NOT_AN_INQUIRY)."""
        def branch(ws: Path, world: World) -> None:
            world.triage_kind = "not_a_quote_request"
            world.spec = {}
            world.customer_message("th0", "thread-thanks-cold", "Thanks so much for your time last week!\n\nPat")
            summary = self.tick(ws, world)
            self.assertEqual(world.cards, [])
            self.assertEqual(world.sent, [])
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 6. Renderings and reference photos
# ---------------------------------------------------------------------------

class RenderingAndPhotoTests(SimBase):

    def test_rendering_requested_before_any_estimate_still_completes_the_gate(self) -> None:
        """WORKFLOW.md 6.6 places renderings strictly after the estimate ('A request to see
        a picture' is a row in "After the estimate: what the customer says next"); nothing
        in WORKFLOW.md offers one earlier. The desk must still behave safely: no rendering
        is generated or sent pre-estimate, and the spec-gate / follow-up flow proceeds
        exactly as if the rendering request had not been there, non-negotiable 7."""
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("rb1", "thread-early-render", (
                "Hi, before we go further could you send me a rendering of what a signet ring like this would "
                "look like? 14k yellow gold, our company logo on the face, a few small lab-grown diamonds, G "
                "color VS clarity, along the shoulders.\n\nPat"
            ))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertEqual(world.renders, [], "no rendering is generated before an estimate exists")
            self.assertEqual(world.cards, [])
        self.run_branch(branch)

    def test_photo_like_this_but_in_rose_gold_is_a_design_change_not_a_new_estimate(self) -> None:
        """RELEASE-PLAN-4.15.md section 4: 'a reference photo is the base to edit, not a
        logo'; WORKFLOW.md 6.6: a design change returns to the gate and pricing under
        owner review. A photo plus 'like this but in rose gold' after an estimate must be
        read as a metal-color change to the existing piece, not silently ignored and not
        treated as a brand-new inquiry."""
        def branch(ws: Path, world: World) -> None:
            thread, estimate_id = self._estimate_sent(ws, world)
            world.design_change = ["metal_color"]
            world.customer_message("ph1", thread, "Love it! Could we do it like this but in rose gold?\n\nPat",
                                    attachments=("inspo.png",))
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["post_estimate_finished"], summary)
            asked = [n for n in world.notices if not n["file"]]
            self.assertEqual(len(asked), 1, asked)
            answered = self.answer(ws, "change")
            self.assertEqual(answered["decision"], "design_change", answered)
            world.spec["metal_color"] = "rose"
            world.spec["metal"] = "rose gold"
            summary = self.tick(ws, world)
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["approval_requested"], summary)
            record = self.record(ws, estimate_id)
            self.assertEqual(record["specification"].get("metal_color"), "rose")
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 7. Quoted text with no ">" markers
# ---------------------------------------------------------------------------

class QuotedTextTests(SimBase):

    def test_unmarked_quoted_shop_email_is_not_mistaken_for_the_customers_words(self) -> None:
        """Nothing in WORKFLOW.md authorizes the desk to treat its own earlier words,
        pasted back by a customer's mail client with no ">" and no "On ... wrote:" line
        (common on plain-text and some mobile replies), as something the customer said.
        Only the customer's own new words may drive a decision (non-negotiables 3 and 6,
        RELEASE-PLAN-4.15.md section 4: "the customer's own words decide meetings").

        Actual: scripts/reading_check.py:own_words only drops a line that starts with
        ">" or matches _QUOTE_START_RE ("On ... wrote:", "----- Original Message -----",
        "From: ..."). A client that quotes inline with neither marker leaves the shop's
        own previous words in "own_words", and scripts/pipeline.py's `handled_words`
        (built from own_words, ~line 758) feeds
        estimate_record.settle_scheduling_intent, whose MEETING_RE
        (scripts/estimate_record.py ~line 1720) matches ordinary phrases a shop email
        would plausibly use ("stop by the shop", "in person"). The quoted shop text below
        contains "stop by the shop ... in person", so the desk manufactures a
        scheduling_intent the customer never wrote, and — because a required field
        (setting_style) is still missing — pipeline.py's before-estimate meeting branch
        (~line 826, `if specification.get("scheduling_intent") and (not
        record.get("appointment_booked") or ...)`) fires and requests an appointment
        approval card instead of asking the customer for the one field still open.

        Fix belongs in scripts/reading_check.py:own_words: strip a pasted prior message
        by content, not only by a leading marker — for example, drop any contiguous block
        of lines that exactly (or near-exactly, after whitespace normalization) matches
        the body of a message already in the thread sent_by == "shop" (available to
        callers via the digest), the way _QUOTE_START_RE already anchors on wrote:/From:
        boundaries.
        """
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("qt1", "thread-quoted", (
                "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face "
                "and a few small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me "
                "an estimate?\n\nPat"
            ))
            self.tick(ws, world)
            self.assertEqual(sorted(self.record(ws, self.only_estimate(ws))["missing_required_fields"]),
                             ["finger_size", "setting_style"])

            world.spec.update({"finger_size": "10"})  # setting_style still not answered
            # The shop's own follow-up, pasted underneath with no ">" and no "On ... wrote:" line, plus a line of the
            # shop's making that asks them to come by: none of it is the customer's words.
            quoted_shop_email = world.sent[0]["body"].rstrip()  # what the shop actually sent, as the thread holds it
            body = f"Size 10, thanks!\n\nPat\n\n{quoted_shop_email}"
            world.customer_message("qt2", "thread-quoted", body)
            summary = self.tick(ws, world)
            # The size is read and the one detail still open is asked once more (a partial answer is new
            # information, not a stall); no meeting is offered on the strength of the shop's own sentence.
            self.assertEqual([i["outcome"] for i in summary["inline"]], ["followup_sent"], summary)
            self.assertFalse(any(c["kind"] == "appointment_offer" for c in world.cards), "no meeting from the shop's own words")
            record = self.record(ws, self.only_estimate(ws))
            self.assertNotIn("scheduling_intent", record["specification"],
                             "the shop's own quoted words must never become the customer's scheduling intent")
        self.run_branch(branch)


# ---------------------------------------------------------------------------
# 8. Two customer messages before the desk has replied
# ---------------------------------------------------------------------------

class ConcurrentMessageTests(SimBase):

    def test_customer_writes_twice_before_a_tick_runs(self) -> None:
        """Both messages land in the same discovery batch because no tick has run between
        them (a customer who sends a quick "oh, and also..." follow-up seconds after their
        first email is entirely realistic). WORKFLOW.md 6.1: 'Open a record' on the first
        inquiry and 'Facts stated anywhere in the thread ... are known and are never asked
        again' — nothing in WORKFLOW.md allows a legitimate first contact to be discarded
        just because it arrived as two messages instead of one, and non-negotiable/6.9
        never lists "more than one unanswered message from a new customer" as mail to
        close without action.

        Actual: scripts/route_ownership.py:decide() (~lines 96-108) treats any thread with
        no existing desk record and thread_message_count != 1 as
        {"decision": "manual_review", "reason_code": "missing_thread_ownership"} — the
        same bucket used for "a reply in a conversation the desk never started" (a
        coworker's personal thread, or a thread that predates the desk). scripts/
        workflow_safe.py:intake() (~lines 1141-1146) handles that reason code by closing
        the message with kolo_safe.complete_claimed and outcome "not_desk_thread": "no
        review, no notice" — by design for a foreign thread, but wrong here because this
        thread is not foreign, it is this customer's first-ever contact, just split across
        two messages before the first tick ran. Both tw1 and tw2 are closed this way (no
        record is ever created, nothing is sent, the owner is never told), so the
        customer's first inquiry vanishes completely and permanently. (Confirmed directly:
        workflow_safe.intake(tw1) and intake(tw2) both return
        {"decision": "manual_review", "reason_code": "missing_thread_ownership",
        "outcome": "not_desk_thread", "next_action": "done"}.)

        Fix belongs in scripts/route_ownership.py:decide(): the "no owning record and
        thread_message_count != 1" branch needs a way to tell a brand-new, all-customer
        thread (nobody, including the shop, has replied yet) from a truly foreign one. The
        caller (workflow_safe.py:intake, which already has the full thread) can pass
        whether every message in the thread is sent_by == "customer" (via gmail_classify
        or gmail_text.thread_digest's sent_by, comparing each From header against the
        shop's own mailbox) and whether the claimed message's thread has never had a shop
        participant; when that holds, thread_message_count > 1 should still resolve to
        "new_inquiry" (that is, all queued customer messages on the thread should feed the
        one record intake creates), and only fall back to
        missing_thread_ownership/manual_review when a shop message already exists on the
        thread that the desk itself never sent (the real foreign-thread signal).
        """
        def branch(ws: Path, world: World) -> None:
            world.spec = {
                "piece_type": "signet ring", "metal": "yellow gold", "metal_karat": "14k",
                "engraving": "our company logo on the face",
                "accent_stones": "a few small lab-grown diamonds along the shoulders",
                "stone_type": "diamond", "stone_origin": "lab-grown", "stone_color": "G", "stone_clarity": "VS",
            }
            world.customer_message("tw1", "thread-twice", (
                "Hi, I would like a custom signet ring in 14k yellow gold with our company logo on the face and "
                "a few small lab-grown diamonds, G color VS clarity, along the shoulders. Can you give me an "
                "estimate?\n\nPat"
            ))
            # A second message, moments later, before the desk has replied to the first
            # (both land in the same discovery batch: no tick has run yet).
            world.customer_message("tw2", "thread-twice", "Oh, and it should be size 10 please.\n\nPat")
            world.spec = {**world.spec, "finger_size": "10"}  # the reading of the thread carries the second message
            summary = self.tick(ws, world)
            self.assertEqual(summary["claimed"], 2, summary)
            self.assertEqual(len(world.sent), 1, "one follow-up, not one per message, and not zero")
            self.assertEqual(self.record(ws, self.only_estimate(ws))["missing_required_fields"], ["setting_style"],
                             "the size from the second message is on the record even though it was never asked")
            for message_id in ("tw1", "tw2"):
                self.assertEqual(self.claim(ws, message_id)["status"], "processed", message_id)
        self.run_branch(branch)


if __name__ == "__main__":
    unittest.main()
