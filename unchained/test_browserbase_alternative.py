"""Focused and adversarial contracts for the Browserbase comparison page."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web
from web_app.handlers import pages
from web_app.routes import ROUTE_SPECS
from web_app.templates import BROWSERBASE_ALTERNATIVE_HTML


PAGE_ROUTE = "/browserbase-alternative"
PAGE_URL = f"https://unchainedsky.com{PAGE_ROUTE}"
TRIAL_HREF = (
    "/trial?ref=browserbase_alternative&amp;utm_source=unchainedsky"
    "&amp;utm_medium=seo_content&amp;utm_campaign=browserbase_alternative_v1"
)


class TestBrowserbaseAlternativeContracts(TestCase):
    def test_page_is_answer_first_and_preserves_unchained_brand(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        self.assertIn(
            "<title>Browserbase Alternative: Browserbase vs Unchained (2026)</title>",
            html,
        )
        self.assertIn(f'<link rel="canonical" href="{PAGE_URL}">', html)
        self.assertIn(
            '<h1 id="page-title">Browserbase vs Unchained: cloud browser fleet '
            "or on-machine workspace?</h1>",
            html,
        )
        self.assertLess(html.index("Is Unchained a Browserbase alternative?"), html.index("Different operating models"))
        self.assertIn("Browserbase excels at managed cloud browser sessions", html)
        self.assertIn("UNCHAINED DRIVES. YOU NAVIGATE.", html)

    def test_page_has_one_exact_measured_guided_trial_cta(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        exact_cta = (
            f'<a class="cta" href="{TRIAL_HREF}" '
            'data-analytics-cta="browserbase_alternative_trial">'
            "Open a guided local-browser trial</a>"
        )
        self.assertEqual(html.count(exact_cta), 1)
        self.assertLess(html.index(exact_cta), html.index('class="verdict"'))

    def test_cta_note_uses_normal_text_contrast_token(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        self.assertIn(
            ".cta-note{display:inline-block;margin-left:.8rem;"
            "color:var(--muted);font-size:.84rem",
            html,
        )
        self.assertNotIn(".cta-note{display:inline-block;margin-left:.8rem;color:var(--dim)", html)

    def test_comparison_is_fair_and_avoids_unsupported_absolutes(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        self.assertIn("For a managed browser fleet, no.", html)
        self.assertIn("Browserbase is likely the better fit", html)
        self.assertIn("This is a fit comparison, not a benchmark.", html)
        self.assertIn("Neither architecture is inherently the right privacy choice", html)
        for forbidden in (
            "faster than Browserbase",
            "cheaper than Browserbase",
            "more private than Browserbase",
            "unlimited concurrency",
            "drop-in replacement",
            "zero data leaves your device",
        ):
            self.assertNotIn(forbidden.lower(), html.lower())
        self.assertNotIn("$", html)

    def test_browserbase_claims_have_current_first_party_sources(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        sources = (
            (
                "https://docs.browserbase.com/platform/browser/getting-started/"
                "create-browser-session",
                "Browserbase: Create a browser session",
            ),
            (
                "https://docs.browserbase.com/platform/browser/core-features/contexts",
                "Browserbase: Contexts",
            ),
            (
                "https://docs.browserbase.com/platform/browser/observability/observability",
                "Browserbase: Observability",
            ),
            (
                "https://docs.browserbase.com/optimizations/concurrency/overview",
                "Browserbase: Concurrency management",
            ),
        )
        positions = []
        for href, label in sources:
            link = f'<a href="{href}" rel="noopener noreferrer">{label}</a>'
            self.assertIn(link, html)
            positions.append(html.index(link))
        self.assertEqual(positions, sorted(positions))
        self.assertIn("An isolated browser session runs in Browserbase's cloud", html)
        self.assertIn("Sessions start fresh by default", html)
        self.assertIn("Live View, video recordings, event timelines, and replay", html)
        self.assertIn("plan-dependent concurrency and session-creation limits", html)

    def test_page_distinguishes_public_research_from_browser_control(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        self.assertIn("SearchAgentSky is the public-web research product", html)
        self.assertIn("Use it for a current, cited answer from public sources with no local client", html)
        self.assertIn(
            "Use Unchained when the task needs a signed-in browser context or browser actions",
            html,
        )
        self.assertIn(
            "utm_campaign=browserbase_alternative_v1",
            html,
        )

    def test_page_discloses_data_flow_and_affiliation(self):
        html = BROWSERBASE_ALTERNATIVE_HTML
        self.assertIn("Unchained is not local-only.", html)
        self.assertIn("browser-derived page context", html)
        self.assertIn("may be transmitted to Unchained and configured AI providers", html)
        self.assertIn(
            "not affiliated with, endorsed by, or sponsored by Browserbase",
            html,
        )
        self.assertIn('<time datetime="2026-07-18">July 18, 2026</time>', html)

    def test_get_handler_tracks_page_view_and_injects_analytics(self):
        request = SimpleNamespace(method="GET", path=PAGE_ROUTE, query={})
        with patch.object(web, "_track_page_view") as track_page_view:
            response = asyncio.run(pages.handle_browserbase_alternative_page(request))
        track_page_view.assert_called_once_with(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("data-uc-analytics-client", response.text)

    def test_head_probe_does_not_write_analytics(self):
        request = SimpleNamespace(method="HEAD", path=PAGE_ROUTE, query={})
        with patch.object(web, "_track_event") as track_event:
            response = asyncio.run(pages.handle_browserbase_alternative_page(request))
        track_event.assert_not_called()
        self.assertEqual(response.status, 200)

    def test_query_values_are_not_reflected_into_static_page(self):
        marker = "browserbase-query-marker-<script>alert(1)</script>"
        request = SimpleNamespace(
            method="GET",
            path=PAGE_ROUTE,
            query={"ref": marker, "utm_campaign": marker},
        )
        with patch.object(web, "_track_page_view"):
            response = asyncio.run(pages.handle_browserbase_alternative_page(request))
        self.assertNotIn(marker, response.text)

    def test_route_is_registered_allowlisted_and_sitemapped(self):
        self.assertIn(
            (
                "GET",
                PAGE_ROUTE,
                "web_app.handlers.pages:handle_browserbase_alternative_page",
            ),
            ROUTE_SPECS,
        )
        self.assertIn(PAGE_ROUTE, web._ANALYTICS_PAGE_VIEW_ROUTES)
        sitemap = asyncio.run(web.handle_sitemap_xml(None))
        self.assertIn(PAGE_URL, sitemap.text)
