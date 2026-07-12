"""Contract tests for web surface area.

These tests protect public routes and exported template contracts while
`web.py` is gradually split into modules.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
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
            ("GET", "/unbrowser"),
            ("GET", "/web/unbrowser/sources"),
            ("GET", "/web/unbrowser/runtime"),
            ("GET", "/web/unbrowser/stream"),
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
            ("GET", "/first-look-preview"),
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
            ("GET", "/web/first-look/preflight"),
            ("GET", "/web/first-look/preview/ws"),
            ("POST", "/web/chat/update-client"),
            ("POST", "/web/first-look/signal"),
            ("POST", "/web/chat/install-research-desk"),
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
            ("GET", "/web/research-desk/files"),
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
            "UNBROWSER_PAGE_HTML",
            "BRANDED_TAB_HTML",
            "HTML",
            "TRIAL_CHAT_HTML",
            "CHAT_GEMINI_HTML",
            "HEADLESS_DEMO_HTML",
            "FIRST_LOOK_PREVIEW_HTML",
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
        self.assertIn("loadSchedulerOpenCodeModels()", web.SCHEDULER_HTML)
        self.assertIn("data.opencode_models", web.SCHEDULER_HTML)
        self.assertIn("opencode-cli:", web.SCHEDULER_HTML)
        self.assertIn("AbortController", web.SCHEDULER_HTML)
        self.assertIn("console.warn('OpenCode scheduler model load failed", web.SCHEDULER_HTML)
        self.assertIn("schedulerOpenCodeModelsLoaded", web.SCHEDULER_HTML)
        self.assertIn("SCHEDULER_OPENCODE_MODEL_CAP = 500", web.SCHEDULER_HTML)
        self.assertIn("credentials:'same-origin'", web.SCHEDULER_HTML)
        self.assertIn("missing opencode_models", web.SCHEDULER_HTML)
        self.assertIn("non-array opencode_models", web.SCHEDULER_HTML)
        self.assertIn("models loaded; use Custom for more", web.SCHEDULER_HTML)
        self.assertIn("exhausted retries", web.SCHEDULER_HTML)
        self.assertIn("openHistoryModal", web.SCHEDULER_HTML)

    def test_landing_auth_cta_points_to_auth_entry(self):
        self.assertIn('href="/trial" class="signin" id="landing-auth-link">Start free trial</a>', web.LANDING_HTML)
        self.assertIn("normalizeLandingRoute", web.LANDING_HTML)
        self.assertIn("Open trial", web.LANDING_HTML)
        self.assertNotIn('href="/setup" class="signin">Sign in</a>', web.LANDING_HTML)

    def test_landing_v4_default_route_clears_preview_cookie(self):
        async def _render(query: dict[str, str], cookies: dict[str, str]):
            return await web.handle_index(SimpleNamespace(query=query, cookies=cookies))

        response = asyncio.run(_render({"ui": "v4"}, {"ui": "v3"}))
        self.assertIn("You call the shots. <em>Unchained runs the steps.</em>", response.text)
        self.assertIn("ui", response.cookies)
        self.assertEqual(response.cookies["ui"]["max-age"], "0")

        default_response = asyncio.run(_render({"ui": "default"}, {"ui": "v2"}))
        self.assertIn("You call the shots. <em>Unchained runs the steps.</em>", default_response.text)
        self.assertIn("ui", default_response.cookies)
        self.assertEqual(default_response.cookies["ui"]["max-age"], "0")

        stale_cookie_response = asyncio.run(_render({}, {"ui": "v3"}))
        self.assertIn("You call the shots. <em>Unchained runs the steps.</em>", stale_cookie_response.text)
        self.assertIn("ui", stale_cookie_response.cookies)
        self.assertEqual(stale_cookie_response.cookies["ui"]["max-age"], "0")

        unknown_query_response = asyncio.run(_render({"ui": "unknown"}, {"ui": "v3"}))
        self.assertIn("You call the shots. <em>Unchained runs the steps.</em>", unknown_query_response.text)
        self.assertNotIn("ui", unknown_query_response.cookies)

        v3_response = asyncio.run(_render({"ui": "v3"}, {}))
        self.assertIn("AI Browser Agent for Everyday Web Tasks", v3_response.text)
        self.assertEqual(v3_response.cookies["ui"].value, "v3")

    def test_chat_markdown_rendering_sanitizes_assistant_output(self):
        chat_templates = {
            "TRIAL_CHAT_HTML": web.TRIAL_CHAT_HTML,
            "CHAT_GEMINI_HTML": web.CHAT_GEMINI_HTML,
            "CHAT_CLAUDE_SDK_HTML": web.CHAT_CLAUDE_SDK_HTML,
            "CHAT_CODEX_HTML": web.CHAT_CODEX_HTML,
            "CLAUDE_CHAT_HTML": web.CLAUDE_CHAT_HTML,
            "HEADLESS_DEMO_HTML": web.HEADLESS_DEMO_HTML,
        }
        for name, html in chat_templates.items():
            with self.subTest(template=name):
                self.assertNotIn("__SAFE_MARKDOWN_RENDERER_JS__", html)
                self.assertNotIn("span.innerHTML = marked.parse(bubble._rawText);", html)
                self.assertIn("span.innerHTML = renderSafeMarkdown(bubble._rawText);", html)
                self.assertIn("marked.parse(escapeMarkdownHtml(raw))", html)
                self.assertIn("function escapeMarkdownHtml(value)", html)
                self.assertIn("name.indexOf('on') === 0", html)
                self.assertIn("\\u2000-\\u200D", html)
                self.assertIn("svg,math,picture,source,video,audio", html)
                self.assertIn("name === 'formaction'", html)
                self.assertIn("Raw assistant HTML is intentionally displayed as text", html)
                self.assertIn("javascript:|vbscript:|data:", html)

        self.assertNotIn("sanitizeHtml(marked.parse(raw))", web.FIRST_LOOK_PREVIEW_HTML)
        self.assertNotIn("__SAFE_MARKDOWN_RENDERER_JS__", web.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn(
            "sanitizeHtml(marked.parse(escapeMarkdownHtml(raw)))",
            web.FIRST_LOOK_PREVIEW_HTML,
        )

    def test_unbrowser_page_links_public_directories(self):
        self.assertIn("https://smithery.ai/servers/protostatis-dev/unbrowser", web.UNBROWSER_PAGE_HTML)
        self.assertIn("https://glama.ai/mcp/servers/protostatis/unbrowser", web.UNBROWSER_PAGE_HTML)
        self.assertIn("https://github.com/protostatis/unbrowser", web.UNBROWSER_PAGE_HTML)
        self.assertIn("https://unchainedsky.com/unbrowser-mcp", web.UNBROWSER_PAGE_HTML)

    def test_unbrowser_page_live_demo_contract(self):
        self.assertIn("/web/unbrowser/sources", web.UNBROWSER_PAGE_HTML)
        self.assertIn("/web/unbrowser/runtime", web.UNBROWSER_PAGE_HTML)
        self.assertIn("/web/unbrowser/stream", web.UNBROWSER_PAGE_HTML)
        self.assertIn("No arbitrary URLs", web.UNBROWSER_PAGE_HTML)
        self.assertIn("Try: ", web.UNBROWSER_PAGE_HTML)

    def test_web_image_installs_unbrowser_binary_package(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        self.assertIn("pyunbrowser==0.0.14", dockerfile.read_text(encoding="utf-8"))

    def test_unbrowser_live_demo_presets_have_dense_source_grids(self):
        from web_app.handlers.unbrowser_demo import SCENARIOS

        self.assertIn("crypto-prices", SCENARIOS)
        for scenario in SCENARIOS.values():
            self.assertGreaterEqual(len(scenario.sources), 16, scenario.id)

    def test_research_desk_page_renders_phase3_connect_markers(self):
        from web_app.handlers.pages import _build_research_desk_html

        html = _build_research_desk_html(authenticated=True)
        self.assertIn("data.provider?.browser_client", html)
        self.assertIn("data.trial?.status", html)
        self.assertIn("data.local_urls?.home", html)
        self.assertIn("data.launch_ready", html)
        self.assertIn("data.missing.join(', ')", html)
        self.assertIn('id="install-local-research-desk"', html)
        self.assertIn("Install / Update Research Desk", html)
        self.assertIn('href="/install">Install Local Agent</a>', html)
        self.assertIn("If the Unchained local client is not installed yet", html)
        self.assertIn("python3 -m unchained_pyreplab bridge-start", html)
        self.assertIn("python3 -m unchained_pyreplab serve --open --reload", html)
        self.assertIn("install the package and ask the local client to run setup, start the bridge, and start the local desk automatically", html)
        self.assertIn("After install starts, the local client tries to run setup, start the browser bridge, and start the local desk for you.", html)
        self.assertIn("safeLocalUrl", html)
        self.assertIn("'launch_ready' in (data||{})", html)
        self.assertIn("Boolean(data.launch_ready)", html)
        self.assertIn("FALLBACK_LOCAL_URL", html)
        self.assertIn("LOCAL_PROBE_TIMEOUT_MS = 5000", html)
        self.assertIn('id="retry-local-desk"', html)
        self.assertIn("POLL_INTERVAL_MS = 3000", html)
        self.assertIn("scheduleDeskProbe()", html)
        self.assertIn("renderMissingDeskState()", html)
        self.assertIn("chip('agent install: /install')", html)
        self.assertIn("let deskLoadInFlight = false", html)
        self.assertIn("let latestDeskRequestId = 0", html)
        self.assertIn("let deskWasDetected = false", html)
        self.assertIn("document.hidden", html)
        self.assertIn("visibilitychange", html)
        self.assertIn("requestId!==latestDeskRequestId", html)
        self.assertIn("opts.silent&&deskWasDetected", html)
        self.assertIn("Install or start the local desk, then click Check Again.", html)
        self.assertIn("RESEARCH_DESK_LAUNCHER_FALLBACK = 'python3 -m unchained_pyreplab'", html)
        self.assertIn("async function fetchLocalJson(url)", html)
        self.assertIn("signal:AbortSignal.timeout(LOCAL_PROBE_TIMEOUT_MS)", html)
        self.assertIn("const statusData=await fetchLocalJson(STATUS_URL)", html)
        self.assertIn("const capsulesData=await fetchLocalJson(CAPSULES_URL)", html)
        self.assertIn("resetCapsulesWaiting('Local Research Desk is running, but recent mission summaries are still loading.')", html)
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
        self.assertIn('id="mission-watch-preferred-link"', html)
        self.assertIn('id="mission-watch-mission-link"', html)
        self.assertIn('id="mission-watch-lab-link"', html)
        self.assertIn('id="mission-watch-stage-rail"', html)
        self.assertIn('id="mission-watch-stats"', html)
        self.assertIn('id="run-local-next-step"', html)
        self.assertIn("const WATCH_STAGE_ORDER = [['planning','Mission'],['scouting','Scout'],['capturing','Gather'],['shaping','Shape'],['analysis','Lab Notes']]", html)
        self.assertIn("function watchStageIndex(data)", html)
        self.assertIn("function statCard(label, value, hint)", html)
        self.assertIn("function finiteNumber(value, fallback)", html)
        self.assertIn("setMissionWatchLink(", html)
        self.assertIn("stage.className='stage-step '+(idx<activeStageIndex?'done':(idx===activeStageIndex?'active':'pending'))", html)
        self.assertIn("const qaLabel=acceptedLikeFraction >= 0 ? (Math.round(acceptedLikeFraction*100)+'%') : 'pending'", html)
        self.assertIn("const statusHint=String(data.blocked_reason||'').trim() || 'Hosted watch is live'", html)
        self.assertIn("stats.appendChild(statCard('Object', String(data.primary_object_name||'pending')", html)
        self.assertIn("stats.appendChild(statCard('Next', String(data.autopilot_next_label||'Open local desk')", html)
        self.assertIn("const qaCounts=Object.entries(data.qa_status_counts||{})", html)
        self.assertIn("scheduleMissionWatch(statusUrl)", html)
        self.assertIn("source_route:'/first-look'", html)
        self.assertIn("const RESEARCH_DESK_INSTALL_AUTHENTICATED = true;", html)
        self.assertIn("window.location.href=RESEARCH_DESK_SIGN_IN_URL", html)
        self.assertIn("const INSTALL_UPDATE_RETRY_INTERVAL_MS = 5000;", html)
        self.assertIn("const MAX_INSTALL_UPDATE_RETRIES = 12;", html)
        self.assertIn("async function requestResearchDeskInstall()", html)
        self.assertIn("async function requestLocalClientUpdate()", html)
        self.assertIn("/web/chat/update-client", html)
        self.assertIn("if(resp.status===409&&Boolean(data.update_required))", html)
        self.assertIn("Updating it to at least", html)
        self.assertIn("if(updateResult.kind==='already_current')", html)
        self.assertIn("The local client is already current. Retrying Research Desk install now...", html)
        self.assertIn("Waiting for the local client to finish updating and reconnect before retrying Research Desk install...", html)
        self.assertIn("The local client update started, but Research Desk install could not be retried automatically yet.", html)
        self.assertIn("Preparing Research Desk...", html)

    def test_research_desk_page_renders_guest_install_state(self):
        from web_app.handlers.pages import _build_research_desk_html

        html = _build_research_desk_html(authenticated=False)
        self.assertIn("const RESEARCH_DESK_INSTALL_AUTHENTICATED = false;", html)
        self.assertIn("Sign In to Install Research Desk", html)
        self.assertIn("Sign in to unchainedsky.com first, then install Research Desk through your local client.", html)
        self.assertIn("Sign in first, then use <strong>Sign In to Install Research Desk</strong> above", html)
        self.assertIn("RESEARCH_DESK_INSTALL_AUTHENTICATED?'Install / Update Research Desk':'Sign In to Install Research Desk'", html)
        self.assertIn("window.location.href=RESEARCH_DESK_SIGN_IN_URL", html)
        self.assertIn('const RESEARCH_DESK_SIGN_IN_URL = "/trial";', html)

    def test_research_desk_page_normalizes_sign_in_url(self):
        from web_app.handlers.pages import _build_research_desk_html

        html = _build_research_desk_html(authenticated=False, sign_in_url="https://evil.example.com")
        self.assertIn('const RESEARCH_DESK_SIGN_IN_URL = "/trial";', html)
        self.assertIn("const missionUrl=safeOptionalLocalUrl(data.mission_url_abs||data.mission_url||'')", html)
        self.assertIn("const labUrl=safeOptionalLocalUrl(data.lab_url_abs||data.capsule_url_abs||data.capsule_url||'')", html)
        self.assertIn("const preferredUrl=safeOptionalLocalUrl(data.preferred_open_url_abs||(Boolean(data.lab_ready)?labUrl:'')||missionUrl||'')", html)
        self.assertIn("const preferredLabel=String(data.preferred_open_label||(Boolean(data.lab_ready)&&Boolean(labUrl)?'Open Lab Notes':'Open Mission'))", html)
        self.assertIn("/* Keep polling after Lab Notes becomes ready so the links and next-step state stay fresh while the local desk settles. */", html)
        self.assertIn("String(data.primary_object_name||'object pending')", html)
        self.assertIn("String(data.advance_busy ? 'running' : 'idle')", html)
        self.assertIn("String(reviewedPageCount > 0 ? ('pages '+reviewedPageCount) : 'pages pending')", html)
        self.assertIn("latestDeskStatus?.handshake?.actions?.mission_advance_url", html)
        self.assertIn("setMissionAdvanceReady", html)
        self.assertIn("fetch('/web/chat/install-research-desk',{method:'POST',credentials:'include',cache:'no-store'})", html)
        self.assertIn("Research Desk install started. The local client is running `'+launcherPrefix+' setup`, starting the browser bridge, starting the local desk automatically, and opening it locally when ready.", html)
        self.assertIn("requested_scope:'mission:create mission:advance'", html)
        self.assertIn("document.getElementById('run-local-next-step')?.addEventListener('click'", html)
        self.assertIn("This mission is ready for Lab Notes. Continue there or keep the local desk open for deeper analysis.", html)
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
        self.assertIn("if(existingPrompt&&!sessionRestored&&RESEARCH_DESK_INSTALL_AUTHENTICATED){setConnectNote('Ready to hand off the current first-look prompt into your local desk once the connection is approved.');}", html)
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

    def test_first_look_preview_template_uses_honest_guest_copy(self):
        from web_app.handlers.pages import _build_first_look_preview_html

        html = _build_first_look_preview_html(prompt_limit=5, remaining=3)
        # Structural assertions — API endpoints and JS constants that must be
        # present for the page to function correctly.
        self.assertIn("const FIRST_LOOK_GUEST_LIMIT = 5;", html)
        self.assertIn("let remainingGuestRuns = 3;", html)
        self.assertIn("/web/first-look/preflight", html)
        self.assertIn("/web/first-look/preview/ws", html)
        self.assertIn("/web/first-look/signal", html)
        self.assertIn("new WebSocket(url)", html)
        self.assertIn("first_look_guest: true", html)
        self.assertIn("headless: true", html)

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


class TestTrialModelStorageIsolation(unittest.TestCase):
    def _run_storage_runtime(self, assertions: str) -> None:
        match = re.search(
            r"const _TRIAL_MODEL_STORAGE_PREFIX = .*?(?=\nfunction _nextAfterLogin\()",
            web.TRIAL_CHAT_HTML,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, "trial model storage runtime missing")
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for trial storage runtime tests")
        runtime = r"""
class MemoryStorage {
  constructor() { this.values = new Map(); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(String(key), String(value)); }
  removeItem(key) { this.values.delete(String(key)); }
}
const localStorage = new MemoryStorage();
const elements = {
  modelsel: {value: 'google/gemini-3.1-flash-lite'},
  'model-custom-input': {value: ''},
  'control-link': {style: {display: 'none'}},
};
const document = {getElementById: id => elements[id] || null};
let _userId = '';
let _isAdmin = false;
function _defaultTrialModel() { return 'google/gemini-3.1-flash-lite'; }
function _syncCustomModelUi() {}
""" + match.group(0) + assertions
        result = subprocess.run(
            [node, "-e", runtime],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_two_accounts_do_not_share_or_retain_model_state(self):
        self._run_storage_runtime(r"""
localStorage.setItem('unrelated', 'keep-me');
_setTrialIdentity('opaque-user-a');
_persistTrialModel('vendor/private-admin-model');
if (_readTrialModelPreference() !== 'vendor/private-admin-model') throw new Error('account A did not restore its model');
_setTrialIdentity('opaque-user-b');
if (_readTrialModelPreference() !== '') throw new Error('account B inherited account A model');
if (localStorage.getItem(_trialModelKey('opaque-user-a')) !== null) throw new Error('account A state survived identity change');
_persistTrialModel('google/gemini-3.1-flash-lite');
if (_readTrialModelPreference() !== 'google/gemini-3.1-flash-lite') throw new Error('account B preference was not scoped');
if (localStorage.getItem('unrelated') !== 'keep-me') throw new Error('unrelated storage was cleared');
""")

    def test_unowned_legacy_model_does_not_cross_accounts_or_logout(self):
        self._run_storage_runtime(r"""
localStorage.setItem('unrelated', 'keep-me');
localStorage.setItem(_TRIAL_ACTIVE_IDENTITY_KEY, 'opaque-user-a');
localStorage.setItem(_LEGACY_MODEL_KEY, 'vendor/account-a-model');
localStorage.setItem(_LEGACY_MODEL_OWNER_KEY, 'opaque-user-a');
_setTrialIdentity('opaque-user-b');
if (_readTrialModelPreference() !== '') throw new Error('account B migrated account A legacy model');
if (localStorage.getItem(_LEGACY_MODEL_KEY) !== null) throw new Error('legacy model was not discarded');
localStorage.setItem(_LEGACY_MODEL_KEY, 'vendor/account-b-model');
localStorage.setItem(_LEGACY_MODEL_OWNER_KEY, 'opaque-user-b');
if (_readTrialModelPreference() !== 'vendor/account-b-model') throw new Error('owned legacy model was not migrated');
_setTrialIdentity('');
if (localStorage.getItem(_trialModelKey('opaque-user-b')) !== null) throw new Error('logout retained account B model');
if (localStorage.getItem(_TRIAL_ACTIVE_IDENTITY_KEY) !== null) throw new Error('logout retained active identity');
if (localStorage.getItem('unrelated') !== 'keep-me') throw new Error('logout cleared unrelated storage');
""")

    def test_admin_control_is_hidden_for_second_non_admin_account(self):
        self._run_storage_runtime(r"""
_applyTrialIdentity({user_id: 'opaque-admin-a', is_admin: true});
if (elements['control-link'].style.display !== '') throw new Error('admin account did not see control link');
_setTrialIdentity('');
if (elements['control-link'].style.display !== 'none') throw new Error('logout retained admin control link');
_applyTrialIdentity({user_id: 'opaque-user-b', is_admin: false});
if (elements['control-link'].style.display !== 'none') throw new Error('non-admin account inherited admin control link');
_syncTrialAdminUi();
if (elements['control-link'].style.display !== 'none') throw new Error('non-admin render revealed admin control link');
""")

    def test_trial_template_has_no_unscoped_model_writes(self):
        self.assertNotIn("localStorage.setItem('unchained_model'", web.TRIAL_CHAT_HTML)
        self.assertIn("_persistTrialModel(evt.model)", web.TRIAL_CHAT_HTML)
        self.assertIn("controlLink.style.display = _isAdmin ? '' : 'none'", web.TRIAL_CHAT_HTML)
        self.assertIn("function showMain()", web.TRIAL_CHAT_HTML)
        self.assertIn("  _syncTrialAdminUi();", web.TRIAL_CHAT_HTML)
        storage_runtime = web.TRIAL_CHAT_HTML[
            web.TRIAL_CHAT_HTML.index("const _TRIAL_MODEL_STORAGE_PREFIX"):
            web.TRIAL_CHAT_HTML.index("function _nextAfterLogin")
        ]
        self.assertNotIn("email", storage_runtime.lower())


class TestAuthenticatedIdentityContracts(unittest.IsolatedAsyncioTestCase):
    async def test_auth_me_returns_stable_opaque_user_id(self):
        from web_app.handlers import auth_admin

        user = {
            "user_id": "opaque-user-a",
            "status": "approved",
            "user_type": "claude",
            "name": "User A",
            "picture": "",
        }
        auth_store = SimpleNamespace(
            find_user_by_email=lambda _email: user,
            get_demo_count=lambda _email: 0,
        )
        core = SimpleNamespace(
            _authenticate=lambda _request: {
                "user_id": user["user_id"],
                "email": "user-a@example.test",
                "agent_id": "claude-test",
            },
            _auth=auth_store,
            _is_demo_unlimited=lambda _user: False,
            ADMIN_EMAILS=set(),
        )
        with patch.object(auth_admin, "_core", return_value=core):
            response = await auth_admin.handle_auth_me(SimpleNamespace())

        payload = json.loads(response.body.decode())
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["user_id"], "opaque-user-a")


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


class TestStealthEvasions(unittest.TestCase):
    """Tests for the modular stealth evasion system in chrome_bridge.py."""

    def test_resolve_all_by_default(self):
        from chrome_bridge import _resolve_stealth_evasions, ALL_STEALTH_EVASION_NAMES
        result = _resolve_stealth_evasions("", "")
        self.assertEqual(result, set(ALL_STEALTH_EVASION_NAMES))

    def test_resolve_explicit_all(self):
        from chrome_bridge import _resolve_stealth_evasions, ALL_STEALTH_EVASION_NAMES
        result = _resolve_stealth_evasions("all", "")
        self.assertEqual(result, set(ALL_STEALTH_EVASION_NAMES))

    def test_resolve_specific_evasions(self):
        from chrome_bridge import _resolve_stealth_evasions
        result = _resolve_stealth_evasions("webdriver,screen", "")
        self.assertEqual(result, {"webdriver", "screen"})

    def test_resolve_disable_subtracts(self):
        from chrome_bridge import _resolve_stealth_evasions, ALL_STEALTH_EVASION_NAMES
        result = _resolve_stealth_evasions("", "webgl,plugins")
        self.assertEqual(result, set(ALL_STEALTH_EVASION_NAMES) - {"webgl", "plugins"})

    def test_resolve_unknown_names_ignored(self):
        from chrome_bridge import _resolve_stealth_evasions
        result = _resolve_stealth_evasions("webdriver,bogus_name", "")
        self.assertEqual(result, {"webdriver"})

    def test_build_stealth_js_empty_set(self):
        from chrome_bridge import _build_stealth_js
        self.assertEqual(_build_stealth_js(set()), "")

    def test_build_stealth_js_subset(self):
        from chrome_bridge import _build_stealth_js
        js = _build_stealth_js({"webdriver"})
        self.assertIn("webdriver", js)
        self.assertNotIn("screen", js)
        self.assertNotIn("plugins", js)

    def test_build_stealth_js_all_evasions(self):
        from chrome_bridge import _build_stealth_js, ALL_STEALTH_EVASION_NAMES
        js = _build_stealth_js(set(ALL_STEALTH_EVASION_NAMES))
        self.assertGreater(len(js), 3000)
        self.assertIn("webdriver", js)
        self.assertIn("screenX", js)  # mouse_coords

    def test_base_and_headless_cover_all(self):
        from chrome_bridge import STEALTH_BASE_EVASIONS, STEALTH_HEADLESS_EVASIONS, ALL_STEALTH_EVASION_NAMES
        self.assertEqual(STEALTH_BASE_EVASIONS | STEALTH_HEADLESS_EVASIONS,
                         ALL_STEALTH_EVASION_NAMES)

    def test_base_and_headless_no_overlap(self):
        from chrome_bridge import STEALTH_BASE_EVASIONS, STEALTH_HEADLESS_EVASIONS
        self.assertFalse(STEALTH_BASE_EVASIONS & STEALTH_HEADLESS_EVASIONS)

    def test_each_js_evasion_returns_nonempty_string(self):
        from chrome_bridge import STEALTH_JS_EVASIONS
        for name, _desc, builder in STEALTH_JS_EVASIONS:
            js = builder()
            self.assertIsInstance(js, str, f"{name} should return str")
            self.assertGreater(len(js), 10, f"{name} should return non-trivial JS")

    def test_webgl_gpu_consistent_per_session(self):
        from chrome_bridge import _ev_webgl
        js1 = _ev_webgl()
        js2 = _ev_webgl()
        self.assertEqual(js1, js2, "WebGL GPU should be consistent per session")


if __name__ == "__main__":
    unittest.main()
