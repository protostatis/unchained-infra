"""Tests for chat status reporting on the local chat page."""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))

from agent_package import VERSION
from chat_agent_cli import (
    _DEFAULT_RESEARCH_DESK_PACKAGE_URL,
    _ensure_research_desk_bridge_running,
    _ensure_research_desk_server_running,
    _research_desk_launcher_prefix,
    _research_desk_package_url,
    _research_desk_python_binary,
    _run_research_desk_install_helper,
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
    async def test_chat_install_research_desk_requires_local_client_update_first(self, mock_auth, mock_agent_request):
        from web_app.handlers.chat_flow import handle_chat_install_research_desk

        mock_auth.return_value = {
            "user_id": "u-test",
            "agent_id": "claude-oldinstall",
            "key_hash": "oldinstall",
            "key": "uc_live_test",
            "email": "dev@example.com",
        }
        web._chat_agents["claude-oldinstall"] = SimpleNamespace(closed=False)
        web._chat_agent_caps["claude-oldinstall"] = {
            "client_version": "0.3.62",
            "remote_update": True,
            "remote_research_desk_install": True,
        }

        response = await handle_chat_install_research_desk(SimpleNamespace())
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 409)
        self.assertTrue(data["update_required"])
        self.assertTrue(data["update_supported"])
        self.assertEqual(data["client_version"], "0.3.62")
        self.assertEqual(data["required_client_version"], "0.3.63")
        self.assertIn("Update it to at least 0.3.63", data["error"])
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
            "client_version": "0.3.63",
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
    def test_research_desk_python_binary_avoids_agent_venv_for_user_install(self):
        def fake_which(name, path=None):
            self.assertEqual(name, "python3")
            if path == "/usr/bin":
                return "/usr/bin/python3"
            return "/tmp/agent-venv/bin/python3"

        with patch("chat_agent_cli.shutil.which", side_effect=fake_which):
            with patch("chat_agent_cli.sys.prefix", "/tmp/agent-venv"):
                with patch("chat_agent_cli.sys.base_prefix", "/usr"):
                    self.assertEqual(_research_desk_python_binary(), "/usr/bin/python3")

    def test_research_desk_python_binary_uses_fallback_candidate_when_base_lookup_misses(self):
        def fake_which(name, path=None):
            self.assertEqual(name, "python3")
            return "/tmp/agent-venv/bin/python3" if path is None else None

        with patch("chat_agent_cli.shutil.which", side_effect=fake_which):
            with patch("chat_agent_cli.sys.prefix", "/tmp/agent-venv"):
                with patch("chat_agent_cli.sys.base_prefix", "/usr"):
                    with patch(
                        "chat_agent_cli.os.path.isfile",
                        side_effect=lambda p: p == "/usr/bin/python3",
                    ):
                        with patch(
                            "chat_agent_cli.os.access",
                            side_effect=lambda p, mode: p == "/usr/bin/python3",
                        ):
                            self.assertEqual(_research_desk_python_binary(), "/usr/bin/python3")

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

    def test_research_desk_bridge_start_skips_when_default_port_is_live(self):
        with patch("chat_agent_cli._localhost_port_open", return_value=True):
            with patch("chat_agent_cli._run_logged") as mock_run:
                _ensure_research_desk_bridge_running("/usr/bin/python3")
        mock_run.assert_not_called()

    def test_research_desk_server_start_skips_when_local_port_is_live(self):
        with patch("chat_agent_cli._localhost_port_open", return_value=True):
            with patch("chat_agent_cli._spawn_detached") as mock_spawn:
                _ensure_research_desk_server_running("/usr/bin/python3")
        mock_spawn.assert_not_called()

    def test_research_desk_bridge_start_uses_timeout(self):
        with patch("chat_agent_cli._localhost_port_open", return_value=False):
            with patch("chat_agent_cli._wait_for_local_port", return_value=True):
                with patch("chat_agent_cli._agent_root", return_value="/tmp/unchained-agent"):
                    with patch("chat_agent_cli.os.path.isfile", return_value=False):
                        with patch("chat_agent_cli._run_logged", return_value=0) as mock_run:
                            _ensure_research_desk_bridge_running("/usr/bin/python3")
        self.assertEqual(mock_run.call_args.kwargs.get("timeout_seconds"), 20.0)

    def test_research_desk_server_start_logs_detached_output(self):
        with patch("chat_agent_cli._localhost_port_open", return_value=False):
            with patch("chat_agent_cli._wait_for_local_port", return_value=True):
                with patch("chat_agent_cli._agent_root", return_value="/tmp/unchained-agent"):
                    with patch("chat_agent_cli._spawn_detached") as mock_spawn:
                        _ensure_research_desk_server_running("/usr/bin/python3")
        self.assertEqual(
            mock_spawn.call_args.kwargs.get("log_path"),
            os.path.join(os.path.expanduser("~/.unchained"), "research-desk-serve.log"),
        )

    def test_research_desk_install_helper_runs_setup_and_bootstrap(self):
        commands: list[list[str]] = []

        def fake_run_logged(cmd, *, cwd, timeout_seconds=None):
            del cwd
            del timeout_seconds
            commands.append(list(cmd))
            return 0

        with patch("chat_agent_cli._research_desk_python_binary", return_value="/usr/bin/python3"):
            with patch(
                "chat_agent_cli._research_desk_package_url",
                return_value="https://github.com/protostatis/unchained_pyreplab/archive/refs/tags/v0.1.0.zip",
            ):
                with patch("chat_agent_cli._run_logged", side_effect=fake_run_logged):
                    with patch("chat_agent_cli._localhost_port_open", return_value=False):
                        with patch("chat_agent_cli._wait_for_local_port", return_value=True):
                            with patch("chat_agent_cli._spawn_detached") as mock_spawn:
                                with patch("chat_agent_cli._agent_root", return_value="/tmp/unchained-agent"):
                                    with patch(
                                        "chat_agent_cli.os.path.isfile",
                                        side_effect=lambda path: path == "/tmp/unchained-agent/unchained/chrome_bridge.py",
                                    ):
                                        _run_research_desk_install_helper()

        self.assertEqual(
            commands[0],
            [
                "/usr/bin/python3",
                "-m",
                "pip",
                "install",
                "--user",
                "--upgrade",
                "https://github.com/protostatis/unchained_pyreplab/archive/refs/tags/v0.1.0.zip",
            ],
        )
        self.assertEqual(commands[1], ["/usr/bin/python3", "-m", "unchained_pyreplab", "setup"])
        self.assertEqual(
            commands[2],
            [
                "/usr/bin/python3",
                "-m",
                "unchained_pyreplab",
                "bridge-start",
                "--daemon",
                "--bridge-dir",
                "/tmp/unchained-agent/unchained",
            ],
        )
        mock_spawn.assert_called_once_with(
            [
                "/usr/bin/python3",
                "-m",
                "unchained_pyreplab",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8766",
            ],
            cwd=ANY,
            log_path=os.path.join(os.path.expanduser("~/.unchained"), "research-desk-serve.log"),
        )


if __name__ == "__main__":
    unittest.main()
