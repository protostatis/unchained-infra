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


if __name__ == "__main__":
    unittest.main()
