#!/usr/bin/env python3
"""The customer-phrase rules, in one table, with the live phrases that shaped them (10 September 2026).

Every rule that reads a customer's own words lives in its own module
(estimate_record, slots, judge, gmail_text); this table names each one, what
it decides, and the phrases from live threads it must keep getting right.
`python3 scripts/words.py` runs the table; tests/test_words.py runs it in the
suite. A tester's new phrasing becomes a row here first, then a change to
the rule it names.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import estimate_record
import gmail_text
import judge
import slots

TUESDAY = datetime(2026, 9, 8, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


@dataclass
class Rule:
    name: str
    decides: str
    check: Callable[[str], Any]
    yes: list[tuple[str, Any]] = field(default_factory=list)  # (phrase, expected)
    no: list[str] = field(default_factory=list)  # phrases that must give a falsy answer
    seen: str = ""  # the live case that shaped it


def _bool(fn: Callable[[str], Any]) -> Callable[[str], bool]:
    return lambda words: bool(fn(words))


RULES: list[Rule] = [
    Rule("meeting words", "the sentences that ask to meet, propose a day and time, or state one", estimate_record.scheduling_sentences,
         yes=[("Any chance we can do Friday at 4pm?", ["Any chance we can do Friday at 4pm?"]), ("Can we meet Tuesday?", None),
              ("I can come by Friday afternoon.", None), ("Could I stop by the shop next week?", None), ("Monday the 21st at 11am please.", None),
              ("Tomorrow at 10 am, and 18k please.", None), ("I'd like to see them in person.", None)],
         no=["Can you have it ready by Friday at 5pm?", "Does it come in rose gold?", "Could you ship it by Tuesday?", "I can pick it up Friday at 4pm."],
         seen="8 Sep (reschedule read as a questionnaire); 9 Sep (a flat day and time priced with no meeting card)"),
    Rule("accepts a time", "a reply that picks or accepts an offered time", estimate_record.accepts_a_time,
         yes=[("The second one works for me.", True), ("Wednesday is fine.", True), ("2pm works.", True), ("See you then!", True)],
         no=["Before I come in, can I get a ballpark?", "What would 14k run me?"],
         seen="8 Sep (the ballpark reply re-offered times three times)"),
    Rule("asks to reschedule", "a meeting they have is being moved", estimate_record.asks_to_reschedule,
         yes=[("Something came up for Saturday... Any chance we can do Friday at 4pm?", True)],
         no=["Can we meet Tuesday?", "Does it come in rose gold?"], seen="8 Sep"),
    Rule("asks for an estimate", "they want a number", estimate_record.asks_for_estimate,
         yes=[("Before I come in, is there any way I can get a ballpark estimate?", True), ("How much would that run?", True), ("Could you quote me?", True)],
         no=["Could I come by next week?", "The second one works for me.", "She loves these earrings."], seen="8 Sep"),
    Rule("leaves it to the jeweler", "an asked detail becomes the jeweler's choice", estimate_record.leaves_to_jeweler,
         yes=[("I don't know. This is just a reference.", True), ("I'm not sure what carat weight", True), ("No, I don't.", True), ("not really", True),
              ("no clue", True), ("haven't decided", True)],
         no=["No, I don't want a halo", "I don't like rose gold", "2.5 ct please"],
         seen="8 Sep (dimensions asked twice); 9 Sep (the jeweler: 'do you know the carat?' 'No' must never loop)"),
    Rule("setting in their words", "a setting they name is theirs", estimate_record.setting_in_words,
         yes=[("I'm not sure the halo size", "halo"), ("A bezel-set solitaire please", "bezel-set"), ("pavé band", "pave")],
         no=["I don't want a halo, something simpler", "no halo please"], seen="9 Sep (ruby earrings: 'jeweler's choice: setting style')"),
    Rule("earring style", "studs, hoops, or drops", estimate_record.earring_style_in_words,
         yes=[("Studs please", "stud"), ("huggies", "hoop"), ("something dangly", "drop"), ("halo studs", "stud")],
         no=["18k white gold please"], seen="9 Sep (a halo pair rendered as leverback drops)"),
    Rule("pair carat basis", "a stated carat is each stone's or the pair's total", estimate_record.carat_basis_in_words,
         yes=[("2.5 ct each", "each"), ("per earring 1 ct", "each"), ("2 ct total", "total"), ("1.5 tcw", "total")],
         no=["2.5 ct"], seen="9 Sep (the owner: pairs are very specific)"),
    Rule("stone size in millimetres", "a stone sized in mm is sized; the carat is derived", estimate_record.stone_size_in_words,
         yes=[("she would like 15mm x 12mm oval blue topaz", "15mm x 12mm"), ("a 6.5 x 8 mm oval", "6.5mm x 8mm")],
         no=["a 2 ct oval"], seen="9 Sep (Blue Topaz: asked the carat, then 'I'm not sure')"),
    Rule("a piece the shop made", "'you made this for me': the piece on file is the base", estimate_record.refers_to_a_prior_piece,
         yes=[("an exact replica of this pendant you made for me", True), ("same as the ring I bought from you last year", True), ("you guys made my wife's band", True)],
         no=["I want a pendant like the one on your website", "my grandmother's ring, made by a jeweler in Italy"],
         seen="9 Sep (Blue Topaz, the jeweler's rule)"),
    Rule("an earlier conversation", "'we talked about earlier': the earlier estimate is the base", estimate_record.refers_to_an_earlier_conversation,
         yes=[("Do you remember this emerald earrings we talked about earlier?", True), ("same as the ring you quoted me last month", True),
              ("from my earlier email", True), ("the estimate you sent for the pendant", True)],
         no=["Can you make me earrings?", "we talked about a budget of 5000 with my wife"], seen="9 Sep (More earrings: asked everything again)"),
    Rule("a phone number", "a number to call, anywhere they wrote it", estimate_record.phone_in_words,
         yes=[("213.431.9336 | david@koloai.com", "213.431.9336"), ("call me at (415) 555-0100 please", "415.555.0100"), ("You can reach me at (415) 555-0100. See you then!", "415.555.0100")],
         no=["a 2.5 ct stone, size 6, 18k", "order 20260909123 shipped"], seen="9 Sep (a call booked from the engagement ring thread)"),
    Rule("a call or a visit", "what the meeting is; a number in a signature never makes it a call", estimate_record.meeting_kind_in_words,
         yes=[("are you available for a call tomorrow at 3pm?", "call"), ("could I come in Friday at 2?", "visit"), ("can we do a quick zoom?", "call"),
              ("Can I bring them in Friday?", "visit")],
         no=["Friday at 3pm works", "Are you available tomorrow at 1pm?\nDavid Trujillo\n(310) 810-3004"],
         seen="9 Sep (David: a visit confirmed as 'I'll call you at (310) 810-3004')"),
    Rule("courtesy only", "a note with nothing to act on: after a booking, nothing is sent", estimate_record.courtesy_only,
         yes=[("See you tomorrow!\n\nThank you,\nDavid Trujillo\nAtelier by Edward Avedis\n(310) 810-3004", True),
              ("Thanks so much, looking forward to it!\n\nDavid", True), ("Perfect. See you Friday.", True), ("See you then!", True)],
         no=["Thanks! Also, could we do 18k instead?", "See you tomorrow, and 18k white gold please.", "Sounds good. What time does the shop open?"],
         seen="9 Sep (David: 'See you tomorrow!' got a questionnaire)"),
    Rule("a day named after an offer", "a bare day answers an offer of times", estimate_record.day_sentences,
         yes=[("Monday the 14th would be best.", None), ("How about next Tuesday?", None), ("Tomorrow morning please.", None)],
         no=["Can you have it ready by Monday?", "I'd like 18k white gold, lab grown please.", "Ship it by the 14th please."],
         seen="9 Sep (a day plus the details was priced with no meeting card)"),
    Rule("inventory words", "a ready-made piece, not a custom order", estimate_record.asks_for_inventory,
         yes=[("Do you have this in stock?", True), ("is it ready to ship?", True)],
         no=["Could you make me a ring like this?"], seen="8 Sep (premade inventory: offer a visit)"),
    Rule("reads like an order", "a piece and its facts named outright", estimate_record.reads_like_an_order,
         yes=[("Do you have a 14k WG lab tennis bracelet, 7-inch, ready to ship?", True)],
         no=["Hi, do you do appraisals?"], seen="8 Sep (the bracelet that was skipped)"),
    Rule("period of days", "'next week', 'Friday afternoon', 'after 1pm' decide where the offered times come from",
         lambda words: (lambda p: (p["start"], p.get("half"), p.get("earliest")) if p else None)(slots.requested_period([words], TUESDAY)),
         yes=[("next week", ("2026-09-14", None, None)), ("early next week", ("2026-09-14", None, None)), ("Friday afternoon", ("2026-09-11", "afternoon", None)),
              ("after 1pm any day next week", ("2026-09-14", None, 780)), ("Monday the 21st", ("2026-09-21", None, None))],
         no=["whenever suits", "Friday at 3pm"], seen="9 Sep (times offered today for 'next week'; 9:00 AM offered for 'after 1pm')"),
    Rule("a day and clock time", "resolved by code, whatever the reading said",
         lambda words: slots.resolve_phrase(words, TUESDAY),
         yes=[("Friday at 3pm", "2026-09-11T15:00"), ("tomorrow at 1pm", "2026-09-09T13:00"), ("Monday at 1pm", "2026-09-14T13:00")],
         no=["after 1pm on Monday", "next week", "Friday 2"], seen="8 Sep ('would Friday at 3pm work' reached the calendar unresolved)"),
    Rule("technical questions", "a question a customer cannot answer, never sent",
         lambda body: [q for q in judge.bench_measurement_questions(body)],
         yes=[("- What is the millimeter diameter of the center emerald?", ["- What is the millimeter diameter of the center emerald?"]),
              ("- How many prongs would you like on each stone?", None)],
         no=["- Which karat, 14K or 18K?", "- Roughly how long would you like the hoops?"], seen="8 Sep (millimetres and drop lengths asked)"),
    Rule("greets someone else", "the gift-giver, not the person the piece is for",
         lambda body: gmail_text.greets_someone_else(body, "David"),
         yes=[("Hi Verónica,\n\nIt is wonderful", "Verónica")],
         no=["Hi David,\n\nx", "Hello,\n\nx", "Hi there,\n\nx"], seen="9 Sep (Blue Topaz: 'Hi Verónica' to David)"),
]


def check_all() -> list[str]:
    """Every phrase in the table against its rule; the failures, in words."""
    failures: list[str] = []
    for rule in RULES:
        for phrase, expected in rule.yes:
            got = rule.check(phrase)
            if expected is None:
                if not got:
                    failures.append(f"{rule.name}: {phrase!r} should hold, got {got!r}")
            elif got != expected:
                failures.append(f"{rule.name}: {phrase!r} should give {expected!r}, got {got!r}")
        for phrase in rule.no:
            got = rule.check(phrase)
            if got:
                failures.append(f"{rule.name}: {phrase!r} should not hold, got {got!r}")
    return failures


def main(argv: list[str] | None = None) -> int:
    failures = check_all()
    for rule in RULES:
        print(f"{rule.name}: {rule.decides} ({len(rule.yes)} yes, {len(rule.no)} no){' | ' + rule.seen if rule.seen else ''}")
    if failures:
        print("\nFAILURES:")
        for line in failures:
            print(" ", line)
        return 1
    print(f"\nOK {len(RULES)} rules, {sum(len(r.yes) + len(r.no) for r in RULES)} phrases, {datetime.now(timezone.utc).date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
