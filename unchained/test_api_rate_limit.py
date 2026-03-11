"""Tests for API rate limiting."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from api import API


class _FakeAuth:
    def validate_key(self, token: str):
        if token == "uc_live_test":
            return {"user_id": "u-test", "key": token}
        return None


class _FakeRelay:
    auth = _FakeAuth()

    @staticmethod
    def agent_belongs_to_user(agent_id: str, user_id: str) -> bool:
        return agent_id == "claude-abc" and user_id == "u-test"

    @staticmethod
    async def http_proxy(agent_id: str, method: str, path: str):
        del agent_id, method, path
        return {"status": 200, "body": {}}

    @staticmethod
    def get_agents_for_user(user_id: str):
        return [{"agent_id": "claude-abc"}] if user_id == "u-test" else []


class TestApiRateLimit(unittest.TestCase):
    def setUp(self):
        self._old_window = os.environ.get("UNCHAINED_API_RATE_WINDOW_S")
        self._old_js = os.environ.get("UNCHAINED_API_JS_RATE_LIMIT")
        self._old_public_base = os.environ.get("UNCHAINED_PUBLIC_BASE_URL")
        self._old_api_url = os.environ.get("UNCHAINED_API_URL")
        os.environ["UNCHAINED_API_RATE_WINDOW_S"] = "60"
        os.environ["UNCHAINED_API_JS_RATE_LIMIT"] = "1"
        os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
        os.environ.pop("UNCHAINED_API_URL", None)
        self.api = API(_FakeRelay())

    def tearDown(self):
        if self._old_window is None:
            os.environ.pop("UNCHAINED_API_RATE_WINDOW_S", None)
        else:
            os.environ["UNCHAINED_API_RATE_WINDOW_S"] = self._old_window
        if self._old_js is None:
            os.environ.pop("UNCHAINED_API_JS_RATE_LIMIT", None)
        else:
            os.environ["UNCHAINED_API_JS_RATE_LIMIT"] = self._old_js
        if self._old_public_base is None:
            os.environ.pop("UNCHAINED_PUBLIC_BASE_URL", None)
        else:
            os.environ["UNCHAINED_PUBLIC_BASE_URL"] = self._old_public_base
        if self._old_api_url is None:
            os.environ.pop("UNCHAINED_API_URL", None)
        else:
            os.environ["UNCHAINED_API_URL"] = self._old_api_url

    def test_js_endpoint_rate_limits_second_request(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer uc_live_test"},
            match_info={"agent_id": "claude-abc"},
            can_read_body=True,
            json=AsyncMock(return_value={"tab_id": "auto", "expression": "document.title"}),
        )
        with patch("api.cloud_tools.run_js", new=AsyncMock(return_value="title")):
            first = asyncio.run(self.api.handle_js(request))
            second = asyncio.run(self.api.handle_js(request))

        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 429)

    def test_create_tab_defaults_to_branded_local_tab_page(self):
        request = SimpleNamespace(
            headers={"Authorization": "Bearer uc_live_test"},
            match_info={"agent_id": "claude-abc"},
            can_read_body=True,
            host="127.0.0.1:8765",
            json=AsyncMock(return_value={}),
        )
        self.api.relay.http_proxy = AsyncMock(return_value={"status": 200, "body": {}})

        response = asyncio.run(self.api.handle_create_tab(request))

        self.assertEqual(response.status, 200)
        self.api.relay.http_proxy.assert_awaited_once_with(
            "claude-abc",
            "PUT",
            "/json/new?http://127.0.0.1:8080/tab",
        )


if __name__ == "__main__":
    unittest.main()
