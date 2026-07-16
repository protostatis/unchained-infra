"""Regression test for server-side SSE write observability on the real
signed-in chat path.

The signed-in chat turn is served by `_stream_turn_journal`, which writes
events through `_write_turn_sse`. That is the path that delivers `done` to
the browser; when its write fails (non-socket transport error) the UI can
go silent until refresh. This test asserts that `_write_turn_sse`:

- traces each written event when UNCHAINED_SSE_DEBUG=1
  (chat.msg.sse_write), making the freeze diagnosable;
- on a non-socket write failure traces chat.msg.sse_write_failed with the
  event type/seq and a safe exception name, then re-raises so the journal
  loop's finally cleans up (instead of silently dropping the turn).
"""

import asyncio
import importlib
import os
import unittest
from unittest import mock

import web_app.handlers.chat_stream as cs


class SafeExcNameTest(unittest.TestCase):
    def test_safe_exc_name(self):
        self.assertEqual(cs._safe_exc_name(RuntimeError("secret")), "RuntimeError")
        self.assertEqual(cs._safe_exc_name(ValueError()), "ValueError")

    def test_sse_debug_toggle(self):
        with mock.patch.dict(os.environ, {"UNCHAINED_SSE_DEBUG": "1"}):
            importlib.reload(cs)
            self.assertTrue(cs._SSE_DEBUG)
        os.environ.pop("UNCHAINED_SSE_DEBUG", None)
        importlib.reload(cs)
        self.assertFalse(cs._SSE_DEBUG)


class WriteTurnSseTest(unittest.TestCase):
    def test_non_socket_write_error_is_traced_and_raised(self):
        traced = []

        class FakeCore:
            def _trace(self, event, **fields):
                traced.append((event, fields))

        class FailingResp:
            async def write(self, b):
                raise RuntimeError("transport closed mid-write")

        # Route _core() to our fake so _trace is captured.
        with mock.patch.object(cs, "_core", return_value=FakeCore()):
            event = {
                "type": "tool_result", "seq": 2, "req_id": "r1",
                "session_id": "s1", "data": "completed", "name": "result",
            }
            with self.assertRaises(RuntimeError):
                asyncio.run(cs._write_turn_sse(FailingResp(), event))

        failed = [t for t in traced if t[0] == "chat.msg.sse_write_failed"]
        self.assertEqual(len(failed), 1, "expected one sse_write_failed trace")
        self.assertEqual(failed[0][1]["type"], "tool_result")
        self.assertEqual(failed[0][1]["seq"], 2)
        self.assertEqual(failed[0][1]["error"], "RuntimeError")

    def test_successful_write_traced_when_debug_on(self):
        traced = []

        class FakeCore:
            def _trace(self, event, **fields):
                traced.append((event, fields))

        class OkResp:
            def __init__(self):
                self.written = 0

            async def write(self, b):
                self.written += 1

        with mock.patch.dict(os.environ, {"UNCHAINED_SSE_DEBUG": "1"}):
            importlib.reload(cs)
            with mock.patch.object(cs, "_core", return_value=FakeCore()):
                event = {"type": "done", "seq": 3, "req_id": "r1", "session_id": "s1"}
                asyncio.run(cs._write_turn_sse(OkResp(), event))
        os.environ.pop("UNCHAINED_SSE_DEBUG", None)
        importlib.reload(cs)

        writes = [t for t in traced if t[0] == "chat.msg.sse_write"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1]["type"], "done")
        self.assertEqual(writes[0][1]["seq"], 3)


if __name__ == "__main__":
    unittest.main()
