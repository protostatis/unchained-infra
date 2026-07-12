#!/usr/bin/env python3
"""Live integration test for the multi-client Agent View scroll fix.

This script spins up a temporary web server with a mocked private-core and
verifies that two concurrent Agent View WebSocket connections to the same
chat session:

1. Both stay alive (neither kills the other — no generation war)
2. Each receives its own mirror key (no shared mirror state)
3. A scroll action from one connection succeeds (no stale-document rejection)

This reproduces the "page resets to top after a second" bug scenario:
  - Before the fix: the second connection's INSTALL disposes the first
    connection's mirror, causing epoch mismatches and scroll action failures.
  - After the fix: each connection has its own mirror key, so mirrors coexist.

Run:
    uv run python test_multi_client_scroll.py
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

# We need to set up the mock before importing the handler.
import cloud_tools
from aiohttp import web, test_utils
import web_app.handlers.chat_flow as chat_flow
from web_app import semantic_mirror


# ── Fake core that simulates an authenticated user with a resolved tab ──────

FAKE_SESSION_ID = "s-claude-aaa11111-demo"
FAKE_AGENT_ID = "claude-testagent"
FAKE_TAB_ID = "T" * 32  # 32-char hex-looking tab
FAKE_KEY_HASH = "aaa11111"
FAKE_EPOCH = "epoch-fixed-1234"


class FakeCore:
    """Minimal fake core that passes the preview WS auth checks."""

    def __init__(self):
        self.authenticated = True
        self._session_tabs = {FAKE_SESSION_ID: FAKE_TAB_ID}
        self._session_agent_map = {FAKE_SESSION_ID: FAKE_AGENT_ID}
        self._session_allowed_tabs = {FAKE_SESSION_ID: {FAKE_TAB_ID}}
        self._source_operation_locks = {}
        self._session_last_active = {}

    def _authenticate(self, request):
        return {
            "authenticated": True,
            "user_id": "u-test",
            "email": "test@localhost",
            "key_hash": FAKE_KEY_HASH,
            "agent_id": FAKE_AGENT_ID,
        }

    def _parse_relay(self):
        return ("127.0.0.1", 8765)

    async def _resolve_bridge_agent(self, auth_info, slot):
        return {"bridge_agent_id": FAKE_AGENT_ID}

    def _same_origin_preview_request(self, request):
        return True


# ── Test ────────────────────────────────────────────────────────────────────

class TestMultiClientScrollFix(test_utils.AioHTTPTestCase):
    """Verify two concurrent Agent View connections coexist and scroll works."""

    async def get_application(self):
        self._original_core = chat_flow._core
        self.fake_core = FakeCore()
        chat_flow._core = lambda: self.fake_core

        app = web.Application()
        app.router.add_get("/ws", chat_flow.handle_chat_preview_ws)
        return app

    async def tearDownAsync(self):
        chat_flow._core = self._original_core
        await super().tearDownAsync()

    async def test_two_clients_coexist_and_scroll_succeeds(self):
        """The core regression test for the scroll-reset-to-top bug."""

        # Track what mirror keys and epochs each connection's stream uses.
        seen_keys = []
        seen_epochs = []
        scroll_action_results = []
        original_execute = semantic_mirror.execute_semantic_action

        async def fake_stream(agent_id, tab_id, **kwargs):
            """Emit a snapshot, then keep the stream alive until the test ends."""
            key = kwargs.get("mirror_key", "")
            seen_keys.append(key)
            yield {
                "type": "snapshot",
                "snapshot": {
                    "captureEpoch": FAKE_EPOCH,
                    "url": "https://example.test",
                    "body": '<main data-ucm-id="ucm-1">Page</main>',
                    "viewport": {"scrollX": 0, "scrollY": 0},
                    "scrollPositions": [],
                    "fidelity": {"truncated": False},
                },
                "resync": False,
            }
            # Block forever (until the test closes the WS).
            await asyncio.Event().wait()

        async def fake_execute(agent_id, tab_id, action, **kwargs):
            """Simulate a successful scroll action."""
            key = kwargs.get("mirror_key", "default")
            result = {
                "ok": True,
                "reason": "ok",
                "navigated": False,
                "actionKind": action.get("kind", ""),
                "targetId": action.get("targetId", ""),
            }
            if action.get("kind") == "scroll":
                result["x"] = action.get("x", 0)
                result["y"] = action.get("y", 0)
                scroll_action_results.append({
                    "key": key,
                    "epoch": kwargs.get("expected_epoch", ""),
                    "y": action.get("y", 0),
                })
            return result

        with patch.object(semantic_mirror, "stream_semantic_mirror", fake_stream), \
             patch.object(semantic_mirror, "execute_semantic_action", fake_execute), \
             patch.object(cloud_tools, "run_js", new_callable=AsyncMock) as mock_run_js, \
             patch.object(cloud_tools, "run_cdp_command", new_callable=AsyncMock) as mock_cdp:

            # run_cdp for Target.getTargetInfo (tab resolution)
            mock_cdp.return_value = {"targetInfo": {"targetId": FAKE_TAB_ID}}
            # run_js for mirror disposal on close
            mock_run_js.return_value = json.dumps("{}")

            print("\n1. Opening first Agent View connection (simulating phone)...")
            ws1 = await self.client.ws_connect(
                f"/ws?session_id={FAKE_SESSION_ID}"
            )
            attached1 = await ws1.receive_json()
            snapshot1 = await ws1.receive_json()
            print(f"   ✓ Connected: mode={attached1['mode']}, interaction={attached1['interaction']}")
            print(f"   ✓ Snapshot received: epoch={snapshot1['capture_epoch']}")
            print(f"   ✓ Mirror key: {seen_keys[0]}")

            print("\n2. Opening second Agent View connection (simulating desktop)...")
            ws2 = await self.client.ws_connect(
                f"/ws?session_id={FAKE_SESSION_ID}"
            )
            attached2 = await ws2.receive_json()
            snapshot2 = await ws2.receive_json()
            print(f"   ✓ Connected: mode={attached2['mode']}, interaction={attached2['interaction']}")
            print(f"   ✓ Snapshot received: epoch={snapshot2['capture_epoch']}")
            print(f"   ✓ Mirror key: {seen_keys[1]}")

            print("\n3. Verifying both connections are alive...")
            self.assertFalse(
                ws1.closed,
                "FAIL: First connection was killed when second connected! "
                "(This is the generation-war bug.)",
            )
            self.assertFalse(
                ws2.closed,
                "FAIL: Second connection did not survive!",
            )
            print("   ✓ Both connections are alive — no generation war!")

            print("\n4. Verifying mirror keys are unique per connection...")
            self.assertNotEqual(
                seen_keys[0],
                seen_keys[1],
                "FAIL: Both connections share the same mirror key! "
                "(This is the shared-symbol bug that causes scroll resets.)",
            )
            self.assertTrue(
                all(k.startswith("unchained.mirror.capture.v1.") for k in seen_keys),
                f"FAIL: Mirror keys should be per-connection: {seen_keys}",
            )
            print(f"   ✓ Keys are unique: {seen_keys[0]} ≠ {seen_keys[1]}")

            print("\n5. Sending a scroll action from the first connection (phone)...")
            await ws1.send_json({
                "type": "preview.action",
                "action_id": "scroll_test_001",
                "mirror_id": snapshot1["mirror_id"],
                "capture_epoch": snapshot1["capture_epoch"],
                "document_seq": snapshot1["document_seq"],
                "action": {
                    "targetId": "document",
                    "kind": "scroll",
                    "x": 0,
                    "y": 500,
                },
            })
            result1 = await ws1.receive_json()
            print(f"   ✓ Scroll action result: ok={result1['ok']}, y={result1.get('y')}")
            self.assertTrue(
                result1["ok"],
                f"FAIL: Scroll action was rejected! reason={result1.get('reason')} "
                "(This is the stale-document/preview-superseded bug.)",
            )

            print("\n6. Sending a scroll action from the second connection (desktop)...")
            await ws2.send_json({
                "type": "preview.action",
                "action_id": "scroll_test_002",
                "mirror_id": snapshot2["mirror_id"],
                "capture_epoch": snapshot2["capture_epoch"],
                "document_seq": snapshot2["document_seq"],
                "action": {
                    "targetId": "document",
                    "kind": "scroll",
                    "x": 0,
                    "y": 800,
                },
            })
            result2 = await ws2.receive_json()
            print(f"   ✓ Scroll action result: ok={result2['ok']}, y={result2.get('y')}")
            self.assertTrue(
                result2["ok"],
                f"FAIL: Second scroll action was rejected! reason={result2.get('reason')}",
            )

            print("\n7. Verifying both scroll actions succeeded with correct keys...")
            self.assertEqual(len(scroll_action_results), 2)
            self.assertEqual(scroll_action_results[0]["y"], 500)
            self.assertEqual(scroll_action_results[1]["y"], 800)
            self.assertNotEqual(
                scroll_action_results[0]["key"],
                scroll_action_results[1]["key"],
                "Scroll actions should use different mirror keys per connection.",
            )
            print(f"   ✓ Phone scrolled to y=500 with key {scroll_action_results[0]['key'][-12:]}")
            print(f"   ✓ Desktop scrolled to y=800 with key {scroll_action_results[1]['key'][-12:]}")

            print("\n8. Cleaning up...")
            await ws1.close()
            await ws2.close()

            print("\n" + "=" * 60)
            print(" ALL CHECKS PASSED — multi-client scroll fix verified!")
            print("=" * 60)
            print()
            print(" Summary:")
            print(" • Two concurrent Agent View connections coexist")
            print(" • Each has its own isolated mirror (unique Symbol.for key)")
            print(" • Scroll actions succeed from both connections")
            print(" • No generation war, no stale-document rejections")
            print()


if __name__ == "__main__":
    unittest.main(verbosity=2)
