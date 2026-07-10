"""Focused transport tests for the hosted semantic Agent View backend."""

import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from web_app.semantic_mirror import (
    DRAIN_MIRROR_EXPRESSION,
    INSTALL_MIRROR_EXPRESSION,
    MAX_MIRROR_PAYLOAD_CHARS,
    MIRROR_CHUNK_CHARS,
    evaluate_mirror_payload,
    parse_evaluation,
    stream_semantic_mirror,
)


def _encoded(value, *, double=False):
    raw = json.dumps(value, separators=(",", ":"))
    return json.dumps(raw) if double else raw


class TestSemanticMirrorParsing(unittest.TestCase):
    def test_capture_expressions_match_mirror_demo_pr_2(self):
        self.assertEqual(
            hashlib.sha256(INSTALL_MIRROR_EXPRESSION.encode()).hexdigest(),
            "70dfa9798202667b699e17e759eba99d0488710c9ff97fe80c23ecb9f8147fd4",
        )
        self.assertEqual(
            hashlib.sha256(DRAIN_MIRROR_EXPRESSION.encode()).hexdigest(),
            "05cee377543661f5a3359c83567b570e60c601b81b96f70b3ae53e361cea2e17",
        )

    def test_direct_and_double_encoded_json(self):
        payload = {"url": "https://example.test", "operations": []}
        self.assertEqual(parse_evaluation(_encoded(payload)), payload)
        self.assertEqual(parse_evaluation(_encoded(payload, double=True)), payload)


class TestSemanticMirrorTransport(unittest.IsolatedAsyncioTestCase):
    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_chunk_reconstruction_uses_bounded_reads(self, run_js):
        payload = {
            "url": "https://example.test",
            "body": "x" * (MIRROR_CHUNK_CHARS + 128),
        }
        encoded = _encoded(payload)
        run_js.side_effect = [
            _encoded({"__unchainedMirrorChunks": 1, "length": len(encoded)}),
            json.dumps(encoded[:MIRROR_CHUNK_CHARS]),
            encoded[MIRROR_CHUNK_CHARS:],
        ]

        result = await evaluate_mirror_payload(
            "agent-resolved", "tab-resolved", INSTALL_MIRROR_EXPRESSION, "relay", 9999
        )

        self.assertEqual(result, payload)
        self.assertEqual(run_js.await_count, 3)
        first_read = run_js.await_args_list[1].args
        final_read = run_js.await_args_list[2].args
        self.assertIn(f"state.readOutbound(0, {MIRROR_CHUNK_CHARS}, false)", first_read[2])
        remainder = len(encoded) - MIRROR_CHUNK_CHARS
        self.assertIn(
            f"state.readOutbound({MIRROR_CHUNK_CHARS}, {remainder}, true)",
            final_read[2],
        )
        self.assertEqual(first_read[:2], ("agent-resolved", "tab-resolved"))
        self.assertEqual(first_read[3:], ("relay", 9999))

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_oversized_chunk_manifest_is_rejected(self, run_js):
        run_js.return_value = _encoded({
            "__unchainedMirrorChunks": 1,
            "length": MAX_MIRROR_PAYLOAD_CHARS + 1,
        })

        with self.assertRaisesRegex(ValueError, "oversized"):
            await evaluate_mirror_payload("agent", "tab", INSTALL_MIRROR_EXPRESSION)

        run_js.assert_awaited_once()


class TestSemanticMirrorStream(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep_patch = patch(
            "web_app.semantic_mirror.asyncio.sleep", new=AsyncMock(return_value=None)
        )
        self.sleep_patch.start()

    async def asyncTearDown(self):
        self.sleep_patch.stop()

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_initial_snapshot_then_patch(self, run_js):
        snapshot = {"url": "https://example.test", "hash": "fnv1a-1"}
        patch_payload = {
            "seq": 1,
            "previousSeq": 0,
            "operations": [{"op": "text", "targetId": "ucm-1", "text": "new"}],
            "rawBytes": 100,
        }
        run_js.side_effect = [_encoded(snapshot), _encoded(patch_payload)]
        stream = stream_semantic_mirror("agent", "tab")

        self.assertEqual(
            await anext(stream),
            {"type": "snapshot", "snapshot": snapshot, "resync": False},
        )
        self.assertEqual(
            await anext(stream),
            {"type": "patch", "patch": patch_payload},
        )
        await stream.aclose()
        self.assertEqual(run_js.await_args_list[0].args[2], INSTALL_MIRROR_EXPRESSION)
        self.assertEqual(run_js.await_args_list[1].args[2], DRAIN_MIRROR_EXPRESSION)

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_idle_patch_is_not_yielded(self, run_js):
        snapshot = {"url": "https://example.test", "hash": "fnv1a-1"}
        idle = {"seq": 0, "previousSeq": 0, "operations": [], "rawBytes": 70}
        changed = {
            "seq": 1,
            "previousSeq": 0,
            "operations": [{"op": "remove", "targetId": "ucm-2"}],
            "rawBytes": 90,
        }
        run_js.side_effect = [_encoded(snapshot), _encoded(idle), _encoded(changed)]
        stream = stream_semantic_mirror("agent", "tab")

        await anext(stream)
        self.assertEqual(
            await anext(stream),
            {"type": "patch", "patch": changed},
        )
        await stream.aclose()
        self.assertEqual(run_js.await_count, 3)

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_reset_reinstalls_and_yields_resync_snapshot(self, run_js):
        initial = {"url": "https://example.test/one", "hash": "fnv1a-1"}
        reset = {
            "seq": 1,
            "previousSeq": 0,
            "operations": [],
            "resetRequired": True,
            "rawBytes": 100,
        }
        resync = {"url": "https://example.test/two", "hash": "fnv1a-2"}
        run_js.side_effect = [_encoded(initial), _encoded(reset), _encoded(resync)]
        stream = stream_semantic_mirror("agent", "tab")

        await anext(stream)
        self.assertEqual(
            await anext(stream),
            {"type": "snapshot", "snapshot": resync, "resync": True},
        )
        await stream.aclose()
        self.assertEqual(
            [call.args[2] for call in run_js.await_args_list],
            [INSTALL_MIRROR_EXPRESSION, DRAIN_MIRROR_EXPRESSION, INSTALL_MIRROR_EXPRESSION],
        )

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_stop_request_ends_before_polling_old_tab(self, run_js):
        snapshot = {"url": "https://example.test", "hash": "fnv1a-1"}
        run_js.return_value = _encoded(snapshot)
        stop = False
        stream = stream_semantic_mirror("agent", "tab", stop_requested=lambda: stop)

        await anext(stream)
        stop = True
        with self.assertRaises(StopAsyncIteration):
            await anext(stream)

        run_js.assert_awaited_once()

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_transient_navigation_error_recovers_with_resync(self, run_js):
        initial = {"url": "https://example.test/one", "hash": "fnv1a-1"}
        reset = {
            "seq": 1,
            "previousSeq": 0,
            "operations": [],
            "resetRequired": True,
            "rawBytes": 100,
        }
        resync = {"url": "https://example.test/two", "hash": "fnv1a-2"}
        run_js.side_effect = [
            _encoded(initial),
            RuntimeError("execution context destroyed"),
            _encoded(reset),
            _encoded(resync),
        ]
        stream = stream_semantic_mirror("agent", "tab")

        await anext(stream)
        self.assertEqual(
            await anext(stream),
            {"type": "snapshot", "snapshot": resync, "resync": True},
        )
        await stream.aclose()


if __name__ == "__main__":
    unittest.main()
