"""Focused contracts for the BrowserMCP.io comparison acquisition page."""

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
from web_app.templates import BROWSER_MCP_ALTERNATIVE_HTML


PAGE_ROUTE = "/browser-mcp-alternative"
PAGE_URL = f"https://unchainedsky.com{PAGE_ROUTE}"
TRIAL_HREF = (
    "/trial?ref=browser_mcp_alternative&amp;utm_source=unchainedsky"
    "&amp;utm_medium=seo_content&amp;utm_campaign=browser_mcp_alternative_v1"
)


class TestBrowserMcpAlternativeContracts(TestCase):
    def test_page_has_answer_first_metadata_and_fair_comparison(self):
        html = BROWSER_MCP_ALTERNATIVE_HTML
        self.assertIn(
            "<title>Browser MCP Alternative: BrowserMCP vs Unchained (2026)</title>",
            html,
        )
        self.assertIn(f'<link rel="canonical" href="{PAGE_URL}">', html)
        h1 = (
            '<h1 id="page-title">BrowserMCP vs Unchained: '
            "two ways to control your real Chrome</h1>"
        )
        self.assertIn(h1, html)
        self.assertLess(html.index("The short answer"), html.index("Side by side"))
        self.assertIn("BrowserMCP.io is better when", html)
        self.assertIn("Unchained is better when", html)
        self.assertIn(
            "There are no invented speed, token, reliability, or pricing claims.",
            html,
        )

    def test_page_has_exact_measured_trial_cta(self):
        html = BROWSER_MCP_ALTERNATIVE_HTML
        self.assertIn(f'href="{TRIAL_HREF}"', html)
        cta = (
            'data-analytics-cta="browser_mcp_alternative_trial">'
            "Start a guided browser trial</a>"
        )
        self.assertIn(cta, html)
        self.assertLess(html.index(cta), html.index('<div class="verdict"'))

    def test_page_has_sources_disclosures_and_name_clarification(self):
        html = BROWSER_MCP_ALTERNATIVE_HTML
        self.assertIn("https://github.com/BrowserMCP/mcp", html)
        self.assertIn("https://docs.browsermcp.io/setup-extension", html)
        self.assertIn("https://browsermcp.dev/", html)
        self.assertIn("Unchained is not local-only.", html)
        self.assertIn("browser-derived page context", html)
        self.assertIn(
            "may be transmitted to Unchained and configured AI providers",
            html,
        )
        self.assertIn(
            "not affiliated with, endorsed by, or sponsored by BrowserMCP.io",
            html,
        )
        self.assertIn('<time datetime="2026-07-18">July 18, 2026</time>', html)

    def test_route_handler_tracks_page_view_and_injects_analytics(self):
        request = SimpleNamespace(method="GET", path=PAGE_ROUTE, query={})
        with patch.object(web, "_track_page_view") as track_page_view:
            response = asyncio.run(pages.handle_browser_mcp_alternative_page(request))
        track_page_view.assert_called_once_with(request)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("data-uc-analytics-client", response.text)

    def test_route_is_registered_allowlisted_and_sitemapped(self):
        self.assertIn(
            (
                "GET",
                PAGE_ROUTE,
                "web_app.handlers.pages:handle_browser_mcp_alternative_page",
            ),
            ROUTE_SPECS,
        )
        self.assertIn(PAGE_ROUTE, web._ANALYTICS_PAGE_VIEW_ROUTES)
        sitemap = asyncio.run(web.handle_sitemap_xml(None))
        self.assertIn(PAGE_URL, sitemap.text)

    def test_mcp_guide_links_to_comparison_once(self):
        html = web._build_mcp_guide_html()
        link = (
            '<a href="/browser-mcp-alternative">'
            "See BrowserMCP.io vs Unchained.</a>"
        )
        self.assertEqual(html.count(link), 1)
