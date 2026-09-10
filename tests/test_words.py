"""The customer-phrase table (scripts/words.py): every live phrase keeps holding, and every rule has both kinds of example."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import words  # noqa: E402


class WordsTableTests(unittest.TestCase):
    def test_every_phrase_in_the_table_holds(self) -> None:
        self.assertEqual(words.check_all(), [])

    def test_every_rule_has_examples_both_ways_and_a_live_case(self) -> None:
        for rule in words.RULES:
            with self.subTest(rule=rule.name):
                self.assertTrue(rule.yes, "a phrase that holds")
                self.assertTrue(rule.no, "a phrase that must not")
                self.assertTrue(rule.seen, "the live case it came from")

    def test_the_command_line_reports(self) -> None:
        self.assertEqual(words.main([]), 0)
