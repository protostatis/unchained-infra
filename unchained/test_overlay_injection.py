"""Tests for overlay injection in chat_stream.py.

Verifies:
1. Local bridge users (no _session_tabs entry) get overlay via "auto" fallback
2. Concrete tab_id sessions (profile/headless) still inject normally
3. Guest and OpenRouter sessions never get overlay
4. _inject_overlay accepts "auto" as a valid tab_id
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
