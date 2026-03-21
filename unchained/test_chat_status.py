"""Tests for chat status reporting on the local chat page."""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))

from agent_package import VERSION
from chat_agent_cli import (
    _DEFAULT_RESEARCH_DESK_PACKAGE_URL,
    _research_desk_launcher_prefix,
    _research_desk_package_url,
)
import web


class TestHandleChatStatus(unittest.IsolatedAsyncioTestCase):
    """Ensure /web/chat/status reports chat and browser bridge separately."""

    def setUp(self):
        self._chat_agents = dict(web._chat_agents)
        self._chat_agent_users = dict(web._chat_agent_users)
        self._chat_agent_caps = dict(web._chat_agent_caps)
        web._chat_agents.clear()
        web._chat_agent_users.clear()
        web._chat_agent_caps.clear()

    def tearDown(self):
        web._chat_agents.clear()
        web._chat_agents.update(self._chat_agents)
        web._chat_agent_users.clear()
        web._chat_agent_users.update(self._chat_agent_users)
        web._chat_agent_caps.clear()
        web._chat_agent_caps.update(self._chat_agent_caps)

    @patch("web._check_relay_agent", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_only_status_reports_bridge_separately(self, mock_auth, mock_check_relay):
        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        mock_check_relay.return_value = True

        request = SimpleNamespace(query={"chat_only": "1", "model": "claude-sonnet-4-6"})
        response = await web.handle_chat_status(request)
        data = json.loads(response.body.decode())

        self.assertFalse(data["connected"])
        self.assertFalse(data["chat_connected"])
        self.assertTrue(data["bridge_connected"])
        self.assertEqual(data["chat_agent_id"], "claude-abc12345")
        self.assertEqual(data["bridge_agent_id"], "claude-abc12345")
        self.assertIn("client_version", data)
        self.assertIn("server_version", data)
        self.assertFalse(data["client_connected"])

    @patch("web._check_relay_agent", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_only_status_reports_mismatch_independently(self, mock_auth, mock_check_relay):
        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-expected",
            "key_hash": "expected",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        mock_check_relay.return_value = False
        web._chat_agents["claude-other"] = SimpleNamespace(closed=False)
        web._chat_agent_users["claude-other"] = "u-test"

        request = SimpleNamespace(query={"chat_only": "1", "model": "claude-sonnet-4-6"})
        response = await web.handle_chat_status(request)
        data = json.loads(response.body.decode())

        self.assertFalse(data["chat_connected"])
        self.assertFalse(data["bridge_connected"])
        self.assertTrue(data["mismatch"])
        self.assertEqual(data["mismatch_agent_id"], "claude-other")

    @patch("web._check_relay_agent", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_status_marks_current_client_as_not_outdated(
        self, mock_auth, mock_check_relay
    ):
        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-updated",
            "key_hash": "updated",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        mock_check_relay.return_value = True
        web._chat_agents["claude-updated"] = SimpleNamespace(closed=False)
        web._chat_agent_users["claude-updated"] = "u-test"
        web._chat_agent_caps["claude-updated"] = {
            "client_version": VERSION,
            "remote_update": True,
        }

        request = SimpleNamespace(query={"chat_only": "1", "model": "claude-sonnet-4-6"})
        response = await web.handle_chat_status(request)
        data = json.loads(response.body.decode())

        self.assertTrue(data["client_connected"])
        self.assertEqual(data["client_version"], VERSION)
        self.assertTrue(data["client_update_supported"])
        self.assertFalse(data["client_outdated"])
        self.assertFalse(data["client_update_required"])

    @patch("web._check_relay_agent", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_status_requires_pre_restore_safety_client(
        self, mock_auth, mock_check_relay
    ):
        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-required",
            "key_hash": "required",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        mock_check_relay.return_value = True
        web._chat_agents["claude-required"] = SimpleNamespace(closed=False)
        web._chat_agent_users["claude-required"] = "u-test"
        web._chat_agent_caps["claude-required"] = {
            "client_version": "0.3.45",
            "remote_update": True,
        }

        request = SimpleNamespace(query={"chat_only": "1", "model": "claude-sonnet-4-6"})
        response = await web.handle_chat_status(request)
        data = json.loads(response.body.decode())

        self.assertTrue(data["client_connected"])
        self.assertEqual(data["client_version"], "0.3.45")
        self.assertTrue(data["client_outdated"])
        self.assertTrue(data["client_update_required"])

    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_update_client_rejects_current_client(self, mock_auth, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_update_client

        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-current",
            "key_hash": "current",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        web._chat_agents["claude-current"] = SimpleNamespace(closed=False)
        web._chat_agent_caps["claude-current"] = {
            "client_version": VERSION,
            "remote_update": True,
        }

        response = await handle_chat_update_client(SimpleNamespace())
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 409)
        self.assertIn("already current", data["error"].lower())
        mock_agent_request.assert_not_awaited()

    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_update_client_allows_outdated_client(self, mock_auth, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_update_client

        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-outdated",
            "key_hash": "outdated",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        web._chat_agents["claude-outdated"] = SimpleNamespace(closed=False)
        web._chat_agent_caps["claude-outdated"] = {
            "client_version": "0.3.37",
            "remote_update": True,
        }
        mock_agent_request.return_value = {"type": "update_client_ok", "status": "updating"}

        response = await handle_chat_update_client(SimpleNamespace())
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        mock_agent_request.assert_awaited_once()

    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_install_research_desk_requires_supported_client(self, mock_auth, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_install_research_desk

        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-nosupport",
            "key_hash": "nosupport",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        web._chat_agents["claude-nosupport"] = SimpleNamespace(closed=False)
        web._chat_agent_caps["claude-nosupport"] = {
            "client_version": "0.3.45",
            "remote_update": True,
            "remote_research_desk_install": False,
        }

        response = await handle_chat_install_research_desk(SimpleNamespace())
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 409)
        self.assertIn("does not support", data["error"])
        mock_agent_request.assert_not_awaited()

    @patch("web_app.handlers.chat_flow.agent_request", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_chat_install_research_desk_starts_remote_helper(self, mock_auth, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_install_research_desk

        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-install",
            "key_hash": "install",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        web._chat_agents["claude-install"] = SimpleNamespace(closed=False)
        web._chat_agent_caps["claude-install"] = {
            "client_version": "0.3.45",
            "remote_update": True,
            "remote_research_desk_install": True,
        }
        mock_agent_request.return_value = {
            "type": "install_research_desk_ok",
            "status": "installing",
            "launcher_prefix": "python3 -m unchained_pyreplab",
        }

        response = await handle_chat_install_research_desk(SimpleNamespace())
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["launcher_prefix"], "python3 -m unchained_pyreplab")
        mock_agent_request.assert_awaited_once()


class TestLocalChatTemplate(unittest.TestCase):
    """Protect local-chat status markers in the exported HTML."""

    def test_local_chat_html_has_separate_status_markers(self):
        self.assertIn('id="agentstatus"', web.CLAUDE_CHAT_HTML)
        self.assertIn('id="bridgestatus"', web.CLAUDE_CHAT_HTML)
        self.assertIn('id="banner-detail"', web.CLAUDE_CHAT_HTML)
        self.assertIn('id="client-update-btn"', web.CLAUDE_CHAT_HTML)
        self.assertIn("Browser bridge and chat agent are tracked separately.", web.CLAUDE_CHAT_HTML)

    def test_local_chat_update_button_only_enables_when_outdated(self):
        self.assertIn(
            "btn.disabled = !clientConnected || !updateSupported || !outdated;",
            web.CLAUDE_CHAT_HTML,
        )
        self.assertIn(
            "else if (clientUpdateSawDisconnect || !data.client_outdated) {",
            web.CLAUDE_CHAT_HTML,
        )


class TestResearchDeskInstallHelpers(unittest.TestCase):
    def test_research_desk_package_url_rejects_invalid_override(self):
        with patch.dict(
            os.environ,
            {"UNCHAINED_RESEARCH_DESK_PACKAGE_URL": "https://evil.example.com/pkg.zip"},
            clear=False,
        ):
            self.assertEqual(_research_desk_package_url(), _DEFAULT_RESEARCH_DESK_PACKAGE_URL)

    def test_research_desk_package_url_accepts_pinned_github_archive(self):
        override = (
            "https://github.com/protostatis/unchained_pyreplab/archive/"
            "refs/tags/v0.1.0.zip"
        )
        with patch.dict(
            os.environ,
            {"UNCHAINED_RESEARCH_DESK_PACKAGE_URL": override},
            clear=False,
        ):
            self.assertEqual(_research_desk_package_url(), override)

    def test_research_desk_launcher_prefix_quotes_spaced_python_paths(self):
        with patch("chat_agent_cli._research_desk_python_binary", return_value="/tmp/odd path/python3"):
            self.assertEqual(
                _research_desk_launcher_prefix(),
                "'/tmp/odd path/python3' -m unchained_pyreplab",
            )


if __name__ == "__main__":
    unittest.main()
