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

import asyncio
import unittest

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


class _AuthenticatedFakeCore(_FakeCore):
    def __init__(self):
        super().__init__()
        self._session_tabs = {"s-claude-abc12345-demo": "TAB" * 10 + "AA"}
        self._session_allowed_tabs = {"s-claude-abc12345-demo": {"TAB" * 10 + "AA"}}
        self._session_agent_map = {"s-claude-abc12345-demo": "claude-abc12345"}
        self.authenticated = True

    def _authenticate(self, _request):
        if not self.authenticated:
            return None
        return {"agent_id": "claude-abc12345", "key_hash": "abc12345", "user_id": "user-1"}

    async def _resolve_bridge_agent(self, auth_info, _requested_profile):
        return {"bridge_agent_id": auth_info["agent_id"]}


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

    async def test_iterator_exhaust_treated_as_transparent_reconnect(self):
        """A clean iterator EOF is reconnect-worthy, not semantic completion."""

        chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = 1
        call_count = {"n": 0}

        async def fake_stream(*_args, **_kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                yield {"type": "frame", "mime": "image/jpeg", "data": "frame-1", "metadata": {}}
                return
            yield {"type": "frame", "mime": "image/jpeg", "data": "frame-2", "metadata": {}}

        cloud_tools.stream_screencast = fake_stream

        ws = await self.client.ws_connect(
            "/ws?session_id=s-guest-aaaa1111-demo&width=800&height=600",
        )
        attached = await ws.receive_json()
        first = await ws.receive_json()
        reconnecting = await ws.receive_json()
        second = await ws.receive_json()
        ended = await ws.receive_json()
        closed = await ws.receive()

        self.assertEqual(attached["v"], 1)
        self.assertEqual(attached["type"], "preview.attached")
        self.assertEqual(attached["tab_id"], "tab-preview")
        self.assertEqual(attached["width"], 800)
        self.assertEqual(attached["height"], 600)

        self.assertEqual(first["type"], "preview.frame")
        self.assertEqual(first["data"], "frame-1")
        self.assertEqual(first["mime"], "image/jpeg")
        self.assertEqual(first["seq"], 1)

        self.assertEqual(reconnecting["type"], "preview.reconnecting")
        self.assertEqual(reconnecting["attempt"], 1)

        self.assertEqual(second["type"], "preview.frame")
        self.assertEqual(second["data"], "frame-2")
        self.assertEqual(second["seq"], 2)

        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "max_reconnects")
        self.assertFalse(ended["retriable"])
        self.assertEqual(ended["frame_count"], 2)
        self.assertEqual(call_count["n"], 2)

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
        chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = 1
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
        self.assertEqual(ended["reason"], "max_reconnects")
        self.assertEqual(ended["frame_count"], 2)

        self.assertEqual(call_count["n"], 2, "underlying stream should have been rebuilt once")

    async def test_max_frames_also_triggers_transparent_reconnect(self):
        chat_flow._FIRST_LOOK_PREVIEW_MAX_TRANSPARENT_RECONNECTS = 1
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
        self.assertEqual(ended["reason"], "max_reconnects")

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


class TestAuthenticatedChatPreviewWebSocket(AioHTTPTestCase):

    async def get_application(self):
        self._original_core = chat_flow._core
        self._original_stream = cloud_tools.stream_screencast
        self._original_cdp = cloud_tools.run_cdp_command
        self.fake_core = _AuthenticatedFakeCore()
        chat_flow._core = lambda: self.fake_core

        async def fake_cdp(_agent_id, tab_id, method, *_args, **_kwargs):
            self.assertEqual(method, "Target.getTargetInfo")
            return {"targetInfo": {"targetId": tab_id}}

        cloud_tools.run_cdp_command = fake_cdp
        app = web.Application()
        app.router.add_get("/ws", chat_flow.handle_chat_preview_ws)
        return app

    async def asyncTearDown(self):
        chat_flow._core = self._original_core
        cloud_tools.stream_screencast = self._original_stream
        cloud_tools.run_cdp_command = self._original_cdp
        await super().asyncTearDown()

    async def test_streams_the_exact_tab_bound_to_the_authenticated_chat_session(self):
        captured = {}

        async def fake_stream(agent_id, tab_id, **_kwargs):
            captured.update(agent_id=agent_id, tab_id=tab_id)
            yield {"type": "frame", "mime": "image/jpeg", "data": "same-browser-frame"}

        cloud_tools.stream_screencast = fake_stream
        ws = await self.client.ws_connect("/ws?session_id=s-claude-abc12345-demo&width=900&height=600&transport=frames")
        attached = await ws.receive_json()
        frame = await ws.receive_json()
        await ws.close()

        self.assertEqual(attached["type"], "preview.attached")
        self.assertEqual(attached["tab_id"], "TAB" * 10 + "AA")
        self.assertEqual(frame["type"], "preview.frame")
        self.assertEqual(frame["data"], "same-browser-frame")
        self.assertEqual(captured, {"agent_id": "claude-abc12345", "tab_id": "TAB" * 10 + "AA"})

    async def test_rejects_a_foreign_authenticated_chat_session(self):
        resp = await self.client.request("GET", "/ws?session_id=s-claude-deadbeef-demo")
        self.assertEqual(resp.status, 403)
        self.assertIn("not owned", await resp.text())

    async def test_rejects_an_unauthenticated_preview(self):
        self.fake_core.authenticated = False
        resp = await self.client.request("GET", "/ws?session_id=s-claude-abc12345-demo")
        self.assertEqual(resp.status, 401)

    async def test_rejects_a_foreign_websocket_origin(self):
        resp = await self.client.request(
            "GET",
            "/ws?session_id=s-claude-abc12345-demo",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(resp.status, 403)
        self.assertIn("foreign websocket origin", await resp.text())

    async def test_streams_semantic_snapshot_and_rebinds_when_session_tab_changes(self):
        from web_app import semantic_mirror

        original_stream = semantic_mirror.stream_semantic_mirror

        async def fake_semantic_stream(agent_id, tab_id, **kwargs):
            self.assertEqual(agent_id, "claude-abc12345")
            self.assertEqual(tab_id, "TAB" * 10 + "AA")
            yield {
                "type": "snapshot",
                "snapshot": {
                    "captureEpoch": "epoch-test-123",
                    "url": "https://example.test",
                    "body": "<main data-ucm-id=\"ucm-1\">Observed</main>",
                    "fidelity": {"truncated": False},
                },
                "resync": False,
            }
            self.fake_core._session_tabs["s-claude-abc12345-demo"] = "NEW" * 10 + "BB"
            self.assertTrue(kwargs["stop_requested"]())

        semantic_mirror.stream_semantic_mirror = fake_semantic_stream
        try:
            ws = await self.client.ws_connect("/ws?session_id=s-claude-abc12345-demo")
            attached = await ws.receive_json()
            snapshot = await ws.receive_json()
            ended = await ws.receive_json()
            await ws.close()
        finally:
            semantic_mirror.stream_semantic_mirror = original_stream

        self.assertEqual(attached["mode"], "semantic")
        self.assertEqual(snapshot["type"], "preview.semantic.snapshot")
        self.assertEqual(snapshot["snapshot"]["url"], "https://example.test")
        self.assertEqual(ended["type"], "preview.ended")
        self.assertEqual(ended["reason"], "tab_changed")
        self.assertTrue(ended["retriable"])
        self.assertEqual(ended["from_tab_id"], "TAB" * 10 + "AA")
        self.assertEqual(ended["to_tab_id"], "NEW" * 10 + "BB")

    async def test_concurrent_connections_coexist_without_generation_war(self):
        """Two Agent View clients on the same session must coexist.

        Previously, _chat_preview_generations killed the older connection when
        a new one arrived, creating a 1.6-second reconnection war that caused
        scroll actions to be rejected (preview-superseded) and the page to
        reset to top. Each connection now gets its own mirror_key so mirrors
        coexist without interference.
        """
        from web_app import semantic_mirror

        original_stream = semantic_mirror.stream_semantic_mirror
        release = asyncio.Event()
        seen_keys = []

        async def fake_semantic_stream(agent_id, tab_id, **kwargs):
            seen_keys.append(kwargs.get("mirror_key", ""))
            yield {
                "type": "snapshot",
                "snapshot": {
                    "captureEpoch": "epoch-test",
                    "url": "https://example.test",
                    "body": "<main data-ucm-id=\"ucm-1\">Hi</main>",
                    "fidelity": {"truncated": False},
                },
                "resync": False,
            }
            await release.wait()

        semantic_mirror.stream_semantic_mirror = fake_semantic_stream
        try:
            ws1 = await self.client.ws_connect("/ws?session_id=s-claude-abc12345-demo")
            attached1 = await ws1.receive_json()
            snapshot1 = await ws1.receive_json()

            ws2 = await self.client.ws_connect("/ws?session_id=s-claude-abc12345-demo")
            attached2 = await ws2.receive_json()
            snapshot2 = await ws2.receive_json()

            # Both connections should be alive — the second must NOT kill the first.
            self.assertFalse(ws1.closed, "first connection was killed by the second")
            self.assertFalse(ws2.closed, "second connection did not survive")

            # Both received snapshots.
            self.assertEqual(snapshot1["type"], "preview.semantic.snapshot")
            self.assertEqual(snapshot2["type"], "preview.semantic.snapshot")

            # Each connection got its own mirror key.
            self.assertEqual(len(seen_keys), 2)
            self.assertNotEqual(seen_keys[0], seen_keys[1])
            self.assertTrue(
                all(k.startswith("unchained.mirror.capture.v1.") for k in seen_keys),
                f"mirror keys should be per-connection: {seen_keys}",
            )

            release.set()
            await ws1.close()
            await ws2.close()
        finally:
            release.set()
            semantic_mirror.stream_semantic_mirror = original_stream

    async def test_stale_default_tab_is_replaced_by_server_resolved_auto_target(self):
        stale = self.fake_core._session_tabs["s-claude-abc12345-demo"]
        replacement = "C" * 32

        async def fake_cdp(_agent_id, tab_id, method, *_args, **_kwargs):
            self.assertEqual(method, "Target.getTargetInfo")
            if tab_id == stale:
                raise RuntimeError("target not found")
            self.assertEqual(tab_id, "auto")
            return {"targetInfo": {"targetId": replacement}}

        async def fake_stream(_agent_id, tab_id, **_kwargs):
            self.assertEqual(tab_id, replacement)
            yield {"type": "frame", "mime": "image/jpeg", "data": "replacement-frame"}

        cloud_tools.run_cdp_command = fake_cdp
        cloud_tools.stream_screencast = fake_stream
        ws = await self.client.ws_connect(
            "/ws?session_id=s-claude-abc12345-demo&transport=frames"
        )
        attached = await ws.receive_json()
        frame = await ws.receive_json()
        await ws.close()

        self.assertEqual(attached["tab_id"], replacement)
        self.assertEqual(frame["data"], "replacement-frame")
        self.assertEqual(self.fake_core._session_tabs["s-claude-abc12345-demo"], replacement)
        self.assertEqual(
            self.fake_core._session_allowed_tabs["s-claude-abc12345-demo"],
            {replacement},
        )

    async def test_interactive_action_uses_server_confirmation_on_the_exact_tab(self):
        from web_app import semantic_mirror

        original_stream = semantic_mirror.stream_semantic_mirror
        original_execute = semantic_mirror.execute_semantic_action
        release_stream = asyncio.Event()
        calls = []

        async def fake_semantic_stream(agent_id, tab_id, **kwargs):
            self.assertEqual(agent_id, "claude-abc12345")
            self.assertEqual(tab_id, "TAB" * 10 + "AA")
            self.assertIn("operation_lock", kwargs)
            yield {
                "type": "snapshot",
                "snapshot": {
                    "captureEpoch": "epoch-test-123",
                    "url": "https://example.test",
                    "body": '<button data-ucm-id="ucm-1">Continue</button>',
                    "fidelity": {"truncated": False},
                },
                "resync": False,
            }
            await release_stream.wait()

        async def fake_execute(agent_id, tab_id, action, **kwargs):
            calls.append((agent_id, tab_id, action, kwargs))
            if not kwargs.get("confirmed"):
                return {"ok": False, "reason": "confirmation-required"}
            return {"ok": True, "reason": "ok", "navigated": False}

        semantic_mirror.stream_semantic_mirror = fake_semantic_stream
        semantic_mirror.execute_semantic_action = fake_execute
        try:
            ws = await self.client.ws_connect("/ws?session_id=s-claude-abc12345-demo")
            attached = await ws.receive_json()
            snapshot = await ws.receive_json()
            self.assertEqual(attached["interaction"], "interactive")
            self.assertRegex(snapshot["mirror_id"], r"^[a-f0-9]{32}$")

            await ws.send_json(
                {
                    "type": "preview.action",
                    "action_id": "action_1234",
                    "mirror_id": snapshot["mirror_id"],
                    "capture_epoch": snapshot["capture_epoch"],
                    "document_seq": snapshot["document_seq"],
                    "action": {
                        "targetId": "ucm-1",
                        "kind": "click",
                        "label": "Continue",
                        # A client cannot self-authorize a confirmation.
                        "confirmed": True,
                    },
                }
            )
            challenge = await ws.receive_json()
            self.assertEqual(
                challenge["type"], "preview.action.confirmation_required"
            )
            self.assertEqual(challenge["action_id"], "action_1234")
            self.assertEqual(challenge["label"], "Continue")
            self.assertFalse(calls[0][3].get("confirmed", False))
            self.assertNotIn("confirmed", calls[0][2])

            await ws.send_json(
                {
                    "type": "preview.action.confirm",
                    "action_id": "action_1234",
                    "confirmation_token": challenge["confirmation_token"],
                }
            )
            result = await ws.receive_json()
            self.assertEqual(result["type"], "preview.action.result")
            self.assertTrue(result["ok"])
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[1][3]["confirmed"])
            self.assertEqual(calls[1][0:2], ("claude-abc12345", "TAB" * 10 + "AA"))
            self.assertIs(calls[0][3]["operation_lock"], calls[1][3]["operation_lock"])
            release_stream.set()
            await ws.close()
        finally:
            release_stream.set()
            semantic_mirror.stream_semantic_mirror = original_stream
            semantic_mirror.execute_semantic_action = original_execute


class TestChatPreviewActionValidation(unittest.TestCase):
    def test_bounds_action_and_drops_client_confirmation_flag(self):
        action_id, mirror_id, capture_epoch, document_seq, action, label = (
            chat_flow._parse_chat_preview_action(
                {
                    "type": "preview.action",
                    "action_id": "action_5678",
                    "mirror_id": "a" * 32,
                    "capture_epoch": "epoch-test-123",
                    "document_seq": 4,
                    "action": {
                        "targetId": "ucm-z",
                        "kind": "input",
                        "value": "hello",
                        "label": "Search",
                        "confirmed": True,
                    },
                }
            )
        )
        self.assertEqual(
            (action_id, mirror_id, capture_epoch, document_seq),
            ("action_5678", "a" * 32, "epoch-test-123", 4),
        )
        self.assertEqual(action, {"targetId": "ucm-z", "kind": "input", "value": "hello"})
        self.assertEqual(label, "Search")

    def test_rejects_unbounded_or_non_finite_action_data(self):
        base = {
            "type": "preview.action",
            "action_id": "action_9012",
            "mirror_id": "b" * 32,
            "capture_epoch": "epoch-test-123",
            "document_seq": 0,
            "action": {"targetId": "ucm-1", "kind": "scroll", "x": float("inf")},
        }
        with self.assertRaisesRegex(ValueError, "coordinate"):
            chat_flow._parse_chat_preview_action(base)
        base["action"]["x"] = 10 ** 10_000
        with self.assertRaisesRegex(ValueError, "coordinate"):
            chat_flow._parse_chat_preview_action(base)

    def test_accepts_document_scroll_and_clamps_target_relative_clicks(self):
        parsed = chat_flow._parse_chat_preview_action(
            {
                "type": "preview.action",
                "action_id": "action_scroll1",
                "mirror_id": "c" * 32,
                "capture_epoch": "epoch-test-123",
                "document_seq": 9,
                "action": {"targetId": "document", "kind": "scroll", "x": 12, "y": 34},
            }
        )
        self.assertEqual(parsed[4], {"targetId": "document", "kind": "scroll", "x": 12.0, "y": 34.0})

        click = chat_flow._parse_chat_preview_action(
            {
                "type": "preview.action",
                "action_id": "action_click1",
                "mirror_id": "d" * 32,
                "capture_epoch": "epoch-test-123",
                "document_seq": 9,
                "action": {"targetId": "ucm-1", "kind": "click", "fx": -2, "fy": 4},
            }
        )
        self.assertEqual(click[4]["fx"], 0.0)
        self.assertEqual(click[4]["fy"], 1.0)


class TestInteractiveAgentViewTemplate(unittest.TestCase):
    def test_generated_chat_has_fullscreen_interactive_agent_view(self):
        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        self.assertIn('id="topbar-agent-view"', html)
        self.assertNotIn('id="banner-agent-view"', html)
        self.assertIn("body.agent-view-open #app-shell #main{position:fixed", html)
        self.assertIn("pointer-events:auto", html)
        self.assertIn("preview.action.confirmation_required", html)
        self.assertIn("function bindAgentViewInteractions", html)
        self.assertIn("Interactive semantic DOM", html)
        self.assertIn('id="agent-view-frame-next"', html)
        self.assertIn("agentViewSnapshotLoading", html)
        self.assertIn("Refreshing same browser tab", html)
        self.assertIn("function agentViewProtectVisualPlaceholders", html)
        self.assertIn("sid !== agentViewBoundSessionId", html)
        self.assertIn("agentViewExpectedScrolls", html)
        self.assertIn("agentViewLocalScrolls", html)
        self.assertIn("agentViewShouldApplySourceScroll", html)
        self.assertIn("snapshot.scrollPositions", html)
        self.assertIn("capture_epoch: agentViewCaptureEpoch", html)
        self.assertIn("fx: Math.max(0, Math.min(1, fx))", html)
        self.assertIn("agent-view-chat-toggle", html)
        self.assertIn("agent-view-chat-open", html)
        self.assertIn("data-ucm-image-error", html)
        self.assertIn("function scheduleAgentViewSemanticRecovery", html)
        self.assertIn("Retrying interactive semantic view", html)
        self.assertIn("critical styles bounded", html)
        self.assertIn("agent-view-browser-positioned", html)
        self.assertIn("--agent-view-mobile-chat-top", html)
        self.assertIn("function positionAgentViewMobileChat", html)
        self.assertIn("frame.style.transformOrigin = mobile ? 'top center' : 'center center'", html)
        self.assertIn("image.addEventListener('load', function() { scheduleAgentViewSemanticFrameScale(false); })", html)
        self.assertIn("patch-targets-omitted", html)
        self.assertNotIn("if (!target) throw new Error('semantic target missing')", html)


class TestFirstLookPreviewClientJsShape(unittest.TestCase):
    """Static string checks on the FIRST_LOOK_PREVIEW_HTML template.

    These are not unit tests of running code — they're grep-style regression
    guards for specific JS invariants that are too awkward to test in a real
    headless browser and that have bitten the preview lifecycle before.
    """

    def _preview_html(self) -> str:
        import web_app.templates as templates
        return templates.FIRST_LOOK_PREVIEW_HTML

    def test_onclose_has_stale_guard_as_first_line(self):
        """Every callback that mutates preview state must early-return on a
        stale WS (one that has been replaced by openPreviewSocket while the
        event was in flight). onopen and onmessage already have this guard;
        onclose needs it too. Without it, a stale close from followTab's
        close+reopen dance triggers a transport-level retry which opens
        *another* WS, and every replacement cascades into more opens.

        The original FSM refactor shipped without this guard on onclose,
        causing 10+ WebSocket reopens per guest run in prod. This test
        catches that exact regression.
        """
        html = self._preview_html()
        # The guard has to appear BEFORE the state mutation and BEFORE the
        # transport-retry branch, so match it relative to the onclose arrow.
        marker = "ws.onclose = () => {"
        self.assertIn(marker, html, "onclose handler missing from template")
        after_onclose = html.split(marker, 1)[1].split("};", 1)[0]
        # The guard must be the FIRST meaningful statement in the handler —
        # anything stateful above it would defeat the purpose.
        stripped = [
            line.strip()
            for line in after_onclose.splitlines()
            if line.strip() and not line.strip().startswith("//")
        ]
        self.assertTrue(stripped, "onclose handler body is empty")
        self.assertIn(
            "previewSocket !== ws",
            stripped[0],
            f"onclose first statement must early-return on stale WS, got: {stripped[0]!r}",
        )
        self.assertIn(
            "return",
            stripped[0],
            f"onclose stale check must return, got: {stripped[0]!r}",
        )

    def test_onopen_and_onmessage_also_have_stale_guard(self):
        """Belt-and-suspenders: the pattern already existed on onopen /
        onmessage before this PR. Make sure nobody removes it later."""
        html = self._preview_html()
        for marker in ("ws.onopen = () => {", "ws.onmessage = (event) => {"):
            self.assertIn(marker, html, f"{marker} missing from template")
            body = html.split(marker, 1)[1].split("};", 1)[0]
            self.assertIn(
                "previewSocket !== ws",
                body,
                f"{marker} missing stale-WS guard",
            )

    def test_placeholder_hidden_by_frame_class(self):
        """Canvas theme uses !important display for the empty placeholder.

        Inline ``style.display = 'none'`` is not enough to hide it once a
        browser frame renders, so the live canvas needs a more specific class
        selector plus JS add/remove hooks.
        """
        html = self._preview_html()
        self.assertIn(
            "body.first-look-canvas #live-canvas-wrap.preview-has-frame #preview-empty{display:none!important}",
            html,
        )
        self.assertIn("wrap.classList.add('preview-has-frame')", html)
        self.assertIn("wrap.classList.remove('preview-has-frame')", html)

    def test_new_chat_control_lives_with_chat_toggle(self):
        html = self._preview_html()
        topbar = html.split('<div id="topbar">', 1)[1].split('<div id="model-notice"', 1)[0]
        self.assertNotIn("New Chat", topbar)
        controls = html.split('<div class="chat-controls">', 1)[1].split('<div id="chat">', 1)[0]
        self.assertIn('id="new-chat-btn"', controls)
        self.assertIn('id="chat-collapse-btn"', controls)
        self.assertNotIn('class="chat-control-btn chat-collapse-btn"', controls)

    def test_shared_browser_status_has_visible_target(self):
        """Bridge readiness copy must have a real DOM target near the send UI.

        The preview note is hidden on the mobile canvas, so the status target is
        the user's only visible explanation when the run button is disabled.
        """
        html = self._preview_html()
        self.assertIn('id="shared-browser-status"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("setStatusCopy('shared-browser-status'", html)

    def test_exhausted_run_control_stays_focusable_with_disabled_semantics(self):
        html = self._preview_html()
        self.assertIn(
            '<button id="sendbtn" type="button" aria-label="Run task" aria-disabled="false" title="Run task">',
            html,
        )
        availability = html.split("function updateSendAvailability() {", 1)[1].split(
            "function autoGrow", 1
        )[0]
        self.assertIn("const hardUnavailable = sending || !agentId || !sharedBrowserReady;", availability)
        self.assertIn("send.disabled = hardUnavailable;", availability)
        self.assertIn("hardUnavailable || quotaExhausted", availability)
        self.assertIn("send.title = 'Guest runs used up. Start a free trial.';", availability)
        self.assertIn(
            '#sendbtn:disabled,#sendbtn[aria-disabled="true"]{opacity:0.4;cursor:not-allowed}',
            html,
        )

    def test_exhausted_run_surfaces_inline_trial_cta_without_modal(self):
        html = self._preview_html()
        quota_copy = html.split("function updateQuotaCopy() {", 1)[1].split(
            "function showQuotaFeedback", 1
        )[0]
        self.assertIn('href="/trial"', quota_copy)
        self.assertNotIn("showQuotaModal()", quota_copy)
        self.assertIn('id="quota-bar" role="status" aria-live="polite"', html)
        self.assertIn("const trialLink = document.querySelector('#quota-bar a[href=\"/trial\"]');", html)
        send_guard = html.split("async function doSend() {", 1)[1].split(
            "const message =", 1
        )[0]
        self.assertIn("if (remainingGuestRuns <= 0)", send_guard)
        self.assertIn("showQuotaFeedback();", send_guard)

    def test_connected_status_clears_idle_preview_fallback(self):
        """Ready status should clear stale warming/unavailable preview copy.

        Keep this guarded so periodic status refreshes do not overwrite run
        progress or the final browser frame after a run has produced output.
        """
        html = self._preview_html()
        marker = "if (data.connected) {"
        self.assertIn(marker, html)
        connected_branch = html.split(marker, 1)[1].split(
            "} else if (!data.bridge_configured)",
            1,
        )[0]
        self.assertIn("setStatusCopy('shared-browser-status'", connected_branch)
        self.assertIn("!sending && !previewHasFrame && previewState === 'idle'", connected_branch)
        self.assertIn("setPreviewNote('Shared browser ready for guest runs.', 'ok')", connected_branch)


if __name__ == "__main__":
    import unittest
    unittest.main()
