from __future__ import annotations

import json
import os
import base64
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import approval_guard
import inbox_claim
import gmail_reply
import kolo_safe
import validate_profile


def valid_profile() -> dict:
    return {
        "schema_version": 1,
        "shop": {
            "mode": "retailer",
            "approver_email": "owner@example.com",
            "outbound_mailbox": "sales@example.com",
        },
        "autonomy": {"trust_stage": 1},
        "pricing": {"markup_multiplier": 1.25},
        "scheduling": {"timezone": "America/Los_Angeles"},
    }


class ProfileTests(unittest.TestCase):
    def test_valid_profile(self) -> None:
        self.assertEqual(validate_profile.validate_profile(valid_profile()), [])

    def test_percentage_string_is_rejected(self) -> None:
        profile = valid_profile()
        profile["pricing"]["markup_multiplier"] = "25%"
        errors = validate_profile.validate_profile(profile)
        self.assertTrue(any("markup_multiplier" in error for error in errors))

    def test_at_cost_is_rejected(self) -> None:
        profile = valid_profile()
        profile["pricing"]["markup_multiplier"] = 1.0
        self.assertTrue(validate_profile.validate_profile(profile))


class ApprovalTests(unittest.TestCase):
    def state(self) -> dict:
        return {
            "estimate_id": "jed-0123456789abcdef",
            "route": {
                "channel": "gmail",
                "recipient": "customer@example.com",
                "thread_id": "thread-1",
                "mailbox": "sales@example.com",
            },
            "specification": {"piece": "ring", "metal": "18k yellow gold"},
            "proposed_price": 4200,
        }

    def test_changed_recipient_invalidates_approval(self) -> None:
        current = self.state()
        approved = approval_guard.build_request(current)
        approved["approval_status"] = "approved"
        approved["owner_approved_price"] = 4300
        current["route"]["recipient"] = "attacker@example.com"
        valid, errors = approval_guard.verify_execution(approved, current)
        self.assertFalse(valid)
        self.assertTrue(any("changed" in error for error in errors))

    def test_owner_price_may_differ_without_changing_route_or_spec(self) -> None:
        current = self.state()
        approved = approval_guard.build_request(current)
        approved["approval_status"] = "approved"
        approved["owner_approved_price"] = 4500
        valid, errors = approval_guard.verify_execution(approved, current)
        self.assertTrue(valid, errors)

    def test_malformed_estimate_id_is_rejected_during_verification(self) -> None:
        current = self.state()
        approved = approval_guard.build_request(current)
        approved["approval_status"] = "approved"
        approved["owner_approved_price"] = 4500
        current["estimate_id"] = approved["estimate_id"] = "customer-ring"
        with self.assertRaises(ValueError):
            approval_guard.verify_execution(approved, current)


class SafeCliTests(unittest.TestCase):
    def test_untrusted_details_remain_one_argv_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.json"
            attack = '$(touch /tmp/should-not-run) `whoami` "; echo bad'
            path.write_text(json.dumps({"customer_text": attack}), encoding="utf-8")
            argv = kolo_safe.build_request_approval(
                "jed-0123456789abcdef", path, "agent:main:kolo:test-session"
            )
            details_value = argv[argv.index("--details") + 1]
            self.assertEqual(json.loads(details_value)["customer_text"], attack)
            self.assertNotIn("sh", argv[:1])

    def test_runner_disables_shell(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        kolo_safe.run_command(["kolo", "--help"], runner=runner)
        self.assertFalse(runner.call_args.kwargs["shell"])

    def test_notification_contains_only_opaque_id(self) -> None:
        argv = kolo_safe.build_notify_owner("jed-0123456789abcdef")
        self.assertIn("JED-0123456789ABCDEF", argv[-1])
        self.assertNotIn("ring", argv[-1].lower())

    def test_invalid_session_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                kolo_safe.build_request_approval(
                    "jed-0123456789abcdef", path, "not a session key"
                )

    def test_log_action_has_idempotency_and_safe_json_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            attack = '$(touch /tmp/should-not-run) `whoami`'
            path.write_text(json.dumps({"note": attack}), encoding="utf-8")
            argv = kolo_safe.build_log_action(
                "Estimate sent",
                "Owner-approved estimate sent through Gmail",
                "estimate_sent",
                "jed-0123456789abcdef:estimate_sent",
                path,
            )
            self.assertEqual(
                argv[argv.index("--idempotency-key") + 1],
                "jed-0123456789abcdef:estimate_sent",
            )
            self.assertEqual(json.loads(argv[argv.index("--details") + 1])["note"], attack)


class InboxClaimTests(unittest.TestCase):
    def test_default_claim_root_is_absolute_workspace_path(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"OPENCLAW_WORKSPACE": "/tmp/kolo-workspace"}):
            self.assertEqual(
                inbox_claim.default_claim_root(),
                Path("/tmp/kolo-workspace/estimate-desk/inbox-claims").resolve(),
            )

    def test_only_one_concurrent_claim_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            results: list[bool] = []
            barrier = threading.Barrier(2)

            def attempt() -> None:
                barrier.wait()
                acquired, _ = inbox_claim.acquire(root, "gmail-message-1")
                results.append(acquired)

            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(results), [False, True])

    def test_claim_token_required_to_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            acquired, state = inbox_claim.acquire(root, "gmail-message-2")
            self.assertTrue(acquired)
            with self.assertRaises(ValueError):
                inbox_claim.finish(root, "gmail-message-2", "wrong", "processed")
            finished = inbox_claim.finish(
                root, "gmail-message-2", state["claim_token"], "processed"
            )
            self.assertEqual(finished["status"], "processed")


class GmailReplyTests(unittest.TestCase):
    def route(self) -> dict:
        return {
            "channel": "gmail",
            "mailbox": "sales@example.com",
            "recipient": "customer@example.net",
            "gmail_message_id": "18d0123456789abc",
            "thread_id": "18d0thread1234567",
            "original_message_id": "<original@example.net>",
            "original_subject": "Custom ring estimate",
            "references": ["<earlier@example.net>"],
        }

    def decode(self, raw: str) -> str:
        padding = "=" * (-len(raw) % 4)
        return base64.urlsafe_b64decode(raw + padding).decode("utf-8")

    def test_reply_stays_in_original_thread(self) -> None:
        payload = gmail_reply.build_reply(self.route(), "Approved estimate: $4,200")
        message = self.decode(payload["raw"])
        self.assertEqual(payload["threadId"], "18d0thread1234567")
        self.assertIn("Subject: Re: Custom ring estimate", message)
        self.assertIn("In-Reply-To: <original@example.net>", message)
        self.assertIn("References: <earlier@example.net> <original@example.net>", message)

    def test_missing_original_thread_is_rejected(self) -> None:
        route = self.route()
        del route["thread_id"]
        with self.assertRaises(ValueError):
            gmail_reply.build_reply(route, "Approved estimate: $4,200")

    def test_missing_original_message_id_is_rejected(self) -> None:
        route = self.route()
        del route["original_message_id"]
        with self.assertRaises(ValueError):
            gmail_reply.build_reply(route, "Approved estimate: $4,200")


if __name__ == "__main__":
    unittest.main()
