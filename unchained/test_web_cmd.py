"""Tests for /web/cmd command dispatch behavior."""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import web


class TestHandleCmd(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_session_tabs = dict(web._session_tabs)
        self._saved_allowed_tabs = {
            sid: set(tabs) for sid, tabs in web._session_allowed_tabs.items()
        }
        self._saved_session_agents = dict(web._session_agent_map)
        self._saved_session_profiles = dict(web._session_profile_paths)
        self._saved_expired_profiles = dict(web._expired_profile_sessions)
        web._session_tabs.clear()
        web._session_allowed_tabs.clear()
        web._session_agent_map.clear()
        web._session_profile_paths.clear()
        web._expired_profile_sessions.clear()

    def tearDown(self):
        web._session_tabs.clear()
        web._session_tabs.update(self._saved_session_tabs)
        web._session_allowed_tabs.clear()
        web._session_allowed_tabs.update(self._saved_allowed_tabs)
        web._session_agent_map.clear()
        web._session_agent_map.update(self._saved_session_agents)
        web._session_profile_paths.clear()
        web._session_profile_paths.update(self._saved_session_profiles)
        web._expired_profile_sessions.clear()
        web._expired_profile_sessions.update(self._saved_expired_profiles)

    def _request(self, body=None, *, json_exc: Exception | None = None, headers=None):
        req = SimpleNamespace(headers=headers or {})
        if json_exc is not None:
            req.json = AsyncMock(side_effect=json_exc)
        else:
            req.json = AsyncMock(return_value=(body if body is not None else {}))
        return req

    @patch("web._authenticate", return_value=None)
    async def test_requires_authentication(self, _mock_auth):
        request = self._request({"action": "text"})
        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 401)
        self.assertEqual(data, {"error": "Not authenticated"})

    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_rejects_unknown_action(self, mock_auth, _mock_parse):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        request = self._request({"action": "unknown"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 400)
        self.assertEqual(data, {"error": "Unknown action: unknown"})

    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_requires_action_and_server_side_agent_id(self, mock_auth, _mock_parse):
        mock_auth.return_value = {"user_id": "u1", "agent_id": ""}
        request = self._request({"action": "navigate", "url": "https://example.com"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 400)
        self.assertEqual(data, {"error": "action and agent_id required"})

    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_navigate_requires_url(self, mock_auth, _mock_parse):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        request = self._request({"action": "navigate"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 400)
        self.assertEqual(data, {"error": "url required"})

    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_invalid_json_body_returns_400(self, mock_auth, _mock_parse):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        request = self._request(json_exc=ValueError("bad json"))

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 400)
        self.assertEqual(data, {"error": "Invalid JSON body"})

    @patch("cloud_tools.navigate", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_maps_connect_errors_to_chrome_unavailable(self, mock_auth, _mock_parse, mock_nav):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        mock_nav.side_effect = RuntimeError("connection refused")
        request = self._request({"action": "navigate", "url": "https://example.com"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 502)
        self.assertIn("Chrome is not open", data.get("error", ""))

    @patch("cloud_tools.screenshot", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_screenshot_dispatch_shape(self, mock_auth, _mock_parse, mock_screenshot):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        mock_screenshot.return_value = "img-b64"
        request = self._request({"action": "screenshot", "tab_id": "auto"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data, {"type": "image", "data": "img-b64"})

    @patch("cloud_tools.click", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_click_casts_coordinates_to_int(self, mock_auth, _mock_parse, mock_click):
        mock_auth.return_value = {"user_id": "u1", "agent_id": "claude-abc"}
        mock_click.return_value = "ok"
        request = self._request({"action": "click", "x": "12", "y": "34", "tab_id": "auto"})

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data, {"type": "text", "data": "ok"})
        mock_click.assert_awaited_once_with("claude-abc", "auto", 12, 34, "relay.local", 8765)

    @patch("web._authenticate")
    async def test_rejects_foreign_chat_session(self, mock_auth):
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        request = self._request({
            "action": "navigate",
            "session_id": "s-claude-deadbeef-demo",
            "url": "https://example.com",
        })

        response = await web.handle_cmd(request)

        self.assertEqual(response.status, 403)
        self.assertIn("not owned", json.loads(response.body.decode())["error"])

    @patch("cloud_tools.navigate", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_chat_navigation_uses_active_tab_without_foregrounding(self, mock_auth, _mock_parse, mock_nav):
        sid = "s-claude-abc12345-demo"
        tab = "A" * 32
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        mock_nav.return_value = "Navigated"
        web._session_agent_map[sid] = "claude-abc12345"
        web._session_tabs[sid] = tab
        request = self._request({
            "action": "navigate",
            "session_id": sid,
            "tab_id": "auto",
            "url": "https://example.com",
        })

        response = await web.handle_cmd(request)

        self.assertEqual(response.status, 200)
        mock_nav.assert_awaited_once_with(
            "claude-abc12345",
            tab,
            "https://example.com",
            "relay.local",
            8765,
            bring_to_front=False,
        )

    @patch("cloud_tools.navigate", new_callable=AsyncMock)
    @patch("cloud_tools.run_cdp_command", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_chat_auto_tab_is_server_pinned_before_navigation(self, mock_auth, _mock_parse, mock_cdp, mock_nav):
        sid = "s-claude-abc12345-demo"
        resolved = "C" * 32
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        mock_cdp.return_value = {"targetInfo": {"targetId": resolved}}
        mock_nav.return_value = "Navigated"
        web._session_agent_map[sid] = "claude-abc12345"
        request = self._request({
            "action": "navigate",
            "session_id": sid,
            "tab_id": "auto",
            "url": "https://example.com",
        })

        response = await web.handle_cmd(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(web._session_tabs[sid], resolved)
        self.assertEqual(web._session_allowed_tabs[sid], {resolved})
        mock_cdp.assert_awaited_once_with(
            "claude-abc12345",
            "auto",
            "Target.getTargetInfo",
            {},
            "relay.local",
            8765,
            bring_to_front=False,
        )
        mock_nav.assert_awaited_once_with(
            "claude-abc12345",
            resolved,
            "https://example.com",
            "relay.local",
            8765,
            bring_to_front=False,
        )

    @patch("cloud_tools.navigate", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_profile_relaunch_window_never_falls_back_to_default(
        self,
        mock_auth,
        mock_nav,
    ):
        sid = "s-claude-abc12345-profile"
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        web._session_agent_map[sid] = "claude-abc12345"
        web._session_profile_paths[sid] = "/chrome/Profile 7"
        request = self._request({
            "action": "navigate",
            "session_id": sid,
            "tab_id": "auto",
            "url": "https://example.com",
        })

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 409)
        self.assertEqual(data["code"], "profile_session_restoring")
        mock_nav.assert_not_awaited()

    @patch("cloud_tools.navigate", new_callable=AsyncMock)
    @patch("web._authenticate")
    async def test_expired_profile_session_never_falls_back_to_default(
        self,
        mock_auth,
        mock_nav,
    ):
        sid = "s-claude-abc12345-expired"
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        web._expired_profile_sessions[sid] = 1.0
        request = self._request({
            "action": "navigate",
            "session_id": sid,
            "tab_id": "auto",
            "url": "https://example.com",
        })

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 409)
        self.assertEqual(data["code"], "profile_session_expired")
        mock_nav.assert_not_awaited()

    @patch("cloud_tools.run_cdp_command", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_new_tab_is_background_created_and_becomes_session_target(self, mock_auth, _mock_parse, mock_cdp):
        sid = "s-claude-abc12345-demo"
        original = "A" * 32
        created = "B" * 32
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        mock_cdp.return_value = {"targetId": created}
        web._session_agent_map[sid] = "claude-abc12345"
        web._session_tabs[sid] = original
        request = self._request({
            "action": "new_tab",
            "session_id": sid,
            "tab_id": "auto",
            "url": "https://example.com/new",
        })

        response = await web.handle_cmd(request)
        data = json.loads(response.body.decode())

        self.assertEqual(response.status, 200)
        self.assertEqual(data["tab_id"], created)
        self.assertEqual(web._session_tabs[sid], created)
        self.assertEqual(web._session_allowed_tabs[sid], {original, created})
        mock_cdp.assert_awaited_once_with(
            "claude-abc12345",
            original,
            "Target.createTarget",
            {"url": "https://example.com/new", "background": True},
            "relay.local",
            8765,
            bring_to_front=False,
        )

    @patch("web._authenticate")
    async def test_rejects_tab_not_authorized_for_chat_session(self, mock_auth):
        sid = "s-claude-abc12345-demo"
        web._session_tabs[sid] = "A" * 32
        web._session_agent_map[sid] = "claude-abc12345"
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        request = self._request({
            "action": "click",
            "session_id": sid,
            "tab_id": "B" * 12,
            "x": 1,
            "y": 2,
        })

        response = await web.handle_cmd(request)

        self.assertEqual(response.status, 403)
        self.assertIn("not authorized", json.loads(response.body.decode())["error"])

    @patch("cloud_tools.click", new_callable=AsyncMock)
    @patch("web._parse_relay", return_value=("relay.local", 8765))
    @patch("web._authenticate")
    async def test_explicit_authorized_tab_becomes_agent_view_target(self, mock_auth, _mock_parse, mock_click):
        sid = "s-claude-abc12345-demo"
        first = "A" * 32
        second = "B" * 32
        web._session_tabs[sid] = first
        web._session_allowed_tabs[sid] = {first, second}
        web._session_agent_map[sid] = "claude-abc12345"
        mock_auth.return_value = {
            "user_id": "u1",
            "agent_id": "claude-abc12345",
            "key_hash": "abc12345",
        }
        mock_click.return_value = "clicked"
        request = self._request({
            "action": "click",
            "session_id": sid,
            "tab_id": second[:12],
            "x": 3,
            "y": 4,
        })

        response = await web.handle_cmd(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(web._session_tabs[sid], second)
        mock_click.assert_awaited_once_with(
            "claude-abc12345", second, 3, 4, "relay.local", 8765
        )


if __name__ == "__main__":
    unittest.main()
