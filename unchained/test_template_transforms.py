"""Focused tests for inline template transform helpers."""

from __future__ import annotations

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

    def test_mcp_api_key_instructions_are_local_and_do_not_autofill(self):
        from agent_package import _WINDOWS_INSTALLER_TEMPLATE, _generate_public_install_script
        from web_app import templates

        html = templates.MCP_PAGE_HTML
        self.assertIn(
            'INSTALL_DIR="$HOME/unchained-agent"',
            _generate_public_install_script("https://unchainedsky.com"),
        )
        self.assertIn('$installDir = Join-Path $HOME "unchained-agent"', _WINDOWS_INSTALLER_TEMPLATE)
        self.assertIn("~/unchained-agent/.env", html)
        self.assertIn(r"%USERPROFILE%\unchained-agent\.env", html)
        self.assertIn('id="load-key-posix"', html)
        self.assertIn('id="load-key-windows"', html)
        self.assertEqual(html.count("YOUR_UNCHAINED_API_KEY"), 6)
        self.assertNotIn("Sign in to auto-fill your API key", html)
        self.assertNotIn("YOUR_API_KEY", html)
        self.assertNotIn("/auth/me", html)
        self.assertNotIn("me.api_key", html)
        self.assertNotIn("copySnippet", html)
        self.assertNotIn("fillKey", html)


if __name__ == "__main__":
    unittest.main()
