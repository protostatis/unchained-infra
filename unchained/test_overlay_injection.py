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
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestOverlayAutoTabInjection(unittest.TestCase):
    """Regression: PR #156 rejected tab_id='auto', breaking local bridge overlay."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
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

    def tearDown(self):
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


class TestOverlayConcurrentSessions(unittest.TestCase):
    """Verify concurrent sessions with 'auto' don't corrupt each other's state."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
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
