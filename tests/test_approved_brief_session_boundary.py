"""The chat session never repairs or resubmits approval cards."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ApprovedBriefSessionBoundaryTests(unittest.TestCase):
    def test_empty_delivered_payload_still_belongs_to_the_watcher(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        boundary = skill.split("### Approved briefs: the desk runs them; you do nothing", 1)[1]
        self.assertIn("empty or incomplete execution payload", boundary)
        self.assertIn("watcher reconstructs the command", boundary)
        self.assertIn("never call `kolo request-approval`", boundary)
        self.assertIn("Do not ask the owner for an", boundary)
        self.assertIn("run the exact `desk-answer` line", boundary)
        self.assertIn("Never turn the owner's words into a manual card revision", boundary)


if __name__ == "__main__":
    unittest.main()
