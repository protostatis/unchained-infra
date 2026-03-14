import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from benchmark.common import BenchmarkBrowser, BenchmarkConfig, apply_default_tab_id, extract_cdp_tool_name
from benchmark.runner import (
    _enforce_safe_targets,
    _is_local_host,
    _is_local_url,
    _normalize_agent_name,
    _preflight_agent_check,
    _resolve_model_name,
    load_tasks,
)


class TestBenchmarkRunner(unittest.TestCase):
    def test_hard_task_count(self):
        tasks = load_tasks(difficulty="hard")
        self.assertEqual(len(tasks), 14)

    def test_default_tab_id_is_injected(self):
        original = {"x": 10}
        updated = apply_default_tab_id(original, "tab-123")
        self.assertEqual(updated["tab_id"], "tab-123")
        self.assertNotIn("tab_id", original)

    def test_explicit_tab_id_is_preserved(self):
        updated = apply_default_tab_id({"tab_id": "task-tab", "x": 10}, "fallback")
        self.assertEqual(updated["tab_id"], "task-tab")

    def test_agent_alias_normalization(self):
        self.assertEqual(_normalize_agent_name("autorouter"), "openrouter")
        self.assertEqual(_normalize_agent_name("openrouter"), "openrouter")

    def test_openrouter_default_model_resolution(self):
        model_name = _resolve_model_name(
            "autorouter",
            cli_model=None,
            orch_model=None,
            or_model=None,
        )
        self.assertEqual(model_name, "arcee-ai/trinity-large-preview:free")

    def test_extract_tool_name(self):
        command = 'uv run python cdp_tool.py navigate "https://example.com"'
        self.assertEqual(extract_cdp_tool_name(command), "navigate")

    def test_local_target_detection(self):
        self.assertTrue(_is_local_host("127.0.0.1"))
        self.assertTrue(_is_local_host("localhost"))
        self.assertTrue(_is_local_host("relay"))
        self.assertTrue(_is_local_url("http://private-core:8770"))

    def test_remote_target_detection(self):
        self.assertFalse(_is_local_host("api.unchainedsky.com"))
        self.assertFalse(_is_local_url("https://api.unchainedsky.com"))

    def test_remote_targets_are_blocked_by_default(self):
        config = BenchmarkConfig(
            agent_id="claude-1234",
            relay_host="api.unchainedsky.com",
            relay_port=443,
            private_core_url="https://api.unchainedsky.com",
        )
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _enforce_safe_targets(config, allow_remote=False)

    def test_remote_targets_can_be_explicitly_allowed(self):
        config = BenchmarkConfig(
            agent_id="claude-1234",
            relay_host="api.unchainedsky.com",
            relay_port=443,
            private_core_url="https://api.unchainedsky.com",
        )
        _enforce_safe_targets(config, allow_remote=True)


class TestBenchmarkBrowser(unittest.IsolatedAsyncioTestCase):
    async def test_browser_close_tolerates_older_private_core_client(self):
        browser = BenchmarkBrowser(
            BenchmarkConfig(
                agent_id="claude-test",
                relay_host="127.0.0.1",
                relay_port=8765,
            )
        )
        browser.client = SimpleNamespace()

        await browser.close()

    async def test_local_tab_lifecycle_uses_private_core_client(self):
        browser = BenchmarkBrowser(
            BenchmarkConfig(
                agent_id="claude-test",
                relay_host="127.0.0.1",
                relay_port=8765,
                private_core_token="benchtoken123",
            )
        )
        browser.client = SimpleNamespace(
            create_tab=AsyncMock(return_value="tab-local-1"),
            close_tab=AsyncMock(return_value=True),
            run_cdp_command=AsyncMock(return_value={"targetId": "tab-remote"}),
            close=AsyncMock(return_value=None),
        )

        tab_id = await browser.create_tab("about:blank")
        await browser.close_tab(tab_id)

        self.assertEqual(tab_id, "tab-local-1")
        browser.client.create_tab.assert_awaited_once_with(
            "claude-test",
            "about:blank",
            "127.0.0.1",
            8765,
        )
        browser.client.close_tab.assert_awaited_once_with(
            "claude-test",
            "tab-local-1",
            "127.0.0.1",
            8765,
        )
        browser.client.run_cdp_command.assert_not_called()


class TestBenchmarkPreflight(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_succeeds_for_reachable_agent(self):
        config = BenchmarkConfig(
            agent_id="claude-test",
            relay_host="127.0.0.1",
            relay_port=8765,
            private_core_token="benchtoken123",
        )
        browser = SimpleNamespace(
            execute_tool=AsyncMock(return_value="=== CDP Tabs (1) ===\npage tab"),
            close=AsyncMock(return_value=None),
        )

        with patch("benchmark.runner.BenchmarkBrowser", return_value=browser):
            error = await _preflight_agent_check(config)

        self.assertIsNone(error)
        browser.execute_tool.assert_awaited_once_with(
            "ddm",
            {"flags": "--tabs"},
            default_tab_id="auto",
        )
        browser.close.assert_awaited_once()

    async def test_preflight_reports_tool_error(self):
        config = BenchmarkConfig(
            agent_id="claude-missing",
            relay_host="127.0.0.1",
            relay_port=8765,
            private_core_token="benchtoken123",
        )
        browser = SimpleNamespace(
            execute_tool=AsyncMock(return_value="Tool error (ddm): Agent claude-missing not found"),
            close=AsyncMock(return_value=None),
        )

        with patch("benchmark.runner.BenchmarkBrowser", return_value=browser):
            error = await _preflight_agent_check(config)

        self.assertIn("claude-missing", error)
        self.assertIn("unreachable", error)
        browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
