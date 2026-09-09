"""Stress tests for scripts/sheet_mirror.py (RELEASE-PLAN-4.15.md 2.8): the optional spreadsheet mirror.

Offline only: every gateway call goes through a fake `opener`, exactly as SheetMirrorTests in
tests/test_runtime.py does. No network, Kolo, Gmail, Sheets, or pod access. Passing cases stay as regular
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import kolo_safe
import sheet_mirror


class FakeGateway:
    """A recording, always-succeeding gateway (same shape as SheetMirrorTests.FakeGateway)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.tabs = {"Customers": 1, "This week": 2, "Price cards": 3, "Facts": 4}

    def __call__(self, request, timeout=30):
        method, url = request.get_method(), request.full_url
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append((method, url, body))
        if method == "POST" and url.endswith("/spreadsheets"):
            reply = {"spreadsheetId": "SHEET1", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/SHEET1/edit",
                     "sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()]}
        elif method == "GET":
            reply = {"spreadsheetId": "SHEET1", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/SHEET1/edit",
                     "properties": {"title": "test"}, "sheets": [{"properties": {"title": t, "sheetId": i}} for t, i in self.tabs.items()]}
        else:
            reply = {"ok": True}
        raw = json.dumps(reply).encode("utf-8")

        class Response(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return Response(raw)


class FlakyGateway(FakeGateway):
    """Fails with a transient error on the Nth HTTP call (1-indexed), succeeds on every other call."""

    def __init__(self, fail_on_call_index: int) -> None:
        super().__init__()
        self.fail_on = fail_on_call_index

    def __call__(self, request, timeout=30):
        if len(self.calls) + 1 == self.fail_on:
            method, url = request.get_method(), request.full_url
            body = json.loads(request.data.decode("utf-8")) if request.data else None
            self.calls.append((method, url, body))
            raise OSError("sheets PUT failed: simulated transient 500")
        return super().__call__(request, timeout)


def _workspace(directory: str, records: list[dict[str, Any]]) -> Path:
    ws = Path(directory) / "ws"
    (ws / "estimate-desk" / "records").mkdir(parents=True)
    (ws / "estimate-desk" / "shop-profile.json").write_text(
        json.dumps({"schema_version": 1, "shop": {"name": "Lomelino Jewelry"}}), encoding="utf-8")
    for record in records:
        (ws / "estimate-desk" / "records" / f"{record['estimate_id']}.json").write_text(json.dumps(record), encoding="utf-8")
    return ws


class PartialTabWriteOnGatewayFailureTests(unittest.TestCase):
    """Defect: push() rewrites the four tabs one at a time with no retry and no atomicity across them. A
    transient failure partway through (the exact "HTTP 500 once then would succeed" scenario this stress
    test was asked to probe) leaves the tabs that already got their PUT holding brand-new data while the
    tabs after the failure still hold the previous push's stale data - a spreadsheet that momentarily
    contradicts itself (e.g. "Customers" says booked, "This week" still shows the old schedule) with nothing
    in the journaled result distinguishing "wrote nothing" from "wrote half the tabs". It self-heals on the
    next successful push (the digest was not saved), but until then the counter/staff view is inconsistent.
    """

    def test_a_failed_write_leaves_no_tab_half_new(self) -> None:
        """Since 4.15.1 the four tabs go in one batched write: a transient failure writes nothing, and the
        retry inside the call absorbs a single hiccup, so the sheet never contradicts itself."""
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-partial000000000", "status": "estimate_sent", "proposed_price": 100.0,
                      "route": {"recipient": "A B <a@example.net>"}, "specification": {"piece_type": "ring"}}
            ws = _workspace(d, [record])
            sheet_mirror.setup(ws, url=None, token="tok", opener=FakeGateway())
            # One hiccup on the write: the call's own retry lands it, all four tabs at once.
            gateway = FlakyGateway(fail_on_call_index=1)
            result = sheet_mirror.push(ws, token="tok", opener=gateway)
            self.assertTrue(result["pushed"], result)
            batches = [c for c in gateway.calls if c[0] == "POST" and c[1].endswith("values:batchUpdate")]
            self.assertEqual(len(batches), 2, "one failed attempt, one that landed")
            self.assertEqual({d["range"] for d in batches[-1][2]["data"]}, {f"'{t}'!A1" for t in sheet_mirror.TABS})
            # Two failures in a row: nothing is written, the result says so, and the next push starts over.
            gateway = FlakyGateway(fail_on_call_index=1)
            gateway.fail_on = 1
            calls_before = 0

            class TwiceFlaky(FakeGateway):
                def __call__(self, request, timeout=30):
                    if len(self.calls) < 2:
                        self.calls.append((request.get_method(), request.full_url, None))
                        raise OSError("sheets POST failed: simulated 500")
                    return super().__call__(request, timeout)

            twice = TwiceFlaky()
            result = sheet_mirror.push(ws, token="tok", opener=twice, force=True)
            self.assertFalse(result["pushed"])
            self.assertEqual(result["tabs_written"], [])
            self.assertFalse(result["partial"])

    def test_a_full_success_writes_all_four_tabs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-full0000000000", "status": "estimate_sent", "proposed_price": 100.0,
                      "route": {"recipient": "A B <a@example.net>"}, "specification": {"piece_type": "ring"}}
            ws = _workspace(d, [record])
            gateway = FakeGateway()
            sheet_mirror.setup(ws, url=None, token="tok", opener=gateway)
            result = sheet_mirror.push(ws, token="tok", opener=gateway)
            self.assertTrue(result["pushed"])
            puts = {d["range"].replace("!A1", "!A:Z") for c in gateway.calls if c[0] == "POST" and c[1].endswith("values:batchUpdate") for d in c[2]["data"]}
            self.assertEqual(puts, {f"'{t}'!A:Z" for t in sheet_mirror.TABS})


class DisplayNameWithAnEmbeddedQuoteTests(unittest.TestCase):
    """Defect: kolo_safe._sender_display()'s regex `"?([^"<]*?)"?\\s*<([^>]+)>` excludes the `"` character
    from the name group entirely, so a display name carrying an embedded quoted nickname (a real-world
    header shape, e.g. `John "JJ" Smith <john@example.com>`) fails to match at all, and the function falls
    back to returning the WHOLE raw header - email address and angle brackets included - as the "name".
    sheet_mirror.rows_for() feeds this straight into the Customers/This week/Price cards tabs' name column,
    so the counter sees raw header noise (including the customer's email address a second time) instead of
    a clean name. A plain comma in a name (no embedded quote) is handled correctly, so this is specifically
    about the embedded-quote case.
    """

    def test_an_embedded_quoted_nickname_still_yields_a_clean_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-quotename0000000", "status": "awaiting_specs",
                      "route": {"recipient": 'John "JJ" Smith <john@example.net>'}}
            ws = _workspace(d, [record])
            rows = sheet_mirror.rows_for(ws)
            name = rows["Customers"][1][0]
            self.assertNotIn("<", name, "the raw header (with the address in angle brackets) leaked into the name column")
            self.assertIn("John", name)

    test_an_embedded_quoted_nickname_still_yields_a_clean_display_name = test_an_embedded_quoted_nickname_still_yields_a_clean_display_name

    def test_a_comma_in_the_name_is_handled_correctly(self) -> None:
        self.assertEqual(kolo_safe._sender_display("Pat Doe, Jr. <pat@example.net>"), "Pat Doe, Jr.")


class PassingSheetMirrorBehaviourTests(unittest.TestCase):
    """A grab-bag of edge cases sheet_mirror.py gets right, kept here as a record of what was checked."""

    def test_a_record_with_no_specification_falls_back_to_their_piece(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-nospec00000000000", "status": "awaiting_specs",
                      "route": {"recipient": "Pat Doe <pat@example.net>"}}
            ws = _workspace(d, [record])
            row = sheet_mirror.rows_for(ws)["Customers"][1]
            self.assertEqual(row[0], "Pat Doe")
            self.assertEqual(row[2], "their piece")

    def test_a_record_closed_as_not_an_inquiry_is_never_listed(self) -> None:
        """Live 9 Sep: vendor and personal mail opened records that triage closed, and the sheet listed them as customers."""
        import sheet_mirror
        vendor = {"estimate_id": "jed-00000000000000aa", "status": "dormant", "route": {"recipient": "Sales Bot <sales@vendor.example>", "thread_id": "t1"},
                  "retirement": {"reason": "not_an_inquiry", "note": "triage: vendor_or_marketing"}}
        withdrew = {"estimate_id": "jed-00000000000000bb", "status": "dormant", "route": {"recipient": "Pat Doe <pat@example.net>", "thread_id": "t2"},
                    "retirement": {"reason": "customer_withdrew"}, "specification": {"piece_type": "ring"}}
        self.assertFalse(sheet_mirror.listed(vendor))
        self.assertTrue(sheet_mirror.listed(withdrew))
        self.assertTrue(sheet_mirror.listed({"estimate_id": "jed-00000000000000cc", "status": "awaiting_specs", "route": {"recipient": "x@y.z"}}))
        for reason in ("test_artifact", "created_in_error", "duplicate_of_another_thread"):
            self.assertFalse(sheet_mirror.listed({**vendor, "retirement": {"reason": reason}}), reason)

    def test_rows_for_skips_a_vendor_record_end_to_end(self) -> None:
        import sheet_mirror
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            root = ws / "estimate-desk" / "records"
            root.mkdir(parents=True)
            (root / "jed-00000000000000aa.json").write_text(json.dumps({
                "estimate_id": "jed-00000000000000aa", "status": "dormant", "route": {"recipient": "Sales Bot <sales@vendor.example>", "thread_id": "t1"},
                "retirement": {"reason": "not_an_inquiry", "note": "triage: vendor_or_marketing"}}), encoding="utf-8")
            (root / "jed-00000000000000bb.json").write_text(json.dumps({
                "estimate_id": "jed-00000000000000bb", "status": "awaiting_specs", "route": {"recipient": "Pat Doe <pat@example.net>", "thread_id": "t2"},
                "specification": {"piece_type": "ring"}}), encoding="utf-8")
            rows = sheet_mirror.rows_for(ws)
            self.assertEqual([r[1] for r in rows["Customers"][1:]], ["pat@example.net"], rows["Customers"])

    def test_the_cost_sheet_lists_every_cost_line_of_every_price_card(self) -> None:
        """The owner, 9 Sep: the full cost breakdown in the spreadsheet."""
        import sheet_mirror
        sheet = {"metal_lines": [{"metal": "18K white gold", "quantity_grams": 9.5, "unit_cost": 65.0}],
                 "stone_lines": [{"stone": "lab-grown sapphire", "quantity": 5.0, "unit_cost": 300.0}],
                 "labor_lines": [{"task": "bench labor", "hours": 3.5, "rate": 90.0}],
                 "other_hard_cost_lines": [{"label": "casting", "total_cost": 120.0}], "hard_cost_total": 2552.5}
        lines = sheet_mirror.cost_lines(sheet, 5105.0)
        self.assertEqual(lines[0], ["metal", "18K white gold", "9.5 g", "$65.00/g", "$617.50"])
        self.assertEqual(lines[1], ["stones", "lab-grown sapphire", "5 ct", "$300.00/ct", "$1,500.00"])
        self.assertEqual(lines[2], ["labor", "bench labor", "3.5 h", "$90.00/h", "$315.00"])
        self.assertEqual(lines[3], ["fee", "casting", "", "", "$120.00"])
        self.assertEqual(lines[4], ["hard cost total", "", "", "", "$2,552.50"])
        self.assertEqual(lines[5], ["quote", "markup 2.00x", "", "", "$5,105.00"])
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            root = ws / "estimate-desk" / "records"
            root.mkdir(parents=True)
            (root / "jed-00000000000000cc.json").write_text(json.dumps({
                "estimate_id": "jed-00000000000000cc", "status": "estimate_sent", "route": {"recipient": "Pat Doe <pat@example.net>", "thread_id": "t3"},
                "specification": {"piece_type": "ring"}, "proposed_price": 5105.0, "internal_cost_sheet": sheet}), encoding="utf-8")
            rows = sheet_mirror.rows_for(ws)
            self.assertEqual(rows["Cost sheet"][0], sheet_mirror.HEADERS["Cost sheet"])
            self.assertEqual(len(rows["Cost sheet"]), 7, rows["Cost sheet"])
            self.assertEqual(rows["Cost sheet"][1][1], "Pat Doe")
            self.assertEqual({r[3] for r in rows["Cost sheet"][1:]}, {"metal", "stones", "labor", "fee", "hard cost total", "quote"})
            self.assertTrue(all(r[9] == "jed-00000000000000cc" for r in rows["Cost sheet"][1:]))

    def test_a_dormant_record_shows_closed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-dormant00000000000", "status": "dormant",
                      "route": {"recipient": "Pat Doe <pat@example.net>"}}
            ws = _workspace(d, [record])
            row = sheet_mirror.rows_for(ws)["Customers"][1]
            self.assertEqual(row[3], "closed")

    def test_a_booking_in_the_past_is_excluded_from_this_week_a_future_one_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            now = datetime.now(timezone.utc)
            past = {"estimate_id": "jed-past000000000000", "status": "appointment_booked",
                    "route": {"recipient": "A <a@example.net>"},
                    "appointment_booked": {"confirmed_start": (now - timedelta(days=2)).isoformat()}}
            future = {"estimate_id": "jed-future0000000000", "status": "appointment_booked",
                      "route": {"recipient": "B <b@example.net>"},
                      "appointment_booked": {"confirmed_start": (now + timedelta(days=2)).isoformat()}}
            ws = _workspace(d, [past, future])
            week = sheet_mirror.rows_for(ws)["This week"]
            names = [row[1] for row in week[1:]]
            self.assertEqual(names, ["B"])

    def test_estimate_history_with_two_sent_estimates_produces_two_price_cards_plus_the_current(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {
                "estimate_id": "jed-history00000000000", "status": "estimate_sent", "proposed_price": 300.0,
                "route": {"recipient": "A <a@example.net>"}, "specification": {"piece_type": "ring"},
                "estimate_history": [
                    {"specification": {"piece_type": "ring"}, "proposed_price": 100.0, "updated_at": "2026-09-01T00:00:00+00:00"},
                    {"specification": {"piece_type": "ring"}, "proposed_price": 200.0, "updated_at": "2026-09-05T00:00:00+00:00"},
                ],
            }
            ws = _workspace(d, [record])
            cards = sheet_mirror.rows_for(ws)["Price cards"]
            self.assertEqual(len(cards) - 1, 3)
            prices = {row[3] for row in cards[1:]}
            self.assertEqual(prices, {"$100.00", "$200.00", "$300.00"})

    def test_a_corrupt_state_file_is_recovered_from_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-corrupt0000000000", "status": "awaiting_specs",
                      "route": {"recipient": "A <a@example.net>"}}
            ws = _workspace(d, [record])
            gateway = FakeGateway()
            sheet_mirror.setup(ws, url=None, token="tok", opener=gateway)
            state_path = ws / "estimate-desk" / "run-work" / "sheet-mirror.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text("{not valid json", encoding="utf-8")
            result = sheet_mirror.push(ws, token="tok", opener=gateway)
            self.assertTrue(result["pushed"])

    def test_sheet_id_parses_a_bare_id_a_full_url_and_rejects_junk(self) -> None:
        bare = "1xHcGuYORewIu9rEjxR6pY1oWwDtosrUuz-mjuUDaUNA"
        url = f"https://docs.google.com/spreadsheets/d/{bare}/edit?gid=0#gid=0"
        self.assertEqual(sheet_mirror.sheet_id_from(bare), bare)
        self.assertEqual(sheet_mirror.sheet_id_from(url), bare)
        with self.assertRaises(ValueError):
            sheet_mirror.sheet_id_from("too-short")
        with self.assertRaises(ValueError):
            sheet_mirror.sheet_id_from("")

    def test_two_hundred_records_stays_fast(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            records = [{"estimate_id": f"jed-perf{i:012d}", "status": "awaiting_specs",
                       "route": {"recipient": f"Cust {i} <c{i}@example.net>"},
                       "specification": {"piece_type": "ring", "metal": "14k yellow gold"}} for i in range(200)]
            ws = _workspace(d, records)
            started = time.monotonic()
            rows = sheet_mirror.rows_for(ws)
            elapsed = time.monotonic() - started
            self.assertEqual(len(rows["Customers"]) - 1, 200)
            self.assertLess(elapsed, 5.0, "200 records should mirror in well under 5s offline")

    def test_values_with_commas_and_newlines_survive_the_round_trip_as_structured_data(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-messy00000000000", "status": "awaiting_specs",
                      "route": {"recipient": "Pat Doe, Jr. <pat@example.net>"},
                      "specification": {"piece_type": "ring", "notes": "line1\nline2, with a note"}}
            ws = _workspace(d, [record])
            row = sheet_mirror.rows_for(ws)["Customers"][1]
            self.assertEqual(row[0], "Pat Doe, Jr.")

    def test_customer_with_only_an_address_falls_back_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            record = {"estimate_id": "jed-addronly0000000000", "status": "awaiting_specs",
                      "route": {"recipient": "pat@example.net"}}
            ws = _workspace(d, [record])
            row = sheet_mirror.rows_for(ws)["Customers"][1]
            self.assertEqual(row[0], "pat@example.net")
            self.assertEqual(row[1], "pat@example.net")

    def test_ledger_rows_for_an_estimate_id_with_no_record_are_simply_never_visited(self) -> None:
        import ledger
        with tempfile.TemporaryDirectory() as d:
            ws = _workspace(d, [])
            desk = ws / "estimate-desk"
            ledger.absorb(desk, "jed-orphan0000000000", {"metal_karat": 14}, "m1", "14k please", "")
            rows = sheet_mirror.rows_for(ws)
            self.assertEqual(rows["Facts"], [sheet_mirror.HEADERS["Facts"]], "no crash, no orphan row")


if __name__ == "__main__":
    unittest.main()
