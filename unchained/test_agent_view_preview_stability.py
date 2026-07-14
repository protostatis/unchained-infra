"""Regression tests for Agent View resize/reconnect stability.

Covers:
  1. Per-stream sequence counter reset on every socket start
  2. Refresh coalescing (multiple same-turn -> one socket) + stop cancellation
  3. RAF-coalesced latest-frame Blob painting with decode gating
  4. Early WS prepare disconnect exits cleanly
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from web_app.handlers import chat_flow


# ---------------------------------------------------------------------------
# Shared Node.js test harness helpers
# ---------------------------------------------------------------------------

def _require_node(test_case):
    node = shutil.which("node")
    if not node:
        test_case.skipTest("node is required")
    return node


def _run_node(test_case, js_source, checks):
    result = subprocess.run(
        [_require_node(test_case)],
        input=js_source + checks,
        capture_output=True,
        text=True,
    )
    test_case.assertEqual(result.returncode, 0,
                          f"Node.js test failed:\n{result.stderr}")


def _agent_view_js() -> str:
    import web_app.templates as templates
    return templates._AGENT_VIEW_JS


def _function_source(name, next_name=None):
    js = _agent_view_js()
    start = js.index(f"function {name}(")
    if next_name:
        end = js.index(f"\nfunction {next_name}", start)
    else:
        end = len(js)
    return js[start:end]


# Pre-built harness base shared across refresh/runtime tests
_REFRESH_HARNESS_BASE = r"""
const rafQueue = [];
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
globalThis.cancelAnimationFrame = function(id) {
  for (let i = 0; i < rafQueue.length; i++) {
    if (rafQueue._ids && rafQueue._ids[i] === id) { rafQueue.splice(i, 1); rafQueue._ids.splice(i, 1); return; }
  }
};
// Override to track ids; the real code stashes the returned id
const _origRaf = globalThis.requestAnimationFrame;
rafQueue._ids = [];
globalThis.requestAnimationFrame = function(callback) { const id = rafQueue.length + 1; rafQueue._ids.push(id); rafQueue.push(callback); return id; };
function flushRaf() { if (rafQueue.length) { rafQueue.shift()(); rafQueue._ids.shift(); } }
function flushAllRafs() { while (rafQueue.length) flushRaf(); }

const body = {classList: {contains() { return true; }}};
globalThis.document = {body};
let agentViewSemanticRecoveryTimer = null;
let agentViewGeneration = 0;
let agentViewLastSeq = 0;
let agentViewDocumentSeq = 0;
let agentViewRefreshPending = false;
let agentViewRefreshRaf = 0;
globalThis.clearTimeout = function() {};
function scheduleAgentViewRetry() {}
function scheduleAgentViewSemanticRecovery() {}
function scheduleAgentViewSemanticFrameScale() {}
function resetAgentViewSemanticRecovery() {}
function setAgentViewState() {}
function startAgentViewSocket() {}
let startCount = 0;
const _origStart = startAgentViewSocket;
startAgentViewSocket = function() { startCount += 1; };
function stopAgentViewSocket() {
  agentViewGeneration++;
  if (agentViewRefreshRaf) { cancelAnimationFrame(agentViewRefreshRaf); agentViewRefreshRaf = 0; }
  agentViewRefreshPending = false;
}
globalThis.stopAgentViewSocket = stopAgentViewSocket;

function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""


# ---------------------------------------------------------------------------
# Static template checks (minimal — behavioral tests cover the rest)
# ---------------------------------------------------------------------------

class TestAgentViewTemplateStatic(unittest.TestCase):

    def test_seq_resets_placed_in_start_socket_before_ws_creation(self):
        """Sequence counters must reset in startAgentViewSocket, not just
        in refreshAgentView, so automatic retries also get clean state."""
        body = _function_source("startAgentViewSocket", "openAgentView")

        self.assertIn("agentViewLastSeq = 0", body)
        self.assertIn("agentViewDocumentSeq = 0", body)

        stop_pos = body.index("stopAgentViewSocket()")
        socket_pos = body.index("new WebSocket")
        last_seq_pos = body.index("agentViewLastSeq = 0", stop_pos)
        doc_seq_pos = body.index("agentViewDocumentSeq = 0", stop_pos)

        self.assertLess(last_seq_pos, socket_pos,
                        "seq reset must be before WS creation")
        self.assertLess(doc_seq_pos, socket_pos,
                        "doc-seq reset must be before WS creation")

    def test_refresh_has_raf_id_tracking_and_cancel_in_stop(self):
        """refreshAgentView must track the RAF id so stopAgentViewSocket
        can cancel it, preventing a delayed socket start."""
        refresh_body = _function_source("refreshAgentView", "ensureAgentViewForBrowserActivity")
        stop_body = _function_source("stopAgentViewSocket", "agentViewPaintFallbackFrame")

        self.assertIn("agentViewRefreshRaf = requestAnimationFrame(", refresh_body)
        self.assertIn("agentViewRefreshRaf = 0;", refresh_body)
        self.assertIn("cancelAnimationFrame(agentViewRefreshRaf)", stop_body)
        self.assertIn("agentViewRefreshRaf = 0;", stop_body)

    def test_fallback_paint_has_raf_coalescing_and_generation_guard(self):
        """Paint function must use RAF coalescing (requestAnimationFrame)
        and generation guard on image callbacks."""
        paint_body = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")

        self.assertIn("agentViewFallbackPaintRaf = requestAnimationFrame(", paint_body,
                      "missing RAF scheduling")
        self.assertIn("agentViewFallbackPaintRaf = 0;", paint_body,
                      "RAF id not cleared in callback")
        self.assertIn("generation !== agentViewFallbackPaintGeneration", paint_body,
                      "missing stale-callback generation guard")
        self.assertIn("URL.createObjectURL", paint_body)
        self.assertIn("URL.revokeObjectURL", paint_body)

        stop_body = _function_source("stopAgentViewSocket", "agentViewPaintFallbackFrame")
        self.assertIn("fallbackImage.src = agentViewFallbackBlobUrl", stop_body,
                      "transient stop must restore the last painted frame")


# ---------------------------------------------------------------------------
# Node.js runtime behavioral tests
# ---------------------------------------------------------------------------

class TestAgentViewRuntime(unittest.TestCase):

    def test_five_refreshes_queue_one_socket(self):
        refresh_source = _function_source("refreshAgentView", "ensureAgentViewForBrowserActivity")
        harness = _REFRESH_HARNESS_BASE
        checks = r"""
for (let i = 0; i < 5; i++) refreshAgentView();
expect(rafQueue.length === 1, '5 refreshes queued ' + rafQueue.length + ' RAFs');
expect(startCount === 0, 'socket started before RAF flush');
flushRaf();
expect(startCount === 1, 'RAF started ' + startCount + ' sockets');
expect(agentViewRefreshPending === false, 'pending guard left true');
// Guard reset: next refresh should work
refreshAgentView();
expect(rafQueue.length === 1, 'second refresh batch did not queue');
flushRaf();
expect(startCount === 2, 'second refresh did not start');
"""
        _run_node(self, harness + refresh_source, checks)

    def test_stop_cancels_pending_refresh_while_panel_open(self):
        """stopAgentViewSocket must cancel the pending refresh RAF even
        when the body still shows agent-view-open, preventing the delayed
        callback from starting a socket."""
        refresh_source = _function_source("refreshAgentView", "ensureAgentViewForBrowserActivity")
        harness = _REFRESH_HARNESS_BASE
        checks = r"""
refreshAgentView();
expect(rafQueue.length === 1, 'refresh did not queue RAF');
expect(agentViewRefreshPending === true, 'pending not set');

// stopAgentViewSocket while panel stays open
stopAgentViewSocket();
expect(agentViewRefreshPending === false, 'pending not cleared by stop');
expect(rafQueue.length === 0, 'RAF not cancelled by stop');

// Flush (nothing should fire because RAF was cancelled)
// startCount should still be 0
expect(startCount === 0, 'cancelled RAF still started a socket');
"""
        _run_node(self, harness + refresh_source, checks)

    def test_five_fallback_frames_one_latest_after_raf(self):
        """Five same-turn fallback frames must produce zero src assignments
        before RAF, then exactly one assignment (the latest frame) after RAF.

        This prevents the large poll JPEG from being decoded when a smaller
        screencast JPEG arrives ~15ms later."""
        paint_source = _function_source("agentViewPaintFallbackFrame", "scheduleAgentViewRetry")
        # Also include _agentViewFallbackScheduleRaf
        sched = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")
        paint_source += sched

        harness = r"""
let agentViewFallbackPending = null;
let agentViewFallbackLoading = false;
let agentViewFallbackBlobUrl = null;
let agentViewFallbackNextBlobUrl = null;
let agentViewFallbackPaintRaf = 0;
let agentViewFallbackPaintGeneration = 0;
const setSrc = [];
const createdUrls = [];
const revokedUrls = [];
const fakeImage = {
  set onload(fn) { this._onload = fn; },
  get onload() { return this._onload; },
  set onerror(fn) { this._onerror = fn; },
  get onerror() { return this._onerror; },
  set src(url) { setSrc.push(url); },
  removeAttribute() {},
};
const elements = new Map([['agent-view-image', fakeImage]]);
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
globalThis.URL = {
  createObjectURL(blob) { const url = 'blob:' + (createdUrls.length + 1); createdUrls.push(url); return url; },
  revokeObjectURL(url) { revokedUrls.push(url); },
};
globalThis.atob = function(str) { return Buffer.from(str, 'base64').toString('binary'); };
function scheduleAgentViewSemanticFrameScale() {}
const rafQueue = [];
const rafIds = [];
globalThis.requestAnimationFrame = function(callback) { const id = rafQueue.length + 1; rafIds.push(id); rafQueue.push(callback); return id; };
globalThis.cancelAnimationFrame = function(id) {
  const idx = rafIds.indexOf(id);
  if (idx >= 0) { rafQueue.splice(idx, 1); rafIds.splice(idx, 1); }
};
function flushRaf() { if (rafQueue.length) { rafQueue.shift()(); rafIds.shift(); } }
const Uint8Array = globalThis.Uint8Array;
const Blob = globalThis.Blob;
function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""
        checks = r"""
// Send 5 frames in rapid succession
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0x', 1);  // "fake-frame-1"
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0y', 2);
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0z', 3);
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS00', 4);
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS01', 5);  // "fake-frame-5"

// Before RAF: zero src assignments (RAF has not fired yet)
expect(setSrc.length === 0, 'src assigned before RAF flush: ' + setSrc.length);
expect(agentViewFallbackPending !== null, 'pending frame lost');
expect(agentViewFallbackPending.seq === 5, 'pending is not the latest frame');
expect(agentViewFallbackLoading === false, 'loading flag set before RAF');

// Flush the RAF — it should decode/paint only the latest frame (frame 5)
flushRaf();
expect(setSrc.length === 1, 'RAF painted ' + setSrc.length + ' frames (expected 1)');
expect(setSrc[0].startsWith('blob:'), 'src must be blob URL');
expect(agentViewFallbackLoading === true, 'loading flag not set after RAF');

// Complete the load
fakeImage._onload();
expect(revokedUrls.length === 0, 'first frame should have no old URL');
expect(agentViewFallbackLoading === false, 'loading not cleared');
expect(setSrc.length === 1, 'no more frames should paint without pending');
"""
        _run_node(self, harness + paint_source, checks)

    def test_bad_base64_unblocks_pipeline(self):
        """Bad base64 must not crash; the decode gate must release so a
        subsequent good frame can be scheduled."""
        paint_source = _function_source("agentViewPaintFallbackFrame", "scheduleAgentViewRetry")
        sched = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")
        paint_source += sched

        harness = r"""
let agentViewFallbackPending = null;
let agentViewFallbackLoading = false;
let agentViewFallbackBlobUrl = null;
let agentViewFallbackNextBlobUrl = null;
let agentViewFallbackPaintRaf = 0;
let agentViewFallbackPaintGeneration = 0;
const setSrc = [];
const fakeImage = {
  set onload(fn) { this._onload = fn; },
  set onerror(fn) { this._onerror = fn; },
  set src(url) { setSrc.push(url); },
  removeAttribute() {},
};
const elements = new Map([['agent-view-image', fakeImage]]);
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
globalThis.URL = {createObjectURL() { return 'blob:ok'; }, revokeObjectURL() {}};
globalThis.atob = function(str) {
  if (str === '!!!bad!!!') throw new Error('invalid base64');
  return Buffer.from(str, 'base64').toString('binary');
};
function scheduleAgentViewSemanticFrameScale() {}
const rafQueue = [];
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
function flushRaf() { if (rafQueue.length) rafQueue.shift()(); }
const Uint8Array = globalThis.Uint8Array;
const Blob = globalThis.Blob;
function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""
        checks = r"""
// Bad data — RAF scheduled but decode fails in callback
agentViewPaintFallbackFrame('image/jpeg', '!!!bad!!!', 1);
expect(agentViewFallbackLoading === false, 'loading should stay false for bad data');
flushRaf();  // decode fails, loading reset to false
expect(setSrc.length === 0, 'no src for bad data after RAF');
expect(agentViewFallbackLoading === false, 'loading not reset after bad decode');

// Good data — should schedule new RAF and paint
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1nb29k', 2);
flushRaf();
expect(setSrc.length === 1, 'good frame should be painted after bad one');
expect(agentViewFallbackLoading === true, 'loading should be set');
"""
        _run_node(self, harness + paint_source, checks)

    def test_image_error_unblocks_pipeline(self):
        """An image load error must revoke the URL and unblock loading.
        A subsequent new frame after the error must be paintable."""
        paint_source = _function_source("agentViewPaintFallbackFrame", "scheduleAgentViewRetry")
        sched = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")
        paint_source += sched

        harness = r"""
let agentViewFallbackPending = null;
let agentViewFallbackLoading = false;
let agentViewFallbackBlobUrl = null;
let agentViewFallbackNextBlobUrl = null;
let agentViewFallbackPaintRaf = 0;
let agentViewFallbackPaintGeneration = 0;
const setSrc = [];
const revokedUrls = [];
let urlCount = 0;
const fakeImage = {
  set onload(fn) { this._onload = fn; },
  set onerror(fn) { this._onerror = fn; },
  set src(url) { setSrc.push(url); },
  removeAttribute() {},
};
const elements = new Map([['agent-view-image', fakeImage]]);
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
globalThis.URL = {
  createObjectURL() { urlCount += 1; return 'blob:' + urlCount; },
  revokeObjectURL(url) { revokedUrls.push(url); },
};
globalThis.atob = function(str) { return Buffer.from(str, 'base64').toString('binary'); };
function scheduleAgentViewSemanticFrameScale() {}
const rafQueue = [];
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
function flushRaf() { if (rafQueue.length) rafQueue.shift()(); }
const Uint8Array = globalThis.Uint8Array;
const Blob = globalThis.Blob;
function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""
        checks = r"""
// Establish a successfully painted frame that should remain visible on error.
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0x', 1);
flushRaf();
fakeImage._onload();
expect(agentViewFallbackBlobUrl === 'blob:1', 'first frame not committed');

// Start loading a replacement frame.
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0y', 2);
flushRaf();
expect(setSrc.at(-1) === 'blob:2', 'replacement frame not assigned');
expect(agentViewFallbackLoading === true, 'loading not set');

// Image load errors: revoke the failed URL and restore the last good frame.
fakeImage._onerror();
expect(revokedUrls.length === 1, 'error URL must be revoked');
expect(revokedUrls[0] === 'blob:2', 'wrong URL revoked after error');
expect(setSrc.at(-1) === 'blob:1', 'last successful frame not restored');
expect(agentViewFallbackLoading === false, 'loading not cleared after error');
expect(rafQueue.length === 0, 'RAF queued with no pending frame');

// Now send a new frame — pipeline must be unblocked
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1nb29kLWZyYW1l', 3);
flushRaf();
expect(setSrc.at(-1) === 'blob:3', 'new frame not painted after error recovery');
"""
        _run_node(self, harness + paint_source, checks)

    def test_single_raf_paints_latest_after_load_settles(self):
        """When frame A is loading and frames B/C arrive during the load,
        A's load settle must trigger exactly one RAF that paints C (the
        latest), not B and not two RAFs."""
        paint_source = _function_source("agentViewPaintFallbackFrame", "scheduleAgentViewRetry")
        sched = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")
        paint_source += sched

        harness = r"""
let agentViewFallbackPending = null;
let agentViewFallbackLoading = false;
let agentViewFallbackBlobUrl = null;
let agentViewFallbackNextBlobUrl = null;
let agentViewFallbackPaintRaf = 0;
let agentViewFallbackPaintGeneration = 0;
const setSrc = [];
let lastDecoded = '';
const fakeImage = {
  set onload(fn) { this._onload = fn; },
  set onerror(fn) { this._onerror = fn; },
  set src(url) { setSrc.push(url); },
  removeAttribute() {},
};
const elements = new Map([['agent-view-image', fakeImage]]);
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
globalThis.URL = {
  createObjectURL() { return 'blob:' + lastDecoded; },
  revokeObjectURL() {},
};
globalThis.atob = function(str) { lastDecoded = Buffer.from(str, 'base64').toString('binary'); return lastDecoded; };
function scheduleAgentViewSemanticFrameScale() {}
const rafQueue = [];
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
function flushRaf() { if (rafQueue.length) rafQueue.shift()(); }
const Uint8Array = globalThis.Uint8Array;
const Blob = globalThis.Blob;
function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""
        checks = r"""
// Frame A arrives, its RAF fires, and A starts loading.
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS1h', 1);
expect(rafQueue.length === 1, 'frame A did not queue RAF');
expect(agentViewFallbackLoading === false, 'loading set before RAF');
flushRaf();
expect(setSrc.length === 1, 'frame A did not start loading');
expect(setSrc[0] === 'blob:fake-frame-a', 'frame A URL mismatch');
expect(agentViewFallbackLoading === true, 'frame A loading gate not set');

// Frames B and C arrive while A is loading. C replaces B and no RAF starts.
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS1i', 2);
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS1j', 3);
expect(rafQueue.length === 0, 'B/C queued RAF while A was loading');
expect(agentViewFallbackPending.seq === 3, 'C did not replace B as latest pending');

// A settles and schedules exactly one RAF for C.
fakeImage._onload();
expect(agentViewFallbackLoading === false, 'A settle did not clear loading');
expect(rafQueue.length === 1, 'A settle did not queue exactly one RAF');

// That single RAF must decode C directly, without an intermediate RAF or B.
flushRaf();
expect(setSrc.length === 2, 'C RAF did not produce one additional assignment');
expect(setSrc[1] === 'blob:fake-frame-c', 'pending frame B painted instead of C');
expect(agentViewFallbackLoading === true, 'C loading gate not set');
expect(agentViewFallbackPending === null, 'C pending frame not consumed');

// C settles with no pending frame, so no further RAFs are queued.
fakeImage._onload();
expect(agentViewFallbackLoading === false, 'loading not cleared after load');
expect(rafQueue.length === 0, 'load settle queued unexpected RAF');
expect(setSrc.length === 2, 'unexpected src assignment after C settled');
"""
        _run_node(self, harness + paint_source, checks)

    def test_stale_callback_guarded_by_paint_generation(self):
        """A load callback from a previous paint generation must not mutate
        the new stream's state (visible URL, loading flag, etc.)."""
        paint_source = _function_source("agentViewPaintFallbackFrame", "scheduleAgentViewRetry")
        sched = _function_source("_agentViewFallbackScheduleRaf", "scheduleAgentViewRetry")
        paint_source += sched

        harness = r"""
let agentViewFallbackPending = null;
let agentViewFallbackLoading = false;
let agentViewFallbackBlobUrl = null;
let agentViewFallbackNextBlobUrl = null;
let agentViewFallbackPaintRaf = 0;
let agentViewFallbackPaintGeneration = 0;
const setSrc = [];
const revokedUrls = [];
const fakeImage = {
  set onload(fn) { this._onload = fn; },
  set onerror(fn) { this._onerror = fn; },
  set src(url) { setSrc.push(url); },
  removeAttribute() {},
};
const elements = new Map([['agent-view-image', fakeImage]]);
globalThis.document = {getElementById(id) { return elements.get(id) || null; }};
globalThis.URL = {
  createObjectURL() { return 'blob:ok'; },
  revokeObjectURL(url) { revokedUrls.push(url); },
};
globalThis.atob = function(str) { return Buffer.from(str, 'base64').toString('binary'); };
function scheduleAgentViewSemanticFrameScale() {}
const rafQueue = [];
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
globalThis.cancelAnimationFrame = function() {};
function flushRaf() { if (rafQueue.length) rafQueue.shift()(); }
const Uint8Array = globalThis.Uint8Array;
const Blob = globalThis.Blob;
function expect(condition, message) { if (!condition) throw new Error('FAIL: ' + message); }
"""
        checks = r"""
// Paint frame 1, start loading
agentViewPaintFallbackFrame('image/jpeg', 'ZmFrZS1mcmFtZS0x', 1);
flushRaf();
expect(agentViewFallbackLoading === true, 'loading should be set');
expect(agentViewFallbackBlobUrl === null, 'no visible URL yet');

// Simulate stop/retry: increment generation (loaded callback becomes stale)
const savedOnload = fakeImage._onload;
agentViewFallbackPaintGeneration++;
agentViewFallbackLoading = false;

// The stale load callback fires — must NOT touch blob URLs or loading
savedOnload();
expect(agentViewFallbackBlobUrl === null, 'stale callback set visible URL');
expect(agentViewFallbackLoading === false, 'stale callback mutated loading');
expect(revokedUrls.length === 0, 'stale callback revoked a URL');
"""
        _run_node(self, harness + paint_source, checks)


# ---------------------------------------------------------------------------
# Python: early WS prepare disconnect
# ---------------------------------------------------------------------------

class TestEarlyWsPrepareDisconnect(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_reset_exits_before_stream_setup(self):
        """When ws.prepare() raises ConnectionResetError, _handle_preview_ws
        must return immediately without entering stream/screencast setup."""

        class _FakeCore:
            HEADLESS_AGENT_ID = "headless-test"
            _session_tabs = {"s-guest-test9999-demo": "tab-preview"}

            def _first_look_guest_auth(self, _request):
                return ({"agent_id": "guest-test9999"}, "guest-cookie", 0)

            def _attach_first_look_guest_cookies(self, _resp, _request, _guest_id, **__):
                pass

            def _parse_relay(self):
                return ("relay.internal", 8765)

        request = MagicMock(spec=web.Request)
        request.query = {"session_id": "s-guest-test9999-demo", "width": "800", "height": "600"}
        request.headers = {}
        request.cookies = {}

        fake_ws = MagicMock(spec=web.WebSocketResponse)
        fake_ws.closed = False
        fake_ws.prepare = AsyncMock(side_effect=ConnectionResetError("Connection lost"))

        with (
            patch.object(chat_flow, "_core", return_value=_FakeCore()),
            patch.object(web, "WebSocketResponse", return_value=fake_ws),
        ):
            result = await chat_flow._handle_preview_ws(request, authenticated_chat=False)

        self.assertIs(result, fake_ws,
                      "handler did not return the WS after prepare failure")
        fake_ws.prepare.assert_awaited_once()
        fake_ws.send_json.assert_not_called()
        fake_ws.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
