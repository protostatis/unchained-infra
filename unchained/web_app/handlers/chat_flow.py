"""Chat status/history/slot handlers extracted from web.py."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from urllib.parse import urlsplit

import httpx
from aiohttp import web

from challenge_detection import detect_challenge
from domain_policy import execution_policy_for_url
from rate_limit import SlidingWindowRateLimiter

from web_app.core import get_core as _core

log = logging.getLogger(__name__)


# Chrome headless launch typically takes 2-5s; 10s gives comfortable margin.
_FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT = 10.0

def _parse_version_tuple(value: str) -> tuple[int, int, int]:
    """Parse a semver-like string into a comparable tuple."""
    try:
        parts = tuple(int(part) for part in str(value or "").strip().split("."))
    except (TypeError, ValueError):
        return (0, 0, 0)
    if len(parts) != 3:
        return (0, 0, 0)
    return parts


def _client_version_status(caps: dict | None) -> dict:
    """Summarize the connected local client package version from auth caps."""
    from agent_package import MIN_VERSION, VERSION

    caps = caps or {}
    local_version = str(caps.get("client_version", "") or "").strip()
    local_t = _parse_version_tuple(local_version) if local_version else (0, 0, 0)
    server_t = _parse_version_tuple(VERSION)
    min_t = _parse_version_tuple(MIN_VERSION)
    return {
        "client_version": local_version,
        "server_version": VERSION,
        "min_client_version": MIN_VERSION,
        "client_update_supported": bool(caps.get("remote_update")),
        "client_outdated": bool(local_version) and local_t < server_t,
        "client_update_required": bool(local_version) and local_t < min_t,
    }


_RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION = "0.3.65"
_FIRST_LOOK_SIGNAL_URL_MAX = 500
_FIRST_LOOK_SIGNAL_TEXT_MAX = 6000
_FIRST_LOOK_PREVIEW_WIDTH_DEFAULT = 960
_FIRST_LOOK_PREVIEW_HEIGHT_DEFAULT = 640
_FIRST_LOOK_PUBLIC_RATE_WINDOW_S = 60.0
_FIRST_LOOK_PREFLIGHT_LIMIT = 30
_FIRST_LOOK_SIGNAL_LIMIT = 20
_PUBLIC_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_FIRST_LOOK_PUBLIC_RATE_LIMITER = SlidingWindowRateLimiter()


def _research_desk_install_requires_update(caps: dict | None) -> dict:
    """Return whether the connected client is new enough for bootstrap install."""
    caps = caps or {}
    local_version = str(caps.get("client_version", "") or "").strip()
    local_t = _parse_version_tuple(local_version) if local_version else (0, 0, 0)
    required_t = _parse_version_tuple(_RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION)
    return {
        "client_version": local_version,
        "required_client_version": _RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION,
        "update_supported": bool(caps.get("remote_update")),
        "update_required": (not local_version) or local_t < required_t,
    }


def _normalize_first_look_public_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > _FIRST_LOOK_SIGNAL_URL_MAX:
        raise ValueError("url too long")
    if "://" not in text and _PUBLIC_HOST_RE.match(text):
        text = "https://" + text
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.hostname:
        return ""
    return parsed.geturl()


def _clip_signal_text(value: str, limit: int = _FIRST_LOOK_SIGNAL_TEXT_MAX) -> str:
    return str(value or "").strip()[:limit]


def _request_source_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        source = forwarded.split(",")[0].strip()
        if source:
            return source
    return (request.remote or "unknown").strip() or "unknown"


def _check_first_look_public_rate_limit(
    request: web.Request,
    *,
    bucket: str,
    limit: int,
) -> web.Response | None:
    allowed, retry_after = _FIRST_LOOK_PUBLIC_RATE_LIMITER.allow(
        f"{bucket}:{_request_source_ip(request)}",
        limit,
        _FIRST_LOOK_PUBLIC_RATE_WINDOW_S,
    )
    if allowed:
        return None
    return web.json_response(
        {"error": "Rate limit exceeded", "retry_after": retry_after},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


def _normalize_first_look_preview_dimension(
    params,
    name: str,
    default: int,
    *,
    min_value: int = 320,
    max_value: int = 1920,
) -> int:
    raw = str(params.get(name, default) or default).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < min_value:
        return min_value
    if value > max_value:
        return max_value
    return value


async def _resolve_first_look_preview_target(
    core,
    guest_auth: dict,
    session_id: str,
    *,
    timeout: float | None = None,
    # 300ms balances responsiveness with CPU overhead during polling.
    poll_interval: float = 0.3,
) -> tuple[str, str]:
    if timeout is None:
        timeout = _FIRST_LOOK_PREVIEW_RESOLVE_TIMEOUT
    sid = str(session_id or "").strip()
    if not sid:
        raise web.HTTPBadRequest(text="session_id required")

    # --- Ownership check ---
    # The session_id is formatted as "s-{agent_id}-{suffix}".  Accept the
    # request when EITHER the guest cookie identity owns the session OR the
    # session already exists in _session_tabs (meaning it was created through
    # an authenticated code path such as /web/chat).
    guest_agent = guest_auth.get("agent_id", "")
    prefix = f"s-{guest_agent}-"
    if not sid.startswith(prefix) and sid not in core._session_tabs:
        raise web.HTTPForbidden(text="session_id not owned by guest")

    if not core.HEADLESS_AGENT_ID:
        raise web.HTTPServiceUnavailable(text="Shared browser is not configured")
    # The tab may not be provisioned yet (headless Chrome launch takes seconds).
    # Poll until it appears or we time out.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        tab_id = str(core._session_tabs.get(sid, "") or "").strip()
        if tab_id:
            return core.HEADLESS_AGENT_ID, tab_id
        if loop.time() >= deadline:
            raise web.HTTPNotFound(text="No live preview available for this session yet")
        await asyncio.sleep(poll_interval)


async def handle_first_look_preflight(request: web.Request) -> web.Response:
    """GET /web/first-look/preflight — classify a public target before a guest run."""
    limited = _check_first_look_public_rate_limit(
        request,
        bucket="first-look-preflight",
        limit=_FIRST_LOOK_PREFLIGHT_LIMIT,
    )
    if limited is not None:
        return limited
    try:
        url = _normalize_first_look_public_url(request.query.get("url", ""))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    if not url:
        return web.json_response({"ok": True, "url": "", "policy": None})
    policy = execution_policy_for_url(url)
    return web.json_response({"ok": True, "url": url, "policy": policy.to_dict()})


async def handle_first_look_signal(request: web.Request) -> web.Response:
    """POST /web/first-look/signal — classify run text for challenge hints."""
    limited = _check_first_look_public_rate_limit(
        request,
        bucket="first-look-signal",
        limit=_FIRST_LOOK_SIGNAL_LIMIT,
    )
    if limited is not None:
        return limited
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)
    try:
        url = _normalize_first_look_public_url(body.get("url", ""))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    text = _clip_signal_text(body.get("text", ""))
    title = _clip_signal_text(body.get("title", ""), limit=256)
    html = _clip_signal_text(body.get("html", ""), limit=6000)
    challenge = detect_challenge(page_text=text, title=title, url=url, html=html)
    payload = {"ok": True, "challenge": challenge.to_dict()}
    if url:
        payload["policy"] = execution_policy_for_url(url).to_dict()
    else:
        payload["policy"] = None
    return web.json_response(payload)


async def handle_first_look_preview_ws(request: web.Request) -> web.StreamResponse:
    """GET /web/first-look/preview/ws — proxy private-core screencast for guest preview."""
    core = _core()
    sid_param = request.query.get("session_id", "")
    log.debug("request received sid=%r tabs=%s", sid_param, dict(core._session_tabs))
    guest_auth, guest_id, _ = core._first_look_guest_auth(request)
    try:
        agent_id, tab_id = await _resolve_first_look_preview_target(
            core,
            guest_auth,
            sid_param,
        )
    except web.HTTPException as exc:
        log.warning("resolve failed: %s %s", exc.status, exc.text)
        denied = web.Response(status=exc.status, text=exc.text)
        core._attach_first_look_guest_cookies(denied, request, guest_id)
        return denied
    log.debug("resolved agent=%s tab=%s", agent_id, tab_id)

    width = _normalize_first_look_preview_dimension(
        request.query,
        "width",
        _FIRST_LOOK_PREVIEW_WIDTH_DEFAULT,
    )
    height = _normalize_first_look_preview_dimension(
        request.query,
        "height",
        _FIRST_LOOK_PREVIEW_HEIGHT_DEFAULT,
        min_value=240,
    )

    import cloud_tools

    relay_host, relay_port = core._parse_relay()
    ws = web.WebSocketResponse(heartbeat=30)
    core._attach_first_look_guest_cookies(ws, request, guest_id)
    await ws.prepare(request)
    log.debug("starting screencast stream agent=%s tab=%s", agent_id, tab_id)
    try:
        async for event in cloud_tools.stream_screencast(
            agent_id,
            tab_id,
            relay_host=relay_host,
            relay_port=relay_port,
            width=width,
            height=height,
            quality=60,
            image_format="jpeg",
            every_nth_frame=2,
            max_frames=900,
            stream_timeout=120.0,
        ):
            await ws.send_json(event)
            if event.get("type") == "status":
                break
    except Exception as exc:
        log.warning("screencast error: %r", exc)
        if not ws.closed:
            try:
                await ws.send_json({"type": "status", "status": "error", "reason": "preview_unavailable"})
            except Exception:
                pass
    finally:
        if not ws.closed:
            await ws.close()
    return ws


async def check_relay_agent(agent_id: str) -> bool:
    """Quick check if an agent is connected to the relay via HTTP API."""
    core = _core()
    relay_host, relay_port = core._parse_relay()
    scheme = "https" if relay_port == 443 else "http"
    if relay_port in (443, 80):
        url = f"{scheme}://{relay_host}/api/agents"
    else:
        url = f"{scheme}://{relay_host}:{relay_port}/api/agents"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=3, headers=core._relay_auth_headers())
            if resp.is_success:
                agents = resp.json()
                return any(a.get("agent_id") == agent_id for a in agents)
    except Exception:
        pass

    # Fallback: try WS connect
    import websockets

    try:
        async with websockets.connect(
            core._relay_cdp_url(agent_id, "auto"),
            open_timeout=3,
            additional_headers=core._relay_auth_headers() or None,
        ) as ws:
            await ws.close()
            return True
    except Exception:
        return False


def resolve_chat_agent_id(auth_info: dict, model: str) -> str:
    """Return the chat agent_id for the given model + authenticated user."""
    core = _core()
    h = auth_info["key_hash"]
    if model and model.startswith("gemini"):
        return f"gemini-{h}"
    if core._is_claude_sdk_model(model):
        return f"claudesdk-{h}"
    if core._is_codex_sdk_model(model):
        return f"codexsdk-{h}"
    if core._is_codex_cli_model(model):
        # Codex CLI runs on the user's local CLI agent, same lane as Claude CLI.
        return auth_info["agent_id"]
    return auth_info["agent_id"]  # claude-{hash}


async def agent_request(agent_id: str, msg: dict, timeout: float = 10) -> dict | None:
    """Send a request to the agent WS and wait for a response."""
    core = _core()
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return None
    req_id = uuid.uuid4().hex[:8]
    msg["req_id"] = req_id
    q: asyncio.Queue = asyncio.Queue()
    core._agent_req_queues[req_id] = q
    try:
        await ws.send_json(msg)
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except (asyncio.TimeoutError, Exception):
        return None
    finally:
        core._agent_req_queues.pop(req_id, None)


async def _agent_request_after_reconnect(
    agent_id: str, msg: dict, *, timeout: float = 10, retry_delay: float = 1.0
) -> dict | None:
    """Wait through an initial reconnect window without replaying ambiguous requests."""
    core = _core()
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        await asyncio.sleep(retry_delay)
        ws = core._chat_agents.get(agent_id)
        if ws is None or ws.closed:
            return None
    return await agent_request(agent_id, dict(msg), timeout=timeout)


async def handle_chat_status(request: web.Request) -> web.Response:
    """GET /web/chat/status — check if user's agent is connected."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        if request.query.get("first_look_guest") != "1":
            return web.json_response({"error": "Not authenticated"}, status=401)
        guest_auth, guest_id, _ = core._first_look_guest_auth(request)
        gws = core._chat_agents.get(core.TRIAL_AGENT_ID)
        chat_connected = bool(core.TRIAL_AGENT_ID) and gws is not None and not gws.closed
        bridge_connected = False
        if core.HEADLESS_AGENT_ID:
            bridge_connected = await core._check_relay_agent(core.HEADLESS_AGENT_ID)
        connected = chat_connected and bridge_connected
        guest_resp = web.json_response(
            {
                "connected": connected,
                "agent_id": guest_auth.get("agent_id", ""),
                "chat_connected": chat_connected,
                "chat_agent_id": core.TRIAL_AGENT_ID or "",
                "bridge_connected": bridge_connected,
                "bridge_agent_id": core.HEADLESS_AGENT_ID or "",
                "bridge_configured": bool(core.HEADLESS_AGENT_ID),
                "guest": True,
            },
        )
        core._attach_first_look_guest_cookies(guest_resp, request, guest_id)
        return guest_resp
    bridge_agent_id = auth_info.get("agent_id", "")
    agent_id = bridge_agent_id
    ws = core._chat_agents.get(agent_id)
    chat_connected = ws is not None and not ws.closed
    connected = chat_connected
    chat_only = request.query.get("chat_only") == "1"
    bridge_connected = False
    if bridge_agent_id:
        bridge_connected = await core._check_relay_agent(bridge_agent_id)
    if not connected and agent_id and not chat_only:
        connected = bridge_connected

    model_hint = request.query.get("model", "")
    if core._is_pending_user(auth_info) and model_hint and not core._is_openrouter_model(model_hint):
        return core._pending_limited_response()
    wants_gemini = request.query.get("gemini") == "1"
    wants_codex = (
        request.query.get("codex") == "1"
        or core._is_codex_cli_model(model_hint)
        or core._is_codex_sdk_model(model_hint)
    )
    wants_claude_sdk = core._is_claude_sdk_model(model_hint) or request.query.get("claude_sdk") == "1"

    gemini_connected = False
    if wants_gemini:
        gemini_id = f"gemini-{auth_info['key_hash']}"
        proc = core._gemini_procs.get(gemini_id)
        if not proc or proc.poll() is not None:
            import signup_agent

            user_id = auth_info.get("user_id", "")
            gemini_key = signup_agent.get_provider_key(user_id, "gemini") if user_id else None
            if gemini_key:
                core._spawn_gemini_agent(user_id, auth_info["key"], gemini_key)
        gws = core._chat_agents.get(gemini_id)
        gemini_connected = gws is not None and not gws.closed

    codex_connected = False
    codex_agent_id = ""
    codex_cli_supported = True
    if wants_codex:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]

        sdk_key = signup_agent.get_provider_key(user_id, "codex-sdk") if user_id else None
        cli_key = signup_agent.get_provider_key(user_id, "codex-cli") if user_id else None
        prefer_cli = core._is_codex_cli_model(model_hint)

        if prefer_cli:
            codex_agent_id = auth_info.get("agent_id", "")
            cws = core._chat_agents.get(codex_agent_id)
            codex_chat_connected = cws is not None and not cws.closed
            codex_connected = codex_chat_connected
            if not codex_connected and codex_agent_id and not chat_only:
                codex_connected = await core._check_relay_agent(codex_agent_id)
            caps = core._chat_agent_caps.get(codex_agent_id, {})
            codex_cli_supported = bool(caps.get("codex_cli"))
            if codex_connected and not codex_cli_supported:
                codex_connected = False
        else:
            codex_key = sdk_key or cli_key
            if codex_key:
                codex_agent_id = f"codexsdk-{key_hash}"
                proc = core._codex_sdk_procs.get(codex_agent_id)
                if not proc or proc.poll() is not None:
                    core._spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)

        if codex_agent_id and not prefer_cli:
            cws = core._chat_agents.get(codex_agent_id)
            codex_connected = cws is not None and not cws.closed
        if prefer_cli:
            chat_connected = codex_chat_connected
            connected = codex_connected
            agent_id = codex_agent_id

    claude_sdk_connected = False
    claude_sdk_agent_id = ""
    if wants_claude_sdk:
        import signup_agent

        user_id = auth_info.get("user_id", "")
        key_hash = auth_info["key_hash"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk") if user_id else None
        if claude_key:
            claude_sdk_agent_id = f"claudesdk-{key_hash}"
            proc = core._claude_sdk_procs.get(claude_sdk_agent_id)
            if not proc or proc.poll() is not None:
                core._spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
            cws = core._chat_agents.get(claude_sdk_agent_id)
            claude_sdk_connected = cws is not None and not cws.closed

            chat_connected = claude_sdk_connected
            connected = claude_sdk_connected
            agent_id = claude_sdk_agent_id

    mismatch_agent = ""
    if not chat_connected and agent_id:
        user_id = auth_info.get("user_id", "")
        if user_id:
            for other_id, other_uid in core._chat_agent_users.items():
                if other_uid == user_id and other_id != agent_id:
                    other_ws = core._chat_agents.get(other_id)
                    if other_ws and not other_ws.closed:
                        mismatch_agent = other_id
                        break

    local_client_agent_id = auth_info.get("agent_id", "")
    local_client_ws = core._chat_agents.get(local_client_agent_id) if local_client_agent_id else None
    local_client_connected = local_client_ws is not None and not local_client_ws.closed

    resp = {"connected": connected, "agent_id": agent_id}
    resp["chat_connected"] = chat_connected
    resp["chat_agent_id"] = agent_id
    resp["bridge_connected"] = bridge_connected
    resp["bridge_agent_id"] = bridge_agent_id
    resp["client_agent_id"] = local_client_agent_id
    resp["client_connected"] = local_client_connected
    resp.update(_client_version_status(core._chat_agent_caps.get(local_client_agent_id, {})))
    if mismatch_agent:
        resp["mismatch"] = True
        resp["mismatch_agent_id"] = mismatch_agent
    if wants_gemini:
        resp["gemini_agent_id"] = f"gemini-{auth_info['key_hash']}"
        resp["gemini_connected"] = gemini_connected
    if wants_codex:
        resp["codex_agent_id"] = codex_agent_id
        resp["codex_connected"] = codex_connected
        if core._is_codex_cli_model(model_hint):
            resp["codex_cli_supported"] = codex_cli_supported
    if wants_claude_sdk:
        resp["claude_sdk_agent_id"] = claude_sdk_agent_id
        resp["claude_sdk_connected"] = claude_sdk_connected
    return web.json_response(resp)


async def handle_chat_update_client(request: web.Request) -> web.Response:
    """POST /web/chat/update-client — ask the local client package to self-update."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    agent_id = str(auth_info.get("agent_id", "") or "").strip()
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return web.json_response({"error": "Your local client is offline."}, status=503)

    caps = core._chat_agent_caps.get(agent_id, {})
    if not bool(caps.get("remote_update")):
        return web.json_response(
            {
                "error": (
                    "This local client package does not support one-click updates yet. "
                    "Run the installer or update script once manually, then retry."
                )
            },
            status=409,
        )
    version_status = _client_version_status(caps)
    if not bool(version_status.get("client_outdated")):
        return web.json_response(
            {
                "error": "Your local client is already current.",
                "client_version": str(version_status.get("client_version") or "").strip(),
                "server_version": str(version_status.get("server_version") or "").strip(),
            },
            status=409,
        )

    resp = await agent_request(agent_id, {"type": "update_client"}, timeout=4)
    if not resp:
        return web.json_response(
            {"error": "Timed out waiting for the local client to start updating."},
            status=504,
        )
    if resp.get("type") == "update_client_ok":
        return web.json_response(
            {
                "ok": True,
                "status": str(resp.get("status") or "updating"),
                "client_version": str(caps.get("client_version") or "").strip(),
            }
        )
    return web.json_response(
        {"error": str(resp.get("error") or "Local client update failed to start.")},
        status=500,
    )


async def handle_chat_install_research_desk(request: web.Request) -> web.Response:
    """POST /web/chat/install-research-desk — ask the local client to install Research Desk."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)

    agent_id = str(auth_info.get("agent_id", "") or "").strip()
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    ws = core._chat_agents.get(agent_id)
    if ws is None or ws.closed:
        return web.json_response({"error": "Your local client is offline."}, status=503)

    caps = core._chat_agent_caps.get(agent_id, {})
    if not bool(caps.get("remote_research_desk_install")):
        return web.json_response(
            {
                "error": (
                    "This local client package does not support one-click Research Desk install yet. "
                    "Run the Research Desk install steps manually, then retry."
                )
            },
            status=409,
        )
    install_version = _research_desk_install_requires_update(caps)
    if bool(install_version.get("update_required")):
        client_version = str(install_version.get("client_version") or "unknown").strip() or "unknown"
        required_version = str(install_version.get("required_client_version") or _RESEARCH_DESK_INSTALL_MIN_CLIENT_VERSION)
        if bool(install_version.get("update_supported")):
            error = (
                "Your local client is still on {client_version}. Update it to at least {required_version}, "
                "then retry Research Desk install."
            ).format(client_version=client_version, required_version=required_version)
        else:
            error = (
                "Your local client is still on {client_version} and cannot self-update here. "
                "Run the installer or update script until it reaches at least {required_version}, then retry."
            ).format(client_version=client_version, required_version=required_version)
        return web.json_response(
            {
                "error": error,
                "update_required": True,
                "update_supported": bool(install_version.get("update_supported")),
                "client_version": client_version,
                "required_client_version": required_version,
            },
            status=409,
        )

    resp = await agent_request(agent_id, {"type": "install_research_desk"}, timeout=4)
    if not resp:
        return web.json_response(
            {"error": "Timed out waiting for the local client to start the Research Desk install."},
            status=504,
        )
    if resp.get("type") == "install_research_desk_ok":
        return web.json_response(
            {
                "ok": True,
                "status": str(resp.get("status") or "installing"),
                "launcher_prefix": str(resp.get("launcher_prefix") or "python3 -m unchained_pyreplab"),
            }
        )
    return web.json_response(
        {"error": str(resp.get("error") or "Local Research Desk install failed to start.")},
        status=500,
    )


async def handle_chat_history(request: web.Request) -> web.Response:
    """GET /web/chat/history — proxy to agent for local chat history."""
    core = _core()
    auth_info = core._authenticate(request)
    guest_mode = False
    guest_id = ""
    if not auth_info:
        if request.query.get("first_look_guest") != "1":
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, guest_id, _ = core._first_look_guest_auth(request)
        guest_mode = True
    agent_id = auth_info.get("agent_id", "")
    model = request.query.get("model", "")
    if (guest_mode or core._is_pending_user(auth_info)) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    if guest_mode and not core._is_openrouter_model(model):
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    requested_session_id = request.query.get("session_id", "")
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)

    if core._is_openrouter_model(model):
        session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
        msgs, found = core._read_trial_history(session_id)
        payload = {"messages": msgs, "trial": True, "session_id": session_id}
        if not found:
            payload["offline"] = True
        if guest_mode:
            payload["guest"] = True
        history_resp = web.json_response(payload)
        if guest_mode:
            core._attach_first_look_guest_cookies(history_resp, request, guest_id)
        return history_resp

    resp = await core._agent_request(
        chat_agent_id, {"type": "get_history", "session_id": requested_session_id}
    )
    if resp is not None:
        payload = {"messages": resp.get("messages", [])}
        if resp.get("session_id"):
            payload["session_id"] = resp["session_id"]
        return web.json_response(payload)

    session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
    msgs, found = core._read_trial_history(session_id)
    if found:
        return web.json_response({"messages": msgs, "trial": True, "session_id": session_id})
    return web.json_response({"messages": [], "offline": True})


async def handle_chat_new(request: web.Request) -> web.Response:
    """POST /web/chat/new — proxy to agent to advance to next slot."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        body = {}
    auth_info = core._authenticate(request)
    if not auth_info:
        if not bool(body.get("first_look_guest")):
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, _, _ = core._first_look_guest_auth(request)
    agent_id = auth_info.get("agent_id", "")

    model = body.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    requested_session_id = body.get("session_id", "")
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        old_session = core._resolve_trial_session_id(agent_id, requested_session_id)
        core._delete_trial_session(old_session)
        await core._close_session_tab(old_session)
        new_session = f"s-{agent_id}-{int(core.time.time() * 1000):x}"
        return web.json_response(
            {
                "ok": True,
                "active_slot": 1,
                "trial": True,
                "session_id": new_session,
            }
        )

    resp = await core._agent_request(chat_agent_id, {"type": "new_chat"})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    result = {"ok": True, "active_slot": resp.get("active_slot", 1)}
    if resp.get("session_id"):
        result["session_id"] = resp["session_id"]
    return web.json_response(result)


async def handle_chat_slots(request: web.Request) -> web.Response:
    """GET /web/chat/slots — get slot info from agent."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")

    model = request.query.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    requested_session_id = request.query.get("session_id", "")
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        session_id = core._resolve_trial_session_id(agent_id, requested_session_id)
        msgs, _ = core._read_trial_history(session_id)
        preview = ""
        for m in msgs:
            if m.get("role") == "user":
                preview = m.get("content", "")[:40]
                break
        return web.json_response(
            {
                "active_slot": 1,
                "slots": [
                    {"slot": 1, "empty": len(msgs) == 0, "preview": preview},
                    {"slot": 2, "empty": True, "preview": ""},
                    {"slot": 3, "empty": True, "preview": ""},
                ],
                "trial": True,
                "session_id": session_id,
            }
        )

    resp = await core._agent_request(chat_agent_id, {"type": "get_slots"})
    if resp is None:
        return web.json_response(
            {
                "active_slot": 1,
                "slots": [
                    {"slot": 1, "empty": True, "preview": ""},
                    {"slot": 2, "empty": True, "preview": ""},
                    {"slot": 3, "empty": True, "preview": ""},
                ],
                "offline": True,
            }
        )
    return web.json_response(
        {
            "active_slot": resp.get("active_slot", 1),
            "slots": resp.get("slots", []),
        }
    )


async def handle_chat_switch(request: web.Request) -> web.Response:
    """POST /web/chat/switch — switch active slot."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    slot = body.get("slot", 1)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    if core._is_pending_user(auth_info) and not core._is_openrouter_model(model):
        return core._pending_limited_response()
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    if core._is_openrouter_model(model):
        return web.json_response({"ok": True, "active_slot": 1, "trial": True})
    resp = await core._agent_request(chat_agent_id, {"type": "switch_slot", "slot": slot})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    return web.json_response({"ok": True, "active_slot": resp.get("active_slot", slot)})


async def handle_chat_archives(request: web.Request) -> web.Response:
    """GET /web/chat/archives — list archived conversations from agent."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    agent_id = auth_info.get("agent_id", "")
    model = request.query.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    resp = await agent_request(chat_agent_id, {"type": "get_archives"})
    if resp is None:
        return web.json_response({"archives": [], "offline": True})
    return web.json_response({"archives": resp.get("archives", [])})


async def handle_chat_restore_archive(request: web.Request) -> web.Response:
    """POST /web/chat/restore-archive — restore an archived conversation."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    archive_id = body.get("archive_id", "")
    if not archive_id:
        return web.json_response({"error": "archive_id required"}, status=400)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    resp = await _agent_request_after_reconnect(
        chat_agent_id, {"type": "restore_archive", "archive_id": archive_id}
    )
    if resp is None:
        return web.json_response(
            {"error": "Your local client is reconnecting. Wait a few seconds and retry."},
            status=503,
        )
    if resp.get("type") == "restore_archive_error":
        return web.json_response({"error": resp.get("error", "Restore failed")}, status=404)
    payload = {"ok": True, "active_slot": resp.get("active_slot", 1)}
    if resp.get("session_id"):
        payload["session_id"] = resp["session_id"]
    return web.json_response(payload)


async def handle_chat_delete_archive(request: web.Request) -> web.Response:
    """POST /web/chat/delete-archive — delete an archived conversation."""
    core = _core()
    auth_info = core._authenticate(request)
    if not auth_info:
        return web.json_response({"error": "Not authenticated"}, status=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    archive_id = body.get("archive_id", "")
    if not archive_id:
        return web.json_response({"error": "archive_id required"}, status=400)
    agent_id = auth_info.get("agent_id", "")
    model = body.get("model", "")
    chat_agent_id = resolve_chat_agent_id(auth_info, model) if model else agent_id
    resp = await agent_request(chat_agent_id, {"type": "delete_archive", "archive_id": archive_id})
    if resp is None:
        return web.json_response({"error": "Agent not connected"}, status=503)
    if resp.get("type") == "delete_archive_error":
        return web.json_response({"error": "Archive not found or could not be deleted"}, status=404)
    return web.json_response({"ok": True})
