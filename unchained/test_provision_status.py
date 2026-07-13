"""Tests for provisioned Chrome tab discovery (provision-status feature).

Validates:
1. Bridge _handle_provision_status returns tabs with prov-prefixed IDs
2. Relay forwards /api/agents/<id>/provision-status to bridge
3. cloud_tools.provision_status passes through to private_core_client
4. MCP list_provisioned_tabs tool formats output correctly
5. OP_PROVISION_STATUS is in PRIVATE_CORE_OPS

Run:
    python3 test_provision_status.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from websockets.datastructures import Headers

from chrome_bridge import Agent
from relay import Relay
from private_core_contracts import OP_PROVISION_STATUS, PRIVATE_CORE_OPS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeProcess:
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


def _make_agent() -> Agent:
    return Agent(relay_url="ws://localhost:8765/tunnel")


def _make_request(path: str, headers: Headers | None = None):
    return SimpleNamespace(path=path, headers=headers or Headers())


def _urlopen_factory(port_to_tabs: dict[int, list[dict]]):
    """Return a mock for urllib.request.urlopen based on port → tabs mapping."""
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


# ===========================================================================
# 1. Contract: OP_PROVISION_STATUS in PRIVATE_CORE_OPS
# ===========================================================================

class TestProvisionStatusContract(unittest.TestCase):
    def test_op_in_private_core_ops(self):
        self.assertIn(OP_PROVISION_STATUS, PRIVATE_CORE_OPS)

    def test_op_value(self):
        self.assertEqual(OP_PROVISION_STATUS, "provision_status")


# ===========================================================================
# 2. Bridge _handle_provision_status
# ===========================================================================

class TestBridgeProvisionStatus(unittest.IsolatedAsyncioTestCase):

    def test_reconcile_restores_persisted_caller_tag(self):
        """Bridge restart recovery retains slot ownership provenance."""
        agent = _make_agent()
        state = {
            "agent_id": agent.agent_id,
            "pid": 1234,
            "port": 10001,
            "temp_dir": "/tmp/fake-ab12",
            "profile_dir_name": "Profile 5",
            "ready": True,
            "launched_at": 1,
            "caller_tag": "chat-owner-tag",
        }
        with (
            patch("chrome_bridge._list_prov_state_slots", return_value=["ab12"]),
            patch("chrome_bridge._read_prov_state", return_value=state),
            patch("chrome_bridge._classify_prov_pid", return_value="alive"),
        ):
            agent._reconcile_prov_chromes(force=True)

        self.assertEqual(
            agent._prov_chromes["ab12"]["caller_tag"],
            "chat-owner-tag",
        )

    async def test_empty_when_no_prov_chromes(self):
        """Returns empty slots dict when no provision Chromes exist."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        await agent._handle_provision_status(req_id="r-empty")

        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["req_id"], "r-empty")
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["body"]["slots"], {})

    @patch("urllib.request.urlopen")
    async def test_returns_tabs_with_prov_prefixed_ids(self, mock_urlopen):
        """Returns tabs from each slot with prov-{slot}-{id} format."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        agent._prov_chromes["ab12"] = {
            "port": 10001,
            "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12",
            "profile_dir_name": "Profile 5",
            "caller_tag": "s-claude-abc12345-demo",
        }

        mock_urlopen.side_effect = _urlopen_factory({
            10001: [
                {"id": "AAA111BBB222CCC333DDD444EEE555FF", "type": "page",
                 "title": "X / Login", "url": "https://x.com/i/flow/login"},
                {"id": "FFF666EEE777DDD888CCC999BBB000AA", "type": "popup",
                 "title": "Sign in - Google", "url": "https://accounts.google.com/signin"},
            ],
        })

        await agent._handle_provision_status(req_id="r-tabs")

        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["status"], 200)
        slots = sent["body"]["slots"]
        self.assertIn("ab12", slots)
        self.assertEqual(slots["ab12"]["profile"], "Profile 5")
        self.assertEqual(slots["ab12"]["port"], 10001)
        self.assertEqual(slots["ab12"]["caller_tag"], "s-claude-abc12345-demo")

        tabs = slots["ab12"]["tabs"]
        self.assertEqual(len(tabs), 2)
        self.assertEqual(tabs[0]["tab_id"], "prov-ab12-AAA111BBB222CCC333DDD444EEE555FF")
        self.assertEqual(tabs[0]["type"], "page")
        self.assertEqual(tabs[0]["title"], "X / Login")
        self.assertEqual(tabs[1]["tab_id"], "prov-ab12-FFF666EEE777DDD888CCC999BBB000AA")
        self.assertEqual(tabs[1]["type"], "popup")

    @patch("urllib.request.urlopen")
    async def test_multiple_slots(self, mock_urlopen):
        """Returns tabs from multiple provisioned slots."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        agent._prov_chromes["ab12"] = {
            "port": 10001, "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12", "profile_dir_name": "Profile 1",
        }
        agent._prov_chromes["cd34"] = {
            "port": 10002, "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-cd34", "profile_dir_name": "Profile 2",
        }

        mock_urlopen.side_effect = _urlopen_factory({
            10001: [{"id": "TAB1", "type": "page", "title": "Tab1", "url": "about:blank"}],
            10002: [{"id": "TAB2", "type": "page", "title": "Tab2", "url": "about:blank"}],
        })

        await agent._handle_provision_status(req_id="r-multi")

        sent = json.loads(agent.ws.send.call_args[0][0])
        slots = sent["body"]["slots"]
        self.assertEqual(len(slots), 2)
        self.assertIn("ab12", slots)
        self.assertIn("cd34", slots)
        self.assertEqual(slots["ab12"]["tabs"][0]["tab_id"], "prov-ab12-TAB1")
        self.assertEqual(slots["cd34"]["tabs"][0]["tab_id"], "prov-cd34-TAB2")

    @patch("urllib.request.urlopen")
    async def test_filters_non_page_tabs(self, mock_urlopen):
        """Only page and popup tabs are returned, not background/service workers."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        agent._prov_chromes["ab12"] = {
            "port": 10001, "process": _FakeProcess(),
            "temp_dir": "/tmp/fake-ab12", "profile_dir_name": "Profile 1",
        }

        mock_urlopen.side_effect = _urlopen_factory({
            10001: [
                {"id": "PAGE1", "type": "page", "title": "Page", "url": "https://example.com"},
                {"id": "SW1", "type": "service_worker", "title": "SW", "url": "chrome://sw"},
                {"id": "BG1", "type": "background_page", "title": "BG", "url": "chrome://bg"},
            ],
        })

        await agent._handle_provision_status(req_id="r-filter")

        sent = json.loads(agent.ws.send.call_args[0][0])
        tabs = sent["body"]["slots"]["ab12"]["tabs"]
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["tab_id"], "prov-ab12-PAGE1")

    async def test_http_dispatch_routes_provision_status(self):
        """_handle_http routes /provision-status to _handle_provision_status."""
        agent = _make_agent()
        agent.ws = AsyncMock()

        await agent._handle_http({
            "type": "http",
            "req_id": "r-dispatch",
            "method": "GET",
            "path": "/provision-status",
        })

        sent = json.loads(agent.ws.send.call_args[0][0])
        self.assertEqual(sent["req_id"], "r-dispatch")
        self.assertEqual(sent["status"], 200)
        self.assertIn("slots", sent["body"])


# ===========================================================================
# 3. Relay route for provision-status
# ===========================================================================

class TestRelayProvisionStatusRoute(unittest.TestCase):

    def test_relay_forwards_provision_status(self):
        """GET /api/agents/<id>/provision-status proxies to bridge."""
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                relay.agents["claude-deadbeef"] = MagicMock()
                relay.agent_users["claude-deadbeef"] = "internal"

                captured_calls = []

                async def mock_http_proxy(agent_id, method, path, **kw):
                    captured_calls.append((agent_id, method, path))
                    return {"status": 200, "body": {"slots": {}}}

                relay.http_proxy = mock_http_proxy

                headers = Headers([("Authorization", "Bearer test-token")])
                req = _make_request(
                    "/api/agents/claude-deadbeef/provision-status",
                    headers,
                )
                resp = asyncio.run(relay._process_request(None, req))

                self.assertIsNotNone(resp)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(len(captured_calls), 1)
                self.assertEqual(captured_calls[0], ("claude-deadbeef", "GET", "/provision-status"))
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token

    def test_relay_rejects_unauthorized(self):
        """provision-status returns 401 without valid auth."""
        old_token = os.environ.get("RELAY_SHARED_TOKEN")
        os.environ["RELAY_SHARED_TOKEN"] = "test-token"
        try:
            with tempfile.NamedTemporaryFile(suffix=".db") as db:
                relay = Relay(db_path=db.name)
                relay.agents["claude-deadbeef"] = MagicMock()
                relay.agent_users["claude-deadbeef"] = "user1"

                headers = Headers([("Authorization", "Bearer wrong-token")])
                req = _make_request(
                    "/api/agents/claude-deadbeef/provision-status",
                    headers,
                )
                resp = asyncio.run(relay._process_request(None, req))

                self.assertIsNotNone(resp)
                self.assertEqual(resp.status_code, 401)
        finally:
            if old_token is None:
                os.environ.pop("RELAY_SHARED_TOKEN", None)
            else:
                os.environ["RELAY_SHARED_TOKEN"] = old_token


# ===========================================================================
# 4. cloud_tools passthrough
# ===========================================================================

class TestCloudToolsPassthrough(unittest.IsolatedAsyncioTestCase):

    async def test_provision_status_reaches_execute(self):
        """cloud_tools.provision_status passes through to client.execute."""
        from private_core_client import PrivateCoreClient, _set_private_core_client_for_tests
        import cloud_tools

        captured_kwargs = {}

        class _CapturingClient(PrivateCoreClient):
            async def execute(self, op, **kwargs):
                captured_kwargs["op"] = op
                captured_kwargs.update(kwargs)
                return {"slots": {}}

        _set_private_core_client_for_tests(_CapturingClient(mode="inprocess"))
        try:
            result = await cloud_tools.provision_status(
                "claude-deadbeef",
                relay_host="127.0.0.1",
                relay_port=8765,
            )
            self.assertEqual(captured_kwargs["op"], "provision_status")
            self.assertEqual(captured_kwargs["agent_id"], "claude-deadbeef")
            self.assertEqual(result, {"slots": {}})
        finally:
            _set_private_core_client_for_tests(None)


# ===========================================================================
# 5. MCP list_provisioned_tabs tool
# ===========================================================================

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import mcp.types
import mcp_server


class TestMcpListProvisionedTabs(unittest.IsolatedAsyncioTestCase):

    async def _call_tool(self, **kwargs) -> str:
        """Call list_provisioned_tabs via the tool manager and extract text."""
        result = await mcp_server.mcp._tool_manager.call_tool(
            "list_provisioned_tabs", kwargs,
        )
        # Tool returns TextContent
        self.assertEqual(len(result.content), 1)
        self.assertIsInstance(result.content[0], mcp.types.TextContent)
        return result.content[0].text

    async def test_no_provisioned_chromes(self):
        """Returns 'No provisioned Chrome instances.' when empty."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.provision_status", new=AsyncMock(return_value={"slots": {}})),
        ):
            text = await self._call_tool(agent_id="")
            self.assertEqual(text, "No provisioned Chrome instances.")

    async def test_formats_tabs_correctly(self):
        """Formats slot/tab output with prov-prefixed IDs."""
        mock_result = {
            "slots": {
                "ab12": {
                    "profile": "Profile 5",
                    "port": 10001,
                    "tabs": [
                        {"tab_id": "prov-ab12-AAA111", "type": "page",
                         "title": "X / Login", "url": "https://x.com/i/flow/login"},
                        {"tab_id": "prov-ab12-BBB222", "type": "popup",
                         "title": "Sign in - Google", "url": "https://accounts.google.com/signin"},
                    ],
                }
            }
        }
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.provision_status", new=AsyncMock(return_value=mock_result)),
        ):
            text = await self._call_tool(agent_id="")
            self.assertIn("Slot ab12", text)
            self.assertIn("Profile 5", text)
            self.assertIn("2 tabs", text)
            self.assertIn("prov-ab12-AAA111", text)
            self.assertIn("prov-ab12-BBB222", text)
            self.assertIn("[popup]", text)
            self.assertIn("X / Login", text)
            self.assertIn("Sign in - Google", text)

    async def test_handles_error(self):
        """Returns error message when provision_status fails."""
        with (
            patch("mcp_server._resolve_agent", return_value="claude-abc"),
            patch("cloud_tools.provision_status", new=AsyncMock(
                return_value={"error": "Agent not connected"})),
        ):
            text = await self._call_tool(agent_id="")
            self.assertIn("Error:", text)
            self.assertIn("Agent not connected", text)


if __name__ == "__main__":
    unittest.main()
