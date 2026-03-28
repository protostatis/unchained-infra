"""Tests for rhythm commands in cdp_tool.py.

Mocks cloud_tools so no relay/private-core connection is needed.
Run: python3 test_cdp_tool_rhythm.py
"""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_tools

HERE = os.path.dirname(os.path.abspath(__file__))
CDT = os.path.join(HERE, "cdp_tool.py")


# ---------------------------------------------------------------------------
# 1. cloud_tools delegation (same pattern as test_cloud_tools_click.py)
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.run_rhythm_train = AsyncMock(return_value='{"schema":"learned","elements":12}')
        self.run_rhythm_catch = AsyncMock(return_value='{"matched":["$450k","3 bed"]}')
        self.run_rhythm_execute = AsyncMock(return_value='{"steps_run":2,"ok":true}')
        self.run_rhythm_query = AsyncMock(return_value='{"sites":["example.com"]}')


class TestCloudToolsRhythmDelegation(unittest.IsolatedAsyncioTestCase):

    async def test_rhythm_train_delegates(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_rhythm_train("agent", "auto", "https://ex.com",
                                                     click_link_text="Products")
        self.assertIn("learned", out)
        fake.run_rhythm_train.assert_awaited_once_with(
            "agent", "auto", "https://ex.com", "Products", "127.0.0.1", 8765)

    async def test_rhythm_catch_delegates(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_rhythm_catch(
                "agent", "auto", "https://ex.com", "find prices",
                ["price", "bed"], click_text="Search")
        self.assertIn("matched", out)
        fake.run_rhythm_catch.assert_awaited_once_with(
            "agent", "auto", "https://ex.com", "find prices",
            ["price", "bed"], "Search", "127.0.0.1", 8765)

    async def test_rhythm_execute_delegates(self):
        fake = _FakeClient()
        targets = [{"action": "click", "text": "Next"}]
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_rhythm_execute(
                "agent", "auto", "https://ex.com", targets)
        self.assertIn("steps_run", out)
        fake.run_rhythm_execute.assert_awaited_once_with(
            "agent", "auto", "https://ex.com", targets, "127.0.0.1", 8765)

    async def test_rhythm_query_delegates(self):
        fake = _FakeClient()
        with patch("cloud_tools._client", return_value=fake):
            out = await cloud_tools.run_rhythm_query("list_all", domain="ex.com")
        self.assertIn("sites", out)
        fake.run_rhythm_query.assert_awaited_once_with("list_all", "", "ex.com")


# ---------------------------------------------------------------------------
# 2. cdp_tool.py CLI arg parsing (subprocess — avoids asyncio.run conflict)
#    Uses a tiny helper script that patches cloud_tools before importing cdp_tool.
# ---------------------------------------------------------------------------

_HELPER = '''\
import asyncio, json, os, sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.getcwd())
import cloud_tools

class _Fake:
    run_rhythm_train = AsyncMock(return_value='{"ok":"train"}')
    run_rhythm_catch = AsyncMock(return_value='{"ok":"catch"}')
    run_rhythm_execute = AsyncMock(return_value='{"ok":"execute"}')
    run_rhythm_query = AsyncMock(return_value='{"ok":"query"}')

cloud_tools._client = lambda: _Fake()

# Now set sys.argv and run cdp_tool
sys.argv = json.loads(sys.argv[1])

# cdp_tool runs asyncio.run(main()) at import time
import cdp_tool
'''


def _run_cdp(argv_list):
    """Run cdp_tool.py in a subprocess with mocked cloud_tools."""
    r = subprocess.run(
        [sys.executable, "-c", _HELPER, json.dumps(argv_list)],
        capture_output=True, text=True, timeout=10,
        cwd=HERE,
        env={**os.environ,
             "CDP_AGENT_ID": "test-agent",
             "CDP_RELAY_HOST": "localhost",
             "CDP_RELAY_PORT": "9999",
             "CDP_TAB_ID": "auto"},
    )
    return r


class TestCdpToolRhythmCLI(unittest.TestCase):

    def test_rhythm_train_ok(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_train", "https://ex.com"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("train", r.stdout)

    def test_rhythm_train_with_click(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_train", "https://ex.com", "--click", "Products"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rhythm_train_missing_url(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_train"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stderr)

    def test_rhythm_catch_ok(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_catch", "https://ex.com", "find prices", "price,bed,sqft"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("catch", r.stdout)

    def test_rhythm_catch_with_click(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_catch", "https://ex.com", "task", "terms", "--click", "Go"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rhythm_catch_missing_args(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_catch", "https://ex.com"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stderr)

    def test_rhythm_execute_ok(self):
        targets = json.dumps([{"action": "click", "text": "Next"}])
        r = _run_cdp(["cdp_tool.py", "rhythm_execute", "https://ex.com", targets])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("execute", r.stdout)

    def test_rhythm_execute_bad_json(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_execute", "https://ex.com", "not-json"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Invalid JSON", r.stderr)

    def test_rhythm_execute_not_array(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_execute", "https://ex.com", '{"not":"array"}'])
        self.assertEqual(r.returncode, 1)
        self.assertIn("JSON array", r.stderr)

    def test_rhythm_execute_missing_args(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_execute", "https://ex.com"])
        self.assertEqual(r.returncode, 1)

    def test_rhythm_query_ok(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_query", "list_all"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("query", r.stdout)

    def test_rhythm_query_with_flags(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_query", "lookup_url", "--url", "https://ex.com"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_rhythm_query_missing_action(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_query"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stderr)

    def test_tab_flag_works_with_rhythm(self):
        r = _run_cdp(["cdp_tool.py", "rhythm_train", "--tab", "my-tab", "https://ex.com"])
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
