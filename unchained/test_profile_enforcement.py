"""Tests for profile enforcement fixes (PR #51).

Covers:
1. _ensure_profile_tab skips short-circuit when tab is pending close
2. _ensure_profile_tab validates and reuses healthy provision slots
3. stale provision slots relaunch the exact selected profile
4. concurrent stale requests launch only one replacement browser
5. profile status failures never fall back to the default browser
6. chat payloads distinguish explicit default from omitted profile intent

Run:
    uv run python test_profile_enforcement.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
sys.path.insert(0, os.path.dirname(__file__))

from web_state import profile_session_caller_tag  # noqa: E402


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
        self._expired_profile_sessions: dict = {}
        self._session_profile_locks: dict = {}
        self._session_allowed_tabs: dict = {}
        self._tabs_pending_close: dict = {}
        self.close_calls = 0

    def _parse_relay(self):
        return "127.0.0.1", 8765

    async def _close_session_tab(
        self,
        session_id: str,
        *,
        profile_lock_held: bool = False,
        preserve_profile_path: str = "",
        preserve_agent_id: str = "",
    ):
        self.assert_profile_lock_held = profile_lock_held
        self.close_calls += 1
        self._session_tabs.pop(session_id, None)
        self._session_agent_map.pop(session_id, None)
        self._session_last_active.pop(session_id, None)
        self._session_profile_paths.pop(session_id, None)
        self._session_allowed_tabs.pop(session_id, None)
        if preserve_profile_path:
            self._session_profile_paths[session_id] = preserve_profile_path
        if preserve_agent_id:
            self._session_agent_map[session_id] = preserve_agent_id


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
        mock_status = AsyncMock(return_value={
            "slots": {"cc33": {
                "profile": "Profile 1",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [{"tab_id": cached_tab}],
            }}
        })
        with (
            patch("cloud_tools.provision_launch", mock_launch),
            patch("cloud_tools.provision_status", mock_status),
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, cached_tab)
        mock_launch.assert_not_called()
        # last_active should be refreshed
        self.assertGreater(core._session_last_active[sid], time.time() - 2)
        mock_status.assert_awaited_once()

    async def test_owned_workspace_adopts_active_tab_even_when_cached_tab_is_live(self):
        """The first active tab in an exactly owned profile slot becomes authoritative."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-active-tab"
        profile = "/chrome/Profile 3"
        cached_tab = "prov-ab12-OLD"
        active_tab = "prov-ab12-NEW"
        core._session_tabs[sid] = cached_tab
        core._session_allowed_tabs[sid] = {cached_tab}
        core._session_profile_paths[sid] = profile

        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [
                    {"tab_id": active_tab},
                    {"tab_id": cached_tab},
                ],
            }}
        }
        mock_launch = AsyncMock()
        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value=status)),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, active_tab)
        self.assertEqual(core._session_tabs[sid], active_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {active_tab})
        mock_launch.assert_not_awaited()

    async def test_agent_handoff_keeps_background_tab_when_status_reports_old_active_tab(self):
        """A stale `/json` ordering cannot undo a hosted agent tab handoff."""
        from web_app.handlers.chat_stream import _ensure_profile_tab
        from web_state import (
            ProfileTabMonitorHandoff,
            profile_tab_monitor_handoffs,
            profile_tab_monitor_observed_tabs,
        )

        core = _FakeCore()
        sid = "s-test-agent-handoff"
        profile = "/chrome/Profile 3"
        old_tab = "prov-ab12-OLD"
        agent_tab = "prov-ab12-NEW"
        core._session_tabs[sid] = agent_tab
        core._session_allowed_tabs[sid] = {old_tab, agent_tab}
        core._session_profile_paths[sid] = profile
        profile_tab_monitor_observed_tabs(core)[sid] = old_tab
        profile_tab_monitor_handoffs(core)[sid] = ProfileTabMonitorHandoff(
            target_tab=agent_tab,
            baseline_observed_tab=old_tab,
        )
        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [
                    {"tab_id": old_tab},
                    {"tab_id": agent_tab},
                ],
            }}
        }

        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value=status)),
            patch("cloud_tools.provision_launch", AsyncMock()) as mock_launch,
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, agent_tab)
        self.assertEqual(core._session_tabs[sid], agent_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {old_tab, agent_tab})
        mock_launch.assert_not_awaited()

    async def test_liveness_poll_does_not_overwrite_a_newer_agent_handoff(self):
        """An in-flight profile poll returns the target installed by the agent."""
        from web_app.handlers.chat_stream import _ensure_profile_tab
        from web_state import (
            ProfileTabMonitorHandoff,
            profile_tab_monitor_handoffs,
            profile_tab_monitor_observed_tabs,
        )

        core = _FakeCore()
        sid = "s-test-agent-handoff-race"
        profile = "/chrome/Profile 3"
        old_tab = "prov-ab12-OLD"
        agent_tab = "prov-ab12-NEW"
        core._session_tabs[sid] = old_tab
        core._session_allowed_tabs[sid] = {old_tab}
        core._session_profile_paths[sid] = profile
        poll_started = asyncio.Event()
        release_poll = asyncio.Event()
        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [{"tab_id": old_tab}],
            }}
        }

        async def delayed_status(*_args, **_kwargs):
            poll_started.set()
            await release_poll.wait()
            return status

        with (
            patch("cloud_tools.provision_status", delayed_status),
            patch("cloud_tools.provision_launch", AsyncMock()) as mock_launch,
        ):
            ensure_task = asyncio.create_task(
                _ensure_profile_tab(core, sid, "claude-test", profile)
            )
            await asyncio.wait_for(poll_started.wait(), timeout=1)
            core._session_tabs[sid] = agent_tab
            core._session_allowed_tabs[sid] = {old_tab, agent_tab}
            profile_tab_monitor_observed_tabs(core)[sid] = old_tab
            profile_tab_monitor_handoffs(core)[sid] = ProfileTabMonitorHandoff(
                target_tab=agent_tab,
                baseline_observed_tab=old_tab,
            )
            release_poll.set()
            tab_id = await ensure_task

        self.assertEqual(tab_id, agent_tab)
        self.assertEqual(core._session_tabs[sid], agent_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {old_tab, agent_tab})
        mock_launch.assert_not_awaited()

    async def test_legacy_untagged_workspace_does_not_adopt_another_live_tab(self):
        """Legacy slots may retain their exact tab but cannot follow another one."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-legacy-active-tab"
        profile = "/chrome/Profile 3"
        cached_tab = "prov-ab12-OLD"
        other_tab = "prov-ab12-NEW"
        core._session_tabs[sid] = cached_tab
        core._session_allowed_tabs[sid] = {cached_tab}
        core._session_profile_paths[sid] = profile

        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": "",
                "tabs": [
                    {"tab_id": other_tab},
                    {"tab_id": cached_tab},
                ],
            }}
        }
        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value=status)),
            patch("cloud_tools.provision_launch", AsyncMock()) as mock_launch,
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, cached_tab)
        self.assertEqual(core._session_tabs[sid], cached_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {cached_tab})
        mock_launch.assert_not_awaited()

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

    async def test_missing_slot_relaunches_exact_profile(self):
        """A cleaned local slot is replaced using the remembered profile path."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-stale"
        profile = "/chrome/Profile 7"
        old_tab = "prov-f8f1-DEAD"
        fresh_tab = "prov-a1b2-FRESH"
        core._session_tabs[sid] = old_tab
        core._session_allowed_tabs[sid] = {old_tab}
        core._session_profile_paths[sid] = profile

        mock_status = AsyncMock(return_value={"slots": {}})
        mock_launch = AsyncMock(return_value={"tab_id": fresh_tab})
        with (
            patch("cloud_tools.provision_status", mock_status),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, fresh_tab)
        self.assertEqual(core._session_tabs[sid], fresh_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {fresh_tab})
        self.assertEqual(core._session_profile_paths[sid], profile)
        self.assertEqual(core.close_calls, 1)
        mock_launch.assert_awaited_once_with(
            "claude-test",
            profile,
            "127.0.0.1",
            8765,
            caller_tag=profile_session_caller_tag(sid),
        )

    async def test_live_replacement_tab_is_rebound_without_relaunch(self):
        """A closed tab in a live slot reuses another tab from the same profile."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-rebind"
        profile = "/chrome/Profile 3"
        core._session_tabs[sid] = "prov-ab12-CLOSED"
        core._session_profile_paths[sid] = profile
        live_tab = "prov-ab12-LIVE"

        mock_status = AsyncMock(return_value={
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [{"tab_id": live_tab}],
            }}
        })
        mock_launch = AsyncMock()
        with (
            patch("cloud_tools.provision_status", mock_status),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            tab_id = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(tab_id, live_tab)
        self.assertEqual(core._session_allowed_tabs[sid], {live_tab})
        self.assertEqual(core.close_calls, 0)
        mock_launch.assert_not_awaited()

    async def test_status_failure_fails_closed(self):
        """A bridge/status outage preserves the profile pin and never launches default."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-offline"
        profile = "/chrome/Profile 5"
        tab = "prov-beef-CURRENT"
        core._session_tabs[sid] = tab
        core._session_profile_paths[sid] = profile
        mock_launch = AsyncMock()

        with (
            patch(
                "cloud_tools.provision_status",
                AsyncMock(side_effect=RuntimeError("bridge offline")),
            ),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            with self.assertRaisesRegex(RuntimeError, "bridge offline"):
                await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(core._session_tabs[sid], tab)
        self.assertEqual(core._session_profile_paths[sid], profile)
        self.assertEqual(core.close_calls, 0)
        mock_launch.assert_not_awaited()

    async def test_concurrent_stale_requests_launch_once(self):
        """The per-session lock prevents duplicate replacement Chromes."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-race"
        profile = "/chrome/Profile 7"
        fresh_tab = "prov-cafe-FRESH"
        core._session_tabs[sid] = "prov-f8f1-DEAD"
        core._session_profile_paths[sid] = profile
        mock_status = AsyncMock(side_effect=[
            {"slots": {}},
            {"slots": {"cafe": {
                "profile": "Profile 7",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [{"tab_id": fresh_tab}],
            }}},
        ])
        mock_launch = AsyncMock(return_value={"tab_id": fresh_tab})

        with (
            patch("cloud_tools.provision_status", mock_status),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            results = await asyncio.gather(
                _ensure_profile_tab(core, sid, "claude-test", profile),
                _ensure_profile_tab(core, sid, "claude-test", profile),
            )

        self.assertEqual(results, [fresh_tab, fresh_tab])
        self.assertEqual(core.close_calls, 1)
        mock_launch.assert_awaited_once()

    async def test_relaunch_failure_preserves_profile_for_retry(self):
        """A failed replacement cannot expose the default browser on retry."""
        from web_app.handlers.chat_stream import (
            _ensure_profile_tab,
            _resolve_profile_intent,
        )

        core = _FakeCore()
        sid = "s-test-retry"
        profile = "/chrome/Profile 7"
        core._session_tabs[sid] = "prov-f8f1-DEAD"
        core._session_profile_paths[sid] = profile

        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value={"slots": {}})),
            patch(
                "cloud_tools.provision_launch",
                AsyncMock(side_effect=RuntimeError("launch failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertNotIn(sid, core._session_tabs)
        self.assertEqual(core._session_profile_paths[sid], profile)
        self.assertEqual(core._session_agent_map[sid], "claude-test")
        self.assertIn(sid, core._session_last_active)
        self.assertEqual(
            _resolve_profile_intent({}, None, core._session_profile_paths[sid]),
            ("profile", profile),
        )

        fresh_tab = "prov-a1b2-FRESH"
        with patch(
            "cloud_tools.provision_launch",
            AsyncMock(return_value={"tab_id": fresh_tab}),
        ):
            result = await _ensure_profile_tab(core, sid, "claude-test", profile)
        self.assertEqual(result, fresh_tab)

    async def test_slot_status_error_fails_closed(self):
        """Tab enumeration errors are not interpreted as a missing slot."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-status-error"
        profile = "/chrome/Profile 3"
        tab = "prov-ab12-CURRENT"
        core._session_tabs[sid] = tab
        core._session_profile_paths[sid] = profile
        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": profile_session_caller_tag(sid),
                "tabs": [{"error": "CDP unavailable"}],
            }}
        }
        mock_launch = AsyncMock()
        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value=status)),
            patch("cloud_tools.provision_launch", mock_launch),
        ):
            with self.assertRaisesRegex(RuntimeError, "status is unavailable"):
                await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(core._session_tabs[sid], tab)
        self.assertEqual(core.close_calls, 0)
        mock_launch.assert_not_awaited()

    async def test_foreign_slot_is_not_cleaned(self):
        """A recycled slot owned by another session is detached, never killed."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-owner"
        profile = "/chrome/Profile 3"
        core._session_tabs[sid] = "prov-ab12-OLD"
        core._session_profile_paths[sid] = profile
        fresh_tab = "prov-cd34-FRESH"
        status = {
            "slots": {"ab12": {
                "profile": "Profile 3",
                "caller_tag": "s-other-session",
                "tabs": [{"tab_id": "prov-ab12-OTHER"}],
            }}
        }
        with (
            patch("cloud_tools.provision_status", AsyncMock(return_value=status)),
            patch(
                "cloud_tools.provision_launch",
                AsyncMock(return_value={"tab_id": fresh_tab}),
            ),
        ):
            result = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(result, fresh_tab)
        self.assertEqual(core.close_calls, 0)

    async def test_profile_marked_default_tab_is_never_reused(self):
        """Corrupt profile state cannot route into the default browser."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-default-invariant"
        profile = "/chrome/Profile 3"
        core._session_tabs[sid] = "DEFAULT-TAB"
        core._session_profile_paths[sid] = profile
        fresh_tab = "prov-cd34-FRESH"
        mock_status = AsyncMock()
        with (
            patch("cloud_tools.provision_status", mock_status),
            patch(
                "cloud_tools.provision_launch",
                AsyncMock(return_value={"tab_id": fresh_tab}),
            ),
        ):
            result = await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertEqual(result, fresh_tab)
        self.assertEqual(core.close_calls, 0)
        mock_status.assert_not_awaited()

    async def test_profile_launch_rejects_non_provisioned_target(self):
        """A profile launch response must carry a slotted prov target."""
        from web_app.handlers.chat_stream import _ensure_profile_tab

        core = _FakeCore()
        sid = "s-test-invalid-launch"
        profile = "/chrome/Profile 3"
        with patch(
            "cloud_tools.provision_launch",
            AsyncMock(return_value={"tab_id": "DEFAULT-TAB"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "slotted tab_id"):
                await _ensure_profile_tab(core, sid, "claude-test", profile)

        self.assertNotIn(sid, core._session_tabs)
        self.assertEqual(core._session_profile_paths[sid], profile)

    async def test_delayed_stale_cleanup_cannot_close_replacement(self):
        """Cleanup captured for an old tab is generation-checked after locking."""
        from web_app import runtime_tabs

        sid = "s-test-cleanup-race"
        old_tab = "prov-f8f1-OLD"
        fresh_tab = "prov-a1b2-FRESH"
        core = SimpleNamespace(
            _session_profile_locks={},
            _session_tabs={sid: fresh_tab},
            _session_allowed_tabs={sid: {fresh_tab}},
            _session_agent_map={sid: "claude-test"},
            _session_last_active={sid: time.time()},
            _session_profile_paths={sid: "/chrome/Profile 7"},
            _expired_profile_sessions={},
            _overlay_sessions={},
            _chat_preview_generations={},
            _tabs_pending_close={},
        )
        mock_cleanup = AsyncMock()
        with (
            patch("web_app.runtime_tabs._core", return_value=core),
            patch("cloud_tools.provision_cleanup", mock_cleanup),
        ):
            await runtime_tabs.close_session_tab(
                sid,
                expected_tab_id=old_tab,
            )

        self.assertEqual(core._session_tabs[sid], fresh_tab)
        self.assertEqual(core._session_profile_paths[sid], "/chrome/Profile 7")
        mock_cleanup.assert_not_awaited()

    async def test_empty_generation_cannot_close_later_replacement(self):
        """An empty captured generation differs from a subsequently bound tab."""
        from web_app import runtime_tabs

        sid = "s-test-empty-generation"
        fresh_tab = "prov-a1b2-FRESH"
        core = SimpleNamespace(
            _session_profile_locks={},
            _session_tabs={sid: fresh_tab},
            _session_allowed_tabs={sid: {fresh_tab}},
            _session_agent_map={sid: "claude-test"},
            _session_last_active={sid: time.time()},
            _session_profile_paths={sid: "/chrome/Profile 7"},
            _expired_profile_sessions={},
            _overlay_sessions={},
            _chat_preview_generations={},
            _tabs_pending_close={},
        )
        with patch("web_app.runtime_tabs._core", return_value=core):
            await runtime_tabs.close_session_tab(sid, expected_tab_id="")

        self.assertEqual(core._session_tabs[sid], fresh_tab)
        self.assertEqual(core._session_profile_paths[sid], "/chrome/Profile 7")

    async def test_lifecycle_eviction_leaves_expired_profile_tombstone(self):
        """Non-explicit eviction can never turn an omitted request into default."""
        from web_app import runtime_tabs
        from web_app.handlers.chat_stream import _resolve_profile_intent

        sid = "s-test-expire"
        tab = "prov-ab12-TAB"
        core = SimpleNamespace(
            _session_profile_locks={},
            _session_tabs={sid: tab},
            _session_allowed_tabs={sid: {tab}},
            _session_agent_map={sid: "claude-test"},
            _session_last_active={sid: time.time()},
            _session_profile_paths={sid: "/chrome/Profile 7"},
            _expired_profile_sessions={},
            _overlay_sessions={},
            _chat_preview_generations={},
            _tabs_pending_close={},
            _tabs_pending_close_caller_tags={},
            _parse_relay=lambda: ("127.0.0.1", 8765),
        )
        with (
            patch("web_app.runtime_tabs._core", return_value=core),
            patch("cloud_tools.provision_cleanup", AsyncMock()),
        ):
            await runtime_tabs.close_session_tab(
                sid,
                expected_tab_id=tab,
                expire_profile=True,
            )

        self.assertIn(sid, core._expired_profile_sessions)
        self.assertEqual(
            _resolve_profile_intent(
                {},
                core._session_tabs.get(sid),
                core._session_profile_paths.get(sid),
                sid in core._expired_profile_sessions,
            ),
            ("expired", ""),
        )

    async def test_legacy_provision_eviction_without_path_leaves_tombstone(self):
        """Legacy prov pins remain fail-closed even without remembered paths."""
        from web_app import runtime_tabs

        sid = "s-test-legacy-expire"
        tab = "prov-ab12-TAB"
        core = SimpleNamespace(
            _session_profile_locks={},
            _session_tabs={sid: tab},
            _session_allowed_tabs={sid: {tab}},
            _session_agent_map={sid: "claude-test"},
            _session_last_active={sid: time.time()},
            _session_profile_paths={},
            _expired_profile_sessions={},
            _overlay_sessions={},
            _chat_preview_generations={},
            _tabs_pending_close={},
            _tabs_pending_close_caller_tags={},
            _parse_relay=lambda: ("127.0.0.1", 8765),
        )
        with (
            patch("web_app.runtime_tabs._core", return_value=core),
            patch("cloud_tools.provision_cleanup", AsyncMock()),
        ):
            await runtime_tabs.close_session_tab(
                sid,
                expected_tab_id=tab,
                expire_profile=True,
            )

        self.assertIn(sid, core._expired_profile_sessions)

    async def test_profile_cleanup_uses_stable_owner_tag(self):
        from web_app import runtime_tabs

        sid = "s-claude-abc12345-" + ("long" * 30)
        tab = "prov-ab12-TAB"
        core = SimpleNamespace(
            _session_profile_locks={},
            _session_tabs={sid: tab},
            _session_allowed_tabs={sid: {tab}},
            _session_agent_map={sid: "claude-test"},
            _session_last_active={sid: time.time()},
            _session_profile_paths={sid: "/chrome/Profile 7"},
            _expired_profile_sessions={},
            _overlay_sessions={},
            _chat_preview_generations={},
            _tabs_pending_close={},
            _tabs_pending_close_caller_tags={},
            _parse_relay=lambda: ("127.0.0.1", 8765),
        )
        mock_cleanup = AsyncMock(return_value={"status": "cleaned_up"})
        with (
            patch("web_app.runtime_tabs._core", return_value=core),
            patch("cloud_tools.provision_cleanup", mock_cleanup),
        ):
            await runtime_tabs.close_session_tab(sid, expected_tab_id=tab)

        expected_tag = profile_session_caller_tag(sid)
        self.assertLessEqual(len(expected_tag), 64)
        mock_cleanup.assert_awaited_once_with(
            "claude-test",
            "127.0.0.1",
            8765,
            slot="ab12",
            caller_tag=expected_tag,
        )

    async def test_explicit_default_clears_profile_session(self):
        """An explicit empty profile selection is allowed to leave profile mode."""
        from web_app.handlers.chat_stream import _clear_profile_tab

        core = _FakeCore()
        sid = "s-test-default"
        core._session_tabs[sid] = "prov-ab12-TAB"
        core._session_profile_paths[sid] = "/chrome/Profile 1"

        await _clear_profile_tab(core, sid)

        self.assertNotIn(sid, core._session_tabs)
        self.assertNotIn(sid, core._session_profile_paths)
        self.assertNotIn(sid, core._session_profile_locks)
        self.assertEqual(core.close_calls, 1)


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
        src = Path(tpl.__file__).read_text()
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
        self.assertIn("Saved profile (unavailable)", fn_body)
        self.assertIn("else if (remembered)", fn_body)

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

    def test_chat_payload_always_sends_profile_intent(self):
        """A resolved empty profile value explicitly selects default Chrome."""
        import web_app.templates as tpl

        src = Path(tpl.__file__).read_text()
        self.assertEqual(
            src.count("if (profileSelectionReady) payload.profile_path = profilePath;"),
            2,
        )
        self.assertIn(
            "if (hostedWorkspaceMode) payload.profile_path = profileSelectionReady ? profilePath : '';",
            src,
        )
        self.assertEqual(src.count("profileSelectionReady = true;"), 6)


class TestProfileSessionRoutingContract(unittest.TestCase):
    """Server routing preserves profile intent and fails closed."""

    def test_profile_intent_is_tri_state_and_fail_closed(self):
        from web_app.handlers.chat_stream import _resolve_profile_intent

        self.assertEqual(
            _resolve_profile_intent({"profile_path": ""}, "prov-ab12-TAB", "/Profile 1"),
            ("default", ""),
        )
        self.assertEqual(
            _resolve_profile_intent({"profile_path": ""}, "C" * 32, ""),
            ("unchanged", ""),
        )
        self.assertEqual(
            _resolve_profile_intent(
                {"profile_path": "/Profile 3"},
                "prov-ab12-TAB",
                "/Profile 1",
            ),
            ("profile", "/Profile 3"),
        )
        self.assertEqual(
            _resolve_profile_intent({}, "prov-ab12-TAB", "/Profile 1"),
            ("profile", "/Profile 1"),
        )
        self.assertEqual(
            _resolve_profile_intent({}, "prov-ab12-TAB", ""),
            ("expired", ""),
        )
        self.assertEqual(
            _resolve_profile_intent({}, "", "", True),
            ("expired", ""),
        )
        self.assertEqual(_resolve_profile_intent({}, "default-tab", ""), ("unchanged", ""))

    def test_handler_preserves_omitted_profile_and_rejects_unknown_stale_pin(self):
        import inspect
        from web_app.handlers.chat_stream import handle_chat_msg

        src = inspect.getsource(handle_chat_msg)
        self.assertIn('profile_selection_present = "profile_path" in body', src)
        self.assertIn('elif profile_intent == "expired":', src)
        self.assertIn('"code": "profile_session_expired"', src)
        self.assertIn("cdp_agent_id = remembered_cdp_agent_id", src)
        self.assertNotIn("_session_profile_paths.pop(session_id, None)\n        if cdp_agent_id", src)

    def test_early_profile_error_clears_both_response_maps(self):
        from web_app.handlers.chat_stream import _discard_response_registration

        core = SimpleNamespace(
            _response_queues={"sid": object()},
            _response_req_ids={"sid": "req"},
        )
        _discard_response_registration(core, "sid")
        self.assertEqual(core._response_queues, {})
        self.assertEqual(core._response_req_ids, {})


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
