"""Stress tests for scripts/ledger.py (RELEASE-PLAN-4.15.md 4.15.0): the estimate ledger in SQLite.

Offline only; no network, Kolo, Gmail, Sheets, or pod access. Passing cases stay as regular
file is green as a whole and still documents the bug with a runnable reproduction.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ledger


class QuotedFactMovesOnlyForACustomerWordedChangeTests(unittest.TestCase):
    """Defect: a named ('changeable') change that resolves to a non-'customer' source can never move a quoted fact.

    ledger.absorb's carve-out at the 'current["source"] == "quoted" and source == "customer"' branch only
    fires when source_of() already resolved the new value's source to "customer" (i.e. the exact words are
    found verbatim in the customer's own words). A customer who releases a quoted grade to the jeweler
    ("actually, you pick the color" / "whatever you think") produces source_of() == "jeweler" (rank 2), which
    never reaches that branch and is instead rejected by the generic "a lesser source never replaces what
    stands" rule (RANK[quoted]=4 > RANK[jeweler]=2), even though `changeable` names the field. The result:
    the desk keeps re-quoting the stale grade forever after the customer explicitly asked to change it.
    """

    def test_releasing_a_quoted_grade_to_the_jeweler_is_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-1", {"stone_color": "d"}, "m1", "D color please.", "")
            ledger.quote(desk, "jed-1", {"stone_color": "d"}, "m1")
            self.assertEqual(ledger.specification(desk, "jed-1")["stone_color"], "d")
            changed = ledger.absorb(
                desk, "jed-1", {"stone_color": "jeweler's choice"}, "m2",
                "Actually, you pick the color, whatever you think.", "",
                changeable={"stone_color"},
            )
            self.assertNotEqual(changed, [], "a named/changeable release to the jeweler's choice should move a quoted fact")
            self.assertEqual(ledger.specification(desk, "jed-1")["stone_color"], "jeweler's choice",
                              "the estimate should stop re-quoting the old grade once the customer explicitly "
                              "released it on a field the classifier named changeable")

    def test_a_literal_customer_worded_change_does_move_a_quoted_fact(self) -> None:
        """The one path the carve-out actually covers: the new value's own text is found in the words."""
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-1", {"metal_color": "yellow"}, "m1", "Yellow gold please.", "")
            ledger.quote(desk, "jed-1", {"metal_color": "yellow"}, "m1")
            changed = ledger.absorb(desk, "jed-1", {"metal_color": "rose"}, "m2", "Actually rose gold please.",
                                    "", changeable={"metal_color"})
            self.assertEqual([(r["field"], r["source"]) for r in changed], [("metal_color", "customer")])
            self.assertEqual(ledger.specification(desk, "jed-1")["metal_color"], "rose")


class MigrateNeverLetsTheCurrentSpecBeatTheLastQuotedSpecTests(unittest.TestCase):
    """Defect: migrate() writes the current specification's facts at rank "reading" (0), which can never
    outrank the archived estimate's "quoted" (4) facts it just wrote a moment earlier for the same field.

    For a pre-ledger record whose live `specification` had moved on since the last sent estimate (a reopen,
    a manual edit, a merge that happened before the ledger existed), migrate() silently discards those
    newer facts in favour of the stale quoted ones for every field present in both. The record's own
    up-to-date state is exactly the thing migrate() claims to preserve ("the current ones as readings") but
    the write is a no-op wherever it actually mattered.
    """

    def test_a_record_reopened_and_changed_before_the_ledger_existed_reverts_to_the_stale_quote(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            record = {
                "estimate_id": "jed-2",
                "route": {"gmail_message_id": "m0"},
                "estimate_history": [{"specification": {"metal_color": "yellow", "stone_carat": 1.0}}],
                "specification": {"metal_color": "rose", "stone_carat": 1.5},
            }
            self.assertGreater(ledger.migrate(desk, record), 0)
            spec = ledger.specification(desk, "jed-2")
            self.assertEqual(spec["metal_color"], "rose", "reproduces the defect: reverts to 'yellow'")
            self.assertEqual(spec["stone_carat"], 1.5, "reproduces the defect: reverts to 1.0")

    def test_migrate_adds_a_brand_new_field_absent_from_the_quoted_history(self) -> None:
        """The part of migrate() that does work: a field only in the current spec is picked up fine."""
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            record = {
                "estimate_id": "jed-2b",
                "route": {"gmail_message_id": "m0"},
                "estimate_history": [{"specification": {"metal_color": "yellow"}}],
                "specification": {"metal_color": "yellow", "stone_type": "sapphire"},
            }
            ledger.migrate(desk, record)
            self.assertEqual(ledger.specification(desk, "jed-2b")["stone_type"], "sapphire")


class CrossPieceWordBleedTests(unittest.TestCase):
    """Defect: source_of()/_find() match a value's text anywhere in the customer's own words with no
    awareness of which piece the field belongs to. In a multi-piece spec, if the reading swaps or
    misattributes a per-piece value, both wrong values still get marked "customer" (rank 4, permanently
    protected) purely because the numbers/words happen to appear somewhere in the message discussing the
    *other* piece. A later, correctly-sourced reading can never fix it (RANK['customer'] >= every other rank
    except owner).
    """

    def test_a_misread_swap_between_two_pieces_is_locked_in_as_customer_sourced_and_never_correctable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            # The reading swapped the two ring sizes between pieces (a plausible extraction mistake).
            # The reading swapped the two sizes: the words say ring 6, band 9; the reading says band 6, ring 9.
            spec = {"pieces": [{"piece_type": "wedding band", "finger_size": 6},
                               {"piece_type": "engagement ring", "finger_size": 9}]}
            words = "My ring size is a 6, but my fiance's band should be a 9."
            added = ledger.absorb(desk, "jed-3", spec, "m1", words, "")
            sources = {(r["field"], r["piece"]): r["source"] for r in added}
            # Each value sits next to the other piece's word, so neither is the customer's for its piece: a reading.
            self.assertEqual(sources[("finger_size", 0)], "reading")
            self.assertEqual(sources[("finger_size", 1)], "reading")
            # A later reading that puts each size with its own piece corrects it.
            fix = ledger.absorb(desk, "jed-3", {"pieces": [{"piece_type": "wedding band", "finger_size": 9},
                                                           {"piece_type": "engagement ring", "finger_size": 6}]}, "m2",
                                words, "")
            self.assertNotEqual(fix, [], "a corrective reading can fix a value that was only a reading")
            spec_after = ledger.specification(desk, "jed-3")
            self.assertEqual(spec_after["pieces"][0]["finger_size"], 9, "the band is a 9")
            self.assertEqual(spec_after["pieces"][1]["finger_size"], 6, "the ring is a 6")
            self.assertEqual({(r["field"], r["piece"]): r["source"] for r in fix}[("finger_size", 1)], "customer")

    def test_a_value_that_only_appears_for_its_own_piece_is_sourced_correctly(self) -> None:
        """The happy path: a per-piece value with no lexical overlap with the other piece sources correctly."""
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            spec = {"pieces": [{"piece_type": "wedding band", "metal_karat": 14},
                               {"piece_type": "engagement ring", "metal_karat": 18}]}
            words = "14k for the band, 18k for the ring."
            added = {(r["field"], r["piece"]): r for r in ledger.absorb(desk, "jed-3b", spec, "m1", words, "")}
            self.assertEqual(added[("metal_karat", 0)]["source"], "customer")
            self.assertEqual(added[("metal_karat", 1)]["source"], "customer")


class OpenAskClosedByAnyLaterValueTests(unittest.TestCase):
    """Defect: the schema has an `answered_in` column meant to record which message answered an ask, but
    nothing in ledger.py ever sets it (grep confirms only the SCHEMA/add_facts plumbing references it).
    open_asks() therefore treats *any* later value row for the field as an answer, including one produced by
    an unrelated, low-confidence "reading"-sourced guess (e.g. a repeated photo re-read), not necessarily the
    customer's actual reply. A field can drop off the "still waiting on" list without the customer ever
    having answered it.
    """

    def test_an_unrelated_reading_sourced_guess_closes_the_ask(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.mark_asked(desk, "jed-4", ["stone_carat"], "sent-1")
            self.assertEqual(ledger.open_asks(desk, "jed-4"), ["stone_carat"])
            added = ledger.absorb(desk, "jed-4", {"stone_carat": 1.2}, "m2",
                                  "Thanks for the reply, when can we meet?", "", default_source="reading")
            self.assertEqual(added[0]["source"], "reading", "not the customer's own words")
            self.assertEqual(ledger.open_asks(desk, "jed-4"), ["stone_carat"],
                              "a guess with no customer wording behind it should not close the ask")

    def test_a_real_customer_answer_closes_the_ask(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.mark_asked(desk, "jed-4b", ["metal_karat", "stone_type"], "sent-1")
            ledger.absorb(desk, "jed-4b", {"metal_karat": 14}, "m2", "14k please", "")
            self.assertEqual(ledger.open_asks(desk, "jed-4b"), ["stone_type"])


class PassingLedgerBehaviourTests(unittest.TestCase):
    """A grab-bag of edge cases the ledger gets right, kept here as a record of what was checked."""

    def test_numbers_vs_strings_and_plurals(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            spec = {"metal_karat": 18, "stone_type": "emerald", "accent_stones": "3 diamonds"}
            words = "18k please, with emeralds and 3 diamonds around it."
            added = {r["field"]: r for r in ledger.absorb(desk, "jed-5", spec, "m1", words, "")}
            self.assertEqual(added["metal_karat"]["source"], "customer", "18 (int) matches '18k' in the words")
            self.assertEqual(added["stone_type"]["source"], "customer", "emerald matches emeralds (plural)")
            self.assertEqual(added["accent_stones"]["source"], "customer")

    def test_unicode_and_very_long_values_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            words = "I would like a café au lait diamond, por favor. 你好"
            added = ledger.absorb(desk, "jed-6", {"stone_color": "café au lait"}, "m1", words, "")
            self.assertEqual(added[0]["source"], "customer")
            long_value = "a" * 5000
            ledger.absorb(desk, "jed-6", {"notes_detail": long_value}, "m1", long_value, "")
            self.assertEqual(len(ledger.specification(desk, "jed-6")["notes_detail"]), 5000)

    def test_a_photo_that_contradicts_the_words_is_sourced_as_photo_not_customer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            words = "I'd like an emerald ring."
            photo = "A round diamond solitaire in yellow gold."
            added = {r["field"]: r for r in ledger.absorb(desk, "jed-7", {"stone_type": "diamond"}, "m1", words, photo)}
            self.assertEqual(added["stone_type"]["source"], "photo")

    def test_jewelers_choice_never_overwrites_a_stated_grade(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-8", {"stone_color": "d"}, "m1", "D color please.", "")
            chosen = ledger.absorb(desk, "jed-8", {"stone_color": "jeweler's choice"}, "m2", "whatever you think", "")
            self.assertEqual(chosen, [])
            self.assertEqual(ledger.specification(desk, "jed-8")["stone_color"], "d")

    def test_empty_and_single_length_pieces_specs_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            self.assertEqual(ledger.absorb(desk, "jed-9", {}, "m1", "hello", ""), [])
            self.assertEqual(ledger.specification(desk, "jed-9"), {})
            added = ledger.absorb(desk, "jed-9", {"pieces": [{"piece_type": "ring", "metal_karat": 14}]}, "m1", "14k", "")
            self.assertEqual(len(added), 2)
            spec = ledger.specification(desk, "jed-9")
            self.assertEqual(len(spec["pieces"]), 1)
            self.assertEqual(spec["pieces"][0]["piece_type"], "ring")

    def test_delete_then_reabsorb_starts_clean(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-10", {"metal_karat": 14}, "m1", "14k please", "")
            self.assertGreater(ledger.delete_estimate(desk, "jed-10"), 0)
            self.assertEqual(ledger.specification(desk, "jed-10"), {})
            added = ledger.absorb(desk, "jed-10", {"metal_karat": 18}, "m2", "18k please", "")
            self.assertEqual(added[0]["value"], 18)

    def test_two_estimates_in_one_ledger_do_not_cross_contaminate(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-11a", {"metal_karat": 14}, "m1", "14k please", "")
            ledger.absorb(desk, "jed-11b", {"metal_karat": 18}, "m1", "18k please", "")
            self.assertEqual(ledger.specification(desk, "jed-11a")["metal_karat"], 14)
            self.assertEqual(ledger.specification(desk, "jed-11b")["metal_karat"], 18)

    def test_quote_then_change_then_quote_again(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            desk = Path(d)
            ledger.absorb(desk, "jed-12", {"metal_color": "yellow"}, "m1", "yellow gold please", "")
            ledger.quote(desk, "jed-12", {"metal_color": "yellow"}, "m1")
            ledger.absorb(desk, "jed-12", {"metal_color": "rose"}, "m2", "actually rose gold", "",
                          changeable={"metal_color"})
            self.assertEqual(ledger.specification(desk, "jed-12")["metal_color"], "rose")
            ledger.quote(desk, "jed-12", {"metal_color": "rose"}, "m2")
            self.assertEqual(ledger.specification(desk, "jed-12")["metal_color"], "rose")
            winning = ledger.winning(desk, "jed-12")[("metal_color", None)]
            self.assertEqual(winning["source"], "quoted")


if __name__ == "__main__":
    unittest.main()
