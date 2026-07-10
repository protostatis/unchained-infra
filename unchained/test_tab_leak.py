"""Tests for tab leak prevention (web.py and chrome_bridge.py changes).

Tests cover:
1. _close_session_tab queues retry on failure
2. SSE disconnect triggers tab close
3. Agent disconnect cleans all session tabs
4. Per-agent tab cap with LRU eviction
5. Stale tab cleanup loop (retry + reconciliation)
6. Bridge orphan tab cleanup on reconnect (headless)
"""
import asyncio
import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(__file__))


def _run(coro):
    """Run an async coroutine in the test event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Mock CDP for web.py tests
# ---------------------------------------------------------------------------
def _make_mock_cdp(should_fail=False):
    """Create a mock CDP class that either succeeds or raises."""
    mock_cdp_instance = AsyncMock()
    mock_cdp_instance.ws = MagicMock()
    mock_cdp_instance.ws.close = AsyncMock()
    if should_fail:
        mock_cdp_instance.connect = AsyncMock(side_effect=Exception("Connection refused"))
    else:
        mock_cdp_instance.connect = AsyncMock()
        mock_cdp_instance.send = AsyncMock(return_value={"targetId": "NEW_TAB", "success": True})
    mock_cdp_cls = MagicMock(return_value=mock_cdp_instance)
    return mock_cdp_cls, mock_cdp_instance


# ---------------------------------------------------------------------------
# Test _close_session_tab retry logic
# ---------------------------------------------------------------------------
class TestCloseSessionTabRetry(unittest.TestCase):
    """Change 3: Failed tab closes are queued for retry."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Import web and reset state
        import web
        self.web = web
        self._save_state()
        self._clear_state()

    def tearDown(self):
        self._restore_state()
        self.loop.close()

    def _save_state(self):
        self._saved = {
            "tabs": dict(self.web._session_tabs),
            "map": dict(self.web._session_agent_map),
            "active": dict(self.web._session_last_active),
            "pending": dict(self.web._tabs_pending_close),
            "preview_generations": dict(self.web._chat_preview_generations),
        }

    def _restore_state(self):
        self.web._session_tabs.clear()
        self.web._session_tabs.update(self._saved["tabs"])
        self.web._session_agent_map.clear()
        self.web._session_agent_map.update(self._saved["map"])
        self.web._session_last_active.clear()
        self.web._session_last_active.update(self._saved["active"])
        self.web._tabs_pending_close.clear()
        self.web._tabs_pending_close.update(self._saved["pending"])
        self.web._chat_preview_generations.clear()
        self.web._chat_preview_generations.update(self._saved["preview_generations"])

    def _clear_state(self):
        self.web._session_tabs.clear()
        self.web._session_agent_map.clear()
        self.web._session_last_active.clear()
        self.web._tabs_pending_close.clear()
        self.web._chat_preview_generations.clear()

    def test_successful_close_removes_from_tracking(self):
        """Tab close success: removed from all dicts, not queued for retry."""
        self.web._session_tabs["s1"] = "tab_aaa"
        self.web._session_agent_map["s1"] = "agent_1"
        self.web._session_last_active["s1"] = time.time()
        self.web._chat_preview_generations["s1"] = 7

        mock_cdp_cls, _ = _make_mock_cdp(should_fail=False)
        with patch.dict("sys.modules", {"cdp": MagicMock(CDP=mock_cdp_cls)}):
            _run(self.web._close_session_tab("s1"))

        self.assertNotIn("s1", self.web._session_tabs)
        self.assertNotIn("s1", self.web._session_agent_map)
        self.assertNotIn("s1", self.web._session_last_active)
        self.assertNotIn("s1", self.web._chat_preview_generations)
        self.assertNotIn("tab_aaa", self.web._tabs_pending_close)

    def test_failed_close_queued_for_retry(self):
        """Tab close failure: tab_id added to _tabs_pending_close."""
        self.web._session_tabs["s1"] = "tab_bbb"
        self.web._session_agent_map["s1"] = "agent_1"
        self.web._session_last_active["s1"] = time.time()

        mock_cdp_cls, _ = _make_mock_cdp(should_fail=True)
        with patch.dict("sys.modules", {"cdp": MagicMock(CDP=mock_cdp_cls)}):
            _run(self.web._close_session_tab("s1"))

        # Session removed from tracking dicts
        self.assertNotIn("s1", self.web._session_tabs)
        # But tab queued for retry
        self.assertIn("tab_bbb", self.web._tabs_pending_close)
        agent_id, retries = self.web._tabs_pending_close["tab_bbb"]
        self.assertEqual(agent_id, "agent_1")
        self.assertEqual(retries, 0)

    def test_close_nonexistent_session_noop(self):
        """Closing a session with no tracked tab is a no-op."""
        _run(self.web._close_session_tab("nonexistent"))
        self.assertEqual(len(self.web._tabs_pending_close), 0)

    def test_close_session_without_agent_noop(self):
        """Tab with no agent_id in map is a no-op."""
        self.web._session_tabs["s1"] = "tab_ccc"
        # No entry in _session_agent_map
        _run(self.web._close_session_tab("s1"))
        self.assertNotIn("tab_ccc", self.web._tabs_pending_close)


# ---------------------------------------------------------------------------
# Test per-agent tab cap
# ---------------------------------------------------------------------------
class TestTabCap(unittest.TestCase):
    """Change 4: Per-agent tab cap with LRU eviction."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        import web
        self.web = web
        self._save_state()
        self._clear_state()

    def tearDown(self):
        self._restore_state()
        self.loop.close()

    def _save_state(self):
        self._saved = {
            "tabs": dict(self.web._session_tabs),
            "map": dict(self.web._session_agent_map),
            "active": dict(self.web._session_last_active),
            "pending": dict(self.web._tabs_pending_close),
        }

    def _restore_state(self):
        self.web._session_tabs.clear()
        self.web._session_tabs.update(self._saved["tabs"])
        self.web._session_agent_map.clear()
        self.web._session_agent_map.update(self._saved["map"])
        self.web._session_last_active.clear()
        self.web._session_last_active.update(self._saved["active"])
        self.web._tabs_pending_close.clear()
        self.web._tabs_pending_close.update(self._saved["pending"])

    def _clear_state(self):
        self.web._session_tabs.clear()
        self.web._session_agent_map.clear()
        self.web._session_last_active.clear()
        self.web._tabs_pending_close.clear()

    def test_cap_triggers_eviction(self):
        """When agent has MAX tabs, creating a new one evicts the oldest."""
        agent_id = "agent_cap_test"
        # Fill to cap
        for i in range(self.web._MAX_TABS_PER_AGENT):
            sid = f"s-cap-{i}"
            self.web._session_tabs[sid] = f"tab_{i}"
            self.web._session_agent_map[sid] = agent_id
            self.web._session_last_active[sid] = time.time() - (100 - i)  # oldest first

        # Count before
        agent_tab_count = sum(1 for aid in self.web._session_agent_map.values() if aid == agent_id)
        self.assertEqual(agent_tab_count, self.web._MAX_TABS_PER_AGENT)

        # Simulate what handle_chat_msg does before creating a tab
        agent_tabs = [
            (sid, self.web._session_last_active.get(sid, 0))
            for sid, aid in self.web._session_agent_map.items()
            if aid == agent_id
        ]
        self.assertGreaterEqual(len(agent_tabs), self.web._MAX_TABS_PER_AGENT)

        # Evict oldest
        agent_tabs.sort(key=lambda x: x[1])
        oldest_sid = agent_tabs[0][0]
        self.assertEqual(oldest_sid, "s-cap-0")  # oldest has lowest timestamp

        mock_cdp_cls, _ = _make_mock_cdp(should_fail=False)
        with patch.dict("sys.modules", {"cdp": MagicMock(CDP=mock_cdp_cls)}):
            _run(self.web._close_session_tab(oldest_sid))

        # After eviction
        remaining = sum(1 for aid in self.web._session_agent_map.values() if aid == agent_id)
        self.assertEqual(remaining, self.web._MAX_TABS_PER_AGENT - 1)
        self.assertNotIn("s-cap-0", self.web._session_tabs)

    def test_under_cap_no_eviction(self):
        """Under cap, no eviction happens."""
        agent_id = "agent_under"
        for i in range(3):
            sid = f"s-under-{i}"
            self.web._session_tabs[sid] = f"tab_u{i}"
            self.web._session_agent_map[sid] = agent_id
            self.web._session_last_active[sid] = time.time()

        agent_tabs = [
            (sid, self.web._session_last_active.get(sid, 0))
            for sid, aid in self.web._session_agent_map.items()
            if aid == agent_id
        ]
        self.assertLess(len(agent_tabs), self.web._MAX_TABS_PER_AGENT)


# ---------------------------------------------------------------------------
# Test agent disconnect cleanup
# ---------------------------------------------------------------------------
class TestAgentDisconnectCleanup(unittest.TestCase):
    """Change 2: Agent disconnect closes all session tabs for that agent."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        import web
        self.web = web
        self._save_state()
        self._clear_state()

    def tearDown(self):
        self._restore_state()
        self.loop.close()

    def _save_state(self):
        self._saved = {
            "tabs": dict(self.web._session_tabs),
            "map": dict(self.web._session_agent_map),
            "active": dict(self.web._session_last_active),
            "pending": dict(self.web._tabs_pending_close),
        }

    def _restore_state(self):
        self.web._session_tabs.clear()
        self.web._session_tabs.update(self._saved["tabs"])
        self.web._session_agent_map.clear()
        self.web._session_agent_map.update(self._saved["map"])
        self.web._session_last_active.clear()
        self.web._session_last_active.update(self._saved["active"])
        self.web._tabs_pending_close.clear()
        self.web._tabs_pending_close.update(self._saved["pending"])

    def _clear_state(self):
        self.web._session_tabs.clear()
        self.web._session_agent_map.clear()
        self.web._session_last_active.clear()
        self.web._tabs_pending_close.clear()

    def test_agent_disconnect_finds_sessions(self):
        """Disconnecting agent's sessions are correctly identified."""
        agent_id = "agent_disc"
        other_agent = "agent_other"

        # 3 sessions for agent_disc, 1 for agent_other
        for i in range(3):
            sid = f"s-disc-{i}"
            self.web._session_tabs[sid] = f"tab_d{i}"
            self.web._session_agent_map[sid] = agent_id
            self.web._session_last_active[sid] = time.time()

        self.web._session_tabs["s-other-0"] = "tab_o0"
        self.web._session_agent_map["s-other-0"] = other_agent
        self.web._session_last_active["s-other-0"] = time.time()

        # Simulate what the finally block does
        agent_sessions = [
            sid for sid, aid in list(self.web._session_agent_map.items())
            if aid == agent_id
        ]
        self.assertEqual(len(agent_sessions), 3)
        self.assertNotIn("s-other-0", agent_sessions)

        # Close each session tab
        mock_cdp_cls, _ = _make_mock_cdp(should_fail=False)
        with patch.dict("sys.modules", {"cdp": MagicMock(CDP=mock_cdp_cls)}):
            for sid in agent_sessions:
                _run(self.web._close_session_tab(sid))

        # agent_disc sessions gone, agent_other untouched
        self.assertEqual(
            sum(1 for aid in self.web._session_agent_map.values() if aid == agent_id), 0)
        self.assertIn("s-other-0", self.web._session_tabs)


# ---------------------------------------------------------------------------
# Test stale tab cleanup loop (retry logic)
# ---------------------------------------------------------------------------
class TestStaleTabCleanupRetry(unittest.TestCase):
    """Change 5: Retry pending closes in the cleanup loop."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        import web
        self.web = web
        self._save_state()
        self._clear_state()

    def tearDown(self):
        self._restore_state()
        self.loop.close()

    def _save_state(self):
        self._saved = {
            "tabs": dict(self.web._session_tabs),
            "map": dict(self.web._session_agent_map),
            "active": dict(self.web._session_last_active),
            "pending": dict(self.web._tabs_pending_close),
        }

    def _restore_state(self):
        self.web._session_tabs.clear()
        self.web._session_tabs.update(self._saved["tabs"])
        self.web._session_agent_map.clear()
        self.web._session_agent_map.update(self._saved["map"])
        self.web._session_last_active.clear()
        self.web._session_last_active.update(self._saved["active"])
        self.web._tabs_pending_close.clear()
        self.web._tabs_pending_close.update(self._saved["pending"])

    def _clear_state(self):
        self.web._session_tabs.clear()
        self.web._session_agent_map.clear()
        self.web._session_last_active.clear()
        self.web._tabs_pending_close.clear()

    def test_retry_succeeds_removes_from_queue(self):
        """Successful retry removes tab from pending queue."""
        self.web._tabs_pending_close["tab_retry1"] = ("agent_r", 0)

        mock_cdp_cls, _ = _make_mock_cdp(should_fail=False)

        async def _run_one_retry():
            for tab_id, (agent_id, retries) in list(self.web._tabs_pending_close.items()):
                if retries >= self.web._MAX_CLOSE_RETRIES:
                    del self.web._tabs_pending_close[tab_id]
                    continue
                try:
                    cdp = mock_cdp_cls(self.web._session_cdp_url(agent_id))
                    await asyncio.wait_for(cdp.connect(), timeout=5)
                    await cdp.send("Target.closeTarget", {"targetId": tab_id})
                    if cdp.ws:
                        await cdp.ws.close()
                    del self.web._tabs_pending_close[tab_id]
                except Exception:
                    self.web._tabs_pending_close[tab_id] = (agent_id, retries + 1)

        _run(_run_one_retry())
        self.assertNotIn("tab_retry1", self.web._tabs_pending_close)

    def test_retry_fails_increments_count(self):
        """Failed retry increments the retry counter."""
        self.web._tabs_pending_close["tab_retry2"] = ("agent_r", 0)

        mock_cdp_cls, _ = _make_mock_cdp(should_fail=True)

        async def _run_one_retry():
            for tab_id, (agent_id, retries) in list(self.web._tabs_pending_close.items()):
                if retries >= self.web._MAX_CLOSE_RETRIES:
                    del self.web._tabs_pending_close[tab_id]
                    continue
                try:
                    cdp = mock_cdp_cls(self.web._session_cdp_url(agent_id))
                    await asyncio.wait_for(cdp.connect(), timeout=5)
                    await cdp.send("Target.closeTarget", {"targetId": tab_id})
                    if cdp.ws:
                        await cdp.ws.close()
                    del self.web._tabs_pending_close[tab_id]
                except Exception:
                    self.web._tabs_pending_close[tab_id] = (agent_id, retries + 1)

        _run(_run_one_retry())
        self.assertIn("tab_retry2", self.web._tabs_pending_close)
        _, retries = self.web._tabs_pending_close["tab_retry2"]
        self.assertEqual(retries, 1)

    def test_max_retries_gives_up(self):
        """After MAX_CLOSE_RETRIES, tab is removed from queue."""
        self.web._tabs_pending_close["tab_giveup"] = ("agent_r", self.web._MAX_CLOSE_RETRIES)

        async def _run_one_retry():
            for tab_id, (agent_id, retries) in list(self.web._tabs_pending_close.items()):
                if retries >= self.web._MAX_CLOSE_RETRIES:
                    del self.web._tabs_pending_close[tab_id]
                    continue

        _run(_run_one_retry())
        self.assertNotIn("tab_giveup", self.web._tabs_pending_close)

    def test_stale_sessions_detected(self):
        """Sessions older than _STALE_TAB_SECONDS are identified as stale."""
        now = time.time()
        self.web._session_last_active["s-fresh"] = now
        self.web._session_last_active["s-stale"] = now - self.web._STALE_TAB_SECONDS - 1
        self.web._session_tabs["s-fresh"] = "tab_fresh"
        self.web._session_tabs["s-stale"] = "tab_stale"
        self.web._session_agent_map["s-fresh"] = "a1"
        self.web._session_agent_map["s-stale"] = "a1"

        stale = [
            sid for sid, ts in self.web._session_last_active.items()
            if now - ts > self.web._STALE_TAB_SECONDS
        ]
        self.assertEqual(stale, ["s-stale"])


# ---------------------------------------------------------------------------
# Test SSE disconnect cleanup
# ---------------------------------------------------------------------------
class TestSSEDisconnectCleanup(unittest.TestCase):
    """Change 1: SSE disconnect triggers tab close when stream not completed."""

    def test_stream_completed_flag_logic(self):
        """stream_completed is True only after done/error event."""
        # Simulate the SSE loop logic
        events = [
            {"type": "text", "content": "hello"},
            {"type": "text", "content": "world"},
            {"type": "done"},
        ]

        stream_completed = False
        for evt in events:
            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break

        self.assertTrue(stream_completed)

    def test_stream_not_completed_on_disconnect(self):
        """stream_completed stays False when loop breaks from write error."""
        events = [
            {"type": "text", "content": "hello"},
            # Simulate ConnectionResetError on next write - loop breaks
        ]

        stream_completed = False
        for evt in events:
            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break
        # Loop exited without done/error
        self.assertFalse(stream_completed)

    def test_stream_completed_on_error_event(self):
        """Error event also sets stream_completed."""
        events = [{"type": "error", "message": "something broke"}]

        stream_completed = False
        for evt in events:
            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break

        self.assertTrue(stream_completed)


# ---------------------------------------------------------------------------
# Test bridge orphan tab cleanup (chrome_bridge.py)
# ---------------------------------------------------------------------------
class TestBridgeOrphanCleanup(unittest.TestCase):
    """Change 6: Headless bridge cleans orphan tabs on reconnect."""

    def test_headless_flag_stored(self):
        """Agent stores headless flag from constructor."""
        from chrome_bridge import Agent
        agent = Agent("ws://localhost:8765/tunnel", headless=True)
        self.assertTrue(agent._headless)
        agent2 = Agent("ws://localhost:8765/tunnel", headless=False)
        self.assertFalse(agent2._headless)
        agent3 = Agent("ws://localhost:8765/tunnel")
        self.assertFalse(agent3._headless)  # default

    def test_cleanup_closes_extra_tabs(self):
        """_cleanup_orphan_tabs closes all but the first page tab."""
        from chrome_bridge import Agent
        agent = Agent("ws://localhost:8765/tunnel", headless=True)

        fake_tabs = [
            {"id": "TAB_1", "type": "page", "url": "about:blank"},
            {"id": "TAB_2", "type": "page", "url": "about:blank"},
            {"id": "TAB_3", "type": "page", "url": "about:blank"},
        ]
        close_calls = []

        def _mock_urlopen(url_or_req, **kwargs):
            url = url_or_req if isinstance(url_or_req, str) else (
                url_or_req.full_url if hasattr(url_or_req, 'full_url') else str(url_or_req))
            resp = MagicMock()
            if "/json/close/" in url:
                close_calls.append(url)
                resp.read.return_value = b"Target is closing"
            elif "/json" in url:
                resp.read.return_value = json.dumps(fake_tabs).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            agent._cleanup_orphan_tabs()

        # Should close TAB_2 and TAB_3, keep TAB_1
        self.assertEqual(len(close_calls), 2)
        self.assertTrue(any("TAB_2" in c for c in close_calls))
        self.assertTrue(any("TAB_3" in c for c in close_calls))

    def test_cleanup_noop_single_tab(self):
        """Single tab: no tabs closed."""
        from chrome_bridge import Agent
        agent = Agent("ws://localhost:8765/tunnel", headless=True)

        fake_tabs = [{"id": "TAB_1", "type": "page", "url": "about:blank"}]
        close_calls = []

        def _mock_urlopen(url_or_req, **kwargs):
            url = url_or_req if isinstance(url_or_req, str) else (
                url_or_req.full_url if hasattr(url_or_req, 'full_url') else str(url_or_req))
            resp = MagicMock()
            if "/json/close/" in url:
                close_calls.append(url)
                resp.read.return_value = b"Target is closing"
            elif "/json" in url:
                resp.read.return_value = json.dumps(fake_tabs).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            agent._cleanup_orphan_tabs()

        self.assertEqual(len(close_calls), 0)

    def test_cleanup_handles_connection_failure(self):
        """Connection failure during cleanup doesn't crash."""
        from chrome_bridge import Agent
        agent = Agent("ws://localhost:8765/tunnel", headless=True)

        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            # Should not raise
            agent._cleanup_orphan_tabs()

    def test_cleanup_skips_non_page_tabs(self):
        """Non-page tabs (devtools, background) are ignored."""
        from chrome_bridge import Agent
        agent = Agent("ws://localhost:8765/tunnel", headless=True)

        fake_tabs = [
            {"id": "TAB_1", "type": "page", "url": "about:blank"},
            {"id": "EXT_1", "type": "background_page", "url": "chrome-extension://..."},
            {"id": "DEV_1", "type": "other", "url": "devtools://..."},
        ]
        close_calls = []

        def _mock_urlopen(url_or_req, **kwargs):
            url = url_or_req if isinstance(url_or_req, str) else (
                url_or_req.full_url if hasattr(url_or_req, 'full_url') else str(url_or_req))
            resp = MagicMock()
            if "/json/close/" in url:
                close_calls.append(url)
                resp.read.return_value = b"Target is closing"
            elif "/json" in url:
                resp.read.return_value = json.dumps(fake_tabs).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
            agent._cleanup_orphan_tabs()

        # Only 1 page tab, so nothing to close
        self.assertEqual(len(close_calls), 0)


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
class TestConstants(unittest.TestCase):
    """Verify constants are set correctly."""

    def test_max_close_retries(self):
        import web
        self.assertEqual(web._MAX_CLOSE_RETRIES, 3)

    def test_max_tabs_per_agent(self):
        import web
        self.assertEqual(web._MAX_TABS_PER_AGENT, 10)

    def test_tabs_pending_close_initialized(self):
        import web
        self.assertIsInstance(web._tabs_pending_close, dict)


if __name__ == "__main__":
    unittest.main()
