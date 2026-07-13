"""Targeted tests for the local OpenCode CLI lane."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class _FakeStdin:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        return None


class _FakeStream:
    def __init__(self, lines: list[str] | bytes = b""):
        if isinstance(lines, bytes):
            self._lines = []
            self._bytes = lines
        else:
            self._lines = [(line + "\n").encode() for line in lines]
            self._bytes = b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return self._bytes

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        if self._lines:
            return self._lines.pop(0)
        raise asyncio.IncompleteReadError(partial=b"", expected=None)


class _OversizedStream(_FakeStream):
    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        raise asyncio.LimitOverrunError("event exceeds configured limit", 100)


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b""):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode
        self.pid = 12345
        self.wait_calls = 0

    async def wait(self):
        self.wait_calls += 1
        return self.returncode


class _FakeWs:
    def __init__(self):
        self.events: list[dict] = []

    async def send(self, payload: str):
        self.events.append(json.loads(payload))


class TestOpenCodeCliLane(unittest.IsolatedAsyncioTestCase):
    def _load_module(self, extra_env: dict[str, str] | None = None):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        test_env = {
            "UNCHAINED_DATA_DIR": tempdir.name,
            "UNCHAINED_API_KEY": "uc_live_test",
            "OPENCODE_BIN": "opencode",
        }
        test_env.update(extra_env or {})
        env_patch = patch.dict(os.environ, test_env, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        module_name = "chat_agent_cli_opencode_test"
        sys.modules.pop(module_name, None)
        self.addCleanup(lambda: sys.modules.pop(module_name, None))
        module_path = os.path.join(os.path.dirname(__file__), "chat_agent_cli.py")
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        module.CWD = tempdir.name
        return module

    async def test_opencode_json_events_emit_existing_sse_contract(self):
        mod = self._load_module()
        ws = _FakeWs()
        captured: dict[str, object] = {}
        lines = [
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "oc-session-1",
                    "part": {
                        "id": "tool-1",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "uv run python cdp_tool.py ddm --text"},
                            "output": "page text",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "oc-session-1",
                    "part": {"id": "text-1", "type": "text", "text": "Final answer"},
                }
            ),
        ]

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env", {})
            captured["limit"] = kwargs.get("limit")
            return _FakeProc(lines)

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            await mod.handle_message_opencode(
                ws,
                "s-claude-abc12345-test",
                "Use the browser",
                "opencode-cli:anthropic/claude-sonnet-4-6",
                cdp_agent_id="claude-abc12345",
                req_id="req1",
            )

        cmd = captured["cmd"]
        self.assertIn("opencode", cmd)
        self.assertIn("run", cmd)
        self.assertIn("--format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--auto", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("anthropic/claude-sonnet-4-6", cmd)
        env = captured["env"]
        self.assertEqual(env["CDP_AGENT_ID"], "claude-abc12345")
        self.assertEqual(env["UNCHAINED_CHAT_SESSION_ID"], "s-claude-abc12345-test")
        self.assertEqual(captured.get("limit"), mod.OPENCODE_MAX_EVENT_BYTES)

        event_types = [evt["type"] for evt in ws.events]
        self.assertEqual(event_types, ["tool_start", "tool_result", "text", "done"])
        self.assertEqual(ws.events[0]["name"], "ddm")
        self.assertEqual(ws.events[1]["data"], "page text")
        self.assertEqual(ws.events[2]["data"], "Final answer")
        self.assertTrue(all(evt["session_id"] == "s-claude-abc12345-test" for evt in ws.events))
        self.assertTrue(all(evt["req_id"] == "req1" for evt in ws.events))
        saved = mod._load_opencode_session()
        self.assertEqual(saved.get("session_id"), "oc-session-1")

    async def test_opencode_stale_session_retries_fresh_and_clears_saved_mapping(self):
        mod = self._load_module()
        ws = _FakeWs()
        sid = "s-claude-abc12345-test"
        model = "opencode-cli:anthropic/claude-sonnet-4-6"
        mod.opencode_sessions[sid] = "stale-session"
        mod._save_opencode_session(sid, "stale-session", model="anthropic/claude-sonnet-4-6")
        fresh_lines = [
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "fresh-session",
                    "part": {"id": "text-1", "type": "text", "text": "Recovered"},
                }
            ),
        ]
        calls: list[list[str]] = []

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            calls.append(list(cmd))
            if len(calls) == 1:
                return _FakeProc([], returncode=1, stderr=b"session not found")
            return _FakeProc(fresh_lines)

        async def fake_sleep(_delay):
            return None

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec), \
             patch.object(mod.asyncio, "sleep", side_effect=fake_sleep):
            await mod.handle_message_opencode(
                ws,
                sid,
                "Recover from stale session",
                model,
                cdp_agent_id="claude-abc12345",
                req_id="req1",
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("--session", calls[0])
        self.assertIn("stale-session", calls[0])
        self.assertNotIn("--session", calls[1])
        self.assertEqual(mod.opencode_sessions.get(sid), "fresh-session")
        saved = mod._load_opencode_session()
        self.assertEqual(saved.get("session_id"), "fresh-session")
        event_types = [evt["type"] for evt in ws.events]
        self.assertEqual(event_types, ["text", "done"])
        self.assertEqual(ws.events[0]["data"], "Recovered")

    async def test_opencode_duplicate_text_and_tool_parts_are_emitted_once(self):
        mod = self._load_module()
        ws = _FakeWs()
        tool_event = {
            "type": "tool_use",
            "sessionID": "oc-session-1",
            "part": {
                "id": "tool-1",
                "tool": "bash",
                "state": {
                    "status": "completed",
                    "input": {"command": "uv run python cdp_tool.py ddm"},
                    "output": "layout",
                },
            },
        }
        text_event = {
            "type": "text",
            "sessionID": "oc-session-1",
            "part": {"id": "text-1", "type": "text", "text": "Only once"},
        }
        lines = [
            json.dumps(tool_event),
            json.dumps(tool_event),
            json.dumps(text_event),
            json.dumps(text_event),
        ]

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return _FakeProc(lines)

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            await mod.handle_message_opencode(
                ws,
                "s-claude-abc12345-test",
                "Use the browser",
                "opencode-cli:",
                cdp_agent_id="claude-abc12345",
            )

        event_types = [evt["type"] for evt in ws.events]
        self.assertEqual(event_types, ["tool_start", "tool_result", "text", "done"])
        self.assertEqual(ws.events[2]["data"], "Only once")

    async def test_opencode_explicit_error_event_emits_done(self):
        mod = self._load_module()
        ws = _FakeWs()
        lines = [
            json.dumps(
                {
                    "type": "error",
                    "sessionID": "oc-session-1",
                    "error": {
                        "message": "tool execution failed",
                    },
                }
            )
        ]

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return _FakeProc(lines)

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            await mod.handle_message_opencode(
                ws,
                "s-claude-abc12345-test",
                "Use the browser",
                "opencode-cli:",
                cdp_agent_id="claude-abc12345",
            )

        event_types = [evt["type"] for evt in ws.events]
        self.assertEqual(event_types, ["error", "done"])
        self.assertEqual(ws.events[0]["data"], "OpenCode CLI error: tool execution failed")

    async def test_opencode_reader_accepts_large_event_with_configured_limit(self):
        mod = self._load_module()
        payload = json.dumps({"type": "text", "part": {"text": "x" * (128 * 1024)}}).encode() + b"\n"
        reader = asyncio.StreamReader(limit=mod.OPENCODE_MAX_EVENT_BYTES)
        reader.feed_data(payload)
        reader.feed_eof()

        lines = [line async for line in mod._iter_opencode_event_lines(reader)]

        self.assertEqual(lines, [payload])

    async def test_opencode_reader_reports_oversized_event_and_preserves_partial_eof(self):
        mod = self._load_module()
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(b"x" * 128 + b"\n")
        reader.feed_eof()
        with self.assertRaises(mod._OpenCodeEventTooLarge):
            _ = [line async for line in mod._iter_opencode_event_lines(reader)]

        partial = asyncio.StreamReader(limit=1024)
        partial.feed_data(b'{"type":"text"}')
        partial.feed_eof()
        self.assertEqual(
            [line async for line in mod._iter_opencode_event_lines(partial)],
            [b'{"type":"text"}'],
        )

    async def test_opencode_oversized_event_kills_and_reaps_subprocess(self):
        mod = self._load_module()
        ws = _FakeWs()
        proc = _FakeProc([], returncode=None)
        proc.stdout = _OversizedStream()
        captured: dict[str, object] = {}

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            captured["limit"] = kwargs.get("limit")
            return proc

        def fake_kill(target):
            self.assertIs(target, proc)
            proc.returncode = -9

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec), \
             patch.object(mod, "_kill_process", side_effect=fake_kill):
            await mod.handle_message_opencode(
                ws,
                "s-claude-abc12345-test",
                "Produce a large event",
                "opencode-cli:",
            )

        self.assertEqual(captured["limit"], mod.OPENCODE_MAX_EVENT_BYTES)
        self.assertEqual([event["type"] for event in ws.events], ["error", "done"])
        self.assertIn("exceeded event size limit", ws.events[0]["data"])
        self.assertGreaterEqual(proc.wait_calls, 1)

    async def test_opencode_processing_value_error_is_not_misreported_as_overrun(self):
        mod = self._load_module()
        ws = _FakeWs()
        proc = _FakeProc([json.dumps({"type": "error", "error": {"message": "provider error"}})], returncode=None)

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            return proc

        def fake_kill(target):
            self.assertIs(target, proc)
            proc.returncode = -9

        with patch.object(mod.asyncio, "create_subprocess_exec", side_effect=fake_create_subprocess_exec), \
             patch.object(mod, "_extract_opencode_error_message", side_effect=ValueError("processing bug")), \
             patch.object(mod, "_kill_process", side_effect=fake_kill):
            await mod.handle_message_opencode(
                ws,
                "s-claude-abc12345-test",
                "Trigger processing failure",
                "opencode-cli:",
            )

        self.assertEqual([event["type"] for event in ws.events], ["error", "done"])
        self.assertIn("internal error (ValueError)", ws.events[0]["data"])
        self.assertNotIn("event size limit", ws.events[0]["data"])
        self.assertGreaterEqual(proc.wait_calls, 1)

    def test_opencode_event_limit_rejects_unsafe_configuration(self):
        default_limit = 8 * 1024 * 1024
        for value in ("invalid", "0", "-1", str(32 * 1024 * 1024)):
            with self.subTest(value=value):
                mod = self._load_module({"OPENCODE_MAX_EVENT_BYTES": value})
                self.assertEqual(mod.OPENCODE_MAX_EVENT_BYTES, default_limit)

    def test_opencode_model_list_parses_cli_lines(self):
        mod = self._load_module()

        def fake_run(*args, **kwargs):
            return types.SimpleNamespace(
                stdout=(
                    "openai/gpt-5.5\n"
                    "openrouter/anthropic/claude-sonnet-4.6\n"
                    "not a model id\n"
                    "openai/gpt-5.5\n"
                    "opencode/mimo-v2.5-free\n"
                )
            )

        with patch.object(mod, "_cli_binary_available", return_value=True), \
             patch.object(mod.subprocess, "run", side_effect=fake_run):
            models = mod._list_opencode_models()

        self.assertEqual(
            models,
            [
                "openai/gpt-5.5",
                "openrouter/anthropic/claude-sonnet-4.6",
                "opencode/mimo-v2.5-free",
            ],
        )


if __name__ == "__main__":
    unittest.main()
