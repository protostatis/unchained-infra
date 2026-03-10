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
    uv run chrome_bridge.py start --relay ws://host:8765/tunnel  # Custom relay
    uv run chrome_bridge.py start --key uk_live_xxx        # With API key
    uv run chrome_bridge.py status                         # Show connection state
    uv run chrome_bridge.py stop                           # Stop running agent
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shlex
import shutil
import signal
import sys
import time
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

AGENT_PID_FILE = os.path.join(DATA_DIR, ".agent_pid")
AGENT_CONFIG_FILE = os.path.join(DATA_DIR, "agent.json")

DEFAULT_RELAY_URL = "ws://127.0.0.1:8765/tunnel"
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9222

HEARTBEAT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 10   # seconds
MAX_BACKOFF = 60          # seconds

VERSION = "0.1.0"


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
                 headless: bool = False):
        self.relay_url = relay_url
        self.api_key = api_key
        self.cdp_host = cdp_host
        self.cdp_port = cdp_port
        self.profile = profile
        self._headless = headless
        self.ws = None
        self.channels: dict[int, websockets.WebSocketClientProtocol] = {}
        self._channel_tasks: dict[int, asyncio.Task] = {}
        self.running = False
        self.agent_id = None  # type: Optional[str]
        self._backoff = 1
        self._last_pong = 0.0
        # Provision Chrome: temporary Chromes keyed by slot (4-char hex)
        self._prov_chromes: dict[str, dict] = {}  # slot → {port, process, temp_dir, profile_dir_name}

    def _relaunch_chrome(self) -> bool:
        """Try to relaunch Chrome if it's not reachable."""
        return _ensure_chrome(self.cdp_host, self.cdp_port, self.profile)

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
            self._cleanup_all_prov_chromes()

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

        # Proxy /prov/{slot}/{path} requests to the provision Chrome's port
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
            ws_url = self._get_tab_ws_url(tab_id)
        except Exception:
            # Chrome may have closed — try to relaunch
            if self._relaunch_chrome():
                try:
                    ws_url = self._get_tab_ws_url(tab_id)
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
        except Exception as e:
            await self.ws.send(json.dumps({
                "type": "ws_error",
                "channel": channel,
                "error": str(e),
            }))

    def _get_tab_ws_url(self, tab_id: str) -> str:
        """Look up a tab's WebSocket URL from local Chrome.

        If tab_id starts with 'prov-', route to the provision Chrome instead.
        New format: prov-{slot}-{real_id}  Old format: prov-{real_id}
        """
        # Provision Chrome routing
        if tab_id.startswith("prov-") and self._prov_chromes:
            slot, real_id = _parse_prov_tab_id(tab_id)
            # Look up by slot; backward compat: if no slot and exactly one prov Chrome, use it
            if slot and slot in self._prov_chromes:
                prov = self._prov_chromes[slot]
            elif not slot and len(self._prov_chromes) == 1:
                prov = next(iter(self._prov_chromes.values()))
            else:
                raise RuntimeError(f"Provision Chrome slot '{slot}' not found")
            prov_port = prov["port"]
            url = f"http://127.0.0.1:{prov_port}/json"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                tabs = json.loads(resp.read())
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            if real_id == "auto" and page_tabs:
                return page_tabs[0]["webSocketDebuggerUrl"]
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
        page_tabs = [t for t in tabs if t.get("type") == "page"]
        if tab_id == "auto" and not page_tabs:
            # Chrome is running but has no page tabs — create one
            new_req = urllib.request.Request(
                f"http://{self.cdp_host}:{self.cdp_port}/json/new",
                method="PUT")
            with urllib.request.urlopen(new_req, timeout=3) as resp:
                new_tab = json.loads(resp.read())
            print(f"[agent] auto-created tab (Chrome had 0 page tabs)")
            return new_tab["webSocketDebuggerUrl"]
        if tab_id == "auto" and page_tabs:
            return page_tabs[0]["webSocketDebuggerUrl"]
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
        if "?" in path:
            import urllib.parse
            qs = path.split("?", 1)[1]
            params = urllib.parse.parse_qs(qs)
            profile_path = params.get("profile_path", [""])[0]
            copy_mode = (params.get("copy_mode", ["light"])[0] or "light").strip().lower()
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
        slot = os.urandom(2).hex()

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
            "about:blank",
        ]
        print(f"[agent:prov] Launching provision Chrome on port {prov_port}...")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for Chrome CDP to be ready
        version_url = f"http://127.0.0.1:{prov_port}/json/version"
        ready = False
        for _ in range(15):
            time.sleep(1)
            try:
                with urllib.request.urlopen(version_url, timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except Exception:
                pass

        if not ready:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            shutil.rmtree(temp_dir, ignore_errors=True)
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": "Provision Chrome did not start in time"},
            }))
            return

        # Get the first page tab
        tabs_url = f"http://127.0.0.1:{prov_port}/json"
        try:
            with urllib.request.urlopen(tabs_url, timeout=3) as resp:
                tabs = json.loads(resp.read())
            page_tabs = [t for t in tabs if t.get("type") == "page"]
            first_tab_id = page_tabs[0]["id"] if page_tabs else ""
        except Exception:
            first_tab_id = ""

        # Store state keyed by slot
        self._prov_chromes[slot] = {
            "port": prov_port,
            "process": proc,
            "temp_dir": temp_dir,
            "profile_dir_name": profile_dir_name,
        }

        prov_tab_id = f"prov-{slot}-{first_tab_id}" if first_tab_id else f"prov-{slot}-auto"
        print(f"[agent:prov] Provision Chrome ready: slot={slot}, port={prov_port}, tab={prov_tab_id}")

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"tab_id": prov_tab_id, "port": prov_port, "copy_mode": copy_mode},
        }))

    async def _handle_provision_cleanup(self, req_id, path=""):
        """Kill provision Chrome(s) and clean up temp dir(s).

        If path contains ?slot=<hex>, clean up only that slot.
        If no slot specified, clean up ALL provision Chromes.
        """
        slot = ""
        if "?" in path:
            import urllib.parse
            qs = path.split("?", 1)[1]
            params = urllib.parse.parse_qs(qs)
            slot = params.get("slot", [""])[0]

        if not self._prov_chromes:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 200,
                "body": {"status": "no_provision_chrome"},
            }))
            return

        if slot and slot in self._prov_chromes:
            prov = self._prov_chromes.pop(slot)
            self._cleanup_single_prov(prov)
        else:
            # No slot or slot not found: clean up all
            self._cleanup_all_prov_chromes()

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"status": "cleaned_up"},
        }))

    def _cleanup_single_prov(self, prov: dict):
        """Kill one provision Chrome process and delete its temp dir."""
        proc = prov.get("process")
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
                print("[agent:prov] Provision Chrome terminated")
            except Exception:
                try:
                    proc.kill()
                    print("[agent:prov] Provision Chrome killed")
                except Exception:
                    pass

        temp_dir = prov.get("temp_dir", "")
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"[agent:prov] Cleaned up {temp_dir}")
            except Exception as e:
                print(f"[agent:prov] Warning: failed to clean up {temp_dir}: {e}")

    def _cleanup_all_prov_chromes(self):
        """Kill all provision Chromes and clean up temp dirs."""
        for slot in list(self._prov_chromes):
            prov = self._prov_chromes.pop(slot)
            self._cleanup_single_prov(prov)

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
        self._cleanup_all_prov_chromes()
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
def _write_pid():
    """Write current PID to agent PID file."""
    with open(AGENT_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _read_pid():
    """Read agent PID from file."""
    try:
        with open(AGENT_PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _remove_pid():
    """Remove agent PID file."""
    try:
        os.remove(AGENT_PID_FILE)
    except OSError:
        pass


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


def _is_agent_running() -> bool:
    """Check if agent process is still alive."""
    pid = _read_pid()
    if pid is None:
        return False
    if pid == os.getpid():
        # Stale PID file from a previous container/run that happened to use the
        # same PID (always PID 1 in Docker). This is us, not a duplicate agent.
        _remove_pid()
        return False

    # Fast path: process exists.
    try:
        os.kill(pid, 0)
    except OSError:
        _remove_pid()
        return False

    cmdline = _process_cmdline(pid)
    if not cmdline:
        # In restricted environments (sandboxed CI, limited containers), we may
        # be unable to inspect command lines even when the process exists.
        # Keep the pid file and treat it as running to avoid false "stopped".
        return True
    if "chrome_bridge.py" not in cmdline:
        # PID got recycled by an unrelated process; treat as stale.
        _remove_pid()
        return False
    return True


def _ensure_chrome(
    host: str,
    port: int,
    profile: str = "default",
    headless: bool = False,
    extra_chrome_args: str = "",
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

    cmd = [
        chrome_bin,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",  # Ensure at least one tab opens
    ]
    if headless:
        cmd.extend([
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--hide-scrollbars",
            "--window-size=1920,1080",
        ])
        # Root-run Chromium still needs --no-sandbox; non-root containers do not.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")
    if extra_chrome_args:
        cmd.extend(shlex.split(extra_chrome_args))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Write Chrome PID so we can stop it later
    chrome_pid_file = os.path.join(DATA_DIR, f".chrome_pid_{port}")
    with open(chrome_pid_file, "w") as f:
        f.write(str(proc.pid))

    for _ in range(15):
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    # Ensure at least one page tab exists
                    tabs_url = f"http://{host}:{port}/json"
                    with urllib.request.urlopen(tabs_url, timeout=2) as tr:
                        tabs = json.loads(tr.read())
                    page_tabs = [t for t in tabs if t.get("type") == "page"]
                    if not page_tabs:
                        # Create a blank tab via CDP
                        req = urllib.request.Request(
                            f"http://{host}:{port}/json/new", method="PUT")
                        urllib.request.urlopen(req, timeout=3)
                    print(f"[agent] Chrome started (PID {proc.pid}, profile={profile}, port={port})")
                    return True
        except Exception:
            pass

    print("[agent] Chrome did not start in time")
    return False


def cmd_start(config: dict):
    """Start the agent (foreground)."""
    if _is_agent_running():
        pid = _read_pid()
        print(f"[agent] already running (PID {pid})")
        return

    if config.get("daemon"):
        _start_detached(config)
        return

    # Ensure Chrome is running with CDP
    if not _ensure_chrome(
        config["cdp_host"],
        config["cdp_port"],
        config["profile"],
        config.get("chrome_headless", False),
        config.get("chrome_args", ""),
    ):
        print("[agent] cannot start without Chrome CDP")
        return

    _write_pid()
    agent = Agent(
        relay_url=config["relay_url"],
        api_key=config["api_key"],
        cdp_host=config["cdp_host"],
        cdp_port=config["cdp_port"],
        profile=config["profile"],
        headless=config.get("chrome_headless", False),
    )

    import atexit
    import logging

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
        _remove_pid()
        loop.close()
        log.info("[bridge] stopped")


def _start_detached(config: dict):
    """Start the bridge as a detached background process."""
    script_path = os.path.abspath(__file__)
    cmd = [
        sys.executable,
        script_path,
        "start",
        "--relay",
        config["relay_url"],
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

        for _ in range(40):
            time.sleep(0.25)
            if _is_agent_running():
                pid = _read_pid()
                print(f"[agent] started in daemon mode (PID {pid})")
                print(f"[agent] logs: {log_path}")
                return

        print("[agent] daemon launch requested, but bridge PID was not confirmed yet")
        print(f"[agent] check logs: {log_path}")
    finally:
        log_fp.close()


def cmd_status():
    """Show agent connection state."""
    if _is_agent_running():
        pid = _read_pid()
        print(json.dumps({"status": "running", "pid": pid}))
    else:
        print(json.dumps({"status": "stopped"}))


def cmd_stop():
    """Stop the running agent."""
    pid = _read_pid()
    if pid is None:
        print("[agent] not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"[agent] sent SIGTERM to PID {pid}")
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
    _remove_pid()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    config = _load_config()
    args = sys.argv[1:]
    config = _parse_args(args, config)

    # Extract command (first non-flag argument)
    cmd = None
    for a in args:
        if not a.startswith("--"):
            cmd = a
            break

    if cmd == "start":
        cmd_start(config)
    elif cmd == "status":
        cmd_status()
    elif cmd == "stop":
        cmd_stop()
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
