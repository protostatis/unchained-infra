#!/usr/bin/env python3
"""
agent_stream.py — AI-orchestrated live travel show

Claude acts as the show host, deciding every ~2 minutes where to go next.
Can navigate Google Maps Street View or browse the web to entertain audiences
and showcase unchainedsky.com's browser agent capabilities.

Usage:
    uv run agent_stream.py                         # auto-detect tabs
    uv run agent_stream.py --obs                   # auto-start OBS stream
    uv run agent_stream.py --start "Kyoto Japan"   # starting location
    uv run agent_stream.py --interval 120          # agent call interval (secs)
    uv run agent_stream.py --no-chat               # skip YouTube chat polling
    uv run agent_stream.py --no-ctl                # don't auto-open control terminal
    uv run agent_stream.py --obs-password secret   # OBS WebSocket password

OBS setup:
    1. Tools → WebSocket Server Settings → enable, set password
    2. Add Browser Source → http://127.0.0.1:8878/overlay
    3. Set width=860, height=145, transparent background
    4. Position at the bottom of your scene
"""

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import random
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp as _cdp_mod
from cdp import CDP, _list_tabs, _create_new_tab, _get_ws_url_for_tab, _select_profile

# ── TTS (Edge TTS — free, no tokens) ─────────────────────────────────────────
TTS_VOICE = os.environ.get("TTS_VOICE", "en-GB-RyanNeural")
_tts_lock = asyncio.Lock()
_tts_speaking = False

async def speak(text: str):
    """Generate TTS audio and play it via PulseAudio (ffplay) for reliable stream output."""
    global _tts_speaking
    if not text or len(text) < 5:
        return
    async with _tts_lock:
        _tts_speaking = True
        try:
            import edge_tts
            tmp = os.path.join(tempfile.gettempdir(), "unchained_tts.mp3")
            tts = edge_tts.Communicate(text, voice=TTS_VOICE)
            await tts.save(tmp)

            # Play directly through PulseAudio — bypasses Chrome audio stack entirely
            env = os.environ.copy()
            env["PULSE_SERVER"] = "unix:/var/run/pulse/native"
            proc = await asyncio.create_subprocess_exec(
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", tmp,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            # Brief pause after speech
            await asyncio.sleep(0.6)
            print(f"[tts] spoke: {text[:60]}", flush=True)
        except Exception as e:
            print(f"[tts] error: {e}", flush=True)
        finally:
            _tts_speaking = False

# Load .env.obs if present
_env_obs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.obs")
if os.path.exists(_env_obs):
    with open(_env_obs) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
OVERLAY_PORT    = 8878          # separate port from maps_stream.py (8877)
AGENT_INTERVAL  = 90            # seconds between Claude decisions
STEP_DELAY      = 2.0           # seconds between Street View steps
CHAT_POLL_SECS  = 6             # seconds between YouTube chat reads
MAX_STEPS       = 30            # force agent call after this many steps (safety fallback)
STEPS_PER_SPOT  = 4             # steps before moving to next attraction within a city
SPOTS_PER_CITY  = 4             # how many attractions to visit per city
VISITED_EXPIRY_HOURS = 12       # destinations are "fresh" for 12h, then eligible again
FAIL_BACKOFF    = 4
DEFAULT_START   = "Times Square, New York"
MAPS_BASE       = "https://www.google.com/maps"
OBS_HOST        = os.environ.get("OBS_HOST", "127.0.0.1")
OBS_PORT        = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD    = os.environ.get("OBS_PASSWORD", "")
SOCKET_PATH     = os.path.expanduser("~/.unchained/stream.sock")
LOG_FILE        = os.path.expanduser("~/.unchained/stream.log")
PID_FILE        = os.path.expanduser("~/.unchained/agent_stream.pid")

# ── Log tee ───────────────────────────────────────────────────────────────────
class _Tee:
    """Write to both a stream and a log file simultaneously."""
    def __init__(self, stream, path: str):
        self._stream = stream
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._file = open(path, "w", buffering=1, encoding="utf-8")   # truncate on start

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _Tee(sys.stdout, LOG_FILE)
sys.stderr = _Tee(sys.stderr, LOG_FILE)


# ── PID file ──────────────────────────────────────────────────────────────────
def _check_pid_file():
    """Abort if another agent_stream is already running, otherwise claim the PID file."""
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    if os.path.exists(PID_FILE):
        try:
            old_pid = int(open(PID_FILE).read().strip())
            os.kill(old_pid, 0)   # signal 0 = just check existence
            print(f"[startup] agent_stream already running (PID {old_pid})")
            print(f"[startup] run  kill {old_pid}  to stop it, or  pkill -f agent_stream.py")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass   # stale PID file — process is gone, safe to continue
    open(PID_FILE, "w").write(str(os.getpid()))


def _remove_pid_file():
    try:
        os.unlink(PID_FILE)
    except FileNotFoundError:
        pass


# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "current":    "",
    "narration":  "AI travel show · unchainedsky.com",
    "thinking":   "",             # short status string shown in overlay row 3
    "research":   "",             # Wikipedia extract for current location
    "queue":      [],
    "city_queue": [],   # remaining (lat, lng, name) spots in current city tour
    "visited":    [],
    "total":      0,
    "status":     "starting",
    "step":       0,
    "mode":       "streetview",   # "streetview" | "website"
}
_seen_chat:       set[str]         = set()
_recent_chat:     list[str]        = []    # rolling buffer for agent context
_visited_urls:    list[str]        = []    # track visited website URLs
_pending_actions: asyncio.Queue    = asyncio.Queue()   # from stream_ctl
_recent_agent_dests: list[str]    = []    # last N agent-picked destinations (detect stuck loops)
_event_log:       list[dict]       = []    # recent events (capped at 100)
_tail_queues:     list             = []    # one asyncio.Queue per tail client
_agent_paused:    bool             = False # pause AI orchestrator decisions
_visited_times:   dict             = {}    # dest_name → unix timestamp of last visit


def _emit(type_: str, **kwargs):
    """Broadcast an event to all tail clients and the local log."""
    evt = {"type": type_, "t": time.time(), **kwargs}
    _event_log.append(evt)
    if len(_event_log) > 100:
        _event_log.pop(0)
    for q in _tail_queues:
        q.put_nowait(evt)


def set_thinking(msg: str):
    """Update the thinking status shown in overlay row 3."""
    state["thinking"] = msg
    if msg:
        print(f"[think] {msg}", flush=True)


# ── Wikipedia research ───────────────────────────────────────────────────────
async def fetch_wikipedia_summary(name: str) -> str:
    """Fetch 1-2 sentence summary from Wikipedia REST API. Non-blocking, 5s timeout."""
    import aiohttp as _aio
    title = name.split(",")[0].strip().replace(" ", "_")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title, safe='')}"
    headers = {"User-Agent": "UnchainedSky/1.0 (https://unchainedsky.com; streaming bot)"}
    try:
        async with _aio.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=_aio.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    data = await r.json()
                    extract = data.get("extract", "")
                    if not extract:
                        return ""
                    sentences = extract.split(". ")
                    short = ". ".join(sentences[:2])
                    if len(sentences) > 1 and not short.endswith("."):
                        short += "."
                    return short
    except Exception as e:
        print(f"[wiki] fetch error for '{name}': {e}", flush=True)
    return ""


async def research_location(name: str):
    """Background task: fetch Wikipedia summary, narrate it, store for LLM context."""
    set_thinking(f"Looking up {name} on Wikipedia…")
    summary = await fetch_wikipedia_summary(name)
    if summary:
        state["research"] = summary
        state["narration"] = summary
        print(f"[wiki] {name}: {summary[:80]}", flush=True)
        asyncio.create_task(speak(summary))
    else:
        print(f"[wiki] no summary for '{name}'", flush=True)
    set_thinking("")


# ── Overlay ───────────────────────────────────────────────────────────────────
OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background: transparent;
  font-family: 'SF Mono', Menlo, monospace;
  padding: 12px 16px;
  width: 860px;
}
.bar {
  background: rgba(0,0,0,0.82);
  border-radius: 10px;
  padding: 10px 16px;
  border: 1px solid rgba(255,255,255,0.08);
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.top {
  display: flex;
  align-items: center;
  gap: 14px;
}
.live {
  background: #e94560;
  color: #fff;
  font-size: 10px;
  font-weight: bold;
  padding: 3px 7px;
  border-radius: 4px;
  letter-spacing: 1.5px;
  animation: blink 2s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.55} }
.location {
  font-size: 17px;
  font-weight: bold;
  color: #fff;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pin { color: #e94560; margin-right: 4px; }
.stats { font-size: 11px; color: rgba(255,255,255,0.4); flex-shrink: 0; }
.brand { font-size: 10px; color: rgba(255,255,255,0.32); white-space: nowrap; flex-shrink: 0; }
.brand b { color: #e94560; }
.narration {
  font-size: 12px;
  color: rgba(255,255,255,0.62);
  font-style: italic;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-left: 2px;
  min-height: 16px;
}
.ai-tag {
  color: #e94560;
  font-style: normal;
  font-weight: bold;
  margin-right: 6px;
  font-size: 10px;
  letter-spacing: 0.5px;
}
.thinking-row {
  font-size: 11px;
  color: rgba(255,255,255,0.45);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-left: 2px;
  min-height: 0;
  max-height: 0;
  opacity: 0;
  transition: opacity 0.6s ease, max-height 0.4s ease;
}
.thinking-row.visible {
  min-height: 14px;
  max-height: 20px;
  opacity: 1;
}
.think-tag {
  color: #f0a030;
  font-weight: bold;
  margin-right: 6px;
  font-size: 9px;
  letter-spacing: 0.5px;
}
</style></head>
<body>
<div class="bar">
  <div class="top">
    <div class="live">LIVE</div>
    <div class="location"><span class="pin">📍</span><span id="loc">Starting…</span></div>
    <div class="stats" id="stats"></div>
    <div class="brand">🤖 <b>Unchained</b> · unchainedsky.com</div>
  </div>
  <div class="narration"><span class="ai-tag">AI▸</span><span id="narr"></span></div>
  <div class="thinking-row" id="think-row"><span class="think-tag">⚙</span><span id="think"></span></div>
</div>
<script>
let lastThinking = '';
let thinkTimer = null;
async function refresh() {
  try {
    const d = await (await fetch('/state')).json();
    document.getElementById('loc').textContent = d.current || '…';
    document.getElementById('narr').textContent = d.narration || '';
    const parts = [];
    if (d.total) parts.push(d.total + ' visited');
    if (d.queue?.length) parts.push('next: ' + d.queue[0]);
    document.getElementById('stats').textContent = parts.join('  ·  ');

    const row = document.getElementById('think-row');
    const thinkEl = document.getElementById('think');
    if (d.thinking) {
      thinkEl.textContent = d.thinking;
      row.classList.add('visible');
      lastThinking = d.thinking;
      if (thinkTimer) clearTimeout(thinkTimer);
      thinkTimer = null;
    } else if (lastThinking) {
      if (!thinkTimer) {
        thinkTimer = setTimeout(function() {
          row.classList.remove('visible');
          lastThinking = '';
          thinkTimer = null;
        }, 8000);
      }
    } else {
      row.classList.remove('visible');
    }
  } catch(e) {}
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""


def _kill_port(port: int):
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(["lsof", "-ti", f":{port}"], stderr=subprocess.DEVNULL, text=True)
        else:
            # Linux: use fuser instead of lsof (more commonly available in minimal containers)
            out = subprocess.check_output(["fuser", f"{port}/tcp"], stderr=subprocess.DEVNULL, text=True)
        for pid in out.strip().split():
            if pid.strip():
                os.kill(int(pid.strip()), signal.SIGKILL)
        time.sleep(0.3)
    except (subprocess.CalledProcessError, ProcessLookupError, ValueError, FileNotFoundError):
        pass


async def serve_overlay(request):
    from aiohttp import web
    if request.path == "/state":
        return web.Response(text=json.dumps(state), content_type="application/json")
    return web.Response(text=OVERLAY_HTML, content_type="text/html")


async def start_overlay_server():
    from aiohttp import web
    _kill_port(OVERLAY_PORT)
    app = web.Application()
    app.router.add_get("/overlay", serve_overlay)
    app.router.add_get("/state",   serve_overlay)
    runner = web.AppRunner(app)
    await runner.setup()
    # Bind to 0.0.0.0 so overlay is accessible from outside Docker containers
    await web.TCPSite(runner, "0.0.0.0", OVERLAY_PORT).start()
    print(f"[overlay] http://0.0.0.0:{OVERLAY_PORT}/overlay  (860×145)")


# ── Docker overlay injection (CDP) ────────────────────────────────────────────
# In Docker mode we inject the overlay directly into the Maps page via CDP
# so FFmpeg just grabs the screen — no OBS compositing needed.

_OVERLAY_INJECT_JS = """
(function() {
    if (document.getElementById('unchained-overlay')) return 'exists';
    // Hide cursor globally
    if (!document.getElementById('ov-hide-cursor')) {
        var s = document.createElement('style');
        s.id = 'ov-hide-cursor';
        s.textContent = '* { cursor: none !important; }';
        document.head.appendChild(s);
    }
    var d = document.createElement('div');
    d.id = 'unchained-overlay';
    d.style.cssText = 'position:fixed;bottom:8px;left:50%;transform:translateX(-50%);' +
        'width:65%;max-width:1200px;z-index:2147483647;' +
        'pointer-events:none;font-family:monospace;padding:0;';
    d.innerHTML = '<div style="background:rgba(0,0,0,0.82);border-radius:10px;' +
        'padding:10px 16px;border:1px solid rgba(255,255,255,0.08);display:flex;' +
        'flex-direction:column;gap:7px;">' +
        '<div style="display:flex;align-items:center;gap:14px;">' +
            '<div style="background:#e94560;color:#fff;font-size:10px;font-weight:bold;' +
                'padding:3px 7px;border-radius:4px;letter-spacing:1.5px;flex-shrink:0;"' +
                ' id="ov-live">LIVE</div>' +
            '<div style="font-size:17px;font-weight:bold;color:#fff;flex:1;overflow:hidden;' +
                'text-overflow:ellipsis;white-space:nowrap;" id="ov-loc">' +
                '<span style="color:#e94560;margin-right:4px;">\\u{1F4CD}</span>' +
                '<span id="ov-loc-text">Starting\\u2026</span></div>' +
            '<div style="font-size:11px;color:rgba(255,255,255,0.4);flex-shrink:0;" id="ov-stats"></div>' +
            '<div style="font-size:10px;color:rgba(255,255,255,0.32);white-space:nowrap;flex-shrink:0;">' +
                '\\u{1F916} <b style="color:#e94560;">Unchained</b> \\u00B7 unchainedsky.com</div>' +
        '</div>' +
        '<div style="font-size:12px;color:rgba(255,255,255,0.62);font-style:italic;' +
            'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:2px;' +
            'min-height:16px;">' +
            '<span style="color:#e94560;font-style:normal;font-weight:bold;margin-right:6px;' +
                'font-size:10px;letter-spacing:0.5px;">AI\\u25B8</span>' +
            '<span id="ov-narr"></span></div>' +
        '<div style="font-size:11px;color:rgba(255,255,255,0.45);overflow:hidden;' +
            'text-overflow:ellipsis;white-space:nowrap;padding-left:2px;min-height:0;' +
            'max-height:0;opacity:0;transition:opacity 0.6s ease,max-height 0.4s ease;" id="ov-think">' +
            '<span style="color:#f0a030;font-weight:bold;margin-right:6px;font-size:9px;' +
                'letter-spacing:0.5px;">\\u2699</span>' +
            '<span id="ov-think-text"></span></div>' +
    '</div>';
    document.body.appendChild(d);
    // Keep overlay on top: re-append every 500ms if buried or removed
    if (!window._ov_interval) {
        window._ov_interval = setInterval(function() {
            var el = document.getElementById('unchained-overlay');
            if (!el) {
                // Re-create if removed by page navigation
                document.body.appendChild(d.cloneNode(true));
            } else if (el.nextSibling) {
                // Re-append to stay as last child (on top)
                document.body.appendChild(el);
            }
        }, 500);
    }
    return 'injected';
})()
"""

_overlay_script_installed = False


async def inject_overlay(tab_id: str):
    """Inject the overlay HTML into the Maps page via CDP Runtime.evaluate.
    Also installs Page.addScriptToEvaluateOnNewDocument for persistence."""
    global _overlay_script_installed
    try:
        c = await get_cdp(tab_id)
        # Install persistent script that re-injects on every navigation
        if not _overlay_script_installed:
            await c.send("Page.enable", {})
            await c.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": _OVERLAY_INJECT_JS,
            })
            _overlay_script_installed = True
            print("[docker] overlay persistent script installed", flush=True)
        r = await c.execute_js(_OVERLAY_INJECT_JS)
        result = r.get("result", {}).get("value", "")
        print(f"[docker] overlay inject: {result}", flush=True)
    except Exception as e:
        print(f"[docker] overlay inject error: {e}", flush=True)


async def update_overlay(tab_id: str):
    """Push current state values into the injected overlay elements.
    Re-injects if overlay is missing (page navigated)."""
    loc = json.dumps(state.get("current", "") or "…")
    narr = json.dumps(state.get("narration", "") or "")
    thinking = state.get("thinking", "")
    think_text = json.dumps(thinking)
    total = state.get("total", 0)
    queue = state.get("queue", [])
    stats_parts = []
    if total:
        stats_parts.append(f"{total} visited")
    if queue:
        stats_parts.append(f"next: {queue[0]}")
    stats = json.dumps("  ·  ".join(stats_parts))

    think_style = (
        "min-height:14px;max-height:20px;opacity:1;" if thinking
        else "min-height:0;max-height:0;opacity:0;"
    )

    js = f"""(function() {{
        var el = document.getElementById('ov-loc-text');
        if (!el) return 'no_overlay';
        el.textContent = {loc};
        var narr = document.getElementById('ov-narr');
        if (narr) narr.textContent = {narr};
        var st = document.getElementById('ov-stats');
        if (st) st.textContent = {stats};
        var th = document.getElementById('ov-think');
        if (th) th.style.cssText += '{think_style}';
        var tt = document.getElementById('ov-think-text');
        if (tt) tt.textContent = {think_text};
        return 'ok';
    }})()"""

    try:
        c = await get_cdp(tab_id)
        r = await c.execute_js(js)
        result = r.get("result", {}).get("value", "")
        if result == "no_overlay":
            await inject_overlay(tab_id)
    except Exception:
        pass  # concurrent CDP access — non-critical, retry next cycle


# ── Docker FFmpeg capture ─────────────────────────────────────────────────────
_ffmpeg_proc = None
_ffmpeg_thread = None


def _ffmpeg_log_thread(proc):
    """Read FFmpeg stderr in background and print lines."""
    for line in proc.stderr:
        line = line.strip()
        if line:
            print(f"[ffmpeg] {line}", flush=True)


def _check_pulse_available() -> bool:
    """Check if PulseAudio is available and accepting connections."""
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True, timeout=3)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def start_ffmpeg(output_target: str):
    """Spawn FFmpeg to capture Xvfb display (+ PulseAudio if available) → RTMP or file."""
    global _ffmpeg_proc, _ffmpeg_thread
    if _ffmpeg_proc and _ffmpeg_proc.poll() is None:
        print("[ffmpeg] already running", flush=True)
        return

    has_audio = _check_pulse_available()
    cmd = [
        "ffmpeg", "-y",
        "-f", "x11grab", "-framerate", "30", "-video_size", "1920x1080", "-i", ":99",
    ]
    if has_audio:
        cmd += ["-f", "pulse", "-ac", "2", "-i", "default"]
    else:
        # Generate silent audio track so RTMP/FLV has an audio stream
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", "3000k", "-maxrate", "3000k", "-bufsize", "6000k",
        "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-shortest",
        "-f", "flv", output_target,
    ]
    print(f"[ffmpeg] audio: {'pulse' if has_audio else 'silent (pulse unavailable)'}", flush=True)
    print(f"[ffmpeg] starting: {' '.join(cmd)}", flush=True)
    _ffmpeg_proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    _ffmpeg_thread = threading.Thread(target=_ffmpeg_log_thread, args=(_ffmpeg_proc,), daemon=True)
    _ffmpeg_thread.start()
    print(f"[ffmpeg] PID {_ffmpeg_proc.pid} → {output_target}", flush=True)


def stop_ffmpeg():
    """Gracefully stop FFmpeg."""
    global _ffmpeg_proc
    if _ffmpeg_proc and _ffmpeg_proc.poll() is None:
        print("[ffmpeg] stopping…", flush=True)
        _ffmpeg_proc.send_signal(signal.SIGINT)
        try:
            _ffmpeg_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _ffmpeg_proc.kill()
        print("[ffmpeg] stopped", flush=True)
    _ffmpeg_proc = None


# ── OBS WebSocket v5 ──────────────────────────────────────────────────────────
_obs_ws = None


async def obs_connect(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD) -> bool:
    global _obs_ws
    import websockets
    try:
        ws = await websockets.connect(f"ws://{host}:{port}")
    except Exception as e:
        print(f"[obs] connect failed: {e}")
        return False

    msg = json.loads(await ws.recv())
    if msg.get("op") != 0:
        await ws.close()
        return False

    auth_str = ""
    auth_info = msg["d"].get("authentication")
    if auth_info and password:
        salt     = auth_info["salt"]
        challenge = auth_info["challenge"]
        secret   = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
        auth_str = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()

    identify: dict = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
    if auth_str:
        identify["d"]["authentication"] = auth_str
    await ws.send(json.dumps(identify))

    msg = json.loads(await ws.recv())
    if msg.get("op") != 2:
        await ws.close()
        return False

    _obs_ws = ws
    print(f"[obs] connected → ws://{host}:{port}")
    return True


async def _obs_request(request_type: str, data: dict | None = None):
    if not _obs_ws:
        return None
    req_id = uuid.uuid4().hex[:8]
    payload: dict = {"op": 6, "d": {"requestType": request_type, "requestId": req_id}}
    if data:
        payload["d"]["requestData"] = data
    try:
        await _obs_ws.send(json.dumps(payload))
        deadline = time.time() + 5
        while time.time() < deadline:
            raw = await asyncio.wait_for(_obs_ws.recv(), timeout=3)
            msg = json.loads(raw)
            if msg.get("op") == 7 and msg["d"].get("requestId") == req_id:
                return msg["d"]
    except Exception as e:
        print(f"[obs] request error: {e}")
    return None


async def obs_start_stream():
    r = await _obs_request("StartStream")
    if r:
        code = r.get("requestStatus", {}).get("code", 0)
        print("[obs] already streaming" if code == 500 else "[obs] stream started")


async def obs_stop_stream():
    r = await _obs_request("StopStream")
    if r:
        print("[obs] stream stopped")


async def obs_close():
    global _obs_ws
    if _obs_ws:
        try:
            await _obs_ws.close()
        except Exception:
            pass
        _obs_ws = None


OBS_CAPTURE_SOURCE = "macOS Screen Capture"

async def obs_configure_window_capture(maps_tab_title: str = "Google Maps"):
    """Switch OBS macOS Screen Capture from application mode to window capture.

    Queries OBS for the list of available windows in the source's selector,
    finds the Chrome window whose title contains maps_tab_title, and pins
    the capture to that specific window so other Chrome windows can't occlude it.
    """
    if not _obs_ws:
        return

    # 1. Get available windows from the source's property list
    items = await _obs_request("GetInputPropertiesListPropertyItems", {
        "inputName": OBS_CAPTURE_SOURCE,
        "propertyName": "window",
    })
    if not items:
        print("[obs] could not list windows for capture source")
        return

    windows = items.get("responseData", {}).get("propertyItems", [])
    if not windows:
        print("[obs] no windows found in capture source selector")
        return

    # 2. Find the Chrome window that contains Google Maps in its title.
    #    Priority order:
    #      a) Chrome window whose title contains the specific maps_tab_title
    #      b) Any Chrome window whose title contains "Google Maps"
    #    We deliberately avoid falling back to "any Chrome window" since that
    #    risks selecting a different Chrome profile (personal browser, etc.).
    target = None
    chrome_windows = [w for w in windows if "google chrome" in w.get("itemName", "").lower()]

    # (a) exact title match
    for w in chrome_windows:
        if maps_tab_title.lower() in w.get("itemName", "").lower():
            target = w
            break

    # (b) any Chrome window with "Google Maps" in the title
    if not target:
        for w in chrome_windows:
            if "google maps" in w.get("itemName", "").lower():
                target = w
                break

    if not target:
        print(f"[obs] no Google Maps Chrome window found — skipping OBS window pin")
        print(f"[obs] Chrome windows seen: {[w.get('itemName','') for w in chrome_windows]}")
        return

    print(f"[obs] targeting window: {target.get('itemName', '')}")

    # 3. Switch source to window capture mode (type=1) with the target window
    await _obs_request("SetInputSettings", {
        "inputName": OBS_CAPTURE_SOURCE,
        "inputSettings": {
            "type": 1,                         # window capture
            "window": target.get("itemValue"),  # window ID from selector
            "show_hidden_windows": True,
        },
    })
    print("[obs] switched to window capture mode")

    # 4. Crop Chrome UI (tabs + address bar ≈ 300px at 2x Retina) and fill canvas
    #    Find the scene item ID for the capture source
    scene_items = await _obs_request("GetSceneItemList", {"sceneName": "Scene"})
    if scene_items:
        for item in scene_items.get("responseData", {}).get("sceneItems", []):
            if item.get("sourceName") == OBS_CAPTURE_SOURCE:
                await _obs_request("SetSceneItemTransform", {
                    "sceneName": "Scene",
                    "sceneItemId": item["sceneItemId"],
                    "sceneItemTransform": {
                        "positionX": 0.0, "positionY": 0.0,
                        "cropTop": 300, "cropLeft": 0, "cropRight": 0, "cropBottom": 0,
                        "boundsType": "OBS_BOUNDS_SCALE_OUTER",
                        "boundsWidth": 1920.0, "boundsHeight": 1080.0,
                    },
                })
                print("[obs] cropped Chrome UI + filled canvas")
                break

    # 5. Ensure desktop audio capture exists for Chrome audio (TTS + lofi)
    OBS_AUDIO_SOURCE = "Chrome Audio"
    existing = await _obs_request("GetInputSettings", {"inputName": OBS_AUDIO_SOURCE})
    if not existing or not existing.get("requestStatus", {}).get("result"):
        await _obs_request("CreateInput", {
            "sceneName": "Scene",
            "inputName": OBS_AUDIO_SOURCE,
            "inputKind": "sck_audio_capture",
            "inputSettings": {"type": 0},  # desktop audio capture
        })
        print("[obs] created desktop audio capture source")
    await _obs_request("SetInputMute", {
        "inputName": OBS_AUDIO_SOURCE, "inputMuted": False,
    })


# ── Persistent CDP connections ────────────────────────────────────────────────
_conns: dict[str, CDP] = {}


async def get_cdp(tab_id: str) -> CDP:
    c = _conns.get(tab_id)
    if c and c.ws and c.ws.state.name == "OPEN":
        return c
    try:
        if c:
            await c.close()
    except Exception:
        pass
    c = CDP(_get_ws_url_for_tab(tab_id))
    await c.connect()
    _conns[tab_id] = c
    return c


async def close_all():
    for c in _conns.values():
        try:
            await c.close()
        except Exception:
            pass
    _conns.clear()


async def send_key(tab_id: str, k: str, code: str, kc: int):
    c = await get_cdp(tab_id)
    for t in ("keyDown", "keyUp"):
        await c.send("Input.dispatchKeyEvent", {
            "type": t, "key": k, "code": code,
            "keyCode": kc, "windowsVirtualKeyCode": kc, "nativeVirtualKeyCode": kc,
        })
        await asyncio.sleep(0.08)


async def click_xy(tab_id: str, x: int, y: int):
    c = await get_cdp(tab_id)
    for t in ("mousePressed", "mouseReleased"):
        await c.send("Input.dispatchMouseEvent", {
            "type": t, "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        await asyncio.sleep(0.05)


async def click_center(tab_id: str):
    c = await get_cdp(tab_id)
    r = await c.execute_js("({w: window.innerWidth, h: window.innerHeight})")
    v = r.get("result", {}).get("value") or {"w": 1280, "h": 800}
    await click_xy(tab_id, v.get("w", 1280) // 2, v.get("h", 800) // 2)


async def js_eval(tab_id: str, expr: str):
    c = await get_cdp(tab_id)
    r = await c.execute_js(expr)
    return r.get("result", {}).get("value")


def find_tab(keyword: str) -> str | None:
    for t in _list_tabs():
        if keyword.lower() in t.get("url", "").lower() or keyword.lower() in t.get("title", "").lower():
            return t["id"]
    return None


_maps_tab_id: str | None = None
_chat_tab_id: str | None = None

# ── Twitch chat writer (module-level so execute_plan/go_to_next_spot can use it) ──
_twitch_writer = None
_twitch_channel: str = ""

async def twitch_say(text: str):
    """Send a message to Twitch chat."""
    global _twitch_writer
    if _twitch_writer and _twitch_channel:
        try:
            _twitch_writer.write(f"PRIVMSG #{_twitch_channel.lower()} :{text}\r\n".encode())
            await _twitch_writer.drain()
            print(f"[twitch] said: {text[:80]}", flush=True)
        except Exception as e:
            print(f"[twitch] send error: {e}", flush=True)

# Expected window bounds — matches cdp.py cmd_start() launch flags
WINDOW_BOUNDS = {"left": -2000, "top": 0, "width": 1920, "height": 1080, "windowState": "normal"}

async def _enforce_window_bounds():
    """Ensure the streaming Chrome window stays at the expected position/size.

    Other Chrome profiles can steal the foreground or push our window around.
    This uses CDP Browser.getWindowForTarget / Browser.setWindowBounds to
    reposition without any AppleScript dependency.
    """
    if not _maps_tab_id:
        return
    try:
        c = await get_cdp(_maps_tab_id)
        resp = await c.send("Browser.getWindowForTarget")
        win = resp.get("bounds", {})
        if (win.get("left") != WINDOW_BOUNDS["left"]
                or win.get("top") != WINDOW_BOUNDS["top"]
                or win.get("width") != WINDOW_BOUNDS["width"]
                or win.get("height") != WINDOW_BOUNDS["height"]
                or win.get("windowState") != "normal"):
            await c.send("Browser.setWindowBounds", {
                "windowId": resp["windowId"],
                "bounds": WINDOW_BOUNDS,
            })
            print("[guard] repositioned Chrome window", flush=True)
    except Exception:
        pass

async def _activate_maps_tab():
    """Bring the Maps tab to front so OBS captures it, not blank/music tabs."""
    if not _maps_tab_id:
        return
    try:
        import urllib.request
        url = f"http://{_cdp_mod.CDP_HOST}:{_cdp_mod.DEBUG_PORT}/json/activate/{_maps_tab_id}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pass


# ── Chat reader ───────────────────────────────────────────────────────────────
async def read_chat(chat_tab: str) -> list[str]:
    try:
        c = await get_cdp(chat_tab)
        text = await c.get_text(max_len=8000)
    except Exception as e:
        print(f"[chat] error: {e}")
        _conns.pop(chat_tab, None)
        return []
    found = []
    for line in text.splitlines():
        # Skip our own bot template messages (welcome + reminders)
        if "Type !go" in line or "e.g. !go" in line:
            continue
        if "visited" in line.lower() and "places" in line.lower():
            continue
        m = re.search(r"!?go\s+(.+)", line, re.IGNORECASE)
        if m:
            dest = m.group(1).strip()[:80]
            if dest not in _seen_chat:
                _seen_chat.add(dest)
                found.append(dest)
    return found


async def send_chat(chat_tab: str, msg: str):
    """Send a message to Twitch (or YouTube) chat via the open chat tab."""
    try:
        c = await get_cdp(chat_tab)
        sent = await c.execute_js(f"""
            (function() {{
                var ci = document.querySelector('[data-a-target="chat-input"]');
                if (!ci) return 'no_input';
                ci.focus();
                ci.textContent = {json.dumps(msg)};
                ci.dispatchEvent(new InputEvent('input', {{bubbles: true}}));
                var btn = document.querySelector('[data-a-target="chat-send-button"]');
                if (btn) {{ btn.click(); return 'sent'; }}
                return 'no_button';
            }})()
        """)
        result = sent.get("result", {}).get("value", "")
        if result == "sent":
            print(f"[chat] sent: {msg}", flush=True)
        else:
            # Fallback: try Enter key
            await c.execute_js("""
                var ci = document.querySelector('[data-a-target="chat-input"]');
                if (ci) ci.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
            """)
            print(f"[chat] sent (enter): {msg}", flush=True)
    except Exception as e:
        print(f"[chat] send error: {e}", flush=True)


# ── Street View coverage check ────────────────────────────────────────────────
SV_RADIUS = 5000  # meters — search radius for nearby panorama

async def has_streetview(lat: float, lng: float) -> bool:
    """Check if Google Street View has coverage near these coordinates.
    Uses the GeoPhotoService endpoint — no API key needed."""
    url = (
        f"https://maps.googleapis.com/maps/api/js/"
        f"GeoPhotoService.SingleImageSearch?pb=!1m5!1sapiv3!5sUS!11m2"
        f"!1m1!1b0!2m4!1m2!3d{lat}!4d{lng}!2d{SV_RADIUS}!3m18!2m2"
        f"!1sen!2sUS!9m1!1e2!11m12!1m3!1e2!2b1!3e2!1m3!1e3!2b1!3e2"
        f"!1m3!1e10!2b1!3e2!4m6!1e1!1e2!1e3!1e4!1e8!1e6&callback=_cb"
    )
    try:
        import urllib.request as _ur
        req = _ur.Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        with _ur.urlopen(req, timeout=5) as resp:
            body = resp.read().decode(errors="replace")
            # Response has nested arrays; a successful hit contains a pano ID
            # string (22+ chars of base64) and image size arrays like [4000,8000].
            # An empty/no-coverage response is very short or has [[0]] only.
            if len(body) > 200 and "[4000,8000]" in body:
                return True
            # Also check for any base64-like pano ID (22+ alphanumeric chars)
            if re.search(r'"[A-Za-z0-9_-]{20,}"', body):
                return True
            return False
    except Exception:
        return True  # assume yes on network error, let enter_streetview handle it


# ── Maps navigation ───────────────────────────────────────────────────────────
async def search_place(place: str, tab: str) -> tuple[float, float] | None:
    q = urllib.parse.quote_plus(place)
    try:
        c = await get_cdp(tab)
        await c.navigate(f"{MAPS_BASE}/search/{q}", wait=4)
    except Exception as e:
        print(f"[maps] nav error: {e}")
        _conns.pop(tab, None)
        return None

    deadline = time.time() + 12
    href = ""
    while time.time() < deadline:
        try:
            r = await (await get_cdp(tab)).execute_js("window.location.href")
            href = r.get("result", {}).get("value", "")
        except Exception:
            _conns.pop(tab, None)
            break
        if re.search(r"@-?\d+\.\d+,-?\d+\.\d+", href):
            break
        await asyncio.sleep(0.8)

    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", href)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


async def get_city_attractions(city: str, tab: str) -> list[tuple[float, float, str]]:
    """Search Google Maps for top sights in a city and return attraction coordinates.

    Uses the Maps search page for "top sights in {city}" which returns specifically
    tourism-relevant place cards — not hotels, suburbs, or geographic centroids.
    Filters results to within 50 km of the search center to exclude outliers.
    Returns a list of (lat, lng, name) tuples (up to 12 candidates).
    """
    set_thinking(f"Searching top sights in {city}…")
    q = urllib.parse.quote_plus(f"top sights in {city}")
    try:
        c = await get_cdp(tab)
        await c.navigate(f"{MAPS_BASE}/search/{q}", wait=5)
    except Exception as e:
        print(f"[maps] city search nav error: {e}", flush=True)
        _conns.pop(tab, None)
        return []

    # Wait up to 12s for search results to load (URL gets @lat,lng once ready)
    deadline = time.time() + 12
    href = ""
    while time.time() < deadline:
        try:
            r = await (await get_cdp(tab)).execute_js("window.location.href")
            href = r.get("result", {}).get("value", "")
            if re.search(r"@-?\d+\.\d+,-?\d+\.\d+", href):
                count = await js_eval(tab,
                    "document.querySelectorAll('a[href*=\"/maps/place/\"]').length")
                if count and count > 2:
                    break
        except Exception:
            break
        await asyncio.sleep(0.8)

    # Extract the search center (used as fallback if no attraction links found)
    center_lat, center_lng = None, None
    m0 = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", href)
    if m0:
        center_lat, center_lng = float(m0.group(1)), float(m0.group(2))

    # Extract place card links that have @lat,lng coordinates in their href
    try:
        results = await js_eval(tab, """(function() {
            var links = Array.from(document.querySelectorAll('a[href*="/maps/place/"]'));
            var found = [];
            var seen = new Set();
            links.forEach(function(a) {
                var href = a.href || '';
                var m = href.match(/[!@]3d(-?[0-9]+\.[0-9]+)[!,@]4d(-?[0-9]+\.[0-9]+)/)
                      || href.match(/@(-?[0-9]+\.[0-9]+),(-?[0-9]+\.[0-9]+)/);
                if (!m) return;
                var lat = parseFloat(m[1]);
                var lng = parseFloat(m[2]);
                var key = lat.toFixed(3) + ',' + lng.toFixed(3);
                if (seen.has(key)) return;
                seen.add(key);
                var name = a.getAttribute('aria-label') || '';
                if (!name) {
                    var pm = href.match(/\\/maps\\/place\\/([^\\/@]+)/);
                    if (pm) name = decodeURIComponent(pm[1].replace(/\\+/g, ' '));
                }
                if (!name) name = (a.innerText || '').split('\\n')[0].trim();
                name = name.trim().substring(0, 80) || 'attraction';
                found.push({lat: lat, lng: lng, name: name});
            });
            return found.slice(0, 20);
        })()""")
        if results and isinstance(results, list):
            attractions = [
                (r["lat"], r["lng"], r.get("name", city))
                for r in results if isinstance(r, dict)
            ]
            if attractions:
                return attractions[:12]
    except Exception as e:
        print(f"[maps] attraction extract error: {e}", flush=True)

    # Fallback: use search center coords from URL
    if center_lat is not None:
        return [(center_lat, center_lng, city)]
    return []


async def enter_streetview(lat: float, lng: float, tab: str, place: str = "") -> bool:
    url = f"https://maps.google.com/?q={lat},{lng}&layer=c&cbll={lat},{lng}"
    try:
        c = await get_cdp(tab)
        await c.navigate(url, wait=6)
    except Exception as e:
        print(f"[maps] streetview nav error: {e}")
        _conns.pop(tab, None)
        return False

    await asyncio.sleep(1)
    try:
        in_sv = await js_eval(tab, """
            !!(document.querySelector('.widget-scene-canvas, canvas[aria-label]')
               || window.location.href.includes('!3m'))
        """)
        if in_sv:
            # Give Street View imagery a moment to render, then verify not black
            await asyncio.sleep(2.5)
            if await is_black_screen(tab):
                return False
            # Visual check: catch user-contributed person photos
            if not await check_streetview_valid(tab, place):
                return False
            return True
        href = await js_eval(tab, "window.location.href") or ""
        return "!3m" in href or "layer=c" in href
    except Exception:
        return True


_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


async def check_streetview_valid(tab_id: str, place: str) -> bool:
    """Screenshot → Claude Haiku vision check. Returns False for blank/black or person photos."""
    try:
        c = await get_cdp(tab_id)
        result = await c.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        b64 = result.get("data")
    except Exception as e:
        print(f"[check] screenshot error: {e}", flush=True)
        return True  # fail open

    if not b64:
        return True

    try:
        client = _get_anthropic()
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a screenshot from Google Maps Street View. "
                            "Reply with exactly one word:\n"
                            "VALID — outdoor location (street, road, landmark, building exterior, landscape)\n"
                            "BLANK — screen is mostly black, grey, or still loading\n"
                            "PERSON — close-up of a person, or indoor/interior user-contributed photo\n"
                            "Reply only: VALID, BLANK, or PERSON"
                        ),
                    },
                ],
            }],
        )
        verdict = msg.content[0].text.strip().upper()
        print(f"[check] '{place}' → {verdict}", flush=True)
        return verdict == "VALID"
    except Exception as e:
        print(f"[check] vision error: {e}", flush=True)
        return True  # fail open


async def is_black_screen(tab: str) -> bool:
    """Return True if Street View has no imagery (black/blank canvas)."""
    val = await js_eval(tab, """(function() {
        if (/!1s[A-Za-z0-9_%-]{20,}/.test(window.location.href)) return 'ok';
        var inner = (document.body || {}).innerText || '';
        if (inner.includes("don't have imagery") || inner.includes('No imagery'))
            return 'no_imagery';
        var cv = document.querySelector('canvas.widget-scene-canvas')
              || document.querySelector('canvas[aria-label]');
        return (cv && cv.width > 200) ? 'ok' : 'no_canvas';
    })()""")
    if val in ("no_imagery", "no_canvas"):
        print(f"[maps] black screen detected ({val})", flush=True)
        return True
    return False


async def go_to(place: str, tab: str) -> bool:
    print(f"[maps] → {place}", flush=True)
    _emit("nav", place=place)
    state["status"] = "searching…"

    coords = await search_place(place, tab)
    if not coords:
        state["status"] = "not found"
        return False

    lat, lng = coords

    # Prefilter: check Street View coverage before navigating
    state["status"] = "checking coverage…"
    if not await has_streetview(lat, lng):
        print(f"[maps] no street view coverage at {lat},{lng} for '{place}'", flush=True)
        state["status"] = "no coverage"
        return False

    state["status"] = "entering street view…"

    ok = await enter_streetview(lat, lng, tab, place)
    if not ok:
        state["status"] = "no street view"
        return False

    await asyncio.sleep(1.5)
    await click_center(tab)
    await asyncio.sleep(1)

    state["current"] = place
    state["mode"]    = "streetview"
    state["total"]  += 1
    state["step"]    = 0
    state["status"]  = "exploring"
    state["visited"].append(place)
    _visited_times[place] = time.time()
    set_thinking("")
    print(f"[maps] arrived: {place}", flush=True)
    _emit("arrived", place=place, total=state["total"])

    # Spawn Wikipedia research in background (non-blocking)
    asyncio.create_task(research_location(place))

    if _chat_tab_id:
        await send_chat(_chat_tab_id, f"📍 {place} ({state['total']} visited)")

    return True


async def go_to_city(city: str, tab: str) -> bool:
    """Start a city tour: navigate to the first attraction and queue the rest.

    Navigates to the first valid attraction in the city, then stores the
    remaining SPOTS_PER_CITY-1 spots in state["city_queue"] so the main loop
    can visit them sequentially before calling the agent for the next city.
    Falls back to go_to() if attraction extraction fails.
    """
    print(f"[maps] city explore: {city}", flush=True)
    _emit("nav", place=city)
    state["status"] = "exploring city…"
    state["city_queue"] = []  # clear any previous city tour

    attractions = await get_city_attractions(city, tab)
    if not attractions:
        print(f"[maps] no attractions for {city}, falling back to search", flush=True)
        return await go_to(city, tab)

    # Prefer places we haven't been to; keep order (top sights first)
    fresh = [(lat, lng, name) for lat, lng, name in attractions
             if name not in state["visited"]]
    candidates = fresh if fresh else attractions

    for idx, (lat, lng, name) in enumerate(candidates[:SPOTS_PER_CITY + 2]):
        display = name if name and name != "attraction" else city
        set_thinking(f"Navigating to {display}…")
        print(f"[maps] → {display} ({lat:.4f}, {lng:.4f})", flush=True)

        state["status"] = "checking coverage…"
        if not await has_streetview(lat, lng):
            print(f"[maps] no SV coverage at {display}", flush=True)
            continue

        state["status"] = "entering street view…"
        ok = await enter_streetview(lat, lng, tab, display)
        if not ok:
            print(f"[maps] SV failed for {display}", flush=True)
            continue

        await asyncio.sleep(1.5)
        await click_center(tab)
        await asyncio.sleep(1)

        state["current"] = display
        state["mode"]    = "streetview"
        state["total"]  += 1
        state["step"]    = 0
        state["status"]  = "exploring"
        state["visited"].append(display)
        _visited_times[display] = time.time()
        set_thinking("")
        print(f"[maps] arrived: {display}", flush=True)
        _emit("arrived", place=display, total=state["total"])

        # Spawn Wikipedia research in background (non-blocking)
        asyncio.create_task(research_location(display))

        # Queue the remaining spots for this city tour
        remaining = [(la, ln, nm) for la, ln, nm in candidates[idx + 1:]
                     if nm not in state["visited"]]
        state["city_queue"] = remaining[:SPOTS_PER_CITY - 1]
        print(f"[maps] city queue: {[nm for _,_,nm in state['city_queue']]}", flush=True)

        if _chat_tab_id:
            await send_chat(_chat_tab_id, f"📍 {display} ({state['total']} visited)")

        return True

    # All candidates failed — fall back to direct search of city center
    print(f"[maps] all city attractions failed for {city}, trying search", flush=True)
    return await go_to(city, tab)


async def go_to_next_spot(tab: str) -> bool:
    """Navigate to the next attraction in the current city tour queue."""
    if not state["city_queue"]:
        return False
    lat, lng, name = state["city_queue"].pop(0)
    display = name if name and name != "attraction" else state["current"]
    set_thinking(f"Moving to next spot: {display}…")
    print(f"[maps] next spot: {display} ({lat:.4f}, {lng:.4f})", flush=True)
    _emit("nav", place=display)
    state["status"] = "next spot…"

    ok = await enter_streetview(lat, lng, tab, display)
    if not ok:
        print(f"[maps] spot failed: {display}", flush=True)
        return False

    await asyncio.sleep(1.5)
    await click_center(tab)
    await asyncio.sleep(1)

    state["current"] = display
    state["mode"]    = "streetview"
    state["total"]  += 1
    state["step"]    = 0
    state["status"]  = "exploring"
    state["visited"].append(display)
    _visited_times[display] = time.time()
    set_thinking("")
    print(f"[maps] at spot: {display}", flush=True)
    _emit("arrived", place=display, total=state["total"])

    # Spawn Wikipedia research in background (non-blocking)
    asyncio.create_task(research_location(display))

    if _chat_tab_id:
        await send_chat(_chat_tab_id, f"📍 {display}")

    # Announce in Twitch chat
    await twitch_say(f"🗺️ {display} (spot {state['total']})")

    return True


async def visit_website(url: str, tab: str, dwell_secs: int = 15):
    """Navigate to a URL for context/entertainment, then return to maps."""
    print(f"[web] → {url}", flush=True)
    _emit("website", url=url)
    state["status"]  = "browsing…"
    state["mode"]    = "website"
    state["current"] = url.split("//")[-1].split("/")[0]   # show domain
    try:
        c = await get_cdp(tab)
        await c.navigate(url, wait=5)
    except Exception as e:
        print(f"[web] nav error: {e}")
        _conns.pop(tab, None)
    await asyncio.sleep(dwell_secs)


async def step_forward(tab: str):
    try:
        await send_key(tab, "ArrowUp", "ArrowUp", 38)
    except Exception as e:
        print(f"[maps] step error: {e}", flush=True)
        _conns.pop(tab, None)
    state["step"] += 1


async def step_turn(tab: str):
    k = random.choice([("ArrowLeft", "ArrowLeft", 37), ("ArrowRight", "ArrowRight", 39)])
    for _ in range(random.randint(2, 5)):
        try:
            await send_key(tab, *k)
        except Exception:
            _conns.pop(tab, None)
            break
        await asyncio.sleep(0.4)


# ── Control socket server ─────────────────────────────────────────────────────
async def _dispatch_ctl(cmd: dict) -> dict:
    global _agent_paused
    action = cmd.get("cmd")
    if action == "status":
        return {**state, "socket": str(SOCKET_PATH), "log": LOG_FILE,
                "agent_paused": _agent_paused, "obs_connected": _obs_ws is not None}
    elif action == "tail":
        return {"_tail": True}   # handled specially in _handle_ctl_client
    elif action == "say":
        text = str(cmd.get("text", ""))[:90]
        state["narration"] = text
        _emit("ctl", cmd="say", text=text)
        print(f"[ctl] say: {text}", flush=True)
        return {"ok": True}
    elif action == "go":
        dest = str(cmd.get("dest", "")).strip()
        if dest and dest not in state["queue"]:
            state["queue"].insert(0, dest)
        _emit("ctl", cmd="go", dest=dest)
        print(f"[ctl] go queued: {dest}", flush=True)
        await _pending_actions.put({"action": "skip"})
        return {"ok": True, "queued": dest}
    elif action == "website":
        url = str(cmd.get("url", "")).strip()
        if url:
            _emit("ctl", cmd="website", url=url)
            await _pending_actions.put({"action": "website", "url": url})
            print(f"[ctl] website: {url}", flush=True)
        return {"ok": True}
    elif action == "skip":
        _emit("ctl", cmd="skip")
        await _pending_actions.put({"action": "skip"})
        print("[ctl] skip → forcing agent call", flush=True)
        return {"ok": True}

    elif action == "agent":
        sub = cmd.get("action")
        if sub == "pause":
            _agent_paused = True
            _emit("agent_paused")
            print("[ctl] agent paused", flush=True)
            return {"ok": True, "paused": True}
        elif sub == "resume":
            _agent_paused = False
            _emit("agent_resumed")
            print("[ctl] agent resumed", flush=True)
            return {"ok": True, "paused": False}
        elif sub == "status":
            return {"paused": _agent_paused}
        return {"error": f"unknown agent action: {sub}"}

    elif action == "obs":
        sub = cmd.get("action")
        if sub == "connect":
            host = cmd.get("host", OBS_HOST)
            port = cmd.get("port", OBS_PORT)
            password = cmd.get("password", OBS_PASSWORD)
            ok = await obs_connect(host, port, password)
            if ok and _maps_tab_id:
                maps_title = await js_eval(_maps_tab_id, "document.title") or "Google Maps"
                await obs_configure_window_capture(maps_title)
            return {"ok": ok, "connected": ok}
        elif sub == "start":
            await obs_start_stream()
            _emit("ctl", cmd="obs_start")
            return {"ok": True}
        elif sub == "stop":
            await obs_stop_stream()
            _emit("ctl", cmd="obs_stop")
            return {"ok": True}
        elif sub == "status":
            r = await _obs_request("GetStreamStatus")
            if r:
                d = r.get("responseData", {})
                return {"active": d.get("outputActive"), "timecode": d.get("outputTimecode")}
            return {"error": "OBS not connected"}
        return {"error": f"unknown obs action: {sub}"}

    return {"error": f"unknown cmd: {action}"}


async def _handle_ctl_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                cmd = json.loads(line)
            except json.JSONDecodeError:
                writer.write((json.dumps({"error": "invalid JSON"}) + "\n").encode())
                await writer.drain()
                continue

            if cmd.get("cmd") == "tail":
                # Switch to push mode — stream events until client disconnects
                tail_q: asyncio.Queue = asyncio.Queue()
                _tail_queues.append(tail_q)
                try:
                    # Replay last 15 events so the client sees recent history
                    for evt in _event_log[-15:]:
                        writer.write((json.dumps(evt) + "\n").encode())
                    await writer.drain()
                    # Stream new events indefinitely
                    while True:
                        evt = await tail_q.get()
                        writer.write((json.dumps(evt) + "\n").encode())
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    _tail_queues.remove(tail_q)
                return

            resp = await _dispatch_ctl(cmd)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def start_ctl_server():
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(_handle_ctl_client, SOCKET_PATH)
    print(f"[ctl] socket: {SOCKET_PATH}")
    return server


async def launch_ctl_terminal():
    """Open stream_ctl.py in a new Terminal window."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    script = (
        f'tell application "Terminal"\n'
        f'  do script "cd {script_dir} && uv run stream_ctl.py"\n'
        f'  activate\n'
        f'end tell'
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    print("[ctl] terminal launched")


# ── Claude Agent ──────────────────────────────────────────────────────────────
AGENT_SYSTEM = """\
You are the live AI host of a travel and discovery show powered by unchainedsky.com — \
a browser agent that controls a real Chrome browser in real-time using CDP.

Your job: plan the next ~2 minute show segment. Be creative, educational, and entertaining. \
Connect destinations thematically when possible. Mix Street View exploration with occasional \
website visits (Wikipedia, YouTube, news articles) to add context and depth.

Naturally remind viewers they're watching a real AI agent browse live — this is \
unchainedsky.com in action. Vary destinations: urban/rural/nature/history/weird/remote.

**NARRATIVE JOURNEY RULE**
Build a sequentially connected journey — each destination should have a clear thematic or \
geographic link to the previous one. Think like a documentary: "From the ancient temples of \
Kyoto we cross continents to the sacred sites of Jerusalem — both cities shaped by millennia \
of pilgrimage." The audience should feel the logic of each transition. \
Vary regions every 2-3 cities to keep the show global, but always justify the jump with a \
narrative thread — shared history, architecture, culture, geography, or a surprising parallel. \
Think GLOBALLY — Paris, Buenos Aires, Cape Town, Reykjavik, Marrakech, Sydney, \
Rio de Janeiro, Istanbul, Cairo, Lisbon, Havana, Petra, Dubrovnik, Cusco, Edinburgh. \
The world is vast — explore it all, one story at a time!

Respond ONLY with valid JSON (no markdown):
{
  "action": "streetview" | "website" | "explore",
  "destination": "Place Name, Country",   // streetview only
  "url": "https://...",                   // website only — Wikipedia, YouTube, news
  "narration": "Punchy overlay line ≤90 chars, present tense, written for a live audience",
  "reason": "Internal reasoning — not shown to viewers"
}

Rules:
- "streetview": go to a new Google Maps Street View location.
- "website": visit a URL for context (~15s). Good for Wikipedia overviews, YouTube clips, news.
- "explore": stay and keep walking. Use when the current spot is genuinely interesting.
- narration ≤90 chars. No quotation marks. Vivid, present tense.
- NEVER repeat ANY destination from the "ALREADY VISITED" list. Pick completely new places every time.
- If chat has !go requests, honor them with a streetview action.
- Occasionally use website to showcase that the agent can browse any site, not just Maps.
- IMPORTANT: Always pick a **city name** as the destination (e.g. "Tokyo", "Istanbul", \
"Cape Town"). The agent will automatically browse that city's Google Maps page and pick a \
featured attraction with confirmed imagery. Do NOT pick specific landmarks or remote locations \
— just the city. Avoid places without Street View (deserts, oceans, Antarctica). \
If a destination fails, you'll be asked to pick a replacement immediately.
- When "Research about current location" is provided, use it to create a thematic connection \
to the next destination. Explain the connection in your narration. Examples:
  - Buddhist temple in Tokyo → suggest Cape Town (ancient spiritual traditions worldwide)
  - Victorian architecture in Melbourne → suggest Edinburgh (shared colonial era)
  - Street food market in Marrakech → suggest Mexico City (street food culture)
  State the connection so viewers understand why you chose the next city. \
Format: "From [current] we travel to [next] — both [shared theme]."
"""


OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "arcee-ai/trinity-large-preview:free")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


_REGION_KEYWORDS = {
    "Asia": ["Japan", "China", "Korea", "Thailand", "Vietnam", "Cambodia", "Laos", "Myanmar",
             "Indonesia", "Malaysia", "Singapore", "Philippines", "India", "Nepal", "Sri Lanka",
             "Taiwan", "Hong Kong", "Bali", "Ubud"],
    "Europe": ["France", "Spain", "Italy", "Germany", "UK", "England", "Scotland", "Ireland",
               "Portugal", "Greece", "Netherlands", "Czech", "Austria", "Switzerland", "Sweden",
               "Norway", "Denmark", "Finland", "Iceland", "Croatia", "Poland", "Hungary",
               "Romania", "Belgium", "Turkey", "Russia"],
    "Americas": ["USA", "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Peru", "Chile",
                 "Cuba", "Costa Rica", "Ecuador", "Bolivia", "Uruguay", "New York", "Havana"],
    "Africa": ["Morocco", "Egypt", "South Africa", "Kenya", "Tanzania", "Ethiopia", "Ghana",
               "Senegal", "Nigeria", "Tunisia", "Namibia", "Rwanda", "Madagascar"],
    "Middle East": ["UAE", "Dubai", "Jordan", "Israel", "Oman", "Qatar", "Saudi", "Iran",
                    "Lebanon", "Petra", "Jerusalem", "Istanbul"],
    "Oceania": ["Australia", "New Zealand", "Fiji", "Sydney", "Melbourne"],
}

def _detect_region(place: str) -> str:
    for region, keywords in _REGION_KEYWORDS.items():
        if any(kw.lower() in place.lower() for kw in keywords):
            return region
    return "Other"

async def call_agent(history: list, chat_msgs: list[str]) -> dict:
    """Ask OpenRouter Trinity (free) to plan the next show segment."""
    visited_all = state["visited"]
    research = state.get("research", "")

    # Build region summary for diversity enforcement
    recent_regions = [_detect_region(v) for v in visited_all[-10:]]
    region_counts = {}
    for r in [_detect_region(v) for v in visited_all]:
        region_counts[r] = region_counts.get(r, 0) + 1
    region_summary = ", ".join(f"{r}: {c}" for r, c in sorted(region_counts.items(), key=lambda x: -x[1]))

    # Detect if stuck in one region
    last_5_regions = recent_regions[-5:] if len(recent_regions) >= 5 else recent_regions
    unique_recent = set(last_5_regions)
    diversity_warning = ""
    if len(unique_recent) == 1 and last_5_regions:
        stuck_region = last_5_regions[0]
        other_regions = [r for r in ["Europe", "Americas", "Africa", "Middle East", "Oceania", "Asia"]
                         if r != stuck_region]
        diversity_warning = (
            f"\n⚠️ WARNING: You've been in {stuck_region} for the last {len(last_5_regions)} cities! "
            f"You MUST pick a city in one of these regions NOW: {', '.join(other_regions)}. "
            f"Suggestions: Paris, Buenos Aires, Cape Town, Marrakech, Sydney, Reykjavik, "
            f"Istanbul, Cairo, Lisbon, Havana, Cusco, Rome, Rio de Janeiro, Petra, Edinburgh."
        )

    # Only show places visited in the last 12h as "off limits" — older ones are fair game
    now_ts = time.time()
    visited_recent = [v for v in visited_all
                      if _visited_times.get(v, 0) > now_ts - VISITED_EXPIRY_HOURS * 3600]

    user_content = (
        f"Current state:\n"
        f"- Location: {state['current'] or 'none'}\n"
        f"- Mode: {state['mode']}\n"
        f"- Steps walked: {state['step']}\n"
        f"- Total destinations visited: {state['total']}\n"
        f"- Research about current location: {research or 'none'}\n"
        f"- Region diversity so far: {region_summary}\n"
        f"- Last 5 regions visited: {' → '.join(recent_regions[-5:]) or 'none'}\n"
        f"- VISITED IN LAST 12H (do NOT repeat): {', '.join(visited_recent[-30:]) or 'none'}\n"
        f"- ALREADY BROWSED URLS (do NOT revisit): {', '.join(_visited_urls[-10:]) or 'none'}\n"
        f"- Pending !go requests: {', '.join(state['queue'][:3]) or 'none'}\n"
        f"- Recent audience chat: {'; '.join(chat_msgs[-5:]) or 'none'}\n"
        f"{diversity_warning}\n\n"
        f"Plan the next segment. Find a destination that connects thematically or geographically "
        f"to the current location — build a narrative journey the audience can follow. "
        f"Vary the region every 2-3 cities to keep the show global."
        + (f" Use the research to find a vivid thematic link to the next city." if research else "")
    )

    # Build full prompt: system + conversation history + new user message
    history.append({"role": "user", "content": user_content})
    turns = "\n\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in history[-10:]   # last 10 turns for context
    )
    full_prompt = f"{AGENT_SYSTEM}\n\n---\n\n{turns}"

    raw = ""
    try:
        import httpx as _httpx
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        messages = [{"role": "system", "content": AGENT_SYSTEM}] + history[-10:]
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 512,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with _httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(OPENROUTER_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # Strip markdown fences if model wraps response
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()

        plan = json.loads(raw)
        history.append({"role": "assistant", "content": raw})
        if len(history) > 20:
            history[:] = history[-20:]

        dest = plan.get("destination", plan.get("url", "—"))
        print(f"[agent] {plan.get('action')} → {dest}", flush=True)
        print(f"[agent] narration: {plan.get('narration', '')}", flush=True)
        _emit("agent_plan",
              action=plan.get("action"),
              dest=dest,
              narration=plan.get("narration", ""),
              reason=plan.get("reason", ""))
        return plan

    except asyncio.TimeoutError:
        print("[agent] timeout", flush=True)
        return {"action": "explore", "narration": "Exploring…"}
    except json.JSONDecodeError:
        print(f"[agent] JSON parse error, raw: {raw[:120]}", flush=True)
        return {"action": "explore", "narration": "Exploring…"}
    except Exception as e:
        print(f"[agent] error: {e}", flush=True)
        return {"action": "explore", "narration": "Exploring…"}


_FALLBACK_CITIES = [
    "Lisbon, Portugal", "Buenos Aires, Argentina", "Cape Town, South Africa",
    "Reykjavik, Iceland", "Cusco, Peru", "Edinburgh, Scotland",
    "Dubrovnik, Croatia", "Havana, Cuba", "Kyoto, Japan",
    "Rio de Janeiro, Brazil", "Istanbul, Turkey", "Cairo, Egypt",
    "Melbourne, Australia", "Petra, Jordan", "Cartagena, Colombia",
    "Prague, Czech Republic", "Hanoi, Vietnam", "Nairobi, Kenya",
    "Bergen, Norway", "Valletta, Malta", "Tallinn, Estonia",
    "Montevideo, Uruguay", "Muscat, Oman", "Luang Prabang, Laos",
    "Accra, Ghana", "Tbilisi, Georgia", "Bogota, Colombia",
    "Seville, Spain", "Bruges, Belgium", "Queenstown, New Zealand",
]


def _is_visited(dest: str, max_age_hours: float = VISITED_EXPIRY_HOURS) -> bool:
    """Fuzzy dedup: check if dest was visited within the last max_age_hours.

    Catches cases like 'Djemaa el-Fna, Marrakech, Morocco' matching
    'Jemaa el-Fnaa' or 'Marrakech' in the visited times dict.
    Destinations older than max_age_hours are eligible again (12h default).
    """
    now = time.time()
    cutoff = now - max_age_hours * 3600
    dest_lower = dest.lower()
    dest_words = [w for w in re.split(r'[,\s]+', dest_lower) if len(w) >= 3]
    for v, ts in _visited_times.items():
        if ts < cutoff:
            continue  # old enough to revisit
        v_lower = v.lower()
        if dest_lower == v_lower:
            return True
        if dest_lower in v_lower or v_lower in dest_lower:
            return True
        v_words = [w for w in re.split(r'[,\s]+', v_lower) if len(w) >= 3]
        if len(set(dest_words) & set(v_words)) >= 2:
            return True
    return False


def _pick_fallback_city() -> str:
    """Pick a random fallback city that hasn't been visited."""
    visited_lower = {v.lower() for v in state["visited"]}
    candidates = [c for c in _FALLBACK_CITIES if c.lower() not in visited_lower
                  and not _is_visited(c)]
    if not candidates:
        candidates = _FALLBACK_CITIES  # all visited? just pick any
    return random.choice(candidates)


async def execute_plan(plan: dict, maps_tab: str, last_place: str, from_chat: bool = False):
    """Execute an agent action plan.

    from_chat=True: destination came from a viewer !go request — skip dedup so
    the audience always gets taken where they asked.
    """
    action   = plan.get("action", "explore")
    narration = plan.get("narration", "")
    state["narration"] = narration

    if action == "streetview":
        dest = plan.get("destination")
        if not dest:
            print("[agent] streetview missing destination, exploring", flush=True)
            return
        if dest in state["queue"]:
            state["queue"].remove(dest)

        if not from_chat:
            # Track consecutive agent picks to detect stuck loops (AI only, not !go)
            _recent_agent_dests.append(dest.lower())
            if len(_recent_agent_dests) > 5:
                _recent_agent_dests[:] = _recent_agent_dests[-5:]

            # Stuck loop: AI picked the exact same dest twice in a row
            if len(_recent_agent_dests) >= 2:
                last_two = _recent_agent_dests[-2:]
                if last_two[0] == last_two[1]:
                    fallback = _pick_fallback_city()
                    print(f"[agent] stuck loop detected ({dest}), forcing fallback: {fallback}", flush=True)
                    _emit("stuck_fallback", original=dest, fallback=fallback)
                    dest = fallback
                    narration = f"Switching things up — heading to {dest}!"
                    state["narration"] = narration
                    _recent_agent_dests[-1] = dest.lower()

            # Dedup: reject if visited within last 12h (AI picks only)
            if _is_visited(dest):
                print(f"[agent] skipping repeat destination: {dest}", flush=True)
                state["narration"] = f"Already visited {dest} — picking somewhere new…"
                _emit("repeat_skipped", dest=dest)
                now_ts = time.time()
                visited_recent = [v for v in state["visited"]
                                  if _visited_times.get(v, 0) > now_ts - VISITED_EXPIRY_HOURS * 3600]
                retry_history = [{"role": "user", "content":
                    f"You suggested '{dest}' but we visited it recently. "
                    f"Pick a COMPLETELY DIFFERENT place we haven't been to in the last 12h. "
                    f"Recently visited: {', '.join(visited_recent[-15:])}. "
                    f"Respond with JSON."}]
                retry_plan = await call_agent(retry_history, [])
                if retry_plan.get("action") == "streetview" and retry_plan.get("destination"):
                    dest = retry_plan["destination"]
                    narration = retry_plan.get("narration", f"Heading to {dest}…")
                    state["narration"] = narration
                else:
                    dest = _pick_fallback_city()
                    narration = f"Off to {dest} — a fresh destination!"
                    state["narration"] = narration
                    print(f"[agent] dedup retry failed, using fallback: {dest}", flush=True)

        # Record the destination so future dedup catches it
        if dest not in state["visited"]:
            state["visited"].append(dest)
            _visited_times[dest] = time.time()

        ok = await go_to_city(dest, maps_tab)
        if ok:
            # Announce in Twitch chat
            await twitch_say(f"📍 Now exploring: {dest}")
            # Narrate AFTER arriving so voice matches what's on screen
            if narration:
                await speak(narration)
        else:
            print(f"[agent] no street view for '{dest}', retrying with new location", flush=True)
            state["narration"] = f"No Street View at {dest} — picking somewhere new…"
            _emit("no_coverage", dest=dest)
            retry_history = [{"role": "user", "content":
                f"'{dest}' has no Street View coverage. Pick a DIFFERENT city "
                f"that definitely has good Street View. Prefer major cities. "
                f"Avoid remote/rural areas. Respond with JSON."}]
            retry_plan = await call_agent(retry_history, [])
            if retry_plan.get("action") == "streetview" and retry_plan.get("destination"):
                retry_dest = retry_plan["destination"]
                retry_narr = retry_plan.get("narration", f"Rerouting to {retry_dest}…")
                state["narration"] = retry_narr
                ok2 = await go_to_city(retry_dest, maps_tab)
                if ok2 and retry_narr:
                    await speak(retry_narr)
                elif not ok2:
                    print(f"[agent] retry also failed for '{retry_dest}'", flush=True)
                    state["status"] = "exploring"
            else:
                state["status"] = "exploring"

    elif action == "website":
        url = plan.get("url")
        if not url:
            print("[agent] website missing url, exploring", flush=True)
            return
        if url in _visited_urls:
            print(f"[agent] skipping repeat url: {url}", flush=True)
            return
        _visited_urls.append(url)
        if narration:
            await speak(narration)
        await visit_website(url, maps_tab, dwell_secs=15)
        # Return to Maps after the website visit
        if last_place:
            state["status"] = "returning to maps…"
            state["narration"] = f"Back to {last_place}…"
            await go_to(last_place, maps_tab)

    else:  # explore
        if narration:
            await speak(narration)
        state["status"] = "exploring"
        print("[agent] continuing current exploration", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    _check_pid_file()
    args = sys.argv[1:]

    # Select Chrome profile BEFORE any CDP operations (--profile flag or CDP_PROFILE env)
    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            _select_profile(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
    elif os.environ.get("CDP_PROFILE"):
        _select_profile(os.environ["CDP_PROFILE"])

    maps_tab       = None
    chat_tab       = None
    no_chat        = False
    start_place    = DEFAULT_START
    obs_enabled    = False
    obs_no_stream  = False
    obs_host       = OBS_HOST
    obs_port       = OBS_PORT
    obs_password   = OBS_PASSWORD
    agent_interval = AGENT_INTERVAL
    launch_ctl     = True           # open control terminal by default
    docker_mode    = False

    i = 0
    while i < len(args):
        a = args[i]
        if   a == "--maps-tab"     and i+1 < len(args): maps_tab = args[i+1]; i += 2
        elif a == "--chat-tab"     and i+1 < len(args): chat_tab = args[i+1]; i += 2
        elif a == "--start"        and i+1 < len(args): start_place = args[i+1]; i += 2
        elif a == "--interval"     and i+1 < len(args): agent_interval = int(args[i+1]); i += 2
        elif a == "--no-chat":      no_chat = True; i += 1
        elif a == "--no-ctl":       launch_ctl = False; i += 1
        elif a == "--no-go-live":   obs_no_stream = True; i += 1
        elif a == "--docker":       docker_mode = True; i += 1
        elif a == "--obs":          obs_enabled = True; i += 1
        elif a == "--obs-host"     and i+1 < len(args): obs_host = args[i+1]; obs_enabled = True; i += 2
        elif a == "--obs-port"     and i+1 < len(args): obs_port = int(args[i+1]); obs_enabled = True; i += 2
        elif a == "--obs-password" and i+1 < len(args): obs_password = args[i+1]; obs_enabled = True; i += 2
        else: i += 1

    # Tab discovery
    if not maps_tab:
        maps_tab = find_tab("google.com/maps") or find_tab("maps.google")
    if not maps_tab:
        print("[setup] opening Google Maps…")
        maps_tab = _create_new_tab(MAPS_BASE)["id"]
        await asyncio.sleep(3)

    global _maps_tab_id, _chat_tab_id
    _maps_tab_id = maps_tab

    if not chat_tab and not no_chat:
        chat_tab = (find_tab("twitch.tv/popout") or
                    find_tab("twitch.tv") or
                    find_tab("studio.youtube.com") or
                    find_tab("youtube.com/live_chat") or
                    find_tab("youtube.com"))
        if not chat_tab:
            # Try opening Twitch popout chat
            try:
                info = _create_new_tab("https://www.twitch.tv/popout/unchainedskytv/chat")
                chat_tab = info["id"]
                print(f"[setup] opened Twitch popout chat")
                await asyncio.sleep(3)
            except Exception:
                print("[setup] no chat tab found — running without chat")
                no_chat = True

    _chat_tab_id = chat_tab if not no_chat else None

    print(f"[setup] maps tab:  {maps_tab[:16]}")
    if chat_tab:
        print(f"[setup] chat tab:  {chat_tab[:16]}")
    print(f"[setup] agent interval: {agent_interval}s")
    if docker_mode:
        print("[setup] Docker mode — overlay via CDP, capture via FFmpeg")

    await start_overlay_server()
    await start_ctl_server()

    if launch_ctl and not docker_mode:
        await launch_ctl_terminal()

    if obs_enabled and not docker_mode:
        ok = await obs_connect(obs_host, obs_port, obs_password)
        if ok:
            if not obs_no_stream:
                await obs_start_stream()
            else:
                print("[obs] connected — stream NOT started (--no-go-live)")

    # Background music — lofi stream in a hidden tab (skip in Docker mode)
    LOFI_URL = "https://www.youtube.com/watch?v=jfKfPfyJRdk"  # lofi hip hop radio
    music_tab = None if docker_mode else find_tab("youtube.com/watch")

    async def _start_lofi(tab_id: str, fresh: bool):
        """Activate tab, reload if stream drifted, CDP-click to unblock autoplay, lock volume."""
        import urllib.request as _ur
        try:
            _ur.urlopen(_ur.Request(
                f"http://{_cdp_mod.CDP_HOST}:{_cdp_mod.DEBUG_PORT}/json/activate/{tab_id}"), timeout=2)
        except Exception:
            pass
        await asyncio.sleep(3 if fresh else 1)
        c = await get_cdp(tab_id)

        # Reload if the live stream currentTime has drifted far ahead (stale buffer)
        drift = await c.execute_js("var v=document.querySelector('video'); v ? v.currentTime : 0")
        if (drift.get("result", {}).get("value") or 0) > 7200:  # >2h ahead = stale
            await c.navigate(LOFI_URL, wait=6)
            await asyncio.sleep(3)

        # Dismiss popups
        await c.execute_js("""
            document.querySelectorAll('button').forEach(function(b) {
                var t = b.textContent.trim().toLowerCase();
                var l = (b.getAttribute('aria-label') || '').toLowerCase();
                if (t === 'dismiss' || t === 'no thanks' || t === 'not now'
                    || t === 'got it' || l.includes('dismiss')) b.click();
            });
        """)
        await asyncio.sleep(0.5)

        # Skip ads — poll up to 3 minutes (YouTube pre-rolls can be 2 min unskippable)
        # Uses userGesture:true so the skip click registers even in a background tab.
        ads_skipped = 0
        for _ in range(180):
            r = await c.send("Runtime.evaluate", {
                "expression": """
                    (function() {
                        var skip = document.querySelector('.ytp-skip-ad-button')
                                || document.querySelector('.ytp-ad-skip-button-modern')
                                || document.querySelector('[id^="skip-button"]')
                                || document.querySelector('.ytp-ad-skip-button');
                        if (skip && skip.offsetParent !== null) { skip.click(); return 'skipped'; }
                        var countdown = document.querySelector('.ytp-ad-duration-remaining, .ytp-timed-visitable-ad-block');
                        if (document.querySelector('.ad-showing') || countdown) return 'ad_playing';
                        return 'no_ad';
                    })()
                """,
                "userGesture": True,
                "returnByValue": True,
            })
            val = (r.get("result", {}).get("value") or "no_ad")
            if val == "skipped":
                ads_skipped += 1
                print(f"[music] skipped ad #{ads_skipped}", flush=True)
                await asyncio.sleep(2)  # wait for next ad or content to load
                continue
            elif val == "no_ad":
                break
            await asyncio.sleep(1)  # ad_playing — keep waiting

        # Play with userGesture:true — bypasses autoplay policy in background tabs
        await c.send("Runtime.evaluate", {
            "expression": "var v=document.querySelector('video'); if(v){v.volume=0.12; v.play();}",
            "userGesture": True,
        })
        await asyncio.sleep(1)

        paused = await c.execute_js("var v=document.querySelector('video'); v ? v.paused : true")
        if paused.get("result", {}).get("value", True):
            # Fallback: click play button with userGesture
            await c.send("Runtime.evaluate", {
                "expression": "var b=document.querySelector('.ytp-play-button'); if(b) b.click();",
                "userGesture": True,
            })
            await asyncio.sleep(1)

        # Lock volume so YouTube can't reset it
        await c.execute_js("""
            var v = document.querySelector('video');
            if (v) {
                v.volume = 0.12;
                Object.defineProperty(v, 'volume', {
                    get: function() { return 0.12; },
                    set: function() {},
                    configurable: true
                });
            }
        """)
        paused2 = await c.execute_js("var v=document.querySelector('video'); v ? v.paused : true")
        playing = not paused2.get("result", {}).get("value", True)
        print(f"[music] {'playing' if playing else 'WARNING: still paused'} (vol 0.12)", flush=True)

    _lofi_proc = None
    _lofi_url = ""
    if docker_mode:
        # Play lofi music via ffplay directly to PulseAudio (no browser tab, no ads)
        try:
            _lofi_url = os.environ.get("LOFI_STREAM_URL", "")
            if not _lofi_url:
                # Fallback lofi/chill internet radio streams (royalty-free, no auth)
                _lofi_streams = [
                    "https://play.streamafrica.net/lofiradio",
                    "https://streams.ilovemusic.de/iloveradio17.mp3",
                    "http://stream.laut.fm/lofi",
                ]
                # Test which stream responds
                import urllib.request as _urlreq
                for url in _lofi_streams:
                    try:
                        req = _urlreq.Request(url, method="HEAD")
                        resp = _urlreq.urlopen(req, timeout=5)
                        if resp.status == 200:
                            _lofi_url = url
                            print(f"[music] found working stream: {url}", flush=True)
                            break
                    except Exception:
                        continue
            if _lofi_url:
                _lofi_proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                     "-volume", "5", _lofi_url],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"[music] lofi playing via ffplay (PID {_lofi_proc.pid})", flush=True)
            else:
                print("[music] no working lofi stream found (set LOFI_STREAM_URL env)", flush=True)
        except Exception as e:
            print(f"[music] lofi error: {e}", flush=True)
    elif not music_tab:
        try:
            info = _create_new_tab(LOFI_URL)
            music_tab = info["id"]
            print(f"[setup] music tab: {music_tab[:16]}")
            await asyncio.sleep(5)
            await _start_lofi(music_tab, fresh=True)
            await _activate_maps_tab()
        except Exception as e:
            print(f"[setup] music tab error: {e}")
            await _activate_maps_tab()
    else:
        try:
            print(f"[setup] music tab: {music_tab[:16]} (existing)")
            await _start_lofi(music_tab, fresh=False)
            await _activate_maps_tab()
        except Exception as e:
            print(f"[setup] music tab error: {e}")

    # ── Twitch chat !go reader (Docker mode) ────────────────────────────────
    global _twitch_writer, _twitch_channel
    _twitch_channel = os.environ.get("TWITCH_CHANNEL", "")
    twitch_token = os.environ.get("TWITCH_CHAT_ACCESS_TOKEN", "")

    if docker_mode and _twitch_channel:
        async def _twitch_chat_reader():
            global _twitch_writer
            import re as _re
            while True:
                try:
                    reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6667)
                    if twitch_token:
                        writer.write(f"PASS oauth:{twitch_token}\r\n".encode())
                        writer.write(f"NICK {_twitch_channel.lower()}\r\n".encode())
                    else:
                        writer.write(b"PASS SCHMOOPIIE\r\n")
                        writer.write(f"NICK justinfan{os.getpid()}\r\n".encode())
                    writer.write(f"JOIN #{_twitch_channel.lower()}\r\n".encode())
                    await writer.drain()
                    _twitch_writer = writer
                    print(f"[twitch] connected to #{_twitch_channel} chat ({'auth' if twitch_token else 'read-only'})", flush=True)
                    # Welcome message
                    if twitch_token:
                        await twitch_say("🤖 Unchained Sky is live! Type !go <place> to send me somewhere!")
                    while True:
                        line = await asyncio.wait_for(reader.readline(), timeout=300)
                        if not line:
                            break
                        msg = line.decode("utf-8", errors="replace").strip()
                        if msg.startswith("PING"):
                            writer.write(b"PONG :tmi.twitch.tv\r\n")
                            await writer.drain()
                            continue
                        # Parse PRIVMSG for !go commands
                        m = _re.search(r"PRIVMSG\s+#\S+\s+:(.+)", msg)
                        if m:
                            text = m.group(1).strip()
                            if text.lower().startswith("!go "):
                                dest = text[4:].strip()
                                if dest:
                                    state["queue"].append(dest)
                                    _recent_chat.append(f"!go {dest}")
                                    print(f"[twitch] !go {dest}", flush=True)
                                    if twitch_token:
                                        await twitch_say(f"📍 Queued: {dest}")
                except asyncio.TimeoutError:
                    print("[twitch] timeout, reconnecting…", flush=True)
                    _twitch_writer = None
                except Exception as e:
                    print(f"[twitch] error: {e}, reconnecting in 10s…", flush=True)
                    _twitch_writer = None
                await asyncio.sleep(10)

        asyncio.create_task(_twitch_chat_reader())
        print(f"[setup] Twitch chat: #{_twitch_channel} ({'read+write' if twitch_token else 'read-only'})")
    elif docker_mode:
        print("[setup] No TWITCH_CHANNEL set — chat !go disabled")

    print(f"[setup] start:     {start_place}")
    print("[stream] running — Ctrl+C to stop\n")

    # TTS welcome
    await speak(f"Welcome to Unchained Sky. Starting our journey at {start_place}.")

    # Welcome message in chat
    if chat_tab and not no_chat:
        await send_chat(chat_tab,
            "Hey! I'm an AI agent exploring the world live. "
            "Type !go <place> to send me somewhere! e.g. !go Kyoto Japan")

    # Dismiss Google consent dialog (Docker fresh profile)
    if docker_mode:
        try:
            c = await get_cdp(maps_tab)
            await c.execute_js("""
                document.querySelectorAll('button').forEach(function(b) {
                    var t = b.textContent.trim().toLowerCase();
                    if (t === 'accept all' || t === 'reject all' || t === 'i agree'
                        || t === 'consent' || t === 'got it') b.click();
                });
            """)
            await asyncio.sleep(1)
        except Exception:
            pass

    await get_cdp(maps_tab)
    conversation_history: list = []

    state["narration"] = f"Starting journey at {start_place} · unchainedsky.com"
    ok = await go_to_city(start_place, maps_tab)
    if not ok:
        state["narration"] = "Couldn't load start location, AI picking next…"

    # Docker mode: inject overlay into Maps page and start FFmpeg capture
    if docker_mode:
        await inject_overlay(maps_tab)
        await update_overlay(maps_tab)
        # Determine output target: RTMP_URL > YouTube stream key > file fallback
        rtmp_url = os.environ.get("RTMP_URL", "")
        yt_key = os.environ.get("YOUTUBE_STREAM_KEY", "")
        if rtmp_url:
            ffmpeg_target = rtmp_url
        elif yt_key:
            ffmpeg_target = f"rtmp://a.rtmp.youtube.com/live2/{yt_key}"
        else:
            os.makedirs("/output", exist_ok=True)
            ffmpeg_target = "/output/stream.flv"
        print(f"[docker] FFmpeg target: {ffmpeg_target}", flush=True)
        start_ffmpeg(ffmpeg_target)

    # Pin OBS window capture AFTER first navigation so the Maps tab has a real
    # title ("Sydney Opera House - Google Maps") rather than "New Tab".
    if obs_enabled and _obs_ws and not docker_mode:
        maps_title = await js_eval(maps_tab, "document.title") or "Google Maps"
        await obs_configure_window_capture(maps_title)

    last_agent_call = time.time()
    last_chat_poll  = 0.0
    last_step       = 0.0
    last_guard      = 0.0
    last_overlay    = 0.0
    last_music_check = 0.0
    GUARD_INTERVAL  = 30          # check window position every 30s
    OVERLAY_UPDATE  = 2           # update injected overlay every 2s
    MUSIC_CHECK_INTERVAL = 60     # check lofi music process every 60s

    try:
      while True:
        now = time.time()

        # Window guard — keep streaming Chrome at the expected position (skip in Docker)
        if not docker_mode and (now - last_guard) >= GUARD_INTERVAL:
            await _enforce_window_bounds()
            last_guard = now

        # Docker overlay update
        if docker_mode and (now - last_overlay) >= OVERLAY_UPDATE:
            await update_overlay(maps_tab)
            last_overlay = now

        # Lofi music watchdog — restart ffplay if it died
        if docker_mode and _lofi_url and (now - last_music_check) >= MUSIC_CHECK_INTERVAL:
            if _lofi_proc is None or _lofi_proc.poll() is not None:
                print(f"[music] watchdog: restarting lofi (was {'dead' if _lofi_proc else 'never started'})", flush=True)
                try:
                    _lofi_proc = subprocess.Popen(
                        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                         "-volume", "5", _lofi_url],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print(f"[music] watchdog: lofi restarted (PID {_lofi_proc.pid})", flush=True)
                except Exception as _me:
                    print(f"[music] watchdog restart error: {_me}", flush=True)
            last_music_check = now

        # Periodic chat reminder about !go syntax
        if chat_tab and not no_chat and state["total"] > 0 and state["total"] % 10 == 0:
            if state.get("_last_reminder") != state["total"]:
                state["_last_reminder"] = state["total"]
                await send_chat(chat_tab,
                    f"We've visited {state['total']} places! "
                    "Type !go <place> to pick the next destination!")

        # Poll chat
        if chat_tab and not no_chat and (now - last_chat_poll) >= CHAT_POLL_SECS:
            new = await read_chat(chat_tab)
            for d in new:
                if d not in state["queue"] and d != state["current"]:
                    state["queue"].append(d)
                    _recent_chat.append(f"!go {d}")
                    print(f"[chat] queued: {d}")
                    _emit("chat", dest=d)
            if len(_recent_chat) > 30:
                _recent_chat[:] = _recent_chat[-30:]
            last_chat_poll = now

        # Drain direct control commands from stream_ctl
        while not _pending_actions.empty():
            pending = await _pending_actions.get()
            if pending["action"] == "skip":
                last_agent_call = 0       # force agent on next tick
                state["city_queue"] = []  # abort current city tour
            elif pending["action"] == "website":
                prev = state["current"]
                await visit_website(pending["url"], maps_tab)
                if prev:
                    await go_to(prev, maps_tab)

        # City tour: advance to next spot before calling agent
        # Wait for TTS to finish before moving on
        spot_ready  = (state["city_queue"] and
                       state["step"] >= STEPS_PER_SPOT and
                       state["mode"] == "streetview" and
                       not state["queue"] and       # honour !go requests first
                       not _tts_speaking)            # let narrator finish

        # Agent decision — on schedule, step cap, or chat request
        agent_due   = (now - last_agent_call) >= agent_interval
        steps_done  = state["step"] >= MAX_STEPS and state["mode"] == "streetview"
        chat_urgent = bool(state["queue"]) and state["mode"] == "streetview"

        if spot_ready:
            await go_to_next_spot(maps_tab)
            last_step = 0.0

        # City exhausted: arrived but no more spots to visit — skip ahead immediately
        elif (not _agent_paused and not _tts_speaking and
              not state["city_queue"] and state["step"] >= STEPS_PER_SPOT and
              state["mode"] == "streetview"):
            print("[maps] city exhausted (no more spots) — skipping to next city", flush=True)
            state["city_queue"] = []
            prev_place = state["current"]

            # Honor !go requests directly when city tour is done
            if state["queue"]:
                dest = state["queue"].pop(0)
                print(f"[chat] honoring !go request: {dest}", flush=True)
                set_thinking(f"Heading to {dest} (viewer request)…")
                state["narration"] = f"A viewer requested {dest} — let's go!"
                _emit("chat", dest=dest)
                plan = {
                    "action": "streetview",
                    "destination": dest,
                    "narration": f"A viewer requested {dest} — let's go!",
                    "reason": "Honoring !go request from chat",
                }
                set_thinking("")
                await execute_plan(plan, maps_tab, prev_place, from_chat=True)
            else:
                set_thinking("AI choosing next destination…")
                state["narration"] = "AI planning next destination…"
                _emit("thinking")
                plan = await call_agent(conversation_history, _recent_chat)
                set_thinking("")
                await execute_plan(plan, maps_tab, prev_place)
            last_agent_call = time.time()
            last_step = 0.0

        elif not _agent_paused and not _tts_speaking and (agent_due or steps_done or chat_urgent):
            state["city_queue"] = []   # clear tour when agent picks next city
            prev_place = state["current"]

            # Handle !go requests directly instead of relying on the AI model
            if chat_urgent and state["queue"]:
                dest = state["queue"].pop(0)
                print(f"[chat] honoring !go request: {dest}", flush=True)
                set_thinking(f"Heading to {dest} (viewer request)…")
                state["narration"] = f"A viewer requested {dest} — let's go!"
                _emit("chat", dest=dest)
                plan = {
                    "action": "streetview",
                    "destination": dest,
                    "narration": f"A viewer requested {dest} — let's go!",
                    "reason": "Honoring !go request from chat",
                }
                set_thinking("")
                await execute_plan(plan, maps_tab, prev_place, from_chat=True)
            else:
                set_thinking("AI choosing next destination…")
                state["narration"] = "AI planning next destination…"
                _emit("thinking")
                plan = await call_agent(conversation_history, _recent_chat)
                set_thinking("")
                await execute_plan(plan, maps_tab, prev_place)
            last_agent_call = time.time()
            last_step = 0.0

        # Street View stepping
        elif state["mode"] == "streetview" and (now - last_step) >= STEP_DELAY:
            await step_forward(maps_tab)
            if state["step"] % 7 == 0:
                await step_turn(maps_tab)
            # Every 5 steps check for black screen (walked off coverage)
            if state["step"] % 5 == 0 and await is_black_screen(maps_tab):
                print("[maps] walked into black screen — forcing agent skip", flush=True)
                await _pending_actions.put({"action": "skip"})
            last_step = now

        await asyncio.sleep(0.5)

    finally:
        if docker_mode:
            stop_ffmpeg()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stream] stopped")
    finally:
        stop_ffmpeg()
        _remove_pid_file()
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(obs_stop_stream())
            loop.run_until_complete(obs_close())
            loop.run_until_complete(close_all())
        except Exception:
            pass
