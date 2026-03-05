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


if __name__ == "__main__":
    unittest.main()
