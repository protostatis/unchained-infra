"""Contract tests for web surface area.

These tests protect public routes and exported template contracts while
`web.py` is gradually split into modules.
"""

from __future__ import annotations

import sys
from types import ModuleType
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web


class TestWebRouteContracts(unittest.TestCase):
    """Ensure runtime app wiring continues to expose expected public routes."""

    def _runtime_routes(self) -> set[tuple[str, str]]:
        captured: dict[str, object] = {}

        def _fake_run_app(app, *args, **kwargs):
            del args, kwargs
            captured["app"] = app

        with patch.object(
            web.argparse.ArgumentParser,
            "parse_args",
            return_value=SimpleNamespace(host="127.0.0.1", port=8080),
        ):
            with patch.object(web.web, "run_app", side_effect=_fake_run_app):
                with patch("builtins.print"):
                    web.main()

        app = captured.get("app")
        self.assertIsNotNone(app, "main() should pass an app to web.run_app")
        return {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.method in {"GET", "POST"}
        }

    def test_main_registers_public_routes(self):
        actual = self._runtime_routes()
        expected = {
            ("GET", "/favicon.svg"),
            ("GET", "/"),
            ("GET", "/tab"),
            ("GET", "/mcp-guide"),
            ("GET", "/test"),
            ("GET", "/privacy"),
            ("GET", "/privacy-policy"),
            ("GET", "/data-deletion"),
            ("POST", "/auth/google"),
            ("GET", "/auth/facebook/start"),
            ("GET", "/auth/facebook/callback"),
            ("GET", "/auth/github/start"),
            ("GET", "/auth/github/callback"),
            ("POST", "/auth/logout"),
            ("GET", "/auth/me"),
            ("POST", "/web/analytics/event"),
            ("POST", "/web/analytics/events"),
            ("POST", "/web/cmd"),
            ("GET", "/setup"),
            ("GET", "/admin"),
            ("GET", "/admin/users"),
            ("GET", "/admin/analytics/funnel"),
            ("GET", "/admin/pending"),
            ("POST", "/admin/approve"),
            ("POST", "/admin/reject"),
            ("GET", "/chat"),
            ("GET", "/trial"),
            ("GET", "/chat-gemini"),
            ("GET", "/first-look"),
            ("GET", "/demo"),
            ("GET", "/labs/research-desk"),
            ("GET", "/labs/you-navigate"),
            ("GET", "/labs/x-manager"),
            ("GET", "/app"),
            ("GET", "/chat/ws"),
            ("POST", "/web/chat"),
            ("POST", "/web/chat/cancel"),
            ("POST", "/web/labs/you-navigate/run"),
            ("POST", "/web/labs/x-manager/run"),
            ("GET", "/web/chat/status"),
            ("POST", "/web/chat/update-client"),
            ("GET", "/web/chat/history"),
            ("POST", "/web/chat/new"),
            ("GET", "/web/chat/slots"),
            ("POST", "/web/chat/switch"),
            ("GET", "/web/download-agent"),
            ("GET", "/install.sh"),
            ("POST", "/web/install-token"),
            ("POST", "/web/install/bootstrap"),
            ("GET", "/install/{token}"),
            ("GET", "/trial/connector"),
            ("POST", "/trial/token"),
            ("GET", "/trial/windows/script"),
            ("GET", "/trial/{token}"),
            ("GET", "/web/agent/version"),
            ("GET", "/web/agent/files"),
            ("GET", "/web/provision/profiles"),
            ("POST", "/web/provision/start"),
            ("GET", "/web/provision/status"),
            ("POST", "/web/provision/confirm"),
            ("POST", "/web/provision/save-manual"),
            ("POST", "/web/provision/revoke"),
            ("GET", "/scheduler"),
            ("GET", "/web/scheduler/jobs"),
            ("POST", "/web/scheduler/jobs"),
            ("POST", "/web/scheduler/preview"),
            ("GET", "/web/scheduler/history"),
            ("POST", "/web/scheduler/agent/list"),
            ("POST", "/web/scheduler/agent/preview"),
            ("POST", "/web/scheduler/agent/upsert"),
            ("POST", "/web/scheduler/agent/delete"),
        }
        if not web.GOOGLE_CLIENT_ID:
            expected.add(("POST", "/auth/dev"))

        for route in expected:
            self.assertIn(route, actual, f"missing runtime route: {route}")


class TestWebTemplateContracts(unittest.TestCase):
    """Ensure web templates keep stable exported symbols."""

    def test_chat_pages_exported(self):
        templates = [
            "LANDING_HTML",
            "BRANDED_TAB_HTML",
            "HTML",
            "TRIAL_CHAT_HTML",
            "CHAT_GEMINI_HTML",
            "HEADLESS_DEMO_HTML",
            "CLAUDE_CHAT_HTML",
            "SCHEDULER_HTML",
            "SETUP_HTML",
            "ADMIN_HTML",
        ]
        for name in templates:
            value = getattr(web, name, "")
            self.assertIsInstance(value, str, f"{name} should be a string")
            self.assertIn("<!DOCTYPE html>", value, f"{name} should be full HTML")

    def test_template_js_contract_markers(self):
        self.assertIn("showInstallCmd", web.CLAUDE_CHAT_HTML)
        self.assertIn("/labs/research-desk", web.HEADLESS_DEMO_HTML)
        self.assertIn("copyInstallCmd", web.CLAUDE_CHAT_HTML)
        self.assertIn("closeInstallModal", web.CLAUDE_CHAT_HTML)
        self.assertIn("/web/chat/update-client", web.CLAUDE_CHAT_HTML)
        self.assertIn("/web/chat/update-client", web.SETUP_HTML)
        self.assertIn("/web/chat/update-client", web.INSTALL_ONBOARD_HTML)
        self.assertIn('id="modelsel"', web.CLAUDE_CHAT_HTML)
        self.assertIn("model: currentModel()", web.CLAUDE_CHAT_HTML)
        self.assertIn('id="f-model"', web.SCHEDULER_HTML)
        self.assertIn("getSchedulerModelValue()", web.SCHEDULER_HTML)
        self.assertIn("openHistoryModal", web.SCHEDULER_HTML)

    def test_research_desk_page_renders_phase2_status_contract(self):
        from web_app.handlers.pages import _build_research_desk_html

        html = _build_research_desk_html()
        self.assertIn("data.provider?.browser_client", html)
        self.assertIn("data.trial?.status", html)
        self.assertIn("data.local_urls?.home", html)
        self.assertIn("data.launch_ready", html)
        self.assertIn("data.missing.join(', ')", html)
        self.assertIn("safeLocalUrl", html)
        self.assertIn("'launch_ready' in (data||{})", html)
        self.assertIn("Boolean(data.launch_ready)", html)
        self.assertIn("FALLBACK_LOCAL_URL", html)
        self.assertIn('id="retry-local-desk"', html)
        self.assertIn("POLL_INTERVAL_MS = 3000", html)
        self.assertIn("scheduleDeskProbe()", html)
        self.assertIn("renderMissingDeskState()", html)
        self.assertIn("let deskLoadInFlight = false", html)
        self.assertIn("document.hidden", html)
        self.assertIn("visibilitychange", html)

    def test_client_update_buttons_disable_when_current_and_clear_after_fast_reconnect(self):
        self.assertIn(
            "CLIENT_UPDATE_TIMEOUT_MS = 90000",
            web.CHAT_GEMINI_HTML,
        )
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.CHAT_GEMINI_HTML,
        )
        self.assertIn(
            "else if (clientUpdateSawDisconnect || !data.client_outdated) {",
            web.CHAT_GEMINI_HTML,
        )
        self.assertIn(
            "Update timed out. Check the local client logs and retry.",
            web.CHAT_GEMINI_HTML,
        )
        self.assertIn(
            "if (!clientUpdateInFlight && !data.client_outdated) clientUpdateError = '';",
            web.CHAT_GEMINI_HTML,
        )
        self.assertIn(
            "CLIENT_UPDATE_TIMEOUT_MS = 90000",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "else if (clientUpdateSawDisconnect || !data.client_outdated) {",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "Update timed out. Check the local client logs and retry.",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "if (!clientUpdateInFlight && !data.client_outdated) clientUpdateError = '';",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "CLIENT_UPDATE_TIMEOUT_MS = 90000",
            web.CHAT_CODEX_HTML,
        )
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.CHAT_CODEX_HTML,
        )
        self.assertIn(
            "else if (clientUpdateSawDisconnect || !data.client_outdated) {",
            web.CHAT_CODEX_HTML,
        )
        self.assertIn(
            "Update timed out. Check the local client logs and retry.",
            web.CHAT_CODEX_HTML,
        )
        self.assertIn(
            "if (!clientUpdateInFlight && !data.client_outdated) clientUpdateError = '';",
            web.CHAT_CODEX_HTML,
        )
        self.assertIn(
            "SETUP_CLIENT_UPDATE_TIMEOUT_MS = 90000",
            web.SETUP_HTML,
        )
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.SETUP_HTML,
        )
        self.assertIn(
            "else if (setupClientUpdateSawDisconnect || !data.client_outdated) {",
            web.SETUP_HTML,
        )
        self.assertIn(
            "Update timed out. Check the local client logs and retry.",
            web.SETUP_HTML,
        )
        self.assertIn(
            "if (!setupClientUpdateInFlight && !data.client_outdated) setupClientUpdateError = '';",
            web.SETUP_HTML,
        )
        self.assertIn(
            "INSTALL_CLIENT_UPDATE_TIMEOUT_MS = 90000",
            web.INSTALL_ONBOARD_HTML,
        )
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.INSTALL_ONBOARD_HTML,
        )
        self.assertIn(
            "else if (installClientUpdateSawDisconnect || !data.client_outdated) {",
            web.INSTALL_ONBOARD_HTML,
        )
        self.assertIn(
            "Update timed out. Check the local client logs and retry.",
            web.INSTALL_ONBOARD_HTML,
        )
        self.assertIn(
            "if (!installClientUpdateInFlight && !data.client_outdated) installClientUpdateError = '';",
            web.INSTALL_ONBOARD_HTML,
        )

    def test_trial_auth_session_handshake_contract(self):
        trial = web.TRIAL_CHAT_HTML
        self.assertIn(
            "fetch('/auth/google', {\n      method: 'POST',\n      credentials: 'include',",
            trial,
        )
        self.assertIn(
            "const meResp = await fetch('/auth/me', {\n          credentials: 'include',\n          cache: 'no-store',",
            trial,
        )
        self.assertIn(
            "const r = await fetch('/auth/me', {\n      credentials: 'include',\n      cache: 'no-store',",
            trial,
        )
        self.assertIn("if (data.pending || data.status === 'pending')", trial)
        self.assertIn("Sign-in succeeded, but session was not established.", trial)
        self.assertEqual(
            trial.count("let activeSlot = 1;"),
            1,
            "TRIAL_CHAT_HTML should not duplicate slot runtime declarations",
        )


class TestWebCoreResolverContracts(unittest.TestCase):
    """Ensure extracted modules bind to the active web runtime module."""

    def test_get_core_prefers_web_py_main_module(self):
        from web_app.core import get_core

        fake_main = ModuleType("__main__")
        fake_main.__file__ = "/tmp/web.py"
        fake_main._auth = object()
        fake_main.create_session_token = lambda *_args, **_kwargs: ""

        with patch.dict(sys.modules, {"__main__": fake_main}, clear=False):
            self.assertIs(get_core(), fake_main)

    def test_get_core_falls_back_to_loaded_web_module(self):
        from web_app.core import get_core

        fake_main = ModuleType("__main__")
        fake_main.__file__ = "/tmp/not_web.py"
        fake_web = ModuleType("web")

        with patch.dict(
            sys.modules,
            {"__main__": fake_main, "web": fake_web},
            clear=False,
        ):
            self.assertIs(get_core(), fake_web)


class TestWebAnalyticsIsolationContracts(unittest.TestCase):
    def test_analytics_db_isolated_from_auth_db_by_default(self):
        configured = os.environ.get("UNCHAINED_ANALYTICS_DB_PATH", "").strip()
        if configured:
            self.skipTest("explicit analytics DB path configured via env")
        auth_db = os.path.abspath(web._auth.db_path)
        analytics_db = os.path.abspath(web._analytics.db_path)
        self.assertNotEqual(
            auth_db,
            analytics_db,
            "analytics writes should not use the auth/session SQLite file",
        )


if __name__ == "__main__":
    unittest.main()
