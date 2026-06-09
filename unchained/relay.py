"""Unchained Relay — bridges agents and clients for remote CDP access.

The relay accepts agent connections (tunnels from user machines) and client
connections (from the orchestrator). It routes CDP messages between them.

Agent connects to:   ws://relay:8765/tunnel
Client connects to:  ws://relay:8765/cdp/<agent_id>/<tab_id>

REST API (Phase 3):
    GET  /api/agents                     → list connected agents
    GET  /api/agents/<id>/tabs           → list tabs
    POST /api/agents/<id>/ddm            → run DDM
    POST /api/agents/<id>/intel          → run intel
    POST /api/agents/<id>/cdp            → raw CDP command
    POST /api/agents/<id>/js             → execute JS

Usage:
    cd unchained/
    uv run relay.py                          # Start on 127.0.0.1:8765
    uv run relay.py --host 0.0.0.0           # Bind to all interfaces
    uv run relay.py --port 9000              # Custom port
    uv run relay.py --db /path/to/auth.db    # Custom auth DB path
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import uuid

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.http11 import Response

from auth import Auth
from rate_limit import SlidingWindowRateLimiter

# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------
class Relay:
    """WebSocket relay bridging agents and clients."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 db_path: str | None = None):
        self.host = host
        self.port = port
        self.auth = Auth(db_path)
        self.agents: dict[str, ServerConnection] = {}  # agent_id → ws
        self.agent_users: dict[str, str] = {}  # agent_id → user_id
        self.agent_profiles: dict[str, str] = {}  # agent_id → profile name
        self._next_channel: dict[str, int] = {}  # agent_id → channel counter
        # client_ws → (agent_id, channel_id) for routing replies
        self.clients: dict[ServerConnection, tuple[str, int]] = {}
        # Pending HTTP proxy requests: req_id → asyncio.Future
        self._pending_http: dict[str, asyncio.Future] = {}
        self.shared_token = (
            os.environ.get("RELAY_SHARED_TOKEN")
            or os.environ.get("PRIVATE_CORE_TOKEN", "")
        ).strip()
        self.rate_limiter = SlidingWindowRateLimiter()
        # Cache valid API keys for 60s so per-message rate checks skip SQLite.
        self._api_key_cache: dict[str, float] = {}  # token → monotonic expiry
        self._API_KEY_CACHE_TTL = 60.0
        self.rate_window_s = int(os.environ.get("RELAY_RATE_WINDOW_S", "60"))
        self.http_limit = int(os.environ.get("RELAY_HTTP_RATE_LIMIT", "120"))
        self.cdp_connect_limit = int(os.environ.get("RELAY_CDP_CONNECT_RATE_LIMIT", "30"))
        self.cdp_message_limit = int(os.environ.get("RELAY_CDP_MESSAGE_RATE_LIMIT", "600"))
        self.internal_http_limit = int(os.environ.get("RELAY_INTERNAL_HTTP_RATE_LIMIT", "1200"))
        self.internal_cdp_connect_limit = int(os.environ.get("RELAY_INTERNAL_CDP_CONNECT_RATE_LIMIT", "300"))
        self.internal_cdp_message_limit = int(os.environ.get("RELAY_INTERNAL_CDP_MESSAGE_RATE_LIMIT", "6000"))

    def _json_response(self, status: int, reason: str, payload: dict | list) -> Response:
        body = json.dumps(payload).encode()
        return Response(status, reason, Headers([
            ("Content-Type", "application/json"),
        ]), body)

    def _is_websocket_upgrade(self, headers: Headers | None) -> bool:
        return (headers.get("Upgrade", "") if headers else "").lower() == "websocket"

    def _get_bearer_token(self, headers: Headers | None) -> str:
        auth_header = headers.get("Authorization", "") if headers else ""
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return ""

    def _authorize_headers(self, headers: Headers | None,
                           agent_id: str | None = None) -> tuple[dict | None, bool, str | None]:
        token = self._get_bearer_token(headers)
        if not token:
            return None, False, "missing"

        if self.shared_token and secrets.compare_digest(token, self.shared_token):
            return {"user_id": "internal"}, True, None

        info = self.auth.validate_key(token)
        if info is None:
            return None, False, "invalid"

        if agent_id and not self.agent_belongs_to_user(agent_id, info["user_id"]):
            return None, False, "forbidden"

        return info, False, None

    def _auth_error_response(self, reason: str, agent_id: str | None = None) -> Response:
        if reason == "forbidden" and agent_id:
            return self._json_response(
                404,
                "Not Found",
                {"error": f"Agent {agent_id} not found or not owned by you"},
            )
        if reason == "missing":
            return self._json_response(401, "Unauthorized", {"error": "Missing Authorization header"})
        return self._json_response(401, "Unauthorized", {"error": "Invalid API key"})

    def _authorize_cdp_client(self, ws: ServerConnection, agent_id: str,
                              query: dict[str, list[str]]) -> str | None:
        relay_token = (query.get("relay_token") or [""])[0].strip()
        if relay_token:
            if self.shared_token and secrets.compare_digest(relay_token, self.shared_token):
                return None
            return "Invalid relay token"

        _, _, reason = self._authorize_headers(
            ws.request.headers if ws.request else None,
            agent_id,
        )
        if reason == "missing":
            return "Missing Authorization header"
        if reason == "forbidden":
            return f"Agent {agent_id} not found or not owned by you"
        if reason is not None:
            return "Invalid API key"
        return None

    def _is_valid_api_key_cached(self, token: str) -> bool:
        """Return True if token is a valid API key. Results are cached to
        avoid hitting SQLite on every CDP message."""
        if not token:
            return False
        now = time.monotonic()
        expiry = self._api_key_cache.get(token)
        if expiry is not None:
            if now < expiry:
                return True
            del self._api_key_cache[token]
        if self.auth.validate_key(token) is not None:
            self._api_key_cache[token] = now + self._API_KEY_CACHE_TTL
            return True
        return False

    def _rate_limit_key_from_headers(self, headers: Headers | None, prefix: str) -> tuple[str, bool]:
        token = self._get_bearer_token(headers)
        if self.shared_token and token and secrets.compare_digest(token, self.shared_token):
            return f"{prefix}:internal", True
        # Valid API key holders get internal-tier rate limits. They are
        # approved users running their own agents — each cdp_tool.py
        # subprocess opens a fresh WebSocket, so external limits (30
        # connects/min) are too low for normal agent usage.
        if self._is_valid_api_key_cached(token):
            return f"{prefix}:{token}", True
        return f"{prefix}:{token or 'anonymous'}", False

    def _rate_limit_key_from_cdp(self, ws: ServerConnection,
                                 query: dict[str, list[str]], prefix: str) -> tuple[str, bool]:
        relay_token = (query.get("relay_token") or [""])[0].strip()
        if self.shared_token and relay_token and secrets.compare_digest(relay_token, self.shared_token):
            return f"{prefix}:internal", True
        return self._rate_limit_key_from_headers(ws.request.headers if ws.request else None, prefix)

    def _check_rate_limit(self, key: str, limit: int) -> tuple[bool, int]:
        return self.rate_limiter.allow(key, limit, self.rate_window_s)

    def _rate_limit_response(self, retry_after: int, scope: str) -> Response:
        return self._json_response(
            429,
            "Too Many Requests",
            {"error": f"Rate limit exceeded for {scope}", "retry_after": retry_after},
        )

    async def start(self):
        """Start the relay server."""
        print(f"[relay] listening on {self.host}:{self.port}")
        print(f"[relay] agent endpoint: ws://{self.host}:{self.port}/tunnel")
        print(f"[relay] client endpoint: ws://{self.host}:{self.port}/cdp/<agent_id>/<tab_id>")
        async with websockets.serve(self._route, self.host, self.port,
                                    max_size=50 * 1024 * 1024,
                                    ping_interval=120,  # WS-level safety net; app heartbeat is primary
                                    process_request=self._process_request):
            await asyncio.Future()  # run forever

    async def _process_request(self, connection, request):
        """Handle HTTP API requests before WebSocket upgrade."""
        del connection
        full_path = request.path
        path = urllib.parse.urlsplit(full_path).path
        if path == "/health" and not self._is_websocket_upgrade(request.headers):
            return self._json_response(200, "OK", {"status": "ok", "agents": len(self.agents)})
        # GET /api/agents — list connected agents
        if path == "/api/agents":
            auth_info, internal, reason = self._authorize_headers(request.headers)
            if reason is not None:
                return self._auth_error_response(reason)
            key, is_internal = self._rate_limit_key_from_headers(request.headers, "http-agents")
            allowed, retry_after = self._check_rate_limit(
                key,
                self.internal_http_limit if (internal or is_internal) else self.http_limit,
            )
            if not allowed:
                return self._rate_limit_response(retry_after, "relay_http")
            agents = self.get_agents() if internal else self.get_agents_for_user(auth_info["user_id"])
            return self._json_response(200, "OK", agents)
        # GET /api/agents/<id>/profiles — proxy profile listing to agent
        if path.startswith("/api/agents/") and path.endswith("/profiles"):
            parts = path.split("/")
            if len(parts) == 5:
                agent_id = parts[3]
                _, _, reason = self._authorize_headers(request.headers, agent_id)
                if reason is not None:
                    return self._auth_error_response(reason, agent_id)
                key, internal = self._rate_limit_key_from_headers(request.headers, "http-profiles")
                allowed, retry_after = self._check_rate_limit(
                    key,
                    self.internal_http_limit if internal else self.http_limit,
                )
                if not allowed:
                    return self._rate_limit_response(retry_after, "relay_http")
                result = await self.http_proxy(agent_id, "GET", "/profiles")
                return self._json_response(result.get("status", 200), "OK", result.get("body", {}))
        # GET /api/agents/<id>/provision-launch?profile_path=<encoded> — launch provision Chrome
        if path.startswith("/api/agents/") and "/provision-launch" in path:
            parts = path.split("/")
            if len(parts) >= 4:
                agent_id = parts[3]
                _, _, reason = self._authorize_headers(request.headers, agent_id)
                if reason is not None:
                    return self._auth_error_response(reason, agent_id)
                key, internal = self._rate_limit_key_from_headers(request.headers, "http-provision")
                allowed, retry_after = self._check_rate_limit(
                    key,
                    self.internal_http_limit if internal else self.http_limit,
                )
                if not allowed:
                    return self._rate_limit_response(retry_after, "relay_http")
                # Forward the full path after the agent_id portion
                bridge_path = "/" + "/".join(parts[4:])
                query = urllib.parse.urlsplit(full_path).query
                if query:
                    bridge_path = f"{bridge_path}?{query}"
                result = await self.http_proxy(agent_id, "POST", bridge_path, timeout=60)
                return self._json_response(result.get("status", 200), "OK", result.get("body", {}))
        # GET /api/agents/<id>/provision-status — list provisioned tabs
        if path.startswith("/api/agents/") and path.endswith("/provision-status"):
            parts = path.split("/")
            if len(parts) == 5:
                agent_id = parts[3]
                _, _, reason = self._authorize_headers(request.headers, agent_id)
                if reason is not None:
                    return self._auth_error_response(reason, agent_id)
                key, internal = self._rate_limit_key_from_headers(request.headers, "http-provision")
                allowed, retry_after = self._check_rate_limit(
                    key,
                    self.internal_http_limit if internal else self.http_limit,
                )
                if not allowed:
                    return self._rate_limit_response(retry_after, "relay_http")
                result = await self.http_proxy(agent_id, "GET", "/provision-status")
                return self._json_response(result.get("status", 200), "OK", result.get("body", {}))
        # GET /api/agents/<id>/provision-cleanup — clean up provision Chrome
        if path.startswith("/api/agents/") and "/provision-cleanup" in path:
            parts = path.split("/")
            if len(parts) >= 5:
                agent_id = parts[3]
                _, _, reason = self._authorize_headers(request.headers, agent_id)
                if reason is not None:
                    return self._auth_error_response(reason, agent_id)
                key, internal = self._rate_limit_key_from_headers(request.headers, "http-provision")
                allowed, retry_after = self._check_rate_limit(
                    key,
                    self.internal_http_limit if internal else self.http_limit,
                )
                if not allowed:
                    return self._rate_limit_response(retry_after, "relay_http")
                bridge_path = "/provision-cleanup"
                query = urllib.parse.urlsplit(full_path).query
                if query:
                    bridge_path = f"{bridge_path}?{query}"
                result = await self.http_proxy(agent_id, "POST", bridge_path)
                return self._json_response(result.get("status", 200), "OK", result.get("body", {}))
        # GET /api/agents/<id>/http/<method>/<chrome_path> — proxy HTTP to agent's Chrome
        # Example: /api/agents/claude-abc/http/PUT/json/new?about:blank
        if path.startswith("/api/agents/") and "/http/" in path:
            # Split: ['', 'api', 'agents', '<id>', 'http', '<method>', '<chrome_path...>']
            parts = path.split("/", 6)
            if len(parts) >= 7:
                agent_id = parts[3]
                _, _, reason = self._authorize_headers(request.headers, agent_id)
                if reason is not None:
                    return self._auth_error_response(reason, agent_id)
                key, internal = self._rate_limit_key_from_headers(request.headers, "http-proxy")
                allowed, retry_after = self._check_rate_limit(
                    key,
                    self.internal_http_limit if internal else self.http_limit,
                )
                if not allowed:
                    return self._rate_limit_response(retry_after, "relay_http")
                method = parts[5]
                chrome_path = "/" + parts[6]  # restore leading /
                query = urllib.parse.urlsplit(full_path).query
                if query:
                    chrome_path = f"{chrome_path}?{query}"
                result = await self.http_proxy(agent_id, method, chrome_path)
                return self._json_response(result.get("status", 200), "OK", result)
        return None  # proceed with WebSocket upgrade

    async def _route(self, ws: ServerConnection):
        """Route incoming WebSocket connection based on path."""
        raw_path = ws.request.path if ws.request else "/"
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path
        if path == "/tunnel":
            await self._handle_agent(ws)
        elif path.startswith("/cdp/"):
            await self._handle_client(ws, path, urllib.parse.parse_qs(parsed.query))
        elif path == "/health":
            await ws.send(json.dumps({"status": "ok", "agents": len(self.agents)}))
            await ws.close()
        else:
            await ws.close(4000, f"Unknown path: {path}")

    # --- Agent handling ---

    async def _handle_agent(self, ws: ServerConnection):
        """Handle an agent tunnel connection."""
        agent_id = None
        try:
            # Wait for auth message
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "auth":
                await ws.send(json.dumps({
                    "type": "auth_fail",
                    "error": "Expected auth message",
                }))
                return

            api_key = msg.get("api_key", "")
            key_info = self.auth.validate_key(api_key)
            if key_info is None:
                await ws.send(json.dumps({
                    "type": "auth_fail",
                    "error": "Invalid API key",
                }))
                return

            key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:8]
            profile = msg.get("profile", "default")
            if not re.match(r'^[a-zA-Z0-9_-]{1,32}$', profile):
                profile = "default"
            if api_key.startswith("uc_headless_"):
                agent_id = f"headless-{key_hash}"
            elif profile and profile != "default":
                agent_id = f"claude-{key_hash}-{profile}"
            else:
                agent_id = f"claude-{key_hash}"
            self.agents[agent_id] = ws
            self.agent_users[agent_id] = key_info["user_id"]
            self.agent_profiles[agent_id] = profile
            self._next_channel[agent_id] = 1
            print(f"[relay] agent {agent_id} connected (user={key_info['user_id']}, profile={profile}, key={api_key[:12]}...)")

            await ws.send(json.dumps({
                "type": "auth_ok",
                "agent_id": agent_id,
            }))

            # Message loop: forward agent responses to the right client
            async for raw in ws:
                msg = json.loads(raw)
                await self._handle_agent_message(agent_id, msg)

        except asyncio.TimeoutError:
            print(f"[relay] agent {agent_id} timed out")
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"[relay] agent {agent_id} connection closed: code={exc.code} reason={exc.reason!r}")
        finally:
            if agent_id:
                # Guard against race: if the agent reconnected quickly, a new
                # handler already registered a fresh ws under the same agent_id.
                # Only clean up if this ws is still the registered one.
                if self.agents.get(agent_id) is ws:
                    self.agents.pop(agent_id, None)
                    self.agent_users.pop(agent_id, None)
                    self.agent_profiles.pop(agent_id, None)
                    self._next_channel.pop(agent_id, None)
                    # Close all clients connected to this agent
                    to_close = [c for c, (aid, _) in self.clients.items()
                                if aid == agent_id]
                    for client_ws in to_close:
                        self.clients.pop(client_ws, None)
                        try:
                            await client_ws.close(4001, "Agent disconnected")
                        except Exception:
                            pass
                    print(f"[relay] agent {agent_id} disconnected")

    async def _handle_agent_message(self, agent_id: str, msg: dict):
        """Process a message from an agent."""
        t = msg.get("type", "")
        if t == "ping":
            # Respond to heartbeat
            agent_ws = self.agents.get(agent_id)
            if agent_ws:
                try:
                    await agent_ws.send(json.dumps({
                        "type": "pong",
                        "ts": msg.get("ts", time.time()),
                    }))
                except Exception:
                    pass  # Connection closing — don't kill the message loop
        elif t == "http_response":
            # Resolve pending HTTP proxy future
            req_id = msg.get("req_id")
            fut = self._pending_http.pop(req_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
            # Also forward to client if one is waiting on this channel
            channel = msg.get("channel", msg.get("req_id", 0))
            for client_ws, (aid, ch) in list(self.clients.items()):
                if aid == agent_id and ch == channel:
                    try:
                        await client_ws.send(json.dumps(msg))
                    except Exception:
                        pass
                    break
        elif t in ("ws_recv", "ws_opened", "ws_error", "ws_closed"):
            # Forward to the client that owns this channel
            channel = msg.get("channel", 0)
            for client_ws, (aid, ch) in list(self.clients.items()):
                if aid == agent_id and ch == channel:
                    try:
                        await client_ws.send(json.dumps(msg))
                    except Exception:
                        pass
                    break

    # --- Client handling ---

    async def _handle_client(self, ws: ServerConnection, path: str,
                             query: dict[str, list[str]]):
        """Handle a CDP client connection.

        Path format: /cdp/<agent_id>/<tab_id>
        The client gets a virtual CDP WebSocket to the agent's Chrome tab.
        """
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            await ws.close(4000, "Expected /cdp/<agent_id>/<tab_id>")
            return

        _, agent_id, tab_id = parts

        auth_error = self._authorize_cdp_client(ws, agent_id, query)
        if auth_error is not None:
            await ws.close(4003, auth_error)
            return
        rate_key, internal = self._rate_limit_key_from_cdp(ws, query, "cdp-connect")
        allowed, retry_after = self._check_rate_limit(
            rate_key,
            self.internal_cdp_connect_limit if internal else self.cdp_connect_limit,
        )
        if not allowed:
            await ws.close(4008, f"Rate limit exceeded. Retry after {retry_after}s")
            return

        agent_ws = self.agents.get(agent_id)
        if not agent_ws:
            await ws.close(4004, f"Agent {agent_id} not connected")
            return

        # Allocate a channel
        channel = self._next_channel.get(agent_id, 1)
        self._next_channel[agent_id] = channel + 1
        self.clients[ws] = (agent_id, channel)

        print(f"[relay] client → agent {agent_id} tab {tab_id} (channel {channel})")

        try:
            # Ask agent to open a WebSocket to the tab
            await agent_ws.send(json.dumps({
                "type": "ws_open",
                "channel": channel,
                "tab_id": tab_id,
            }))

            # Wait for ws_opened or ws_error
            opened = False
            async for raw in ws:
                msg_rate_key = rate_key.replace("cdp-connect:", "cdp-msg:", 1)
                allowed, retry_after = self._check_rate_limit(
                    msg_rate_key,
                    self.internal_cdp_message_limit if internal else self.cdp_message_limit,
                )
                if not allowed:
                    await ws.close(4008, f"Rate limit exceeded. Retry after {retry_after}s")
                    break
                # Check for ws_opened response (comes via agent → relay → here)
                # But actually, ws_opened goes to _handle_agent_message which
                # forwards to this client. So we receive it normally.
                msg = json.loads(raw)

                if not opened:
                    # First message might be confirmation or it might be
                    # a CDP message from the client. Actually, the opened
                    # confirmation goes to the client via _handle_agent_message.
                    # The client sends CDP messages which we forward to agent.
                    pass

                # Forward client's CDP messages to agent
                await agent_ws.send(json.dumps({
                    "type": "ws_send",
                    "channel": channel,
                    "data": msg,
                }))

        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.pop(ws, None)
            # Tell agent to close the channel
            agent_ws = self.agents.get(agent_id)
            if agent_ws:
                try:
                    await agent_ws.send(json.dumps({
                        "type": "ws_close",
                        "channel": channel,
                    }))
                except Exception:
                    pass
            print(f"[relay] client disconnected (agent {agent_id}, channel {channel})")

    # --- HTTP API proxy ---

    async def http_proxy(self, agent_id: str, method: str, path: str,
                         timeout: float = 10) -> dict:
        """Send an HTTP proxy request to an agent and wait for response.

        Returns the agent's response dict with 'status' and 'body' fields.
        """
        agent_ws = self.agents.get(agent_id)
        if not agent_ws:
            return {"status": 404, "body": {"error": f"Agent {agent_id} not found"}}

        req_id = f"r-{uuid.uuid4().hex[:8]}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_http[req_id] = fut

        await agent_ws.send(json.dumps({
            "type": "http",
            "req_id": req_id,
            "method": method,
            "path": path,
        }))

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return {
                "status": result.get("status", 200),
                "body": result.get("body", {}),
            }
        except asyncio.TimeoutError:
            self._pending_http.pop(req_id, None)
            return {"status": 504, "body": {"error": "Agent did not respond in time"}}

    # --- Public accessors for API layer ---

    def get_agents(self) -> list[dict]:
        """List all connected agents."""
        return [
            {"agent_id": aid, "user_id": self.agent_users.get(aid, ""),
             "profile": self.agent_profiles.get(aid, "default")}
            for aid in self.agents
        ]

    def get_agents_for_user(self, user_id: str) -> list[dict]:
        """List agents belonging to a specific user."""
        return [
            {"agent_id": aid, "profile": self.agent_profiles.get(aid, "default")}
            for aid, uid in self.agent_users.items()
            if uid == user_id
        ]

    def agent_belongs_to_user(self, agent_id: str, user_id: str) -> bool:
        """Check if an agent belongs to a user."""
        return self.agent_users.get(agent_id) == user_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    host = "127.0.0.1"
    port = 8765
    db_path = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i] in ("--help", "-h"):
            print("""Usage: uv run relay.py [options]

Options:
    --host <host>    Bind address (default: 127.0.0.1)
    --port <port>    Bind port (default: 8765)
    --db <path>      Auth database path (default: ~/.unchained/auth.db)
""")
            return
        else:
            i += 1

    relay = Relay(host=host, port=port, db_path=db_path)
    asyncio.run(relay.start())


if __name__ == "__main__":
    main()
