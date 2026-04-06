"""Unchained Agent — tunnels local Chrome CDP to a remote relay server.

The agent discovers the local Chrome CDP endpoint, connects to a relay
server via WebSocket, and relays CDP messages bidirectionally. This lets
a remote orchestrator control the user's browser through their own
credentials, cookies, and IP.

Usage:
    cd unchained/
    uv run chrome_bridge.py start                          # Connect to default relay
    uv run chrome_bridge.py start --daemon                 # Start detached (survives terminal close)
    uv run chrome_bridge.py start --headless               # Launch local Chrome headless
    uv run chrome_bridge.py start --headless --stealth     # Headless with stealth flags
    uv run chrome_bridge.py start --relay ws://host:8765/tunnel  # Custom relay
    uv run chrome_bridge.py start --key uk_live_xxx        # With API key
    uv run chrome_bridge.py status                         # Show connection state
    uv run chrome_bridge.py stop                           # Stop running agent
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import platform
import random
import re
import shlex
import shutil
import signal
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

import subprocess

import websockets

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = os.environ.get("UNCHAINED_DATA_DIR",
                          os.path.join(os.path.expanduser("~"), ".unchained"))
os.makedirs(DATA_DIR, exist_ok=True)

AGENT_PID_FILE = os.path.join(DATA_DIR, ".agent_pid")  # legacy default
AGENT_CONFIG_FILE = os.path.join(DATA_DIR, "agent.json")
PROVISION_STATE_DIR = os.path.join(DATA_DIR, "provision_slots")


def _sanitize_profile(name: str) -> str:
    """Normalize a profile name for use as an agent_id suffix.

    Replaces spaces/dots with underscores, strips invalid chars,
    truncates to 32 chars. Matches relay validation: ^[a-zA-Z0-9_-]{1,32}$
    Preserves case to avoid breaking existing agent IDs.
    """
    name = name.replace(" ", "_").replace(".", "_")
    name = re.sub(r'[^a-zA-Z0-9_-]', '', name)
    return name[:32] or "default"


def _pid_file(profile: str = "default") -> str:
    """Return per-profile PID file path."""
    if profile and profile != "default":
        return os.path.join(DATA_DIR, f".agent_pid_{profile}")
    return AGENT_PID_FILE

DEFAULT_RELAY_URL = "ws://127.0.0.1:8765/tunnel"
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222

# --- Stealth fingerprint overrides (injected via CDP on every new tab) ---
#
# Each evasion is a named module that can be individually toggled for testing.
# Use --stealth-disable name1,name2 to disable specific evasions, or
# --stealth-evasions name1,name2 to enable only those.  Default: all enabled.
#
# GPU strings rotated per-tab to avoid fingerprint clustering.
_WEBGL_GPUS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.6)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti, OpenGL 4.6)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1, OpenGL 4.1)"),
]


# --- Individual evasion builder functions ---
# Each returns a JS string to inject via Page.addScriptToEvaluateOnNewDocument.

def _ev_webdriver() -> str:
    """navigator.webdriver → undefined"""
    return 'Object.defineProperty(Navigator.prototype,"webdriver",{get:()=>false,configurable:true});'


def _ev_navigator_props() -> str:
    """deviceMemory, hardwareConcurrency.

    Must define on Navigator.prototype (not the navigator instance) because
    Chromium implements these as prototype getters — instance-level
    defineProperty is silently ignored.
    """
    return (
        'Object.defineProperty(Navigator.prototype,"deviceMemory",{get:()=>8,configurable:true});'
        'Object.defineProperty(Navigator.prototype,"hardwareConcurrency",{get:()=>8,configurable:true});'
        'Object.defineProperty(Navigator.prototype,"platform",{get:()=>"Linux x86_64",configurable:true});'
    )


def _ev_screen() -> str:
    """screen dimensions 1920x1080"""
    return (
        'Object.defineProperty(screen,"width",{get:()=>1920});'
        'Object.defineProperty(screen,"height",{get:()=>1080});'
        'Object.defineProperty(screen,"availWidth",{get:()=>1920});'
        'Object.defineProperty(screen,"availHeight",{get:()=>1040});'
    )


# Selected once at process start so all tabs in the same session report the
# same GPU — avoids cross-tab correlation when a tracker links different GPU
# strings to the same origin.
_SESSION_WEBGL_GPU = random.choice(_WEBGL_GPUS)


def _ev_webgl() -> str:
    """WebGL vendor/renderer spoofing (fixed per session)"""
    vendor, renderer = _SESSION_WEBGL_GPU
    v_js = json.dumps(vendor)
    r_js = json.dumps(renderer)
    return (
        'const _gc=HTMLCanvasElement.prototype.getContext;'
        'HTMLCanvasElement.prototype.getContext=function(t,a){'
        'const c=_gc.call(this,t,a);'
        'if(t==="webgl"||t==="webgl2"||t==="experimental-webgl"){'
        'if(!c)return{getExtension:n=>n==="WEBGL_debug_renderer_info"'
        '?{UNMASKED_VENDOR_WEBGL:0x9245,UNMASKED_RENDERER_WEBGL:0x9246}:null,'
        f'getParameter:p=>p===0x9245?{v_js}'
        f':p===0x9246?{r_js}:0,'
        'getSupportedExtensions:()=>["WEBGL_debug_renderer_info"],'
        'drawingBufferWidth:300,drawingBufferHeight:150,canvas:this};'
        'const _ge=c.getExtension.bind(c);const _gp=c.getParameter.bind(c);'
        'c.getExtension=n=>n==="WEBGL_debug_renderer_info"'
        '?{UNMASKED_VENDOR_WEBGL:0x9245,UNMASKED_RENDERER_WEBGL:0x9246}:_ge(n);'
        f'c.getParameter=p=>p===0x9245?{v_js}'
        f':p===0x9246?{r_js}:_gp(p)'
        '}return c};'
    )


def _ev_outer_dimensions() -> str:
    """outerWidth/outerHeight fix (Arkose Labs detection)"""
    # CDP-provisioned Chrome reports 0 for these.  The +85 offset approximates
    # the Chrome toolbar/frame height (macOS ~74-79px, Linux/Windows ~85px).
    return (
        'Object.defineProperty(window,"outerWidth",'
        '{get:()=>window.innerWidth,configurable:true});'
        'Object.defineProperty(window,"outerHeight",'
        '{get:()=>window.innerHeight+85,configurable:true});'
    )


def _ev_chrome_props() -> str:
    """chrome.app/csi/loadTimes/runtime stubs.

    Timing values are captured once at injection time (inside an IIFE) so
    they remain stable across repeated calls — real Chrome returns fixed
    navigation-time values, not live Date.now().  We prefer
    performance.timing.navigationStart for alignment with real Chrome.
    """
    return (
        '(()=>{'
        'window.chrome=window.chrome||{};'
        'var _rt={connect:function(){},sendMessage:function(){}};'
        'try{Object.defineProperty(window.chrome,"runtime",'
        '{get:()=>_rt,configurable:true})}catch(e){window.chrome.runtime=_rt};'
        'window.chrome.app=window.chrome.app||'
        '{isInstalled:false,InstallState:{DISABLED:"disabled",'
        'INSTALLED:"installed",NOT_INSTALLED:"not_installed"},'
        'RunningState:{CANNOT_RUN:"cannot_run",READY_TO_RUN:"ready_to_run",'
        'RUNNING:"running"},getDetails:function(){},getIsInstalled:function(){},'
        'installState:function(){return"not_installed"}};'
        'const _t=(performance&&performance.timing)?performance.timing.navigationStart:Date.now();const _ts=_t/1000;'
        'window.chrome.csi=window.chrome.csi||function(){return{startE:_t,onloadT:_t,pageT:0.1,tran:15}};'
        'window.chrome.loadTimes=window.chrome.loadTimes||function(){'
        'return{commitLoadTime:_ts,connectionInfo:"h2",'
        'finishDocumentLoadTime:_ts+0.05,finishLoadTime:_ts+0.15,'
        'firstPaintAfterLoadTime:_ts+0.1,'
        'firstPaintTime:_ts+0.08,navigationType:"Other",'
        'npnNegotiatedProtocol:"h2",requestTime:_ts-0.3,'
        'startLoadTime:_ts-0.3,wasAlternateProtocolAvailable:false,'
        'wasFetchedViaSpdy:false,wasNpnNegotiated:false}}'
        '})()'
    )


def _ev_media_devices() -> str:
    """Notification permission + media device enumeration"""
    return (
        'Object.defineProperty(Notification,"permission",{get:()=>"default"});'
        'if(navigator.mediaDevices&&navigator.mediaDevices.enumerateDevices){'
        'navigator.mediaDevices.enumerateDevices=async()=>['
        '{deviceId:"",kind:"audioinput",label:"",groupId:""},'
        '{deviceId:"",kind:"videoinput",label:"",groupId:""},'
        '{deviceId:"",kind:"audiooutput",label:"",groupId:""}]}'
    )


def _ev_plugins() -> str:
    """navigator.plugins mock with MimeType entries.

    Empty PluginArray is a strong headless signal.  CreepJS also iterates
    plugin[0].type and checks navigator.mimeTypes.length, so we must
    provide associated MimeType objects.  Symbol.toStringTag is set so
    toString checks return '[object PluginArray]' / '[object MimeTypeArray]'.
    """
    return (
        '(()=>{'
        'const mt={type:"application/pdf",suffixes:"pdf",'
        'description:"Portable Document Format"};'
        'const names=["PDF Viewer","Chrome PDF Viewer","Chromium PDF Viewer",'
        '"Microsoft Edge PDF Viewer","WebKit built-in PDF"];'
        'const p=names.map(n=>{const pl={name:n,filename:"internal-pdf-viewer",'
        'description:"Portable Document Format",length:1};'
        'const m=Object.create(mt);m.enabledPlugin=pl;pl[0]=m;return pl});'
        'p.item=i=>p[i]||null;p.namedItem=n=>p.find(x=>x.name===n)||null;'
        'p.refresh=()=>{};'
        'Object.defineProperty(p,Symbol.toStringTag,{value:"PluginArray"});'
        'Object.defineProperty(navigator,"plugins",{get:()=>p});'
        'const mimes=p.map(pl=>pl[0]);'
        'mimes.item=i=>mimes[i]||null;'
        'mimes.namedItem=n=>mimes.find(x=>x.type===n)||null;'
        'Object.defineProperty(mimes,Symbol.toStringTag,{value:"MimeTypeArray"});'
        'Object.defineProperty(navigator,"mimeTypes",{get:()=>mimes})'
        '})()'
    )


def _ev_languages() -> str:
    """navigator.languages (headless may return empty)"""
    return (
        'Object.defineProperty(navigator,"languages",'
        '{get:()=>["en-US","en"]});'
    )


def _ev_permissions() -> str:
    """permissions.query override for notifications.

    Guarded: navigator.permissions may be undefined in embedded contexts
    or older WebViews — an unguarded access would throw and abort the
    entire stealth injection chain.
    """
    return (
        '(()=>{if(!navigator.permissions||!navigator.permissions.query)return;'
        'const _pq=navigator.permissions.query.bind(navigator.permissions);'
        'navigator.permissions.query=p=>p.name==="notifications"'
        '?Promise.resolve({state:Notification.permission==="default"'
        '?"prompt":Notification.permission,onchange:null}):_pq(p)})()'
    )


def _ev_mouse_coords() -> str:
    """MouseEvent screenX/screenY offset (CDP coordinate leak fix)

    CDP's Input.dispatchMouseEvent sets screenX==clientX, screenY==clientY,
    which real browsers never produce (screen coords include window position).
    Brotector and similar detectors explicitly test for this.  We intercept the
    MouseEvent prototype getters to add a realistic window-position offset.

    Offsets are generated in Python and embedded as literals so they remain
    stable across navigations within the same tab (a real browser window
    does not change position between page loads).  Each tab gets its own
    random offsets (per-tab, not per-session) — this is intentional to
    reduce cross-tab correlation by fingerprinters.
    """
    win_x = random.randint(50, 250)
    win_y = random.randint(50, 150)
    return (
        '(()=>{'
        f'const _winX={win_x};const _winY={win_y};'
        'for(const [sp,cp,off] of [["screenX","clientX",_winX],["screenY","clientY",_winY]]){'
        'const cd=Object.getOwnPropertyDescriptor(MouseEvent.prototype,cp);'
        'if(!cd||!cd.get)continue;'
        'const cGet=cd.get;'
        'Object.defineProperty(MouseEvent.prototype,sp,{'
        'get(){return cGet.call(this)+off},'
        'configurable:true})}'
        '})()'
    )


# --- Evasion registry ---
# Ordered: name → (description, builder_fn)
# builder_fn returns JS string.  CDP-only evasions (emulation_override) have
# no JS — they're handled separately in _inject_stealth/_inject_stealth_provision.
STEALTH_JS_EVASIONS = [
    ("webdriver",        "navigator.webdriver → undefined",          _ev_webdriver),
    ("navigator_props",  "deviceMemory, hardwareConcurrency",        _ev_navigator_props),
    ("screen",           "screen dimensions 1920x1080",              _ev_screen),
    ("webgl",            "WebGL vendor/renderer spoofing",           _ev_webgl),
    ("outer_dimensions", "outerWidth/outerHeight fix",               _ev_outer_dimensions),
    ("chrome_props",     "chrome.app/csi/loadTimes/runtime stubs",   _ev_chrome_props),
    ("media_devices",    "Notification + media device enumeration",  _ev_media_devices),
    ("plugins",          "navigator.plugins mock",                   _ev_plugins),
    ("languages",        "navigator.languages",                      _ev_languages),
    ("permissions",      "permissions.query override",               _ev_permissions),
    ("mouse_coords",     "MouseEvent screenX/screenY offset",        _ev_mouse_coords),
]

# CDP-level evasion names (no JS — handled in _inject_stealth).
STEALTH_CDP_EVASIONS = [
    ("emulation_override", "Emulation.setDeviceMetricsOverride 1920x1080"),
]

# All known evasion names for validation.
ALL_STEALTH_EVASION_NAMES = frozenset(
    [name for name, _, _ in STEALTH_JS_EVASIONS]
    + [name for name, _ in STEALTH_CDP_EVASIONS]
)

# --- Two-tier stealth ---
# Base evasions: safe on all machines (fix CDP artifacts, no-ops on real browsers).
# These are always on by default.
STEALTH_BASE_EVASIONS = frozenset({
    "webdriver",       # CDP always sets navigator.webdriver
    "mouse_coords",    # CDP click events always leak screenX==clientX
    "chrome_props",    # stubs for chrome.app/csi/loadTimes (real Chrome already has them)
    "plugins",         # mock (real Chrome already has plugins, mock is harmless)
    "languages",       # real Chrome already has languages
    "permissions",     # harmless override
    "media_devices",   # real Chrome already has devices
})

# Headless-only evasions: override real values with fake ones.  These would
# harm real browsers by replacing clean native values with synthetic ones.
STEALTH_HEADLESS_EVASIONS = frozenset({
    "screen",              # overwrites real screen dimensions with 1920x1080
    "emulation_override",  # forces Chrome layout engine to 1920x1080
    "webgl",               # replaces real GPU string with fake one
    "navigator_props",     # overwrites real hardwareConcurrency/deviceMemory
    "outer_dimensions",    # overwrites outerWidth/outerHeight (real browsers have correct values)
})


def _resolve_stealth_evasions(
    evasions_csv: str = "",
    disable_csv: str = "",
) -> set[str]:
    """Resolve which stealth evasions are active.

    Args:
        evasions_csv: Comma-separated list of evasion names to enable.
                      "all" or empty string means all evasions.
        disable_csv:  Comma-separated list of evasion names to disable
                      (subtracted from the enabled set).
    Returns:
        Set of active evasion names.
    """
    if not evasions_csv or evasions_csv.strip().lower() == "all":
        enabled = set(ALL_STEALTH_EVASION_NAMES)
    else:
        enabled = {n.strip() for n in evasions_csv.split(",") if n.strip()}
        unknown = enabled - ALL_STEALTH_EVASION_NAMES
        if unknown:
            logging.warning("[agent:stealth] unknown evasions ignored: %s", ", ".join(sorted(unknown)))
            enabled &= ALL_STEALTH_EVASION_NAMES

    if disable_csv:
        disable = {n.strip() for n in disable_csv.split(",") if n.strip()}
        unknown = disable - ALL_STEALTH_EVASION_NAMES
        if unknown:
            logging.warning("[agent:stealth] unknown evasions in disable list: %s", ", ".join(sorted(unknown)))
        enabled -= disable

    return enabled


def _build_stealth_js(enabled: set[str] | None = None) -> str:
    """Build stealth JS by concatenating enabled evasion modules.

    Args:
        enabled: Set of evasion names to include.  None means all.
    """
    if enabled is None:
        enabled = ALL_STEALTH_EVASION_NAMES
    parts = []
    for name, _desc, builder in STEALTH_JS_EVASIONS:
        if name in enabled:
            # Wrap each evasion in try/catch so one failure doesn't kill the rest.
            js = builder()
            parts.append("try{" + js + "}catch(_e){}")
    return "".join(parts)

DEFAULT_NEW_TAB_PATH = "/tab"
DEFAULT_WEB_PORT = 8080

HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 10   # seconds
MAX_BACKOFF = 60          # seconds
PROVISION_LAUNCH_READY_TIMEOUT = 15  # seconds
PROVISION_RECONCILE_DEBOUNCE = 0.5   # seconds
PROVISION_STARTUP_STALE_TTL = PROVISION_LAUNCH_READY_TIMEOUT + 15  # seconds
PROVISION_SLOT_ALLOCATION_ATTEMPTS = 1000

VERSION = "0.1.0"


def _configured_public_base_url() -> str:
    """Return an explicit public base URL override, if configured."""
    # UNCHAINED_API_URL controls HTTP calls from the agent; do not reuse it as
    # a browser navigation target. Startup tabs should only honor the dedicated
    # public-base override or relay-derived allowlisted hosts.
    base_url = os.environ.get("UNCHAINED_PUBLIC_BASE_URL", "").strip()
    if not base_url:
        return ""
    parsed = urllib.parse.urlparse(base_url)
    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        logging.warning(
            "[agent] ignoring invalid public base URL %r; using relay-derived defaults",
            base_url,
        )
        return ""
    if parsed.username or parsed.password:
        logging.warning(
            "[agent] ignoring public base URL with credentials %r; using relay-derived defaults",
            base_url,
        )
        return ""
    if _is_local_hostname(hostname):
        scheme = parsed.scheme
    elif _is_trusted_public_hostname(hostname):
        if parsed.scheme != "https":
            logging.warning(
                "[agent] ignoring non-https public base URL %r; using relay-derived defaults",
                base_url,
            )
            return ""
        scheme = "https"
    else:
        logging.warning(
            "[agent] ignoring untrusted public base URL host %r; using relay-derived defaults",
            hostname,
        )
        return ""
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        logging.warning(
            "[agent] stripping path/query/fragment from configured public base URL %r",
            base_url,
        )
    netloc = _format_url_host(hostname)
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((scheme, netloc, "", "", ""))


def _format_url_host(hostname: str) -> str:
    """Format a hostname for use in an absolute URL."""
    hostname = (hostname or "").strip()
    if hostname.startswith("[") and hostname.endswith("]"):
        hostname = hostname[1:-1].strip()
    if ":" in hostname:
        return f"[{hostname}]"
    return hostname


def _web_port() -> int:
    """Return the local web port with a safe fallback."""
    raw_port = os.environ.get("WEB_PORT", "").strip()
    if not raw_port:
        return DEFAULT_WEB_PORT
    try:
        port = int(raw_port)
    except ValueError:
        logging.warning("[agent] WEB_PORT=%r is invalid; using default %d", raw_port, DEFAULT_WEB_PORT)
        return DEFAULT_WEB_PORT
    if 1 <= port <= 65535:
        return port
    logging.warning("[agent] WEB_PORT=%r is invalid; using default %d", raw_port, DEFAULT_WEB_PORT)
    return DEFAULT_WEB_PORT


def _is_local_hostname(hostname: str) -> bool:
    """Return True when hostname resolves to a loopback/local binding."""
    normalized = (hostname or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _local_tab_hostname(hostname: str) -> str:
    """Return the browser-reachable local host for startup pages."""
    normalized = (hostname or "").strip().lower()
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return "localhost" if normalized == "localhost" else normalized
    if addr.is_unspecified:
        return "::1" if addr.version == 6 else "127.0.0.1"
    return normalized


def _is_trusted_public_hostname(hostname: str) -> bool:
    """Return True for the official public service hostnames."""
    normalized = (hostname or "").strip().lower()
    # Leading dot ensures "fakeunchainedsky.com" does not match.
    return normalized == "unchainedsky.com" or normalized.endswith(".unchainedsky.com")


def _build_tab_url(scheme: str, hostname: str, port: int | None = None) -> str:
    """Build an absolute URL for the default startup tab page."""
    netloc = _format_url_host(hostname)
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, DEFAULT_NEW_TAB_PATH, "", ""))


def _default_new_tab_url(relay_url: str = DEFAULT_RELAY_URL) -> str:
    """Resolve the default branded new-tab URL for this bridge instance."""
    configured = _configured_public_base_url()
    if configured:
        return f"{configured}{DEFAULT_NEW_TAB_PATH}"

    parsed = urllib.parse.urlparse(relay_url)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        logging.warning(
            "[agent] could not resolve branded tab URL for relay %r; falling back to about:blank",
            relay_url,
        )
        return "about:blank"
    if _is_local_hostname(hostname):
        return _build_tab_url("http", _local_tab_hostname(hostname), port=_web_port())
    if not _is_trusted_public_hostname(hostname):
        logging.warning(
            "[agent] untrusted relay host %r for branded tab URL; falling back to about:blank",
            hostname,
        )
        return "about:blank"

    scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    return _build_tab_url(scheme, hostname, port=parsed.port)


def _new_tab_request(host: str, port: int, target_url: str) -> urllib.request.Request:
    """Build a CDP /json/new request with a safely encoded target URL."""
    encoded = urllib.parse.quote(target_url, safe="")
    request_host = _format_url_host(host)
    return urllib.request.Request(f"http://{request_host}:{port}/json/new?{encoded}", method="PUT")


def _first_page_tab(host: str, port: int, startup_url: str) -> dict:
    """Return the first page tab, creating one if Chrome has none."""
    request_host = _format_url_host(host)
    tabs_url = f"http://{request_host}:{port}/json"
    with urllib.request.urlopen(tabs_url, timeout=3) as resp:
        tabs = json.loads(resp.read())
    page_tabs = [t for t in tabs if t.get("type") == "page"]
    if page_tabs:
        return page_tabs[0]
    with urllib.request.urlopen(_new_tab_request(host, port, startup_url), timeout=3) as resp:
        raw_body = resp.read()
    try:
        return json.loads(raw_body)
    except Exception as e:
        logging.warning("[agent] /json/new response parse failed; retrying /json lookup: %s", e)
        with urllib.request.urlopen(tabs_url, timeout=3) as retry_resp:
            retry_tabs = json.loads(retry_resp.read())
        retry_page_tabs = [t for t in retry_tabs if t.get("type") == "page"]
        if retry_page_tabs:
            return retry_page_tabs[0]
        raise RuntimeError("Chrome created a startup tab but it could not be discovered") from e


def _wait_for_process_exit(proc, timeout_s: float) -> bool:
    """Wait for a subprocess to exit without relying on a blocking wait when poll exists."""
    poll = getattr(proc, "poll", None)
    if callable(poll):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if poll() is not None:
                    return True
            except Exception:
                break
            time.sleep(0.05)
        return False
    try:
        proc.wait(timeout=timeout_s)
        return True
    except Exception:
        return False


def _pid_is_running(pid: int) -> bool:
    """Return True when the pid exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _wait_for_pid_exit(pid: int, timeout_s: float) -> bool:
    """Wait for a PID to disappear."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_running(pid)


def _terminate_pid(pid: int, label: str, prefix: str = "[agent]") -> bool:
    """Terminate a process by PID when the original Popen handle is gone.

    This is best-effort only. We validate recovered PIDs against their saved
    command line before calling this helper, but a narrow PID-reuse race still
    exists between validation and signal delivery.
    """
    if pid <= 0 or not _pid_is_running(pid):
        return True
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            os.kill(pid, signal.SIGTERM)
        if _wait_for_pid_exit(pid, 5):
            print(f"{prefix} {label} terminated")
            return True
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            os.kill(pid, signal.SIGKILL)
        if _wait_for_pid_exit(pid, 1):
            print(f"{prefix} {label} killed")
            return True
    except Exception as e:
        print(f"{prefix} Warning: failed to fully stop {label}: {e}")
        return False
    print(f"{prefix} Warning: failed to fully stop {label}")
    return False


def _terminate_process(proc, label: str, prefix: str = "[agent]") -> bool:
    """Terminate a subprocess and confirm exit when possible."""
    if not proc:
        return True
    try:
        proc.terminate()
        if _wait_for_process_exit(proc, 5):
            print(f"{prefix} {label} terminated")
            return True
    except Exception:
        pass
    try:
        proc.kill()
        if _wait_for_process_exit(proc, 1):
            print(f"{prefix} {label} killed")
            return True
    except Exception as e:
        print(f"{prefix} Warning: failed to fully stop {label}: {e}")
        return False
    print(f"{prefix} Warning: failed to fully stop {label}")
    return False


def _prov_state_path(slot: str) -> str:
    """Return the persisted metadata file for a provision slot."""
    return os.path.join(PROVISION_STATE_DIR, f"{slot}.json")


def _list_prov_state_slots() -> list[str]:
    """Return persisted provision slot IDs."""
    try:
        names = os.listdir(PROVISION_STATE_DIR)
    except OSError:
        return []
    slots = []
    for name in names:
        if not name.endswith(".json"):
            continue
        slot = name[:-5]
        if re.fullmatch(r"[0-9a-f]{4}", slot):
            slots.append(slot)
    return sorted(slots)


def _read_prov_state(slot: str) -> dict | None:
    """Load persisted metadata for a provision slot."""
    try:
        with open(_prov_state_path(slot)) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_prov_state(slot: str, prov: dict):
    """Persist metadata needed to recover a provisioned Chrome after reconnect."""
    os.makedirs(PROVISION_STATE_DIR, exist_ok=True)
    payload = {
        "slot": slot,
        "pid": int(prov.get("pid") or 0),
        "port": int(prov.get("port") or 0),
        "temp_dir": prov.get("temp_dir", ""),
        "profile_dir_name": prov.get("profile_dir_name", ""),
        "copy_mode": prov.get("copy_mode", ""),
        "launched_at": prov.get("launched_at", time.time()),
        "ready": bool(prov.get("ready", True)),
        "agent_id": prov.get("agent_id", ""),
        "caller_tag": prov.get("caller_tag", ""),
    }
    tmp_path = f"{_prov_state_path(slot)}.tmp.{os.getpid()}"
    with open(tmp_path, "w") as f:
        json.dump(payload, f)
    os.replace(tmp_path, _prov_state_path(slot))


def _remove_prov_state(slot: str):
    """Delete persisted metadata for a provision slot."""
    try:
        os.remove(_prov_state_path(slot))
    except OSError:
        pass


def _classify_prov_pid(pid: int, temp_dir: str, port: int) -> str:
    """Return alive/dead/mismatch for a persisted provisioned Chrome PID."""
    if not _pid_is_running(pid):
        return "dead"
    cmdline = _process_cmdline(pid)
    if not cmdline:
        return "alive"
    temp_marker = os.path.basename((temp_dir or "").rstrip(os.sep))
    port_marker = f"--remote-debugging-port={port}" if port else ""
    if temp_marker and temp_marker not in cmdline:
        return "mismatch"
    if port_marker and port_marker not in cmdline:
        return "mismatch"
    return "alive"


# ---------------------------------------------------------------------------
# Chrome profile discovery (runs on user's machine)
# ---------------------------------------------------------------------------

def _chrome_user_data_dir():
    """Return the system Chrome user data directory, or None."""
    s = platform.system()
    if s == "Darwin":
        p = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    elif s == "Linux":
        p = os.path.expanduser("~/.config/google-chrome")
    elif s == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        p = os.path.join(local, "Google", "Chrome", "User Data") if local else ""
    else:
        return None
    return p if os.path.isdir(p) else None


def _find_chrome_binary() -> str | None:
    """Find a local Chromium-based browser binary suitable for CDP."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    if platform.system() == "Windows":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name, "").strip()
            if not base:
                continue
            candidates.extend([
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(base, "Chromium", "Application", "chrome.exe"),
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            ])

    for path in candidates:
        if os.path.exists(path):
            return path

    for cmd in (
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
        "chrome",
        "chrome.exe",
        "msedge",
        "msedge.exe",
    ):
        found = shutil.which(cmd)
        if found:
            return found
    return None


def _list_chrome_profiles():
    """List Chrome profiles on this machine that are signed into Google."""
    chrome_dir = _chrome_user_data_dir()
    if not chrome_dir:
        return []
    profiles = []
    for entry in sorted(os.listdir(chrome_dir)):
        prefs_path = os.path.join(chrome_dir, entry, "Preferences")
        if not os.path.isfile(prefs_path):
            continue
        if entry != "Default" and not re.match(r"^Profile \d+$", entry):
            continue
        try:
            with open(prefs_path, "r") as f:
                prefs = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        account_info = prefs.get("account_info", [])
        if not account_info:
            continue
        email = account_info[0].get("email", "")
        full_name = account_info[0].get("full_name", "")
        profile_name = prefs.get("profile", {}).get("name", entry)
        if not email:
            continue
        profiles.append({
            "path": os.path.join(chrome_dir, entry),
            "dir_name": entry,
            "name": profile_name,
            "full_name": full_name,
            "email": email,
        })
    return profiles


_PROFILE_CACHE_DIRS = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "Service Worker",
    "GrShaderCache",
    "DawnCache",
}
_LIGHT_PROFILE_FILES = (
    "Preferences",
    "Secure Preferences",
    "Cookies",
    "Cookies-journal",
    "Login Data",
    "Login Data-journal",
    "Web Data",
    "Web Data-journal",
    os.path.join("Network", "Cookies"),
    os.path.join("Network", "Cookies-journal"),
)
_LIGHT_PROFILE_DIRS = (
    "Local Storage",
    "Session Storage",
    "IndexedDB",
)


def _copy_profile_full(src_profile: str, dest_user_data_dir: str, profile_dir_name: str):
    """Copy full profile directory excluding heavyweight cache folders."""
    shutil.copytree(
        src_profile,
        os.path.join(dest_user_data_dir, profile_dir_name),
        ignore=lambda _directory, contents: [c for c in contents if c in _PROFILE_CACHE_DIRS],
    )


def _copy_profile_light(src_profile: str, dest_user_data_dir: str, profile_dir_name: str):
    """Copy only sign-in/session state required to reduce re-login prompts."""
    dest_profile = os.path.join(dest_user_data_dir, profile_dir_name)
    os.makedirs(dest_profile, exist_ok=True)

    for rel in _LIGHT_PROFILE_FILES:
        src = os.path.join(src_profile, rel)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dest_profile, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)

    for rel in _LIGHT_PROFILE_DIRS:
        src = os.path.join(src_profile, rel)
        if not os.path.isdir(src):
            continue
        shutil.copytree(
            src,
            os.path.join(dest_profile, rel),
            dirs_exist_ok=True,
            ignore=lambda _directory, contents: [c for c in contents if c in _PROFILE_CACHE_DIRS],
        )


def _parse_prov_tab_id(tab_id: str) -> tuple[str, str]:
    """Parse a prov-prefixed tab ID into (slot, real_id).

    New format: ``prov-{slot}-{real_id}`` → (slot, real_id)
    Old format: ``prov-{real_id}``        → ("", real_id)
    """
    parts = tab_id.split("-", 2)
    if len(parts) == 3:
        return parts[1], parts[2]
    return "", parts[1] if len(parts) == 2 else ""


def _extract_prov_slot(tab_id: str) -> str:
    """Return the slot portion of a prov tab ID, or empty string."""
    slot, _ = _parse_prov_tab_id(tab_id)
    return slot


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class Agent:
    """Local agent that tunnels Chrome CDP to a relay server."""

    def __init__(self, relay_url: str, api_key: str = "",
                 cdp_host: str = DEFAULT_CDP_HOST,
                 cdp_port: int = DEFAULT_CDP_PORT,
                 profile: str = "default",
                 headless: bool = False,
                 stealth: bool = True,
                 stealth_evasions: set[str] | None = None):
        """
        Args:
            stealth: Enable CDP fingerprint injection.  Default ``True`` —
                     base evasions (CDP artifact fixes) are always active.
                     Pass ``stealth=False`` or ``--no-stealth`` to disable.
            stealth_evasions: Set of evasion names to enable.  ``None``
                     means auto-select: base evasions on real machines,
                     all evasions on headless.
        """
        self.relay_url = relay_url
        self.api_key = api_key
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.profile = profile
        self._headless = headless
        self._stealth = stealth
        # Auto-select tier when no explicit evasion set is given.
        if stealth_evasions is not None:
            self._stealth_evasions = stealth_evasions
        elif headless:
            self._stealth_evasions = set(STEALTH_BASE_EVASIONS | STEALTH_HEADLESS_EVASIONS)
        else:
            self._stealth_evasions = set(STEALTH_BASE_EVASIONS)
        if self._stealth:
            active = sorted(self._stealth_evasions & ALL_STEALTH_EVASION_NAMES)
            tier = "all (headless)" if headless else "base (real browser)"
            logging.info("[agent:stealth] %s — active evasions (%d): %s",
                         tier, len(active), ", ".join(active))
        self.ws = None
        self.channels: dict[int, websockets.WebSocketClientProtocol] = {}
        self._channel_tasks: dict[int, asyncio.Task] = {}
        self.running = False
        self.agent_id = None  # type: Optional[str]
        self._backoff = 1
        self._last_pong = 0.0
        # Provision Chrome: temporary Chromes keyed by slot (4-char hex)
        self._prov_chromes: dict[str, dict] = {}  # slot → {port, process, pid, temp_dir, profile_dir_name}
        # Tab leasing: prevent multiple channels from auto-resolving to the same tab.
        # All mutations happen on the asyncio event loop (single-threaded), so no
        # lock is needed.  Tab IDs are Chrome UUIDs — main and provisioned Chrome
        # share the same namespace; collision is astronomically unlikely.
        self._tab_leases: dict[int, str] = {}   # channel → Chrome tab ID
        self._leased_tabs: set[str] = set()     # Chrome tab IDs currently leased
        self._last_prov_reconcile_ts = 0.0

    def _reconcile_prov_chromes(self, force: bool = False):
        """Reload persisted provision slots and prune stale metadata.

        Only adopts slots whose persisted ``agent_id`` matches this agent
        (or slots written before agent_id tracking was added).
        """
        if not force and not self.running:
            return
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_prov_reconcile_ts) < PROVISION_RECONCILE_DEBOUNCE:
            return
        self._last_prov_reconcile_ts = now_mono
        now_wall = time.time()
        my_id = self.agent_id or ""
        if not my_id:
            # agent_id not yet populated (e.g. reconnecting before auth).
            # Fall back to legacy adopt-all behavior to avoid leaking our
            # own slots from a prior run.
            print("[agent:prov] Warning: agent_id not set during reconcile — adopting all slots")
        for slot in _list_prov_state_slots():
            state = _read_prov_state(slot)
            if not state:
                _remove_prov_state(slot)
                continue
            # Skip slots belonging to a different agent.  Legacy slots
            # (empty agent_id) are adopted by any agent for backward compat.
            # When my_id is empty we also adopt everything (see warning above).
            # For foreign slots we still prune dead-PID state files and their
            # temp dirs to prevent accumulation from crashed agents.  There is
            # a negligible TOCTOU window where a foreign agent could restart
            # Chrome between the PID check and the prune — acceptable.
            slot_agent = state.get("agent_id", "")
            is_foreign = bool(my_id and slot_agent and slot_agent != my_id)
            if is_foreign:
                pid = int(state.get("pid") or 0)
                temp_dir = state.get("temp_dir", "")
                port = int(state.get("port") or 0)
                if pid <= 0 or _classify_prov_pid(pid, temp_dir, port) != "alive":
                    if temp_dir and os.path.isdir(temp_dir):
                        try:
                            shutil.rmtree(temp_dir)
                            print(f"[agent:prov] Pruned orphan temp dir {temp_dir} (foreign slot {slot})")
                        except Exception:
                            pass
                    _remove_prov_state(slot)
                continue
            pid = int(state.get("pid") or 0)
            port = int(state.get("port") or 0)
            temp_dir = state.get("temp_dir", "")
            profile_dir_name = state.get("profile_dir_name", "")
            ready = bool(state.get("ready", True))
            launched_at = float(state.get("launched_at") or 0)
            if pid <= 0 or port <= 0:
                _remove_prov_state(slot)
                continue
            if not ready:
                age = (now_wall - launched_at) if launched_at else (PROVISION_STARTUP_STALE_TTL + 1)
                if age > PROVISION_STARTUP_STALE_TTL:
                    self._prov_chromes.pop(slot, None)
                    self._cleanup_single_prov(
                        {
                            "pid": pid,
                            "temp_dir": temp_dir,
                        },
                        slot=slot,
                    )
                    continue
            status = _classify_prov_pid(pid, temp_dir, port)
            if status == "alive":
                prov = self._prov_chromes.get(slot)
                if not prov:
                    self._prov_chromes[slot] = {
                        "port": port,
                        "process": None,
                        "pid": pid,
                        "temp_dir": temp_dir,
                        "profile_dir_name": profile_dir_name,
                        "ready": ready,
                    }
                else:
                    prov.setdefault("pid", pid)
                    prov.setdefault("port", port)
                    prov.setdefault("temp_dir", temp_dir)
                    prov.setdefault("profile_dir_name", profile_dir_name)
                    prov["ready"] = ready
                continue
            self._prov_chromes.pop(slot, None)
            if status == "mismatch":
                print(f"[agent:prov] Warning: dropping stale provision state for slot {slot} (PID {pid} reused)")
            self._cleanup_single_prov(
                {
                    "pid": 0,
                    "temp_dir": temp_dir,
                },
                slot=slot,
                kill_process=False,
            )

    def _relaunch_chrome(self) -> bool:
        """Try to relaunch Chrome if it's not reachable."""
        return _ensure_chrome(
            self.cdp_host,
            self.cdp_port,
            self.profile,
            self._headless,
            relay_url=self.relay_url,
        )

    def _cleanup_orphan_tabs(self):
        """Close all Chrome tabs except one (headless only).

        Called on reconnect to clean tabs leaked from previous sessions.
        """
        url = f"http://{self.cdp_host}:{self.cdp_port}/json"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                tabs = json.loads(resp.read())
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            if len(page_tabs) <= 1:
                return
            for tab in page_tabs[1:]:
                try:
                    urllib.request.urlopen(
                        f"http://{self.cdp_host}:{self.cdp_port}/json/close/{tab['id']}",
                        timeout=3)
                except Exception:
                    pass
            print(f"[agent] cleaned {len(page_tabs) - 1} orphan tabs")
        except Exception as e:
            print(f"[agent] orphan cleanup failed: {e}")

    # --- Main lifecycle ---

    async def start(self):
        """Connect to relay and relay messages. Reconnects on failure."""
        self.running = True
        print(f"[agent] connecting to {self.relay_url}")
        print(f"[agent] Chrome CDP at {self.cdp_host}:{self.cdp_port}")
        while self.running:
            try:
                await self._connect_and_run()
            # asyncio.TimeoutError is not consistently an OSError across Python versions.
            except (ConnectionError, OSError, asyncio.TimeoutError,
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidURI,
                    websockets.exceptions.InvalidHandshake) as e:
                if not self.running:
                    break
                print(f"[agent] connection lost: {e}")
                await self._reconnect_with_backoff()
            except asyncio.CancelledError:
                break

    async def _connect_and_run(self):
        """Single connection lifecycle: connect → auth → message loop."""
        self._watchdog_triggered = False
        async with websockets.connect(self.relay_url,
                                      max_size=50 * 1024 * 1024) as ws:
            self.ws = ws
            self._backoff = 1  # reset on successful connect
            await self._authenticate()
            self._reconcile_prov_chromes(force=True)
            print(f"[agent] authenticated as {self.agent_id}")
            if self._headless:
                self._cleanup_orphan_tabs()
            await self._message_loop()
        if self._watchdog_triggered:
            raise ConnectionError("pong timeout — relay tunnel dead")

    async def _authenticate(self):
        """Send auth message, wait for auth_ok."""
        await self.ws.send(json.dumps({
            "type": "auth",
            "api_key": self.api_key,
            "agent_version": VERSION,
            "cdp_port": self.cdp_port,
            "profile": self.profile,
        }))
        raw = await asyncio.wait_for(self.ws.recv(), timeout=10)
        resp = json.loads(raw)
        if resp.get("type") == "auth_ok":
            self.agent_id = resp.get("agent_id", "unknown")
        elif resp.get("type") == "auth_fail":
            raise RuntimeError(f"Authentication failed: {resp.get('error', 'unknown')}")
        else:
            raise RuntimeError(f"Unexpected auth response: {resp}")

    async def _message_loop(self):
        """Main relay loop: listen on tunnel, dispatch messages."""
        self._last_pong = time.time()  # seed with connect time
        ping_task = asyncio.create_task(self._heartbeat())
        watchdog_task = asyncio.create_task(self._pong_watchdog())
        try:
            async for raw in self.ws:
                self._last_pong = time.time()  # any recv proves tunnel alive
                msg = json.loads(raw)
                await self._handle_message(msg)
                # Reset after handler: long blocking handlers (e.g.
                # provision-launch doing sync file copies) starve the
                # event loop so pongs queue up and _last_pong goes stale.
                # Resetting here gives the watchdog a fresh baseline —
                # if the tunnel is alive, the next pong arrives within
                # HEARTBEAT_INTERVAL; if dead, the watchdog catches it.
                self._last_pong = time.time()
        finally:
            ping_task.cancel()
            watchdog_task.cancel()
            await self._close_all_channels()

    async def _handle_message(self, msg: dict):
        """Dispatch incoming message from relay."""
        t = msg.get("type", "")
        if t == "pong":
            self._last_pong = msg.get("ts", time.time())
        elif t == "http":
            await self._handle_http(msg)
        elif t == "ws_open":
            await self._handle_ws_open(msg)
        elif t == "ws_send":
            await self._handle_ws_send(msg)
        elif t == "ws_close":
            await self._handle_ws_close(msg)
        # Ignore unknown message types silently

    # --- CDP HTTP proxy ---

    async def _handle_http(self, msg: dict):
        """Proxy an HTTP request to local Chrome CDP (or handle special paths)."""
        req_id = msg.get("req_id", "")
        method = msg.get("method", "GET")
        path = msg.get("path", "/json")

        # Special path: list Chrome profiles on this machine
        if path == "/profiles":
            profiles = _list_chrome_profiles()
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 200,
                "body": {"profiles": profiles},
            }))
            return

        # Provision Chrome: launch a temporary Chrome with a user-selected profile
        if path.startswith("/provision-launch"):
            await self._handle_provision_launch(req_id, path)
            return

        # Provision Chrome: cleanup (kill temp Chrome, delete temp dir)
        if path.startswith("/provision-cleanup"):
            await self._handle_provision_cleanup(req_id, path)
            return

        # Provision Chrome: status (list all provisioned slots and their tabs)
        if path == "/provision-status":
            await self._handle_provision_status(req_id)
            return

        # Proxy /prov/{slot}/{path} requests to the provision Chrome's port
        if path.startswith("/prov/"):
            self._reconcile_prov_chromes()
        if path.startswith("/prov/") and self._prov_chromes:
            # Parse: /prov/{slot}/{chrome_path}
            prov_parts = path.split("/", 3)  # ['', 'prov', slot, chrome_path]
            prov_slot = prov_parts[2] if len(prov_parts) > 2 else ""
            prov = self._prov_chromes.get(prov_slot)
            if not prov:
                # Backward compat: if no matching slot and exactly one prov Chrome, use it
                if len(self._prov_chromes) == 1:
                    prov = next(iter(self._prov_chromes.values()))
                    prov_path = path[5:]  # strip "/prov" prefix
                else:
                    await self.ws.send(json.dumps({
                        "type": "http_response",
                        "req_id": req_id,
                        "status": 404,
                        "body": {"error": f"Provision Chrome slot '{prov_slot}' not found"},
                    }))
                    return
            else:
                prov_path = "/" + prov_parts[3] if len(prov_parts) > 3 else "/"
            prov_url = f"http://127.0.0.1:{prov['port']}{prov_path}"
            try:
                req = urllib.request.Request(prov_url, method=method)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read())
                    status = resp.status
            except json.JSONDecodeError:
                body = {}
                status = 200
            except Exception as e:
                body = {"error": str(e)}
                status = 502
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": status,
                "body": body,
            }))
            return

        url = f"http://{self.cdp_host}:{self.cdp_port}{path}"
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                status = resp.status
        except urllib.error.URLError:
            # Chrome not reachable — try to relaunch
            if self._relaunch_chrome():
                try:
                    req = urllib.request.Request(url, method=method)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        body = json.loads(resp.read())
                        status = resp.status
                except Exception as e2:
                    body = {"error": str(e2)}
                    status = 502
            else:
                body = {"error": "Chrome not running and could not be launched"}
                status = 502
        except json.JSONDecodeError:
            body = {}
            status = 200
        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": status,
            "body": body,
        }))

    # --- CDP WebSocket channels ---

    async def _handle_ws_open(self, msg: dict):
        """Open a WebSocket channel to a local Chrome tab."""
        channel = msg.get("channel", 0)
        tab_id = msg.get("tab_id", "")
        try:
            ws_url = self._get_tab_ws_url(tab_id, channel)
        except Exception:
            # Chrome may have closed — try to relaunch
            if self._relaunch_chrome():
                try:
                    ws_url = self._get_tab_ws_url(tab_id, channel)
                except Exception as e2:
                    await self.ws.send(json.dumps({
                        "type": "ws_error",
                        "channel": channel,
                        "error": str(e2),
                    }))
                    return
            else:
                await self.ws.send(json.dumps({
                    "type": "ws_error",
                    "channel": channel,
                    "error": "Chrome not running and could not be launched",
                }))
                return
        try:
            chrome_ws = await websockets.connect(ws_url,
                                                 max_size=50 * 1024 * 1024)
            # Skip stealth on browser-level connections — no page context.
            # The agent's per-tab connection already has stealth injected.
            if tab_id != "browser":
                try:
                    await self._inject_stealth(chrome_ws)
                except Exception as e:
                    # Best-effort: stealth failure should not block tab usage.
                    print(f"[agent] stealth inject failed (non-fatal): {e}")
            self.channels[channel] = chrome_ws
            # Start background task to forward Chrome → relay
            task = asyncio.create_task(
                self._forward_chrome_to_relay(channel, chrome_ws))
            self._channel_tasks[channel] = task
            await self.ws.send(json.dumps({
                "type": "ws_opened",
                "channel": channel,
                "ws_url": ws_url,
            }))
            # Record tab lease for auto-resolution isolation.
            # The ws_url returned by _get_tab_ws_url always contains the real
            # Chrome tab ID (even for newly-created tabs), so extracting it
            # here covers both the "reuse existing" and "create new" branches.
            # No race with concurrent opens: all code runs on the asyncio
            # event loop, so _get_tab_ws_url + lease write is atomic from
            # the perspective of other coroutines (no await between them).
            if tab_id == "auto" or (tab_id.startswith("prov-") and
                                     _parse_prov_tab_id(tab_id)[1] == "auto"):
                resolved_tab_id = self._extract_tab_id_from_ws_url(ws_url)
                if resolved_tab_id:
                    self._tab_leases[channel] = resolved_tab_id
                    self._leased_tabs.add(resolved_tab_id)
        except Exception as e:
            await self.ws.send(json.dumps({
                "type": "ws_error",
                "channel": channel,
                "error": str(e),
            }))

    async def _inject_stealth(self, chrome_ws):
        """Inject stealth fingerprint overrides into a Chrome tab.

        Called before ``self.channels[channel]`` is assigned — the forwarding
        loop has not started yet, so any unsolicited CDP events drained by
        ``_cdp()`` would not have reached the relay anyway.

        ``Page.addScriptToEvaluateOnNewDocument`` only affects future
        navigations, not the already-loaded document in the tab.

        Raises on failure so the caller can close the orphaned websocket.
        """
        if not self._stealth:
            return
        # High range avoids collision with relay-forwarded CDP message IDs.
        sid = random.randint(2**28, 2**30)

        async def _cdp(method, params=None):
            nonlocal sid
            sid += 1
            await chrome_ws.send(json.dumps(
                {"id": sid, "method": method, "params": params or {}}))
            # Drain until we get our response — skip unsolicited CDP events.
            while True:
                raw = await asyncio.wait_for(chrome_ws.recv(), timeout=5)
                msg = json.loads(raw)
                if msg.get("id") == sid:
                    return msg

        # Clear stale emulation from previous bridge sessions so headed
        # browsers don't keep a viewport override from an old headless run.
        # Also remove any lingering screen property overrides from old stealth
        # scripts that may have been registered via addScriptToEvaluateOnNewDocument
        # in a previous session (those scripts persist across bridge restarts).
        await _cdp("Emulation.clearDeviceMetricsOverride")
        if "screen" not in self._stealth_evasions:
            # Undo stale screen overrides from previous sessions.  delete
            # doesn't work on Object.defineProperty getters, so we re-define
            # them to return the native CSS value (which is correct after
            # Emulation.clearDeviceMetricsOverride).
            await _cdp("Page.addScriptToEvaluateOnNewDocument", {
                "source": (
                    '(()=>{const s=window.screen;'
                    'for(const p of ["width","height","availWidth","availHeight"]){'
                    'const d=Object.getOwnPropertyDescriptor(Screen.prototype,p);'
                    'if(d&&d.get)Object.defineProperty(s,p,{get:d.get.bind(s),configurable:true})}'
                    '})()'
                ),
            })

        if "emulation_override" in self._stealth_evasions:
            await _cdp("Emulation.setDeviceMetricsOverride", {
                "width": 1920, "height": 1080, "deviceScaleFactor": 1,
                "mobile": False, "screenWidth": 1920, "screenHeight": 1080,
            })
        # Override UA via CDP to strip "HeadlessChrome" (--user-agent flag is
        # ignored by headless=new) and align navigator.platform with the UA.
        if self._headless:
            ua_version = "146.0.0.0"
            try:
                ver_info = json.loads(urllib.request.urlopen(
                    f"http://{self.cdp_host}:{self.cdp_port}/json/version",
                    timeout=2,
                ).read())
                import re as _re
                m = _re.search(r"(\d+\.\d+\.\d+\.\d+)", ver_info.get("Browser", ""))
                if m:
                    ua_version = m.group(1)
            except Exception:
                pass
            await _cdp("Network.setUserAgentOverride", {
                "userAgent": (
                    f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                    f" (KHTML, like Gecko) Chrome/{ua_version} Safari/537.36"
                ),
                "platform": "Linux x86_64",
                "acceptLanguage": "en-US,en;q=0.9",
            })
        js = _build_stealth_js(self._stealth_evasions)
        if js:
            # Register for future navigations
            await _cdp("Page.addScriptToEvaluateOnNewDocument", {
                "source": js,
            })
            # Also inject into the already-loaded page — addScriptToEvaluate
            # only fires on future navigations, and some overrides (webgl,
            # chrome.runtime) don't persist from that context.
            await _cdp("Runtime.evaluate", {"expression": js})

    @staticmethod
    async def _inject_stealth_provision(
        prov_port: int,
        tab_info: dict,
        enabled_evasions: set[str],
    ):
        """Inject stealth overrides into a provisioned Chrome tab.

        Connects directly to the provisioned Chrome's CDP endpoint (not
        through the relay) to call Page.addScriptToEvaluateOnNewDocument
        before the user navigates anywhere.
        """
        ws_url = tab_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            tab_id = tab_info.get("id", "")
            if not tab_id:
                raise RuntimeError("Tab info has no webSocketDebuggerUrl or id")
            ws_url = f"ws://127.0.0.1:{prov_port}/devtools/page/{tab_id}"

        chrome_ws = await asyncio.wait_for(
            websockets.connect(ws_url, max_size=10 * 1024 * 1024),
            timeout=5,
        )
        try:
            sid = random.randint(2**28, 2**30)

            async def _ws_cdp(method, params=None):
                nonlocal sid
                sid += 1
                msg_id = sid
                await chrome_ws.send(json.dumps(
                    {"id": msg_id, "method": method, "params": params or {}}))
                while True:
                    raw = await chrome_ws.recv()
                    msg = json.loads(raw)
                    if msg.get("id") == msg_id:
                        if msg.get("error"):
                            raise RuntimeError(
                                f"CDP {method} failed: {msg['error']}")
                        return msg

            stealth_js = _build_stealth_js(enabled_evasions)
            _ev = enabled_evasions if enabled_evasions is not None else ALL_STEALTH_EVASION_NAMES

            # Enable Page domain (required for addScriptToEvaluateOnNewDocument)
            await asyncio.wait_for(_ws_cdp("Page.enable"), timeout=10)
            # Apply CDP-level evasions
            if "emulation_override" in _ev:
                await asyncio.wait_for(_ws_cdp(
                    "Emulation.setDeviceMetricsOverride", {
                        "width": 1920, "height": 1080, "deviceScaleFactor": 1,
                        "mobile": False, "screenWidth": 1920, "screenHeight": 1080,
                    }), timeout=10)
            # Inject stealth JS into all future navigations
            if stealth_js:
                await asyncio.wait_for(
                    _ws_cdp("Page.addScriptToEvaluateOnNewDocument",
                            {"source": stealth_js}),
                    timeout=10,
                )
                # Also inject into the currently loaded page
                await asyncio.wait_for(
                    _ws_cdp("Runtime.evaluate", {"expression": stealth_js}),
                    timeout=10,
                )
        finally:
            await chrome_ws.close()

    @staticmethod
    def _extract_tab_id_from_ws_url(ws_url: str) -> str:
        """Extract Chrome tab ID from ws://host:port/devtools/page/<TAB_ID>.

        Returns empty string on malformed input (caller guards with
        ``if resolved_tab_id:``).  Logs a warning so lease-recording
        failures are visible in agent output.
        """
        if "/devtools/" not in ws_url:
            print(f"[agent] warning: cannot extract tab ID from ws_url (no /devtools/): {ws_url!r}")
            return ""
        parts = ws_url.rsplit("/", 1)
        tab_id = parts[-1] if len(parts) == 2 and parts[-1] else ""
        if not tab_id:
            print(f"[agent] warning: cannot extract tab ID from ws_url (empty segment): {ws_url!r}")
        return tab_id

    def _get_tab_ws_url(self, tab_id: str, channel: int = -1) -> str:
        """Look up a tab's WebSocket URL from local Chrome.

        If tab_id starts with 'prov-', route to the provision Chrome instead.
        New format: prov-{slot}-{real_id}  Old format: prov-{real_id}

        When channel >= 0 and tab_id is 'auto', tab leasing prevents multiple
        channels from resolving to the same tab.  A channel that already holds
        a lease reuses its tab; tabs leased by *other* channels are skipped.
        """
        # Browser-level WebSocket — used by screencast to avoid tab eviction
        if tab_id == "browser":
            url = f"http://{self.cdp_host}:{self.cdp_port}/json/version"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                version = json.loads(resp.read())
            return version["webSocketDebuggerUrl"]

        # Provision Chrome routing
        if tab_id.startswith("prov-"):
            self._reconcile_prov_chromes()
        if tab_id.startswith("prov-") and self._prov_chromes:
            slot, real_id = _parse_prov_tab_id(tab_id)
            # Look up by slot; backward compat: if no slot and exactly one prov Chrome, use it
            if slot and slot in self._prov_chromes:
                prov = self._prov_chromes[slot]
            elif not slot and len(self._prov_chromes) == 1:
                prov = next(iter(self._prov_chromes.values()))
            else:
                raise RuntimeError(f"Provision Chrome slot '{slot}' not found")
            if not prov.get("ready", True):
                raise RuntimeError(f"Provision Chrome slot '{slot}' is still starting up")
            prov_port = prov["port"]
            url = f"http://127.0.0.1:{prov_port}/json"
            req = urllib.request.Request(url)
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    tabs = json.loads(resp.read())
            except (urllib.error.URLError, OSError):
                # Provisioned Chrome is no longer running — clean up stale slot.
                self._prov_chromes.pop(slot or next(iter(self._prov_chromes), ""), None)
                if slot:
                    _remove_prov_state(slot)
                raise RuntimeError(
                    f"Provisioned Chrome (slot '{slot}') is no longer running. "
                    f"Re-provision with cdp_provision_launch to continue."
                )
            page_tabs = [t for t in tabs if t.get("type") in ("page", "popup")]
            if real_id == "auto":
                # Reuse existing lease for this channel
                if channel >= 0 and channel in self._tab_leases:
                    leased_id = self._tab_leases[channel]
                    leased_match = [t for t in page_tabs if t["id"] == leased_id]
                    if leased_match:
                        return leased_match[0]["webSocketDebuggerUrl"]
                    # Leased tab was closed externally — release stale lease
                    self._tab_leases.pop(channel, None)
                    self._leased_tabs.discard(leased_id)
                # Auto prefers page tabs; fall back to popup only if no pages
                pages_only = [t for t in tabs if t.get("type") == "page"]
                # Filter out tabs leased by other channels
                if channel >= 0:
                    available = [t for t in pages_only if t["id"] not in self._leased_tabs]
                    available_all = [t for t in page_tabs if t["id"] not in self._leased_tabs]
                else:
                    available = pages_only
                    available_all = page_tabs
                auto_tab = available[0] if available else (available_all[0] if available_all else None)
                if auto_tab:
                    return auto_tab["webSocketDebuggerUrl"]
                # All tabs appear leased — reconcile stale leases first.
                stale_channels = [ch for ch in self._tab_leases
                                  if ch not in self.channels]
                for ch in stale_channels:
                    tid = self._tab_leases.pop(ch, None)
                    if tid:
                        self._leased_tabs.discard(tid)
                live_ids = {t["id"] for t in page_tabs}
                stale_tabs = self._leased_tabs - live_ids
                if stale_tabs:
                    self._leased_tabs -= stale_tabs
                    for ch, tid in list(self._tab_leases.items()):
                        if tid in stale_tabs:
                            self._tab_leases.pop(ch, None)
                if stale_channels or stale_tabs:
                    available = [t for t in pages_only if t["id"] not in self._leased_tabs]
                    if available:
                        print(f"[agent:prov] reclaimed {len(stale_channels)} dead ch, {len(stale_tabs)} stale leases")
                        return available[0]["webSocketDebuggerUrl"]
                    available_all = [t for t in page_tabs if t["id"] not in self._leased_tabs]
                    if available_all:
                        print(f"[agent:prov] reclaimed {len(stale_channels)} dead ch, {len(stale_tabs)} stale leases")
                        return available_all[0]["webSocketDebuggerUrl"]
                # Still all leased — share rather than creating unbounded tabs
                if pages_only:
                    print(f"[agent:prov] sharing existing tab (all leased)")
                    return pages_only[0]["webSocketDebuggerUrl"]
                if page_tabs:
                    return page_tabs[0]["webSocketDebuggerUrl"]
                try:
                    startup_url = _default_new_tab_url(self.relay_url)
                    new_req = _new_tab_request("127.0.0.1", prov_port, startup_url)
                    with urllib.request.urlopen(new_req, timeout=3) as resp:
                        new_tab = json.loads(resp.read())
                except Exception as e:
                    raise RuntimeError(f"No provisioned tabs and /json/new failed: {e}")
                print(f"[agent:prov] auto-created tab (no tabs remained)")
                return new_tab["webSocketDebuggerUrl"]
            matches = [t for t in page_tabs if t["id"].startswith(real_id)]
            if len(matches) == 1:
                return matches[0]["webSocketDebuggerUrl"]
            elif len(matches) == 0:
                raise RuntimeError(f"Provision tab {real_id} not found")
            else:
                raise RuntimeError(f"Provision tab prefix '{real_id}' is ambiguous ({len(matches)} matches)")

        url = f"http://{self.cdp_host}:{self.cdp_port}/json"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            tabs = json.loads(resp.read())
        page_tabs = [t for t in tabs if t.get("type") in ("page", "popup")]
        pages_only = [t for t in tabs if t.get("type") == "page"]
        if tab_id == "auto":
            # Reuse existing lease for this channel
            if channel >= 0 and channel in self._tab_leases:
                leased_id = self._tab_leases[channel]
                leased_match = [t for t in page_tabs if t["id"] == leased_id]
                if leased_match:
                    return leased_match[0]["webSocketDebuggerUrl"]
                # Leased tab was closed externally — release stale lease
                self._tab_leases.pop(channel, None)
                self._leased_tabs.discard(leased_id)
            if not page_tabs:
                # Chrome is running but has no page tabs — create one
                try:
                    startup_url = _default_new_tab_url(self.relay_url)
                    new_req = _new_tab_request(self.cdp_host, self.cdp_port, startup_url)
                    with urllib.request.urlopen(new_req, timeout=3) as resp:
                        new_tab = json.loads(resp.read())
                except Exception as e:
                    raise RuntimeError(f"Chrome has 0 page tabs and /json/new failed: {e}")
                print(f"[agent] auto-created tab (Chrome had 0 page tabs)")
                return new_tab["webSocketDebuggerUrl"]
            # Filter out tabs leased by other channels.  Stale entries in
            # _leased_tabs (from externally-closed tabs) are harmless here:
            # the closed tab won't appear in pages_only/page_tabs, so the
            # stale ID simply doesn't match anything and is a no-op filter.
            if channel >= 0:
                available = [t for t in pages_only if t["id"] not in self._leased_tabs]
                available_all = [t for t in page_tabs if t["id"] not in self._leased_tabs]
            else:
                available = pages_only
                available_all = page_tabs
            if available:
                return available[0]["webSocketDebuggerUrl"]
            if available_all:
                return available_all[0]["webSocketDebuggerUrl"]
            # All tabs appear leased — reconcile stale leases first.
            # Purge leases whose channels are no longer in self.channels
            # (dead connections the relay forgot to close).
            stale_channels = [ch for ch in self._tab_leases
                              if ch not in self.channels]
            for ch in stale_channels:
                tid = self._tab_leases.pop(ch, None)
                if tid:
                    self._leased_tabs.discard(tid)
            # Also purge leases for tabs that no longer exist in Chrome
            live_ids = {t["id"] for t in page_tabs}
            stale_tabs = self._leased_tabs - live_ids
            if stale_tabs:
                self._leased_tabs -= stale_tabs
                for ch, tid in list(self._tab_leases.items()):
                    if tid in stale_tabs:
                        self._tab_leases.pop(ch, None)
            if stale_channels or stale_tabs:
                # Re-filter after purge
                available = [t for t in pages_only if t["id"] not in self._leased_tabs]
                if available:
                    print(f"[agent] reclaimed {len(stale_channels)} dead channel(s), {len(stale_tabs)} stale tab lease(s)")
                    return available[0]["webSocketDebuggerUrl"]
                available_all = [t for t in page_tabs if t["id"] not in self._leased_tabs]
                if available_all:
                    print(f"[agent] reclaimed {len(stale_channels)} dead channel(s), {len(stale_tabs)} stale tab lease(s)")
                    return available_all[0]["webSocketDebuggerUrl"]
            # Still all leased — share the first page tab rather than
            # creating an unbounded number of blank tabs.
            if pages_only:
                print(f"[agent] sharing existing tab (all {len(pages_only)} leased, {len(self._leased_tabs)} leases)")
                return pages_only[0]["webSocketDebuggerUrl"]
            if page_tabs:
                return page_tabs[0]["webSocketDebuggerUrl"]
            # Truly no tabs at all (shouldn't happen — handled above)
            try:
                startup_url = _default_new_tab_url(self.relay_url)
                new_req = _new_tab_request(self.cdp_host, self.cdp_port, startup_url)
                with urllib.request.urlopen(new_req, timeout=3) as resp:
                    new_tab = json.loads(resp.read())
            except Exception as e:
                raise RuntimeError(f"No tabs available and /json/new failed: {e}")
            print(f"[agent] auto-created tab (no tabs remained)")
            return new_tab["webSocketDebuggerUrl"]
        matches = [t for t in page_tabs if t["id"].startswith(tab_id)]
        if len(matches) == 1:
            return matches[0]["webSocketDebuggerUrl"]
        elif len(matches) == 0:
            raise RuntimeError(f"Tab {tab_id} not found")
        else:
            raise RuntimeError(f"Tab prefix '{tab_id}' is ambiguous ({len(matches)} matches)")

    # --- Provision Chrome lifecycle ---

    async def _handle_provision_launch(self, req_id, path):
        """Launch a temporary Chrome with the user's selected profile for provisioning."""
        # Parse profile_path from query string: /provision-launch?profile_path=<encoded>
        profile_path = ""
        copy_mode = "light"
        stealth = False
        caller_tag = ""
        if "?" in path:
            import urllib.parse
            qs = path.split("?", 1)[1]
            params = urllib.parse.parse_qs(qs)
            profile_path = params.get("profile_path", [""])[0]
            copy_mode = (params.get("copy_mode", ["light"])[0] or "light").strip().lower()
            stealth = params.get("stealth", [""])[0].lower() in ("1", "true", "yes")
            caller_tag = params.get("caller_tag", [""])[0].strip()[:64]
        if copy_mode not in {"light", "full"}:
            copy_mode = "light"

        if not profile_path or not os.path.isdir(profile_path):
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 400,
                "body": {"error": f"Invalid profile_path: {profile_path}"},
            }))
            return

        # Generate a unique slot for this provision Chrome
        for _ in range(PROVISION_SLOT_ALLOCATION_ATTEMPTS):
            slot = os.urandom(2).hex()
            if slot not in self._prov_chromes and not os.path.exists(_prov_state_path(slot)):
                break
        else:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": "Provision slot space exhausted"},
            }))
            return

        # Copy profile to temp dir (same logic as signup_agent._copy_chrome_profile)
        temp_dir = os.path.join(DATA_DIR, f"prov_tmp_{slot}_{os.getpid()}_{int(time.time())}")
        profile_dir_name = os.path.basename(profile_path)
        chrome_parent = os.path.dirname(profile_path)

        try:
            os.makedirs(temp_dir, exist_ok=True)

            # Copy Local State (cookie encryption keys)
            local_state = os.path.join(chrome_parent, "Local State")
            if os.path.isfile(local_state):
                shutil.copy2(local_state, os.path.join(temp_dir, "Local State"))

            if copy_mode == "full":
                _copy_profile_full(profile_path, temp_dir, profile_dir_name)
            else:
                _copy_profile_light(profile_path, temp_dir, profile_dir_name)
            print(f"[agent:prov] Copied profile {profile_dir_name} to {temp_dir} (mode={copy_mode})")
        except Exception as e:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": f"Failed to copy profile: {e}"},
            }))
            # Clean up partial temp dir
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
            return

        # Find a free port
        import socket as _socket
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            prov_port = s.getsockname()[1]

        # Find Chrome binary
        chrome_bin = _find_chrome_binary()
        if not chrome_bin:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": "Chrome/Chromium binary not found"},
            }))
            shutil.rmtree(temp_dir, ignore_errors=True)
            return

        # Launch Chrome with the copied profile (visible window for sign-in/ToS)
        startup_url = _default_new_tab_url(self.relay_url)
        cmd = [
            chrome_bin,
            f"--user-data-dir={temp_dir}",
            f"--profile-directory={profile_dir_name}",
            f"--remote-debugging-port={prov_port}",
            "--disable-sync",
            "--disable-background-networking",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1280,900",
        ]
        # Note: --disable-blink-features=AutomationControlled was removed
        # because Chrome shows an "unsupported command-line flag" banner
        # that itself becomes a bot detection signal. The stealth JS
        # injected via Page.addScriptToEvaluateOnNewDocument already
        # overrides navigator.webdriver, making the flag redundant.
        cmd.append(startup_url)
        print(f"[agent:prov] Launching provision Chrome on port {prov_port}"
              f"{' (stealth)' if stealth else ''}...")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        prov_state = {
            "port": prov_port,
            "process": proc,
            "pid": proc.pid,
            "temp_dir": temp_dir,
            "profile_dir_name": profile_dir_name,
            "copy_mode": copy_mode,
            "launched_at": time.time(),
            "ready": False,
            "agent_id": self.agent_id or "",
            "caller_tag": caller_tag,
        }
        try:
            _write_prov_state(slot, prov_state)
        except Exception as e:
            self._cleanup_single_prov(prov_state, slot=slot)
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": f"Failed to persist provision state: {e}"},
            }))
            return

        # Wait for Chrome CDP to be ready
        version_url = f"http://127.0.0.1:{prov_port}/json/version"
        ready = False
        for _ in range(PROVISION_LAUNCH_READY_TIMEOUT):
            time.sleep(1)
            try:
                with urllib.request.urlopen(version_url, timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass

        if not ready:
            self._cleanup_single_prov(prov_state, slot=slot)
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": "Provision Chrome did not start in time"},
            }))
            return

        try:
            first_tab = _first_page_tab("127.0.0.1", prov_port, startup_url)
            first_tab_id = str(first_tab.get("id", "")).strip()
            if not first_tab_id:
                raise RuntimeError("Provision Chrome returned no page tab id")
        except Exception as e:
            self._cleanup_single_prov(prov_state, slot=slot)
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": f"Provision Chrome could not open a startup tab: {e}"},
            }))
            return

        # Store state keyed by slot
        self._prov_chromes[slot] = {
            "port": prov_port,
            "process": proc,
            "pid": proc.pid,
            "temp_dir": temp_dir,
            "profile_dir_name": profile_dir_name,
            "ready": True,
            "caller_tag": caller_tag,
        }
        try:
            _write_prov_state(slot, {**prov_state, "ready": True})
        except Exception as e:
            print(f"[agent:prov] Warning: failed to update ready state for slot {slot}: {e}")

        # Inject stealth fingerprint overrides before any user navigation
        if stealth:
            try:
                await self._inject_stealth_provision(prov_port, first_tab, self._stealth_evasions)
                print(f"[agent:prov] Stealth JS injected into provision slot {slot}")
            except Exception as e:
                print(f"[agent:prov] Stealth inject failed (non-fatal): {e}")

        prov_tab_id = f"prov-{slot}-{first_tab_id}" if first_tab_id else f"prov-{slot}-auto"
        print(f"[agent:prov] Provision Chrome ready: slot={slot}, port={prov_port}, tab={prov_tab_id}")

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"tab_id": prov_tab_id, "slot": slot, "port": prov_port, "copy_mode": copy_mode},
        }))

    async def _handle_provision_cleanup(self, req_id, path=""):
        """Kill provision Chrome(s) and clean up temp dir(s).

        If path contains ?slot=<hex>, clean up only that slot.
        If path contains ?caller_tag=<tag>, clean up only slots from that caller.
        If no slot or caller_tag specified, clean up ALL provision Chromes
        (legacy behavior, but callers should prefer caller_tag isolation).
        """
        self._reconcile_prov_chromes()
        slot = ""
        caller_tag = ""
        if "?" in path:
            import urllib.parse
            qs = path.split("?", 1)[1]
            params = urllib.parse.parse_qs(qs)
            slot = params.get("slot", [""])[0]
            caller_tag = params.get("caller_tag", [""])[0].strip()[:64]

        if not self._prov_chromes:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 200,
                "body": {"status": "no_provision_chrome"},
            }))
            return

        cleaned = 0
        if slot:
            # Specific slot requested — clean only that one.
            # If caller_tag is set, verify ownership before cleaning.
            prov = self._prov_chromes.get(slot)
            if prov:
                if caller_tag:
                    slot_tag = prov.get("caller_tag", "")
                    if not slot_tag:
                        state = _read_prov_state(slot)
                        slot_tag = state.get("caller_tag", "") if state else ""
                    if slot_tag and slot_tag != caller_tag:
                        # Slot belongs to a different caller — refuse cleanup
                        await self.ws.send(json.dumps({
                            "type": "http_response",
                            "req_id": req_id,
                            "status": 403,
                            "body": {"error": f"Slot {slot} belongs to a different caller"},
                        }))
                        return
                self._prov_chromes.pop(slot, None)
                self._cleanup_single_prov(prov, slot=slot)
                cleaned = 1
        elif caller_tag:
            # Clean only slots matching this caller_tag — leaves other
            # callers' provisioned Chrome instances untouched.
            matching_slots = []
            for s, p in self._prov_chromes.items():
                # Prefer in-memory caller_tag, fall back to disk state
                slot_tag = p.get("caller_tag", "")
                if not slot_tag:
                    state = _read_prov_state(s)
                    slot_tag = state.get("caller_tag", "") if state else ""
                if slot_tag == caller_tag:
                    matching_slots.append((s, p))
            for s, p in matching_slots:
                self._prov_chromes.pop(s, None)
                self._cleanup_single_prov(p, slot=s)
                cleaned += 1
        else:
            # No slot or caller_tag — clean up all (legacy behavior).
            while self._prov_chromes:
                s, prov = self._prov_chromes.popitem()
                self._cleanup_single_prov(prov, slot=s)
                cleaned += 1

        status = "cleaned_up" if cleaned else "nothing_to_clean"
        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"status": status, "cleaned": cleaned},
        }))

    async def _handle_provision_status(self, req_id):
        """Return all provisioned Chrome slots with their tabs (prov-prefixed IDs)."""
        self._reconcile_prov_chromes()
        if not self._prov_chromes:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 200,
                "body": {"slots": {}},
            }))
            return

        slots = {}
        for slot, prov in self._prov_chromes.items():
            if not prov.get("ready", True):
                continue
            port = prov["port"]
            profile_dir = prov.get("profile_dir_name", "")
            tabs_info = []
            try:
                url = f"http://127.0.0.1:{port}/json"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    tabs = json.loads(resp.read())
                for t in tabs:
                    if t.get("type") not in ("page", "popup"):
                        continue
                    tabs_info.append({
                        "tab_id": f"prov-{slot}-{t['id']}",
                        "type": t.get("type", ""),
                        "title": t.get("title", ""),
                        "url": t.get("url", ""),
                    })
            except Exception as e:
                tabs_info.append({"error": str(e)})
            slots[slot] = {
                "profile": profile_dir,
                "port": port,
                "tabs": tabs_info,
            }

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"slots": slots},
        }))

    def _cleanup_single_prov(self, prov: dict, slot: str = "", kill_process: bool = True):
        """Kill one provision Chrome process and delete its temp dir."""
        if kill_process:
            proc = prov.get("process")
            pid = int(prov.get("pid") or getattr(proc, "pid", 0) or 0)
            if proc:
                _terminate_process(proc, "Provision Chrome", prefix="[agent:prov]")
            elif pid:
                _terminate_pid(pid, "Provision Chrome", prefix="[agent:prov]")

        temp_dir = prov.get("temp_dir", "")
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"[agent:prov] Cleaned up {temp_dir}")
            except Exception as e:
                print(f"[agent:prov] Warning: failed to clean up {temp_dir}: {e}")
        if slot:
            _remove_prov_state(slot)

    def _cleanup_all_prov_chromes(self, include_persisted: bool = False):
        """Kill all provision Chromes owned by this agent and clean up temp dirs.

        Agent isolation depends on _reconcile_prov_chromes filtering out slots
        belonging to other agents before they enter self._prov_chromes.
        """
        if include_persisted:
            self._reconcile_prov_chromes(force=True)
        for slot in list(self._prov_chromes):
            prov = self._prov_chromes.pop(slot)
            self._cleanup_single_prov(prov, slot=slot)

    async def _handle_ws_send(self, msg: dict):
        """Forward a CDP message from relay to Chrome."""
        channel = msg.get("channel", 0)
        data = msg.get("data", {})
        chrome_ws = self.channels.get(channel)
        if chrome_ws:
            await chrome_ws.send(json.dumps(data))

    async def _handle_ws_close(self, msg: dict):
        """Close a CDP WebSocket channel."""
        channel = msg.get("channel", 0)
        await self._close_channel(channel)
        await self.ws.send(json.dumps({
            "type": "ws_closed",
            "channel": channel,
        }))

    async def _forward_chrome_to_relay(self, channel: int, chrome_ws):
        """Background task: forward all messages from Chrome to relay."""
        try:
            async for raw in chrome_ws:
                data = json.loads(raw)
                if self.ws:
                    await self.ws.send(json.dumps({
                        "type": "ws_recv",
                        "channel": channel,
                        "data": data,
                    }))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            # Release tab lease on abnormal disconnect (prevents stale leases).
            # _close_channel also releases the same lease via .pop(); both use
            # dict.pop(key, None) and set.discard(), which are idempotent, so
            # double-release from both paths is safe and intentional.
            leased_tab_id = self._tab_leases.pop(channel, None)
            if leased_tab_id:
                self._leased_tabs.discard(leased_tab_id)
            # Notify relay that channel closed from Chrome side
            if self.ws:
                try:
                    await self.ws.send(json.dumps({
                        "type": "ws_closed",
                        "channel": channel,
                    }))
                except Exception:
                    pass

    async def _close_channel(self, channel: int):
        """Close a single channel and its forwarding task."""
        task = self._channel_tasks.pop(channel, None)
        if task:
            task.cancel()
        ws = self.channels.pop(channel, None)
        if ws:
            await ws.close()
        # Release tab lease
        leased_tab_id = self._tab_leases.pop(channel, None)
        if leased_tab_id:
            self._leased_tabs.discard(leased_tab_id)

    async def _close_all_channels(self):
        """Close all open channels."""
        for ch in list(self.channels.keys()):
            await self._close_channel(ch)

    # --- Heartbeat ---

    async def _heartbeat(self):
        """Send ping every HEARTBEAT_INTERVAL seconds."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.ws:
                try:
                    await self.ws.send(json.dumps({
                        "type": "ping",
                        "ts": time.time(),
                    }))
                except Exception:
                    break

    async def _pong_watchdog(self):
        """Close tunnel if no activity within timeout.

        _last_pong is reset both on message receipt AND after each handler
        returns, so long blocking handlers (provision-launch, etc.) cannot
        cause a false trigger — the post-handler reset gives a fresh baseline.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if not self.ws:
                continue
            elapsed = time.time() - self._last_pong
            if elapsed > HEARTBEAT_INTERVAL + HEARTBEAT_TIMEOUT:
                print(f"[agent] pong timeout ({elapsed:.0f}s), closing tunnel")
                self._watchdog_triggered = True
                await self.ws.close()
                break

    # --- Reconnection ---

    async def _reconnect_with_backoff(self):
        """Wait with exponential backoff before reconnecting."""
        wait = min(self._backoff, MAX_BACKOFF)
        print(f"[agent] reconnecting in {wait}s...")
        await asyncio.sleep(wait)
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    # --- Shutdown ---

    async def stop(self):
        """Clean shutdown."""
        print("[agent] stopping...")
        self.running = False
        await self._close_all_channels()
        self._cleanup_all_prov_chromes(include_persisted=True)
        if self.ws:
            await self.ws.close()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    """Load config from ~/.unchained/agent.json, env vars, then CLI flags."""
    config = {
        "relay_url": DEFAULT_RELAY_URL,
        "api_key": "",
        "cdp_host": DEFAULT_CDP_HOST,
        "cdp_port": DEFAULT_CDP_PORT,
        "profile": "default",
        "chrome_headless": False,
        "chrome_stealth": True,
        "stealth_evasions": "",   # "" or "all" = all; "name1,name2" = only those
        "stealth_disable": "",    # "name1,name2" = disable these from the enabled set
        "chrome_args": "",
        "daemon": False,
    }
    # Layer 1: config file
    if os.path.exists(AGENT_CONFIG_FILE):
        try:
            with open(AGENT_CONFIG_FILE) as f:
                file_config = json.load(f)
            config.update({k: v for k, v in file_config.items() if k in config})
        except (json.JSONDecodeError, OSError):
            pass
    # Layer 2: env vars
    if os.environ.get("UNCHAINED_RELAY_URL"):
        config["relay_url"] = os.environ["UNCHAINED_RELAY_URL"]
    if os.environ.get("UNCHAINED_API_KEY"):
        config["api_key"] = os.environ["UNCHAINED_API_KEY"]
    if os.environ.get("CDP_HOST"):
        config["cdp_host"] = os.environ["CDP_HOST"]
    if os.environ.get("CDP_PORT"):
        config["cdp_port"] = int(os.environ["CDP_PORT"])
    if os.environ.get("CDP_PROFILE"):
        config["profile"] = os.environ["CDP_PROFILE"]
    if os.environ.get("UNCHAINED_CHROME_HEADLESS", "").lower() in (
        "1", "true", "yes", "on",
    ):
        config["chrome_headless"] = True
    _stealth_env = os.environ.get("UNCHAINED_CHROME_STEALTH", "").lower()
    if _stealth_env in ("1", "true", "yes", "on"):
        config["chrome_stealth"] = True
    elif _stealth_env in ("0", "false", "no", "off"):
        config["chrome_stealth"] = False
    if os.environ.get("UNCHAINED_STEALTH_EVASIONS"):
        config["stealth_evasions"] = os.environ["UNCHAINED_STEALTH_EVASIONS"]
    if os.environ.get("UNCHAINED_STEALTH_DISABLE"):
        config["stealth_disable"] = os.environ["UNCHAINED_STEALTH_DISABLE"]
    if os.environ.get("UNCHAINED_CHROME_ARGS"):
        config["chrome_args"] = os.environ["UNCHAINED_CHROME_ARGS"]
    return config


def _parse_args(args: list[str], config: dict) -> dict:
    """Parse CLI flags into config dict."""
    i = 0
    while i < len(args):
        if args[i] == "--relay" and i + 1 < len(args):
            config["relay_url"] = args[i + 1]
            i += 2
        elif args[i] == "--key" and i + 1 < len(args):
            config["api_key"] = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            config["cdp_port"] = int(args[i + 1])
            i += 2
        elif args[i] == "--host" and i + 1 < len(args):
            config["cdp_host"] = args[i + 1]
            i += 2
        elif args[i] == "--profile" and i + 1 < len(args):
            config["profile"] = args[i + 1]
            i += 2
        elif args[i] == "--headless":
            config["chrome_headless"] = True
            i += 1
        elif args[i] == "--no-headless":
            config["chrome_headless"] = False
            i += 1
        elif args[i] == "--stealth":
            config["chrome_stealth"] = True
            i += 1
        elif args[i] == "--no-stealth":
            config["chrome_stealth"] = False
            i += 1
        elif args[i] == "--stealth-evasions" and i + 1 < len(args):
            config["stealth_evasions"] = args[i + 1]
            i += 2
        elif args[i] == "--stealth-disable" and i + 1 < len(args):
            config["stealth_disable"] = args[i + 1]
            i += 2
        elif args[i] in ("--daemon", "-d"):
            config["daemon"] = True
            i += 1
        elif args[i] == "--chrome-args" and i + 1 < len(args):
            config["chrome_args"] = args[i + 1]
            i += 2
        else:
            i += 1
    return config


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------
def _port_file(profile: str = "default") -> str:
    """Return per-profile port file path."""
    if profile and profile != "default":
        return os.path.join(DATA_DIR, f".agent_port_{profile}")
    return os.path.join(DATA_DIR, ".agent_port")


def _write_pid(profile: str = "default", port: int = 0):
    """Write current PID and port to per-profile agent files."""
    with open(_pid_file(profile), "w") as f:
        f.write(str(os.getpid()))
    if port:
        with open(_port_file(profile), "w") as f:
            f.write(str(port))


def _read_pid(profile: str = "default"):
    """Read agent PID from per-profile file."""
    try:
        with open(_pid_file(profile)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_port(profile: str = "default") -> int | None:
    """Read agent CDP port from per-profile file."""
    try:
        with open(_port_file(profile)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _remove_pid(profile: str = "default"):
    """Remove per-profile agent PID and port files."""
    try:
        os.remove(_pid_file(profile))
    except OSError:
        pass
    try:
        os.remove(_port_file(profile))
    except OSError:
        pass


def _parse_port_from_cmdline(cmdline: str) -> int:
    """Extract --port value from a bridge process cmdline.

    Returns DEFAULT_CDP_PORT if --port is absent or unparseable.
    """
    parts = cmdline.split()
    for i, part in enumerate(parts):
        if part == "--port" and i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                break
    return DEFAULT_CDP_PORT


def _check_port_conflict(port: int, profile: str) -> str | None:
    """Check if another profile is already using this CDP port.

    Returns the conflicting profile name, or None if no conflict.
    """
    import glob as _glob
    # Check all PID files to find running agents
    for pid_path in _glob.glob(os.path.join(DATA_DIR, ".agent_pid*")):
        basename = os.path.basename(pid_path)
        if basename == ".agent_pid":
            other_profile = "default"
        elif basename.startswith(".agent_pid_"):
            other_profile = basename[len(".agent_pid_"):]
        else:
            continue
        if other_profile == profile:
            continue  # same profile, not a conflict
        # Check if this other agent is actually alive AND is a bridge process
        try:
            with open(pid_path) as f:
                other_pid = int(f.read().strip())
            os.kill(other_pid, 0)  # just check existence
        except (OSError, ValueError):
            continue  # dead or unreadable, skip
        cmdline = _process_cmdline(other_pid)
        if cmdline and "chrome_bridge" not in cmdline:
            continue  # PID recycled by unrelated process, not a real bridge
        other_port = _read_port(other_profile)
        if other_port is None:
            # Legacy bridge (pre-port-file). Parse --port from cmdline,
            # fall back to default CDP port.
            other_port = _parse_port_from_cmdline(cmdline)
        if other_port == port:
            return other_profile
    return None


def _process_cmdline(pid: int) -> str:
    """Return command line for pid, or empty string when unavailable."""
    if pid <= 0:
        return ""

    if platform.system() == "Windows":
        # WMI gives us command-line contents so we can reject reused stale PIDs.
        ps_cmd = (
            f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}" '
            " -ErrorAction SilentlyContinue; "
            'if ($p) { [string]$p.CommandLine }'
        )
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            return out.strip()
        except Exception:
            return ""

    proc_cmdline = f"/proc/{pid}/cmdline"
    if os.path.exists(proc_cmdline):
        try:
            raw = open(proc_cmdline, "rb").read().replace(b"\x00", b" ").strip()
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return out.strip()
    except Exception:
        return ""


def _is_agent_running(profile: str = "default") -> bool:
    """Check if agent process is still alive for the given profile."""
    pid = _read_pid(profile)
    if pid is None:
        return False
    if pid == os.getpid():
        # Stale PID file from a previous container/run that happened to use the
        # same PID (always PID 1 in Docker). This is us, not a duplicate agent.
        _remove_pid(profile)
        return False

    # Fast path: process exists.
    try:
        os.kill(pid, 0)
    except OSError:
        _remove_pid(profile)
        return False

    cmdline = _process_cmdline(pid)
    if not cmdline:
        # In restricted environments (sandboxed CI, limited containers), we may
        # be unable to inspect command lines even when the process exists.
        # Keep the pid file and treat it as running to avoid false "stopped".
        return True
    if "chrome_bridge.py" not in cmdline:
        # PID got recycled by an unrelated process; treat as stale.
        _remove_pid(profile)
        return False
    return True


def _ensure_chrome(
    host: str,
    port: int,
    profile: str = "default",
    headless: bool = False,
    stealth: bool = False,
    extra_chrome_args: str = "",
    relay_url: str = DEFAULT_RELAY_URL,
):
    """Launch Chrome with CDP if not already running."""
    url = f"http://{host}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            if resp.status == 200:
                print(f"[agent] Chrome CDP already running on {host}:{port}")
                return True
    except Exception:
        pass

    if host not in ("127.0.0.1", "localhost"):
        print(f"[agent] Chrome not reachable at {host}:{port} (remote host, can't launch)")
        return False

    print(f"[agent] Chrome not running, launching (profile={profile}, port={port})...")
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        if platform.system() == "Windows":
            print("[agent] no Chrome/Chromium/Edge binary found (checked standard install paths and PATH)")
        else:
            print("[agent] no Chrome/Chromium binary found in PATH")
        return False

    profile_dir = os.path.join(DATA_DIR, f"chrome_{profile}")
    os.makedirs(profile_dir, exist_ok=True)
    # Remove stale SingletonLock from previous container (different hostname).
    lock_file = os.path.join(profile_dir, "SingletonLock")
    if os.path.islink(lock_file) or os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            print("[agent] removed stale SingletonLock")
        except OSError:
            pass

    startup_url = _default_new_tab_url(relay_url)
    cmd = [
        chrome_bin,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        startup_url,
    ]
    if headless:
        cmd.extend([
            "--headless=new",
            "--disable-gpu",
            "--use-gl=angle",
            "--use-angle=swiftshader-webgl",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1920,1080",
            "--ozone-override-screen-size=1920,1080",
        ])
        # Root-run Chromium still needs --no-sandbox; non-root containers do not.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")
    if stealth:
        # AutomationControlled flag is safe on all platforms — defense-in-depth
        # alongside the JS navigator.webdriver override.
        cmd.append("--disable-blink-features=AutomationControlled")
        # UA override only in headless — on real browsers the native macOS/
        # Windows UA is already clean and a Linux UA would be a mismatch.
        if headless:
            ua_version = "131.0.0.0"
            try:
                ver_out = subprocess.check_output(
                    [chrome_bin, "--version"], stderr=subprocess.DEVNULL, timeout=5,
                ).decode().strip()
                m = re.search(r"(\d+\.\d+\.\d+\.\d+)", ver_out)
                if m:
                    ua_version = m.group(1)
            except Exception:
                pass
            cmd.append(
                f"--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                f" (KHTML, like Gecko) Chrome/{ua_version} Safari/537.36",
            )
    # Auto-CAPTCHA solver extension (NopeCHA). When NOPECHA_EXT_DIR points to
    # an unpacked extension directory, load it into Chrome so CAPTCHAs are
    # solved transparently before the agent sees them.
    # Free tier: 100 solves/day, no API key needed.
    nopecha_dir = os.environ.get("NOPECHA_EXT_DIR", "")
    nopecha_active = nopecha_dir and os.path.isdir(nopecha_dir)
    if nopecha_active:
        cmd.extend([
            f"--disable-extensions-except={nopecha_dir}",
            f"--load-extension={nopecha_dir}",
        ])
        print(f"[agent] NopeCHA extension loaded from {nopecha_dir}")
    if extra_chrome_args:
        parts = shlex.split(extra_chrome_args)
        # Strip extension-blocking flags when NopeCHA is active — they
        # would prevent the extension from loading.
        if nopecha_active:
            parts = [p for p in parts
                     if p != "--disable-extensions"
                     and not p.startswith("--disable-extensions-except")]
        cmd.extend(parts)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Write Chrome PID so we can stop it later
    chrome_pid_file = os.path.join(DATA_DIR, f".chrome_pid_{port}")
    with open(chrome_pid_file, "w") as f:
        f.write(str(proc.pid))

    startup_attempts = 30 if headless else 15
    for _ in range(startup_attempts):
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status != 200:
                    continue
        except Exception:
            continue

        try:
            # Chrome is up; fail fast if the startup tab cannot be opened.
            _first_page_tab(host, port, startup_url)
        except Exception as e:
            _terminate_process(proc, "Chrome")
            try:
                os.remove(chrome_pid_file)
            except OSError:
                pass
            print(f"[agent] Chrome started but could not open startup tab: {e}")
            return False

        print(f"[agent] Chrome started (PID {proc.pid}, profile={profile}, port={port})")
        return True

    print("[agent] Chrome did not start in time")
    _terminate_process(proc, "Chrome")
    try:
        os.remove(chrome_pid_file)
    except OSError:
        pass
    return False


def cmd_start(config: dict):
    """Start the agent (foreground)."""
    relay_url = config.get("relay_url", DEFAULT_RELAY_URL)
    profile = config["profile"]
    if _is_agent_running(profile):
        pid = _read_pid(profile)
        print(f"[agent] already running (PID {pid}, profile={profile})")
        return

    # Guard: block if another profile already uses this CDP port
    cdp_port = config["cdp_port"]
    conflict = _check_port_conflict(cdp_port, profile)
    if conflict:
        print(f"[agent] CDP port {cdp_port} is already used by profile '{conflict}'")
        print(f"[agent] use --port <other_port> to avoid conflicts")
        return

    if config.get("daemon"):
        _start_detached(config)
        return

    # Ensure Chrome is running with CDP
    if not _ensure_chrome(
        config["cdp_host"],
        cdp_port,
        profile,
        config.get("chrome_headless", False),
        config.get("chrome_stealth", False),
        config.get("chrome_args", ""),
        relay_url,
    ):
        print("[agent] cannot start without Chrome CDP")
        return

    _write_pid(profile, port=cdp_port)
    # Only pass explicit evasion set if the user specified --stealth-evasions
    # or --stealth-disable.  Otherwise pass None so Agent.__init__ auto-selects
    # the correct tier (base for headed, all for headless).
    evasions_csv = config.get("stealth_evasions", "")
    disable_csv = config.get("stealth_disable", "")
    if evasions_csv or disable_csv:
        stealth_evasions = _resolve_stealth_evasions(evasions_csv, disable_csv)
    else:
        stealth_evasions = None
    agent = Agent(
        relay_url=relay_url,
        api_key=config["api_key"],
        cdp_host=config["cdp_host"],
        cdp_port=config["cdp_port"],
        profile=config["profile"],
        headless=config.get("chrome_headless", False),
        stealth=config.get("chrome_stealth", True),
        stealth_evasions=stealth_evasions,
    )

    import atexit
    log = logging.getLogger("chrome_bridge")
    if not log.handlers:
        _log_dir = os.environ.get("UNCHAINED_DATA_DIR", os.path.expanduser("~/.unchained"))
        os.makedirs(_log_dir, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            handlers=[
                logging.FileHandler(os.path.join(_log_dir, "bridge.log")),
                logging.StreamHandler(),
            ],
        )

    loop = asyncio.new_event_loop()

    def _shutdown(sig, frame):
        sig_name = signal.Signals(sig).name
        log.warning("[bridge] received %s (signal %d) — shutting down", sig_name, sig)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(agent.stop()))

    def _on_exit():
        log.info("[bridge] process exiting (atexit)")

    atexit.register(_on_exit)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, _shutdown)
        except (OSError, ValueError):
            pass  # SIGHUP may not be available/allowed in this environment

    try:
        loop.run_until_complete(agent.start())
    except KeyboardInterrupt:
        log.info("[bridge] stopped by KeyboardInterrupt")
    except BaseException:
        log.critical("[bridge] crashed with unhandled exception", exc_info=True)
        raise
    finally:
        _remove_pid(profile)
        loop.close()
        log.info("[bridge] stopped")


def _start_detached(config: dict):
    """Start the bridge as a detached background process."""
    relay_url = config.get("relay_url", DEFAULT_RELAY_URL)
    script_path = os.path.abspath(__file__)
    cmd = [
        sys.executable,
        script_path,
        "start",
        "--relay",
        relay_url,
        "--host",
        config["cdp_host"],
        "--port",
        str(config["cdp_port"]),
        "--profile",
        config["profile"],
    ]
    if config.get("api_key"):
        cmd.extend(["--key", config["api_key"]])
    if config.get("chrome_headless", False):
        cmd.append("--headless")
    else:
        cmd.append("--no-headless")
    if config.get("chrome_args"):
        cmd.extend(["--chrome-args", config["chrome_args"]])

    log_path = os.path.join(DATA_DIR, "bridge.log")
    log_fp = open(log_path, "a", buffering=1)

    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fp,
        "stderr": log_fp,
        "cwd": os.getcwd(),
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(cmd, **kwargs)

        profile = config["profile"]
        for _ in range(40):
            time.sleep(0.25)
            if _is_agent_running(profile):
                pid = _read_pid(profile)
                print(f"[agent] started in daemon mode (PID {pid}, profile={profile})")
                print(f"[agent] logs: {log_path}")
                return

        print("[agent] daemon launch requested, but bridge PID was not confirmed yet")
        print(f"[agent] check logs: {log_path}")
    finally:
        log_fp.close()


def cmd_status(config: dict):
    """Show agent connection state."""
    profile = config["profile"]
    if _is_agent_running(profile):
        pid = _read_pid(profile)
        print(json.dumps({"status": "running", "pid": pid, "profile": profile}))
    else:
        print(json.dumps({"status": "stopped", "profile": profile}))


def cmd_stop(config: dict):
    """Stop the running agent."""
    profile = config["profile"]
    pid = _read_pid(profile)
    if pid is None:
        print(f"[agent] not running (profile={profile})")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"[agent] sent SIGTERM to PID {pid} (profile={profile})")
    except ProcessLookupError:
        print("[agent] process not found")
    except OSError:
        if platform.system() == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                print(f"[agent] requested termination for PID {pid} via taskkill")
            except Exception:
                print("[agent] failed to stop process")
        else:
            print("[agent] failed to stop process")
    _remove_pid(profile)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    config = _load_config()
    args = sys.argv[1:]
    config = _parse_args(args, config)

    # Sanitize profile name (e.g. "Profile 5" → "profile_5")
    config["profile"] = _sanitize_profile(config["profile"])

    # Extract command (first non-flag argument)
    cmd = None
    for a in args:
        if not a.startswith("--"):
            cmd = a
            break

    if cmd == "start":
        cmd_start(config)
    elif cmd == "status":
        cmd_status(config)
    elif cmd == "stop":
        cmd_stop(config)
    else:
        print("""Usage: uv run chrome_bridge.py <command> [options]

Commands:
    start       Connect to relay and start tunneling
    status      Show agent connection state
    stop        Stop running agent

Options:
    --relay <url>       Relay WebSocket URL (default: ws://127.0.0.1:8765/tunnel)
    --key <api_key>     API key for authentication
    --port <port>       Chrome CDP port (default: 9222)
    --host <host>       Chrome CDP host (default: 127.0.0.1)
    --profile <name>    Chrome profile name (default: default)
    --headless          Launch local Chrome in headless mode
    --no-headless       Launch local Chrome with a visible window
    --daemon, -d        Run in background (detached from terminal)
    --chrome-args <s>   Extra Chrome launch args string

Each profile gets its own Chrome data directory (~/.unchained/chrome_<name>/)
with separate cookies, sessions, history, and extensions.

Config file: ~/.unchained/agent.json
Env vars: UNCHAINED_RELAY_URL, UNCHAINED_API_KEY, CDP_HOST, CDP_PORT, CDP_PROFILE,
          UNCHAINED_CHROME_HEADLESS, UNCHAINED_CHROME_ARGS
""")


if __name__ == "__main__":
    main()
