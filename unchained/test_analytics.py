"""Unit tests for lightweight analytics storage and funnel summaries."""

from __future__ import annotations

import tempfile
import time
from types import SimpleNamespace
import unittest

from analytics import AnalyticsStore


def _req(path: str, ip: str, ua: str = "Mozilla/5.0"):
    return SimpleNamespace(
        headers={"User-Agent": ua},
        remote=ip,
        path=path,
    )


class TestAnalyticsStore(unittest.TestCase):
    def test_track_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            req = _req("/trial", "1.2.3.4")
            inserted = store.track(
                "login_gate_visible",
                request=req,
                route="/trial",
                now=100.0,
                dedupe_ttl_s=60.0,
            )
            self.assertTrue(inserted)
            skipped = store.track(
                "login_gate_visible",
                request=req,
                route="/trial",
                now=120.0,
                dedupe_ttl_s=60.0,
            )
            self.assertFalse(skipped)
            inserted_after_ttl = store.track(
                "login_gate_visible",
                request=req,
                route="/trial",
                now=161.0,
                dedupe_ttl_s=60.0,
            )
            self.assertTrue(inserted_after_ttl)

    def test_login_funnel_summary(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            now = time.time()
            v1 = _req("/trial", "10.0.0.1")
            v2 = _req("/trial", "10.0.0.2")

            # Visitor 1 progresses through the funnel.
            store.track("page_view", request=v1, route="/trial", now=now - 20)
            store.track("login_gate_visible", request=v1, route="/trial", now=now - 18)
            store.track("google_signin_click", request=v1, route="/trial", now=now - 16)
            store.track("auth_google_attempt", request=v1, source="trial", now=now - 14)
            store.track("auth_google_success", request=v1, source="trial", user_id="u-1", user_type="trial", now=now - 12)
            store.track("chat_message_send", request=v1, user_id="u-1", user_type="trial", now=now - 10)
            store.track("signup_created", request=v1, user_id="u-1", user_type="trial", meta={"source": "trial"}, now=now - 9)

            # Visitor 2 drops before clicking sign in, and has one auth failure.
            store.track("page_view", request=v2, route="/trial", now=now - 8)
            store.track("login_gate_visible", request=v2, route="/trial", now=now - 7)
            store.track("auth_google_fail", request=v2, meta={"reason": "invalid_google_token"}, now=now - 6)

            summary = store.login_funnel(days=7)
            step_counts = {row["step"]: row["visitors"] for row in summary["steps"]}

            self.assertEqual(step_counts["login_page_view"], 2)
            self.assertEqual(step_counts["login_gate_visible"], 2)
            self.assertEqual(step_counts["google_signin_click"], 1)
            self.assertEqual(step_counts["auth_google_attempt"], 1)
            self.assertEqual(step_counts["auth_google_success"], 1)
            self.assertEqual(step_counts["chat_message_send"], 1)
            self.assertEqual(summary["auth_google_fail_total"], 1)
            self.assertEqual(summary["signups_by_source"][0]["source"], "trial")
            self.assertEqual(summary["signups_by_source"][0]["count"], 1)

    def test_cleanup_old_events(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            req = _req("/trial", "8.8.8.8")
            now = time.time()
            store.track("page_view", request=req, route="/trial", now=now)
            store.track("page_view", request=req, route="/trial", now=now - (86400.0 * 200))
            deleted = store.cleanup_old_events(keep_days=90)
            self.assertEqual(deleted, 1)


if __name__ == "__main__":
    unittest.main()

