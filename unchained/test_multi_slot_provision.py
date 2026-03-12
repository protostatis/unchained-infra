"""Tests for multi-slot provision Chrome feature in chrome_bridge.py.

Validates parsing helpers, multi-slot storage, tab WS URL routing,
and provision cleanup path parsing -- all without a real Chrome or relay.

Run:
    python3 test_multi_slot_provision.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

from chrome_bridge import Agent, _parse_prov_tab_id, _extract_prov_slot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeProcess:
    """Minimal stand-in for a subprocess.Popen used by provision Chrome."""

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


def _make_agent() -> Agent:
    """Create an Agent with dummy relay URL; no real WebSocket is opened."""
    return Agent(relay_url="ws://localhost:8765/tunnel")


def _make_tab_json(tab_id: str, port: int) -> bytes:
    """Return fake /json response bytes for a single page tab."""
    tabs = [
        {
            "id": tab_id,
            "type": "page",
            "title": "Test",
            "url": "about:blank",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/{tab_id}",
        }
    ]
    return json.dumps(tabs).encode()


def _urlopen_factory(port_to_tabs: dict[int, list[dict]]):
    """Return a context-manager mock for urllib.request.urlopen.

    *port_to_tabs* maps port numbers to lists of tab dicts.  The mock inspects
    the requested URL to decide which tab list to return.
    """

    def _urlopen(req_or_url, **_kw):
        url = req_or_url if isinstance(req_or_url, str) else req_or_url.full_url
        for port, tabs in port_to_tabs.items():
            if f":{port}/" in url:
                cm = MagicMock()
                cm.__enter__ = lambda s: io.BytesIO(json.dumps(tabs).encode())
                cm.__exit__ = lambda s, *a: None
                return cm
        raise RuntimeError(f"Unexpected urlopen URL: {url}")

    return _urlopen


# ===================================================================
# 1. Parsing helpers
# ===================================================================

class TestParseProvTabId(unittest.TestCase):
    """_parse_prov_tab_id and _extract_prov_slot."""

    def test_new_format(self):
        """prov-ab12-HEXCHROMEID -> slot='ab12', real_id='HEXCHROMEID'."""
        slot, real_id = _parse_prov_tab_id("prov-ab12-HEXCHROMEID")
        self.assertEqual(slot, "ab12")
        self.assertEqual(real_id, "HEXCHROMEID")

    def test_old_format(self):
        """prov-HEXCHROMEID -> slot='', real_id='HEXCHROMEID'."""
        slot, real_id = _parse_prov_tab_id("prov-HEXCHROMEID")
        self.assertEqual(slot, "")
        self.assertEqual(real_id, "HEXCHROMEID")

    def test_new_format_auto(self):
        """prov-ab12-auto -> slot='ab12', real_id='auto'."""
        slot, real_id = _parse_prov_tab_id("prov-ab12-auto")
        self.assertEqual(slot, "ab12")
        self.assertEqual(real_id, "auto")

    def test_real_id_with_dashes(self):
        """prov-ab12-ABC-DEF should keep everything after second dash as real_id."""
        slot, real_id = _parse_prov_tab_id("prov-ab12-ABC-DEF")
        self.assertEqual(slot, "ab12")
        self.assertEqual(real_id, "ABC-DEF")

    def test_extract_prov_slot_new(self):
        self.assertEqual(_extract_prov_slot("prov-ab12-HEXID"), "ab12")

    def test_extract_prov_slot_old(self):
        self.assertEqual(_extract_prov_slot("prov-HEXID"), "")

    def test_extract_prov_slot_auto(self):
        self.assertEqual(_extract_prov_slot("prov-ab12-auto"), "ab12")


# ===================================================================
# 2. Multi-slot storage
# ===================================================================

class TestMultiSlotStorage(unittest.TestCase):
    """Verify _prov_chromes dict and cleanup helpers."""

    def test_initial_empty(self):
        agent = _make_agent()
        self.assertIsInstance(agent._prov_chromes, dict)
        self.assertEqual(len(agent._prov_chromes), 0)

    def test_store_multiple_slots(self):
        agent = _make_agent()
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12",
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-cd34",
            "profile_dir_name": "Profile 2",
        }
        self.assertEqual(len(agent._prov_chromes), 2)
        self.assertIn("ab12", agent._prov_chromes)
        self.assertIn("cd34", agent._prov_chromes)
        self.assertEqual(agent._prov_chromes["ab12"]["port"], 10001)
        self.assertEqual(agent._prov_chromes["cd34"]["port"], 10002)

    def test_cleanup_single_prov_removes_one(self):
        """_cleanup_single_prov cleans one slot without affecting the other."""
        agent = _make_agent()

        # Use real temp dirs so _cleanup_single_prov can attempt rmtree
        tmp_ab = tempfile.mkdtemp(prefix="prov_test_ab_")
        tmp_cd = tempfile.mkdtemp(prefix="prov_test_cd_")

        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp_ab,
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": tmp_cd,
            "profile_dir_name": "Profile 2",
        }

        # Pop and clean just "ab12"
        prov_ab = agent._prov_chromes.pop("ab12")
        agent._cleanup_single_prov(prov_ab)

        # "cd34" should still exist
        self.assertNotIn("ab12", agent._prov_chromes)
        self.assertIn("cd34", agent._prov_chromes)
        self.assertEqual(agent._prov_chromes["cd34"]["port"], 10002)

        # temp_dir for ab12 should be removed (or at least attempted)
        self.assertFalse(os.path.isdir(tmp_ab))
        # cd34 temp dir should still be intact
        self.assertTrue(os.path.isdir(tmp_cd))

        # Clean up remaining temp dir
        shutil.rmtree(tmp_cd, ignore_errors=True)

    def test_cleanup_all_prov_chromes(self):
        """_cleanup_all_prov_chromes clears everything."""
        agent = _make_agent()

        tmp_ab = tempfile.mkdtemp(prefix="prov_test_ab_")
        tmp_cd = tempfile.mkdtemp(prefix="prov_test_cd_")

        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp_ab,
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": tmp_cd,
            "profile_dir_name": "Profile 2",
        }

        agent._cleanup_all_prov_chromes()

        self.assertEqual(len(agent._prov_chromes), 0)
        self.assertFalse(os.path.isdir(tmp_ab))
        self.assertFalse(os.path.isdir(tmp_cd))


# ===================================================================
# 3. _get_tab_ws_url routing
# ===================================================================

class TestGetTabWsUrlRouting(unittest.TestCase):
    """Verify _get_tab_ws_url routes prov- tab IDs to the right port."""

    def _setup_agent_with_two_slots(self) -> Agent:
        agent = _make_agent()
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12",
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-cd34",
            "profile_dir_name": "Profile 2",
        }
        return agent

    @patch("urllib.request.urlopen")
    def test_prov_auto_routes_to_correct_slot(self, mock_urlopen):
        """prov-ab12-auto routes to port 10001 and returns first page tab."""
        agent = self._setup_agent_with_two_slots()

        tab_ab = {
            "id": "AAA111",
            "type": "page",
            "title": "Tab",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:10001/devtools/page/AAA111",
        }
        mock_urlopen.side_effect = _urlopen_factory({
            10001: [tab_ab],
            10002: [],
        })

        ws_url = agent._get_tab_ws_url("prov-ab12-auto")
        self.assertEqual(ws_url, "ws://127.0.0.1:10001/devtools/page/AAA111")

    @patch("urllib.request.urlopen")
    def test_prov_specific_id_routes_to_correct_slot(self, mock_urlopen):
        """prov-cd34-SOMEID routes to port 10002."""
        agent = self._setup_agent_with_two_slots()

        tab_cd = {
            "id": "SOMEID",
            "type": "page",
            "title": "Tab CD",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:10002/devtools/page/SOMEID",
        }
        mock_urlopen.side_effect = _urlopen_factory({
            10001: [],
            10002: [tab_cd],
        })

        ws_url = agent._get_tab_ws_url("prov-cd34-SOMEID")
        self.assertEqual(ws_url, "ws://127.0.0.1:10002/devtools/page/SOMEID")

    @patch("urllib.request.urlopen")
    def test_old_format_single_prov_falls_back(self, mock_urlopen):
        """prov-HEXID with exactly one prov Chrome falls back correctly."""
        agent = _make_agent()
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12",
            "profile_dir_name": "Profile 1",
        }

        tab = {
            "id": "HEXID",
            "type": "page",
            "title": "Tab",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:10001/devtools/page/HEXID",
        }
        mock_urlopen.side_effect = _urlopen_factory({10001: [tab]})

        ws_url = agent._get_tab_ws_url("prov-HEXID")
        self.assertEqual(ws_url, "ws://127.0.0.1:10001/devtools/page/HEXID")

    def test_old_format_multiple_prov_raises(self):
        """prov-HEXID with multiple prov Chromes raises RuntimeError."""
        agent = self._setup_agent_with_two_slots()

        with self.assertRaises(RuntimeError) as ctx:
            agent._get_tab_ws_url("prov-HEXID")
        self.assertIn("not found", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_prov_nonexistent_slot_raises(self, mock_urlopen):
        """prov-zz99-auto raises RuntimeError when slot doesn't exist."""
        agent = self._setup_agent_with_two_slots()

        with self.assertRaises(RuntimeError) as ctx:
            agent._get_tab_ws_url("prov-zz99-auto")
        self.assertIn("not found", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_prov_tab_not_found_raises(self, mock_urlopen):
        """prov-ab12-NOSUCH raises RuntimeError when tab ID doesn't match."""
        agent = self._setup_agent_with_two_slots()

        tab = {
            "id": "OTHERID",
            "type": "page",
            "title": "Tab",
            "url": "about:blank",
            "webSocketDebuggerUrl": "ws://127.0.0.1:10001/devtools/page/OTHERID",
        }
        mock_urlopen.side_effect = _urlopen_factory({10001: [tab]})

        with self.assertRaises(RuntimeError) as ctx:
            agent._get_tab_ws_url("prov-ab12-NOSUCH")
        self.assertIn("not found", str(ctx.exception))


# ===================================================================
# 4. _handle_provision_cleanup path parsing
# ===================================================================

class TestHandleProvisionCleanup(unittest.IsolatedAsyncioTestCase):
    """Verify _handle_provision_cleanup parses the slot query param."""

    async def test_cleanup_with_slot_query(self):
        """'/provision-cleanup?slot=ab12' cleans only slot ab12."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp_ab = tempfile.mkdtemp(prefix="prov_test_ab_")
        tmp_cd = tempfile.mkdtemp(prefix="prov_test_cd_")

        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp_ab,
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": tmp_cd,
            "profile_dir_name": "Profile 2",
        }

        await agent._handle_provision_cleanup(req_id=42, path="/provision-cleanup?slot=ab12")

        # ab12 should be gone, cd34 should remain
        self.assertNotIn("ab12", agent._prov_chromes)
        self.assertIn("cd34", agent._prov_chromes)

        # Verify the response was sent
        agent.ws.send.assert_called_once()
        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["status"], "cleaned_up")

        shutil.rmtree(tmp_cd, ignore_errors=True)

    async def test_cleanup_no_query_single_prov_backward_compat(self):
        """'/provision-cleanup' with one prov Chrome cleans it (backward compat)."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp = tempfile.mkdtemp(prefix="prov_test_single_")
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp,
            "profile_dir_name": "Profile 1",
        }

        await agent._handle_provision_cleanup(req_id=99, path="/provision-cleanup")

        self.assertEqual(len(agent._prov_chromes), 0)
        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["status"], "cleaned_up")

    async def test_cleanup_no_query_multiple_prov_cleans_all(self):
        """'/provision-cleanup' with multiple prov Chromes cleans up all."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp_ab = tempfile.mkdtemp(prefix="prov_test_ab_")
        tmp_cd = tempfile.mkdtemp(prefix="prov_test_cd_")

        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp_ab,
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": tmp_cd,
            "profile_dir_name": "Profile 2",
        }

        await agent._handle_provision_cleanup(req_id=99, path="/provision-cleanup")

        # Both should be cleaned up
        self.assertEqual(len(agent._prov_chromes), 0)

        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["status"], "cleaned_up")
        self.assertEqual(sent["body"]["cleaned"], 2)

    async def test_cleanup_no_prov_chromes(self):
        """Cleanup with nothing to clean returns 'no_provision_chrome'."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        await agent._handle_provision_cleanup(req_id=1, path="/provision-cleanup")

        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["status"], "no_provision_chrome")

    async def test_cleanup_unknown_slot_is_noop(self):
        """Cleanup with slot=UNKNOWN is a no-op (slot not found, don't destroy others)."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp = tempfile.mkdtemp(prefix="prov_test_")
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp,
            "profile_dir_name": "Profile 1",
        }

        await agent._handle_provision_cleanup(req_id=7, path="/provision-cleanup?slot=UNKNOWN")

        # slot=UNKNOWN not found → no-op, ab12 should survive
        self.assertEqual(len(agent._prov_chromes), 1)
        self.assertIn("ab12", agent._prov_chromes)

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
