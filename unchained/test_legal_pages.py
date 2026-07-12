"""Content and route contracts for the public legal pages."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web
from aiohttp.test_utils import make_mocked_request
from web_app.handlers import pages
from web_app.routes import ROUTE_SPECS


class TestLegalPageRoutes(unittest.TestCase):
    def test_legal_routes_keep_their_public_handlers(self):
        routes = {(method, path): handler for method, path, handler in ROUTE_SPECS}

        self.assertEqual(
            routes[("GET", "/privacy")],
            "web_app.handlers.pages:handle_privacy_page",
        )
        self.assertEqual(
            routes[("GET", "/privacy-policy")],
            "web_app.handlers.pages:handle_privacy_page",
        )
        self.assertEqual(
            routes[("GET", "/data-deletion")],
            "web_app.handlers.pages:handle_data_deletion_page",
        )


class TestLegalPageContent(unittest.IsolatedAsyncioTestCase):
    async def _render(self, handler, path: str):
        core = SimpleNamespace(
            CONTACT_EMAIL="hello@unchainedsky.com",
            _track_page_view=Mock(),
        )
        request = make_mocked_request("GET", path)
        with patch.object(pages, "_core", return_value=core):
            response = await handler(request)
        core._track_page_view.assert_called_once_with(request)
        return response

    async def test_privacy_page_preserves_copy_and_uses_branded_shell(self):
        response = await self._render(pages.handle_privacy_page, "/privacy")
        html = response.text

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        self.assertIn('<header class="site-header">', html)
        self.assertIn('aria-label="Primary navigation"', html)
        self.assertIn('<footer class="site-footer">', html)
        self.assertIn('href="#main-content">Skip to content</a>', html)
        self.assertIn('@media (max-width: 680px)', html)
        self.assertIn('aria-current="page">Privacy</a>', html)
        self.assertIn("Last updated: <time datetime=\"2026-07-12\">July 12, 2026</time>", html)
        self.assertNotIn("March 6, 2026", html)
        self.assertIn("We do not sell personal data.", html)
        self.assertIn("We retain account and operational data for as long as needed", html)
        self.assertIn('mailto:hello@unchainedsky.com', html)
        self.assertNotIn("@gmail.com", html)

    async def test_data_deletion_page_preserves_instructions_and_status(self):
        response = await self._render(pages.handle_data_deletion_page, "/data-deletion")
        html = response.text

        self.assertEqual(response.status, 200)
        self.assertIn('<h1 id="page-title">User Data Deletion</h1>', html)
        self.assertIn('aria-current="page">Data deletion</a>', html)
        self.assertIn("send a request from your account email", html)
        self.assertIn("Subject line: <code>Data Deletion Request</code>.", html)
        self.assertIn("We verify account ownership.", html)
        self.assertIn("We delete or anonymize eligible personal data from active systems.", html)
        self.assertIn('href="/privacy">/privacy</a>', html)
        self.assertIn('mailto:hello@unchainedsky.com', html)

    def test_public_contact_fallback_never_exposes_admin_email(self):
        with patch.dict(
            os.environ,
            {"CONTACT_EMAIL": "", "ADMIN_EMAILS": "personal.account@gmail.com"},
        ):
            self.assertEqual(web._resolve_contact_email(), "hello@unchainedsky.com")

    def test_public_contact_honors_explicit_configuration(self):
        with patch.dict(os.environ, {"CONTACT_EMAIL": "support@example.test"}):
            self.assertEqual(web._resolve_contact_email(), "support@example.test")


if __name__ == "__main__":
    unittest.main()
