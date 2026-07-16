"""Regression tests for chat UI action-step completion on structured tool
results (e.g. Claude WebSearch).

Background: Claude's WebSearch tool returns a *structured* result block (a list
of ``web_search_result`` items with no plain ``text`` field). The chat agent's
``user``-event handler must still emit a ``tool_result`` event for it, otherwise
the UI action step for "Search" freezes on the "running" dot forever.

Run from the ``unchained/`` package directory:
    uv run python test_structured_tool_result.py
"""

import asyncio
import unittest

import nudge
import chat_agent_cli


class _CollectingEmit:
    def __init__(self):
        self.events = []

    async def __call__(self, event):
        self.events.append(event)


def _run(coro):
    return asyncio.run(coro)


class TestExtractToolResultText(unittest.TestCase):
    def test_plain_text_block(self):
        self.assertEqual(
            nudge._extract_tool_result_text([{"type": "text", "text": "hello"}]),
            "hello",
        )

    def test_string_content(self):
        self.assertEqual(nudge._extract_tool_result_text("raw result"), "raw result")

    def test_web_search_structured_summary(self):
        content = [
            {"type": "web_search_result", "url": "https://a", "title": "A"},
            {"type": "web_search_result", "url": "https://b", "title": "B"},
        ]
        text = nudge._extract_tool_result_text(content)
        self.assertIn("2 results", text)
        self.assertIn("A", text)
        self.assertIn("B", text)

    def test_empty_list_yields_empty(self):
        # An empty structured result is still a *completed* tool call; the empty
        # string signals the caller to emit a hidden "completed" placeholder
        # rather than suppressing the event.
        self.assertEqual(nudge._extract_tool_result_text([]), "")
        self.assertEqual(nudge._extract_tool_result_text(None), "")

    def test_non_string_label_is_safe(self):
        content = [{"type": "web_search_result", "title": 123, "url": None}]
        text = nudge._extract_tool_result_text(content)
        self.assertIn("123", text)


class TestEmitCliToolResult(unittest.TestCase):
    def _emit(self, text, **kw):
        emit = _CollectingEmit()
        _run(chat_agent_cli._emit_cli_tool_result(emit, text, **kw))
        return emit.events

    def test_web_search_result_emits_visible_summary(self):
        events = self._emit(
            "Found 2 results: A, B", visible=True
        )
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["type"], "tool_result")
        self.assertTrue(evt["visible"])
        self.assertIn("2 results", evt["data"])

    def test_empty_text_emits_hidden_completed(self):
        # The freeze bug: empty structured result previously suppressed the
        # event. Now it emits a hidden "completed" so the step resolves.
        events = self._emit("")
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["type"], "tool_result")
        self.assertEqual(evt["data"], "completed")
        self.assertFalse(evt["visible"])

    def test_error_block_emits_error(self):
        events = self._emit("", is_error=True)
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["type"], "tool_result")
        self.assertEqual(evt["data"], "tool failed")
        self.assertTrue(evt["visible"])


class TestUserEventPerBlockEmission(unittest.TestCase):
    """Mirror the chat_agent_cli `user` branch: one completion per result block."""

    def _run_user_event(self, event, pending_tool_calls=None):
        emit = _CollectingEmit()
        tool_result = event.get("tool_use_result", {})
        # Reuse the real per-block extraction + emit helper used by the handler.
        async def go():
            if isinstance(tool_result, str) and tool_result:
                await chat_agent_cli._emit_cli_tool_result(emit, tool_result)
            elif isinstance(tool_result, dict) and tool_result.get("stdout"):
                await chat_agent_cli._emit_cli_tool_result(emit, tool_result.get("stdout", ""))
            else:
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") != "tool_result":
                        continue
                    text = nudge._extract_tool_result_text(block.get("content", ""))
                    await chat_agent_cli._emit_cli_tool_result(
                        emit, text, is_error=bool(block.get("is_error")), visible=bool(text)
                    )

        _run(go())
        return emit.events

    def test_single_web_search_result_completes(self):
        events = self._run_user_event({
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {"type": "web_search_result", "url": "https://a", "title": "A"}
                        ],
                    }
                ]
            },
        })
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "tool_result")

    def test_parallel_results_each_complete(self):
        events = self._run_user_event({
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "r1"}]},
                    {
                        "type": "tool_result",
                        "content": [
                            {"type": "web_search_result", "url": "https://b", "title": "B"}
                        ],
                    },
                ]
            },
        })
        # Both blocks must resolve — not just the first (the original bug froze
        # the second step).
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["type"] == "tool_result" for e in events))

    def test_empty_structured_result_still_completes(self):
        events = self._run_user_event({
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": []}]},
        })
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"], "completed")
        self.assertFalse(events[0]["visible"])


if __name__ == "__main__":
    unittest.main()
