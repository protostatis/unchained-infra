"""Focused transport tests for the hosted semantic Agent View backend."""

import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from web_app.semantic_mirror import (
    DEFAULT_MIRROR_KEY,
    DRAIN_MIRROR_EXPRESSION,
    INSTALL_MIRROR_EXPRESSION,
    MAX_MIRROR_PAYLOAD_CHARS,
    MIRROR_CHUNK_CHARS,
    build_dispose_mirror_expression,
    build_drain_mirror_expression,
    build_install_mirror_expression,
    evaluate_mirror_payload,
    execute_semantic_action,
    mirror_action_expression,
    mirror_payload_chunk_expression,
    parse_evaluation,
    stream_semantic_mirror,
)


def _encoded(value, *, double=False):
    raw = json.dumps(value, separators=(",", ":"))
    return json.dumps(raw) if double else raw


class TestSemanticMirrorParsing(unittest.TestCase):
    def test_capture_expressions_match_reviewed_semantic_protocol(self):
        self.assertEqual(
            hashlib.sha256(INSTALL_MIRROR_EXPRESSION.encode()).hexdigest(),
            "a19cce7be880dd2192822e006728aabb423d8f13d1464d829fbadf0bed418438",
        )
        self.assertEqual(
            hashlib.sha256(DRAIN_MIRROR_EXPRESSION.encode()).hexdigest(),
            "5ef57181dde9e1f1da31db9221e94c39efdd598c12c6290980f4f1344bb9f480",
        )

    def test_direct_and_double_encoded_json(self):
        payload = {"url": "https://example.test", "operations": []}
        self.assertEqual(parse_evaluation(_encoded(payload)), payload)
        self.assertEqual(parse_evaluation(_encoded(payload, double=True)), payload)

    def test_capture_protocol_preserves_nested_scroll_and_safe_search_values(self):
        self.assertIn("captureEpoch: state.captureEpoch", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("scrollPositions: collectScrollPositions()", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("MAX_SCROLL_POSITIONS = 500", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("role === 'searchbox' || role === 'combobox'", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("email|tel|password|file|hidden", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("canvas|video|iframe|frame|object|embed", INSTALL_MIRROR_EXPRESSION)

    def test_capture_protocol_preserves_bounded_viewport_critical_styles(self):
        self.assertIn("MAX_CRITICAL_STYLE_BYTES = 512 * 1024", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("MAX_CRITICAL_STYLE_BYTES_PER_NODE = 1024", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("function applyCriticalComputedStyle", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("if (!isInViewport(source)) return", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("computed.getPropertyValue(property)", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("criticalStylesTruncated", INSTALL_MIRROR_EXPRESSION)
        for property_name in (
            "'display'",
            "'width'",
            "'height'",
            "'flex'",
            "'grid-template-columns'",
            "'font-size'",
            "'fill'",
            "'stroke'",
        ):
            self.assertIn(property_name, INSTALL_MIRROR_EXPRESSION)

    def test_capture_protocol_prioritizes_body_and_reports_style_fidelity(self):
        self.assertIn("MAX_HEAD_CAPTURE_BYTES = 384 * 1024", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("styleLimit: Math.max(0, headLimit - 64 * 1024)", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("function collectStyleDiagnostics", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("omittedInlineStyleSheets", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("capturedHeadBytes", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("truncationStage", INSTALL_MIRROR_EXPRESSION)
        self.assertLess(
            INSTALL_MIRROR_EXPRESSION.index(
                "const bodyClone = document.body ? cloneSanitized(document.body, bodyBudget, fidelity)"
            ),
            INSTALL_MIRROR_EXPRESSION.index(
                "const headClone = document.head ? cloneSanitized(document.head, headBudget, fidelity)"
            ),
        )
        self.assertLess(
            INSTALL_MIRROR_EXPRESSION.index("snapshot.head = ''"),
            INSTALL_MIRROR_EXPRESSION.index(
                "snapshot.body = '<div data-ucm-capture-truncated=\"output-limit\"></div>'"
            ),
        )

    def test_capture_protocol_uses_safe_stylesheet_fallbacks(self):
        self.assertIn("httpEquiv === 'content-security-policy'", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("viewportStyleRefresh", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("criticalStyleAnchorY", INSTALL_MIRROR_EXPRESSION)
        self.assertIn("Math.round((window.innerHeight || 0) * 0.5)", INSTALL_MIRROR_EXPRESSION)

    def test_action_expression_binds_sequence_and_keeps_server_safety_guards(self):
        expression = mirror_action_expression(
            {
                "targetId": "ucm-a",
                "kind": "input",
                "value": "line\u2028break",
            },
            expected_seq=7,
            expected_epoch="epoch-test-123",
        )

        self.assertIn("const expectedSeq = 7", expression)
        self.assertIn("state.captureEpoch !== expectedEpoch", expression)
        self.assertIn("expectedSeq > state.seq", expression)
        self.assertIn("sensitive-target", expression)
        self.assertIn("confirmation-required", expression)
        self.assertIn("target.form || buttonLike", expression)
        self.assertIn("confirmationControl.parentElement", expression)
        self.assertIn("line\\u2028break", expression)
        self.assertIn('"confirmed":false', expression)


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

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_semantic_action_runs_under_source_lock(self, run_js):
        run_js.return_value = _encoded(
            {"ok": True, "reason": "ok", "navigated": False, "currentSeq": 3}
        )
        lock = asyncio.Lock()

        result = await execute_semantic_action(
            "agent",
            "tab",
            {"targetId": "ucm-1", "kind": "click"},
            expected_seq=3,
            expected_epoch="epoch-test-123",
            relay_host="relay",
            relay_port=9999,
            operation_lock=lock,
        )

        self.assertTrue(result["ok"])
        self.assertFalse(lock.locked())
        args = run_js.await_args.args
        self.assertEqual(args[:2], ("agent", "tab"))
        self.assertIn("const expectedSeq = 3", args[2])
        self.assertIn('const expectedEpoch = "epoch-test-123"', args[2])
        self.assertEqual(args[3:], ("relay", 9999))

    @patch("web_app.semantic_mirror.cloud_tools.click", new_callable=AsyncMock)
    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_semantic_click_uses_trusted_cdp_coordinates(self, run_js, click):
        run_js.return_value = _encoded(
            {
                "ok": False,
                "reason": "cdp-click-required",
                "x": 320,
                "y": 180,
                "targetId": "ucm-2",
                "currentSeq": 5,
                "captureEpoch": "epoch-test-123",
            }
        )
        click.return_value = "clicked"

        result = await execute_semantic_action(
            "agent",
            "tab",
            {"targetId": "ucm-2", "kind": "click", "fx": 0.25, "fy": 0.75},
            expected_seq=4,
            expected_epoch="epoch-test-123",
            relay_host="relay",
            relay_port=9999,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["actionKind"], "click")
        click.assert_awaited_once_with("agent", "tab", 320, 180, "relay", 9999)


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
    async def test_transient_initial_chunk_error_reinstalls_and_recovers(self, run_js):
        snapshot = {"url": "https://example.test", "hash": "fnv1a-recovered"}
        run_js.side_effect = [
            _encoded({"__unchainedMirrorChunks": 1, "length": 12}),
            json.dumps("short"),
            _encoded(snapshot),
        ]
        stream = stream_semantic_mirror("agent", "tab")

        self.assertEqual(
            await anext(stream),
            {"type": "snapshot", "snapshot": snapshot, "resync": False},
        )
        await stream.aclose()
        self.assertEqual(
            [call.args[2] for call in run_js.await_args_list],
            [
                INSTALL_MIRROR_EXPRESSION,
                run_js.await_args_list[1].args[2],
                INSTALL_MIRROR_EXPRESSION,
            ],
        )
        self.assertIn("state.readOutbound(0, 12, true)", run_js.await_args_list[1].args[2])

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_initial_capture_retries_are_bounded(self, run_js):
        run_js.side_effect = RuntimeError("relay unavailable")
        stream = stream_semantic_mirror("agent", "tab")

        with self.assertRaisesRegex(RuntimeError, "relay unavailable"):
            await anext(stream)

        self.assertEqual(run_js.await_count, 3)

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


class TestPerConnectionMirrorKey(unittest.TestCase):
    """Regression guard for the scroll-reset-to-top bug caused by multiple
    Agent View clients sharing a single global mirror symbol.

    Each WS connection must get its own Symbol.for key so that INSTALL by one
    client does not dispose another client's mirror (which invalidates the
    captureEpoch and causes scroll actions to fail with stale-document).
    """

    def test_build_install_replaces_symbol_key(self):
        default_expr = build_install_mirror_expression()
        custom_expr = build_install_mirror_expression(
            "unchained.mirror.capture.v1.abc123def456"
        )
        self.assertEqual(default_expr, INSTALL_MIRROR_EXPRESSION)
        self.assertNotEqual(custom_expr, INSTALL_MIRROR_EXPRESSION)
        self.assertIn("unchained.mirror.capture.v1.abc123def456", custom_expr)
        self.assertNotIn(
            "Symbol.for('unchained.mirror.capture.v1')",
            custom_expr,
        )

    def test_build_drain_replaces_symbol_key(self):
        default_expr = build_drain_mirror_expression()
        custom_expr = build_drain_mirror_expression("unchained.mirror.capture.v1.keyB")
        self.assertEqual(default_expr, DRAIN_MIRROR_EXPRESSION)
        self.assertIn("unchained.mirror.capture.v1.keyB", custom_expr)
        self.assertNotIn(
            "Symbol.for('unchained.mirror.capture.v1')",
            custom_expr,
        )

    def test_two_different_keys_produce_different_expressions(self):
        key_a = "unchained.mirror.capture.v1.connA"
        key_b = "unchained.mirror.capture.v1.connB"
        self.assertNotEqual(
            build_install_mirror_expression(key_a),
            build_install_mirror_expression(key_b),
        )
        self.assertNotEqual(
            build_drain_mirror_expression(key_a),
            build_drain_mirror_expression(key_b),
        )

    def test_action_expression_uses_custom_mirror_key(self):
        expr = mirror_action_expression(
            {"targetId": "ucm-1", "kind": "scroll", "x": 0, "y": 500},
            expected_seq=3,
            expected_epoch="epoch-test",
            mirror_key="unchained.mirror.capture.v1.connA",
        )
        self.assertIn("unchained.mirror.capture.v1.connA", expr)
        self.assertNotIn(
            "Symbol.for('unchained.mirror.capture.v1')",
            expr,
        )

    def test_chunk_expression_uses_custom_mirror_key(self):
        expr = mirror_payload_chunk_expression(
            0, 1024, True, mirror_key="unchained.mirror.capture.v1.connA"
        )
        self.assertIn("unchained.mirror.capture.v1.connA", expr)

    def test_dispose_expression_targets_correct_key(self):
        expr = build_dispose_mirror_expression("unchained.mirror.capture.v1.connA")
        self.assertIn("unchained.mirror.capture.v1.connA", expr)
        self.assertIn(".dispose()", expr)

    def test_invalid_mirror_key_is_rejected(self):
        with self.assertRaises(ValueError):
            build_install_mirror_expression("unchained.mirror.capture.v1'; alert(1); '")
        with self.assertRaises(ValueError):
            build_drain_mirror_expression("bad key with spaces")

    def test_default_key_constant_matches_backward_compat(self):
        self.assertEqual(DEFAULT_MIRROR_KEY, "unchained.mirror.capture.v1")


class TestPerConnectionMirrorKeyStream(unittest.IsolatedAsyncioTestCase):
    """Verify that stream_semantic_mirror and execute_semantic_action pass the
    per-connection mirror_key through to the underlying CDP evaluations."""

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_stream_passes_custom_key_to_install_and_drain(self, run_js):
        snapshot = {"url": "https://example.test"}
        run_js.return_value = _encoded(snapshot)
        custom_key = "unchained.mirror.capture.v1.test123"
        stream = stream_semantic_mirror("agent", "tab", mirror_key=custom_key)

        await anext(stream)
        await stream.aclose()

        install_expr = run_js.await_args_list[0].args[2]
        self.assertIn(custom_key, install_expr)
        self.assertNotIn(f"Symbol.for('{DEFAULT_MIRROR_KEY}')", install_expr)

    @patch("web_app.semantic_mirror.cloud_tools.run_js", new_callable=AsyncMock)
    async def test_action_passes_custom_key(self, run_js):
        run_js.return_value = _encoded(
            {"ok": True, "reason": "ok", "navigated": False, "currentSeq": 3}
        )
        custom_key = "unchained.mirror.capture.v1.act456"
        await execute_semantic_action(
            "agent",
            "tab",
            {"targetId": "ucm-1", "kind": "scroll", "x": 0, "y": 500},
            expected_seq=3,
            expected_epoch="epoch-test",
            mirror_key=custom_key,
        )
        action_expr = run_js.await_args.args[2]
        self.assertIn(custom_key, action_expr)
        self.assertNotIn(f"Symbol.for('{DEFAULT_MIRROR_KEY}')", action_expr)


if __name__ == "__main__":
    unittest.main()
