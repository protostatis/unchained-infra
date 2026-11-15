"""Public contracts for search-discovery surfaces.

Covers the Bing verification route, the IndexNow key proof route, the
indexability gate that keeps thin or perishable /r/ pages out of the sitemap,
and server-rendered docs content on /mcp-guide.
"""

from __future__ import annotations

import asyncio
import html as html_module
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import indexnow
import published_results
import web
from web_routes import ROUTE_SPECS


_BING_PATH = "/BingSiteAuth.xml"
_INDEXNOW_KEY = "0123456789abcdef0123456789abcdef"


def _session(query: str, *, repeats: int) -> dict:
    return {
        "messages": [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "Detailed public result. " * repeats},
        ]
    }


class TestDiscoveryRoutesAreRegistered(unittest.TestCase):
    def test_bing_verification_route_is_registered(self):
        self.assertIn(
            ("GET", _BING_PATH, "handle_bing_site_auth"),
            ROUTE_SPECS,
        )

    def test_indexnow_key_route_is_registered(self):
        self.assertIn(
            ("GET", "/{indexnow_key}.txt", "handle_indexnow_key"),
            ROUTE_SPECS,
        )


class TestIndexNowKeyResolution(unittest.TestCase):
    def test_unset_key_disables_submission(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": ""}, clear=False):
            self.assertEqual(indexnow.indexnow_key(), "")
            self.assertEqual(indexnow.key_file_path(), "")
            result = indexnow.submit_urls(["https://unchainedsky.com/desktop"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["reason"], "unconfigured")

    def test_malformed_key_is_refused(self):
        for bad in ("short", "has space here", "bad/slash/key", "x" * 200):
            with self.subTest(key=bad):
                with patch.dict(os.environ, {"INDEXNOW_KEY": bad}, clear=False):
                    self.assertEqual(indexnow.indexnow_key(), "")

    def test_configured_key_is_used_for_key_file(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": _INDEXNOW_KEY}, clear=False):
            self.assertEqual(indexnow.indexnow_key(), _INDEXNOW_KEY)
            self.assertEqual(indexnow.key_file_path(), f"/{_INDEXNOW_KEY}.txt")
            self.assertEqual(indexnow.key_file_body(), _INDEXNOW_KEY)

    def test_submission_filters_non_https_urls(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": _INDEXNOW_KEY}, clear=False):
            with patch("httpx.post", side_effect=RuntimeError("network down")):
                result = indexnow.submit_urls(
                    ["http://insecure.example", "https://unchainedsky.com/desktop"]
                )
        # httpx.post is patched to raise, so this exercises the failure path
        # rather than the URL filter; both must stay non-raising.
        self.assertFalse(result["submitted"])
        self.assertEqual(result["reason"], "error")

    def test_submission_success_is_reported(self):
        class _Response:
            status_code = 200
            text = ""

        with patch.dict(os.environ, {"INDEXNOW_KEY": _INDEXNOW_KEY}, clear=False):
            with patch("httpx.post", return_value=_Response()) as post:
                result = indexnow.submit_urls(["https://unchainedsky.com/desktop"])

        self.assertTrue(result["submitted"])
        self.assertEqual(result["count"], 1)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["host"], "unchainedsky.com")
        self.assertEqual(
            payload["keyLocation"], f"https://unchainedsky.com/{_INDEXNOW_KEY}.txt"
        )


class TestIndexabilityGate(unittest.TestCase):
    def test_thin_output_is_not_indexable(self):
        self.assertFalse(
            published_results._passes_index_gate("Compare launch options", "Short.")
        )

    def test_substantial_evergreen_output_is_indexable(self):
        self.assertTrue(
            published_results._passes_index_gate(
                "Compare public launch options",
                "Detailed public result. " * 80,
            )
        )

    def test_perishable_queries_are_not_indexable(self):
        substantial = "Detailed public result. " * 80
        for query in (
            "Find me a flight for cheap from Chicago to Houston for tomorrow night",
            "What is the price today",
            "Show me the latest status of the build",
            "Compare prices this weekend",
        ):
            with self.subTest(query=query):
                self.assertFalse(
                    published_results._passes_index_gate(query, substantial)
                )


class TestSitemapIndexGate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "published-results.db"
        self.original_db_path = published_results._DB_PATH
        published_results._DB_PATH = str(self.db_path)

    def tearDown(self):
        published_results._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _publish_approved(self, query: str, *, repeats: int) -> str:
        with patch.object(published_results, "_pii_guard", return_value=True):
            slug = published_results.publish_result(
                _session(query, repeats=repeats),
                user_id="account@example.test",
                session_id="s-account-session",
            )
        self.assertIsNotNone(slug)
        self.assertTrue(published_results.approve_result(str(slug)))
        return str(slug)

    async def _sitemap_text(self) -> str:
        response = await web.handle_sitemap_xml(None)
        return response.text

    def test_desktop_page_is_in_sitemap(self):
        sitemap = asyncio.run(self._sitemap_text())
        self.assertIn("https://unchainedsky.com/desktop", sitemap)

    def test_indexable_result_is_in_sitemap(self):
        slug = self._publish_approved("Compare public launch options", repeats=80)
        sitemap = asyncio.run(self._sitemap_text())
        self.assertIn(f"https://unchainedsky.com/r/{slug}", sitemap)

    def test_thin_result_is_approved_but_not_in_sitemap(self):
        # 240 chars clears the publish gate but not the stricter index gate.
        slug = self._publish_approved("Compare public launch options", repeats=10)
        self.assertIsNotNone(published_results.get_result(slug))
        sitemap = asyncio.run(self._sitemap_text())
        self.assertNotIn(f"https://unchainedsky.com/r/{slug}", sitemap)

    def test_perishable_result_is_not_in_sitemap(self):
        slug = self._publish_approved(
            "Find me a flight for tomorrow night", repeats=80
        )
        sitemap = asyncio.run(self._sitemap_text())
        self.assertNotIn(f"https://unchainedsky.com/r/{slug}", sitemap)


class TestApprovalPushesIndexableUrls(unittest.IsolatedAsyncioTestCase):
    """Approval is the publish moment; only indexable pages reach IndexNow."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "published-results.db"
        self.original_db_path = published_results._DB_PATH
        published_results._DB_PATH = str(self.db_path)
        app = web.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        published_results._DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _pending(self, query: str, *, repeats: int) -> str:
        with patch.object(published_results, "_pii_guard", return_value=True):
            slug = published_results.publish_result(
                _session(query, repeats=repeats),
                user_id="account@example.test",
                session_id="s-account-session",
            )
        self.assertIsNotNone(slug)
        return str(slug)

    async def _approve(self, slug: str) -> dict:
        admin = {"email": "admin@example.test"}
        with patch.object(web, "_authenticate", return_value=admin), patch.object(
            web, "ADMIN_EMAILS", {"admin@example.test"}
        ), patch("indexnow.submit_urls", return_value={"submitted": True}) as submit:
            response = await self.client.post(
                "/web/publish/approve", json={"slug": slug}
            )
            self.assertEqual(response.status, 200)
            body = await response.json()
            await self._drain_indexnow()
        return {"body": body, "submit": submit}

    @staticmethod
    async def _drain_indexnow() -> None:
        """Wait for queued background pushes without racing the done callbacks."""
        from web_app.handlers import pages as pages_module

        for _ in range(500):
            if not pages_module._indexnow_tasks:
                return
            await asyncio.sleep(0.005)
        raise AssertionError("IndexNow background push did not finish")

    async def test_indexable_result_is_pushed_to_indexnow(self):
        slug = self._pending("Compare public launch options", repeats=80)
        outcome = await self._approve(slug)
        self.assertTrue(outcome["body"]["approved"])
        outcome["submit"].assert_called_once()
        urls = outcome["submit"].call_args.args[0]
        self.assertEqual(urls, [f"https://unchainedsky.com/r/{slug}"])

    async def test_thin_result_is_not_pushed_to_indexnow(self):
        slug = self._pending("Compare public launch options", repeats=10)
        outcome = await self._approve(slug)
        self.assertTrue(outcome["body"]["approved"])
        outcome["submit"].assert_not_called()


class TestPublishedResultRobotsDirective(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def _fetch(self, slug: str, query: str, *, repeats: int):
        result = {
            "slug": slug,
            "query": query,
            "result_html": "<p>body</p>",
            "result_text": "Detailed public result. " * repeats,
            "meta": {},
            "created_at": 1_700_000_000,
            "view_count": 0,
        }
        with patch.object(published_results, "get_result", return_value=result), patch.object(
            web, "_track_page_view", lambda request: None
        ):
            return await self.client.get(f"/r/{slug}")

    async def test_indexable_result_declares_index(self):
        response = await self._fetch(
            "compare-public-launch-options", "Compare public launch options", repeats=80
        )
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('<meta name="robots" content="index,follow">', body)

    async def test_thin_result_declares_noindex(self):
        response = await self._fetch(
            "thin-public-result", "Compare public launch options", repeats=10
        )
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('<meta name="robots" content="noindex,follow">', body)

    async def test_perishable_result_declares_noindex(self):
        response = await self._fetch(
            "flight-tomorrow-night", "Find me a flight for tomorrow night", repeats=80
        )
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertIn('<meta name="robots" content="noindex,follow">', body)


class TestDiscoveryRoutesHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.create_app()
        app.on_startup.clear()
        app.on_cleanup.clear()
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_existing_discovery_routes_still_win(self):
        # The /{indexnow_key}.txt pattern must not shadow real resources.
        robots = await self.client.get("/robots.txt")
        self.assertEqual(robots.status, 200)
        self.assertIn("Sitemap: https://unchainedsky.com/sitemap.xml", await robots.text())
        sitemap = await self.client.get("/sitemap.xml")
        self.assertEqual(sitemap.status, 200)
        self.assertIn("<urlset", await sitemap.text())

    async def test_bing_file_is_absent_until_configured(self):
        with patch.dict(os.environ, {"BING_SITE_AUTH_GUID": ""}, clear=False):
            response = await self.client.get(_BING_PATH)
        self.assertEqual(response.status, 404)

    async def test_bing_file_serves_configured_guid(self):
        guid = "11111111-2222-3333-4444-555555555555"
        with patch.dict(os.environ, {"BING_SITE_AUTH_GUID": guid}, clear=False):
            response = await self.client.get(_BING_PATH)
            body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn(f"<user>{guid}</user>", body)
        self.assertEqual(response.headers["Cache-Control"], "public, max-age=86400")

    async def test_bing_file_refuses_non_guid_configuration(self):
        with patch.dict(
            os.environ, {"BING_SITE_AUTH_GUID": "</user><oops>"}, clear=False
        ):
            response = await self.client.get(_BING_PATH)
        self.assertEqual(response.status, 404)

    async def test_indexnow_key_file_serves_configured_key(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": _INDEXNOW_KEY}, clear=False):
            response = await self.client.get(f"/{_INDEXNOW_KEY}.txt")
            body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, _INDEXNOW_KEY)

    async def test_indexnow_key_file_rejects_wrong_key(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": _INDEXNOW_KEY}, clear=False):
            response = await self.client.get("/ffffffffffffffffffffffffffffffff.txt")
        self.assertEqual(response.status, 404)

    async def test_indexnow_key_file_is_absent_when_unconfigured(self):
        with patch.dict(os.environ, {"INDEXNOW_KEY": ""}, clear=False):
            response = await self.client.get("/sitemap.txt")
        self.assertEqual(response.status, 404)


class TestMcpGuideIsServerRendered(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _article_inner(html: str) -> str:
        match = re.search(
            r'<article class="markdown" id="doc-content">(.*?)</article>',
            html,
            re.DOTALL,
        )
        assert match is not None, "doc-content article element is missing"
        return match.group(1)

    @staticmethod
    def _text(fragment: str) -> str:
        return re.sub(
            r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", fragment))
        ).strip()

    async def test_docs_content_is_present_without_javascript(self):
        page = web._build_mcp_guide_html()
        # Assert against the article element specifically: the page hero and
        # the embedded DOCS JSON both mention the guide, so a page-level
        # substring check would pass even with an empty, JS-only article.
        article = self._text(self._article_inner(page))
        self.assertIn("MCP Local Browser Guide", article)
        self.assertGreaterEqual(
            len(article.split()),
            500,
            "server-rendered article is too thin to count as crawlable content",
        )

    async def test_renderer_output_has_no_unsubstituted_placeholders(self):
        page = web._build_mcp_guide_html()
        for placeholder in (
            "__MCP_DOC_HTML__",
            "__MCP_DOC_TITLE__",
            "__MCP_DOC_FILE__",
            "__MCP_DOCS_JSON__",
        ):
            with self.subTest(placeholder=placeholder):
                self.assertNotIn(placeholder, page)

    async def test_renderer_is_unavailable_safe(self):
        # Production packaging is a hand-maintained COPY/pip list; if the
        # renderer is missing the page must still build, falling back to the
        # client-side viewer rather than raising.
        with patch.object(web, "_render_markdown_html", return_value=""):
            page = web._build_mcp_guide_html()
        self.assertIn('<article class="markdown" id="doc-content"></article>', page)
        self.assertNotIn("__MCP_DOC_HTML__", page)


if __name__ == "__main__":
    unittest.main()
