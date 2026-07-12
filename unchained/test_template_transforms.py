"""Focused tests for inline template transform helpers."""

from __future__ import annotations

import shutil
import subprocess
import unittest

from web_app.template_transforms import (
    TemplateReplacement,
    TemplateTransformError,
    apply_template_replacements,
)


class TestTemplateTransforms(unittest.TestCase):
    def test_apply_template_replacements_replaces_expected_marker(self):
        html = "<head></head>"
        rendered = apply_template_replacements(
            html,
            (
                TemplateReplacement(
                    "</head>",
                    "<style id='x'></style></head>",
                    "style injection",
                ),
            ),
            template_name="sample template",
        )
        self.assertEqual(rendered, "<head><style id='x'></style></head>")

    def test_apply_template_replacements_raises_on_marker_drift(self):
        with self.assertRaises(TemplateTransformError) as ctx:
            apply_template_replacements(
                "<head></head>",
                (
                    TemplateReplacement(
                        "<body>",
                        "<body class='x'>",
                        "missing body marker",
                    ),
                ),
                template_name="sample template",
            )
        self.assertIn("missing body marker", str(ctx.exception))
        self.assertIn("sample template", str(ctx.exception))

    def test_chat_templates_import_with_sidebar_and_theme_applied(self):
        from web_app import templates

        self.assertIn('id="modern-chat-theme"', templates.CHAT_CODEX_HTML)
        self.assertIn('id="playful-chat-skin"', templates.TRIAL_CHAT_HTML)
        self.assertIn('id="playful-chat-skin"', templates.CLAUDE_CHAT_HTML)
        self.assertIn("Lane A", templates.TRIAL_CHAT_HTML)
        self.assertIn("Lane A", templates.CLAUDE_CHAT_HTML)
        self.assertIn('id="dev-login-btn"', templates.TRIAL_CHAT_HTML)
        self.assertIn("maybeShowDevLogin", templates.TRIAL_CHAT_HTML)
        self.assertIn('class="topbar-new"', templates.TRIAL_CHAT_HTML)
        self.assertIn('class="topbar-new"', templates.CLAUDE_CHAT_HTML)
        self.assertIn('id="sidebar"', templates.CHAT_CODEX_HTML)
        self.assertIn("codex-sdk:codex-mini-latest", templates.CHAT_CODEX_HTML)
        self.assertIn("claude-sdk:claude-sonnet-4-6", templates.CHAT_CLAUDE_SDK_HTML)

    def test_agent_view_chat_state_and_response_reveal_across_lanes(self):
        from web_app import templates

        lane_templates = {
            "Claude CLI": templates.CLAUDE_CHAT_HTML,
            "Claude SDK": templates.CHAT_CLAUDE_SDK_HTML,
            "Codex": templates.CHAT_CODEX_HTML,
        }
        for lane, html in lane_templates.items():
            with self.subTest(lane=lane):
                self.assertEqual(html.count('id="agent-view"'), 1)
                self.assertEqual(html.count('id="agent-view-chat-controls"'), 1)
                self.assertEqual(html.count('id="chat-card-expand"'), 1)
                self.assertEqual(html.count('id="chat-card-exit"'), 1)
                self.assertEqual(html.count('id="chat-card-minimize"'), 1)
                self.assertEqual(html.count('id="agent-view-chat-restore"'), 1)
                self.assertEqual(html.count('id="msginput"'), 1)
                self.assertEqual(html.count("appendText(bubble, evt.data)"), 1)
                self.assertIn("function setAgentViewChatState(mode, surface)", html)
                self.assertIn("function maybeRevealAgentResponse()", html)
                self.assertIn("beginAgentViewResponseTurn()", html)
                self.assertIn(
                    "if (agentViewChatMode === 'fullscreen') { exitAgentViewFullscreen(); return; }",
                    html,
                )
                self.assertIn("setAgentViewChatState('docked', 'chat');", html)
                self.assertNotIn('id="topbar-chat-size"', html)
                self.assertNotIn('id="av-chat-size"', html)
                self.assertNotIn('id="chat-card-size"', html)
                self.assertNotIn("_chatSizeCycle", html)
                self.assertNotIn("av-fullscreen-minimize", html)

    def test_agent_view_controls_are_buttons_not_filtered_nav_links(self):
        from web_app import templates

        html = templates.CHAT_CLAUDE_SDK_HTML
        controls = html.split('id="agent-view-chat-controls"', 1)[1].split(
            '<a href="#" onclick="doDisconnect();return false">', 1
        )[0]
        self.assertIn('<button type="button" id="chat-card-expand"', controls)
        self.assertIn('<button type="button" id="chat-card-exit"', controls)
        self.assertIn('<button type="button" id="chat-card-minimize"', controls)
        self.assertNotIn("<a ", controls)
        self.assertEqual(controls.count('<svg viewBox="0 0 16 16"'), 3)
        self.assertIn('title="Expand chat"', controls)
        self.assertIn('title="Default chat size"', controls)
        self.assertIn('title="Minimize chat"', controls)
        self.assertIn("body.agent-view-open #agent-view-chat-controls{display:inline-flex}", html)
        self.assertIn("body.agent-view-open.agent-view-chat-expanded #chat-card-exit{display:inline-flex}", html)
        self.assertIn(
            "body.agent-view-open #main #chat .bubble{min-width:0;max-width:100%;flex-shrink:0}",
            html,
        )

    def test_mobile_chat_controls_are_right_aligned_and_responsive(self):
        from web_app import templates

        html = templates.CHAT_CLAUDE_SDK_HTML
        local_html = templates.CLAUDE_CHAT_HTML
        self.assertIn(
            'role="group" aria-label="Same conversation controlling browser preview"',
            html,
        )
        self.assertIn(
            "body.agent-view-open #main #topbar .nav{margin-left:auto!important;width:100%!important;max-width:none!important;gap:6px!important;flex-wrap:nowrap!important;justify-content:flex-end!important;overflow:visible!important}",
            html,
        )
        self.assertIn(".chat-size-btn{width:44px;min-width:44px;height:44px}", html)
        self.assertIn(".agent-view-foot,.agent-view-chat-context{display:none}", html)
        self.assertIn(
            "body.agent-view-open.chat-minimized .agent-view-chat-restore{display:none!important}",
            html,
        )
        self.assertIn(
            "body.agent-view-open.agent-view-chat-expanded #main #inputbar{margin-bottom:max(8px,env(safe-area-inset-bottom))!important}",
            html,
        )
        self.assertIn(
            "height:min(62dvh,560px,calc(100dvh - var(--agent-view-mobile-chat-top,0px) - 10px))!important",
            html,
        )
        self.assertIn("function positionAgentViewMobileChat(renderedHeight)", html)
        self.assertIn("agent-view-browser-positioned", html)
        self.assertIn(
            "@media(min-width:761px) and (max-width:900px) and (max-height:500px)",
            html,
        )
        self.assertIn(
            "grid-template-columns:max-content minmax(0,1fr)!important",
            local_html,
        )
        self.assertIn('placeholder="Ask anything..."', local_html)
        self.assertNotIn('placeholder="Ask the agent anything..."', local_html)

    def test_agent_view_uses_browser_preview_copy_without_exposing_diagnostics(self):
        from web_app import templates

        lane_templates = {
            "Claude CLI": templates.CLAUDE_CHAT_HTML,
            "Claude SDK": templates.CHAT_CLAUDE_SDK_HTML,
            "Codex": templates.CHAT_CODEX_HTML,
        }
        for lane, html in lane_templates.items():
            with self.subTest(lane=lane):
                self.assertIn(">Browser Preview</a>", html)
                self.assertIn("<strong>Browser Preview</strong>", html)
                self.assertIn("<strong>Same conversation</strong>", html)
                self.assertIn("Controls this preview", html)
                self.assertIn("Current page", html)
                self.assertNotIn("Shared semantic browser", html)
                self.assertNotIn("semantic://", html)
                self.assertIn(".agent-view-foot{display:none", html)
                self.assertIn('id="agent-view-fidelity"', html)
                self.assertIn("fidelityEl.textContent", html)
                self.assertIn("setAgentViewState", html)

    def test_closed_mobile_sidebar_is_removed_from_focus_order(self):
        from web_app import templates

        runtime = templates._SIDEBAR_JS
        self.assertIn("function syncSidebarInteractivity()", runtime)
        self.assertIn("sidebar.toggleAttribute('inert', hidden);", runtime)
        self.assertIn("sidebar.setAttribute('aria-hidden', String(hidden));", runtime)
        self.assertIn("window.addEventListener('resize', syncSidebarInteractivity);", runtime)

    def test_agent_view_mobile_response_reveal_is_once_per_turn(self):
        from web_app import templates

        runtime = templates._AGENT_VIEW_JS
        self.assertIn("agentViewResponseRevealPending = true", runtime)
        self.assertIn("agentViewResponseRevealDone = false", runtime)
        self.assertIn(
            "if (!agentViewResponseRevealPending || agentViewResponseRevealDone) return;",
            runtime,
        )
        self.assertIn("if (!_agentViewIsMobile()", runtime)
        self.assertIn("if (agentViewChatMode === 'minimized') {", runtime)
        self.assertIn("agentViewResponseRevealDone = true;\n    return;", runtime)
        self.assertIn("setAgentViewChatState('docked', 'chat');", runtime)
        self.assertIn(
            "} else if (evt.type === 'done') {\n            maybeRevealAgentResponse();",
            templates.CHAT_CODEX_HTML,
        )

    def test_agent_view_state_transitions_execute_consistently(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent View runtime checks")

        from web_app import templates

        harness = r"""
class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach(value => this.values.add(value)); }
  remove(...values) { values.forEach(value => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = typeof force === 'boolean' ? force : !this.values.has(value);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id,
    classList: new FakeClassList(),
    attrs: {},
    textContent: '',
    scrollTop: 0,
    scrollHeight: 100,
    setAttribute(name, value) { this.attrs[name] = String(value); },
  });
  return elements.get(id);
}
const body = {classList: new FakeClassList()};
globalThis.document = {
  body,
  getElementById: element,
  addEventListener() {},
};
let mobile = true;
globalThis.window = {
  matchMedia() { return {matches: mobile}; },
  addEventListener() {},
};
globalThis.requestAnimationFrame = function() {};
globalThis.location = {search: '', protocol: 'https:', host: 'example.test'};
globalThis._setActiveSlotSession = function() {};
globalThis.addUserBubble = function() {};
globalThis.appendText = function() {};
let sending = false;
"""
        checks = r"""
function expect(condition, message) { if (!condition) throw new Error(message); }
openAgentView();
expect(body.classList.contains('agent-view-open'), 'Agent View did not open');
expect(!body.classList.contains('agent-view-chat-open'), 'mobile should open on browser surface');

toggleAgentViewChat(true);
expect(body.classList.contains('agent-view-chat-open'), 'Chat did not open');
expandAgentViewChat();
expect(body.classList.contains('agent-view-chat-expanded'), 'Chat did not expand');
exitAgentViewFullscreen();
expect(!body.classList.contains('agent-view-chat-expanded'), 'Fullscreen exit did not dock chat');
expect(body.classList.contains('agent-view-chat-open'), 'Fullscreen exit hid transcript');

minimizeAgentViewChat();
expect(body.classList.contains('chat-minimized'), 'Chat did not minimize');
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(body.classList.contains('chat-minimized'), 'Response reveal overrode explicit minimize');
restoreAgentViewChat();
expect(!body.classList.contains('chat-minimized'), 'Restore left chat minimized');
expect(body.classList.contains('agent-view-chat-open'), 'Restore did not open transcript');

setAgentViewChatState('docked', 'browser');
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(body.classList.contains('agent-view-chat-open'), 'First mobile response did not reveal transcript');
setAgentViewChatState('docked', 'browser');
maybeRevealAgentResponse();
expect(!body.classList.contains('agent-view-chat-open'), 'Later response chunk ignored Browser choice');

closeAgentView();
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(!body.classList.contains('agent-view-open'), 'Response opened Agent View before browser activity');
openAgentView();
maybeRevealAgentResponse();
expect(body.classList.contains('agent-view-chat-open'), 'Response pending before browser activity was lost');
"""
        result = subprocess.run(
            [node],
            input=harness + templates._AGENT_VIEW_JS + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_landing_signin_targets_last_provider_route(self):
        from web_app import templates

        self.assertIn('id="landing-auth-link"', templates.LANDING_HTML)
        self.assertIn("normalizeLandingRoute", templates.LANDING_HTML)
        self.assertIn("provider=codex-cli", templates.LANDING_HTML)
        self.assertIn("Codex CLI", templates.LANDING_HTML)
        self.assertIn("provider=opencode-cli", templates.LANDING_HTML)
        self.assertIn("OpenCode CLI", templates.LANDING_HTML)

    def test_chat_and_setup_pages_remember_provider_routes(self):
        from web_app import templates

        self.assertIn("_rememberLastAppRoute", templates.CLAUDE_CHAT_HTML)
        self.assertIn("/local?provider=opencode-cli", templates.CLAUDE_CHAT_HTML)
        self.assertIn("updateOpenCodeModelOptions", templates.CLAUDE_CHAT_HTML)
        self.assertIn("opencode_models", templates.CLAUDE_CHAT_HTML)
        self.assertIn("/local?provider=codex-cli", templates.CHAT_CODEX_HTML)
        self.assertIn("rememberSetupRoute", templates.SETUP_HTML)
        self.assertIn("unchained_last_route", templates.SETUP_HTML)

    def test_opencode_lane_state_uses_stable_provider_route(self):
        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        self.assertIn("function currentLaneModel()", html)
        self.assertIn("? 'opencode-cli:' : model", html)
        self.assertEqual(html.count("model: currentLaneModel()"), 4)
        self.assertIn("const model = currentModel();", html)
        self.assertIn("model: model,", html)

    def test_ux_contract_copy_and_demo_routes(self):
        from web_app import templates

        self.assertIn("You navigate. The agent drives.", templates.LANDING_HTML)
        self.assertIn("font-family:'Familjen Grotesk',system-ui,sans-serif", templates.LANDING_HTML)
        self.assertNotIn("Instrument Serif", templates.LANDING_HTML)
        self.assertIn("Chrome profile", templates.LANDING_HTML)
        self.assertIn("Product preview", templates.LANDING_HTML)
        self.assertIn('aria-live="polite"', templates.LANDING_HTML)
        self.assertIn("prefers-reduced-motion:reduce", templates.LANDING_HTML)
        self.assertIn("See what you can <em>hand off</em>", templates.LANDING_HTML)
        self.assertIn("workflow-preview", templates.LANDING_HTML)
        self.assertIn("SCENARIOS", templates.LANDING_HTML)
        self.assertIn("Shortlist apartments", templates.LANDING_HTML)
        self.assertIn("Prepared result", templates.LANDING_HTML)
        self.assertIn("decision-tree", templates.LANDING_HTML)
        self.assertIn("Watch it work first", templates.LANDING_HTML)
        self.assertIn("Start a guided trial", templates.LANDING_HTML)
        self.assertIn("Recommended", templates.LANDING_HTML)
        self.assertIn("Built for browser work", templates.LANDING_HTML)
        self.assertIn('href="/demo" class="cta-btn">Try live demo', templates.LANDING_HTML)
        self.assertIn('href="/trial" class="signin" id="landing-auth-link">Start free trial</a>', templates.LANDING_HTML)
        self.assertIn("Start free trial", templates.LANDING_HTML)
        self.assertIn("Guided bridge install", templates.LANDING_HTML)
        self.assertIn("Work from a selected Chrome profile", templates.LANDING_HTML)
        self.assertIn("rec-cta", templates.LANDING_HTML)
        self.assertNotIn('href="/chrome-tax"', templates.LANDING_HTML)
        self.assertNotIn("Drive my browser", templates.LANDING_HTML)
        self.assertNotIn("narrative rhythm", templates.LANDING_HTML)
        self.assertNotIn("Sign in / Sign up", templates.LANDING_HTML)

    def test_auth_setup_trust_and_accessibility_copy(self):
        from web_app import templates

        self.assertNotIn("Signing up as: <strong>Trial</strong>", templates.TRIAL_CHAT_HTML)
        self.assertNotIn("Signing up as: <strong>Claude</strong>", templates.CLAUDE_CHAT_HTML)
        self.assertIn("Mode: <strong>Your Chrome + free models</strong>", templates.TRIAL_CHAT_HTML)
        self.assertIn("Mode: <strong>Local CLI + your Chrome</strong>", templates.CLAUDE_CHAT_HTML)
        self.assertIn("Unchained Chat", templates.CLAUDE_CHAT_HTML)
        self.assertIn('role="dialog" aria-modal="true"', templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("showSampleRun", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("Task for the shared browser", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("Shared browser is reconnecting. Try again in a moment.", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertNotIn("Shared browser is unavailable right now.", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("https://unchainedsky.com/install.sh", templates.MCP_PAGE_HTML)
        self.assertNotIn("https://api.unchainedsky.com/install.sh", templates.MCP_PAGE_HTML)
        self.assertIn("API key handling", templates.SETUP_HTML)
        self.assertIn("Your Chrome profile stays on your machine", templates.INSTALL_ONBOARD_HTML)


if __name__ == "__main__":
    unittest.main()
