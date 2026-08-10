"""Regression tests for hosted Unbrowser MCP session isolation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.routing import Route

from unbrowser_mcp_router import (
    MCPHTTPRouter,
    RouterConfig,
    WorkerPool,
    WorkerStartupError,
)


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _AsyncBytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):
        yield self.content

    async def aclose(self) -> None:
        return None


def _json_response(
    payload: dict[str, object], *, headers: dict[str, str] | None = None
) -> httpx.Response:
    response_headers = {"content-type": "application/json"}
    if headers:
        response_headers.update(headers)
    return httpx.Response(
        200,
        headers=response_headers,
        stream=_AsyncBytesStream(json.dumps(payload).encode("utf-8")),
    )


class UnbrowserMCPRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = 0.0
        self.launches: list[tuple[int, Path, dict[str, str]]] = []
        self.terminated: list[_FakeProcess] = []
        self.forwarded: list[dict[str, str | int]] = []
        self._next_inner_session = 0

        async def launch(port: int, state_dir: Path, environment: dict[str, str]) -> _FakeProcess:
            process = _FakeProcess(pid=10_000 + port)
            self.launches.append((port, state_dir, environment))
            return process

        async def ready(_process: _FakeProcess, _port: int) -> None:
            return None

        async def terminate(process: _FakeProcess) -> None:
            process.returncode = 0
            self.terminated.append(process)

        def upstream_handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content) if request.content else {}
            method = str(payload.get("method", ""))
            session_id = request.headers.get("mcp-session-id", "")
            self.forwarded.append(
                {
                    "method": method,
                    "session_id": session_id,
                    "port": request.url.port or 0,
                }
            )
            if method == "initialize":
                self._next_inner_session += 1
                inner_session = f"inner-session-{self._next_inner_session}"
                return _json_response(
                    {"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}},
                    headers={"mcp-session-id": inner_session},
                )
            return _json_response({"jsonrpc": "2.0", "id": payload.get("id"), "result": {"ok": True}})

        self.upstream_client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream_handler),
            base_url="http://upstream",
        )
        config = RouterConfig(
            max_sessions=2,
            idle_timeout_seconds=120.0,
            max_session_seconds=900.0,
            startup_timeout_seconds=1.0,
            worker_port_start=21_000,
            session_root=Path(self.tempdir.name),
        )
        self.pool = WorkerPool(
            config,
            self.upstream_client,
            clock=lambda: self.now,
            launcher=launch,
            readiness_probe=ready,
            terminator=terminate,
        )
        self.router = MCPHTTPRouter(self.pool, self.upstream_client)
        self.app = Starlette(
            routes=[
                Route("/mcp", self.router.handle_mcp, methods=["GET", "POST", "DELETE", "OPTIONS"]),
                Route("/status", self.router.handle_status, methods=["GET"]),
            ]
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://router",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.pool.close()
        await self.upstream_client.aclose()
        self.tempdir.cleanup()

    async def _initialize(self) -> str:
        response = await self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
            headers={"accept": "application/json"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        session_id = response.headers.get("mcp-session-id")
        self.assertTrue(session_id)
        self.assertNotIn("inner-session-", session_id)
        return session_id

    async def test_each_mcp_session_gets_a_distinct_worker_and_upstream_state(self) -> None:
        first_session = await self._initialize()
        second_session = await self._initialize()
        self.assertNotEqual(first_session, second_session)
        self.assertEqual(len(self.launches), 2)
        self.assertNotEqual(self.launches[0][0], self.launches[1][0])
        self.assertNotEqual(self.launches[0][1], self.launches[1][1])
        self.assertEqual(self.launches[0][2]["HOME"], str(self.launches[0][1]))
        self.assertEqual(self.launches[1][2]["HOME"], str(self.launches[1][1]))

        for session_id in (first_session, second_session):
            response = await self.client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "text", "arguments": {}},
                },
                headers={"mcp-session-id": session_id},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers.get("mcp-session-id"), session_id)

        tool_calls = [call for call in self.forwarded if call["method"] == "tools/call"]
        self.assertEqual(
            [call["session_id"] for call in tool_calls],
            ["inner-session-1", "inner-session-2"],
        )
        self.assertNotEqual(tool_calls[0]["port"], tool_calls[1]["port"])

    async def test_delete_terminates_only_the_callers_worker(self) -> None:
        first_session = await self._initialize()
        second_session = await self._initialize()

        response = await self.client.delete("/mcp", headers={"mcp-session-id": first_session})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((await self.pool.snapshot())["active_sessions"], 1)
        self.assertEqual(len(self.terminated), 1)

        expired = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            headers={"mcp-session-id": first_session},
        )
        self.assertEqual(expired.status_code, 404)

        still_live = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
            headers={"mcp-session-id": second_session},
        )
        self.assertEqual(still_live.status_code, 200, still_live.text)

    async def test_idle_worker_self_closes_after_two_minutes_without_inflight_work(self) -> None:
        session_id = await self._initialize()
        self.now = 119.9
        self.assertEqual(await self.pool.reap_expired(), 0)
        self.assertEqual((await self.pool.snapshot())["active_sessions"], 1)

        self.now = 120.0
        self.assertEqual(await self.pool.reap_expired(), 1)
        self.assertEqual((await self.pool.snapshot())["active_sessions"], 0)
        self.assertEqual(len(self.terminated), 1)

        expired = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
            headers={"mcp-session-id": session_id},
        )
        self.assertEqual(expired.status_code, 404)

    async def test_idle_reaper_does_not_interrupt_an_inflight_request(self) -> None:
        worker = await self.pool.start_worker()
        await self.pool.begin_unbound_request(worker)
        await self.pool.bind_session(worker, "inner-active")

        self.now = 121.0
        self.assertEqual(await self.pool.reap_expired(), 0)
        self.assertEqual((await self.pool.snapshot())["active_sessions"], 1)
        self.assertEqual(self.terminated, [])

        await self.pool.finish(worker)
        self.now = 241.0
        self.assertEqual(await self.pool.reap_expired(), 1)
        self.assertEqual(len(self.terminated), 1)

    async def test_max_lifetime_closes_a_recently_active_session(self) -> None:
        session_id = await self._initialize()
        self.now = 899.0
        worker = await self.pool.acquire(session_id)
        self.assertIsNotNone(worker)
        await self.pool.finish(worker)

        self.now = 900.0
        self.assertEqual(await self.pool.reap_expired(), 1)
        self.assertEqual((await self.pool.snapshot())["active_sessions"], 0)
        self.assertEqual(len(self.terminated), 1)

    async def test_session_capacity_rejects_an_extra_initialize_without_spawning_a_worker(self) -> None:
        await self._initialize()
        await self._initialize()

        rejected = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        )
        self.assertEqual(rejected.status_code, 429)
        self.assertEqual(len(self.launches), 2)

    async def test_startup_failure_cleans_up_the_reserved_worker_and_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            terminated: list[_FakeProcess] = []

            async def launch(_port: int, _state_dir: Path, _environment: dict[str, str]) -> _FakeProcess:
                return _FakeProcess(pid=42)

            async def fail_ready(_process: _FakeProcess, _port: int) -> None:
                raise WorkerStartupError("not ready")

            async def terminate(process: _FakeProcess) -> None:
                process.returncode = 0
                terminated.append(process)

            failing_pool = WorkerPool(
                RouterConfig(max_sessions=1, worker_port_start=22_000, session_root=Path(root)),
                self.upstream_client,
                launcher=launch,
                readiness_probe=fail_ready,
                terminator=terminate,
            )
            with self.assertRaises(WorkerStartupError):
                await failing_pool.start_worker()
            self.assertEqual(len(terminated), 1)
            self.assertEqual(list(Path(root).iterdir()), [])
            await failing_pool.close()

    async def test_uninitialized_calls_do_not_create_workers(self) -> None:
        response = await self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.launches, [])


if __name__ == "__main__":
    unittest.main()
