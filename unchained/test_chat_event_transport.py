"""Regression tests for bounded chat-agent WebSocket events."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from aiohttp import ClientSession, web

import agent_package
import chat_agent_cli
import chat_agent_codex
import chat_agent_gemini
import chat_agent_openrouter
import chat_agent_sdk
from chat_event_transport import (
    CHAT_WS_MAX_MESSAGE_BYTES,
    EVENT_OMITTED_MESSAGE,
    MAX_AGENT_EVENT_BYTES,
    MAX_INLINE_SCREENSHOT_BASE64_BYTES,
    SCREENSHOT_OMITTED_MESSAGE,
    bound_agent_event,
    overlay_event,
    read_inline_screenshot,
    serialize_agent_event,
)
from web_app.handlers import chat_stream


class TestChatEventTransport(unittest.TestCase):
    def test_inline_screenshot_within_limit_is_preserved(self):
        data = "iVBOR" + "A" * 1024
        event = {"type": "tool_result", "data": data, "is_screenshot": True}

        bounded, payload = serialize_agent_event(event)

        self.assertEqual(bounded, event)
        self.assertEqual(json.loads(payload), event)

    def test_oversized_screenshot_becomes_visible_omission(self):
        data = "iVBOR" + "A" * MAX_INLINE_SCREENSHOT_BASE64_BYTES
        event = {
            "type": "tool_result",
            "name": "screenshot",
            "data": data,
            "is_screenshot": True,
            "session_id": "s-test",
            "req_id": "r-test",
        }

        bounded, payload = serialize_agent_event(event)

        self.assertEqual(bounded["type"], "tool_result")
        self.assertEqual(bounded["data"], SCREENSHOT_OMITTED_MESSAGE)
        self.assertFalse(bounded["is_screenshot"])
        self.assertTrue(bounded["screenshot_omitted"])
        self.assertTrue(bounded["visible"])
        self.assertEqual(bounded["session_id"], "s-test")
        self.assertLess(len(payload.encode("utf-8")), 1024)

    def test_oversized_non_image_event_becomes_bounded_error(self):
        event = {
            "type": "text",
            "data": "x" * (MAX_AGENT_EVENT_BYTES + 1),
            "session_id": "s-test",
            "req_id": "r-test",
        }

        bounded = bound_agent_event(event)

        self.assertEqual(bounded["type"], "text")
        self.assertEqual(bounded["data"], EVENT_OMITTED_MESSAGE)
        self.assertEqual(bounded["session_id"], "s-test")
        self.assertEqual(bounded["req_id"], "r-test")

    def test_oversized_file_is_rejected_before_open(self):
        with patch("chat_event_transport.os.path.getsize", return_value=MAX_INLINE_SCREENSHOT_BASE64_BYTES + 1), \
             patch("builtins.open", mock_open()) as mocked_open:
            data, oversized = read_inline_screenshot("/tmp/large.b64")

        self.assertIsNone(data)
        self.assertTrue(oversized)
        mocked_open.assert_not_called()

    def test_overlay_event_never_copies_screenshot_data(self):
        original = {"type": "tool_result", "data": "iVBOR-secret", "is_screenshot": True}

        stripped = overlay_event(original)

        self.assertEqual(stripped["data"], "")
        self.assertTrue(stripped["screenshot_data_omitted"])
        self.assertEqual(original["data"], "iVBOR-secret")

    def test_live_preview_is_bounded_and_never_reaches_overlay(self):
        preview = {
            "type": "live_preview",
            "data": "A" * (MAX_INLINE_SCREENSHOT_BASE64_BYTES + 1),
            "session_id": "s-test",
        }
        bounded = bound_agent_event(preview)
        self.assertEqual(bounded["type"], "live_preview_omitted")
        self.assertEqual(bounded["data"], "")
        self.assertTrue(bounded["screenshot_omitted"])

        normal = overlay_event({"type": "live_preview", "data": "image-bytes"})
        self.assertEqual(normal["data"], "")
        self.assertTrue(normal["screenshot_data_omitted"])

    def test_oversized_request_response_keeps_type_and_req_id(self):
        bounded = bound_agent_event({
            "type": "history_response",
            "req_id": "r-test",
            "messages": ["x" * (MAX_AGENT_EVENT_BYTES + 1)],
        })
        self.assertEqual(bounded["type"], "history_response")
        self.assertEqual(bounded["req_id"], "r-test")
        self.assertEqual(bounded["error"], EVENT_OMITTED_MESSAGE)
        self.assertTrue(bounded["event_omitted"])

    def test_server_uses_bounded_receive_and_sanitization(self):
        source = inspect.getsource(chat_stream.handle_chat_ws)
        overlay_source = inspect.getsource(chat_stream._broadcast_overlay)
        self.assertEqual(CHAT_WS_MAX_MESSAGE_BYTES, 16 * 1024 * 1024)
        self.assertIn("max_msg_size=CHAT_WS_MAX_MESSAGE_BYTES", source)
        self.assertIn("data = bound_agent_event(data, encoded_size=", source)
        self.assertIn("event = overlay_event(event)", overlay_source)

    def test_every_agent_sender_uses_bounded_serialization(self):
        self.assertIn("send_agent_event(ws, evt)", inspect.getsource(chat_agent_cli._make_emitter))
        for cls, method in (
            (chat_agent_sdk.ChatAgent, "_send_event"),
            (chat_agent_gemini.GeminiChatAgent, "_send"),
            (chat_agent_codex.CodexChatAgent, "_send"),
            (chat_agent_openrouter.TrialAgent, "_send"),
        ):
            with self.subTest(cls=cls.__name__):
                self.assertIn("send_agent_event(self.ws, event)", inspect.getsource(getattr(cls, method)))
        self.assertEqual(inspect.getsource(chat_agent_sdk).count("await self.ws.send(json.dumps("), 1)
        self.assertEqual(inspect.getsource(chat_agent_gemini).count("await self.ws.send(json.dumps("), 1)
        self.assertEqual(inspect.getsource(chat_agent_codex).count("await self.ws.send(json.dumps("), 1)
        self.assertEqual(inspect.getsource(chat_agent_openrouter).count("await self.ws.send(json.dumps("), 1)

    def test_packaged_agent_includes_transport_and_version_bump(self):
        self.assertEqual(agent_package.VERSION, "0.3.112")
        self.assertEqual(
            agent_package._PACKAGE_FILES["unchained/chat_event_transport.py"],
            "chat_event_transport.py",
        )
        pyproject = Path(__file__).with_name("pyproject.toml").read_text()
        self.assertIn('"chat_event_transport"', pyproject)


class TestLargeChatWebSocket(unittest.IsolatedAsyncioTestCase):
    async def test_server_accepts_event_above_old_four_mib_limit(self):
        queue: asyncio.Queue = asyncio.Queue()

        async def close_session_tab(_sid):
            return None

        core = SimpleNamespace(
            TRIAL_AGENT_KEY="trial-key",
            TRIAL_AGENT_ID="trial-agent",
            _chat_agents={},
            _chat_agent_caps={},
            _chat_agent_users={},
            _agent_req_queues={},
            _response_queues={"s-large": queue},
            _response_req_ids={"s-large": "r-large"},
            _session_agent_map={},
            _close_session_tab=close_session_tab,
        )
        app = web.Application()
        app.router.add_get("/chat/ws", chat_stream.handle_chat_ws)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with patch.object(chat_stream, "_core", return_value=core):
                async with ClientSession() as session:
                    ws = await session.ws_connect(f"http://127.0.0.1:{port}/chat/ws")
                    await ws.send_json({"key": "trial-key"})
                    auth = await ws.receive_json()
                    self.assertEqual(auth["type"], "auth_ok")
                    event = {
                        "type": "text",
                        "data": "x" * (5 * 1024 * 1024),
                        "session_id": "s-large",
                        "req_id": "r-large",
                    }
                    await ws.send_str(json.dumps(event, separators=(",", ":")))
                    received = await asyncio.wait_for(queue.get(), timeout=3)
                    self.assertEqual(len(received["data"]), 5 * 1024 * 1024)
                    self.assertFalse(ws.closed)
                    await ws.close()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
