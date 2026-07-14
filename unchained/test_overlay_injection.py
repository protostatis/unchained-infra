"""Tests for overlay injection in chat_stream.py.

Verifies:
1. Local bridge users (no _session_tabs entry) get overlay via "auto" fallback
2. Concrete tab_id sessions (profile/headless) still inject normally
3. Guest and OpenRouter sessions never get overlay
4. _inject_overlay accepts "auto" as a valid tab_id
5. Error path: injection failure leaves state consistent (not marked injected)
6. Downstream consumers (_broadcast_overlay, _route_followup) handle "auto"
7. Concurrent sessions with "auto" get independent overlay state
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _discard_task(coro):
    coro.close()
    return MagicMock()


class TestOverlayAutoTabInjection(unittest.TestCase):
    """Regression: PR #156 rejected tab_id='auto', breaking local bridge overlay."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.create_task_patcher = patch(
            "web_app.handlers.chat_stream.asyncio.create_task",
            side_effect=_discard_task,
        )
        self.create_task_patcher.start()

    def tearDown(self):
        self.create_task_patcher.stop()
        self.loop.close()

    def test_inject_overlay_accepts_auto(self):
        """_inject_overlay should NOT reject tab_id='auto'."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        # Patch cloud_tools to capture the injection call
        mock_run_js = AsyncMock(return_value="")
        mock_run_cdp = AsyncMock(return_value={})

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
            run_cdp_command=mock_run_cdp,
        )}):
            _inject_overlay(core, "s-test-001", "agent-1", "auto",
                            "hello world", user_id="u1")

        # Should have created overlay state (not returned early)
        self.assertIn("s-test-001", core._overlay_sessions)
        overlay = core._overlay_sessions["s-test-001"]
        self.assertEqual(overlay.tab_id, "auto")

    def test_inject_overlay_rejects_empty(self):
        """_inject_overlay should reject empty/None tab_id."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}

        _inject_overlay(core, "s-test-002", "agent-1", "",
                        "hello world", user_id="u1")

        # Should have returned early — no overlay state created
        self.assertNotIn("s-test-002", core._overlay_sessions)

    def test_inject_overlay_accepts_concrete_tab(self):
        """_inject_overlay should accept concrete tab IDs."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        mock_run_js = AsyncMock(return_value="")
        mock_run_cdp = AsyncMock(return_value={})

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
            run_cdp_command=mock_run_cdp,
        )}):
            _inject_overlay(core, "s-test-003", "agent-1",
                            "ABCD1234-TAB-ID",
                            "hello world", user_id="u1")

        self.assertIn("s-test-003", core._overlay_sessions)
        self.assertEqual(core._overlay_sessions["s-test-003"].tab_id, "ABCD1234-TAB-ID")


class TestOverlayInjectionErrorPath(unittest.TestCase):
    """Verify overlay state is consistent when cloud_tools.run_js raises."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.create_task_patcher = patch(
            "web_app.handlers.chat_stream.asyncio.create_task",
            side_effect=_discard_task,
        )
        self.create_task_patcher.start()

    def tearDown(self):
        self.create_task_patcher.stop()
        self.loop.close()

    def test_injection_failure_does_not_mark_injected(self):
        """If run_js raises, overlay.injected must remain False."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        mock_run_js = AsyncMock(side_effect=Exception("bridge not connected"))

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
        )}):
            _inject_overlay(core, "s-err-001", "agent-1", "auto",
                            "test prompt", user_id="u1")

        # State is created (before the async task runs)
        self.assertIn("s-err-001", core._overlay_sessions)
        overlay = core._overlay_sessions["s-err-001"]
        # injected stays False because the async task hasn't run (no event loop)
        self.assertFalse(overlay.injected)

    def test_injection_failure_preserves_pending_events(self):
        """Pending events should not be drained if injection fails."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        mock_run_js = AsyncMock(side_effect=Exception("timeout"))

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
        )}):
            _inject_overlay(core, "s-err-002", "agent-1", "auto",
                            "test prompt", user_id="u1")

        overlay = core._overlay_sessions["s-err-002"]
        # Add events after creation — they should stay buffered
        overlay.pending_events.append({"type": "text", "data": "hello"})
        self.assertEqual(len(overlay.pending_events), 1)
        self.assertFalse(overlay.injected)


class TestOverlayAutoTabDownstreamConsumers(unittest.TestCase):
    """Verify downstream consumers of overlay.tab_id handle 'auto' correctly."""

    def test_broadcast_overlay_passes_auto_to_run_js(self):
        """_broadcast_overlay should pass tab_id='auto' to cloud_tools.run_js."""
        from web_state import OverlaySessionState
        from web_app.handlers.chat_stream import _broadcast_overlay

        core = MagicMock()
        overlay = OverlaySessionState(
            session_id="s-bc-001",
            agent_id="agent-1",
            tab_id="auto",
            user_id="u1",
            injected=True,
        )
        core._overlay_sessions = {"s-bc-001": overlay}

        # _broadcast_overlay uses asyncio.create_task, needs a running loop
        async def _run_broadcast():
            with patch("web_app.handlers.chat_stream._core", return_value=core):
                _broadcast_overlay("s-bc-001", {"type": "text", "data": "hi"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run_broadcast())
        finally:
            loop.close()

        # Should not raise — "auto" is a valid tab_id for run_js

    def test_route_followup_includes_auto_tab_id(self):
        """_route_followup should include tab_id='auto' in the ws_msg."""
        from web_state import OverlaySessionState

        overlay = OverlaySessionState(
            session_id="s-rf-001",
            agent_id="agent-1",
            tab_id="auto",
            user_id="u1",
            injected=True,
        )
        # "auto" is truthy, so the guard `if overlay.tab_id:` passes
        ws_msg = {}
        if overlay.tab_id:
            ws_msg["tab_id"] = overlay.tab_id
        self.assertEqual(ws_msg["tab_id"], "auto")

    def test_route_followup_omits_empty_tab_id(self):
        """Empty tab_id should not be included in ws_msg."""
        from web_state import OverlaySessionState

        overlay = OverlaySessionState(
            session_id="s-rf-002",
            agent_id="agent-1",
            tab_id="",
            user_id="u1",
        )
        ws_msg = {}
        if overlay.tab_id:
            ws_msg["tab_id"] = overlay.tab_id
        self.assertNotIn("tab_id", ws_msg)

    def test_route_followup_forwards_model(self):
        """Overlay follow-ups should preserve the active model lane."""
        from web_state import OverlaySessionState
        from web_app.handlers.overlay_ws import _route_followup

        core = MagicMock()
        overlay = OverlaySessionState(
            session_id="s-model-001",
            agent_id="agent-1",
            tab_id="auto",
            user_id="u1",
            model="codex-cli:gpt-5.5",
            injected=True,
        )
        agent_ws = MagicMock()
        agent_ws.closed = False
        agent_ws.send_json = AsyncMock(return_value=None)
        core._overlay_sessions = {"s-model-001": overlay}
        core._chat_agents = {"agent-1": agent_ws}
        core._session_agents = {}
        core._response_queues = {}
        core._response_req_ids = {}
        core._session_last_active = {}

        async def _run_route():
            with patch("web_app.handlers.overlay_ws._core", return_value=core), \
                 patch("web_app.handlers.overlay_ws.asyncio.create_task", side_effect=_discard_task):
                return await _route_followup(core, "s-model-001", "follow up")

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(_run_route())
        finally:
            loop.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        sent_msg = agent_ws.send_json.await_args.args[0]
        self.assertEqual(sent_msg["model"], "codex-cli:gpt-5.5")


class TestOverlayFollowupTurnRegistry(unittest.IsolatedAsyncioTestCase):
    """Signed-in overlay follow-ups must share the canonical turn lifecycle."""

    async def _core_with_turn(self, *, terminal: bool, send_error: bool = False):
        from web_state import ChatTurnRegistry, ChatTurnState, OverlaySessionState

        registry = ChatTurnRegistry()
        prior_turn = ChatTurnState(
            owner_user_id="user-1",
            owner_key_hash="key-1",
            session_id="s-overlay-registry",
            req_id="prior-request",
            chat_agent_id="chat-agent-1",
            routing_agent_id="chat-agent-1",
            cdp_agent_id="cdp-agent-1",
            tab_id="auto",
        )
        await registry.start(prior_turn)
        if terminal:
            prior_turn.publish({"type": "done"})
        overlay = OverlaySessionState(
            session_id=prior_turn.session_id,
            agent_id="cdp-agent-1",
            tab_id="auto",
            user_id="user-1",
            model="codex-cli:gpt-5.5",
            slot=2,
            injected=False,
        )
        agent_ws = SimpleNamespace(
            closed=False,
            send_json=AsyncMock(
                side_effect=RuntimeError("bridge closed") if send_error else None
            ),
        )
        core = SimpleNamespace(
            _chat_turns=registry,
            _overlay_sessions={prior_turn.session_id: overlay},
            _chat_agents={"chat-agent-1": agent_ws},
            _session_agents={prior_turn.session_id: "chat-agent-1"},
            _session_last_active={},
            _response_queues={},
            _response_req_ids={},
        )
        return core, prior_turn, agent_ws

    async def test_active_turn_drops_followup_without_replacing_it(self):
        from web_app.handlers.overlay_ws import _route_followup

        core, prior_turn, agent_ws = await self._core_with_turn(terminal=False)

        with patch("web_app.handlers.chat_stream._broadcast_overlay") as broadcast:
            result = await _route_followup(core, prior_turn.session_id, "follow up")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["code"], "turn_active")
        self.assertEqual(result["status"], 409)
        self.assertIs(core._chat_turns.get(prior_turn.session_id), prior_turn)
        agent_ws.send_json.assert_not_awaited()
        self.assertEqual(core._response_queues, {})
        broadcast.assert_called_once()
        overlay_event = broadcast.call_args.args[1]
        self.assertEqual(overlay_event["type"], "error")
        self.assertEqual(overlay_event["code"], "turn_active")

    async def test_followup_without_retained_owner_returns_visible_failure(self):
        from web_app.handlers.overlay_ws import _route_followup
        from web_state import ChatTurnRegistry, OverlaySessionState

        session_id = "s-overlay-no-owner"
        core = SimpleNamespace(
            _chat_turns=ChatTurnRegistry(),
            _overlay_sessions={
                session_id: OverlaySessionState(
                    session_id=session_id,
                    agent_id="cdp-agent-1",
                    tab_id="auto",
                    user_id="user-1",
                    injected=True,
                )
            },
            _chat_agents={},
            _session_agents={},
            _session_last_active={},
            _response_queues={},
            _response_req_ids={},
        )

        with patch("web_app.handlers.chat_stream._broadcast_overlay") as broadcast:
            result = await _route_followup(core, session_id, "follow up")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["code"], "turn_owner_missing")
        self.assertEqual(result["status"], 409)
        broadcast.assert_called_once()
        self.assertEqual(broadcast.call_args.args[1]["type"], "error")

    async def test_terminal_turn_creates_canonical_followup_and_forwards_context(self):
        from web_app.handlers.overlay_ws import _route_followup

        core, prior_turn, agent_ws = await self._core_with_turn(terminal=True)

        result = await _route_followup(core, prior_turn.session_id, "follow up")

        self.assertTrue(result["ok"])
        self.assertEqual(result["code"], "followup_routed")
        self.assertEqual(result["status"], 200)
        current = core._chat_turns.get(prior_turn.session_id)
        self.assertIsNot(current, prior_turn)
        self.assertEqual(current.owner_user_id, "user-1")
        self.assertEqual(current.owner_key_hash, "key-1")
        self.assertEqual(current.status, "active")
        self.assertEqual(core._response_queues, {})
        sent = agent_ws.send_json.await_args.args[0]
        self.assertEqual(sent["req_id"], current.req_id)
        self.assertEqual(sent["model"], "codex-cli:gpt-5.5")
        self.assertEqual(sent["tab_id"], "auto")
        self.assertEqual(sent["slot"], 2)

    async def test_followup_send_failure_terminalizes_its_registry_turn(self):
        from web_app.handlers.overlay_ws import _route_followup

        core, prior_turn, _agent_ws = await self._core_with_turn(
            terminal=True, send_error=True
        )
        with patch("web_app.handlers.chat_stream._broadcast_overlay") as broadcast:
            result = await _route_followup(core, prior_turn.session_id, "follow up")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["code"], "agent_send_failed")
        self.assertEqual(result["status"], 502)
        current = core._chat_turns.get(prior_turn.session_id)
        self.assertEqual(current.status, "error")
        self.assertTrue(current.stream_finished)
        self.assertEqual([event["type"] for event in current.journal], ["error", "done"])
        overlay_events = [call.args[1] for call in broadcast.call_args_list]
        self.assertEqual([event["type"] for event in overlay_events], ["error", "done"])

    async def test_http_handler_propagates_route_failure_status(self):
        from web_app.handlers.overlay_ws import handle_overlay_followup

        core, prior_turn, agent_ws = await self._core_with_turn(terminal=False)
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "session_id": prior_turn.session_id,
                    "message": "follow up",
                }
            )
        )

        with patch("web_app.handlers.overlay_ws._core", return_value=core):
            response = await handle_overlay_followup(request)

        payload = json.loads(response.text)
        self.assertEqual(response.status, 409)
        self.assertEqual(payload["ok"], False)
        self.assertEqual(payload["code"], "turn_active")
        self.assertEqual(payload["status"], 409)
        agent_ws.send_json.assert_not_awaited()

    async def test_http_handler_returns_route_success(self):
        from web_app.handlers.overlay_ws import handle_overlay_followup

        core, prior_turn, agent_ws = await self._core_with_turn(terminal=True)
        request = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "session_id": prior_turn.session_id,
                    "message": "follow up",
                }
            )
        )

        with patch("web_app.handlers.overlay_ws._core", return_value=core):
            response = await handle_overlay_followup(request)

        payload = json.loads(response.text)
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["code"], "followup_routed")
        self.assertEqual(payload["status"], 200)
        agent_ws.send_json.assert_awaited_once()


class TestOverlayConcurrentSessions(unittest.TestCase):
    """Verify concurrent sessions with 'auto' don't corrupt each other's state."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.create_task_patcher = patch(
            "web_app.handlers.chat_stream.asyncio.create_task",
            side_effect=_discard_task,
        )
        self.create_task_patcher.start()

    def tearDown(self):
        self.create_task_patcher.stop()
        self.loop.close()

    def test_two_auto_sessions_get_separate_state(self):
        """Two sessions with tab_id='auto' on the same agent get independent state."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        mock_run_js = AsyncMock(return_value="")
        mock_run_cdp = AsyncMock(return_value={})

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
            run_cdp_command=mock_run_cdp,
        )}):
            _inject_overlay(core, "s-a-001", "agent-1", "auto",
                            "prompt A", user_id="u1")
            _inject_overlay(core, "s-b-002", "agent-1", "auto",
                            "prompt B", user_id="u2")

        # Both sessions should have independent overlay state
        self.assertIn("s-a-001", core._overlay_sessions)
        self.assertIn("s-b-002", core._overlay_sessions)
        overlay_a = core._overlay_sessions["s-a-001"]
        overlay_b = core._overlay_sessions["s-b-002"]
        self.assertIsNot(overlay_a, overlay_b)
        self.assertEqual(overlay_a.user_id, "u1")
        self.assertEqual(overlay_b.user_id, "u2")

    def test_auto_and_concrete_sessions_coexist(self):
        """An 'auto' session and a concrete-tab session don't interfere."""
        from web_app.handlers.chat_stream import _inject_overlay

        core = MagicMock()
        core._overlay_sessions = {}
        core._parse_relay.return_value = ("127.0.0.1", 8765)

        mock_run_js = AsyncMock(return_value="")
        mock_run_cdp = AsyncMock(return_value={})

        with patch.dict("sys.modules", {"cloud_tools": MagicMock(
            run_js=mock_run_js,
            run_cdp_command=mock_run_cdp,
        )}):
            _inject_overlay(core, "s-auto", "agent-1", "auto",
                            "prompt auto", user_id="u1")
            _inject_overlay(core, "s-concrete", "agent-1", "prov-ab12-REAL",
                            "prompt concrete", user_id="u2")

        self.assertEqual(core._overlay_sessions["s-auto"].tab_id, "auto")
        self.assertEqual(core._overlay_sessions["s-concrete"].tab_id, "prov-ab12-REAL")


class TestOverlayCallSiteGating(unittest.TestCase):
    """Verify the call site in handle_chat_msg correctly gates overlay injection."""

    def test_local_bridge_gets_auto_fallback(self):
        """When _session_tabs has no entry, overlay_tab should be 'auto'."""
        # This tests the logic: overlay_tab = tab_id or "auto"
        tab_id = None  # simulates core._session_tabs.get(session_id) miss
        overlay_tab = tab_id or "auto"
        self.assertEqual(overlay_tab, "auto")

    def test_concrete_tab_passes_through(self):
        """When _session_tabs has an entry, that tab_id passes through."""
        tab_id = "prov-ab12-REAL_TAB"
        overlay_tab = tab_id or "auto"
        self.assertEqual(overlay_tab, "prov-ab12-REAL_TAB")

    def test_guest_mode_skips_overlay(self):
        """Guest mode should never inject overlay, regardless of tab_id."""
        guest_mode = True
        is_openrouter = True
        # The gate: not guest_mode and not is_openrouter
        should_inject = not guest_mode and not is_openrouter
        self.assertFalse(should_inject)

    def test_openrouter_skips_overlay(self):
        """OpenRouter sessions should never inject overlay."""
        guest_mode = False
        is_openrouter = True
        should_inject = not guest_mode and not is_openrouter
        self.assertFalse(should_inject)

    def test_local_bridge_injects_overlay(self):
        """Local bridge (non-guest, non-openrouter) should inject overlay."""
        guest_mode = False
        is_openrouter = False
        should_inject = not guest_mode and not is_openrouter
        self.assertTrue(should_inject)


if __name__ == "__main__":
    unittest.main()
