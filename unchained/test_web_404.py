"""Tests for branded browser-page 404 responses."""

from __future__ import annotations

import os
import re
import unittest

from aiohttp import web as aiohttp_web
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted(
        (luminance(foreground), luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


class TestBrandedPublic404(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()

        async def missing_page(_request):
            raise aiohttp_web.HTTPNotFound()

        async def missing_json(_request):
            return aiohttp_web.json_response({"error": "missing"}, status=404)

        app.router.add_get("/__test-page-missing", missing_page)
        app.router.add_get("/__test-json-missing", missing_json)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_unknown_browser_page_uses_branded_html_with_helpful_links(self):
        response = await self.client.get(
            "/route-that-does-not-exist?source=test",
            headers={"Accept": "text/html,application/xhtml+xml"},
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(response.content_type, "text/html")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        body = await response.text()
        self.assertIn("<title>Page not found | Unchained</title>", body)
        self.assertIn('href="/"', body)
        self.assertIn('href="/demo"', body)
        self.assertIn('href="/trial"', body)
        self.assertIn('href="/mcp"', body)

    async def test_trial_cta_meets_aa_contrast_in_all_states(self):
        response = await self.client.get(
            "/missing-page",
            headers={"Accept": "text/html"},
        )
        body = await response.text()
        colors = dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{6})", body))

        self.assertIn(
            ".nav-links .trial{padding:8px 13px;border-radius:7px;"
            "background:var(--accent);color:var(--bg)}",
            body,
        )
        self.assertIn(
            ".nav-links .trial:hover,.nav-links .trial:focus-visible"
            "{background:var(--trial-hover);color:var(--bg)}",
            body,
        )
        self.assertNotIn("color:#fff!important", body)
        for state_background in ("accent", "trial-hover"):
            with self.subTest(state=state_background):
                self.assertGreaterEqual(
                    _contrast_ratio(colors["bg"], colors[state_background]),
                    4.5,
                )

    async def test_non_page_requests_keep_aiohttp_plain_404(self):
        requests = (
            ("GET", "/missing", {"Accept": "application/json"}),
            ("GET", "/missing", {"Accept": "text/html;q=0,application/json"}),
            ("POST", "/missing", {"Accept": "text/html"}),
            ("GET", "/api/missing", {"Accept": "text/html"}),
            ("GET", "/web/missing", {"Accept": "text/html"}),
            ("GET", "/auth/missing", {"Accept": "text/html"}),
            ("GET", "/core/missing", {"Accept": "text/html"}),
            ("GET", "/mcp/missing", {"Accept": "text/html"}),
            ("GET", "/tunnel/missing", {"Accept": "text/html"}),
        )

        for method, path, headers in requests:
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path, headers=headers)
                self.assertEqual(response.status, 404)
                self.assertEqual(response.content_type, "text/plain")
                self.assertEqual(await response.text(), "404: Not Found")

    async def test_missing_resource_on_page_route_uses_branded_html(self):
        response = await self.client.get(
            "/__test-page-missing",
            headers={"Accept": "text/html"},
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn("This tab took a wrong turn.", await response.text())

    async def test_returned_json_404_is_not_rewritten(self):
        response = await self.client.get(
            "/__test-json-missing",
            headers={"Accept": "text/html"},
        )

        self.assertEqual(response.status, 404)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(await response.json(), {"error": "missing"})

    async def test_registered_public_pages_are_not_intercepted(self):
        for path in ("/", "/demo", "/trial", "/mcp"):
            with self.subTest(path=path):
                response = await self.client.get(path, headers={"Accept": "text/html"})
                self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
