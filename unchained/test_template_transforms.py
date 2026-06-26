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

    def test_chat_and_setup_pages_remember_provider_routes(self):
        from web_app import templates

        self.assertIn("_rememberLastAppRoute", templates.CLAUDE_CHAT_HTML)
        self.assertIn("/local?provider=codex-cli", templates.CHAT_CODEX_HTML)
        self.assertIn("rememberSetupRoute", templates.SETUP_HTML)
        self.assertIn("unchained_last_route", templates.SETUP_HTML)

    def test_ux_contract_copy_and_demo_routes(self):
        from web_app import templates

        self.assertIn("AI browser agent for your real Chrome", templates.LANDING_HTML)
        self.assertIn("Act I / the chain breaks", templates.LANDING_HTML)
        self.assertIn("Chains fall from my wrists", templates.LANDING_HTML)
        self.assertIn("spectrum-prism", templates.LANDING_HTML)
        self.assertIn("hero-act-strip", templates.LANDING_HTML)
        self.assertIn('href="/first-look" class="hero-act-card"', templates.LANDING_HTML)
        self.assertIn('href="/trial" class="hero-act-card"', templates.LANDING_HTML)
        self.assertIn('href="#use-cases" class="hero-act-card"', templates.LANDING_HTML)
        self.assertIn('href="/demo" class="cta-primary-big">Watch live demo', templates.LANDING_HTML)
        self.assertIn('href="/demo" class="btn-cta">Watch Live Demo', templates.LANDING_HTML)
        self.assertIn("Connect My Chrome", templates.LANDING_HTML)
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
