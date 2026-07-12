"""Content and route contracts for the public legal pages."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
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
        self.assertIn("Google, Facebook, or GitHub sign-in", html)
        self.assertIn("GitHub access token", html)
        self.assertIn("random per-record salt", html)
        self.assertIn("Hosted trial conversations", html)
        self.assertIn("provider-agent histories default to container-local files", html)
        self.assertIn("may disappear when the runtime container is recreated", html)
        self.assertIn("Local CLI chat slots and archives are stored on the machine", html)
        self.assertIn("scheduler job prompts", html)
        self.assertIn("run outputs", html)
        self.assertIn("Optional public results", html)
        self.assertIn("first prompt, rendered visible conversation/result", html)
        self.assertIn("account identifier, source session ID", html)
        self.assertIn("through OpenRouter for PII classification", html)
        self.assertIn("limited to 8,000 characters of assistant output", html)
        self.assertIn("stored with <code>pending</code> status", html)
        self.assertIn("available without authentication", html)
        self.assertIn("added to the public sitemap", html)
        self.assertIn("AI systems or training datasets", html)
        self.assertIn("50 most recent records per job", html)
        self.assertIn("90-day cleanup window", html)
        self.assertIn("Revoking a saved provider credential marks it inactive", html)
        self.assertIn("does not by itself delete existing state or run-history files", html)
        self.assertIn("no guaranteed retention period", html)
        self.assertIn("Rejecting a pending public result deletes its database row", html)
        self.assertIn("does not automatically remove a pending or approved result", html)
        self.assertIn("There is no one-click full-account deletion endpoint", html)
        self.assertIn("pending or approved public-result records", html)
        self.assertIn("A server-side deletion request cannot remove local CLI chats", html)
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
        self.assertIn("encrypted provider-credential records (including inactive records)", html)
        self.assertIn("hosted conversation files", html)
        self.assertIn("scheduler definitions/state/run history", html)
        self.assertIn("account-linked analytics records", html)
        self.assertIn("pending or approved public-result records", html)
        self.assertIn("deleting an account or source conversation does not automatically delete them", html)
        self.assertIn("Requests are handled manually.", html)
        self.assertIn("Revoking a provider credential only marks it inactive", html)
        self.assertIn("Deleting a scheduler job does not remove its prior state or run history", html)
        self.assertIn("Records that must be retained for security or legal reasons", html)
        self.assertIn("Approved public results have no self-service deletion control", html)
        self.assertIn("public-result copies already indexed, cached, copied, or redistributed", html)
        self.assertIn("cannot delete local CLI chat slots or archives", html)
        self.assertIn("contact third parties about data they control", html)
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


class TestLegalPageDeploymentConfig(unittest.TestCase):
    @staticmethod
    def _compose_service(compose: str, name: str, next_name: str) -> str:
        return compose.split(f"\n  {name}:\n", 1)[1].split(
            f"\n  {next_name}:\n", 1
        )[0]

    def test_compose_passes_public_contact_email_to_web_service(self):
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        compose = compose_path.read_text(encoding="utf-8")
        web_service = self._compose_service(compose, "web", "scheduler")

        self.assertIn(
            "- CONTACT_EMAIL=${CONTACT_EMAIL:-hello@unchainedsky.com}",
            web_service,
        )

    def test_compose_only_persists_trial_agent_session_directory(self):
        compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
        compose = compose_path.read_text(encoding="utf-8")
        web_service = self._compose_service(compose, "web", "scheduler")
        trial_service = compose.split("\n  trial-agent:\n", 1)[1].split(
            "\nvolumes:\n", 1
        )[0]

        self.assertIn("- relay_data:/data", web_service)
        self.assertNotIn("SESSION_DIR=", web_service)
        self.assertIn("- relay_data:/data", trial_service)
        self.assertIn("- SESSION_DIR=/data/sessions", trial_service)


if __name__ == "__main__":
    unittest.main()
