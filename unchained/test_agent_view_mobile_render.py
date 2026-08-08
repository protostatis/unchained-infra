"""Optional local Chrome smoke test for mobile-sized Agent View rendering.

The test drives the real Agent View template in headless desktop Chrome with
iPhone-sized CDP metrics. A local fake preview socket repeatedly sends large
semantic snapshots while the viewport changes between portrait, keyboard-sized,
and landscape dimensions.

It is deliberately not an iOS crash reproduction: Chrome on iOS uses WebKit,
while this tool uses desktop Chrome's renderer. Run it locally before validating
Agent View changes on a physical phone:

    uv run python test_agent_view_mobile_render.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from aiohttp import web
import websockets


_IPHONE_16_PRO_MAX = {
    "width": 440,
    "height": 956,
    "deviceScaleFactor": 3,
    "mobile": True,
    "screenWidth": 440,
    "screenHeight": 956,
}
_CDP_ORIGIN = "http://127.0.0.1"


def _chrome_binary() -> str | None:
    candidates = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    )
    return next((path for path in candidates if path and Path(path).is_file()), None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chrome_stderr_tail(path: Path, *, max_chars: int = 8_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""


def _render_harness_html() -> str:
    from web_app.templates import _AGENT_VIEW_JS, _AGENT_VIEW_PANEL, _AGENT_VIEW_STYLE

    runtime = r"""
<script>
try { Object.defineProperty(navigator, 'maxTouchPoints', {configurable:true, value:5}); } catch (_err) {}
window.__previewSocketCount = 0;
window.__previewSnapshots = 0;
window.__previewRequest = '';

function largeSnapshot(index) {
  let cards = '';
  for (let item = 0; item < 2600; item += 1) {
    cards += '<article class="card" data-ucm-id="card-' + index + '-' + item + '"><h2>Snapshot ' + index + ' / card ' + item + '</h2><p>Mobile semantic render stress content.</p></article>';
  }
  const body = '<main data-ucm-id="main-' + index + '">' + cards + '</main>';
  return {
    url: 'https://source.example.test/snapshot/' + index,
    doctype: '<!DOCTYPE html>',
    htmlAttrs: {lang:'en'},
    bodyAttrs: {},
    head: '<style>body{margin:0;font-family:system-ui}.card{padding:8px;margin:4px;border:1px solid #456;background:#eef;color:#123}</style>',
    body: body,
    adoptedStyles: [],
    viewport: {width:440,height:956,scrollX:0,scrollY:0},
    scrollPositions: [],
    fidelity: {capturedHeadBytes:128,capturedBodyBytes:body.length,criticalStyleBytes:0}
  };
}

class FakePreviewSocket {
  // This mirrors the WebSocket surface currently exercised by Agent View:
  // ready-state constants, readyState, send(), close(), and lifecycle callbacks.
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakePreviewSocket.CONNECTING;
    this.binaryType = 'blob';
    this.bufferedAmount = 0;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    this.closed = false;
    this.timer = 0;
    window.__previewRequest = url;
    window.__previewSocketCount += 1;
    setTimeout(() => this.start(), 10);
  }
  emit(event) {
    if (!this.closed && this.onmessage) this.onmessage({data:JSON.stringify(event)});
  }
  start() {
    if (this.closed) return;
    this.readyState = FakePreviewSocket.OPEN;
    if (this.onopen) this.onopen();
    this.emit({type:'preview.attached', mode:'semantic'});
    let sequence = 0;
    this.timer = setInterval(() => {
      if (this.closed) return;
      sequence += 1;
      window.__previewSnapshots += 1;
      this.emit({
        type:'preview.semantic.snapshot',
        snapshot:largeSnapshot(sequence),
        seq:sequence,
        mirror_id:'mirror-' + sequence,
        capture_epoch:'epoch-' + sequence,
        document_seq:0,
        resync:sequence > 1
      });
      if (sequence >= 8) clearInterval(this.timer);
    }, 150);
  }
  send() {}
  close() {
    this.readyState = FakePreviewSocket.CLOSING;
    this.closed = true;
    clearInterval(this.timer);
    this.readyState = FakePreviewSocket.CLOSED;
    if (this.onclose) this.onclose();
  }
}
window.WebSocket = FakePreviewSocket;
// No chat turn runs in this harness, but the assembled template references
// these chat helpers during initialization.
function addUserBubble() {}
function appendText() {}
function updateAgentStatusUI() {}
let sessionId = 's-mobile-render-test';
let sending = false;
</script>
"""
    boot = "<script>openAgentView();</script>"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        + _AGENT_VIEW_STYLE
        + "</head><body>"
        + runtime
        + _AGENT_VIEW_PANEL
        + "<script>"
        + _AGENT_VIEW_JS
        + "</script>"
        + boot
        + "</body></html>"
    )


class _Cdp:
    def __init__(self, socket_client):
        self.socket = socket_client
        self.next_id = 0

    async def command(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message_id = self.next_id
        await self.socket.send(json.dumps({
            "id": message_id,
            "method": method,
            "params": params or {},
        }))
        while True:
            response = json.loads(await self.socket.recv())
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise RuntimeError(f"CDP {method} failed: {response['error']}")
            return response.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.command("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        payload = result.get("result", {})
        if "value" not in payload:
            raise RuntimeError(f"evaluation did not return a value: {payload}")
        return payload["value"]


async def _chrome_ws_url(port: int) -> str:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                targets = json.load(response)
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except OSError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("Chrome DevTools endpoint did not become ready")


async def _wait_for(cdp: _Cdp, expression: str, *, timeout: float = 10):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await cdp.evaluate(expression)
        if last:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for browser condition: {expression}; last={last!r}")


@unittest.skipUnless(_chrome_binary(), "Google Chrome or Chromium is not installed")
class TestAgentViewMobileRender(unittest.TestCase):
    def test_mobile_sized_semantic_render_survives_snapshot_and_viewport_stress(self):
        asyncio.run(self._run_mobile_render_smoke())

    async def _run_mobile_render_smoke(self) -> None:
        chrome = _chrome_binary()
        assert chrome is not None
        app = web.Application()
        html = _render_harness_html()

        async def page(_request: web.Request) -> web.Response:
            return web.Response(text=html, content_type="text/html")

        app.router.add_get("/{tail:.*}", page)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        addresses = runner.addresses
        assert addresses
        page_port = int(addresses[0][1])

        debug_port = _free_port()
        with tempfile.TemporaryDirectory(prefix="unchained-mobile-agent-view-") as profile_dir:
            stderr_path = Path(profile_dir) / "chrome.stderr.log"
            with stderr_path.open("w", encoding="utf-8") as chrome_stderr:
                process: subprocess.Popen | None = None
                try:
                    process = subprocess.Popen(
                        [
                            chrome,
                            "--headless=new",
                            "--remote-debugging-address=127.0.0.1",
                            f"--remote-debugging-port={debug_port}",
                            f"--remote-allow-origins={_CDP_ORIGIN}",
                            f"--user-data-dir={profile_dir}",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=chrome_stderr,
                    )
                    ws_url = await _chrome_ws_url(debug_port)
                    async with websockets.connect(
                        ws_url,
                        origin=_CDP_ORIGIN,
                        max_size=8 * 1024 * 1024,
                    ) as socket_client:
                        cdp = _Cdp(socket_client)
                        await cdp.command("Page.enable")
                        await cdp.command("Emulation.setDeviceMetricsOverride", _IPHONE_16_PRO_MAX)
                        await cdp.command("Emulation.setTouchEmulationEnabled", {
                            "enabled": True,
                            "maxTouchPoints": 5,
                        })
                        await cdp.command(
                            "Page.navigate",
                            {"url": f"http://127.0.0.1:{page_port}/"},
                        )
                        await _wait_for(cdp, "window.__previewSocketCount === 1")

                        async def assert_browser_alive(stage: str) -> None:
                            self.assertIsNone(
                                process.poll(),
                                f"desktop Chrome renderer exited during {stage}",
                            )
                            health = await cdp.evaluate("""({
                              readyState: document.readyState,
                              socketCount: window.__previewSocketCount,
                            })""")
                            self.assertEqual(health["readyState"], "complete", stage)
                            self.assertEqual(health["socketCount"], 1, stage)

                        landscape = dict(_IPHONE_16_PRO_MAX)
                        landscape.update({
                            "width": 956,
                            "height": 440,
                            "screenWidth": 956,
                            "screenHeight": 440,
                        })
                        keyboard = dict(_IPHONE_16_PRO_MAX)
                        keyboard["height"] = 560
                        for iteration in range(4):
                            await cdp.command("Emulation.setDeviceMetricsOverride", landscape)
                            await asyncio.sleep(0.1)
                            await assert_browser_alive(f"landscape transition {iteration + 1}")
                            await cdp.command("Emulation.setDeviceMetricsOverride", keyboard)
                            await asyncio.sleep(0.1)
                            await assert_browser_alive(f"keyboard transition {iteration + 1}")
                            await cdp.command("Emulation.setDeviceMetricsOverride", _IPHONE_16_PRO_MAX)
                            await asyncio.sleep(0.1)
                            await assert_browser_alive(f"portrait transition {iteration + 1}")

                        final_state = await _wait_for(cdp, """(() => {
                          const canvas = document.getElementById('agent-view-canvas');
                          const activeFrames = [...document.querySelectorAll('.agent-view-semantic-frame')]
                            .filter(frame => frame.classList.contains('active') && frame.hasAttribute('srcdoc'));
                          const renderedCards = activeFrames[0] && activeFrames[0].contentDocument
                            ? activeFrames[0].contentDocument.querySelectorAll('.card').length : 0;
                          return canvas && canvas.classList.contains('has-semantic') &&
                            window.__previewSnapshots >= 8 && activeFrames.length && renderedCards >= 2600 ? {
                            snapshots: window.__previewSnapshots,
                            sockets: window.__previewSocketCount,
                            activeFrames: activeFrames.length,
                            renderedCards: renderedCards,
                          } : null;
                        })()""", timeout=30)
                        self.assertGreaterEqual(final_state["snapshots"], 8)
                        self.assertEqual(final_state["sockets"], 1)
                        self.assertEqual(final_state["activeFrames"], 1)
                        self.assertGreaterEqual(final_state["renderedCards"], 2_600)
                        screenshot = await cdp.command("Page.captureScreenshot", {"format": "png"})
                        self.assertGreater(len(str(screenshot.get("data", ""))), 1_000)
                        self.assertIsNone(process.poll(), "desktop Chrome renderer exited during the stress run")
                except Exception as exc:
                    chrome_stderr.flush()
                    diagnostics = _chrome_stderr_tail(stderr_path)
                    if diagnostics:
                        raise AssertionError(
                            f"{exc}\n\nChrome stderr tail:\n{diagnostics}"
                        ) from exc
                    raise
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:
                            await asyncio.to_thread(process.wait, 5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            await asyncio.to_thread(process.wait, 5)
                    with contextlib.suppress(Exception):
                        await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
