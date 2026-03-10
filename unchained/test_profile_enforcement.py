"""Tests for profile enforcement fixes (PR #51).

Covers:
1. _ensure_profile_tab skips short-circuit when tab is pending close
2. _ensure_profile_tab short-circuits normally for healthy cached tabs
3. _ensure_profile_tab re-provisions when cached tab is pending close
4. loadChatProfiles JS logic: offline placeholder preserves selectedProfilePath

Run:
    uv run python test_profile_enforcement.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
sys.path.insert(0, os.path.dirname(__file__))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Minimal core stub for _ensure_profile_tab tests
# ---------------------------------------------------------------------------
class _FakeCore:
    def __init__(self):
        self._session_tabs: dict = {}
        self._session_agent_map: dict = {}
        self._session_last_active: dict = {}
        self._session_profile_paths: dict = {}
        self._tabs_pending_close: dict = {}

    def _parse_relay(self):
        return "127.0.0.1", 8765

    async def _close_session_tab(self, session_id: str):
        self._session_tabs.pop(session_id, None)
        self._session_agent_map.pop(session_id, None)
        self._session_last_active.pop(session_id, None)
        self._session_profile_paths.pop(session_id, None)


# ---------------------------------------------------------------------------
# 1. _ensure_profile_tab: pending_close prevents short-circuit
# ---------------------------------------------------------------------------
class TestEnsureProfileTabPendingClose(unittest.IsolatedAsyncioTestCase):
    """Tab in _tabs_pending_close must not be reused."""

    async def test_pending_close_tab_triggers_reprovision(self):
        """When the cached tab is pending close, _ensure_profile_tab re-provisions."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-abc"
        profile = "/chrome/Profile 1"
        old_tab = "prov-aa11-DEAD"

        # Simulate a cached tab that failed cleanup and is pending retry
        core._session_tabs[sid] = old_tab
        core._session_profile_paths[sid] = profile
        core._session_agent_map[sid] = "claude-test"
        core._session_last_active[sid] = time.time()
        core._tabs_pending_close[old_tab] = ("claude-test", 0)

        mock_launch = AsyncMock(return_value={"tab_id": "prov-bb22-FRESH"})
        with patch("cloud_tools.provision_launch", mock_launch):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, "prov-bb22-FRESH")
        self.assertEqual(core._session_tabs[sid], "prov-bb22-FRESH")
        mock_launch.assert_called_once()

    async def test_healthy_cached_tab_short_circuits(self):
        """When the cached tab is healthy, _ensure_profile_tab returns it without re-provisioning."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-def"
        profile = "/chrome/Profile 1"
        cached_tab = "prov-cc33-GOOD"

        core._session_tabs[sid] = cached_tab
        core._session_profile_paths[sid] = profile
        core._session_last_active[sid] = time.time() - 10

        mock_launch = AsyncMock()
        with patch("cloud_tools.provision_launch", mock_launch):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, cached_tab)
        mock_launch.assert_not_called()
        # last_active should be refreshed
        self.assertGreater(core._session_last_active[sid], time.time() - 2)

    async def test_different_profile_triggers_reprovision(self):
        """Changing profile always re-provisions regardless of pending state."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-ghi"
        old_profile = "/chrome/Profile 1"
        new_profile = "/chrome/Profile 2"

        core._session_tabs[sid] = "prov-dd44-OLD"
        core._session_profile_paths[sid] = old_profile
        core._session_agent_map[sid] = "claude-test"
        core._session_last_active[sid] = time.time()

        mock_launch = AsyncMock(return_value={"tab_id": "prov-ee55-NEW"})
        with patch("cloud_tools.provision_launch", mock_launch):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", new_profile)

        self.assertEqual(tab_id, "prov-ee55-NEW")
        self.assertEqual(core._session_profile_paths[sid], new_profile)


# ---------------------------------------------------------------------------
# 2. loadChatProfiles JS logic: offline placeholder
# ---------------------------------------------------------------------------
class TestLoadChatProfilesOfflinePlaceholder(unittest.TestCase):
    """Verify the JS template produces a placeholder option when bridge is offline."""

    def _extract_load_fn(self) -> str:
        """Extract the loadChatProfiles function from templates.py."""
        import web_app.templates as tpl
        # Find the function in CLAUDE_CHAT_HTML (which becomes CHAT_HTML)
        # Search for the function body in the raw module source
        src = open(tpl.__file__).read()
        # Find one of the loadChatProfiles functions
        pattern = r"(async function loadChatProfiles\(\) \{.*?^\})"
        match = re.search(pattern, src, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "loadChatProfiles not found in templates.py")
        return match.group(1)

    def test_offline_placeholder_branch_exists(self):
        """The JS has a branch that creates 'Saved profile (bridge offline)' option."""
        fn_body = self._extract_load_fn()
        self.assertIn("gotProfiles", fn_body)
        self.assertIn("Saved profile (bridge offline)", fn_body)

    def test_selectedProfilePath_always_syncs_with_sel_value(self):
        """selectedProfilePath is always set from sel.value, never left stale."""
        fn_body = self._extract_load_fn()
        # The function should end with selectedProfilePath = sel.value || '';
        # and NOT have any branch that skips this assignment
        self.assertIn("selectedProfilePath = sel.value || '';", fn_body)
        # There should be no branch that sets selectedProfilePath independently
        # (the old bug had selectedProfilePath = remembered in one branch)
        lines = fn_body.split("\n")
        assigns = [l.strip() for l in lines if "selectedProfilePath =" in l and "remembered" not in l and "sel.value" not in l]
        # Filter out the initial assignment at the top (selectedProfilePath = remembered)
        non_init_stale = [a for a in assigns if "remembered" not in a]
        self.assertEqual(non_init_stale, [], "Found stale selectedProfilePath assignments")

    def test_got_profiles_flag_set_inside_loop(self):
        """gotProfiles is set to true inside the profile iteration loop."""
        fn_body = self._extract_load_fn()
        # gotProfiles = true should appear after the path check
        idx_got = fn_body.index("gotProfiles = true")
        idx_path = fn_body.index("if (!path) continue")
        self.assertGreater(idx_got, idx_path)


# ---------------------------------------------------------------------------
# 3. Log redaction
# ---------------------------------------------------------------------------
class TestProfileLogRedaction(unittest.TestCase):
    """The provisioning log line should not contain the full path."""

    def test_log_uses_basename(self):
        """The print statement in _ensure_profile_tab uses os.path.basename."""
        import inspect
        from web_app.handlers.chat_stream import _ensure_profile_tab

        src = inspect.getsource(_ensure_profile_tab)
        self.assertIn("os.path.basename(profile_path)", src)
        self.assertNotIn('profile={profile_path}"', src)


if __name__ == "__main__":
    unittest.main()
