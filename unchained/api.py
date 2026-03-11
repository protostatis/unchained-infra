"""Unchained REST API — HTTP endpoints for the orchestrator and external clients.

Runs as part of the relay process. Provides request-response semantics
on top of the WebSocket tunnel — the orchestrator calls REST endpoints,
and the API layer translates them into tunnel messages.

Endpoints:
    GET  /api/agents                     → list connected agents
    GET  /api/agents/<id>/tabs           → list Chrome tabs
    POST /api/agents/<id>/tabs           → create a new Chrome tab
    DELETE /api/agents/<id>/tabs/<tab>   → close a Chrome tab
    POST /api/agents/<id>/ddm            → run DDM
    POST /api/agents/<id>/intel          → run page_intel
    POST /api/agents/<id>/cdp            → raw CDP command
    POST /api/agents/<id>/js             → execute JavaScript
    POST /api/agents/<id>/navigate       → navigate to URL
    POST /api/agents/<id>/click          → click at coordinates
    POST /api/agents/<id>/type           → type text
    POST /api/agents/<id>/screenshot     → capture screenshot
    POST /api/task                       → run orchestrator task

Usage:
    Integrated into relay — starts automatically on relay port.
    Or standalone: uv run api.py --port 8080 --relay-port 8765
"""

import asyncio
import json
import os
import sys
import urllib.parse

from aiohttp import web

from auth import Auth
import cloud_tools
from rate_limit import SlidingWindowRateLimiter

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def _get_bearer_token(request: web.Request) -> str | None:
    """Extract Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


_DEFAULT_TAB_PATH = "/tab"


def _configured_public_base_url() -> str:
    base_url = (
        os.environ.get("UNCHAINED_PUBLIC_BASE_URL", "").strip()
        or os.environ.get("UNCHAINED_API_URL", "").strip()
    )
    return base_url.rstrip("/")


def _hostname_from_host(host: str) -> str:
    return host.split(":", 1)[0].strip().lower()


def _default_new_tab_url(request: web.Request) -> str:
    configured = _configured_public_base_url()
    if configured:
        return f"{configured}{_DEFAULT_TAB_PATH}"

    host = (getattr(request, "host", "") or "").strip()
    hostname = _hostname_from_host(host)
    if hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return f"http://{hostname}:8080{_DEFAULT_TAB_PATH}"
    if hostname == "unchainedsky.com" or hostname.endswith(".unchainedsky.com"):
        return f"https://{host}{_DEFAULT_TAB_PATH}"
    return f"https://api.unchainedsky.com{_DEFAULT_TAB_PATH}"


# ---------------------------------------------------------------------------
# API Server
# ---------------------------------------------------------------------------
class API:
    """REST API server backed by a Relay instance."""

    def __init__(self, relay, auth: Auth | None = None):
        """Initialize API with a Relay instance.

        Args:
            relay: Relay instance (provides agent connections and http_proxy).
            auth: Auth instance. If None, uses relay's auth.
        """
        self.relay = relay
        self.auth = auth or relay.auth
        self.rate_limiter = SlidingWindowRateLimiter()
        self.rate_window_s = int(os.environ.get("UNCHAINED_API_RATE_WINDOW_S", "60"))
        self.auth_limit = int(os.environ.get("UNCHAINED_API_RATE_LIMIT", "120"))
        self.tool_limit = int(os.environ.get("UNCHAINED_API_TOOL_RATE_LIMIT", "90"))
        self.js_limit = int(os.environ.get("UNCHAINED_API_JS_RATE_LIMIT", "30"))
        self.cdp_limit = int(os.environ.get("UNCHAINED_API_CDP_RATE_LIMIT", "30"))

    def _check_auth(self, request: web.Request) -> dict | web.Response:
        """Validate API key from request. Returns user info or error response."""
        token = _get_bearer_token(request)
        if not token:
            return web.json_response(
                {"error": "Missing Authorization header"},
                status=401,
            )
        info = self.auth.validate_key(token)
        if info is None:
            return web.json_response(
                {"error": "Invalid API key"},
                status=401,
            )
        return info

    def _check_agent_access(self, request: web.Request,
                            agent_id: str) -> dict | web.Response:
        """Validate auth and agent ownership. Returns user info or error."""
        result = self._check_auth(request)
        if isinstance(result, web.Response):
            return result
        user_id = result["user_id"]
        if not self.relay.agent_belongs_to_user(agent_id, user_id):
            return web.json_response(
                {"error": f"Agent {agent_id} not found or not owned by you"},
                status=404,
            )
        return result

    def _rate_limit_response(self, retry_after: int, scope: str) -> web.Response:
        return web.json_response(
            {
                "error": f"Rate limit exceeded for {scope}",
                "retry_after": retry_after,
            },
            status=429,
            headers={"Retry-After": str(retry_after)},
        )

    def _check_rate_limit(self, request: web.Request, bucket: str, limit: int) -> web.Response | None:
        token = _get_bearer_token(request) or "anonymous"
        allowed, retry_after = self.rate_limiter.allow(
            f"{bucket}:{token}",
            limit,
            self.rate_window_s,
        )
        if allowed:
            return None
        return self._rate_limit_response(retry_after, bucket)

    # --- Handlers ---

    async def handle_list_agents(self, request: web.Request) -> web.Response:
        """GET /api/agents — list connected agents for this user."""
        result = self._check_auth(request)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "agents", self.auth_limit)
        if limited is not None:
            return limited
        agents = self.relay.get_agents_for_user(result["user_id"])
        return web.json_response({"agents": agents})

    async def handle_list_tabs(self, request: web.Request) -> web.Response:
        """GET /api/agents/<id>/tabs — list Chrome tabs."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "tabs", self.tool_limit)
        if limited is not None:
            return limited

        resp = await self.relay.http_proxy(agent_id, "GET", "/json")
        return web.json_response(resp["body"], status=resp["status"])

    async def handle_create_tab(self, request: web.Request) -> web.Response:
        """POST /api/agents/{id}/tabs — create a new Chrome tab (internal)."""
        agent_id = request.match_info["agent_id"]
        auth_result = self._check_agent_access(request, agent_id)
        if isinstance(auth_result, web.Response):
            return auth_result
        limited = self._check_rate_limit(request, "tabs-write", self.tool_limit)
        if limited is not None:
            return limited
        body = await request.json() if request.can_read_body else {}
        url = body.get("url") or _default_new_tab_url(request)
        encoded = urllib.parse.quote(url, safe=':/?#[]@!$&\'()*+,;=-._~')
        resp = await self.relay.http_proxy(agent_id, "PUT", f"/json/new?{encoded}")
        return web.json_response(resp["body"], status=resp["status"])

    async def handle_close_tab(self, request: web.Request) -> web.Response:
        """DELETE /api/agents/{id}/tabs/{tab_id} — close a Chrome tab (internal)."""
        agent_id = request.match_info["agent_id"]
        tab_id = request.match_info["tab_id"]
        auth_result = self._check_agent_access(request, agent_id)
        if isinstance(auth_result, web.Response):
            return auth_result
        limited = self._check_rate_limit(request, "tabs-write", self.tool_limit)
        if limited is not None:
            return limited
        resp = await self.relay.http_proxy(agent_id, "PUT", f"/json/close/{tab_id}")
        return web.json_response(resp["body"], status=resp["status"])

    async def handle_ddm(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/ddm — run DDM."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "ddm", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        flags = body.get("flags", ["--llm-2pass", "--cols", "60"])

        output = await cloud_tools.run_ddm(agent_id, tab_id, flags)
        return web.json_response({"output": output})

    async def handle_intel(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/intel — run page_intel."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "intel", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        flags = body.get("flags", ["--probe"])

        output = await cloud_tools.run_intel(agent_id, tab_id, flags)
        return web.json_response({"output": output})

    async def handle_cdp(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/cdp — raw CDP command."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "cdp", self.cdp_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        method = body.get("method")
        params = body.get("params", {})

        if not method:
            return web.json_response(
                {"error": "Missing 'method' field"},
                status=400,
            )

        cdp_result = await cloud_tools.run_cdp_command(
            agent_id, tab_id, method, params)
        return web.json_response({"result": cdp_result})

    async def handle_js(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/js — execute JavaScript."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "js", self.js_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        expression = body.get("expression")

        if not expression:
            return web.json_response(
                {"error": "Missing 'expression' field"},
                status=400,
            )

        output = await cloud_tools.run_js(agent_id, tab_id, expression)
        return web.json_response({"output": output})

    async def handle_navigate(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/navigate — navigate to URL."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "navigate", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        url = body.get("url")

        if not url:
            return web.json_response(
                {"error": "Missing 'url' field"},
                status=400,
            )

        output = await cloud_tools.navigate(agent_id, tab_id, url)
        return web.json_response({"output": output})

    async def handle_click(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/click — click at coordinates."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "click", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        x = body.get("x")
        y = body.get("y")

        if x is None or y is None:
            return web.json_response(
                {"error": "Missing 'x' and/or 'y' fields"},
                status=400,
            )

        output = await cloud_tools.click(agent_id, tab_id, int(x), int(y))
        return web.json_response({"output": output})

    async def handle_type(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/type — type text."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "type", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        tab_id = body.get("tab_id", "auto")
        text = body.get("text")

        if not text:
            return web.json_response(
                {"error": "Missing 'text' field"},
                status=400,
            )

        output = await cloud_tools.type_text(agent_id, tab_id, text)
        return web.json_response({"output": output})

    async def handle_screenshot(self, request: web.Request) -> web.Response:
        """POST /api/agents/<id>/screenshot — capture screenshot."""
        agent_id = request.match_info["agent_id"]
        result = self._check_agent_access(request, agent_id)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "screenshot", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json() if request.can_read_body else {}
        tab_id = body.get("tab_id", "auto")

        data = await cloud_tools.screenshot(agent_id, tab_id)
        return web.json_response({"image": data, "format": "png", "encoding": "base64"})

    async def handle_task(self, request: web.Request) -> web.Response:
        """POST /api/task — run an orchestrator task."""
        result = self._check_auth(request)
        if isinstance(result, web.Response):
            return result
        limited = self._check_rate_limit(request, "task", self.tool_limit)
        if limited is not None:
            return limited

        body = await request.json()
        agent_id = body.get("agent_id")
        task = body.get("task")

        if not agent_id or not task:
            return web.json_response(
                {"error": "Missing 'agent_id' and/or 'task' fields"},
                status=400,
            )

        if not self.relay.agent_belongs_to_user(agent_id, result["user_id"]):
            return web.json_response(
                {"error": f"Agent {agent_id} not found or not owned by you"},
                status=404,
            )

        try:
            from orchestrator import Orchestrator
            orch = Orchestrator(agent_id=agent_id, relay_url="http://127.0.0.1:8765")
            output = await orch.run(task)
            return web.json_response({"output": output})
        except ImportError:
            return web.json_response(
                {"error": "Orchestrator not available (missing anthropic dependency)"},
                status=503,
            )

    # --- App factory ---

    def create_app(self) -> web.Application:
        """Create the aiohttp application with all routes."""
        app = web.Application()
        app.router.add_get("/api/agents", self.handle_list_agents)
        app.router.add_get("/api/agents/{agent_id}/tabs", self.handle_list_tabs)
        app.router.add_post("/api/agents/{agent_id}/tabs", self.handle_create_tab)
        app.router.add_delete("/api/agents/{agent_id}/tabs/{tab_id}", self.handle_close_tab)
        app.router.add_post("/api/agents/{agent_id}/ddm", self.handle_ddm)
        app.router.add_post("/api/agents/{agent_id}/intel", self.handle_intel)
        app.router.add_post("/api/agents/{agent_id}/cdp", self.handle_cdp)
        app.router.add_post("/api/agents/{agent_id}/js", self.handle_js)
        app.router.add_post("/api/agents/{agent_id}/navigate", self.handle_navigate)
        app.router.add_post("/api/agents/{agent_id}/click", self.handle_click)
        app.router.add_post("/api/agents/{agent_id}/type", self.handle_type)
        app.router.add_post("/api/agents/{agent_id}/screenshot", self.handle_screenshot)
        app.router.add_post("/api/task", self.handle_task)
        return app


# ---------------------------------------------------------------------------
# Standalone entry point (for development/testing)
# ---------------------------------------------------------------------------
def main():
    """Run API server standalone (connects to existing relay)."""
    port = 8080
    relay_port = 8765
    db_path = None

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--relay-port" and i + 1 < len(args):
            relay_port = int(args[i + 1])
            i += 2
        elif args[i] == "--db" and i + 1 < len(args):
            db_path = args[i + 1]
            i += 2
        elif args[i] in ("--help", "-h"):
            print("""Usage: uv run api.py [options]

Options:
    --port <port>         API port (default: 8080)
    --relay-port <port>   Relay WebSocket port (default: 8765)
    --db <path>           Auth database path
""")
            return
        else:
            i += 1

    # Import relay to create a connected instance
    from relay import Relay
    relay = Relay(port=relay_port, db_path=db_path)
    api = API(relay)
    app = api.create_app()
    print(f"[api] Starting standalone API on port {port}")
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
