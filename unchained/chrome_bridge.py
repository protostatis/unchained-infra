"""Unchained Agent — tunnels local Chrome CDP to a remote relay server.

The agent discovers the local Chrome CDP endpoint, connects to a relay
server via WebSocket, and relays CDP messages bidirectionally. This lets
a remote orchestrator control the user's browser through their own
credentials, cookies, and IP.

Usage:
    cd unchained/
    uv run chrome_bridge.py start                          # Connect to default relay
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
    else:
        return None
    return p if os.path.isdir(p) else None


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
        # Provision Chrome: temporary Chrome launched with a user-selected profile
        self._prov_chrome = None  # {port, process, temp_dir, profile_dir_name}

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
            except (ConnectionError, OSError,
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
        async with websockets.connect(self.relay_url,
                                      max_size=50 * 1024 * 1024) as ws:
            self.ws = ws
            self._backoff = 1  # reset on successful connect
            await self._authenticate()
            print(f"[agent] authenticated as {self.agent_id}")
            if self._headless:
                self._cleanup_orphan_tabs()
            await self._message_loop()

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
        ping_task = asyncio.create_task(self._heartbeat())
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                await self._handle_message(msg)
        finally:
            ping_task.cancel()
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
        if path == "/provision-cleanup":
            await self._handle_provision_cleanup(req_id)
            return

        # Proxy /prov/* requests to the provision Chrome's port
        if path.startswith("/prov/") and self._prov_chrome:
            prov_path = path[5:]  # strip "/prov" prefix → e.g. "/json/new?..."
            prov_url = f"http://127.0.0.1:{self._prov_chrome['port']}{prov_path}"
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
        """
        # Provision Chrome routing: strip "prov-" prefix, query provision Chrome
        if tab_id.startswith("prov-") and self._prov_chrome:
            real_id = tab_id[5:]
            prov_port = self._prov_chrome["port"]
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
        if "?" in path:
            import urllib.parse
            qs = path.split("?", 1)[1]
            params = urllib.parse.parse_qs(qs)
            profile_path = params.get("profile_path", [""])[0]

        if not profile_path or not os.path.isdir(profile_path):
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 400,
                "body": {"error": f"Invalid profile_path: {profile_path}"},
            }))
            return

        # Clean up any existing provision Chrome first
        if self._prov_chrome:
            self._cleanup_prov_chrome_sync()

        # Copy profile to temp dir (same logic as signup_agent._copy_chrome_profile)
        temp_dir = os.path.join(DATA_DIR, f"prov_tmp_{os.getpid()}_{int(time.time())}")
        profile_dir_name = os.path.basename(profile_path)
        chrome_parent = os.path.dirname(profile_path)

        try:
            os.makedirs(temp_dir, exist_ok=True)

            # Copy Local State (cookie encryption keys)
            local_state = os.path.join(chrome_parent, "Local State")
            if os.path.isfile(local_state):
                shutil.copy2(local_state, os.path.join(temp_dir, "Local State"))

            # Copy the profile dir, skipping caches
            cache_dirs = {"Cache", "Code Cache", "GPUCache", "ShaderCache",
                          "Service Worker", "GrShaderCache", "DawnCache"}
            def _ignore(directory, contents):
                return [c for c in contents if c in cache_dirs]

            shutil.copytree(
                profile_path,
                os.path.join(temp_dir, profile_dir_name),
                ignore=_ignore,
            )
            print(f"[agent:prov] Copied profile {profile_dir_name} to {temp_dir}")
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
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]
        chrome_bin = next((p for p in chrome_paths if os.path.exists(p)), None)
        if not chrome_bin:
            for candidate in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
                found = shutil.which(candidate)
                if found:
                    chrome_bin = found
                    break
        if not chrome_bin:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 500,
                "body": {"error": "Chrome binary not found"},
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

        # Store state
        self._prov_chrome = {
            "port": prov_port,
            "process": proc,
            "temp_dir": temp_dir,
            "profile_dir_name": profile_dir_name,
        }

        prov_tab_id = f"prov-{first_tab_id}" if first_tab_id else "prov-auto"
        print(f"[agent:prov] Provision Chrome ready: port={prov_port}, tab={prov_tab_id}")

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"tab_id": prov_tab_id, "port": prov_port},
        }))

    async def _handle_provision_cleanup(self, req_id):
        """Kill the provision Chrome and clean up temp dir."""
        if not self._prov_chrome:
            await self.ws.send(json.dumps({
                "type": "http_response",
                "req_id": req_id,
                "status": 200,
                "body": {"status": "no_provision_chrome"},
            }))
            return

        self._cleanup_prov_chrome_sync()

        await self.ws.send(json.dumps({
            "type": "http_response",
            "req_id": req_id,
            "status": 200,
            "body": {"status": "cleaned_up"},
        }))

    def _cleanup_prov_chrome_sync(self):
        """Synchronously kill provision Chrome and clean up temp dir."""
        if not self._prov_chrome:
            return
        prov = self._prov_chrome
        self._prov_chrome = None

        # Close any prov-* channels
        prov_channels = [ch for ch in self.channels if isinstance(ch, int)]
        # Note: channels are identified by integer IDs, not tab_id strings.
        # The relay routes prov- tab IDs through normal channels, so no special
        # channel cleanup is needed here.

        # Kill Chrome process
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

        # Delete temp dir
        temp_dir = prov.get("temp_dir", "")
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                print(f"[agent:prov] Cleaned up {temp_dir}")
            except Exception as e:
                print(f"[agent:prov] Warning: failed to clean up {temp_dir}: {e}")

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
    try:
        os.kill(pid, 0)  # signal 0 = check existence
        return True
    except ProcessLookupError:
        _remove_pid()
        return False


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
    chrome_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    chrome_bin = next((p for p in chrome_paths if os.path.exists(p)), None)
    if not chrome_bin:
        for candidate in (
            "google-chrome",
            "google-chrome-stable",
            "chromium-browser",
            "chromium",
        ):
            found = shutil.which(candidate)
            if found:
                chrome_bin = found
                break
    if not chrome_bin:
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

    loop = asyncio.new_event_loop()

    def _shutdown(sig, frame):
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(agent.stop()))

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(agent.start())
    finally:
        _remove_pid()
        loop.close()
        print("[agent] stopped")


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
    --chrome-args <s>   Extra Chrome launch args string

Each profile gets its own Chrome data directory (~/.unchained/chrome_<name>/)
with separate cookies, sessions, history, and extensions.

Config file: ~/.unchained/agent.json
Env vars: UNCHAINED_RELAY_URL, UNCHAINED_API_KEY, CDP_HOST, CDP_PORT, CDP_PROFILE,
          UNCHAINED_CHROME_HEADLESS, UNCHAINED_CHROME_ARGS
""")


if __name__ == "__main__":
    main()
