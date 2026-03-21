"""Chat transport handlers (WS + SSE) extracted from web.py."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
import uuid

from aiohttp import web


from web_app.core import get_core as _core


_SCHEDULER_TRIGGER_RE = re.compile(r"^/schedule(?:\s+|$)")


# ---------------------------------------------------------------------------
# Overlay copilot v2
# ---------------------------------------------------------------------------


def _broadcast_overlay(session_id: str, event: dict) -> None:
    """Push an event to the overlay WS subscriber for a session."""
    try:
        from web_app.handlers.overlay_ws import broadcast_to_overlay
        broadcast_to_overlay(session_id, event)
    except Exception:
        pass


def _inject_overlay(core, session_id: str, agent_id: str, tab_id: str,
                     prompt_text: str, user_id: str = "", slot: int | None = None) -> None:
    """Inject the overlay copilot into a concrete browser tab.

    Only injects when tab_id is a real tab (not "auto"). Creates or updates
    OverlaySessionState. Cleans up any previous bootstrap script before
    re-injecting.

    Must be called from a running asyncio event loop (uses create_task).
    """
    # Never inject on "auto" — we need a concrete tab to avoid cross-profile bleed
    if not tab_id or tab_id == "auto":
        return

    try:
        import cloud_tools
        from overlay_js import build_overlay_js, build_overlay_bootstrap_js
        from web_app.handlers.overlay_ws import mint_overlay_token
        from web_state import OverlaySessionState

        relay_host, relay_port = core._parse_relay()
        public_host = os.environ.get("PUBLIC_HOST", "api.unchainedsky.com")
        token = mint_overlay_token(session_id, user_id)

        overlay_js = build_overlay_js(
            token=token,
            relay_host=public_host,
            session_id=session_id,
            prompt_text=prompt_text[:500],
        )
        bootstrap_js = build_overlay_bootstrap_js(
            token=token,
            relay_host=public_host,
            session_id=session_id,
            prompt_text=prompt_text[:500],
        )

        # Create or update overlay state
        overlay = core._overlay_sessions.get(session_id)
        if not overlay:
            overlay = OverlaySessionState(
                session_id=session_id,
                agent_id=agent_id,
                tab_id=tab_id,
                user_id=user_id,
                slot=slot,
                token=token,
            )
            core._overlay_sessions[session_id] = overlay
        else:
            overlay.agent_id = agent_id
            overlay.tab_id = tab_id
            overlay.user_id = user_id
            overlay.token = token
            if slot is not None:
                overlay.slot = slot

        async def _inject():
            try:
                # Clean up previous bootstrap script if tab changed
                old_bootstrap = overlay.bootstrap_id
                if old_bootstrap:
                    try:
                        await cloud_tools.run_cdp_command(
                            agent_id, tab_id,
                            "Page.removeScriptToEvaluateOnNewDocument",
                            {"identifier": old_bootstrap},
                            relay_host, relay_port,
                        )
                    except Exception:
                        pass
                    overlay.bootstrap_id = ""

                # Inject overlay JS directly (uses fetch polling, not WS,
                # so CSP doesn't block it)
                await cloud_tools.run_js(agent_id, tab_id, overlay_js, relay_host, relay_port)
                # Persist across navigations
                result = await cloud_tools.run_cdp_command(
                    agent_id, tab_id,
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": bootstrap_js},
                    relay_host, relay_port,
                )
                if isinstance(result, dict) and result.get("identifier"):
                    overlay.bootstrap_id = result["identifier"]
                overlay.injected = True
                # Drain any events buffered before injection
                for evt in overlay.pending_events:
                    from web_app.handlers.overlay_ws import broadcast_to_overlay as _bo
                    _bo(session_id, evt)
                overlay.pending_events.clear()
                print(f"[overlay] Injected overlay for {session_id} on tab {tab_id[:12]}")
                # Start outbox poller for follow-up messages from overlay
                asyncio.create_task(_poll_outbox(
                    core, session_id, agent_id, tab_id, relay_host, relay_port))
            except Exception as e:
                print(f"[overlay] Injection failed: {e}")

        async def _poll_outbox(core, sid, aid, tid, rh, rp):
            """Poll the overlay's JS outbox for follow-up messages."""
            import cloud_tools as _ct
            import json as _j
            while True:
                await asyncio.sleep(2)
                o = core._overlay_sessions.get(sid)
                if not o or not o.injected:
                    break
                try:
                    raw = await _ct.run_js(
                        aid, tid,
                        "(function(){var q=window.__uc_overlay_outbox||[];window.__uc_overlay_outbox=[];return JSON.stringify(q)})()",
                        rh, rp,
                    )
                    if raw and raw != "[]":
                        msgs = _j.loads(raw)
                        for msg in msgs:
                            if msg.get("type") == "user_followup":
                                text = str(msg.get("message", "")).strip()
                                if text and len(text) <= 4000:
                                    from web_app.handlers.overlay_ws import _route_followup
                                    await _route_followup(core, sid, text)
                except Exception:
                    pass  # tab may have navigated

        import asyncio
        asyncio.create_task(_inject())
    except Exception as e:
        print(f"[overlay] Injection setup failed: {e}")


def _normalize_profile_path(raw: object) -> str:
    """Normalize optional profile path from client payloads."""
    return str(raw or "").strip()


def _extract_scheduler_turn(message: str, *, allow_trigger: bool = True) -> tuple[bool, str]:
    text = str(message or "").strip()
    if not allow_trigger:
        return False, text
    match = _SCHEDULER_TRIGGER_RE.match(text)
    if not match:
        return False, text
    remainder = text[match.end():].strip()
    if not remainder:
        remainder = "List my scheduled tasks and help me manage them."
    return True, remainder


def _scheduler_trigger_supported(*, guest_mode: bool, is_openrouter: bool) -> bool:
    return not guest_mode and not is_openrouter


async def _allowed_profile_paths(core, agent_id: str) -> set[str]:
    """Return server-validated profile paths for this user bridge."""
    import signup_agent

    profiles = signup_agent.list_chrome_profiles()
    if not profiles and agent_id:
        relay_host, relay_port = core._parse_relay()
        profiles = await core.provision_helpers.fetch_relay_profiles(
            agent_id=agent_id,
            relay_host=relay_host,
            relay_port=relay_port,
            headers=core._relay_auth_headers(),
        )
    allowed: set[str] = set()
    for profile in profiles:
        path = _normalize_profile_path(profile.get("profile_path") or profile.get("path"))
        if path:
            allowed.add(path)
    return allowed


async def _ensure_profile_tab(core, session_id: str, cdp_agent_id: str, profile_path: str) -> str:
    """Ensure session is pinned to a provision browser tab for selected profile."""
    current_tab = core._session_tabs.get(session_id, "")
    current_profile = core._session_profile_paths.get(session_id, "")
    pending_close = current_tab and current_tab in getattr(core, "_tabs_pending_close", {})
    if current_tab and current_profile == profile_path and not pending_close:
        core._session_last_active[session_id] = time.time()
        return current_tab

    if current_tab:
        await core._close_session_tab(session_id)

    relay_host, relay_port = core._parse_relay()
    import cloud_tools

    print(f"[profile] Provisioning Chrome for session {session_id} profile={os.path.basename(profile_path)}")
    launch = await cloud_tools.provision_launch(cdp_agent_id, profile_path, relay_host, relay_port)
    tab_id = str((launch or {}).get("tab_id", "")).strip()
    if not tab_id:
        raise RuntimeError("Profile browser launch did not return tab_id")

    core._session_tabs[session_id] = tab_id
    core._session_agent_map[session_id] = cdp_agent_id
    core._session_last_active[session_id] = time.time()
    core._session_profile_paths[session_id] = profile_path
    return tab_id


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
                    "update_client_ok",
                    "update_client_error",
                    "archives_response",
                    "restore_archive_ok",
                    "restore_archive_error",
                    "delete_archive_ok",
                    "delete_archive_error",
                ):
                    rq = core._agent_req_queues.get(req_id)
                    if rq:
                        await rq.put(data)
                    continue

                sid = data.get("session_id", "")
                q = core._response_queues.get(sid)
                if q:
                    expected_rid = core._response_req_ids.get(sid, "")
                    event_rid = data.get("req_id", "")
                    if event_rid and expected_rid and event_rid != expected_rid:
                        continue  # stale event from previous turn
                    await q.put(data)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        is_current = core._chat_agents.get(agent_id) is ws
        if is_current:
            del core._chat_agents[agent_id]
            core._chat_agent_caps.pop(agent_id, None)
            core._chat_agent_users.pop(agent_id, None)
        agent_sessions = []
        if is_current:
            agent_sessions = [
                sid
                for sid, aid in list(core._session_agent_map.items())
                if aid == agent_id
            ]
            for sid in agent_sessions:
                asyncio.create_task(core._close_session_tab(sid))
        if is_current:
            print(f"[chat] Agent {agent_id} disconnected, cleaning {len(agent_sessions)} session tabs")
        else:
            print(f"[chat] Agent {agent_id} stale connection closed (superseded by reconnect)")

    return ws


async def handle_chat_msg(request: web.Request) -> web.StreamResponse:
    """POST /web/chat — phone sends message, gets SSE stream back."""
    core = _core()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json body"}, status=400)

    auth_info = core._authenticate(request)
    guest_mode = False
    guest_id = ""
    guest_quota_count = 0
    guest_quota_increment = False
    if auth_info is None:
        wants_guest_first_look = bool(body.get("first_look_guest")) and bool(body.get("headless"))
        if not wants_guest_first_look:
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, guest_id, guest_quota_count = core._first_look_guest_auth(request)
        guest_mode = True
    if guest_mode and not core.HEADLESS_AGENT_ID:
        err = web.json_response({"error": "headless_bridge_not_configured"}, status=503)
        core._attach_first_look_guest_cookies(err, request, guest_id, quota_count=guest_quota_count)
        return err

    req_id = core._request_id(request)
    raw_message = body.get("message", "").strip()
    scheduler_armed, message = _extract_scheduler_turn(
        raw_message,
        allow_trigger=bool(body.get("allow_scheduler_trigger", True)),
    )
    agent_id = auth_info.get("agent_id")
    key_hash = auth_info["key_hash"]
    session_id = body.get("session_id", "")
    model = body.get("model", "")
    slot = body.get("slot")
    if (guest_mode or core._is_pending_user(auth_info)) and not model:
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
    if guest_mode and not is_openrouter:
        return web.json_response(
            {"error": "guest_mode_requires_openrouter_model"},
            status=400,
        )
    if core._is_pending_user(auth_info) and not is_openrouter:
        return core._pending_limited_response()
    if scheduler_armed and not _scheduler_trigger_supported(
        guest_mode=guest_mode,
        is_openrouter=is_openrouter,
    ):
        return web.json_response(
            {
                "error": "scheduler_trigger_requires_local_bridge_lane",
                "message": "The /schedule trigger is supported only on bridge-backed local agent lanes, not the guest/trial OpenRouter lane.",
            },
            status=400,
        )
    openrouter_forced_model = ""
    openrouter_forced_from_model = ""
    openrouter_forced_notice = ""
    openrouter_budget_state: dict | None = None
    chat_agent_id = core._resolve_chat_agent_id(auth_info, model)
    selected_profile_path = _normalize_profile_path(body.get("profile_path"))

    def _session_owned(sid: str) -> bool:
        parts = sid.split("-")
        return len(parts) >= 4 and parts[0] == "s" and parts[2] == key_hash

    if not session_id:
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    elif not _session_owned(session_id):
        session_id = f"s-{chat_agent_id}-{uuid.uuid4().hex[:8]}"
    scheduler_grant_id = ""
    if scheduler_armed:
        scheduler_grant_id = core._mint_scheduler_turn_grant(auth_info.get("user_id", ""), session_id)
    analytics_route = core._analytics_route_from_request(request) or request.path

    core._track_event(
        request,
        "chat_message_send",
        session_id=core._analytics_session_id_from_request(request) or session_id,
        page_view_id=core._analytics_page_view_id_from_request(request),
        route="/web/chat",
        route_intended=body.get("route_intended", analytics_route),
        route_effective=body.get("route_effective", analytics_route),
        user_id=auth_info.get("user_id", ""),
        user_type=auth_info.get("user_type", ""),
        source="web",
        meta={
            "model": model or "",
            "headless": bool(body.get("headless", False)),
            "scheduler_armed": scheduler_armed,
            "chat_session_id": session_id,
        },
        status_code=200,
    )

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
        if guest_mode:
            if guest_quota_count >= core._FIRST_LOOK_GUEST_PROMPT_LIMIT:
                quota_resp = web.json_response(
                    {
                        "error": "demo_quota_exceeded",
                        "demo_prompt_count": guest_quota_count,
                        "demo_prompt_limit": core._FIRST_LOOK_GUEST_PROMPT_LIMIT,
                    },
                    status=429,
                )
                core._attach_first_look_guest_cookies(
                    quota_resp, request, guest_id, quota_count=guest_quota_count,
                )
                return quota_resp
            guest_quota_count += 1
            guest_quota_increment = True
        else:
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
    old_q = core._response_queues.get(session_id)
    if old_q is not None:
        # Signal the previous turn's SSE stream to end cleanly
        await old_q.put({"type": "done"})
    core._response_queues[session_id] = q
    core._response_req_ids[session_id] = req_id

    use_headless = body.get("headless", False) and core.HEADLESS_AGENT_ID
    if use_headless and selected_profile_path:
        core._response_queues.pop(session_id, None)
        return web.json_response(
            {"error": "Profile selection is not supported in headless mode."},
            status=400,
        )

    cdp_agent_id = core.HEADLESS_AGENT_ID if use_headless else agent_id

    tab_id = core._session_tabs.get(session_id)
    if selected_profile_path:
        allowed_paths = await _allowed_profile_paths(core, agent_id)
        if selected_profile_path not in allowed_paths:
            core._response_queues.pop(session_id, None)
            return web.json_response({"error": "Selected profile is invalid or unavailable."}, status=403)
        try:
            tab_id = await _ensure_profile_tab(core, session_id, cdp_agent_id, selected_profile_path)
        except Exception as e:
            core._response_queues.pop(session_id, None)
            return web.json_response({"error": f"Failed to launch selected profile: {e}"}, status=502)
    elif tab_id and str(tab_id).startswith("prov-"):
        await core._close_session_tab(session_id)
        tab_id = None

    try:
        ws_msg = {
            "type": "user_message",
            "session_id": session_id,
            "agent_id": cdp_agent_id,
            "message": message,
            "req_id": req_id,
        }
        if slot is not None:
            ws_msg["slot"] = slot
        if scheduler_armed:
            ws_msg["scheduler_armed"] = True
            ws_msg["scheduler_grant_id"] = scheduler_grant_id
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
        if not tab_id and use_headless:
            tab_id = await core._ensure_session_tab(session_id, cdp_agent_id)
        if tab_id:
            ws_msg["tab_id"] = tab_id
        if not selected_profile_path:
            core._session_profile_paths.pop(session_id, None)
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
        if scheduler_grant_id:
            core._scheduler_turn_grants.pop(scheduler_grant_id, None)
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

    # --- Inject overlay copilot into the task browser ---
    # Only inject when we have a concrete tab_id (not None/"auto")
    if tab_id and not guest_mode and not is_openrouter:
        _inject_overlay(core, session_id, cdp_agent_id, tab_id, message,
                        user_id=auth_info.get("user_id", ""), slot=slot)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    if guest_mode:
        core._attach_first_look_guest_cookies(
            resp,
            request,
            guest_id,
            quota_count=guest_quota_count if guest_quota_increment else None,
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

            # Broadcast to overlay copilot subscribers
            _broadcast_overlay(session_id, evt)

            if evt.get("type") == "done" or evt.get("type") == "error":
                stream_completed = True
                break
    finally:
        # Only clean up if we still own the queue (a new turn may have replaced it)
        if core._response_queues.get(session_id) is q:
            core._response_queues.pop(session_id, None)
            core._response_req_ids.pop(session_id, None)
            # Keep session_agents alive if overlay has an active subscriber
            overlay = core._overlay_sessions.get(session_id)
            has_overlay = overlay and overlay.subscriber is not None
            if not has_overlay:
                core._session_agents.pop(session_id, None)
            if not stream_completed:
                # Abnormal disconnect — clean up overlay state too
                core._overlay_sessions.pop(session_id, None)
                asyncio.create_task(core._close_session_tab(session_id))
        if scheduler_grant_id:
            core._scheduler_turn_grants.pop(scheduler_grant_id, None)
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
    try:
        body = await request.json()
    except Exception:
        body = {}
    auth_info = core._authenticate(request)
    guest_mode = False
    guest_id = ""
    if auth_info is None:
        if not bool(body.get("first_look_guest")):
            return web.json_response({"error": "Not authenticated"}, status=401)
        auth_info, guest_id, _ = core._first_look_guest_auth(request)
        guest_mode = True
    session_id = body.get("session_id", "")
    if not session_id:
        return web.json_response({"error": "session_id required"}, status=400)

    agent_id = auth_info.get("agent_id", "")
    if guest_mode and not session_id.startswith(f"s-{agent_id}-"):
        denied = web.json_response({"error": "session_id not owned by guest"}, status=403)
        core._attach_first_look_guest_cookies(denied, request, guest_id)
        return denied
    default_agent = core.TRIAL_AGENT_ID if guest_mode else agent_id
    routing_agent_id = core._session_agents.get(session_id, default_agent)

    # Capture queue ref before awaiting WS send (queue may be replaced by a new turn)
    cancel_q = core._response_queues.get(session_id)
    cancel_rid = core._response_req_ids.get(session_id, "")

    ws = core._chat_agents.get(routing_agent_id)
    if ws and not ws.closed:
        await ws.send_json({"type": "cancel", "session_id": session_id})

    # Inject cancelled+done into the SSE queue so the client sees a clean end
    if cancel_q is not None:
        cancelled_evt = {"type": "cancelled", "session_id": session_id}
        done_evt = {"type": "done", "session_id": session_id}
        if cancel_rid:
            cancelled_evt["req_id"] = cancel_rid
            done_evt["req_id"] = cancel_rid
        await cancel_q.put(cancelled_evt)
        await cancel_q.put(done_evt)
        ok = web.json_response({"ok": True})
        if guest_mode:
            core._attach_first_look_guest_cookies(ok, request, guest_id)
        return ok

    if ws and not ws.closed:
        ok = web.json_response({"ok": True})
        if guest_mode:
            core._attach_first_look_guest_cookies(ok, request, guest_id)
        return ok
    err = web.json_response({"error": "Agent not connected"}, status=503)
    if guest_mode:
        core._attach_first_look_guest_cookies(err, request, guest_id)
    return err
