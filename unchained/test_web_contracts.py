"""Contract tests for web surface area.

These tests protect public routes and exported template contracts while
`web.py` is gradually split into modules.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch
import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web


class TestWebRouteContracts(unittest.TestCase):
    """Ensure runtime app wiring continues to expose expected public routes."""

    def _runtime_routes(self) -> set[tuple[str, str]]:
        captured: dict[str, object] = {}

        def _fake_run_app(app, *args, **kwargs):
            del args, kwargs
            captured["app"] = app

        with patch.object(
            web.argparse.ArgumentParser,
            "parse_args",
            return_value=SimpleNamespace(host="127.0.0.1", port=8080),
        ):
            with patch.object(web.web, "run_app", side_effect=_fake_run_app):
                with patch("builtins.print"):
                    web.main()

        app = captured.get("app")
        self.assertIsNotNone(app, "main() should pass an app to web.run_app")
        return {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
            if route.method in {"GET", "POST"}
        }

    def test_main_registers_public_routes(self):
        actual = self._runtime_routes()
        expected = {
            ("GET", "/favicon.svg"),
            ("GET", "/"),
            ("GET", "/test"),
            ("POST", "/auth/google"),
            ("POST", "/auth/logout"),
            ("GET", "/auth/me"),
            ("POST", "/web/cmd"),
            ("GET", "/setup"),
            ("GET", "/admin"),
            ("GET", "/admin/users"),
            ("GET", "/admin/pending"),
            ("POST", "/admin/approve"),
            ("POST", "/admin/reject"),
            ("GET", "/chat"),
            ("GET", "/trial"),
            ("GET", "/chat-gemini"),
            ("GET", "/demo"),
            ("GET", "/app"),
            ("GET", "/chat/ws"),
            ("POST", "/web/chat"),
            ("POST", "/web/chat/cancel"),
            ("GET", "/web/chat/status"),
            ("GET", "/web/chat/history"),
            ("POST", "/web/chat/new"),
            ("GET", "/web/chat/slots"),
            ("POST", "/web/chat/switch"),
            ("GET", "/web/download-agent"),
            ("POST", "/web/install-token"),
            ("POST", "/web/install/bootstrap"),
            ("GET", "/install/{token}"),
            ("GET", "/trial/connector"),
            ("POST", "/trial/token"),
            ("GET", "/trial/{token}"),
            ("GET", "/web/agent/version"),
            ("GET", "/web/agent/files"),
            ("GET", "/web/provision/profiles"),
            ("POST", "/web/provision/start"),
            ("GET", "/web/provision/status"),
            ("POST", "/web/provision/confirm"),
            ("POST", "/web/provision/save-manual"),
            ("POST", "/web/provision/revoke"),
            ("GET", "/scheduler"),
            ("GET", "/web/scheduler/jobs"),
            ("POST", "/web/scheduler/jobs"),
            ("POST", "/web/scheduler/preview"),
            ("GET", "/web/scheduler/history"),
        }
        if not web.GOOGLE_CLIENT_ID:
            expected.add(("POST", "/auth/dev"))

        for route in expected:
            self.assertIn(route, actual, f"missing runtime route: {route}")


class TestWebTemplateContracts(unittest.TestCase):
    """Ensure web templates keep stable exported symbols."""

    def test_chat_pages_exported(self):
        templates = [
            "LANDING_HTML",
            "HTML",
            "TRIAL_CHAT_HTML",
            "CHAT_GEMINI_HTML",
            "HEADLESS_DEMO_HTML",
            "CLAUDE_CHAT_HTML",
            "SCHEDULER_HTML",
            "SETUP_HTML",
            "ADMIN_HTML",
        ]
        for name in templates:
            value = getattr(web, name, "")
            self.assertIsInstance(value, str, f"{name} should be a string")
            self.assertIn("<!DOCTYPE html>", value, f"{name} should be full HTML")

    def test_template_js_contract_markers(self):
        self.assertIn("showInstallCmd", web.CLAUDE_CHAT_HTML)
        self.assertIn("copyInstallCmd", web.CLAUDE_CHAT_HTML)
        self.assertIn("closeInstallModal", web.CLAUDE_CHAT_HTML)
        self.assertIn('id="modelsel"', web.CLAUDE_CHAT_HTML)
        self.assertIn("model: currentModel()", web.CLAUDE_CHAT_HTML)
        self.assertIn('id="f-model"', web.SCHEDULER_HTML)
        self.assertIn("getSchedulerModelValue()", web.SCHEDULER_HTML)
        self.assertIn("openHistoryModal", web.SCHEDULER_HTML)


if __name__ == "__main__":
    unittest.main()
