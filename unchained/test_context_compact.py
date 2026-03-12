"""Tests for context_compact.py — deterministic browser-agent context compaction."""

import unittest

from context_compact import (
    _build_tool_name_index,
    _classify_tool,
    _compact_tool_result,
    _find_turn_boundary,
    _strip_page_layout,
    compact_messages,
    emergency_trim,
    estimate_tokens,
)


# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------


class TestClassifyTool(unittest.TestCase):
    def test_page_state_tools(self):
        for name in ("ddm", "navigate", "cdp_navigate", "click", "cdp_click",
                      "screenshot", "cdp_screenshot", "intel_probe", "intel_extract"):
            self.assertEqual(_classify_tool(name), "page_state", name)

    def test_ephemeral_tools(self):
        for name in ("cdp_type", "type_text", "cdp_set_file", "submit_form", "press_enter"):
            self.assertEqual(_classify_tool(name), "ephemeral", name)

    def test_data_tools(self):
        for name in ("js_eval", "intel_stores", "intel_shape", "intel_find_paths"):
            self.assertEqual(_classify_tool(name), "data", name)

    def test_meta_tools(self):
        for name in ("list_connected_agents", "cdp_provision_launch",
                      "cdp_provision_cleanup", "list_provisioned_tabs"):
            self.assertEqual(_classify_tool(name), "meta", name)

    def test_unknown(self):
        self.assertEqual(_classify_tool("some_random_tool"), "unknown")
        self.assertEqual(_classify_tool(""), "unknown")

    def test_mcp_prefix_stripped(self):
        self.assertEqual(_classify_tool("mcp__unchainedsky__ddm"), "page_state")
        self.assertEqual(_classify_tool("mcp__unchainedsky__cdp_click"), "page_state")
        self.assertEqual(_classify_tool("mcp__unchainedsky__js_eval"), "data")


# ---------------------------------------------------------------------------
# Page-layout stripping
# ---------------------------------------------------------------------------


class TestStripPageLayout(unittest.TestCase):
    def test_strips_layout_section(self):
        text = (
            "Navigated to: https://example.com\n"
            "Title: Example\n"
            "=== Page Layout ===\n"
            "lots of DOM density data here\n"
            "row1 | col1 | col2\n"
        )
        result = _strip_page_layout(text)
        self.assertIn("Navigated to:", result)
        self.assertNotIn("=== Page Layout ===", result)
        self.assertNotIn("lots of DOM density", result)

    def test_no_layout_section(self):
        text = "Navigated to: https://example.com\nTitle: Example"
        self.assertEqual(_strip_page_layout(text), text)

    def test_empty_after_strip(self):
        text = "=== Page Layout ===\nonly layout data"
        result = _strip_page_layout(text)
        # Should fall back to original if stripping yields empty
        self.assertTrue(len(result) > 0)


# ---------------------------------------------------------------------------
# Per-tool compaction
# ---------------------------------------------------------------------------


class TestCompactToolResult(unittest.TestCase):
    def test_screenshot_compacted(self):
        result = _compact_tool_result("screenshot", "base64data...", "page_state")
        self.assertEqual(result, "[screenshot — compacted]")

    def test_cdp_screenshot_compacted(self):
        result = _compact_tool_result("cdp_screenshot", "data", "page_state")
        self.assertEqual(result, "[screenshot — compacted]")

    def test_ddm_compacted(self):
        result = _compact_tool_result("ddm", "big layout map", "page_state")
        self.assertEqual(result, "[ddm — compacted]")

    def test_navigate_strips_layout(self):
        content = (
            "Navigated to: https://example.com\n"
            "=== Page Layout ===\nlayout data"
        )
        result = _compact_tool_result("navigate", content, "page_state")
        self.assertIn("Navigated to:", result)
        self.assertNotIn("=== Page Layout ===", result)

    def test_ephemeral_truncation(self):
        content = "a" * 200
        result = _compact_tool_result("cdp_type", content, "ephemeral")
        self.assertLessEqual(len(result), 200)
        self.assertIn("[truncated from 200 chars]", result)

    def test_ephemeral_short_kept(self):
        content = "typed hello"
        result = _compact_tool_result("cdp_type", content, "ephemeral")
        self.assertEqual(result, content)

    def test_data_truncation(self):
        content = "x" * 1000
        result = _compact_tool_result("js_eval", content, "data", max_data_chars=300)
        self.assertTrue(result.startswith("x" * 300))
        self.assertIn("[truncated from 1000 chars]", result)

    def test_data_short_kept(self):
        content = "small result"
        result = _compact_tool_result("js_eval", content, "data")
        self.assertEqual(result, content)

    def test_meta_unchanged(self):
        content = "agent-abc123\nagent-def456"
        result = _compact_tool_result("list_connected_agents", content, "meta")
        self.assertEqual(result, content)

    def test_unknown_truncated(self):
        content = "z" * 500
        result = _compact_tool_result("mystery_tool", content, "unknown", max_data_chars=300)
        self.assertIn("[truncated from 500 chars]", result)

    def test_empty_content(self):
        self.assertEqual(_compact_tool_result("ddm", "", "page_state"), "")
        self.assertEqual(_compact_tool_result("ddm", None, "page_state"), "")

    def test_mcp_prefixed_screenshot(self):
        result = _compact_tool_result(
            "mcp__unchainedsky__cdp_screenshot", "data", "page_state"
        )
        self.assertEqual(result, "[screenshot — compacted]")


# ---------------------------------------------------------------------------
# Tool name index building
# ---------------------------------------------------------------------------


class TestBuildToolNameIndex(unittest.TestCase):
    def test_anthropic_dict_format(self):
        messages = [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "ddm",
                        "input": {"flags": "--llm-2pass"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "layout"}
                ],
            },
        ]
        index = _build_tool_name_index(messages, "anthropic")
        self.assertEqual(index, {"tu_1": "ddm"})

    def test_anthropic_sdk_object(self):
        """Simulate an SDK ContentBlock object with attributes."""
        class FakeToolUse:
            type = "tool_use"
            id = "tu_sdk"
            name = "screenshot"
            input = {}

        messages = [
            {"role": "assistant", "content": [FakeToolUse()]},
        ]
        index = _build_tool_name_index(messages, "anthropic")
        self.assertEqual(index, {"tu_sdk": "screenshot"})

    def test_openai_format(self):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "navigate", "arguments": '{"url":"https://x.com"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Navigated to..."},
        ]
        index = _build_tool_name_index(messages, "openai")
        self.assertEqual(index, {"call_1": "navigate"})


# ---------------------------------------------------------------------------
# Turn boundary
# ---------------------------------------------------------------------------


class TestFindTurnBoundary(unittest.TestCase):
    def test_short_conversation_no_compaction(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "done"},
        ]
        boundary = _find_turn_boundary(messages, "anthropic", keep_recent=6)
        self.assertEqual(boundary, len(messages))

    def test_openai_boundary(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            messages.append({"role": "user", "content": f"msg {i}"})
            messages.append({"role": "assistant", "content": f"resp {i}"})
        boundary = _find_turn_boundary(messages, "openai", keep_recent=3)
        # Should be at the 3rd-from-last user message
        self.assertGreater(boundary, 0)
        self.assertLess(boundary, len(messages))


# ---------------------------------------------------------------------------
# Anthropic-format compaction (integration)
# ---------------------------------------------------------------------------


class TestCompactMessagesAnthropic(unittest.TestCase):
    def _make_conversation(self, n_turns=10):
        """Build a synthetic Anthropic conversation with DDM + screenshot results."""
        messages = []
        for i in range(n_turns):
            messages.append({"role": "user", "content": f"Turn {i} instruction"})
            messages.append({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": f"Let me check turn {i}"},
                    {"type": "tool_use", "id": f"tu_{i}", "name": "ddm", "input": {}},
                ],
            })
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"tu_{i}",
                        "content": f"=== Page Layout ===\nbig layout data turn {i}\nrow | col",
                    }
                ],
            })
        return messages

    def test_old_ddm_compacted(self):
        messages = self._make_conversation(10)
        messages, stats = compact_messages(messages, fmt="anthropic", keep_recent=3)
        self.assertGreater(stats["compacted"], 0)

        # Old tool results should be compacted
        first_tool_result = messages[2]["content"][0]
        self.assertEqual(first_tool_result["content"], "[ddm — compacted]")

    def test_recent_ddm_preserved(self):
        messages = self._make_conversation(10)
        messages, stats = compact_messages(messages, fmt="anthropic", keep_recent=3)

        # Last few tool results should be untouched
        last_tool_result = messages[-1]["content"][0]
        self.assertIn("=== Page Layout ===", last_tool_result["content"])

    def test_screenshot_compacted(self):
        messages = [
            {"role": "user", "content": "Take screenshot"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_ss", "name": "screenshot", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_ss", "content": "base64pngdata..." * 100},
                ],
            },
        ]
        # Add enough recent turns to push screenshot into old zone
        for i in range(8):
            messages.append({"role": "user", "content": f"Recent {i}"})
            messages.append({"role": "assistant", "content": f"Got it {i}"})

        messages, stats = compact_messages(messages, fmt="anthropic", keep_recent=6)
        ss_result = messages[2]["content"][0]
        self.assertEqual(ss_result["content"], "[screenshot — compacted]")


# ---------------------------------------------------------------------------
# OpenAI-format compaction (integration)
# ---------------------------------------------------------------------------


class TestCompactMessagesOpenAI(unittest.TestCase):
    def _make_conversation(self, n_turns=10):
        """Build a synthetic OpenAI conversation with navigate + DDM results."""
        messages = [{"role": "system", "content": "You are a browser agent."}]
        for i in range(n_turns):
            messages.append({"role": "user", "content": f"Turn {i}"})
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "navigate", "arguments": f'{{"url":"https://example.com/{i}"}}'},
                    }
                ],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"Navigated to: https://example.com/{i}\n=== Page Layout ===\nDOM data turn {i}",
            })
        return messages

    def test_old_navigate_compacted(self):
        messages = self._make_conversation(10)
        messages, stats = compact_messages(messages, fmt="openai", keep_recent=3)
        self.assertGreater(stats["compacted"], 0)

        # Old tool result: layout stripped
        first_tool = messages[3]  # system + user + assistant + tool
        self.assertNotIn("=== Page Layout ===", first_tool["content"])
        self.assertIn("Navigated to:", first_tool["content"])

    def test_recent_navigate_preserved(self):
        messages = self._make_conversation(10)
        messages, stats = compact_messages(messages, fmt="openai", keep_recent=3)

        # Last tool result should still have layout
        last_tool = messages[-1]
        self.assertIn("=== Page Layout ===", last_tool["content"])


# ---------------------------------------------------------------------------
# Data tool truncation
# ---------------------------------------------------------------------------


class TestDataToolTruncation(unittest.TestCase):
    def test_js_eval_truncated(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "user", "content": "Run JS"})
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_js", "type": "function",
                 "function": {"name": "js_eval", "arguments": '{"expression":"document.body.innerHTML"}'}},
            ],
        })
        messages.append({
            "role": "tool", "tool_call_id": "call_js",
            "content": "x" * 5000,
        })
        # Add recent turns
        for i in range(8):
            messages.append({"role": "user", "content": f"Recent {i}"})
            messages.append({"role": "assistant", "content": f"OK {i}"})

        messages, stats = compact_messages(messages, fmt="openai", keep_recent=6)
        js_result = messages[3]
        self.assertIn("[truncated from 5000 chars]", js_result["content"])
        self.assertLessEqual(len(js_result["content"]), 400)


# ---------------------------------------------------------------------------
# Meta tool preservation
# ---------------------------------------------------------------------------


class TestMetaToolPreservation(unittest.TestCase):
    def test_meta_tool_unchanged(self):
        messages = [{"role": "system", "content": "sys"}]
        messages.append({"role": "user", "content": "List agents"})
        messages.append({
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "call_la", "type": "function",
                 "function": {"name": "list_connected_agents", "arguments": "{}"}},
            ],
        })
        original_content = "agent-1: chrome-profile\nagent-2: firefox"
        messages.append({
            "role": "tool", "tool_call_id": "call_la",
            "content": original_content,
        })
        for i in range(8):
            messages.append({"role": "user", "content": f"Recent {i}"})
            messages.append({"role": "assistant", "content": f"OK {i}"})

        messages, stats = compact_messages(messages, fmt="openai", keep_recent=6)
        meta_result = messages[3]
        self.assertEqual(meta_result["content"], original_content)


# ---------------------------------------------------------------------------
# Emergency trim
# ---------------------------------------------------------------------------


class TestEmergencyTrim(unittest.TestCase):
    def test_openai_trim(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(20):
            messages.append({"role": "user", "content": f"msg {i}"})
            messages.append({"role": "assistant", "content": f"resp {i}"})
        original_len = len(messages)
        trimmed = emergency_trim(messages, fmt="openai", keep_tail=10)
        self.assertLessEqual(len(trimmed), 12)  # head + ~10 tail
        self.assertEqual(trimmed[0]["role"], "system")
        # Original unchanged
        self.assertEqual(len(messages), original_len)

    def test_anthropic_trim(self):
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"msg {i}"})
            messages.append({"role": "assistant", "content": f"resp {i}"})
        trimmed = emergency_trim(messages, fmt="anthropic", keep_tail=10)
        self.assertLessEqual(len(trimmed), 12)

    def test_no_orphaned_tool_results_openai(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(5):
            messages.append({"role": "user", "content": f"msg {i}"})
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"c_{i}", "type": "function",
                                "function": {"name": "ddm", "arguments": "{}"}}],
            })
            messages.append({"role": "tool", "tool_call_id": f"c_{i}", "content": "layout"})

        trimmed = emergency_trim(messages, fmt="openai", keep_tail=4)
        # First message after head should NOT be a bare tool result
        if len(trimmed) > 1:
            self.assertNotEqual(trimmed[1].get("role"), "tool")

    def test_no_orphaned_tool_results_anthropic(self):
        messages = []
        for i in range(5):
            messages.append({"role": "user", "content": f"msg {i}"})
            messages.append({
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"tu_{i}", "name": "ddm", "input": {}}],
            })
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"tu_{i}", "content": "layout"}],
            })
        trimmed = emergency_trim(messages, fmt="anthropic", keep_tail=4)
        # First message after head should not be an orphaned tool_result
        if len(trimmed) > 1:
            content = trimmed[1].get("content")
            if isinstance(content, list):
                is_orphan = all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                self.assertFalse(is_orphan, "First tail message is orphaned tool_result")

    def test_short_conversation_unchanged(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        trimmed = emergency_trim(messages, fmt="openai", keep_tail=10)
        self.assertEqual(len(trimmed), 3)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency(unittest.TestCase):
    def test_compact_twice_same_result(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            messages.append({"role": "user", "content": f"Turn {i}"})
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": f"c_{i}", "type": "function",
                     "function": {"name": "ddm", "arguments": "{}"}},
                ],
            })
            messages.append({
                "role": "tool", "tool_call_id": f"c_{i}",
                "content": f"=== Page Layout ===\ndata {i}",
            })

        messages, stats1 = compact_messages(messages, fmt="openai", keep_recent=3)
        snapshot = [m.get("content") if m.get("role") == "tool" else None for m in messages]

        messages, stats2 = compact_messages(messages, fmt="openai", keep_recent=3)
        snapshot2 = [m.get("content") if m.get("role") == "tool" else None for m in messages]

        self.assertEqual(snapshot, snapshot2)
        # Second pass should compact nothing new
        self.assertEqual(stats2["compacted"], 0)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats(unittest.TestCase):
    def test_stats_keys(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        _, stats = compact_messages(messages, fmt="anthropic", keep_recent=6)
        self.assertIn("compacted", stats)
        self.assertIn("preserved", stats)
        self.assertIn("tokens_before", stats)
        self.assertIn("tokens_after", stats)

    def test_tokens_decrease_after_compaction(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(10):
            messages.append({"role": "user", "content": f"Turn {i}"})
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": f"c_{i}", "type": "function",
                     "function": {"name": "cdp_screenshot", "arguments": "{}"}},
                ],
            })
            messages.append({
                "role": "tool", "tool_call_id": f"c_{i}",
                "content": "base64screenshotdata" * 100,
            })

        _, stats = compact_messages(messages, fmt="openai", keep_recent=3)
        self.assertLess(stats["tokens_after"], stats["tokens_before"])


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens(unittest.TestCase):
    def test_basic(self):
        messages = [{"role": "user", "content": "Hello world"}]
        tokens = estimate_tokens(messages)
        self.assertGreater(tokens, 0)

    def test_empty(self):
        self.assertEqual(estimate_tokens([]), 0)


if __name__ == "__main__":
    unittest.main()
