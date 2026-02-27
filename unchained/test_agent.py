"""Unit tests for chrome_bridge.py and relay.py — tunnel protocol, auth, channels.

Tests use a real WebSocket relay (in-process) but mock Chrome CDP.
No Chrome instance needed.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

# Ensure unchained/ is on the path
sys.path.insert(0, os.path.dirname(__file__))

import websockets

from agent import Agent, _load_config, _parse_args, VERSION
from auth import Auth
from relay import Relay


def _run(coro):
    """Run an async coroutine in a new event loop."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------
class TestAuth(unittest.TestCase):
    """Test SQLite auth module."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_auth.db")
        self.auth = Auth(self.db_path)

    def test_create_and_validate_key(self):
        key = self.auth.create_key("user1")
        self.assertTrue(key.startswith("uc_live_"))
        self.assertEqual(len(key), len("uc_live_") + 24)
        info = self.auth.validate_key(key)
        self.assertIsNotNone(info)
        self.assertEqual(info["user_id"], "user1")
        self.assertEqual(info["key"], key)

    def test_validate_invalid_key(self):
        self.assertIsNone(self.auth.validate_key("bad_key"))

    def test_revoke_key(self):
        key = self.auth.create_key("user1")
        self.assertTrue(self.auth.revoke_key(key))
        self.assertIsNone(self.auth.validate_key(key))

    def test_revoke_nonexistent(self):
        self.assertFalse(self.auth.revoke_key("nonexistent"))

    def test_list_keys(self):
        self.auth.create_key("user1")
        self.auth.create_key("user2")
        keys = self.auth.list_keys()
        self.assertEqual(len(keys), 2)

    def test_list_keys_excludes_revoked(self):
        key = self.auth.create_key("user1")
        self.auth.create_key("user2")
        self.auth.revoke_key(key)
        active = self.auth.list_keys(active_only=True)
        self.assertEqual(len(active), 1)
        all_keys = self.auth.list_keys(active_only=False)
        self.assertEqual(len(all_keys), 2)

    def test_last_used_updated(self):
        key = self.auth.create_key("user1")
        keys_before = self.auth.list_keys()
        self.assertIsNone(keys_before[0]["last_used_at"])
        self.auth.validate_key(key)
        keys_after = self.auth.list_keys()
        self.assertIsNotNone(keys_after[0]["last_used_at"])


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    """Test config loading and CLI arg parsing."""

    def test_default_config(self):
        config = _load_config()
        self.assertIn("relay_url", config)
        self.assertIn("api_key", config)
        self.assertIn("cdp_host", config)
        self.assertIn("cdp_port", config)

    def test_parse_args_relay(self):
        config = {"relay_url": "ws://old", "api_key": "", "cdp_host": "127.0.0.1", "cdp_port": 9222}
        result = _parse_args(["start", "--relay", "ws://new:8765/tunnel"], config)
        self.assertEqual(result["relay_url"], "ws://new:8765/tunnel")

    def test_parse_args_key(self):
        config = {"relay_url": "", "api_key": "", "cdp_host": "127.0.0.1", "cdp_port": 9222}
        result = _parse_args(["start", "--key", "uk_test_key"], config)
        self.assertEqual(result["api_key"], "uk_test_key")

    def test_parse_args_port(self):
        config = {"relay_url": "", "api_key": "", "cdp_host": "127.0.0.1", "cdp_port": 9222}
        result = _parse_args(["start", "--port", "9515"], config)
        self.assertEqual(result["cdp_port"], 9515)

    def test_env_var_override(self):
        with patch.dict(os.environ, {"UNCHAINED_RELAY_URL": "ws://env:1234/tunnel"}):
            config = _load_config()
            self.assertEqual(config["relay_url"], "ws://env:1234/tunnel")


# ---------------------------------------------------------------------------
# Integration tests: Agent ↔ Relay
# ---------------------------------------------------------------------------
class TestAgentRelay(unittest.TestCase):
    """Test agent-relay communication with real WebSockets."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Create a temp DB and a test key for each test
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_relay.db")
        self.auth = Auth(self.db_path)
        self.test_key = self.auth.create_key("test_user")

    def tearDown(self):
        self.loop.close()

    def test_auth_success(self):
        """Agent authenticates with relay and receives agent_id."""
        async def _test():
            relay = Relay(port=0, db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": self.test_key,
                    "agent_version": VERSION,
                    "cdp_port": 9222,
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                resp = json.loads(raw)
                self.assertEqual(resp["type"], "auth_ok")
                self.assertIn("agent_id", resp)

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_auth_fail_bad_key(self):
        """Agent with wrong API key gets auth_fail."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": "bad_key",
                    "agent_version": VERSION,
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                resp = json.loads(raw)
                self.assertEqual(resp["type"], "auth_fail")

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_heartbeat(self):
        """Agent ping gets pong from relay."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                # Auth first
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": self.test_key,
                    "agent_version": VERSION,
                }))
                await ws.recv()  # auth_ok

                # Send ping
                ts = time.time()
                await ws.send(json.dumps({"type": "ping", "ts": ts}))
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                resp = json.loads(raw)
                self.assertEqual(resp["type"], "pong")
                self.assertEqual(resp["ts"], ts)

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_http_proxy(self):
        """Relay forwards HTTP request to agent, agent responds."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            # Connect as agent
            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": self.test_key,
                    "agent_version": VERSION,
                }))
                auth_resp = json.loads(await ws.recv())
                agent_id = auth_resp["agent_id"]

                # Ask relay to send HTTP request to agent (now async with future)
                proxy_task = asyncio.create_task(
                    relay.http_proxy(agent_id, "GET", "/json"))

                # Agent should receive the HTTP request
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                self.assertEqual(msg["type"], "http")
                self.assertEqual(msg["path"], "/json")

                # Agent sends response
                await ws.send(json.dumps({
                    "type": "http_response",
                    "req_id": msg["req_id"],
                    "status": 200,
                    "body": [{"id": "TAB1", "type": "page", "url": "https://example.com"}],
                }))

                # Verify the proxy task got the response
                result = await asyncio.wait_for(proxy_task, timeout=5)
                self.assertEqual(result["status"], 200)
                self.assertIsInstance(result["body"], list)

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_http_proxy_timeout(self):
        """HTTP proxy returns 504 when agent doesn't respond."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": self.test_key,
                    "agent_version": VERSION,
                }))
                auth_resp = json.loads(await ws.recv())
                agent_id = auth_resp["agent_id"]

                # Agent receives but doesn't respond — short timeout
                result = await relay.http_proxy(agent_id, "GET", "/json", timeout=0.5)
                self.assertEqual(result["status"], 504)

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_client_connects_to_nonexistent_agent(self):
        """Client connecting to unknown agent_id gets closed."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            try:
                async with websockets.connect(
                        f"ws://127.0.0.1:{port}/cdp/nonexistent/auto",
                        max_size=50 * 1024 * 1024) as ws:
                    # Should be closed by relay
                    await asyncio.wait_for(ws.recv(), timeout=5)
                    self.fail("Expected connection to be closed")
            except websockets.exceptions.ConnectionClosed as e:
                self.assertEqual(e.code, 4004)

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())

    def test_agent_user_tracking(self):
        """Relay tracks which user owns each agent."""
        async def _test():
            relay = Relay(db_path=self.db_path)
            server = await websockets.serve(
                relay._route, "127.0.0.1", 0,
                max_size=50 * 1024 * 1024)
            port = server.sockets[0].getsockname()[1]

            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/tunnel",
                    max_size=50 * 1024 * 1024) as ws:
                await ws.send(json.dumps({
                    "type": "auth",
                    "api_key": self.test_key,
                    "agent_version": VERSION,
                }))
                auth_resp = json.loads(await ws.recv())
                agent_id = auth_resp["agent_id"]

                # Check agent lists
                all_agents = relay.get_agents()
                self.assertEqual(len(all_agents), 1)
                self.assertEqual(all_agents[0]["user_id"], "test_user")

                user_agents = relay.get_agents_for_user("test_user")
                self.assertEqual(len(user_agents), 1)

                other_agents = relay.get_agents_for_user("other_user")
                self.assertEqual(len(other_agents), 0)

                self.assertTrue(relay.agent_belongs_to_user(agent_id, "test_user"))
                self.assertFalse(relay.agent_belongs_to_user(agent_id, "other_user"))

            server.close()
            await server.wait_closed()

        self.loop.run_until_complete(_test())


# ---------------------------------------------------------------------------
# Agent unit tests (no relay needed)
# ---------------------------------------------------------------------------
class TestAgentUnit(unittest.TestCase):
    """Test Agent methods in isolation."""

    def test_get_tab_ws_url_auto(self):
        """_get_tab_ws_url with 'auto' returns first page tab."""
        agent = Agent("ws://localhost:8765/tunnel")
        fake_tabs = json.dumps([
            {"id": "TAB1", "type": "page", "url": "https://example.com",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/TAB1"},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_tabs
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ws_url = agent._get_tab_ws_url("auto")
            self.assertEqual(ws_url, "ws://127.0.0.1:9222/devtools/page/TAB1")

    def test_get_tab_ws_url_by_id(self):
        """_get_tab_ws_url with prefix matches specific tab."""
        agent = Agent("ws://localhost:8765/tunnel")
        fake_tabs = json.dumps([
            {"id": "TAB_AAA", "type": "page", "url": "https://a.com",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/TAB_AAA"},
            {"id": "TAB_BBB", "type": "page", "url": "https://b.com",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/TAB_BBB"},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_tabs
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ws_url = agent._get_tab_ws_url("TAB_BBB")
            self.assertEqual(ws_url, "ws://127.0.0.1:9222/devtools/page/TAB_BBB")

    def test_get_tab_ws_url_not_found(self):
        """_get_tab_ws_url raises RuntimeError for missing tab."""
        agent = Agent("ws://localhost:8765/tunnel")
        fake_tabs = json.dumps([
            {"id": "TAB1", "type": "page", "url": "https://example.com",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/TAB1"},
        ]).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_tabs
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with self.assertRaises(RuntimeError):
                agent._get_tab_ws_url("NONEXISTENT")

    def test_backoff_increases(self):
        """Backoff doubles on each reconnect attempt."""
        agent = Agent("ws://localhost:8765/tunnel")
        self.assertEqual(agent._backoff, 1)
        # Simulate reconnect attempts
        agent._backoff = min(agent._backoff * 2, 60)
        self.assertEqual(agent._backoff, 2)
        agent._backoff = min(agent._backoff * 2, 60)
        self.assertEqual(agent._backoff, 4)
        agent._backoff = min(agent._backoff * 2, 60)
        self.assertEqual(agent._backoff, 8)


if __name__ == "__main__":
    unittest.main()
