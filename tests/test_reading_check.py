"""ARCHITECTURE-OPTIONS.md E': the reading is checked against the customer's words, in code."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reading_check  # noqa: E402


def digest(*bodies: str, shop: str = "") -> dict:
    messages = [{"body": b, "sent_by": "customer"} for b in bodies]
    if shop:
        messages.append({"body": shop, "sent_by": "shop"})
    return {"messages": messages}


class ReadingCheckTests(unittest.TestCase):
    def test_two_sizes_read_as_one_piece_is_a_piece_count_check(self) -> None:
        found = reading_check.compare(digest("An engagement ring size 6 and a matching band, size 10, both 14k yellow gold."),
                                      {"piece_type": "engagement ring", "finger_size": "6", "metal": "yellow gold"})
        self.assertEqual([d["topic"] for d in found], ["piece_count"])
        self.assertIn("how many pieces", found[0]["question"])
        self.assertEqual(found[0]["said"], "sizes 6, 10")

    def test_two_pieces_read_for_two_sizes_agrees(self) -> None:
        spec = {"pieces": [{"piece_type": "engagement ring", "finger_size": "6"}, {"piece_type": "wedding band", "finger_size": "10"}]}
        self.assertEqual(reading_check.compare(digest("Ring size 6 and the band a size 10."), spec), [])

    def test_a_misread_size_is_confirmed(self) -> None:
        found = reading_check.compare(digest("She wears a size 7."), {"piece_type": "ring", "finger_size": "1"})
        self.assertEqual([d["topic"] for d in found], ["finger_size"])
        self.assertEqual(found[0]["said"], "size 7")

    def test_half_sizes_and_no_size_word_never_count(self) -> None:
        self.assertEqual(reading_check.compare(digest("Size 6.5 please."), {"piece_type": "ring", "finger_size": "6.5"}), [])
        self.assertEqual(reading_check.compare(digest("A ring for her, we will measure later."), {"piece_type": "ring"}), [])

    def test_a_carat_the_reading_does_not_carry_is_confirmed(self) -> None:
        found = reading_check.compare(digest("A 1.5 ct round lab-grown diamond."), {"piece_type": "ring", "stone_carat": "1", "stone_origin": "lab-grown"})
        self.assertEqual([d["topic"] for d in found], ["stone_carat"])
        self.assertEqual(found[0]["said"], "1.5 ct")
        self.assertEqual(reading_check.compare(digest("A 1.5 ct round lab-grown diamond."), {"piece_type": "ring", "stone_carat": "1.5", "stone_origin": "lab-grown"}), [])

    def test_origin_words_against_the_opposite_reading(self) -> None:
        lab = reading_check.compare(digest("A lab grown diamond please."), {"piece_type": "ring", "stone_origin": "natural"})
        self.assertEqual([d["topic"] for d in lab], ["stone_origin"])
        natural = reading_check.compare(digest("A natural diamond, not lab."), {"piece_type": "ring", "stone_origin": "lab-grown"})
        self.assertEqual([d["topic"] for d in natural], ["stone_origin"])
        self.assertEqual(reading_check.compare(digest("A diamond ring."), {"piece_type": "ring", "stone_origin": "lab-grown"}), [],
                         "no origin word, nothing to confirm; the gate asks as before")

    def test_the_customers_own_stone_read_as_supplied_is_confirmed(self) -> None:
        found = reading_check.compare(digest("I would like to reset my grandmother's diamond in a bezel."),
                                      {"piece_type": "pendant", "stone_type": "diamond", "stone_origin": "lab-grown"})
        self.assertEqual([d["topic"] for d in found], ["customer_stone"])
        agreed = {"piece_type": "pendant", "stone_type": "diamond", "customer_supplied_materials": "her grandmother's diamond"}
        self.assertEqual(reading_check.compare(digest("I would like to reset my grandmother's diamond in a bezel."), agreed), [])

    def test_the_shops_own_words_are_never_read_as_the_customers(self) -> None:
        self.assertEqual(reading_check.compare(digest("A signet ring.", shop="What size 6 or size 10 would you like?"),
                                               {"piece_type": "signet ring", "finger_size": "8"}), [])

    def test_quoted_shop_text_under_a_reply_never_counts(self) -> None:
        """6 September 2026: "I have attached the design renderings" quoted under a reply read as the customer's own stone."""
        body = ("Actually, can we change it to 14k rose gold, keeping everything else the same?\n\nTony\n\n"
                "On Sun, Sep 6, 2026 at 8:39 PM <shop@example.com> wrote:\n"
                "> Hi Tony, I have attached the design renderings you requested. Reset your\n"
                "> expectations on lead time; my grandmother's ring took six weeks.\n")
        spec = {"piece_type": "men's wedding band", "metal": "rose gold", "metal_karat": "14k", "stone_type": "diamond",
                "stone_origin": "lab-grown"}
        self.assertEqual(reading_check.compare(digest(body), spec), [])
        self.assertEqual(reading_check.own_words(body).strip().splitlines()[-1], "Tony")
        # The customer's own line above the quote still counts.
        own = "I would like to reset my grandmother's diamond.\n\nOn Sun wrote:\n> anything"
        self.assertEqual([d["topic"] for d in reading_check.compare(digest(own), spec)], ["customer_stone"])

    def test_a_denied_own_stone_is_not_a_claim(self) -> None:
        """6 September 2026: "I don't have stone of my own" matched "my own" and the owner was asked."""
        spec = {"piece_type": "men's wedding band", "metal": "rose gold", "metal_karat": "14k", "stone_type": "diamond",
                "stone_origin": "lab-grown"}
        for body in ("I don't have stone of my own. Just the change to 14k rose gold.",
                     "No stone of my own, same design.",
                     "We do not have a family diamond to use."):
            self.assertEqual(reading_check.compare(digest(body), spec), [], body)
        self.assertEqual([d["topic"] for d in reading_check.compare(digest("I have a diamond of my own to set."), spec)], ["customer_stone"])

    def test_reusing_a_band_is_not_a_stone(self) -> None:
        """7 September 2026: "We would like to reuse the wedding band" asked the customer about a stone of their own."""
        spec = {"piece_type": "engagement ring", "metal": "rose gold", "metal_karat": "18k", "stone_type": "diamond",
                "stone_origin": "lab-grown", "stone_carat": "3"}
        for body in ("We would like a classic 3 ct solitaire in 18k rose gold. We would like to reuse the wedding band. Can you remove it from the original?",
                     "Please remount it in a new setting.", "Can we reset the ring in yellow gold?", "We have an existing band to match."):
            self.assertEqual(reading_check.compare(digest(body), spec), [], body)
        for body in ("Please reuse my mother's diamond.", "Can you reset her stone into this?", "We would like to remount the sapphire we have."):
            self.assertEqual([d["topic"] for d in reading_check.compare(digest(body), spec)], ["customer_stone"], body)

    def test_names_and_questions(self) -> None:
        self.assertTrue(reading_check.is_confirm("confirm.piece_count"))
        self.assertFalse(reading_check.is_confirm("finger_size"))
        self.assertIn("how many pieces", reading_check.question_for("confirm.piece_count"))
        self.assertIsNone(reading_check.question_for("finger_size"))


if __name__ == "__main__":
    unittest.main()
