"""Chat transport handlers (WS + SSE) extracted from web.py."""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid

from aiohttp import web


def _core():
    import web as core

    return core


async def handle_chat_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket endpoint for the local chat agent."""
    core = _core()
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    try:
        auth_msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, TypeError):
        await ws.close(code=4001, message=b"auth timeout")
        return ws

    key = auth_msg.get("key", "")
    is_trial_agent = bool(core.TRIAL_AGENT_KEY) and hmac.compare_digest(key, core.TRIAL_AGENT_KEY)
    if not is_trial_agent:
        key_info = core._auth.validate_key(key)
        if not key_info:
            await ws.send_json({"type": "error", "data": "invalid key"})
            await ws.close(code=4003, message=b"invalid key")
            return ws

    if is_trial_agent and core.TRIAL_AGENT_ID:
        agent_id = core.TRIAL_AGENT_ID
    elif auth_msg.get("agent_id"):
        agent_id = auth_msg["agent_id"]
    else:
        agent_id = f"claude-{core._key_hash(key)}"
    caps = auth_msg.get("capabilities", {})
    if not isinstance(caps, dict):
        caps = {}
    await ws.send_json({"type": "auth_ok"})
    core._chat_agents[agent_id] = ws
    core._chat_agent_caps[agent_id] = caps
    if not is_trial_agent and key_info:
        core._chat_agent_users[agent_id] = key_info.get("user_id", "")
    print(f"[chat] Agent {agent_id} connected")

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type", "")

                if msg_type == "openrouter_usage":
                    try:
                        usage_user_id = str(data.get("user_id", "")).strip()
                        usage_sid = str(data.get("session_id", "")).strip()
                        usage_model = str(data.get("model", "")).strip()
                        prompt_tokens = max(0, core._coerce_int(data.get("prompt_tokens"), 0))
                        completion_tokens = max(0, core._coerce_int(data.get("completion_tokens"), 0))
                        total_tokens = max(0, core._coerce_int(data.get("total_tokens"), 0))
                        if total_tokens <= 0:
                            total_tokens = prompt_tokens + completion_tokens
                        direct_cost = core._coerce_float(data.get("cost_usd"), 0.0)
                        estimated_cost = core._coerce_float(data.get("estimated_cost_usd"), 0.0)
                        usage_cost = max(0.0, direct_cost if direct_cost > 0 else estimated_cost)
                        if usage_user_id and (usage_cost > 0 or total_tokens > 0):
                            budget_state = core._track_openrouter_usage_for_user(
                                usage_user_id,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=usage_cost,
                            )
                            core._trace(
                                "openrouter.usage",
                                user_id=usage_user_id,
                                session_id=usage_sid,
                                model=usage_model or "-",
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=f"{usage_cost:.9f}",
                                estimated_cost_usd=f"{estimated_cost:.9f}",
                                spend_usd=f"{core._coerce_float(budget_state.get('spent_usd'), 0.0):.6f}",
                                budget_usd=f"{core._coerce_float(budget_state.get('budget_usd'), 0.0):.6f}",
                                remaining_usd=f"{core._coerce_float(budget_state.get('remaining_usd'), 0.0):.6f}",
                                usage_events=core._coerce_int(budget_state.get("usage_events"), 0),
                                total_user_tokens=core._coerce_int(budget_state.get("total_tokens"), 0),
                                capped=int(bool(budget_state.get("capped"))),
                            )
                    except Exception as e:
                        core.log.warning("[chat] failed to track OpenRouter usage: %s", e)
                    continue

                req_id = data.get("req_id", "")
                if req_id and msg_type in (
                    "history_response",
                    "new_chat_ok",
                    "switch_slot_ok",
                    "slots_response",
                ):
                    rq = core._agent_req_queues.get(req_id)
                    if rq:
                        await rq.put(data)
                    continue

                sid = data.get("session_id", "")
                q = core._response_queues.get(sid)
                if q:
                    await q.put(data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        if core._chat_agents.get(agent_id) is ws:
            del core._chat_agents[agent_id]
        core._chat_agent_caps.pop(agent_id, None)
        core._chat_agent_users.pop(agent_id, None)
        agent_sessions = [
            sid
            for sid, aid in list(core._session_agent_map.items())
            if aid == agent_id
        ]
        for sid in agent_sessions:
            asyncio.create_task(core._close_session_tab(sid))
        print(f"[chat] Agent {agent_id} disconnected, cleaning {len(agent_sessions)} session tabs")

    return ws


async def handle_chat_msg(request: web.Request) -> web.StreamResponse:
    """POST /web/chat — phone sends message, gets SSE stream back."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    body = await request.json()
    req_id = core._request_id(request)
    message = body.get("message", "").strip()
    agent_id = auth_info.get("agent_id")
    key_hash = auth_info["key_hash"]
    session_id = body.get("session_id", "")
    model = body.get("model", "")
    if core._is_pending_user(auth_info) and not model:
        model = core._OPENROUTER_TRIAL_DEFAULT_MODEL
    core._trace(
        "chat.msg.in",
        req_id=req_id,
        user_id=auth_info.get("user_id", ""),
        agent_id=agent_id,
        session_id=session_id or "-",
        model=model or "-",
    )

    if not message:
        return web.json_response({"error": "message required"}, status=400)
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)

    is_gemini = model and model.startswith("gemini")
    is_claude_sdk = core._is_claude_sdk_model(model)
    is_codex_sdk = core._is_codex_sdk_model(model)
    is_codex_cli = core._is_codex_cli_model(model)
    is_openrouter = core._is_openrouter_model(model)
    if core._is_pending_user(auth_info) and not is_openrouter:
        return core._pending_limited_response()
    openrouter_forced_model = ""
    openrouter_forced_from_model = ""
    openrouter_forced_notice = ""
    openrouter_budget_state: dict | None = None
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)

    def _session_owned(sid: str) -> bool:
        parts = sid.split("-")
        return len(parts) >= 4 and parts[0] == "s" and parts[2] == key_hash

    if not session_id:
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    elif not _session_owned(session_id):
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"

    if is_gemini:
        import signup_agent

        gemini_key = signup_agent.get_provider_key(auth_info["user_id"], "gemini")
        if not gemini_key:
            return web.json_response(
                {"error": "No Gemini API key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_gemini_agent(auth_info["user_id"], auth_info["key"], gemini_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Gemini API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_claude_sdk:
        import signup_agent

        user_id = auth_info["user_id"]
        claude_key = signup_agent.get_provider_key(user_id, "claude-sdk")
        if not claude_key:
            return web.json_response(
                {"error": "No Claude API key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_claude_sdk_agent(user_id, auth_info["key"], claude_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            for _wait in range(20):
                await asyncio.sleep(0.5)
                ws = core._chat_agents.get(chat_agent_id)
                if ws is not None and not ws.closed:
                    break
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Claude API agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_sdk:
        import signup_agent

        user_id = auth_info["user_id"]
        primary = "codex-sdk"
        fallback = "codex-cli"
        codex_key = signup_agent.get_provider_key(user_id, primary) or signup_agent.get_provider_key(
            user_id, fallback
        )
        if not codex_key:
            return web.json_response(
                {"error": "No Codex key. Visit /setup to provision one."},
                status=400,
            )
        core._spawn_codex_sdk_agent(user_id, auth_info["key"], codex_key)
        ws = core._chat_agents.get(chat_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Codex agent starting up. Try again in a few seconds."},
                status=503,
            )
    elif is_codex_cli:
        local_agent_id = auth_info["agent_id"]
        ws = core._chat_agents.get(local_agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {
                    "error": "Codex CLI requires your local agent connection. Open /app and connect your agent."
                },
                status=503,
            )
        caps = core._chat_agent_caps.get(local_agent_id, {})
        if not bool(caps.get("codex_cli")):
            return web.json_response(
                {
                    "error": "Your local agent does not support Codex CLI yet. Please update/restart your local agent package and try again."
                },
                status=426,
            )
    elif is_openrouter:
        if not core.TRIAL_AGENT_ID:
            return web.json_response(
                {"error": "Trial agent is not configured. Please try a Claude model."},
                status=503,
            )
        user_id = auth_info.get("user_id", "")
        requested_model = (model or core._OPENROUTER_TRIAL_DEFAULT_MODEL).strip()
        model = requested_model
        if user_id:
            openrouter_budget_state = core._openrouter_budget_state_for_user(user_id)
            if openrouter_budget_state.get("capped") and not core._is_openrouter_post_cap_allowed_model(
                requested_model
            ):
                model = core._OPENROUTER_TRIAL_FALLBACK_MODEL
                openrouter_forced_model = model
                openrouter_forced_from_model = requested_model
                openrouter_forced_notice = (
                    "Trial model budget reached "
                    f"(${openrouter_budget_state.get('spent_usd', 0):.2f}/"
                    f"${openrouter_budget_state.get('budget_usd', 0):.2f}). "
                    "Switched to a free model for continued access. "
                    "After the $1 cap, available models are Trinity and StepFun."
                )
        ws = core._chat_agents.get(core.TRIAL_AGENT_ID)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Trial agent is not available. Please try a Claude model."},
                status=503,
            )
    else:
        ws = core._chat_agents.get(agent_id)
        if ws is None or ws.closed:
            return web.json_response(
                {"error": "Your agent is not connected. Download and run the agent package."},
                status=503,
            )

    if body.get("headless", False):
        email = auth_info.get("email", "")
        if email:
            user = core._auth.find_user_by_email(email)
            if not core._is_demo_unlimited(user):
                count = core._auth.get_demo_count(email)
                if count >= core._DEMO_PROMPT_LIMIT:
                    return web.json_response(
                        {
                            "error": "demo_quota_exceeded",
                            "demo_prompt_count": count,
                            "demo_prompt_limit": core._DEMO_PROMPT_LIMIT,
                        },
                        status=429,
                    )
                core._auth.increment_demo_count(email)

    email = auth_info.get("email", "")
    if email:
        user = core._auth.find_user_by_email(email)
        if core._is_rate_limited_user(user):
            result = core._auth.check_and_consume_turn(
                email,
                core._FREE_DAILY_TURN_LIMIT,
                core._FREE_WINDOW_TURN_LIMIT,
                core._FREE_WINDOW_SECONDS,
            )
            if not result["allowed"]:
                resp = {
                    "error": "turn_rate_limit",
                    "daily_remaining": result["daily_remaining"],
                    "window_remaining": result["window_remaining"],
                    "resets_in": result.get("resets_in", 0),
                }
                return web.json_response(resp, status=429)

    q: asyncio.Queue = asyncio.Queue()
    core._response_queues[session_id] = q

    use_headless = body.get("headless", False) and core.HEADLESS_AGENT_ID
    cdp_agent_id = core.HEADLESS_AGENT_ID if use_headless else agent_id
    try:
        ws_msg = {
            "type": "user_message",
            "session_id": session_id,
            "agent_id": cdp_agent_id,
            "message": message,
        }
        if model:
            ws_msg["model"] = model
        if is_openrouter and auth_info.get("user_id"):
            ws_msg["user_id"] = auth_info["user_id"]
        if is_gemini:
            core._gemini_last_active[chat_agent_id] = time.time()
        elif is_claude_sdk:
            core._claude_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_sdk:
            core._codex_sdk_last_active[chat_agent_id] = time.time()
        elif is_codex_cli:
            core._session_last_active[session_id] = time.time()
        tab_id = core._session_tabs.get(session_id)
        if not tab_id and use_headless:
            tab_id = await core._ensure_session_tab(session_id, cdp_agent_id)
        if tab_id:
            ws_msg["tab_id"] = tab_id
        core._session_last_active[session_id] = time.time()
        await ws.send_json(ws_msg)
        core._trace(
            "chat.msg.forwarded",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
            cdp_agent_id=cdp_agent_id,
        )
    except Exception:
        core._trace(
            "chat.msg.forward_error",
            req_id=req_id,
            session_id=session_id,
            chat_agent_id=chat_agent_id,
        )
        core._response_queues.pop(session_id, None)
        return web.json_response({"error": "Failed to reach chat agent"}, status=502)

    routing_agent_id = core.TRIAL_AGENT_ID if is_openrouter else chat_agent_id
    core._session_agents[session_id] = routing_agent_id

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)
    if openrouter_forced_model:
        forced_evt = {
            "type": "model_forced",
            "reason": "openrouter_budget_limit",
            "model": openrouter_forced_model,
            "allowed_models": list(core._OPENROUTER_TRIAL_POST_CAP_ALLOWED_MODELS),
        }
        if openrouter_forced_from_model:
            forced_evt["requested_model"] = openrouter_forced_from_model
        if openrouter_budget_state:
            forced_evt["budget"] = openrouter_budget_state
        await resp.write(f"data: {json.dumps(forced_evt)}\n\n".encode())
    if openrouter_forced_notice:
        await resp.write(
            f"data: {json.dumps({'type': 'text', 'data': openrouter_forced_notice})}\n\n".encode()
        )

    stream_completed = False
    try:
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    await resp.write(b": keepalive\n\n")
                except (ConnectionResetError, Exception):
                    break
                continue

            sse = f"data: {json.dumps(evt)}\n\n"
            try:
                await resp.write(sse.encode())
            except (ConnectionResetError, Exception):
                break

            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break
    finally:
        core._response_queues.pop(session_id, None)
        core._session_agents.pop(session_id, None)
        if not stream_completed:
            asyncio.create_task(core._close_session_tab(session_id))
        core._trace(
            "chat.msg.stream_end",
            req_id=req_id,
            session_id=session_id,
            stream_completed=stream_completed,
        )

    return resp


async def handle_chat_cancel(request: web.Request) -> web.Response:
    """POST /web/chat/cancel — cancel an active chat session."""
    core = _core()
    auth_info = core._authenticate(request)
    if auth_info is None:
        return web.json_response({"error": "Not authenticated"}, status=401)

    body = await request.json()
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)

    agent_id = auth_info.get("agent_id", "")
    routing_agent_id = core._session_agents.get(session_id, agent_id)
    ws = core._chat_agents.get(routing_agent_id)
    if ws and not ws.closed:
        await ws.send_json({"type": "cancel", "session_id": session_id})
        return web.json_response({"ok": True})
    return web.json_response({"error": "Agent not connected"}, status=503)
