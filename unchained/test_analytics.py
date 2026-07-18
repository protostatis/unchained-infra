"""Unit tests for lightweight analytics storage and funnel summaries."""

from __future__ import annotations

import json
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
            self.assertIn("referrer_host", event_cols)
            self.assertIn("user_agent_hash", event_cols)
            self.assertIn("user_agent_class", event_cols)
            self.assertIn("is_bot", event_cols)
            self.assertIn("session_id", session_cols)
            self.assertIn("visitor_id", session_cols)

    def test_track_records_privacy_safe_traffic_quality_fields(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            req = _req(
                "/unbrowser",
                "1.2.3.4",
                "Mozilla/5.0 AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            )
            req.headers["Referer"] = "https://github.com/protostatis/unbrowser"
            self.assertTrue(store.track("page_view", request=req, route="/unbrowser"))

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT referrer_host, user_agent_hash, user_agent_class, is_bot "
                "FROM analytics_events"
            ).fetchone()
            conn.close()

            self.assertEqual(row[0], "github.com")
            self.assertRegex(row[1], r"^ua:[0-9a-f]{20}$")
            self.assertEqual(row[2], "chrome_like")
            self.assertEqual(row[3], 0)

    def test_track_flags_declared_automation_user_agents(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=db_path)
            req = _req("/first-look", "1.2.3.4", "ExampleCrawler/1.0")
            self.assertTrue(store.track("page_view", request=req, route="/first-look"))

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT user_agent_class, is_bot FROM analytics_events"
            ).fetchone()
            conn.close()
            self.assertEqual(row, ("declared_automation", 1))

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
            page_view_id = "pv-local-1"

            store.track("page_view", request=req, route="/local", page_view_id=page_view_id, now=now - 10)
            store.track("gate_shown", request=req, route="/local", page_view_id=page_view_id, gate_type="inline_gsi", now=now - 9.5)
            store.track("login_gate_visible", request=req, route="/local", page_view_id=page_view_id, gate_type="inline_gsi", now=now - 9.25)
            store.track("gsi_iframe_loaded", request=req, route="/local", page_view_id=page_view_id, gate_type="inline_gsi", now=now - 9)
            store.track("google_signin_click", request=req, route="/local", page_view_id=page_view_id, gate_type="inline_gsi", now=now - 8)
            store.track("auth_google_attempt", request=req, route="/local", source="claude", now=now - 7)

            summary = store.login_funnel(days=7)
            step_counts = {row["step"]: row["visitors"] for row in summary["steps"]}
            self.assertEqual(step_counts["login_page_view"], 1)
            self.assertEqual(step_counts["login_gate_visible"], 1)
            self.assertEqual(step_counts["auth_google_attempt"], 1)
            self.assertEqual(
                summary["gate_exposures_by_route"],
                [{"route": "/local", "count": 1}],
            )

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

    def test_funnel_report_excludes_current_registered_users(self):
        with tempfile.TemporaryDirectory() as td:
            analytics_db_path = f"{td}/analytics.db"
            auth_db_path = f"{td}/auth.db"
            store = AnalyticsStore(db_path=analytics_db_path, auth_db_path=auth_db_path)
            now = time.time()
            req = _req("/trial", "10.0.0.7")

            conn = sqlite3.connect(auth_db_path)
            conn.execute(
                """
                CREATE TABLE users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    status TEXT DEFAULT 'approved'
                )
                """
            )
            conn.execute(
                "INSERT INTO users (user_id, email, status) VALUES (?, ?, ?)",
                ("u-registered", "registered@example.com", "approved"),
            )
            conn.commit()
            conn.close()

            store.track("page_view", request=req, route="/trial", now=now - 20)
            store.track("login_gate_visible", request=req, route="/trial", now=now - 18)
            store.track("google_signin_click", request=req, route="/trial", now=now - 17)
            store.track("auth_google_attempt", request=req, route="/trial", now=now - 16.5)
            store.track(
                "auth_google_success",
                request=req,
                user_id="u-registered",
                user_type="trial",
                now=now - 16,
            )

            filtered = store.funnel_report(funnel="auth_inline_gsi", days=7)
            filtered_counts = {row["step"]: row["visitors"] for row in filtered["steps"]}
            self.assertEqual(filtered_counts["login_page_view"], 0)
            self.assertEqual(filtered_counts["login_gate_visible"], 0)
            self.assertEqual(filtered_counts["google_signin_click"], 0)
            self.assertEqual(filtered_counts["auth_google_success"], 0)
            self.assertEqual(filtered["matched_registered_user_count"], 1)
            self.assertEqual(filtered["excluded_registered_visitor_count"], 1)

            unfiltered = store.funnel_report(
                funnel="auth_inline_gsi",
                days=7,
                exclude_current_registered_users=False,
            )
            unfiltered_counts = {row["step"]: row["visitors"] for row in unfiltered["steps"]}
            self.assertEqual(unfiltered_counts["login_page_view"], 1)
            self.assertEqual(unfiltered_counts["login_gate_visible"], 1)
            self.assertEqual(unfiltered_counts["google_signin_click"], 1)
            self.assertEqual(unfiltered_counts["auth_google_success"], 1)

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
    def test_growth_and_seo_routes_are_page_view_allowlisted(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        expected = {
            "/chrome-tax",
            "/use/apartment-hunting",
            "/use/flight-comparison",
            "/use/competitor-monitoring",
            "/use/price-tracking",
            "/labs/research-desk",
            "/cli",
        }
        self.assertTrue(expected.issubset(web._ANALYTICS_PAGE_VIEW_ROUTES))

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

    def test_page_views_are_get_only_and_keep_only_bounded_acquisition_tokens(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/analytics.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                request = SimpleNamespace(
                    headers={"User-Agent": "Mozilla/5.0 Chrome/140.0"},
                    remote="10.0.0.90",
                    path="/unbrowser",
                    method="HEAD",
                    query={
                        "ref": "unbrowser-readme",
                        "task": "research",
                        "utm_source": "github",
                        "utm_medium": "repository",
                        "utm_campaign": "unbrowser_guide",
                        "utm_term": "private search terms",
                        "email": "person@example.com",
                    },
                )
                with patch.object(
                    web,
                    "_authenticate",
                    side_effect=AssertionError("HEAD must skip authentication"),
                ):
                    self.assertFalse(web._track_page_view(request))
                request.method = "GET"
                self.assertTrue(web._track_page_view(request, auth_info={}))

                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT event, route, meta_json FROM analytics_events ORDER BY id"
                ).fetchall()
                conn.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][:2], ("page_view", "/unbrowser"))
                self.assertEqual(
                    json.loads(rows[0][2]),
                    {
                        "ref": "unbrowser-readme",
                        "task": "research",
                        "utm_source": "github",
                        "utm_medium": "repository",
                        "utm_campaign": "unbrowser_guide",
                    },
                )
            finally:
                web._analytics = original_analytics
                web._analytics_last_cleanup_ts = original_cleanup

    def test_acquisition_tokens_are_rejected_instead_of_rewritten(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        request = SimpleNamespace(
            query={
                "ref": "person@example.com",
                "task": "untrusted_task",
                "utm_source": "github/repository",
                "utm_medium": "repository",
                "utm_campaign": "x" * 97,
            }
        )
        self.assertEqual(
            web._analytics_acquisition_meta(request),
            {"utm_medium": "repository"},
        )


class TestLandingVariantAnalytics(unittest.IsolatedAsyncioTestCase):
    async def test_root_page_views_store_only_the_server_resolved_variant(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        cases = (
            ("missing", {}, web.LANDING_HTML, "landing_value_prop_v1"),
            ("v4", {"ui": "v4"}, web.LANDING_V4_HTML, "landing_value_prop_v1"),
            ("default", {"ui": "default"}, web.LANDING_V4_HTML, "landing_value_prop_v1"),
            ("v2", {"ui": "v2"}, web.LANDING_V2_HTML, "landing_legacy_v2"),
            ("v3", {"ui": "v3"}, web.LANDING_V3_HTML, "landing_legacy_v3"),
            (
                "invalid",
                {
                    "ui": "v2<script>",
                    "landing_variant": "landing_legacy_v2",
                    "meta": '{"landing_variant":"landing_legacy_v3"}',
                    "email": "private@example.com",
                    "ref": "growth-check",
                    "task": "research",
                    "utm_source": "github",
                    "utm_medium": "repository",
                    "utm_campaign": "landing_value_prop_v1",
                },
                web.LANDING_HTML,
                "landing_value_prop_v1",
            ),
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/analytics.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                for index, (name, query, template, _marker) in enumerate(cases, start=1):
                    with self.subTest(variant=name):
                        request = SimpleNamespace(
                            headers={
                                "User-Agent": "Mozilla/5.0 Chrome/140.0",
                                "X-Forwarded-For": f"198.51.100.{index}",
                            },
                            remote="10.0.0.90",
                            path="/",
                            method="GET",
                            query=query,
                            cookies={},
                        )
                        with patch.object(web, "_authenticate", return_value={}):
                            response = await web.handle_index(request)
                        expected_html = web.inject_google_client_id(
                            template.replace("__CONTACT_EMAIL__", web.CONTACT_EMAIL),
                            web.GOOGLE_CLIENT_ID,
                        )
                        self.assertEqual(response.text, expected_html)

                head_request = SimpleNamespace(
                    headers={
                        "User-Agent": "Mozilla/5.0 Chrome/140.0",
                        "X-Forwarded-For": "198.51.100.200",
                    },
                    remote="10.0.0.90",
                    path="/",
                    method="HEAD",
                    query={"ui": "v2"},
                    cookies={},
                )
                with patch.object(
                    web,
                    "_authenticate",
                    side_effect=AssertionError("HEAD must skip authentication"),
                ):
                    await web.handle_index(head_request)

                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT meta_json FROM analytics_events ORDER BY id"
                ).fetchall()
                conn.close()
                self.assertEqual(len(rows), len(cases))
                metadata = [json.loads(row[0]) for row in rows]
                self.assertEqual(
                    [meta["landing_variant"] for meta in metadata],
                    [case[3] for case in cases],
                )
                for meta in metadata:
                    self.assertIn(
                        meta["landing_variant"], web._ANALYTICS_LANDING_VARIANTS
                    )
                self.assertEqual(
                    metadata[-1],
                    {
                        "landing_variant": "landing_value_prop_v1",
                        "ref": "growth-check",
                        "task": "research",
                        "utm_source": "github",
                        "utm_medium": "repository",
                        "utm_campaign": "landing_value_prop_v1",
                    },
                )
            finally:
                web._analytics = original_analytics
                web._analytics_last_cleanup_ts = original_cleanup

    async def test_reserved_landing_marker_rejects_generic_or_unbounded_meta(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/analytics.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                request = SimpleNamespace(
                    headers={"User-Agent": "Mozilla/5.0 Chrome/140.0"},
                    remote="198.51.100.210",
                    path="/",
                    method="GET",
                    query={},
                )
                self.assertTrue(
                    web._track_page_view(
                        request,
                        auth_info={},
                        meta={"landing_variant": "caller_spoof", "context": "kept"},
                        landing_variant="landing_legacy_v2",
                    )
                )
                request.remote = "198.51.100.211"
                self.assertTrue(
                    web._track_page_view(
                        request,
                        auth_info={},
                        meta={"landing_variant": "caller_spoof"},
                        landing_variant="x" * 500,
                    )
                )

                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT meta_json FROM analytics_events ORDER BY id"
                ).fetchall()
                conn.close()
                self.assertEqual(
                    [json.loads(row[0]) for row in rows],
                    [
                        {"context": "kept", "landing_variant": "landing_legacy_v2"},
                        {},
                    ],
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
    async def test_single_ingest_rejects_server_only_events_without_persistence(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web
        from web_app.handlers import analytics as analytics_handlers

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/analytics.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                for event in (
                    "first_look_run_accepted",
                    "FIRST_LOOK_RUN_REJECTED",
                    "first look run terminal",
                    "unbrowser_demo_run_accepted",
                    "UNBROWSER_DEMO_RUN_REJECTED",
                    "unbrowser demo run terminal",
                    "unbrowser_outbound_click",
                ):
                    with self.subTest(event=event):
                        with patch.object(analytics_handlers, "_core", return_value=web):
                            resp = await analytics_handlers.handle_analytics_event(
                                _FakeRequest({"event": event})
                            )
                        self.assertEqual(resp.status, 400)
                        self.assertEqual(
                            json.loads(resp.body.decode())["error"],
                            "event reserved for server use",
                        )

                conn = sqlite3.connect(db_path)
                count = conn.execute("SELECT COUNT(*) FROM analytics_events").fetchone()[0]
                conn.close()
                self.assertEqual(count, 0)

                inserted = web._track_event(
                    _FakeRequest({}),
                    "first_look_run_accepted",
                    event_id="server-event",
                    source="server",
                    meta={"run_id": "0123456789abcdefabcd"},
                )
                self.assertTrue(inserted)
            finally:
                web._analytics = original_analytics
                web._analytics_last_cleanup_ts = original_cleanup

    async def test_batch_ingest_rejects_server_only_events_and_persists_allowed_event_only(self):
        os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
        import web
        from web_app.handlers import analytics as analytics_handlers

        with tempfile.TemporaryDirectory() as td:
            db_path = f"{td}/analytics.db"
            original_analytics = web._analytics
            original_cleanup = web._analytics_last_cleanup_ts
            web._analytics = AnalyticsStore(db_path=db_path)
            web._analytics_last_cleanup_ts = time.time()
            try:
                body = {
                    "events": [
                        {"event": "first_look_run_accepted"},
                        {"event": "first_look_run_rejected"},
                        {"event": "first_look_run_terminal"},
                        {"event": "unbrowser_demo_run_accepted"},
                        {"event": "unbrowser_demo_run_rejected"},
                        {"event": "unbrowser_demo_run_terminal"},
                        {"event": "unbrowser_outbound_click"},
                        {"event": "cta_click", "cta_id": "first_look_trial"},
                    ]
                }
                with patch.object(analytics_handlers, "_core", return_value=web):
                    resp = await analytics_handlers.handle_analytics_events(
                        _FakeRequest(body, path="/web/analytics/events")
                    )

                self.assertEqual(resp.status, 200)
                self.assertEqual(
                    json.loads(resp.body.decode()),
                    {"ok": True, "received": 8, "accepted": 1, "rejected": 7},
                )
                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT event FROM analytics_events ORDER BY id"
                ).fetchall()
                conn.close()
                self.assertEqual(rows, [("cta_click",)])
            finally:
                web._analytics = original_analytics
                web._analytics_last_cleanup_ts = original_cleanup

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
