"""Tests for relay auth gates on HTTP and CDP paths."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

from websockets.datastructures import Headers

from relay import Relay


def _make_request(path: str, headers: Headers | None = None):
    return SimpleNamespace(path=path, headers=headers or Headers())


def _make_ws(headers: Headers | None = None):
    return SimpleNamespace(request=SimpleNamespace(headers=headers or Headers()))


class TestRelayAuth(unittest.TestCase):
    def test_api_agents_requires_auth(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as db:
            relay = Relay(db_path=db.name)
            resp = asyncio.run(relay._process_request(None, _make_request("/api/agents")))
            self.assertIsNotNone(resp)
            self.assertEqual(resp.status_code, 401)
            self.assertEqual(json.loads(resp.body.decode())["error"], "Missing Authorization header")

    def test_api_agents_allows_shared_token(self):
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "relay-test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                headers = Headers([("Authorization", "Bearer relay-test-token")])
                resp = asyncio.run(relay._process_request(None, _make_request("/api/agents", headers)))
                self.assertIsNotNone(resp)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(json.loads(resp.body.decode()), [])
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token

    def test_cdp_client_rejects_missing_or_invalid_token(self):
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "relay-test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                ws = _make_ws()
                self.assertEqual(
                    relay._authorize_cdp_client(ws, "claude-deadbeef", {}),
                    "Missing Authorization header",
                )
                self.assertEqual(
                    relay._authorize_cdp_client(ws, "claude-deadbeef", {"relay_token": ["wrong"]}),
                    "Invalid relay token",
                )
                self.assertIsNone(
                    relay._authorize_cdp_client(ws, "claude-deadbeef", {"relay_token": ["relay-test-token"]})
                )
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token

    def test_http_rate_limit_returns_429(self):
        old_limit = os.environ.get("RELAY_HTTP_RATE_LIMIT")
        old_window = os.environ.get("RELAY_RATE_WINDOW_S")
        os.environ["RELAY_HTTP_RATE_LIMIT"] = "1"
        os.environ["RELAY_RATE_WINDOW_S"] = "60"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                api_key = relay.auth.create_key("u-test")
                headers = Headers([("Authorization", f"Bearer {api_key}")])
                first = asyncio.run(relay._process_request(None, _make_request("/api/agents", headers)))
                second = asyncio.run(relay._process_request(None, _make_request("/api/agents", headers)))
                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.status_code, 429)
        finally:
            if old_limit is None:
                os.environ.pop("RELAY_HTTP_RATE_LIMIT", None)
            else:
                os.environ["RELAY_HTTP_RATE_LIMIT"] = old_limit
            if old_window is None:
                os.environ.pop("RELAY_RATE_WINDOW_S", None)
            else:
                os.environ["RELAY_RATE_WINDOW_S"] = old_window


if __name__ == "__main__":
    unittest.main()
