#!/usr/bin/env python3
"""
maps_stream.py — Unchained on Google Maps Live Stream

Controls a Chrome tab navigating Google Maps Street View based on
YouTube Live chat commands. Serves an OBS overlay on localhost:8877.

Usage:
    uv run maps_stream.py                          # auto-detect tabs
    uv run maps_stream.py --maps-tab <id>          # specify maps tab
    uv run maps_stream.py --chat-tab <id>          # specify YouTube chat tab
    uv run maps_stream.py --no-chat                # explore without chat
    uv run maps_stream.py --start "Kyoto Japan"    # set starting location
    uv run maps_stream.py --obs                    # auto-start OBS stream on launch
    uv run maps_stream.py --obs-password secret    # OBS WebSocket password
    uv run maps_stream.py --obs-port 4455          # OBS WebSocket port (default 4455)
    uv run maps_stream.py --obs-host 127.0.0.1     # OBS WebSocket host

OBS setup:
    1. Tools → WebSocket Server Settings → enable, set password
    2. Add Browser Source → http://127.0.0.1:8877/overlay
    3. Set width=860, height=80, transparent background
    4. Position at the bottom of your scene
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import random
import urllib.parse
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import CDP, _list_tabs, _create_new_tab, _get_ws_url_for_tab

# Load .env.obs if present (OBS credentials)
_env_obs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env.obs")
if os.path.exists(_env_obs):
    with open(_env_obs) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
OVERLAY_PORT    = 8877
OBS_HOST        = os.environ.get("OBS_HOST", "127.0.0.1")
OBS_PORT        = int(os.environ.get("OBS_PORT", "4455"))
OBS_PASSWORD    = os.environ.get("OBS_PASSWORD", "")
EXPLORE_SECS    = 90        # seconds to spend at each destination
STEP_DELAY      = 3.5       # seconds between Street View forward steps
CHAT_POLL_SECS  = 6         # seconds between YouTube chat reads
MAX_STEPS       = 22        # max forward steps before moving on
FAIL_BACKOFF    = 4         # seconds to wait after a go_to failure
DEFAULT_START   = "Times Square, New York"
MAPS_BASE       = "https://www.google.com/maps"

CITY_LIST = [
    "New York City", "London", "Paris", "Tokyo", "Sydney",
    "Rome", "Barcelona", "Amsterdam", "Dubai", "Singapore",
    "Hong Kong", "Istanbul", "Berlin", "Vienna", "Prague",
    "Bangkok", "Seoul", "Mumbai", "Buenos Aires", "São Paulo",
    "Mexico City", "Cairo", "Cape Town", "Toronto", "Vancouver",
    "San Francisco", "Los Angeles", "Chicago", "Miami", "New Orleans",
    "Lisbon", "Madrid", "Athens", "Copenhagen", "Stockholm",
    "Oslo", "Warsaw", "Budapest", "Taipei", "Manila",
    "Jakarta", "Kuala Lumpur", "Casablanca", "Nairobi", "Lagos",
    "Johannesburg", "Lima", "Bogotá", "Santiago", "Havana",
    "Dublin", "Edinburgh", "Brussels", "Zurich", "Milan",
    "Florence", "Naples", "Porto", "Seville", "Kraków",
    "Reykjavik", "Helsinki", "Osaka", "Hanoi", "Ho Chi Minh City",
    "Marrakech", "Addis Ababa", "Dar es Salaam", "Doha", "Riyadh",
]

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "current":  "",
    "queue":    [],
    "visited":  [],
    "total":    0,
    "status":   "starting",
    "step":     0,
}
_seen_chat = set()


# ── Overlay server ────────────────────────────────────────────────────────────
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
  background: rgba(0,0,0,0.78);
  border-radius: 10px;
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 16px;
  border: 1px solid rgba(255,255,255,0.08);
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
.queue {
  font-size: 11px;
  color: rgba(255,255,255,0.5);
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 0;
}
.stats { font-size: 11px; color: rgba(255,255,255,0.4); flex-shrink: 0; }
.status { font-size: 11px; color: rgba(255,255,255,0.45); flex-shrink: 0; }
.brand { font-size: 10px; color: rgba(255,255,255,0.35); white-space: nowrap; flex-shrink: 0; }
.brand b { color: #e94560; }
</style></head>
<body>
<div class="bar">
  <div class="live">LIVE</div>
  <div class="location"><span class="pin">📍</span><span id="loc">Starting…</span></div>
  <div class="queue" id="q"></div>
  <div class="stats" id="stats"></div>
  <div class="status" id="st"></div>
  <div class="brand">🤖 <b>Unchained</b> · api.unchainedsky.com</div>
</div>
<script>
async function refresh() {
  try {
    const d = await (await fetch('/state')).json();
    document.getElementById('loc').textContent = d.current || '…';
    const q = d.queue || [];
    document.getElementById('q').textContent =
      q.length ? 'next: ' + q.slice(0,2).join(' · ') + (q.length > 2 ? ' +'+(q.length-2) : '') : '';
    document.getElementById('stats').textContent = d.total ? d.total + ' visited' : '';
    document.getElementById('st').textContent = d.status || '';
  } catch(e) {}
}
refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""


def _kill_port(port: int):
    """Kill any process listening on port (prevents EADDRINUSE on restart)."""
    try:
        out = subprocess.check_output(["lsof", "-ti", f":{port}"], stderr=subprocess.DEVNULL, text=True)
        for pid in out.strip().split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGKILL)
        time.sleep(0.3)
    except (subprocess.CalledProcessError, ProcessLookupError, ValueError):
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
    await web.TCPSite(runner, "127.0.0.1", OVERLAY_PORT).start()
    print(f"[overlay] http://127.0.0.1:{OVERLAY_PORT}/overlay  (860×80)")


# ── Persistent CDP connections ────────────────────────────────────────────────
# One connection per tab, reused across all operations. Reconnect on error.

_conns: dict[str, CDP] = {}   # tab_id → CDP


async def get_cdp(tab_id: str) -> CDP:
    """Get or create a persistent CDP connection for a tab."""
    c = _conns.get(tab_id)
    if c and c.ws and c.ws.state.name == "OPEN":
        return c
    # Need a new connection
    try:
        if c:
            await c.close()
    except Exception:
        pass
    ws_url = _get_ws_url_for_tab(tab_id)
    c = CDP(ws_url)
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


# ── OBS WebSocket v5 client ───────────────────────────────────────────────────
_obs_ws = None


async def obs_connect(host: str = OBS_HOST, port: int = OBS_PORT, password: str = OBS_PASSWORD) -> bool:
    global _obs_ws
    import websockets
    uri = f"ws://{host}:{port}"
    try:
        ws = await websockets.connect(uri)
    except Exception as e:
        print(f"[obs] connect failed: {e}", flush=True)
        return False

    # Receive Hello (op 0)
    msg = json.loads(await ws.recv())
    if msg.get("op") != 0:
        print(f"[obs] unexpected hello: {msg}", flush=True)
        await ws.close()
        return False

    auth_str = ""
    auth_info = msg["d"].get("authentication")
    if auth_info and password:
        salt      = auth_info["salt"]
        challenge = auth_info["challenge"]
        secret    = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
        auth_str  = base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()

    # Send Identify (op 1)
    identify: dict = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
    if auth_str:
        identify["d"]["authentication"] = auth_str
    await ws.send(json.dumps(identify))

    # Receive Identified (op 2)
    msg = json.loads(await ws.recv())
    if msg.get("op") != 2:
        print(f"[obs] identify failed: {msg}", flush=True)
        await ws.close()
        return False

    _obs_ws = ws
    print(f"[obs] connected → {uri}", flush=True)
    return True


async def _obs_request(request_type: str, data: dict | None = None) -> dict | None:
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
        print(f"[obs] request error: {e}", flush=True)
    return None


async def obs_start_stream():
    r = await _obs_request("StartStream")
    if r:
        code = r.get("requestStatus", {}).get("code", 0)
        if code == 500:
            print("[obs] already streaming", flush=True)
        else:
            print("[obs] stream started", flush=True)


async def obs_stop_stream():
    r = await _obs_request("StopStream")
    if r:
        print("[obs] stream stopped", flush=True)


async def obs_close():
    global _obs_ws
    if _obs_ws:
        try:
            await _obs_ws.close()
        except Exception:
            pass
        _obs_ws = None


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
            "type": t, "x": x, "y": y, "button": "left", "clickCount": 1
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


# ── Tab discovery ─────────────────────────────────────────────────────────────
def find_tab(keyword: str) -> str | None:
    for t in _list_tabs():
        if keyword.lower() in t.get("url", "").lower() or keyword.lower() in t.get("title", "").lower():
            return t["id"]
    return None


# ── Chat reader / writer ──────────────────────────────────────────────────────
async def send_chat_message(chat_tab: str, message: str):
    """Post a message into the YouTube live chat input and press Enter."""
    try:
        c = await get_cdp(chat_tab)
        # Inject text into the contenteditable chat input and trigger the SPA's
        # input handler so it registers the new content before we press Enter.
        js = """
        (function(msg) {
            const input = document.querySelector('#simplebox-input')
                       || document.querySelector('#input[contenteditable]')
                       || document.querySelector('[contenteditable="true"]');
            if (!input) return false;
            input.focus();
            input.textContent = msg;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            return true;
        })(arguments[0])
        """
        # execute_js doesn't accept arguments directly — embed via JSON
        escaped = json.dumps(message)
        js_inline = f"""
        (function() {{
            const msg = {escaped};
            const input = document.querySelector('#simplebox-input')
                       || document.querySelector('#input[contenteditable]')
                       || document.querySelector('[contenteditable="true"]');
            if (!input) return false;
            input.focus();
            input.textContent = msg;
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            return true;
        }})()
        """
        result = await c.execute_js(js_inline)
        ok = result.get("result", {}).get("value", False)
        if not ok:
            print("[chat] send: input field not found", flush=True)
            return
        await asyncio.sleep(0.5)
        await send_key(chat_tab, "Enter", "Enter", 13)
        print(f"[chat] posted: {message}", flush=True)
    except Exception as e:
        print(f"[chat] send error: {e}", flush=True)


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
        m = re.search(r"!?go\s+(.+)", line, re.IGNORECASE)
        if m:
            dest = m.group(1).strip()[:80]
            if dest not in _seen_chat:
                _seen_chat.add(dest)
                found.append(dest)
    return found


# ── Screenshot validation ─────────────────────────────────────────────────────
_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic()
    return _anthropic_client


async def screenshot_b64(tab_id: str) -> str | None:
    """Capture a JPEG screenshot and return raw base64 data, or None on error."""
    try:
        c = await get_cdp(tab_id)
        result = await c.send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        return result.get("data")
    except Exception as e:
        print(f"[check] screenshot error: {e}", flush=True)
        return None


async def check_streetview_valid(tab_id: str, place: str) -> bool:
    """
    Take a screenshot and ask Claude Haiku whether the Street View looks valid.
    Returns True for outdoor scenes, False for blank/black screens or person photos.
    """
    b64 = await screenshot_b64(tab_id)
    if not b64:
        return True  # Can't check → assume OK, don't block on screenshot failure

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
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
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
        print(f"[check] '{place}' screenshot → {verdict}", flush=True)
        return verdict == "VALID"
    except Exception as e:
        print(f"[check] vision API error: {e}", flush=True)
        return True  # Fail open — don't skip locations on API errors


# ── Maps navigation ───────────────────────────────────────────────────────────
async def search_place(place: str, tab: str) -> tuple[float, float] | None:
    """Search Google Maps for a place, poll URL until @lat,lng appears."""
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
    """Navigate to Street View URL. Returns True if Street View loaded and looks valid."""
    url = f"https://maps.google.com/?q={lat},{lng}&layer=c&cbll={lat},{lng}"
    try:
        c = await get_cdp(tab)
        await c.navigate(url, wait=6)
    except Exception as e:
        print(f"[maps] streetview nav error: {e}")
        _conns.pop(tab, None)
        return False

    # DOM check: verify Street View canvas or URL markers are present
    await asyncio.sleep(1)
    try:
        in_sv = await js_eval(tab, """
            !!(document.querySelector('.widget-scene-canvas, canvas[aria-label]')
               || window.location.href.includes('!3m'))
        """)
        if not in_sv:
            href = await js_eval(tab, "window.location.href") or ""
            if "!3m" not in href and "layer=c" not in href:
                print(f"[maps] no street view canvas for '{place}'", flush=True)
                return False
    except Exception:
        pass  # DOM check failure → continue to visual check

    # Visual check: let the image render then verify it's an outdoor scene
    await asyncio.sleep(2)
    if not await check_streetview_valid(tab, place):
        return False

    return True


async def go_to(place: str, tab: str, chat_tab: str | None = None) -> bool:
    """Full: search → get coords → enter Street View → focus viewport."""
    print(f"[maps] → {place}", flush=True)
    state["status"] = "searching…"

    coords = await search_place(place, tab)
    if not coords:
        print(f"[maps] no coords for '{place}'", flush=True)
        state["status"] = "not found"
        return False

    lat, lng = coords
    print(f"[maps] coords: {lat:.4f}, {lng:.4f}", flush=True)
    state["status"] = "entering street view…"

    ok = await enter_streetview(lat, lng, tab, place)
    if not ok:
        print(f"[maps] street view failed for '{place}'", flush=True)
        state["status"] = "no street view"
        return False

    await asyncio.sleep(1.5)
    await click_center(tab)
    await asyncio.sleep(1)

    state["current"] = place
    state["total"]  += 1
    state["step"]    = 0
    state["status"]  = "exploring"
    state["visited"].append(place)
    print(f"[maps] arrived: {place}", flush=True)

    if chat_tab:
        msg = f"📍 {place} ({state['total']} visited)"
        await send_chat_message(chat_tab, msg)

    return True


async def go_to_city(city: str, tab: str, chat_tab: str | None = None) -> bool:
    """Explore a city via its Google Maps page, picking a featured attraction.

    More reliable than go_to() because city pages instantly surface curated
    places that already have imagery — no URL polling needed.
    Falls back to go_to() if attraction extraction fails.
    """
    print(f"[maps] city explore: {city}", flush=True)
    state["status"] = "exploring city…"

    attractions = await get_city_attractions(city, tab)
    if not attractions:
        print(f"[maps] no attractions for {city}, falling back to search", flush=True)
        return await go_to(city, tab, chat_tab)

    # Prefer places we haven't been to; shuffle for variety
    fresh = [(lat, lng, name) for lat, lng, name in attractions
             if name not in state["visited"]]
    candidates = fresh if fresh else attractions
    random.shuffle(candidates)

    for lat, lng, name in candidates[:6]:
        display = name if name and name != "attraction" else city
        print(f"[maps] → {display} ({lat:.4f}, {lng:.4f})", flush=True)
        state["status"] = "entering street view…"

        ok = await enter_streetview(lat, lng, tab, display)
        if not ok:
            print(f"[maps] SV failed for {display}", flush=True)
            continue

        await asyncio.sleep(1.5)
        await click_center(tab)
        await asyncio.sleep(1)

        state["current"] = display
        state["total"]  += 1
        state["step"]    = 0
        state["status"]  = "exploring"
        state["visited"].append(display)
        print(f"[maps] arrived: {display}", flush=True)

        if chat_tab:
            await send_chat_message(chat_tab, f"📍 {display} ({state['total']} visited)")

        return True

    # All candidates failed — fall back to direct search of city center
    print(f"[maps] all city attractions failed for {city}, trying search", flush=True)
    return await go_to(city, tab, chat_tab)


async def step_forward(tab: str):
    try:
        await send_key(tab, "ArrowUp", "ArrowUp", 38)
    except Exception as e:
        print(f"[maps] step error: {e}", flush=True)
        _conns.pop(tab, None)
    state["step"] += 1


async def step_turn(tab: str):
    """Pan left or right for variety."""
    k = random.choice([("ArrowLeft", "ArrowLeft", 37), ("ArrowRight", "ArrowRight", 39)])
    for _ in range(random.randint(2, 5)):
        try:
            await send_key(tab, *k)
        except Exception:
            _conns.pop(tab, None)
            break
        await asyncio.sleep(0.4)


# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    args = sys.argv[1:]
    maps_tab     = None
    chat_tab     = None
    no_chat      = False
    start_place  = DEFAULT_START
    obs_enabled  = False
    obs_host     = OBS_HOST
    obs_port     = OBS_PORT
    obs_password = OBS_PASSWORD

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--maps-tab" and i+1 < len(args):
            maps_tab = args[i+1]; i += 2
        elif a == "--chat-tab" and i+1 < len(args):
            chat_tab = args[i+1]; i += 2
        elif a == "--no-chat":
            no_chat = True; i += 1
        elif a == "--start" and i+1 < len(args):
            start_place = args[i+1]; i += 2
        elif a == "--obs":
            obs_enabled = True; i += 1
        elif a == "--obs-host" and i+1 < len(args):
            obs_host = args[i+1]; obs_enabled = True; i += 2
        elif a == "--obs-port" and i+1 < len(args):
            obs_port = int(args[i+1]); obs_enabled = True; i += 2
        elif a == "--obs-password" and i+1 < len(args):
            obs_password = args[i+1]; obs_enabled = True; i += 2
        else:
            i += 1

    # Auto-detect tabs
    if not maps_tab:
        maps_tab = find_tab("google.com/maps") or find_tab("maps.google")
    if not maps_tab:
        print("[setup] opening Google Maps tab…", flush=True)
        tab = _create_new_tab(MAPS_BASE)
        maps_tab = tab["id"]
        await asyncio.sleep(3)

    if not chat_tab and not no_chat:
        chat_tab = (find_tab("studio.youtube.com") or
                    find_tab("youtube.com/live_chat") or
                    find_tab("youtube.com"))
        if not chat_tab:
            print("[setup] no YouTube tab found — running without chat", flush=True)
            no_chat = True

    print(f"[setup] maps tab:  {maps_tab[:16]}", flush=True)
    if chat_tab:
        print(f"[setup] chat tab:  {chat_tab[:16]}", flush=True)
    else:
        print("[setup] chat:      disabled", flush=True)

    await start_overlay_server()

    if obs_enabled:
        ok = await obs_connect(obs_host, obs_port, obs_password)
        if ok:
            await obs_start_stream()

    print(f"[setup] start:     {start_place}", flush=True)
    print("[stream] running — Ctrl+C to stop\n", flush=True)

    # Warm up the persistent maps connection
    await get_cdp(maps_tab)

    await go_to_city(start_place, maps_tab, chat_tab if not no_chat else None)

    last_chat  = 0.0
    last_step  = 0.0
    at_since   = time.time()

    while True:
        now = time.time()

        # Poll YouTube chat
        if chat_tab and not no_chat and (now - last_chat) >= CHAT_POLL_SECS:
            new = await read_chat(chat_tab)
            for d in new:
                if d not in state["queue"] and d != state["current"]:
                    state["queue"].append(d)
                    print(f"[chat] queued: {d}", flush=True)
            last_chat = now

        # Time to move on?
        done_exploring = (now - at_since) >= EXPLORE_SECS or state["step"] >= MAX_STEPS
        if done_exploring:
            if state["queue"]:
                next_place = state["queue"].pop(0)
                ok = await go_to(next_place, maps_tab, chat_tab if not no_chat else None)
            else:
                city = random.choice([c for c in CITY_LIST if c != state["current"]])
                ok = await go_to_city(city, maps_tab, chat_tab if not no_chat else None)
            if ok:
                at_since  = time.time()
                last_step = 0.0
            else:
                # Backoff on failure — don't spin
                await asyncio.sleep(FAIL_BACKOFF)
                at_since  = time.time()
                last_step = 0.0

        # Step forward in Street View
        elif (now - last_step) >= STEP_DELAY:
            await step_forward(maps_tab)
            if state["step"] % 7 == 0:
                await step_turn(maps_tab)
            last_step = now

        await asyncio.sleep(0.5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stream] stopped")
    finally:
        # Best-effort cleanup
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(obs_stop_stream())
            loop.run_until_complete(obs_close())
            loop.run_until_complete(close_all())
        except Exception:
            pass
