"""Tests for the first-look preview WebSocket FSM.

The handler wraps private-core's low-level screencast stream in a small,
explicit event protocol (``preview.attached``, ``preview.frame``,
``preview.reconnecting``, ``preview.ended``) and transparently reconnects the
underlying stream on idle-timeout / max-frames so a single client WS
represents one *logical* stream for the lifetime of a guest run. These tests
mock ``cloud_tools.stream_screencast`` so they exercise the FSM without a real
Chrome / relay / private-core.
"""

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
        self._original_max_reconnects = chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS
        self._original_backoff = chat_flow._FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S
        self.fake_core = _FakeCore()
        chat_flow._core = lambda: self.fake_core
        # Keep the reconnect backoff tiny so reconnect tests run fast.
        chat_flow._FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S = 0.0
        app = web.Application()
        app.router.add_get("/ws", chat_flow.handle_first_look_preview_ws)
        return app

    async def asyncTearDown(self):
        chat_flow._core = self._original_core
        cloud_tools.stream_screencast = self._original_stream
        chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = self._original_max_reconnects
        chat_flow._FIRST_LOOK_PREVIEW_RECONNECT_BACKOFF_S = self._original_backoff
        await super().asyncTearDown()

    # --- Happy path -------------------------------------------------------

    async def test_emits_attached_then_frame_then_done(self):
        """A clean screencast run: attached → frame → done (not retriable)."""

        async def fake_stream(*_args, **_kwargs):
            yield {"type": "frame", "mime": "image/jpeg", "data": "frame-1", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect(
            "/ws?session_id=s-guest-aaaa1111-demo&width=800&height=600",
        )
        attached = await ws.receive_json()
        frame = await ws.receive_json()
        ended = await ws.receive_json()
        closed = await ws.receive()

        self.assertEqual(attached["v"], 1)
        self.assertEqual(attached["type"], "preview.attached")
        self.assertEqual(attached["tab_id"], "tab-preview")
        self.assertEqual(attached["width"], 800)
        self.assertEqual(attached["height"], 600)

        self.assertEqual(frame["type"], "preview.frame")
        self.assertEqual(frame["data"], "frame-1")
        self.assertEqual(frame["mime"], "image/jpeg")
        self.assertEqual(frame["seq"], 1)

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "done")
        self.assertFalse(ended["retriable"])
        self.assertEqual(ended["frame_count"], 1)

        self.assertIn(closed.type, {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})

    async def test_frame_seq_increments_per_frame(self):
        async def fake_stream(*_args, **_kwargs):
            yield {"type": "frame", "mime": "image/jpeg", "data": "f1", "metadata": {}}
            yield {"type": "frame", "mime": "image/jpeg", "data": "f2", "metadata": {}}
            yield {"type": "frame", "mime": "image/jpeg", "data": "f3", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        await ws.receive_json()  # preview.attached
        seqs = []
        for _ in range(3):
            evt = await ws.receive_json()
            self.assertEqual(evt["type"], "preview.frame")
            seqs.append(evt["seq"])
        self.assertEqual(seqs, [1, 2, 3])

    # --- Transparent reconnect -------------------------------------------

    async def test_stream_timeout_triggers_transparent_reconnect(self):
        """Engine status=stream_timeout → preview.reconnecting, NOT preview.ended.

        This is the bug fix: the old handler leaked status events to the
        client, which then refused to reconnect once the first frame had been
        painted, freezing the preview. The new FSM rebuilds the underlying
        stream server-side without the client noticing.
        """
        call_count = {"n": 0}

        async def fake_stream(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                yield {"type": "frame", "mime": "image/jpeg", "data": "f1", "metadata": {}}
                yield {"type": "status", "status": "limit_reached", "reason": "stream_timeout"}
            else:
                yield {"type": "frame", "mime": "image/jpeg", "data": "f2", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        attached = await ws.receive_json()
        first = await ws.receive_json()
        reconnecting = await ws.receive_json()
        second = await ws.receive_json()
        ended = await ws.receive_json()

        self.assertEqual(attached["type"], "preview.attached")
        self.assertEqual(first["type"], "preview.frame")
        self.assertEqual(first["data"], "f1")
        self.assertEqual(first["seq"], 1)

        self.assertEqual(reconnecting["type"], "preview.reconnecting")
        self.assertEqual(reconnecting["attempt"], 1)

        self.assertEqual(second["type"], "preview.frame")
        self.assertEqual(second["data"], "f2")
        # Frame seq continues monotonically across reconnects in one WS.
        self.assertEqual(second["seq"], 2)

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "done")
        self.assertEqual(ended["frame_count"], 2)

        self.assertEqual(call_count["n"], 2, "underlying stream should have been rebuilt once")

    async def test_max_frames_also_triggers_transparent_reconnect(self):
        call_count = {"n": 0}

        async def fake_stream(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                yield {"type": "status", "status": "limit_reached", "reason": "max_frames"}
            else:
                yield {"type": "frame", "mime": "image/jpeg", "data": "after", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        await ws.receive_json()  # attached
        reconnecting = await ws.receive_json()
        frame = await ws.receive_json()
        ended = await ws.receive_json()

        self.assertEqual(reconnecting["type"], "preview.reconnecting")
        self.assertEqual(frame["type"], "preview.frame")
        self.assertEqual(frame["data"], "after")
        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "done")

    async def test_reconnect_cap_emits_max_reconnects(self):
        """After N consecutive stream_timeouts, the server gives up."""
        chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = 2

        async def fake_stream(*_args, **_kwargs):
            yield {"type": "status", "status": "limit_reached", "reason": "stream_timeout"}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        await ws.receive_json()  # attached

        # Two reconnecting events (attempts 1 and 2), then ended(max_reconnects).
        r1 = await ws.receive_json()
        r2 = await ws.receive_json()
        ended = await ws.receive_json()

        self.assertEqual(r1["type"], "preview.reconnecting")
        self.assertEqual(r1["attempt"], 1)
        self.assertEqual(r2["type"], "preview.reconnecting")
        self.assertEqual(r2["attempt"], 2)

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "max_reconnects")
        self.assertFalse(ended["retriable"])

    # --- Terminal error translation --------------------------------------

    async def test_slow_client_is_retriable_terminal(self):
        async def fake_stream(*_args, **_kwargs):
            yield {"type": "frame", "mime": "image/jpeg", "data": "f", "metadata": {}}
            yield {"type": "status", "status": "limit_reached", "reason": "slow_client"}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        await ws.receive_json()  # attached
        await ws.receive_json()  # frame
        ended = await ws.receive_json()

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "slow_client")
        self.assertTrue(ended["retriable"])
        self.assertEqual(ended["frame_count"], 1)

    async def test_underlying_exception_becomes_fatal_retriable(self):
        async def fake_stream(*_args, **_kwargs):
            yield {"type": "frame", "mime": "image/jpeg", "data": "f", "metadata": {}}
            raise RuntimeError("private-core exploded")

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect("/ws?session_id=s-guest-aaaa1111-demo")
        await ws.receive_json()  # attached
        await ws.receive_json()  # frame
        ended = await ws.receive_json()

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "fatal")
        self.assertTrue(ended["retriable"])

    # --- Auth / ownership unchanged --------------------------------------

    async def test_rejects_foreign_guest_session(self):
        resp = await self.client.request("GET", "/ws?session_id=s-guest-bbbb2222-demo")
        self.assertEqual(resp.status, 403)
        self.assertIn("session_id not owned by guest", await resp.text())

    async def test_owned_but_unprovisioned_session_returns_404(self):
        import web_app.handlers.chat_flow as cf
        saved = cf._FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT
        cf._FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT = 0.1
        try:
            resp = await self.client.request("GET", "/ws?session_id=s-guest-aaaa1111-unknown")
            self.assertEqual(resp.status, 404)
            self.assertIn("No live preview available", await resp.text())
        finally:
            cf._FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT = saved

    async def test_explicit_tab_id_override_accepted(self):
        async def fake_stream(agent_id, tab_id, **_kwargs):
            # Assert the override propagates to private-core.
            self.assertEqual(tab_id, "DEADBEEF" * 4)
            yield {"type": "frame", "mime": "image/jpeg", "data": "f", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        override = "D" * 32  # valid hex
        ws = await self.client.ws_connect(
            f"/ws?session_id=s-guest-aaaa1111-demo&tab_id={override}",
        )
        attached = await ws.receive_json()
        self.assertEqual(attached["tab_id"], override)

    async def test_invalid_tab_id_rejected_with_400(self):
        resp = await self.client.request(
            "GET",
            "/ws?session_id=s-guest-aaaa1111-demo&tab_id=not-hex!!",
        )
        self.assertEqual(resp.status, 400)
        self.assertIn("invalid tab_id", await resp.text())


if __name__ == "__main__":
    import unittest
    unittest.main()
