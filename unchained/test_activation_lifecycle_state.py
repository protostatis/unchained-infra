"""Durable activation and lifecycle-email safety state contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import tempfile
import unittest

from auth import Auth


class TestActivationLifecycleState(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.auth_path = os.path.join(self.tempdir.name, "auth.db")
        self.analytics_path = os.path.join(self.tempdir.name, "analytics.db")
        self.auth = Auth(self.auth_path)

    def tearDown(self):
        self.tempdir.cleanup()

    def _user(self, email: str = "member@example.test") -> dict:
        return self.auth.get_or_create_user(email, "Member", "")

    def _analytics_db(self, rows: list[tuple[float, str, str]]) -> None:
        with sqlite3.connect(self.analytics_path) as conn:
            conn.execute(
                "CREATE TABLE analytics_events (ts REAL NOT NULL, event TEXT NOT NULL, user_id TEXT)"
            )
            conn.executemany(
                "INSERT INTO analytics_events (ts, event, user_id) VALUES (?, ?, ?)",
                rows,
            )

    def test_consumed_install_token_records_lifetime_activation_once(self):
        user = self._user()
        token = self.auth.create_install_token(user["user_id"], user["api_key"])

        preview = self.auth.validate_install_token(token, consume=False)
        self.assertIsNotNone(preview)
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"]),
            {
                "first_install_bootstrap_at": None,
                "last_install_bootstrap_at": None,
                "install_bootstrap_count": 0,
            },
        )

        consumed = self.auth.consume_install_token_for_bootstrap(token)
        self.assertIsNotNone(consumed)
        state = self.auth.get_install_activation(user["user_id"])
        self.assertIsNotNone(state)
        self.assertIsNotNone(state["first_install_bootstrap_at"])
        self.assertEqual(state["first_install_bootstrap_at"], state["last_install_bootstrap_at"])
        self.assertEqual(state["install_bootstrap_count"], 1)

        self.assertIsNone(self.auth.consume_install_token_for_bootstrap(token))
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"])["install_bootstrap_count"],
            1,
        )

    def test_bootstrap_consume_is_atomic_under_concurrency(self):
        user = self._user()
        token = self.auth.create_install_token(user["user_id"], user["api_key"])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _index: self.auth.consume_install_token_for_bootstrap(token),
                    range(8),
                )
            )

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"])["install_bootstrap_count"],
            1,
        )

    def test_preview_generic_consume_and_orphan_do_not_stamp_activation(self):
        user = self._user()
        preview_token = self.auth.create_install_token(user["user_id"], user["api_key"])
        self.assertIsNotNone(self.auth.validate_install_token(preview_token, consume=False))
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"])["install_bootstrap_count"],
            0,
        )

        generic_token = self.auth.create_install_token(user["user_id"], user["api_key"])
        self.assertIsNotNone(self.auth.validate_install_token(generic_token, consume=True))
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"])["install_bootstrap_count"],
            0,
        )

        orphan = self.auth.create_install_token("missing-user", "orphan-key")
        self.assertIsNone(self.auth.consume_install_token_for_bootstrap(orphan))

    def test_distinct_bootstraps_keep_first_timestamp_and_increment_count(self):
        user = self._user()
        first = self.auth.create_install_token(user["user_id"], user["api_key"])
        second = self.auth.create_install_token(user["user_id"], user["api_key"])
        self.assertIsNotNone(self.auth.consume_install_token_for_bootstrap(first))
        first_state = self.auth.get_install_activation(user["user_id"])
        self.assertIsNotNone(self.auth.consume_install_token_for_bootstrap(second))
        second_state = self.auth.get_install_activation(user["user_id"])

        self.assertEqual(
            second_state["first_install_bootstrap_at"],
            first_state["first_install_bootstrap_at"],
        )
        self.assertGreaterEqual(
            second_state["last_install_bootstrap_at"],
            first_state["last_install_bootstrap_at"],
        )
        self.assertEqual(second_state["install_bootstrap_count"], 2)

    def test_backfill_is_idempotent_and_ignores_unknown_accounts(self):
        user = self._user()
        self._analytics_db(
            [
                (100.0, "install_bootstrap_success", user["user_id"]),
                (120.0, "install_bootstrap_success", user["user_id"]),
                (130.0, "chat_message_send", user["user_id"]),
                (140.0, "install_bootstrap_success", "unknown-user"),
            ]
        )

        self.assertEqual(self.auth.backfill_install_bootstrap_markers(self.analytics_path), 1)
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"]),
            {
                "first_install_bootstrap_at": 100.0,
                "last_install_bootstrap_at": 120.0,
                "install_bootstrap_count": 2,
            },
        )

        self.assertEqual(self.auth.backfill_install_bootstrap_markers(self.analytics_path), 1)
        self.assertEqual(
            self.auth.get_install_activation(user["user_id"])["install_bootstrap_count"],
            2,
        )

    def test_lifecycle_delivery_is_suppression_aware_and_one_shot(self):
        member = self._user()
        campaign = "dormant_install_reactivation_v1"

        self.assertTrue(
            self.auth.reserve_lifecycle_email(member["user_id"], campaign, now=100.0)
        )
        self.assertFalse(
            self.auth.reserve_lifecycle_email(member["user_id"], campaign, now=101.0)
        )
        self.assertTrue(
            self.auth.mark_lifecycle_email_delivery(
                member["user_id"],
                campaign,
                "smtp_accepted",
                now=102.0,
            )
        )
        self.assertFalse(
            self.auth.mark_lifecycle_email_delivery(
                member["user_id"], campaign, "failed", now=103.0
            )
        )
        self.assertEqual(
            self.auth.get_lifecycle_email_delivery(member["user_id"], campaign),
            {
                "status": "smtp_accepted",
                "created_at": 100.0,
                "updated_at": 102.0,
            },
        )

        suppressed = self._user("suppressed@example.test")
        self.auth.suppress_lifecycle_email(suppressed["user_id"], "user_request", now=110.0)
        self.assertTrue(self.auth.is_lifecycle_email_suppressed(suppressed["user_id"]))
        self.assertFalse(
            self.auth.reserve_lifecycle_email(suppressed["user_id"], campaign, now=111.0)
        )

    def test_activation_or_suppression_cancels_reserved_delivery(self):
        campaign = "dormant_install_reactivation_v1"
        activated = self._user("activated@example.test")
        token = self.auth.create_install_token(
            activated["user_id"], activated["api_key"]
        )
        self.assertIsNotNone(self.auth.consume_install_token_for_bootstrap(token))
        self.assertFalse(
            self.auth.reserve_lifecycle_email(activated["user_id"], campaign, now=100.0)
        )

        member = self._user("reserved@example.test")
        self.assertTrue(
            self.auth.reserve_lifecycle_email(member["user_id"], campaign, now=101.0)
        )
        self.auth.suppress_lifecycle_email(member["user_id"], "user_request", now=102.0)
        self.assertFalse(
            self.auth.recheck_lifecycle_email_reservation(
                member["user_id"], campaign, now=103.0
            )
        )
        self.assertEqual(
            self.auth.get_lifecycle_email_delivery(member["user_id"], campaign)["status"],
            "cancelled",
        )

    def test_concurrent_campaign_reservation_is_at_most_once(self):
        member = self._user()
        campaign = "dormant_install_reactivation_v1"
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _index: self.auth.reserve_lifecycle_email(
                        member["user_id"], campaign
                    ),
                    range(8),
                )
            )
        self.assertEqual(sum(results), 1)
        self.assertTrue(
            self.auth.recheck_lifecycle_email_reservation(member["user_id"], campaign)
        )

    def test_reservation_requires_existing_account_and_valid_identity(self):
        self.assertFalse(
            self.auth.reserve_lifecycle_email("missing-user", "campaign-v1", now=100.0)
        )
        with self.assertRaises(ValueError):
            self.auth.reserve_lifecycle_email("", "campaign-v1")
        with self.assertRaises(ValueError):
            self.auth.reserve_lifecycle_email("user", "")
        with self.assertRaises(ValueError):
            self.auth.mark_lifecycle_email_delivery("user", "campaign-v1", "sent")
        with self.assertRaises(ValueError):
            self.auth.reserve_lifecycle_email("user", "x" * 121)


if __name__ == "__main__":
    unittest.main()
