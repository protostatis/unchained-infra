"""Regression tests for bounded chat-agent WebSocket events."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, mock_open, patch

from aiohttp import ClientSession, web

import agent_package
import chat_agent_cli
import chat_agent_codex
import chat_agent_gemini
import chat_agent_openrouter
import chat_agent_sdk
from chat_event_transport import (
    CHAT_TEXT_REPLAY_TARGET_BYTES,
    CHAT_WS_MAX_MESSAGE_BYTES,
    EVENT_OMITTED_MESSAGE,
    MALFORMED_TEXT_EVENT_MESSAGE,
    MAX_AGENT_EVENT_BYTES,
    MAX_INLINE_SCREENSHOT_BASE64_BYTES,
    SCREENSHOT_OMITTED_MESSAGE,
    bound_agent_event,
    iter_replay_safe_text_chunks,
    overlay_event,
    read_inline_screenshot,
    serialize_agent_event,
)
from nudge import MAX_SAME_PAGE_LINK_SCANS, NudgeState
from web_app.handlers import chat_stream


class TestChatEventTransport(unittest.TestCase):
    def test_replay_safe_text_chunks_preserve_escaped_unicode_exactly(self):
        text = ("line \\\" \\ \u0001 cafe \u3053\u3093\u306b\u3061\u306f \U0001f916\n" * 700)
        chunks = list(
            iter_replay_safe_text_chunks(
                text,
                session_id="s-" + "x" * 180,
                req_id="r-" + "y" * 80,
            )
        )

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            event = {
                "type": "text",
                "data": chunk,
                "session_id": "s-" + "x" * 180,
                "req_id": "r-" + "y" * 80,
            }
            encoded = json.dumps(event, separators=(",", ":"), default=str)
            self.assertLessEqual(
                len(encoded.encode("utf-8")), CHAT_TEXT_REPLAY_TARGET_BYTES
            )

    def test_local_cli_emits_chunks_before_done(self):
        text = ("large final response \U0001f916\n" * 900)
        events = []

        async def emit(event):
            events.append(event)

        async def exercise():
            await chat_agent_cli._emit_replay_safe_text(
                emit, text, "s-local-chunks", "r-local-chunks"
            )
            await emit({"type": "done"})

        asyncio.run(exercise())

        self.assertGreater(len(events), 2)
        self.assertEqual([event["type"] for event in events][-1], "done")
        self.assertEqual(
            "".join(event["data"] for event in events[:-1]), text
        )

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

    def test_malformed_text_event_becomes_safe_user_message(self):
        event = {
            "type": "text",
            "data": None,
            "session_id": "s-test",
            "req_id": "r-test",
        }

        bounded, payload = serialize_agent_event(event)

        self.assertEqual(bounded["data"], MALFORMED_TEXT_EVENT_MESSAGE)
        self.assertTrue(bounded["malformed_text_event"])
        self.assertEqual(bounded["malformed_text_data_type"], "NoneType")
        self.assertEqual(json.loads(payload)["data"], MALFORMED_TEXT_EVENT_MESSAGE)

    def test_structured_text_event_uses_safe_text_contract(self):
        bounded = bound_agent_event(
            {"type": "text", "data": {"answer": "unexpected structure"}},
            encoded_size=MAX_AGENT_EVENT_BYTES + 1,
        )

        self.assertEqual(bounded["data"], MALFORMED_TEXT_EVENT_MESSAGE)
        self.assertEqual(bounded["malformed_text_data_type"], "dict")

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
        self.assertIn("replaced malformed text event", source)
        self.assertIn("event = overlay_event(event)", overlay_source)

    def test_every_agent_sender_uses_bounded_serialization(self):
        source = inspect.getsource(chat_agent_cli._make_emitter)
        self.assertIn("send_agent_event(target, evt)", source)
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

    def test_hosted_agents_correlate_turn_events_and_preserve_explicit_ids(self):
        agents = (
            (chat_agent_sdk, chat_agent_sdk.ChatAgent, "_send_event"),
            (chat_agent_gemini, chat_agent_gemini.GeminiChatAgent, "_send"),
            (chat_agent_codex, chat_agent_codex.CodexChatAgent, "_send"),
            (chat_agent_openrouter, chat_agent_openrouter.TrialAgent, "_send"),
        )

        async def check_sender(module, cls, method):
            agent = cls.__new__(cls)
            agent.ws = object()
            agent.active_req_ids = {"s-turn": "r-turn"}
            with patch.object(module, "send_agent_event", new_callable=AsyncMock) as sender:
                for event_type in (
                    "tool_start", "tool_result", "text", "done", "error",
                    "cancelled", "live_preview",
                ):
                    await getattr(agent, method)("s-turn", {"type": event_type})
                await getattr(agent, method)("s-turn", {
                    "type": "text", "req_id": "r-explicit",
                })

            events = [call.args[1] for call in sender.await_args_list]
            self.assertTrue(all(event["req_id"] == "r-turn" for event in events[:-1]))
            self.assertEqual(events[-1]["req_id"], "r-explicit")

        for module, cls, method in agents:
            with self.subTest(cls=cls.__name__):
                asyncio.run(check_sender(module, cls, method))

    def test_hosted_agents_do_not_clear_a_replacement_turn_correlation(self):
        for cls in (
            chat_agent_sdk.ChatAgent,
            chat_agent_gemini.GeminiChatAgent,
            chat_agent_codex.CodexChatAgent,
            chat_agent_openrouter.TrialAgent,
        ):
            with self.subTest(cls=cls.__name__):
                agent = cls.__new__(cls)
                old_task = object()
                replacement_task = object()
                agent.active_tasks = {"s-turn": replacement_task}
                agent.active_req_ids = {"s-turn": "r-new"}

                agent._finish_task("s-turn", "r-old", old_task)
                self.assertIs(agent.active_tasks["s-turn"], replacement_task)
                self.assertEqual(agent.active_req_ids["s-turn"], "r-new")

                agent._finish_task("s-turn", "r-new", replacement_task)
                self.assertNotIn("s-turn", agent.active_tasks)
                self.assertNotIn("s-turn", agent.active_req_ids)

    def test_hosted_agents_keep_old_task_request_id_after_replacement(self):
        agents = (
            (chat_agent_sdk, chat_agent_sdk.ChatAgent, "_send_event"),
            (chat_agent_gemini, chat_agent_gemini.GeminiChatAgent, "_send"),
            (chat_agent_codex, chat_agent_codex.CodexChatAgent, "_send"),
            (chat_agent_openrouter, chat_agent_openrouter.TrialAgent, "_send"),
        )

        async def check_old_task_context(module, cls, method):
            agent = cls.__new__(cls)
            agent.ws = object()
            agent.active_req_ids = {"s-turn": "r-old"}
            with patch.object(module, "send_agent_event", new_callable=AsyncMock) as sender:
                token = module._task_req_id.set("r-old")
                try:
                    old_task = asyncio.create_task(
                        getattr(agent, method)("s-turn", {"type": "error"})
                    )
                finally:
                    module._task_req_id.reset(token)

                agent.active_req_ids["s-turn"] = "r-new"
                await old_task

            self.assertEqual(sender.await_args.args[1]["req_id"], "r-old")

        for module, cls, method in agents:
            with self.subTest(cls=cls.__name__):
                asyncio.run(check_old_task_context(module, cls, method))

    def test_hosted_agents_correlate_cancellation_and_special_event_paths(self):
        for cls, send_method in (
            (chat_agent_sdk.ChatAgent, "_send_event"),
            (chat_agent_gemini.GeminiChatAgent, "_send"),
            (chat_agent_codex.CodexChatAgent, "_send"),
            (chat_agent_openrouter.TrialAgent, "_send"),
        ):
            with self.subTest(cls=cls.__name__):
                source = inspect.getsource(cls.run)
                self.assertIn('{"type": "cancelled", "req_id": req_id}', source)
                self.assertIn(f"self.{send_method}", inspect.getsource(cls._emit_intervention_event))
        self.assertIn(
            "await self._send(",
            inspect.getsource(chat_agent_openrouter.TrialAgent._emit_live_preview),
        )

    def test_packaged_agent_includes_transport_and_version_bump(self):
        self.assertEqual(agent_package.VERSION, "0.3.128")
        self.assertEqual(
            agent_package._PACKAGE_FILES["unchained/chat_event_transport.py"],
            "chat_event_transport.py",
        )
        pyproject = Path(__file__).with_name("pyproject.toml").read_text()
        self.assertIn('"chat_event_transport"', pyproject)


class TestWorkspaceHarnessGuardrails(unittest.TestCase):
    def test_broad_link_scans_are_limited_per_page_and_tab(self):
        state = NudgeState()
        expression = (
            "Array.from(document.querySelectorAll('a'))"
            ".filter(a => a.href).map(a => a.href)"
        )

        for _ in range(MAX_SAME_PAGE_LINK_SCANS):
            self.assertTrue(
                state.allow_broad_link_scan(
                    page_url="https://www.cnbc.com/markets/",
                    tab_id="tab-1",
                    expression=expression,
                )
            )
        self.assertFalse(
            state.allow_broad_link_scan(
                page_url="https://www.cnbc.com/markets",
                tab_id="tab-1",
                expression=expression,
            )
        )
        self.assertTrue(
            state.allow_broad_link_scan(
                page_url="https://www.cnbc.com/search/?query=market",
                tab_id="tab-1",
                expression=expression,
            )
        )
        self.assertTrue(
            state.allow_broad_link_scan(
                page_url="https://www.cnbc.com/markets/",
                tab_id="tab-1",
                expression="document.querySelector('.headline').textContent",
            )
        )
        self.assertTrue(
            state.allow_broad_link_scan(
                page_url="https://www.cnbc.com/markets/",
                tab_id="tab-2",
                expression=expression,
            )
        )

    def test_all_anchor_scan_variants_are_limited(self):
        expressions = (
            "document.querySelectorAll('a[href]').map(a => a.href)",
            "Array.from(document.links, link => link.href)",
            "[...document.getElementsByTagName('a')].filter(a => a.href)",
            "for (const link of document.querySelectorAll('a')) { urls.push(link.href) }",
        )

        for expression in expressions:
            with self.subTest(expression=expression):
                state = NudgeState()
                for _ in range(MAX_SAME_PAGE_LINK_SCANS):
                    self.assertTrue(
                        state.allow_broad_link_scan(
                            page_url="https://example.test/markets",
                            tab_id="tab-1",
                            expression=expression,
                        )
                    )
                self.assertFalse(
                    state.allow_broad_link_scan(
                        page_url="https://example.test/markets",
                        tab_id="tab-1",
                        expression=expression,
                    )
                )

    def test_targeted_anchor_selector_is_not_treated_as_full_page_scan(self):
        state = NudgeState()
        expression = "Array.from(document.querySelectorAll('a[href*=\"/article/\"]'))"

        for _ in range(MAX_SAME_PAGE_LINK_SCANS + 1):
            self.assertTrue(
                state.allow_broad_link_scan(
                    page_url="https://example.test/markets",
                    tab_id="tab-1",
                    expression=expression,
                )
            )

    def test_revisited_navigation_does_not_count_as_new_progress(self):
        state = NudgeState()
        self.assertTrue(state.record_navigation("https://www.CNBC.com/markets/"))
        self.assertFalse(state.record_navigation("https://www.cnbc.com/markets"))
        self.assertTrue(state.record_navigation("https://www.cnbc.com/markets/bonds/"))

    def test_page_url_tracking_never_falls_back_to_another_tab(self):
        state = NudgeState()
        state.observe_page("https://example.test/one", tab_id="tab-one")

        self.assertEqual(state.page_url_for_tab("tab-one"), "https://example.test/one")
        self.assertEqual(state.page_url_for_tab("tab-two"), "")
        self.assertEqual(state.page_url_for_tab("auto"), "")

    def test_not_found_navigation_is_explicitly_detected(self):
        self.assertTrue(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/missing\nTitle: Not Found"
            )
        )
        self.assertFalse(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/article\nTitle: Article"
            )
        )

    def test_not_found_detection_uses_navigation_metadata_not_page_text(self):
        self.assertTrue(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/missing\nTitle: CNBC Page Not Found"
            )
        )
        self.assertTrue(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/missing\nTitle: 404 Error"
            )
        )
        self.assertTrue(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/missing\nTitle: Error\nHTTP Status: 404"
            )
        )
        self.assertFalse(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/article\n"
                "Title: Page Not Found: An Archaeology of Error Pages"
            )
        )
        self.assertFalse(
            chat_agent_openrouter._navigation_result_is_not_found(
                "Navigated to: https://example.test/article\nTitle: Article\n\n"
                "=== Page Layout ===\nArticle body mentions 404 not found in its source text"
            )
        )

    def test_page_url_parser_uses_confirmed_navigation_and_click_urls(self):
        self.assertEqual(
            chat_agent_openrouter._page_url_from_tool_result(
                "Navigated to: https://example.test/redirected\nTitle: Landing"
            ),
            "https://example.test/redirected",
        )
        self.assertEqual(
            chat_agent_openrouter._page_url_from_tool_result(
                "Clicked A\n--- changed ---\nurl: https://example.test/article"
            ),
            "https://example.test/article",
        )
        self.assertEqual(
            chat_agent_openrouter._page_url_from_tool_result(
                "Clicked A\n--- changed ---\nurl: https://example.test/challenge\n"
                "[challenge cleared] Now on: https://example.test/article\n\n"
                "=== Page Layout ===\nURL: https://example.test/article"
            ),
            "https://example.test/article",
        )
        self.assertEqual(
            chat_agent_openrouter._page_url_from_tool_result(
                "Navigated to: https://example.test/article\nTitle: Article\n\n"
                "=== Page Layout ===\nURL: https://example.test/untrusted-page-text"
            ),
            "https://example.test/article",
        )

    def test_navigation_progress_requires_nonempty_nonerror_page_state(self):
        self.assertTrue(
            chat_agent_openrouter._navigation_result_succeeded(
                "Navigated to: https://example.test/article\nTitle: Article"
            )
        )
        self.assertFalse(chat_agent_openrouter._navigation_result_succeeded(""))
        self.assertFalse(
            chat_agent_openrouter._navigation_result_succeeded(
                "BROWSER_UNAVAILABLE: Chrome connector is not running."
            )
        )

    def test_hosted_templates_never_coerce_missing_text_to_undefined(self):
        from web_app import templates

        for html in (
            templates.TRIAL_CHAT_HTML,
            templates.CLAUDE_CHAT_HTML,
            templates.CHAT_CLAUDE_SDK_HTML,
            templates.CHAT_CODEX_HTML,
        ):
            with self.subTest(template=html[:80]):
                self.assertIn(
                    "const safeText = typeof text === 'string' ? text : '';",
                    html,
                )
                self.assertIn(
                    "appendText(bubble, typeof evt.data === 'string' ? evt.data : '');",
                    html,
                )


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
