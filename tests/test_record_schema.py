"""RECORD-SCHEMA.md names every key the code writes on an estimate record, and nothing the code no longer writes."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "RECORD-SCHEMA.md"
WRITERS = [ROOT / "scripts" / "estimate_record.py", ROOT / "scripts" / "owner_questions.py",
           ROOT / "scripts" / "doctor.py", ROOT / "scripts" / "pipeline.py"]
WRITE_RE = re.compile(r'record(?:\.setdefault\(|\[)"([a-z_]+)"')
INITIAL = {"schema_version", "estimate_id", "status", "route", "inbound_timestamp_ms"}


def documented_keys() -> set[str]:
    keys: set[str] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `"):
            keys.update(re.findall(r"`([a-z_]+)`", line.split("|")[1]))
    return keys


def written_keys() -> set[str]:
    keys: set[str] = set()
    for path in WRITERS:
        keys.update(WRITE_RE.findall(path.read_text(encoding="utf-8")))
    return keys | INITIAL


class RecordSchemaTests(unittest.TestCase):
    def test_every_written_key_is_documented(self) -> None:
        self.assertEqual(sorted(written_keys() - documented_keys()), [])

    def test_every_documented_key_is_written(self) -> None:
        self.assertEqual(sorted(documented_keys() - written_keys()), [])

    def test_statuses_and_retirement_reasons_match_the_code(self) -> None:
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import estimate_record, route_ownership  # noqa: E401
        text = DOC.read_text(encoding="utf-8")
        for status in route_ownership.VALID_STATUSES:
            self.assertIn(f"`{status}`", text)
        for reason in estimate_record.RETIREMENT_REASONS:
            self.assertIn(f"`{reason}`", text)
