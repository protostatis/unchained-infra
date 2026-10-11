"""Reconnect-generation isolation tests for the CDP relay."""

from __future__ import annotations

import asyncio
import json
import unittest

import websockets
from websockets.exceptions import ConnectionClosedError

from relay import Relay


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self.close_calls: list[tuple[int, str]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self, code: int, reason: str) -> None:
        self.close_calls.append((code, reason))


class TestRelayGenerationIsolation(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_closes_old_tunnel_and_keeps_new_heartbeat(self):
        class FakeAuth:
            def validate_key(self, key):
                return {"user_id": "test-user"} if key == "test-key" else None

        relay = Relay.__new__(Relay)
        relay.auth = FakeAuth()
        relay.agents = {}
        relay.agent_users = {}
        relay.agent_profiles = {}
        relay._next_channel = {}
        relay._agent_generation = {}
        relay._agent_close_tasks = set()
        relay.clients = {}
        relay._pending_http = {}

        server = await websockets.serve(
            relay._route,
            "127.0.0.1",
            0,
            ping_interval=None,
        )
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}/tunnel"

        try:
            old_ws = await websockets.connect(url, ping_interval=None)
            await old_ws.send(json.dumps({"type": "auth", "api_key": "test-key"}))
            self.assertEqual(json.loads(await old_ws.recv())["type"], "auth_ok")

            new_ws = await websockets.connect(url, ping_interval=None)
            await new_ws.send(json.dumps({"type": "auth", "api_key": "test-key"}))
            self.assertEqual(json.loads(await new_ws.recv())["type"], "auth_ok")

            with self.assertRaises(ConnectionClosedError):
                await asyncio.wait_for(old_ws.recv(), timeout=1)

            await new_ws.send(json.dumps({"type": "ping", "ts": 67890}))
            self.assertEqual(
                json.loads(await asyncio.wait_for(new_ws.recv(), timeout=1)),
                {"type": "pong", "ts": 67890},
            )
            await new_ws.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_superseded_agent_socket_is_closed(self):
        relay = Relay.__new__(Relay)
        relay._agent_close_tasks = set()
        old_socket = _FakeWebSocket()

        relay._schedule_agent_close(old_socket, "claude-test-agent")
        await asyncio.sleep(0)

        self.assertEqual(old_socket.close_calls, [(4001, "Agent reconnected")])

    async def test_stale_generation_ping_does_not_pong_on_replacement_socket(self):
        agent_id = "claude-test-agent"
        old_socket = _FakeWebSocket()
        replacement_socket = _FakeWebSocket()
        relay = Relay.__new__(Relay)
        relay.agents = {agent_id: replacement_socket}
        relay._agent_generation = {agent_id: 2}
        relay.clients = {}
        relay._pending_http = {}

        await relay._handle_agent_message(
            agent_id,
            {"type": "ping", "ts": 12345},
            agent_gen=1,
            source_ws=old_socket,
        )

        self.assertEqual(old_socket.sent, [])
        self.assertEqual(
            replacement_socket.sent,
            [],
            "a ping from a stale tunnel must not be answered on the new tunnel",
        )

        await relay._handle_agent_message(
            agent_id,
            {"type": "ping", "ts": 67890},
            agent_gen=2,
            source_ws=replacement_socket,
        )
        self.assertEqual(replacement_socket.sent, [{"type": "pong", "ts": 67890}])


if __name__ == "__main__":
    unittest.main()
