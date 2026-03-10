"""Integration test: provision cleanup-by-slot through the full relay→bridge path.

Validates that:
1. Relay forwards ?slot= query string to the bridge
2. Bridge cleans only the targeted slot
3. Private-core engine builds the correct cleanup URL with ?slot=
4. cloud_tools/private_core_client pass slot through the call chain

Run:
    python3 test_provision_cleanup_slot.py
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from websockets.datastructures import Headers

from relay import Relay
from chrome_bridge import Agent, _parse_prov_tab_id, _extract_prov_slot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeProcess:
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


def _make_request(path: str, headers: Headers | None = None):
    return SimpleNamespace(path=path, headers=headers or Headers())


def _make_agent() -> Agent:
    return Agent(relay_url="ws://localhost:8765/tunnel")


# ===========================================================================
# 1. Relay forwards ?slot= to bridge via http_proxy
# ===========================================================================

class TestRelayForwardsSlotQuery(unittest.TestCase):
    """Relay._process_request should include ?slot=XX in the bridge path."""

    def test_cleanup_with_slot_query_forwards_to_bridge(self):
        """GET /api/agents/<id>/provision-cleanup?slot=ab12 sends /provision-cleanup?slot=ab12 to bridge."""
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                # Register a fake agent
                relay.agents["claude-deadbeef"] = MagicMock()
                relay.agent_users["claude-deadbeef"] = "internal"

                # Capture what path relay.http_proxy receives
                captured_paths = []
                original_http_proxy = relay.http_proxy

                async def mock_http_proxy(agent_id, method, path, **kw):
                    captured_paths.append(path)
                    return {"status": 200, "body": {"status": "cleaned_up"}}

                relay.http_proxy = mock_http_proxy

                headers = Headers([("Authorization", "Bearer test-token")])
                req = _make_request(
                    "/api/agents/claude-deadbeef/provision-cleanup?slot=ab12",
                    headers,
                )
                resp = asyncio.run(relay._process_request(None, req))

                self.assertIsNotNone(resp)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(len(captured_paths), 1)
                self.assertEqual(captured_paths[0], "/provision-cleanup?slot=ab12")
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token

    def test_cleanup_without_slot_sends_bare_path(self):
        """GET /api/agents/<id>/provision-cleanup (no query) sends /provision-cleanup."""
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                relay.agents["claude-deadbeef"] = MagicMock()
                relay.agent_users["claude-deadbeef"] = "internal"

                captured_paths = []

                async def mock_http_proxy(agent_id, method, path, **kw):
                    captured_paths.append(path)
                    return {"status": 200, "body": {"status": "cleaned_up"}}

                relay.http_proxy = mock_http_proxy

                headers = Headers([("Authorization", "Bearer test-token")])
                req = _make_request(
                    "/api/agents/claude-deadbeef/provision-cleanup",
                    headers,
                )
                resp = asyncio.run(relay._process_request(None, req))

                self.assertIsNotNone(resp)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(len(captured_paths), 1)
                self.assertEqual(captured_paths[0], "/provision-cleanup")
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token


# ===========================================================================
# 2. Bridge handles slot-targeted cleanup correctly
# ===========================================================================

class TestBridgeSlotCleanup(unittest.IsolatedAsyncioTestCase):
    """Bridge _handle_provision_cleanup + _handle_http dispatch."""

    async def test_http_dispatch_routes_cleanup_with_query(self):
        """_handle_http routes /provision-cleanup?slot=ab12 to cleanup handler."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp = tempfile.mkdtemp(prefix="prov_slot_test_")
        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": tmp,
            "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002,
            "process": _FakeProcess(),
            "temp_dir": tempfile.mkdtemp(prefix="prov_slot_test_"),
            "profile_dir_name": "Profile 2",
        }

        # Simulate the HTTP message the relay sends to the bridge
        await agent._handle_http({
            "type": "http",
            "req_id": "r-test123",
            "method": "POST",
            "path": "/provision-cleanup?slot=ab12",
        })

        # ab12 cleaned, cd34 survives
        self.assertNotIn("ab12", agent._prov_chromes)
        self.assertIn("cd34", agent._prov_chromes)

        # Verify response
        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["req_id"], "r-test123")
        self.assertEqual(sent["body"]["status"], "cleaned_up")

        # Clean up remaining
        import shutil
        shutil.rmtree(agent._prov_chromes["cd34"]["temp_dir"], ignore_errors=True)


# ===========================================================================
# 3. Private-core engine builds correct URL with ?slot=
# ===========================================================================

def _engine_available():
    try:
        import private_core_engine  # noqa: F401
        return True
    except RuntimeError:
        return False


@unittest.skipUnless(_engine_available(), "private_core_engine stub — needs overlay")
class TestEngineCleanupURL(unittest.IsolatedAsyncioTestCase):
    """private_core_engine.provision_cleanup appends ?slot= to the URL."""

    @patch("httpx.AsyncClient")
    async def test_cleanup_url_includes_slot(self, mock_client_cls):
        """provision_cleanup(slot='ab12') appends ?slot=ab12 to the API URL."""
        import private_core_engine as engine

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await engine.provision_cleanup(
            agent_id="claude-deadbeef",
            relay_host="127.0.0.1",
            relay_port=8765,
            slot="ab12",
        )

        self.assertTrue(result)
        # Verify the URL includes ?slot=ab12
        call_args = mock_client.get.call_args
        url = call_args[0][0]
        self.assertIn("/provision-cleanup", url)
        self.assertIn("?slot=ab12", url)

    @patch("httpx.AsyncClient")
    async def test_cleanup_url_no_slot(self, mock_client_cls):
        """provision_cleanup(slot='') does NOT append ?slot= to the URL."""
        import private_core_engine as engine

        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await engine.provision_cleanup(
            agent_id="claude-deadbeef",
            relay_host="127.0.0.1",
            relay_port=8765,
        )

        self.assertTrue(result)
        url = mock_client.get.call_args[0][0]
        self.assertIn("/provision-cleanup", url)
        self.assertNotIn("?slot=", url)


# ===========================================================================
# 4. cloud_tools → private_core_client slot passthrough
# ===========================================================================

class TestCloudToolsSlotPassthrough(unittest.IsolatedAsyncioTestCase):
    """cloud_tools.provision_cleanup passes slot through to private_core_client."""

    async def test_slot_reaches_execute(self):
        """cloud_tools.provision_cleanup(slot='ab12') passes slot to client.execute."""
        from private_core_client import PrivateCoreClient, _set_private_core_client_for_tests
        import cloud_tools

        captured_kwargs = {}

        class _CapturingClient(PrivateCoreClient):
            async def execute(self, op, **kwargs):
                captured_kwargs.update(kwargs)
                return True

        _set_private_core_client_for_tests(_CapturingClient(mode="inprocess"))
        try:
            await cloud_tools.provision_cleanup(
                "claude-deadbeef",
                relay_host="127.0.0.1",
                relay_port=8765,
                slot="ab12",
            )
            self.assertEqual(captured_kwargs.get("slot"), "ab12")
        finally:
            _set_private_core_client_for_tests(None)

    async def test_empty_slot_reaches_execute(self):
        """cloud_tools.provision_cleanup() with no slot passes slot='' to execute."""
        from private_core_client import PrivateCoreClient, _set_private_core_client_for_tests
        import cloud_tools

        captured_kwargs = {}

        class _CapturingClient(PrivateCoreClient):
            async def execute(self, op, **kwargs):
                captured_kwargs.update(kwargs)
                return True

        _set_private_core_client_for_tests(_CapturingClient(mode="inprocess"))
        try:
            await cloud_tools.provision_cleanup(
                "claude-deadbeef",
                relay_host="127.0.0.1",
                relay_port=8765,
            )
            self.assertEqual(captured_kwargs.get("slot"), "")
        finally:
            _set_private_core_client_for_tests(None)


# ===========================================================================
# 5. End-to-end slot extraction from tab_id
# ===========================================================================

class TestSlotExtractionEndToEnd(unittest.TestCase):
    """Verify the full path: tab_id → _extract_prov_slot → cleanup targets correct slot."""

    def test_new_format_tab_id_extracts_slot(self):
        """prov-ab12-DEADBEEF → slot 'ab12' → cleanup targets 'ab12'."""
        tab_id = "prov-ab12-DEADBEEF1234"
        slot = _extract_prov_slot(tab_id)
        self.assertEqual(slot, "ab12")

        # Simulate what runtime_tabs/signup_agent do
        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp_ab = tempfile.mkdtemp(prefix="e2e_ab_")
        tmp_cd = tempfile.mkdtemp(prefix="e2e_cd_")
        agent._prov_chromes["ab12"] = {
            "port": 10001, "process": _FakeProcess(),
            "temp_dir": tmp_ab, "profile_dir_name": "P1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002, "process": _FakeProcess(),
            "temp_dir": tmp_cd, "profile_dir_name": "P2",
        }

        # This is what the cleanup path does
        asyncio.run(agent._handle_provision_cleanup(
            req_id="e2e", path=f"/provision-cleanup?slot={slot}",
        ))

        self.assertNotIn("ab12", agent._prov_chromes)
        self.assertIn("cd34", agent._prov_chromes)

        import shutil
        shutil.rmtree(tmp_cd, ignore_errors=True)

    def test_old_format_tab_id_backward_compat(self):
        """prov-DEADBEEF → slot '' → single prov Chrome cleaned (backward compat)."""
        tab_id = "prov-DEADBEEF1234"
        slot = _extract_prov_slot(tab_id)
        self.assertEqual(slot, "")

        agent = _make_agent()
        agent.ws = AsyncMock()

        tmp = tempfile.mkdtemp(prefix="e2e_old_")
        agent._prov_chromes["ab12"] = {
            "port": 10001, "process": _FakeProcess(),
            "temp_dir": tmp, "profile_dir_name": "P1",
        }

        # Empty slot + single prov Chrome → backward compat cleanup
        asyncio.run(agent._handle_provision_cleanup(
            req_id="e2e-old", path="/provision-cleanup",
        ))

        self.assertEqual(len(agent._prov_chromes), 0)


if __name__ == "__main__":
    unittest.main()
