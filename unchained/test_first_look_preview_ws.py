from __future__ import annotations

from aiohttp import WSMsgType
from aiohttp.test_utils import AioHTTPTestCase
from aiohttp import web

import cloud_tools
from web_app.handlers import chat_flow


class _FakeCore:
    HEADLESS_AGENT_ID = "headless-123"

    def __init__(self):
        self._session_tabs = {"s-guest-aaaa1111-demo": "tab-preview"}

    def _first_look_guest_auth(self, _request):
        return {"agent_id": "guest-aaaa1111"}, "guest-cookie", 0

    def _attach_first_look_guest_cookies(self, _resp, _request, _guest_id, *, quota_count=None):
        del quota_count

    def _parse_relay(self):
        return "relay.internal", 8765


class TestFirstLookPreviewWebSocket(AioHTTPTestCase):

    async def get_application(self):
        self._original_core = chat_flow._core
        self._original_stream = cloud_tools.stream_screencast
        self.fake_core = _FakeCore()
        chat_flow._core = lambda: self.fake_core
        app = web.Application()
        app.router.add_get("/ws", chat_flow.handle_first_look_preview_ws)
        return app

    async def asyncTearDown(self):
        chat_flow._core = self._original_core
        cloud_tools.stream_screencast = self._original_stream
        await super().asyncTearDown()

    async def test_forwards_frame_and_status_messages(self):
        async def fake_stream(*_args, **_kwargs):
            yield {"type": "frame", "mime": "image/jpeg", "data": "frame-1", "metadata": {"index": 1}}
            yield {"type": "status", "status": "limit_reached", "reason": "max_frames"}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo&width=800&height=600")
        first = await ws.receive_json()
        second = await ws.receive_json()
        closed = await ws.receive()

        self.assertEqual(first["type"], "frame")
        self.assertEqual(first["data"], "frame-1")
        self.assertEqual(second["type"], "status")
        self.assertEqual(second["reason"], "max_frames")
        self.assertIn(closed.type, {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})

    async def test_rejects_foreign_guest_session(self):
        resp = await self.client.request("GET", "/ws?session_id=s-guest-bbbb2222-demo")

        self.assertEqual(resp.status, 403)
        self.assertIn("session_id not owned by guest", await resp.text())


if __name__ == "__main__":
    import unittest
    unittest.main()
