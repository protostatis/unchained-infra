"""Chat status/history/slot handlers extracted from web.py."""

from __future__ import annotations

import asyncio
import uuid

import httpx
from aiohttp import web


from web_app.core import get_core as _core


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
        return web.json_response({"messages": resp.get("messages", [])})

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
