"""Regression tests for provision launches keeping the bridge loop responsive."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

import chrome_bridge as cb

_REAL_TIME_SLEEP = time.sleep


class _RecordingWebSocket:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


class _BoundSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def bind(self, _address):
        pass

    def getsockname(self):
        return ("127.0.0.1", 43123)


class _ReadyResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestProvisionLaunchEventLoop(unittest.IsolatedAsyncioTestCase):
    async def test_provision_http_request_does_not_hold_message_dispatch(self):
        agent = cb.Agent("ws://relay.invalid/tunnel")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_provision(_msg):
            started.set()
            await release.wait()

        agent._handle_http = slow_provision
        try:
            await asyncio.wait_for(
                agent._handle_message(
                    {"type": "http", "path": "/provision-launch?profile_path=/tmp/Profile"}
                ),
                timeout=0.05,
            )
        except asyncio.TimeoutError:
            release.set()
            self.fail("provision launch is awaited inline and blocks message dispatch")

        await asyncio.wait_for(started.wait(), timeout=0.05)
        tasks = list(agent._provision_tasks)
        release.set()
        if tasks:
            await asyncio.gather(*tasks)

    async def _launch(
        self,
        *,
        copy_profile,
        urlopen,
        sleep,
        probe=None,
        finished_event: threading.Event | None = None,
    ):
        with tempfile.TemporaryDirectory(prefix="provision-loop-test-") as root:
            profile_path = os.path.join(root, "Profile 5")
            os.mkdir(profile_path)
            data_dir = os.path.join(root, "agent-data")
            state_dir = os.path.join(root, "state")

            agent = cb.Agent("ws://relay.invalid/tunnel")
            agent.agent_id = "claude-test-agent"
            agent.ws = _RecordingWebSocket()

            with (
                patch.object(cb, "DATA_DIR", data_dir),
                patch.object(
                    cb,
                    "_prov_state_path",
                    side_effect=lambda slot: os.path.join(state_dir, f"{slot}.json"),
                ),
                patch.object(cb, "_write_prov_state"),
                patch.object(cb, "_copy_profile_light", side_effect=copy_profile),
                patch.object(cb, "_find_chrome_binary", return_value="/fake/chrome"),
                patch.object(
                    cb.subprocess,
                    "Popen",
                    return_value=MagicMock(pid=43123),
                ),
                patch.object(socket, "socket", return_value=_BoundSocket()),
                patch.object(cb.urllib.request, "urlopen", side_effect=urlopen),
                patch.object(cb, "_first_page_tab", return_value={"id": "tab-123"}),
                patch.object(cb, "_default_new_tab_url", return_value="https://relay.invalid/tab"),
                patch.object(cb.time, "sleep", side_effect=sleep),
            ):
                path = "/provision-launch?" + urllib.parse.urlencode(
                    {"profile_path": profile_path}
                )
                await agent._handle_provision_launch("req-test", path)
                if finished_event is not None:
                    finished_event.set()

            self.assertEqual(agent.ws.messages[-1]["type"], "http_response")
            self.assertEqual(agent.ws.messages[-1]["status"], 200)
            if probe is not None:
                await asyncio.sleep(0)
                self.assertEqual(probe, [True], "event loop did not run during the blocking stage")

    async def test_profile_copy_does_not_starve_the_bridge_loop(self):
        loop = asyncio.get_running_loop()
        stage_finished = threading.Event()
        callback_ran = threading.Event()
        probe: list[bool] = []

        def observe_from_loop():
            probe.append(not stage_finished.is_set())
            callback_ran.set()

        def slow_copy(*_args):
            loop.call_soon_threadsafe(observe_from_loop)
            # If the copy runs on the event-loop thread, this wait prevents the
            # queued callback from running until the copy has finished. If it
            # runs in a worker, the callback runs while the copy is still open.
            callback_ran.wait(timeout=0.25)
            stage_finished.set()

        await self._launch(
            copy_profile=slow_copy,
            urlopen=lambda *_args, **_kwargs: _ReadyResponse(),
            sleep=lambda _seconds: None,
            probe=probe,
        )

    async def test_chrome_readiness_request_does_not_starve_the_bridge_loop(self):
        loop = asyncio.get_running_loop()
        stage_finished = threading.Event()
        callback_ran = threading.Event()
        probe: list[bool] = []

        def observe_from_loop():
            probe.append(not stage_finished.is_set())
            callback_ran.set()

        def slow_urlopen(*_args, **_kwargs):
            loop.call_soon_threadsafe(observe_from_loop)
            callback_ran.wait(timeout=0.25)
            stage_finished.set()
            return _ReadyResponse()

        await self._launch(
            copy_profile=lambda *_args: None,
            urlopen=slow_urlopen,
            sleep=lambda _seconds: None,
            probe=probe,
        )

    async def test_readiness_poll_yields_instead_of_blocking_with_time_sleep(self):
        loop = asyncio.get_running_loop()
        handler_finished = threading.Event()
        probe: list[bool] = []
        loop.call_later(0.01, lambda: probe.append(not handler_finished.is_set()))

        await self._launch(
            copy_profile=lambda *_args: None,
            urlopen=lambda *_args, **_kwargs: _ReadyResponse(),
            # The production readiness loop asks for one second. Keep the
            # timing probe isolated to that wait; the current synchronous
            # sleep blocks the loop until the handler is complete.
            sleep=_REAL_TIME_SLEEP,
            finished_event=handler_finished,
        )
        await asyncio.sleep(0.02)
        self.assertEqual(probe, [True], "readiness wait blocked the bridge event loop")


if __name__ == "__main__":
    unittest.main()
