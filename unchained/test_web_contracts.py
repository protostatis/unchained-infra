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

    def test_research_desk_page_renders_phase3_connect_markers(self):
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
        self.assertIn("let latestDeskRequestId = 0", html)
        self.assertIn("let deskWasDetected = false", html)
        self.assertIn("document.hidden", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("requestId!==latestDeskRequestId", html)
        self.assertIn("opts.silent&&deskWasDetected", html)
        self.assertIn('id="connect-local-desk"', html)
        self.assertIn('id="create-local-mission"', html)
        self.assertIn("latestDeskStatus?.handshake?.start_url", html)
        self.assertIn("latestDeskStatus?.handshake?.actions?.mission_create_url", html)
        self.assertIn("scheduleHandshakePoll(", html)
        self.assertIn("Approve the request in the local desk tab", html)
        self.assertIn("safeOptionalLocalUrl", html)
        self.assertIn("sanitizeBearerToken", html)
        self.assertIn("MAX_HANDSHAKE_POLL_ATTEMPTS = 40", html)
        self.assertIn("MISSION_WATCH_POLL_INTERVAL_MS = 2500", html)
        self.assertIn("MAX_MISSION_WATCH_ATTEMPTS = 20", html)
        self.assertIn("MAX_IDENTICAL_MISSION_STATES = 6", html)
        self.assertIn("FIRST_LOOK_PROMPT_KEY", html)
        self.assertIn("MAX_HANDOFF_PROMPT_CHARS = 4000", html)
        self.assertIn("MAX_URL_PROMPT_CHARS = 500", html)
        self.assertIn("SESSION_ID_RE = /^[A-Za-z0-9._:-]{1,120}$/", html)
        self.assertIn("normalizeHandoffPrompt", html)
        self.assertIn("normalizeSourceSessionId", html)
        self.assertIn("window.history.replaceState", html)
        self.assertIn("new URL(statusUrl)", html)
        self.assertIn("searchParams.set('request_id', requestId)", html)
        self.assertIn("new URL(String(data.mission_url), FALLBACK_LOCAL_URL).toString()", html)
        self.assertIn("'Authorization':'Bearer '+safeToken", html)
        self.assertIn("let handshakeInFlight = false", html)
        self.assertIn("AbortSignal.timeout(5000)", html)
        self.assertIn("if(handshakeInFlight) return;", html)
        self.assertIn("Approval tab could not be opened.", html)
        self.assertIn("window.addEventListener('beforeunload'", html)
        self.assertIn("credentials:'omit'", html)
        self.assertIn("cache:'no-store'", html)
        self.assertIn("referrerPolicy:'no-referrer'", html)
        self.assertIn("about '+remainingSeconds+'s left", html)
        self.assertIn("replace(/[^\\x21-\\x7E]/g,'')", html)
        self.assertIn("if(handshakeReady){handshakeNotReadyCount=0;}else{handshakeNotReadyCount+=1;", html)
        self.assertIn("Local desk did not return a request ID.", html)
        self.assertIn("const approvalWindow=window.open(", html)
        self.assertIn('id="mission-watch"', html)
        self.assertIn('id="mission-watch-mission-link"', html)
        self.assertIn('id="mission-watch-lab-link"', html)
        self.assertIn('id="run-local-next-step"', html)
        self.assertIn("setMissionWatchLink(", html)
        self.assertIn("scheduleMissionWatch(statusUrl)", html)
        self.assertIn("source_route:'/first-look'", html)
        self.assertIn("const missionUrl=safeOptionalLocalUrl(data.mission_url_abs||data.mission_url||'')", html)
        self.assertIn("const labUrl=safeOptionalLocalUrl(data.lab_url_abs||data.capsule_url_abs||data.capsule_url||'')", html)
        self.assertIn("String(data.primary_object_name||'object pending')", html)
        self.assertIn("String(data.advance_busy ? 'running' : 'idle')", html)
        self.assertIn("String(data.reviewed_page_count ? ('pages '+Number(data.reviewed_page_count||0)) : 'pages pending')", html)
        self.assertIn("latestDeskStatus?.handshake?.actions?.mission_advance_url", html)
        self.assertIn("setMissionAdvanceReady", html)
        self.assertIn("requested_scope:'mission:create mission:advance'", html)
        self.assertIn("document.getElementById('run-local-next-step')?.addEventListener('click'", html)
        self.assertIn("if(data.advance_busy){setConnectNote('The local desk is still running the current step.", html)
        self.assertIn("if(resp.status===429&&data.error==='advance_busy')", html)
        self.assertIn("Mission watch timed out.", html)
        self.assertIn("Mission watch paused because the local state stopped changing.", html)
        self.assertIn("const HANDSHAKE_TOKEN_KEY = 'research-desk-handshake-token'", html)
        self.assertIn("const PENDING_HANDSHAKE_KEY = 'research-desk-pending-handshake'", html)
        self.assertIn("const MISSION_WATCH_STATE_KEY = 'research-desk-mission-watch-state'", html)
        self.assertIn("const MAX_TRANSIENT_HANDSHAKE_NOT_READY = 5;", html)
        self.assertIn("let pendingHandshakeState = null;", html)
        self.assertIn("let missionWatchSnapshot = null;", html)
        self.assertIn("function restoreStoredDeskSession()", html)
        self.assertIn("const startAttempt=Math.min(Math.floor(elapsedSeconds/(HANDSHAKE_POLL_INTERVAL_MS/1000)), MAX_HANDSHAKE_POLL_ATTEMPTS)", html)
        self.assertIn("pendingHandshakeState={statusUrl:safeStatusUrl,requestId:safeRequestId,startAttempt:0};", html)
        self.assertIn("persistPendingHandshake(statusUrl,String(data.request_id))", html)
        self.assertIn("persistApprovedHandshake(data.session_token, data.token_expires_at_epoch)", html)
        self.assertIn("persistMissionWatchState(statusUrl, null)", html)
        self.assertIn("missionWatchSnapshot=data;", html)
        self.assertIn("if(!data.ok){clearMissionWatchState(false);return;}", html)
        self.assertIn("/* Preserve the existing snapshot while the watch URL stays the same; URL changes intentionally drop the old snapshot. */", html)
        self.assertIn("const hasRecoveryState=Boolean(approvedHandshakeToken||handshakeInFlight||missionWatchUrl||missionWatchSnapshot);", html)
        self.assertIn("if(handshakeReady){handshakeNotReadyCount=0;}else{handshakeNotReadyCount+=1;", html)
        self.assertIn("if(handshakeNotReadyCount>=MAX_TRANSIENT_HANDSHAKE_NOT_READY){clearApprovedHandshake();clearPendingHandshake();}", html)
        self.assertIn("if(handshakeReady&&handshakeInFlight&&!handshakePollTimer&&pendingHandshakeState){scheduleHandshakePoll(pendingHandshakeState.statusUrl, pendingHandshakeState.requestId, pendingHandshakeState.startAttempt||0);}", html)
        self.assertIn("Local desk looks offline right now. The last mission snapshot is still shown below while this page keeps retrying.", html)
        self.assertIn("missionWatchVisible=false;missionCanAdvance=false;", html)
        self.assertIn("missionWatchUrl='';", html)
        self.assertIn("This accepts same-origin script access in exchange for tab-scoped recovery.", html)
        self.assertIn("Resuming the pending local approval check from your last hosted session...", html)
        self.assertIn("if(existingPrompt&&!sessionRestored){setConnectNote('Ready to hand off the current first-look prompt into your local desk once the connection is approved.');}", html)
        self.assertIn("restoreStoredDeskSession();", html)

    def test_first_look_template_renders_research_desk_handoff_markers(self):
        self.assertIn("continueInResearchDesk()", web.HEADLESS_DEMO_HTML)
        self.assertIn("FIRST_LOOK_PROMPT_KEY", web.HEADLESS_DEMO_HTML)
        self.assertIn("rememberFirstLookPrompt(msg)", web.HEADLESS_DEMO_HTML)
        self.assertIn("typeof sessionId !== 'undefined'", web.HEADLESS_DEMO_HTML)
        self.assertIn("Intentionally same-origin readable", web.HEADLESS_DEMO_HTML)
        self.assertIn("prompt.slice(0, 500)", web.HEADLESS_DEMO_HTML)
        self.assertIn("window.location.href = '/labs/research-desk' + suffix", web.HEADLESS_DEMO_HTML)
        self.assertIn("Shared demo first. Your Claude next.", web.HEADLESS_DEMO_HTML)
        self.assertIn("2 shared demo runs &middot; selected public sites", web.HEADLESS_DEMO_HTML)
        self.assertIn("Connect Claude Free", web.HEADLESS_DEMO_HTML)
        self.assertIn("updateFirstLookChromeUI()", web.HEADLESS_DEMO_HTML)
        self.assertIn("hint-panel accent", web.HEADLESS_DEMO_HTML)
        self.assertNotIn("maybeAutoPrompt();", web.HEADLESS_DEMO_HTML)
        self.assertIn("showClaudeUpgradeCard()", web.HEADLESS_DEMO_HTML)
        self.assertIn("removeClaudeUpgradeCard()", web.HEADLESS_DEMO_HTML)
        self.assertIn("Want to run this with your Claude?", web.HEADLESS_DEMO_HTML)
        self.assertIn("currentFirstLookPrompt()", web.HEADLESS_DEMO_HTML)
        self.assertIn(".replace(/\"/g,'&quot;')", web.HEADLESS_DEMO_HTML)
        self.assertIn(".replace(/'/g,'&#39;')", web.HEADLESS_DEMO_HTML)
        self.assertIn("Compare three computing pioneers on Wikipedia", web.HEADLESS_DEMO_HTML)
        self.assertIn("Pick the better outdoor coffee day in NYC", web.HEADLESS_DEMO_HTML)
        self.assertIn("Group the top Hacker News stories into themes", web.HEADLESS_DEMO_HTML)
        self.assertIn(
            "On Wikipedia, compare Ada Lovelace, Grace Hopper, and Katherine Johnson.",
            web.HEADLESS_DEMO_HTML,
        )
        self.assertNotIn("set up an API key", web.HEADLESS_DEMO_HTML)

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
