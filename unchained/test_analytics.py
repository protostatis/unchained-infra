"""Unit tests for lightweight analytics storage and funnel summaries."""

from __future__ import annotations

import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
import os
from unittest.mock import patch

from analytics import AnalyticsStore


def _req(path: str, ip: str, ua: str = "Mozilla/5.0"):
    return SimpleNamespace(
        headers={"User-Agent": ua},
        remote=ip,
        path=path,
    )


class TestAnalyticsStore(unittest.TestCase):
    def test_migrates_legacy_events_table_and_creates_sessions_table(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    event TEXT NOT NULL,
                    visitor_id TEXT NOT NULL,
                    user_id TEXT,
                    user_type TEXT,
                    route TEXT,
                    referrer_path TEXT,
                    source TEXT,
                    status_code INTEGER,
                    meta_json TEXT
                )
                """
            )
            conn.commit()
            conn.close()

            store = AnalyticsStore(db_path=db_path)
            self.assertIsNotNone(store)
            conn = sqlite3.connect(db_path)
            event_cols = {row[1] for row in conn.execute("PRAGMA table_info(analytics_events)").fetchall()}
            session_cols = {row[1] for row in conn.execute("PRAGMA table_info(analytics_sessions)").fetchall()}
            conn.close()

            self.assertIn("event_id", event_cols)
            self.assertIn("session_id", event_cols)
            self.assertIn("route_effective", event_cols)
            self.assertIn("latency_ms", event_cols)
            self.assertIn("session_id", session_cols)
            self.assertIn("visitor_id", session_cols)

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

    def test_login_funnel_treats_gsi_iframe_as_gate_visible(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            now = time.time()
            req = _req("/local", "10.0.0.7")

            store.track("page_view", request=req, route="/local", now=now - 10)
            store.track("gsi_iframe_loaded", request=req, route="/local", gate_type="inline_gsi", now=now - 9)
            store.track("google_signin_click", request=req, route="/local", gate_type="inline_gsi", now=now - 8)
            store.track("auth_google_attempt", request=req, route="/local", source="claude", now=now - 7)

            summary = store.login_funnel(days=7)
            step_counts = {row["step"]: row["visitors"] for row in summary["steps"]}
            self.assertEqual(step_counts["login_page_view"], 1)
            self.assertEqual(step_counts["login_gate_visible"], 1)
            self.assertEqual(step_counts["auth_google_attempt"], 1)

    def test_install_funnel_report(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            now = time.time()
            req = _req("/install", "10.0.0.9")

            store.track("page_view", request=req, route="/install", now=now - 40)
            store.track("cta_click", request=req, route="/install", cta_id="install_signin_link", now=now - 30)
            store.track("install_token_issued", request=req, route="/web/install-token", now=now - 20)
            store.track("installer_download_start", request=req, route="/web/download-installer", now=now - 10)
            store.track("install_bootstrap_success", request=req, route="/web/install/bootstrap", now=now - 5)

            summary = store.funnel_report(funnel="install", days=7)
            step_counts = {row["step"]: row["visitors"] for row in summary["steps"]}
            self.assertEqual(step_counts["install_page_view"], 1)
            self.assertEqual(step_counts["install_signin_click"], 1)
            self.assertEqual(step_counts["install_token_issued"], 1)
            self.assertEqual(step_counts["installer_download_start"], 1)
            self.assertEqual(step_counts["install_bootstrap_success"], 1)

    def test_chat_activation_counts_page_view_before_auth_success(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            now = time.time()
            req = _req("/local", "10.0.0.5")

            store.track("page_view", request=req, route="/local", now=now - 20)
            store.track("auth_google_success", request=req, route="/local", user_id="u-1", user_type="claude", now=now - 10)
            store.track("chat_message_send", request=req, route="/web/chat", user_id="u-1", user_type="claude", now=now - 5)

            summary = store.funnel_report(funnel="chat_activation", days=7)
            step_counts = {row["step"]: row["visitors"] for row in summary["steps"]}
            self.assertEqual(step_counts["local_or_chat_page_view"], 1)
            self.assertEqual(step_counts["auth_google_success"], 1)
            self.assertEqual(step_counts["chat_message_send"], 1)

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


class TestWebAnalyticsContext(unittest.TestCase):
    def test_track_event_uses_request_analytics_headers(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                req = SimpleNamespace(
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "X-Unchained-Analytics-Session": "s-browser-123",
                        "X-Unchained-Analytics-Page-View": "pv-browser-123",
                        "X-Unchained-Analytics-Route": "/trial",
                        "X-Unchained-Analytics-Gate-Type": "inline_gsi",
                    },
                    remote="10.0.0.88",
                    path="/auth/google",
                )
                inserted = web._track_event(
                    req,
                    "auth_google_attempt",
                    source="trial",
                    status_code=200,
                )
                self.assertTrue(inserted)
                conn = sqlite3.connect(db_path)
                row = conn.execute(
                    "SELECT session_id, page_view_id, route, route_intended, route_effective, gate_type "
                    "FROM analytics_events"
                ).fetchone()
                conn.close()
                self.assertEqual(
                    row,
                    ("s-browser-123", "pv-browser-123", "/trial", "/trial", "/trial", "inline_gsi"),
                )
            finally:
                web._analytics = original_analytics
                web._analytics_last_cleanup_ts = original_cleanup


class _FakeRequest:
    def __init__(self, body, path="/web/analytics/event"):
        self._body = body
        self.path = path
        self.remote = "10.0.0.99"
        self.headers = {}
        self.query = {}

    async def json(self):
        return self._body


class TestAnalyticsHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_event_ingest_is_rate_limited(self):
        from web_app.handlers import analytics as analytics_handlers

        class _Core:
            def _analytics_ingest_allow(self, request, units=1):
                del request, units
                return False, 9

            def _coerce_analytics_event_payload(self, raw, request):
                del raw, request
                return None, "should_not_reach"

        with patch.object(analytics_handlers, "_core", return_value=_Core()):
            resp = await analytics_handlers.handle_analytics_event(
                _FakeRequest({"event": "page_view"})
            )
        self.assertEqual(resp.status, 429)
        self.assertEqual(resp.headers.get("Retry-After"), "9")

    async def test_batch_ingest_uses_event_count_for_rate_units(self):
        from web_app.handlers import analytics as analytics_handlers

        seen: dict[str, int] = {}

        class _Core:
            def _analytics_ingest_allow(self, request, units=1):
                del request
                seen["units"] = int(units)
                return False, 4

            def _coerce_analytics_event_payload(self, raw, request):
                del raw, request
                return None, "should_not_reach"

        with patch.object(analytics_handlers, "_core", return_value=_Core()):
            resp = await analytics_handlers.handle_analytics_events(
                _FakeRequest(
                    {
                        "events": [
                            {"event": "page_view"},
                            {"event": "gate_shown"},
                            {"event": "cta_click"},
                        ]
                    },
                    path="/web/analytics/events",
                )
            )
        self.assertEqual(resp.status, 429)
        self.assertEqual(seen.get("units"), 3)


if __name__ == "__main__":
    unittest.main()
