import json
import unittest
from unittest.mock import patch

from benchmark.agent_cli import _run_task_claude
from benchmark.common import BenchmarkConfig


class _FakeStdin:
    def __init__(self):
        self.buffer = b""
        self.closed = False

    def write(self, data):
        self.buffer += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeStream:
    def __init__(self, lines=None, *, read_data=b""):
        self._lines = [
            line if isinstance(line, bytes) else line.encode("utf-8")
            for line in (lines or [])
        ]
        self._read_data = read_data

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self):
        return self._read_data


class _FakeProcess:
    def __init__(self, lines):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream(read_data=b"")
        self.returncode = 0

    async def wait(self):
        return self.returncode


class TestBenchmarkAgentCli(unittest.IsolatedAsyncioTestCase):
    async def test_claude_raw_tool_results_follow_tool_use_order(self):
        events = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "Bash",
                            "input": {
                                "command": 'uv run python cdp_tool.py navigate "https://example.com"',
                            },
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_2",
                            "name": "Bash",
                            "input": {
                                "command": "uv run python cdp_tool.py ddm --text",
                            },
                        },
                    ]
                },
            },
            {"type": "user", "tool_use_result": {"stdout": "first result"}},
            {"type": "user", "tool_use_result": {"stdout": "second result"}},
            {"type": "result", "result": "done", "num_turns": 2},
        ]
        proc = _FakeProcess([json.dumps(event) + "\n" for event in events])

        with patch(
            "benchmark.agent_cli.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            result = await _run_task_claude(
                "test prompt",
                config=BenchmarkConfig(),
                model="sonnet",
                max_turns=3,
                timeout=5,
            )

        self.assertEqual(result.answer, "done")
        self.assertEqual(len(result.tool_log), 2)
        self.assertEqual(result.tool_log[0].get("output_preview"), "first result")
        self.assertEqual(result.tool_log[1].get("output_preview"), "second result")


if __name__ == "__main__":
    unittest.main()
