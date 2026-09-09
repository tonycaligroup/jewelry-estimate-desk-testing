"""Stress tests for scripts/rendering.py (RELEASE-PLAN-4.15.md 4.15.0): plan, prompts, and checks.

Offline only; the model calls (plan_render/judge.ask_json) are driven through a fake `runner` that returns
canned JSON, exactly as judge.complete() falls back to when image_provider is unavailable (verified true in
this sandbox). No network, Kolo, Gmail, Sheets, or pod access. Passing cases stay as regular tests;
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rendering


def _fake_planner(archetype: str, notes: str = "") -> "rendering.Runner":
    def runner(argv, **kw):
        payload = {"outputs": [{"text": json.dumps({
            "archetype": archetype, "mark_source": "none", "must_be_exact": [],
            "fine_lettering": False, "notes": notes,
        })}]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
    return runner


class NotesWordCollisionHijacksTheArchetypeTests(unittest.TestCase):
    """Defect: archetype_for() scans piece_type + setting_style + notes as one bag of words and returns the
    FIRST matching entry in ARCHETYPE_WORDS, with no awareness of negation or of which field the word came
    from. A negated mention in freeform notes text ("Not hoops, please") plants the word "hoop" in that bag,
    and since ("hoop", "hoop_earrings") sits earlier in ARCHETYPE_WORDS than ("drop earring",
    "drop_earrings"), archetype_for() returns "hoop_earrings" even though piece_type says "drop earrings"
    outright. Worse, plan_piece() treats archetype_for()'s answer as authoritative and silently overrides
    the (correct) planner's choice whenever they differ - this is precisely the class of live incident
    RELEASE-PLAN-4.15.md 4.15.0 was written to prevent ("emerald halo studs rendered as diamond drops"), now
    reproduced by the fix itself.
    """

    def test_archetype_for_is_hijacked_by_a_negated_word_in_notes(self) -> None:
        spec = {"piece_type": "drop earrings", "notes": "Not hoops, please - drop earrings only."}
        self.assertEqual(rendering.archetype_for(spec), "drop_earrings",
                          "the piece_type is explicit and unambiguous; a negated word in notes should not override it")

    def test_plan_piece_overrides_a_correct_planner_answer_with_the_hijacked_archetype(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            spec = {"piece_type": "drop earrings", "notes": "Not hoops, please - drop earrings only.",
                    "metal": "14k yellow gold"}
            planned = rendering.plan_piece(spec, Path(d) / "out", openclaw="openclaw",
                                           runner=_fake_planner("drop_earrings", "drop earrings, clearly"))
            self.assertEqual(planned["plan"]["archetype"], "drop_earrings",
                              "the planner correctly said drop_earrings and there is no real disagreement to settle")

    def test_an_unambiguous_word_still_settles_the_archetype_over_the_planner(self) -> None:
        """The mechanism is sound when there is no collision: this is the behaviour 4.15.0 intends to keep."""
        spec = {"piece_type": "stud earrings"}
        self.assertEqual(rendering.archetype_for(spec), "stud_earrings")
        with tempfile.TemporaryDirectory() as d:
            planned = rendering.plan_piece(spec, Path(d) / "out", openclaw="openclaw",
                                           runner=_fake_planner("drop_earrings", "the planner guessed wrong"))
            self.assertEqual(planned["plan"]["archetype"], "stud_earrings",
                              "the piece's own words correctly overrule a wrong planner guess")


class BooleanCenterStoneFalseIsTreatedAsPresentTests(unittest.TestCase):
    """Defect: exact_facts() decides whether a piece has a center stone with
    `str(spec.get("center_stone") or "").lower() not in ("no", "none", "false")`. Python's `or` coalesces a
    real boolean False to '' before the string ever gets built, so the exclusion list (which only contains
    the literal strings "no"/"none"/"false") never matches and the piece is treated as HAVING a center
    stone. ledger._present() explicitly special-cases bool values as a first-class possibility elsewhere in
    this codebase, so a genuine `center_stone: false` (e.g. from an LLM extraction that emits a JSON
    boolean rather than the string "no") is a realistic input here. The practical effect is exactly the
    4.14.1 defect this release describes ("an eternity, channel-set, pave or all-around band has no center
    stone whatever the reading says") reappearing through a different door: the render prompt and the
    vision checker both demand a center stone that should not exist.
    """

    def test_a_boolean_false_center_stone_is_excluded_like_the_string_no(self) -> None:
        spec_bool = {"piece_type": "eternity band", "stone_type": "diamond", "center_stone": False,
                     "accent_stones": "melee diamonds all around"}
        spec_str = {"piece_type": "eternity band", "stone_type": "diamond", "center_stone": "no",
                    "accent_stones": "melee diamonds all around"}
        self.assertEqual(rendering.exact_facts(spec_bool), rendering.exact_facts(spec_str),
                          "a boolean False should be excluded the same way the string 'no' is")
        self.assertNotIn("white, colorless diamond center stone", rendering.exact_facts(spec_bool))

    def test_the_string_no_is_correctly_excluded(self) -> None:
        spec = {"piece_type": "eternity band", "stone_type": "diamond", "center_stone": "no",
               "accent_stones": "melee diamonds all around"}
        facts = rendering.exact_facts(spec)
        self.assertNotIn("white, colorless diamond center stone", " ".join(facts))
        self.assertIn("accent stones: melee diamonds all around", facts)


class PassingRenderingBehaviourTests(unittest.TestCase):
    """A grab-bag of edge cases rendering.py gets right, kept here as a record of what was checked."""

    def test_archetype_for_covers_every_kind_of_piece_named_in_its_word_list(self) -> None:
        cases = {
            "stud earrings": "stud_earrings", "hoop earrings": "hoop_earrings", "huggie earrings": "hoop_earrings",
            "drop earrings": "drop_earrings", "dangle earrings": "drop_earrings", "tennis bracelet": "tennis_bracelet",
            "signet ring": "signet", "eternity band": "eternity_band", "three stone ring": "three_stone_ring",
            "solitaire ring": "solitaire_ring", "locket": "locket", "cufflinks": "cufflinks", "brooch": "brooch",
            "tie bar": "tie_bar",
        }
        for piece_type, archetype in cases.items():
            with self.subTest(piece_type=piece_type):
                self.assertEqual(rendering.archetype_for({"piece_type": piece_type}), archetype)
        self.assertIsNone(rendering.archetype_for({"piece_type": "a plain ring"}), "no archetype word, left to the planner")
        self.assertIsNone(rendering.archetype_for("not a dict"))
        self.assertIsNone(rendering.archetype_for({}))

    def test_a_spec_with_no_stone_produces_no_stone_facts(self) -> None:
        facts = rendering.exact_facts({"piece_type": "plain band", "metal": "14k yellow gold"})
        self.assertEqual(facts, ["the piece is plain band", "metal: 14k yellow gold"])

    def test_a_customer_stated_stone_color_wins_over_the_default_word(self) -> None:
        stated = rendering.exact_facts({"stone_type": "sapphire", "stone_color": "pink", "center_stone": "yes"})
        default = rendering.exact_facts({"stone_type": "sapphire", "center_stone": "yes"})
        self.assertIn("pink sapphire center stone", stated)
        self.assertIn("blue sapphire center stone", default)

    def test_exact_checks_ids_and_questions_name_the_fact(self) -> None:
        facts = rendering.exact_facts({"piece_type": "stud earrings", "stone_type": "emerald",
                                       "stone_color": "green", "center_stone": "yes", "metal": "18k white gold"})
        checks = rendering.exact_checks(facts)
        ids = [c["id"] for c in checks]
        self.assertEqual(len(ids), len(set(ids)), "no id collisions across a normal set of facts")
        self.assertTrue(all(c["id"].startswith("exact_") for c in checks))
        self.assertTrue(all(len(c["id"]) <= 46 for c in checks))
        self.assertIn("emerald", checks[1]["question"])

    def test_an_example_photo_vs_a_logo_reference_kind_produce_different_prompt_language(self) -> None:
        spec = {"piece_type": "signet ring", "metal": "14k yellow gold"}
        plan_example = {"archetype": "signet", "mark_source": "artwork", "must_be_exact": [], "reference_kind": "example"}
        plan_mark = {**plan_example, "reference_kind": "mark"}
        example_prompt = rendering.build_prompts(plan_example, spec, has_artwork=True, has_exemplar=False)[0]
        mark_prompt = rendering.build_prompts(plan_mark, spec, has_artwork=True, has_exemplar=False)[0]
        self.assertIn("customer's example piece", example_prompt)
        self.assertIn("customer's mark: reproduce it exactly", mark_prompt)
        self.assertNotIn("example piece", mark_prompt)

    def test_a_multi_piece_style_set_note_does_not_break_archetype_or_facts(self) -> None:
        piece = {"piece_type": "wedding band", "metal": "14k rose gold", "notes": "matching set, same finish"}
        self.assertEqual(rendering.archetype_for(piece), None)
        facts = rendering.exact_facts(piece)
        self.assertEqual(facts[0], "the piece is wedding band")

    def test_all_checks_combines_archetype_checks_with_exact_checks(self) -> None:
        plan = {"archetype": "stud_earrings", "exact_checks": rendering.exact_checks(["the piece is stud earrings"])}
        checks = rendering.all_checks(plan)
        ids = [c["id"] for c in checks]
        self.assertIn("exact_the_piece_is_stud_earrings", ids)
        self.assertGreaterEqual(len(checks), 4, "the archetype's own checks plus the exact ones")


if __name__ == "__main__":
    unittest.main()
