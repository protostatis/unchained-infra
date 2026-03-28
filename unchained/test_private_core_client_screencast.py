from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp

from private_core_client import PrivateCoreClient, PrivateCoreError


async def _collect(gen):
    items = []
    async for item in gen:
        items.append(item)
    return items


class _FakeWebSocket:
    def __init__(self):
        self._done = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data='{"type":"status","status":"done"}')


class _FakeClientSession:
    def __init__(self, capture: dict, *, timeout=None, headers=None):
        capture["timeout"] = timeout
        capture["headers"] = headers
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        return False

    def ws_connect(self, url, *, params, heartbeat, autoping):
        self._capture["url"] = url
        self._capture["params"] = params
        self._capture["heartbeat"] = heartbeat
        self._capture["autoping"] = autoping
        return _FakeWebSocket()


class TestPrivateCoreClientScreencast(unittest.IsolatedAsyncioTestCase):

    async def test_http_mode_rejects_unsupported_image_format(self):
        client = PrivateCoreClient(base_url="http://private-core", mode="http", timeout_seconds=5.0)
        with patch("private_core_client.aiohttp.ClientSession") as session_ctor:
            with self.assertRaisesRegex(PrivateCoreError, "Unsupported screencast image format"):
                await _collect(client.stream_screencast("agent-1", "tab-1", image_format="gif"))
            session_ctor.assert_not_called()

    async def test_http_mode_sets_finite_websocket_timeouts(self):
        client = PrivateCoreClient(base_url="http://private-core", mode="http", timeout_seconds=5.0)
        capture: dict[str, object] = {}

        def _make_session(*args, **kwargs):
            del args
            return _FakeClientSession(capture, **kwargs)

        with patch("private_core_client.aiohttp.ClientSession", side_effect=_make_session):
            events = await _collect(
                client.stream_screencast(
                    "agent-1",
                    "tab-1",
                    image_format="png",
                    stream_timeout=12.5,
                )
            )

        self.assertEqual(events, [{"type": "status", "status": "done"}])
        timeout = capture["timeout"]
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertEqual(capture["params"]["format"], "png")
        self.assertEqual(timeout.sock_connect, 5.0)
        self.assertIsNotNone(timeout.sock_read)
        self.assertIsNotNone(timeout.total)
        self.assertGreaterEqual(timeout.sock_read, 27.5)
        self.assertGreater(timeout.total, timeout.sock_read)


if __name__ == "__main__":
    unittest.main()
