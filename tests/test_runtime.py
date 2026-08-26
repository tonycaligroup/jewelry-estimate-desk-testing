from __future__ import annotations

import json
import os
import base64
import io
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
import customer_content_guard
import inbox_claim
import inbox_monitor
import gmail_reply
import gmail_route
import gmail_classify
import kolo_safe
import route_ownership
import validate_profile


def valid_profile() -> dict:
    return {
        "schema_version": 1,
        "shop": {
            "mode": "retailer",
            "approver_email": "owner@example.com",
            "outbound_mailbox": "sales@example.com",
            "address": {
                "street": "123 Main St",
                "city": "Los Angeles",
                "state": "CA",
                "zip": "90001"
            },
            "website": "https://example.com"
        },
        "autonomy": {"trust_stage": 1},
        "pricing": {"markup_multiplier": 1.25},
        "scheduling": {"timezone": "America/Los_Angeles"},
    }


class ProfileTests(unittest.TestCase):
    def test_valid_profile(self) -> None:
        result = validate_profile.validate_profile(valid_profile())
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["ready"])

    def test_percentage_string_is_rejected(self) -> None:
        profile = valid_profile()
        profile["pricing"]["markup_multiplier"] = "25%"
        result = validate_profile.validate_profile(profile)
        self.assertTrue(any("markup_multiplier" in error for error in result["errors"]))
        self.assertFalse(result["ready"])

    def test_at_cost_is_rejected(self) -> None:
        profile = valid_profile()
        profile["pricing"]["markup_multiplier"] = 1.0
        result = validate_profile.validate_profile(profile)
        self.assertTrue(result["errors"])
        self.assertFalse(result["ready"])


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

    def test_customer_reply_notifies_owner_without_customer_data(self) -> None:
        argv = kolo_safe.build_notify_owner(
            "jed-0123456789abcdef", "customer-replied"
        )
        self.assertEqual(argv[:2], ["kolo", "notify-owner"])
        self.assertEqual(
            argv[-1],
            "Customer replied on estimate JED-0123456789ABCDEF. Open Kolo to review.",
        )
        self.assertNotIn("@", argv[-1])

    def test_unknown_owner_notification_event_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            kolo_safe.build_notify_owner(
                "jed-0123456789abcdef", "customer-name-from-inbox"
            )

    def test_generic_monitor_notification_contains_no_customer_or_estimate_data(self) -> None:
        argv = kolo_safe.build_notify_monitor("system-actionable")
        self.assertEqual(argv[:2], ["kolo", "notify-owner"])
        self.assertNotIn("@", argv[-1])
        self.assertNotIn("jed-", argv[-1].lower())

    def test_unknown_monitor_notification_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            kolo_safe.build_notify_monitor("customer@example.com")

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

    def test_duplicate_claim_is_successful_noop_with_existing_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            acquired, state = inbox_claim.acquire(root, "gmail-message-duplicate")
            self.assertTrue(acquired)
            duplicate, duplicate_state = inbox_claim.acquire(
                root, "gmail-message-duplicate"
            )
            self.assertFalse(duplicate)
            self.assertEqual(duplicate_state["status"], "processing")
            self.assertEqual(duplicate_state["claim_token"], state["claim_token"])

    def test_duplicate_claim_cli_returns_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "--root",
                str(Path(directory) / "claims"),
                "claim",
                "--message-id",
                "gmail-message-cli",
            ]
            with unittest.mock.patch("sys.stdout", new=io.StringIO()):
                self.assertEqual(inbox_claim.main(argv), 0)
                self.assertEqual(inbox_claim.main(argv), 0)

    def test_legacy_claim_is_migrated_without_losing_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            path = inbox_claim.claim_path(root, "legacy-message")
            path.mkdir(parents=True)
            legacy = {
                "message_id_sha256": inbox_claim.claim_key("legacy-message"),
                "claim_token": "legacy-token",
                "status": "processed",
                "claimed_at": "2026-08-25T00:00:00+00:00",
                "finished_at": "2026-08-25T00:01:00+00:00",
            }
            (path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
            migrated = inbox_claim.read_state(path)
            self.assertEqual(migrated["schema_version"], 1)
            self.assertEqual(migrated["status"], "processed")

    def test_notification_write_ahead_crash_becomes_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            _, state = inbox_claim.acquire(root, "gmail-message-notify")
            pending = inbox_claim.begin_notification(
                root,
                "gmail-message-notify",
                state["claim_token"],
                "customer_replied:jed-0123456789abcdef:gmail-message-notify",
            )
            self.assertEqual(pending["owner_notification"]["status"], "pending")
            reconciled = inbox_claim.reconcile_notification(
                root, "gmail-message-notify"
            )
            self.assertEqual(reconciled["owner_notification"]["status"], "uncertain")
            with self.assertRaises(ValueError):
                inbox_claim.begin_notification(
                    root,
                    "gmail-message-notify",
                    state["claim_token"],
                    "customer_replied:jed-0123456789abcdef:gmail-message-notify",
                )

    def test_notification_allows_only_one_pre_delivery_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            _, state = inbox_claim.acquire(root, "gmail-message-retry")
            key = "system_actionable:gmail-message-retry"
            inbox_claim.begin_notification(
                root, "gmail-message-retry", state["claim_token"], key
            )
            inbox_claim.finish_notification(
                root,
                "gmail-message-retry",
                state["claim_token"],
                "failed_pre_delivery",
            )
            retried = inbox_claim.begin_notification(
                root, "gmail-message-retry", state["claim_token"], key
            )
            self.assertEqual(retried["owner_notification"]["attempts"], 2)
            inbox_claim.finish_notification(
                root,
                "gmail-message-retry",
                state["claim_token"],
                "failed_pre_delivery",
            )
            with self.assertRaises(ValueError):
                inbox_claim.begin_notification(
                    root, "gmail-message-retry", state["claim_token"], key
                )


class InboxMonitorTests(unittest.TestCase):
    def capabilities(self) -> dict:
        return {
            "gmail_after_epoch": True,
            "gmail_internal_date_ms": True,
            "gmail_complete_pagination": True,
        }

    def cron(self) -> dict:
        return {
            "name": "jed-inbox-monitor",
            "schedule": "*/5 9-17 * * 1-5",
            "timezone": "America/Los_Angeles",
            "model": "litellm-fireworks/qwen-3-7-plus",
            "fallbacks": "",
        }

    def active_root(self, directory: str) -> Path:
        root = Path(directory) / "monitor"
        inbox_monitor.prepare(root, self.capabilities(), self.cron())
        inbox_monitor.activate(root, self.cron(), 1_000)
        return root

    def test_activation_fails_closed_without_required_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capabilities = self.capabilities()
            capabilities["gmail_complete_pagination"] = False
            with self.assertRaises(ValueError):
                inbox_monitor.prepare(Path(directory), capabilities, self.cron())
            self.assertFalse((Path(directory) / "monitor-state.json").exists())

    def test_two_phase_activation_and_exact_cron_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "monitor"
            prepared = inbox_monitor.prepare(root, self.capabilities(), self.cron())
            self.assertEqual(prepared["activation_state"], "prepared")
            self.assertIsNone(prepared["activated_at_ms"])
            changed = self.cron()
            changed["fallbacks"] = "another-model"
            with self.assertRaises(ValueError):
                inbox_monitor.activate(root, changed, 1_000)
            active = inbox_monitor.activate(root, self.cron(), 1_000)
            self.assertEqual(active["activation_state"], "active")
            self.assertEqual(active["discovery_watermark_ms"], 1_000)

    def test_prepare_is_idempotent_only_for_the_exact_cron(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "monitor"
            first = inbox_monitor.prepare(root, self.capabilities(), self.cron())
            second = inbox_monitor.prepare(root, self.capabilities(), self.cron())
            self.assertEqual(second, first)
            changed = self.cron()
            changed["schedule"] = "*/10 9-17 * * 1-5"
            with self.assertRaises(ValueError):
                inbox_monitor.prepare(root, self.capabilities(), changed)

    def test_missing_or_corrupt_active_state_never_reinitializes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "monitor"
            with self.assertRaises(ValueError):
                inbox_monitor.load_monitor_state(root)
            root.mkdir()
            (root / "monitor-state.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                inbox_monitor.load_monitor_state(root)

    def test_discovery_filters_pre_activation_and_orders_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            batch = [
                {
                    "gmail_message_id": "old",
                    "thread_id": "old-thread",
                    "internal_date_ms": 999,
                },
                {
                    "gmail_message_id": "second",
                    "thread_id": "thread-a",
                    "internal_date_ms": 1_200,
                },
                {
                    "gmail_message_id": "first",
                    "thread_id": "thread-a",
                    "internal_date_ms": 1_100,
                },
                {
                    "gmail_message_id": "other",
                    "thread_id": "thread-b",
                    "internal_date_ms": 1_150,
                },
            ]
            result = inbox_monitor.discover_complete(root, batch, 1_000, 2_000)
            self.assertEqual(result["inserted"], 3)
            self.assertEqual(result["ignored_before_activation"], 1)
            self.assertEqual(inbox_monitor.next_eligible(root)["gmail_message_id"], "first")

            claim = {
                "acquired": True,
                "schema_version": 1,
                "message_id_sha256": inbox_monitor.message_key("first"),
                "claim_token": "token",
                "status": "processing",
                "claimed_at": "2026-08-25T00:00:00+00:00",
            }
            inbox_monitor.sync_claim(root, "first", claim)
            self.assertEqual(inbox_monitor.next_eligible(root)["gmail_message_id"], "other")

    def test_duplicate_terminal_claim_completes_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            inbox_monitor.discover_complete(
                root,
                [
                    {
                        "gmail_message_id": "done",
                        "thread_id": "thread-done",
                        "internal_date_ms": 1_100,
                    }
                ],
                1_000,
                2_000,
            )
            terminal = {
                "acquired": False,
                "schema_version": 1,
                "message_id_sha256": inbox_monitor.message_key("done"),
                "claim_token": "token",
                "status": "manual_review",
                "claimed_at": "2026-08-25T00:00:00+00:00",
                "finished_at": "2026-08-25T00:01:00+00:00",
                "reason_code": "uncertain_classification",
            }
            item = inbox_monitor.sync_claim(root, "done", terminal)
            self.assertEqual(item["discovery_status"], "complete")
            self.assertEqual(item["processing_status"], "manual_review")
            self.assertEqual(item["reason_code"], "uncertain_classification")

    def test_duplicate_processing_claim_keeps_queue_in_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            inbox_monitor.discover_complete(
                root,
                [
                    {
                        "gmail_message_id": "still-processing",
                        "thread_id": "thread-processing",
                        "internal_date_ms": 1_100,
                    }
                ],
                1_000,
                2_000,
            )
            duplicate = {
                "acquired": False,
                "schema_version": 1,
                "message_id_sha256": inbox_monitor.message_key("still-processing"),
                "claim_token": "token",
                "status": "processing",
                "claimed_at": "2026-08-25T00:00:00+00:00",
            }
            item = inbox_monitor.sync_claim(root, "still-processing", duplicate)
            self.assertEqual(item["discovery_status"], "pending")
            self.assertEqual(item["processing_status"], "processing")
            self.assertIsNone(inbox_monitor.next_eligible(root))

    def test_failed_enumeration_does_not_advance_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            state = inbox_monitor.load_monitor_state(root)
            self.assertEqual(state["discovery_watermark_ms"], 1_000)
            # A failed/timeout enumeration never calls discover_complete.
            self.assertEqual(
                inbox_monitor.load_monitor_state(root)["discovery_watermark_ms"],
                1_000,
            )

    def test_invalid_partial_batch_never_advances_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            batch = [
                {
                    "gmail_message_id": "valid-first",
                    "thread_id": "thread-first",
                    "internal_date_ms": 1_100,
                },
                {"gmail_message_id": "invalid-second"},
            ]
            with self.assertRaises(ValueError):
                inbox_monitor.discover_complete(root, batch, 1_000, 2_000)
            self.assertEqual(
                inbox_monitor.load_monitor_state(root)["discovery_watermark_ms"],
                1_000,
            )
            result = inbox_monitor.discover_complete(
                root,
                [
                    {
                        "gmail_message_id": "valid-first",
                        "thread_id": "thread-first",
                        "internal_date_ms": 1_100,
                    }
                ],
                1_000,
                2_000,
            )
            self.assertEqual(result["existing"], 1)

    def test_queue_claim_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            inbox_monitor.discover_complete(
                root,
                [
                    {
                        "gmail_message_id": "expected",
                        "thread_id": "thread-expected",
                        "internal_date_ms": 1_100,
                    }
                ],
                1_000,
                2_000,
            )
            mismatched = {
                "acquired": True,
                "message_id_sha256": inbox_monitor.message_key("different"),
                "status": "processing",
                "claimed_at": "2026-08-25T00:00:00+00:00",
            }
            with self.assertRaises(ValueError):
                inbox_monitor.sync_claim(root, "expected", mismatched)

    def test_stale_discovery_cannot_roll_back_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.active_root(directory)
            inbox_monitor.discover_complete(root, [], 1_000, 3_000)
            with self.assertRaises(ValueError):
                inbox_monitor.discover_complete(root, [], 1_000, 2_000)
            self.assertEqual(
                inbox_monitor.load_monitor_state(root)["discovery_watermark_ms"],
                3_000,
            )


class GmailReplyTests(unittest.TestCase):
    def route(self) -> dict:
        return {
            "channel": "gmail",
            "mailbox": "sales@example.com",
            "recipient": "customer@example.net",
            "identity_key": gmail_route.email_identity_key("customer@example.net"),
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

    def test_recipient_cannot_change_independently_of_identity(self) -> None:
        route = self.route()
        route["recipient"] = "previous-customer@example.net"
        with self.assertRaises(ValueError):
            gmail_reply.build_reply(route, "Approved estimate: $4,200")

    def test_owner_only_pricing_information_is_rejected(self) -> None:
        forbidden = (
            "Assumptions: finished weight is 8 grams",
            "Our metal cost is $42 per gram",
            "COGS is $2,100 with a 1.25 markup",
            "Bench labor rate is $125 per hour",
            "The vendor is Example Stones",
            "Our margin is 20%",
            "We use a 1.25 multiplier on the base price",
            "The bench charge for setting is $150",
            "The stone supplier is Example Gems",
            "The casting fee is $200",
            "The per-ounce price of gold is $1,800",
            "Our jeweler's fee is $500",
            "Gold is $1,800/oz",
            "Our jeweler’s charge is $500",
        )
        for body in forbidden:
            with self.subTest(body=body), self.assertRaises(ValueError):
                gmail_reply.build_reply(self.route(), body)
            with self.subTest(generic_guard=body), self.assertRaises(ValueError):
                customer_content_guard.validate_customer_text(body)

    def test_customer_safe_specification_is_allowed(self) -> None:
        allowed = (
            "The design is an 18K white gold ring with a 1.5 carat oval center. Your approved estimate is $4,200.",
            "I assume you would like to proceed with the design.",
            "We assume all measurements are in millimeters.",
            "The manufacturer provides a warranty on this stone.",
            "The vendor of this diamond is certified by GIA.",
        )
        for body in allowed:
            with self.subTest(body=body):
                customer_content_guard.validate_customer_text(body)
                payload = gmail_reply.build_reply(self.route(), body)
                self.assertEqual(payload["threadId"], self.route()["thread_id"])


class GmailRouteTests(unittest.TestCase):
    def message(
        self,
        sender: str,
        message_id: str = "<new@example.net>",
        gmail_message_id: str = "gmail-message-2",
        thread_id: str = "gmail-thread-2",
    ) -> dict:
        return {
            "id": gmail_message_id,
            "threadId": thread_id,
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": "Quote request"},
                    {"name": "Message-ID", "value": message_id},
                    {"name": "References", "value": "<first@example.net>"},
                ]
            },
        }

    def test_sender_email_not_display_name_is_identity(self) -> None:
        first = gmail_route.build_route(
            self.message("Alex Smith <alex.one@example.net>"), "sales@example.com"
        )
        second = gmail_route.build_route(
            self.message("Alex Smith <alex.two@example.net>"), "sales@example.com"
        )
        self.assertEqual(first["recipient"], "alex.one@example.net")
        self.assertEqual(second["recipient"], "alex.two@example.net")
        self.assertNotEqual(first["identity_key"], second["identity_key"])
        self.assertNotIn("Alex Smith", json.dumps(first))

    def test_second_same_name_quote_uses_second_email_and_thread(self) -> None:
        first = gmail_route.build_route(
            self.message(
                "Alex Smith <alex.one@example.net>",
                "<first@example.net>",
                "gmail-message-1",
                "gmail-thread-1",
            ),
            "sales@example.com",
        )
        second = gmail_route.build_route(
            self.message(
                "Alex Smith <alex.two@example.net>",
                "<second@example.net>",
                "gmail-message-2",
                "gmail-thread-2",
            ),
            "sales@example.com",
        )
        first_payload = gmail_reply.build_reply(first, "First estimate")
        second_payload = gmail_reply.build_reply(second, "Second estimate")
        padding = "=" * (-len(second_payload["raw"]) % 4)
        second_message = base64.urlsafe_b64decode(
            second_payload["raw"] + padding
        ).decode("utf-8")

        self.assertNotEqual(first["identity_key"], second["identity_key"])
        self.assertEqual(first_payload["threadId"], "gmail-thread-1")
        self.assertEqual(second_payload["threadId"], "gmail-thread-2")
        self.assertIn("To: alex.two@example.net", second_message)
        self.assertNotIn("alex.one@example.net", second_message)

    def test_shop_outbound_message_is_not_customer_route(self) -> None:
        with self.assertRaises(ValueError):
            gmail_route.build_route(
                self.message("Jeweler <sales@example.com>"), "sales@example.com"
            )

    def test_route_preserves_exact_source_thread_and_message(self) -> None:
        route = gmail_route.build_route(
            self.message("Customer <customer@example.net>"), "sales@example.com"
        )
        self.assertEqual(route["gmail_message_id"], "gmail-message-2")
        self.assertEqual(route["thread_id"], "gmail-thread-2")
        self.assertEqual(route["original_message_id"], "<new@example.net>")

    def test_plus_addressing_is_preserved_as_exact_identity(self) -> None:
        plus = gmail_route.build_route(
            self.message("Customer <customer+design@example.net>"),
            "sales@example.com",
        )
        plain = gmail_route.build_route(
            self.message("Customer <customer@example.net>"),
            "sales@example.com",
        )
        self.assertEqual(plus["recipient"], "customer+design@example.net")
        self.assertNotEqual(plus["identity_key"], plain["identity_key"])


class GmailClassificationTests(unittest.TestCase):
    def message(self, headers: list[dict[str, str]]) -> dict:
        return {"payload": {"headers": headers}}

    def test_google_mailer_daemon_dsn_is_deterministic(self) -> None:
        result = gmail_classify.classify(
            self.message(
                [
                    {"name": "From", "value": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"},
                    {"name": "Subject", "value": "Delivery Status Notification (Failure)"},
                    {"name": "Auto-Submitted", "value": "auto-generated"},
                    {"name": "Content-Type", "value": "multipart/report; report-type=delivery-status"},
                ]
            )
        )
        self.assertEqual(result["classification"], "dsn_candidate")

    def test_auto_reply_is_filtered_without_llm(self) -> None:
        result = gmail_classify.classify(
            self.message(
                [
                    {"name": "From", "value": "customer@example.net"},
                    {"name": "Subject", "value": "Automatic Reply: quote"},
                    {"name": "Auto-Submitted", "value": "auto-replied"},
                ]
            )
        )
        self.assertEqual(result["classification"], "auto_reply")

    def test_ordinary_message_is_not_guessed_system_noise(self) -> None:
        result = gmail_classify.classify(
            self.message(
                [
                    {"name": "From", "value": "customer@example.net"},
                    {"name": "Subject", "value": "Custom ring estimate"},
                ]
            )
        )
        self.assertEqual(result["classification"], "customer_or_uncertain")

    def test_multiple_from_addresses_are_rejected(self) -> None:
        message = self.message(
            [
                {
                    "name": "From",
                    "value": "first@example.net, second@example.net",
                },
                {"name": "Subject", "value": "Quote request"},
            ]
        )
        with self.assertRaises(ValueError):
            gmail_classify.classify(message)


class RouteOwnershipTests(unittest.TestCase):
    def route(self) -> dict:
        return {
            "thread_id": "thread-1",
            "identity_key": gmail_route.email_identity_key("customer+one@example.net"),
        }

    def record(self, status: str = "estimate_sent") -> dict:
        return {
            "schema_version": 1,
            "estimate_id": "jed-0123456789abcdef",
            "status": status,
            "route": {
                "thread_id": "thread-1",
                "identity_key": gmail_route.email_identity_key("customer+one@example.net"),
                "gmail_message_id": "initiating-message",
            },
        }

    def processed_claim(self, root: Path) -> None:
        _, state = inbox_claim.acquire(root, "initiating-message")
        inbox_claim.finish(
            root, "initiating-message", state["claim_token"], "processed"
        )

    def test_exact_route_and_initiating_claim_prove_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            self.processed_claim(root)
            result = route_ownership.decide(self.route(), [self.record()], root)
            self.assertEqual(result["decision"], "owned")
            self.assertEqual(result["estimate_id"], "jed-0123456789abcdef")

    def test_same_thread_different_email_is_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            self.processed_claim(root)
            route = self.route()
            route["identity_key"] = gmail_route.email_identity_key(
                "customer+two@example.net"
            )
            result = route_ownership.decide(route, [self.record()], root)
            self.assertEqual(result["decision"], "manual_review")
            self.assertEqual(result["reason_code"], "identity_mismatch")

    def test_two_records_for_one_thread_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            self.processed_claim(root)
            second = self.record()
            second["estimate_id"] = "jed-fedcba9876543210"
            result = route_ownership.decide(
                self.route(), [self.record(), second], root
            )
            self.assertEqual(result["decision"], "manual_review")
            self.assertEqual(result["reason_code"], "ambiguous_thread_ownership")

    def test_missing_initiating_claim_never_proves_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = route_ownership.decide(
                self.route(), [self.record()], Path(directory) / "claims"
            )
            self.assertEqual(result["decision"], "manual_review")
            self.assertEqual(result["reason_code"], "missing_initiating_claim")

    def test_terminal_record_retains_ownership_but_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            self.processed_claim(root)
            result = route_ownership.decide(
                self.route(), [self.record("dormant")], root
            )
            self.assertEqual(result["decision"], "owned_manual_review")

    def test_existing_manual_review_record_cannot_resume_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "claims"
            self.processed_claim(root)
            result = route_ownership.decide(
                self.route(), [self.record("manual_review")], root
            )
            self.assertEqual(result["decision"], "owned_manual_review")


if __name__ == "__main__":
    unittest.main()
